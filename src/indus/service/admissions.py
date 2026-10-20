"""The admission service: the one door every surface uses to hand the daemon an input (P17-02).

``admit(principal, session, request_id, input)`` records the input in the runtime ledger
(``state.runtime_ledger``) and answers a ``Receipt``: the admission id, its state (``queued`` /
``started`` / ``terminal`` / ``unknown``), its generation once started and its FIFO place while
queued. One worker task per session claims the FIFO head, takes the durable turn lease for that
generation (P17-03, when a ``TurnLeases`` is given), runs the turn through the host's ``runner``
under the liveness watchdog (P17-04, when settings are given) and settles the admission with the
turn's outcome and final text. Then it takes the next input.

- **Exact retry.** The same request with the same input returns the receipt of the existing
  admission. When that admission is already terminal the receipt carries its stored final text
  (``replayed``): the client shows it once and nothing runs again. Another input under the same
  request id is refused (``admission_conflict``).
- **Lost acks.** ``receipt(admission_id)`` answers for a client that lost its ack; ``wait`` waits
  for an admission to leave ``queued`` / ``started``.
- **Controls.** ``cancel`` drops a queued input or stops the running turn (it settles
  ``cancelled`` or as the runner reports); ``steer(id, text)`` gives the running turn more text;
  ``steer_queued(id)`` hands a queued input to the running turn (the ledger's steer transfer:
  delivered → ``steered`` when the turn completes; not delivered → back in its FIFO place).
- **Unknown turns.** A turn started by an earlier daemon is ``unknown`` and never replayed; the
  session's FIFO pauses behind it. ``on_blocked(session_id, row)`` runs once per unknown admission
  (messaging tells the chat once per pause, R06.19). ``discard_unknown(id, generation)`` is the
  operator's resolution; a stale generation is refused (``stale_generation``). Its
  ``terminal_write`` (``close_lost_turn``) closes the lost turn in its transcript in the same
  transaction.
- **Gate.** ``gate(conn)``, when given, is the owner's admission gate (a drain refuses new
  work): it runs inside the ledger transaction of every new admission and every claim, and raises
  to refuse.
- **Fences.** With ``fences`` (P17-05) the worker reports each claim to the daemon's
  ``GenerationFences``: ``begin`` at claim, ``adopt`` with the ticket as the turn's controls (so a
  generation-checked interrupt or stop also ends a lease wait), ``release`` when the turn has
  ended, before the settle. A soft interrupt stops the turn with ``interrupted``, a hard one (and
  the fence's ``stop``) with ``cancelled``, a system one with ``interrupted_by_system``.
- **Failures.** A runner that raises settles ``failed`` with a cleaned detail. A lease that cannot
  be taken settles ``failed`` (``lease_timeout``); the turn never runs unserialised. A lost lease
  or a stall interrupts the turn (``lease_lost`` / ``stalled``). A settle that cannot be written
  leaves the admission ``started``: the next daemon start makes it ``unknown``.

Ledger calls run off the event loop (``offload``, the ``io`` pool by default). Refusals leave as
``LedgerRefusal`` with their bounded reason. Every method runs on the service's event loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol

from indus.foundation.blocking import run_blocking
from indus.foundation.clock import Clock
from indus.protocol.events import ExitReason
from indus.safety.redaction import redact_output
from indus.service.fences import GenerationFences
from indus.service.turn_lease import HeldLease, NotAcquired, TurnLeases, WaitInterrupt
from indus.service.turn_liveness import (
    STALLED,
    Activity,
    ActivityMeter,
    LivenessSettings,
    LivenessWatchdog,
)
from indus.state import messages
from indus.state import runtime_ledger as ledger
from indus.state.registry import StoreHandle
from indus.state.runtime_ledger import (
    AdmissionRow,
    AdmissionState,
    LedgerRefusal,
    Outcome,
    SettleOutcome,
)
from indus.state.sessions import END_COMPRESSION

__all__ = [
    "CANCELLED",
    "INTERRUPTED",
    "LEASE_TIMEOUT",
    "LOST_TURN_NOTE",
    "SHUTDOWN",
    "SYSTEM_INTERRUPT",
    "AdmissionService",
    "LedgerRefusal",
    "Offload",
    "Receipt",
    "TerminalWrite",
    "TurnOutcome",
    "TurnRunner",
    "TurnTicket",
    "close_lost_turn",
]

log = logging.getLogger(__name__)

CANCELLED: Final = "cancelled"
INTERRUPTED: Final = "interrupted"
SYSTEM_INTERRUPT: Final = ExitReason.INTERRUPTED_BY_SYSTEM.value
SHUTDOWN: Final = "shutdown"
LEASE_TIMEOUT: Final = "lease_timeout"
_DETAIL_CHARS: Final = 160


type TerminalWrite = Callable[[sqlite3.Connection, AdmissionRow], None]
"""The owner's effect when an unknown turn is discarded, inside the ledger transaction."""

LOST_TURN_NOTE: Final = (
    "[This turn was lost when the agent process stopped; its outcome is unknown and it was not "
    "run again.]"
)


def close_lost_turn(note: str = LOST_TURN_NOTE, *, now: Callable[[], float]) -> TerminalWrite:
    """A ``terminal_write`` that closes the lost turn in its transcript with ``note``.

    The note is an assistant message appended to the admission's session when that session's last
    active message is the person's (the lost turn ended without a reply). Nothing is written for a
    session with no row, no messages, a reply already in place, or one that was compressed into a
    continuation (its transcript is closed already)."""

    def write(conn: sqlite3.Connection, row: AdmissionRow) -> None:
        found = conn.execute(
            "SELECT ended_at, end_reason FROM sessions WHERE id = ?", (row.session_id,)
        ).fetchone()
        if found is None or (found[0] is not None and found[1] == END_COMPRESSION):
            return
        last = conn.execute(
            "SELECT role FROM messages WHERE session_id = ? AND active = 1 ORDER BY id DESC "
            "LIMIT 1",
            (row.session_id,),
        ).fetchone()
        if last is None or last[0] != "user":
            return
        messages.append_in(
            conn,
            row.session_id,
            {"role": "assistant", "content": note, "finish_reason": "lost"},
            now=now(),
        )

    return write


class Offload(Protocol):
    """Runs a blocking ledger call off the event loop."""

    def __call__[T](self, fn: Callable[[], T], /) -> Awaitable[T]: ...


async def _io_pool[T](fn: Callable[[], T]) -> T:
    return await run_blocking(fn, label="runtime ledger")


@dataclass(frozen=True)
class Receipt:
    """What a surface learns about its admission."""

    admission_id: str
    state: AdmissionState
    session_id: str
    """The session the admission belongs to (controls check it before acting)."""
    generation: int | None = None
    position: int | None = None
    """1-based FIFO place while queued."""
    outcome: Outcome | None = None
    final_text: str | None = None
    """Only on an exact retry of a terminal admission: the text to show once (``replayed``)."""
    replayed: bool = False
    blocked_by: str | None = None
    """The unknown admission this session's FIFO waits behind, if any."""
    retired: bool = False


@dataclass(frozen=True)
class TurnOutcome:
    """What the runner reports when its turn ends."""

    outcome: SettleOutcome
    final_text: str | None = None
    detail: str | None = None


class TurnTicket:
    """The running claim the runner works on: its admission, generation and controls."""

    def __init__(self, admission: AdmissionRow, meter: ActivityMeter) -> None:
        assert admission.generation is not None
        self.admission = admission
        self.generation: int = admission.generation
        self.meter = meter
        self.stop_reason: str | None = None
        self.stopped = asyncio.Event()
        self.transfers: dict[str, bool] = {}
        self._interrupt: Callable[[str], object] | None = None
        self._steer: Callable[[str], bool] | None = None
        self.lease_wait = WaitInterrupt()
        """Ends this turn's wait for the session lease when the turn is stopped first."""
        self.lease_waited = False
        """True when the lease was won only after another holder was seen: that holder may have
        changed the session, so the runner reloads it before running (P17-08)."""

    @property
    def session_id(self) -> str:
        return self.admission.session_id

    @property
    def text(self) -> str:
        return ledger.payload_text(self.admission)

    def bind(
        self,
        *,
        interrupt: Callable[[str], object] | None = None,
        steer: Callable[[str], bool] | None = None,
    ) -> None:
        """The runner's hooks: how to stop its turn, how to give it more text."""
        if interrupt is not None:
            self._interrupt = interrupt
        if steer is not None:
            self._steer = steer

    def interrupt(self, reason: str) -> bool:
        """Stop the turn (first reason wins); must not block (the watchdog calls it under a
        lock). False when it was already stopped."""
        if self.stop_reason is not None:
            return False
        self.stop_reason = reason
        self.stopped.set()
        self.lease_wait.stop()
        hook = self._interrupt
        if hook is not None:
            try:
                hook(reason)
            except Exception:
                log.warning("interrupting turn %s failed", self.generation, exc_info=True)
        return True

    def steer(self, text: str) -> bool:
        if self.stop_reason is not None or self._steer is None:
            return False
        try:
            return bool(self._steer(text))
        except Exception:
            log.warning("steering turn %s failed", self.generation, exc_info=True)
            return False

    def touch(self, note: str = "") -> None:
        """Activity for the liveness watchdog (any thread)."""
        self.meter.touch(note)


class _FencedTurn:
    """A ticket as the generation fence's ``TurnControls``: calls for another generation do
    nothing; interrupts before the runner binds are kept by the ticket (``stop_reason``)."""

    def __init__(self, ticket: TurnTicket) -> None:
        self._ticket = ticket

    def interrupt(self, generation: int, *, hard: bool = False, by_system: bool = False) -> bool:
        if generation != self._ticket.generation:
            return False
        reason = SYSTEM_INTERRUPT if by_system else CANCELLED if hard else INTERRUPTED
        return self._ticket.interrupt(reason)

    def steer(self, text: str) -> bool:
        return self._ticket.steer(text)


type TurnRunner = Callable[[TurnTicket], Awaitable[TurnOutcome]]


class AdmissionService:
    """The daemon's admission door and per-session workers (see the module notes)."""

    def __init__(
        self,
        store: StoreHandle,
        *,
        epoch: int,
        runner: TurnRunner,
        clock: Clock | None = None,
        leases: TurnLeases | None = None,
        liveness: LivenessSettings | None = None,
        waiting: Callable[[str], bool] | None = None,
        on_blocked: Callable[[str, AdmissionRow], object] | None = None,
        offload: Offload | None = None,
        gate: Callable[[sqlite3.Connection], None] | None = None,
        fences: GenerationFences | None = None,
    ) -> None:
        self.store = store
        self.epoch = epoch
        self.runner = runner
        self.clock: Clock = clock if clock is not None else store.scope.clock
        self.leases = leases
        self.liveness = liveness
        self._waiting = waiting
        self._on_blocked = on_blocked
        self._off: Offload = offload if offload is not None else _io_pool
        self._gate = gate
        self.fences = fences
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._dirty: set[str] = set()
        self._running: dict[str, TurnTicket] = {}
        self._told: set[str] = set()
        self._settled: dict[str, asyncio.Event] = {}
        self._closing = False

    @classmethod
    async def open(
        cls,
        store: StoreHandle,
        *,
        instance_id: str,
        runner: TurnRunner,
        offload: Offload | None = None,
        **options: Any,
    ) -> AdmissionService:
        """Begin a new owner epoch (the caller holds the profile ownership lock), then restart
        the workers of sessions that still hold queued input."""
        off: Offload = offload if offload is not None else _io_pool
        start = await off(lambda: ledger.begin_owner_epoch(store, instance_id))
        if start.unknown:
            log.warning(
                "%d turn(s) were running when the previous owner stopped; their outcome is "
                "unknown and they will not be run again",
                len(start.unknown),
            )
        service = cls(store, epoch=start.epoch, runner=runner, offload=off, **options)
        for session_id in await off(lambda: ledger.sessions_with_queued(store)):
            service._kick(session_id)
        return service

    # -- surfaces ---------------------------------------------------------------------------------

    async def admit(
        self,
        principal_id: str,
        session_id: str,
        request_id: str,
        input: Mapping[str, Any],
        *,
        replaces: str | None = None,
    ) -> Receipt:
        """Admit ``input`` (see the module notes); refusals raise ``LedgerRefusal``."""
        self._check_open()
        payload = dict(input)
        result = await self._off(
            lambda: ledger.admit(
                self.store,
                epoch=self.epoch,
                principal_id=principal_id,
                session_id=session_id,
                request_id=request_id,
                payload=payload,
                replaces=replaces,
                authorize=self._gate,
            )
        )
        row = result.row
        if not result.created and row.terminal:
            return self._terminal_receipt(row, replayed=True)
        self._kick(session_id)
        return await self._receipt_for(row)

    async def receipt(self, admission_id: str) -> Receipt:
        row = await self._row(admission_id)
        if row.terminal:
            return self._terminal_receipt(row, replayed=False)
        return await self._receipt_for(row)

    async def wait(self, admission_id: str) -> Receipt:
        """Wait until the admission is terminal or unknown."""
        while True:
            row = await self._row(admission_id)
            if row.state in ("terminal", "unknown"):
                return await self.receipt(admission_id)
            event = self._settled.setdefault(admission_id, asyncio.Event())
            await event.wait()

    def running(self, session_id: str) -> TurnTicket | None:
        return self._running.get(session_id)

    async def cancel(self, admission_id: str) -> Receipt:
        """Drop a queued input, or stop the running turn of a started one."""
        row = await self._row(admission_id)
        if row.state == "started":
            ticket = self._running.get(row.session_id)
            if ticket is not None and ticket.admission.admission_id == admission_id:
                ticket.interrupt(CANCELLED)
            return await self._receipt_for(row)
        settled = await self._off(
            lambda: ledger.cancel(self.store, epoch=self.epoch, admission_id=admission_id)
        )
        self._mark_settled(admission_id)
        return self._terminal_receipt(settled, replayed=False)

    async def steer(self, admission_id: str, text: str) -> bool:
        """Give the running turn of ``admission_id`` more text; False when it is not running."""
        row = await self._row(admission_id)
        ticket = self._running.get(row.session_id)
        if ticket is None or ticket.admission.admission_id != admission_id:
            return False
        return ticket.steer(text)

    async def steer_queued(self, admission_id: str) -> Receipt:
        """Hand a queued input to its session's running turn (see the module notes)."""
        row = await self._row(admission_id)
        ticket = self._running.get(row.session_id)
        if ticket is None:
            raise LedgerRefusal("session_idle", field="admission_id")
        generation = ticket.generation
        moved, committed = await self._off(
            lambda: ledger.steer_into(
                self.store, epoch=self.epoch, admission_id=admission_id, generation=generation
            )
        )
        if committed:
            if ticket.steer(ledger.payload_text(moved)):
                ticket.transfers[admission_id] = True
            else:
                await self._off(
                    lambda: ledger.revert_steer(
                        self.store,
                        epoch=self.epoch,
                        admission_id=admission_id,
                        generation=generation,
                    )
                )
                self._kick(row.session_id)
        return await self.receipt(admission_id)

    async def discard_unknown(
        self,
        admission_id: str,
        expected_generation: int,
        *,
        terminal_write: TerminalWrite | None = None,
    ) -> Receipt:
        """Resolve a turn lost in a restart (generation checked); the FIFO then moves on.

        ``terminal_write(conn, row)`` runs in the discard's own transaction (``close_lost_turn``
        writes a short note into the lost turn's transcript); when it raises nothing changes."""
        settled = await self._off(
            lambda: ledger.resolve_unknown(
                self.store,
                epoch=self.epoch,
                admission_id=admission_id,
                generation=expected_generation,
                terminal_write=terminal_write,
            )
        )
        self._told.discard(admission_id)
        self._mark_settled(admission_id)
        self._kick(settled.session_id)
        return self._terminal_receipt(settled, replayed=False)

    async def aclose(self) -> None:
        """Stop taking input, interrupt running turns, wait for the workers."""
        self._closing = True
        for ticket in list(self._running.values()):
            ticket.interrupt(SHUTDOWN)
        workers = list(self._workers.values())
        for task in workers:
            with contextlib.suppress(BaseException):
                await task

    # -- receipts ---------------------------------------------------------------------------------

    async def _row(self, admission_id: str) -> AdmissionRow:
        row = await self._off(lambda: ledger.get(self.store, admission_id))
        if row is None:
            raise LedgerRefusal("not_found", field="admission_id")
        return row

    def _terminal_receipt(self, row: AdmissionRow, *, replayed: bool) -> Receipt:
        return Receipt(
            admission_id=row.admission_id,
            state="terminal",
            session_id=row.session_id,
            generation=row.generation,
            outcome=row.outcome,
            final_text=row.final_text if replayed else None,
            replayed=replayed,
            retired=row.retired,
        )

    async def _receipt_for(self, row: AdmissionRow) -> Receipt:
        rows = await self._off(lambda: ledger.pending(self.store, row.session_id))
        current = next((r for r in rows if r.admission_id == row.admission_id), row)
        blocked = next((r.admission_id for r in rows if r.state == "unknown"), None)
        if current.terminal:
            return self._terminal_receipt(current, replayed=False)
        return Receipt(
            admission_id=current.admission_id,
            state=current.state,
            session_id=current.session_id,
            generation=current.generation,
            position=ledger.queue_position(rows, current.admission_id),
            blocked_by=blocked if current.state == "queued" else None,
        )

    def _mark_settled(self, admission_id: str) -> None:
        event = self._settled.pop(admission_id, None)
        if event is not None:
            event.set()

    # -- workers ----------------------------------------------------------------------------------

    def _check_open(self) -> None:
        if self._closing:
            raise RuntimeError("the admission service is closed")

    def _kick(self, session_id: str) -> None:
        if self._closing:
            return
        self._dirty.add(session_id)
        task = self._workers.get(session_id)
        if task is None or task.done():
            self._workers[session_id] = asyncio.get_running_loop().create_task(
                self._work(session_id), name=f"admissions {session_id}"
            )

    async def _work(self, session_id: str) -> None:
        try:
            while not self._closing:
                self._dirty.discard(session_id)
                claim = await self._off(
                    lambda: ledger.claim_head(
                        self.store, epoch=self.epoch, session_id=session_id, guard=self._gate
                    )
                )
                if claim.kind == "claimed" and claim.row is not None:
                    await self._run(claim.row)
                    continue
                if claim.kind == "moved":
                    continue
                if claim.kind == "blocked" and claim.row is not None:
                    self._tell_blocked(session_id, claim.row)
                if session_id in self._dirty and claim.kind == "idle":
                    continue
                return
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("the admission worker of session %s failed", session_id)
        finally:
            if self._workers.get(session_id) is asyncio.current_task():
                del self._workers[session_id]

    def _tell_blocked(self, session_id: str, row: AdmissionRow) -> None:
        if row.admission_id in self._told:
            return
        self._told.add(row.admission_id)
        log.warning(
            "session %s waits behind turn %s, whose outcome was lost in a restart",
            session_id,
            row.admission_id,
        )
        if self._on_blocked is not None:
            try:
                self._on_blocked(session_id, row)
            except Exception:
                log.warning("the blocked-session notice failed", exc_info=True)

    async def _run(self, row: AdmissionRow) -> None:
        meter = ActivityMeter(self.clock)
        ticket = TurnTicket(row, meter)
        session_id = row.session_id
        self._running[session_id] = ticket
        lease: HeldLease | None = None
        outcome = TurnOutcome("failed", detail="not run")
        watchdog: LivenessWatchdog | None = None
        fences = self.fences
        fenced = False
        try:
            if fences is not None:
                fences.begin(session_id, ticket.generation)
                fenced = True
                fences.adopt(session_id, ticket.generation, _FencedTurn(ticket))
            if self.leases is not None:
                got = await self.leases.acquire(
                    session_id, generation=ticket.generation, interrupt=ticket.lease_wait
                )
                if isinstance(got, NotAcquired):
                    outcome = self._not_run(ticket, got)
                else:
                    lease = got
                    ticket.lease_waited = got.waited
                    lease.start(on_lost=ticket.interrupt)
            if lease is not None or self.leases is None:
                meter.begin(ticket.generation)
                watchdog = self._watchdog(ticket, lease)
                outcome = await self._call_runner(ticket)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # the lease or the watchdog failed around the turn
            log.warning("turn %s of session %s could not run", ticket.generation, session_id)
            outcome = TurnOutcome("failed", detail=self._detail(exc))
        finally:
            if fenced and fences is not None:
                fences.release(session_id, ticket.generation)
            if watchdog is not None:
                watchdog.stop()
            meter.end(ticket.generation)
        try:
            await self._settle(ticket, outcome)
            if lease is not None:
                await lease.release()
        finally:
            if self._running.get(session_id) is ticket:
                del self._running[session_id]
            self._mark_settled(row.admission_id)

    def _not_run(self, ticket: TurnTicket, got: NotAcquired) -> TurnOutcome:
        """The turn never got the session lease: it did not run at all."""
        if got.reason == "timeout":
            return TurnOutcome("failed", detail=LEASE_TIMEOUT)
        reason = ticket.stop_reason or "interrupted"
        return TurnOutcome("cancelled" if reason == CANCELLED else "interrupted", detail=reason)

    def _watchdog(self, ticket: TurnTicket, lease: HeldLease | None) -> LivenessWatchdog | None:
        if self.liveness is None or self.liveness.timeout_s is None:
            return None
        waiting = self._waiting
        session_id = ticket.session_id

        def abort(seen: Activity) -> bool:
            del seen
            return ticket.interrupt(STALLED)

        watchdog = LivenessWatchdog(
            ticket.meter,
            self.liveness,
            abort=abort,
            clock=self.clock,
            waiting=None if waiting is None else (lambda: waiting(session_id)),
            deactivate=None if lease is None else lease.stop_refresh,
            label=session_id,
        )
        watchdog.start()
        return watchdog

    async def _call_runner(self, ticket: TurnTicket) -> TurnOutcome:
        try:
            outcome = await self.runner(ticket)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("turn %s of session %s failed", ticket.generation, ticket.session_id)
            return TurnOutcome("failed", detail=self._detail(exc))
        if outcome.outcome == "interrupted" and outcome.detail is None and ticket.stop_reason:
            return TurnOutcome("interrupted", outcome.final_text, ticket.stop_reason)
        return outcome

    def _detail(self, value: object) -> str | None:
        """A failure detail fit to store and show: secrets masked (the scope's own values too),
        paths shortened, one printable line."""
        if value is None:
            return None
        masked = redact_output(str(value), force=True, scope=self.store.scope)
        return ledger.clean_text(masked, _DETAIL_CHARS) or "error"

    async def _settle(self, ticket: TurnTicket, outcome: TurnOutcome) -> None:
        transfers = dict(ticket.transfers)
        detail = self._detail(outcome.detail)
        try:
            await self._off(
                lambda: ledger.settle(
                    self.store,
                    epoch=self.epoch,
                    admission_id=ticket.admission.admission_id,
                    generation=ticket.generation,
                    outcome=outcome.outcome,
                    final_text=outcome.final_text,
                    detail=detail,
                    transfers=transfers,
                )
            )
        except Exception:
            log.error(
                "could not record the end of turn %s of session %s; it will read as unknown "
                "after a restart",
                ticket.generation,
                ticket.session_id,
                exc_info=True,
            )
