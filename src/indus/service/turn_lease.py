"""The durable turn lease: one process at a time loads, runs and flushes a session (P17-03).

``TurnLeases.acquire(session_id)`` takes the session's row in ``turn_leases``
(``state.turn_leases``) or waits for it:

- **Waiting.** While another live holder has the row, the caller waits up to ``wait`` (1,800 s),
  re-trying every ``poll`` (1 s) and at once when a holder in this process releases. ``on_wait``
  is called at the first wait and every ``notice_every`` (15 s) after, with the seconds waited, so
  the person sees "waiting for another run". A busy store is not a holder: the attempt is simply
  repeated, with no notice. The wait ends early through a ``WaitInterrupt``: ``stop()`` drops the
  waiting input, ``follow_up()`` carries it (``NotAcquired.carried``) into the follow-up's turn —
  an accepted input is never consumed by a wait it did not win. Timing out never runs the turn.
- **Holding.** ``HeldLease.start(on_lost)`` renews the row every ``refresh_every`` (60 s). A renewal
  waits at most ``write_patience`` (20 s) for the write lock and never past ``deadline −
  expiry_margin`` (2 s): ``deadline`` is the committed expiry, moved only by a renewal that
  committed (stamped before its lock wait, so it is never later than the row's). A renewal that
  finds the row gone or re-owned, a failure that is not lock contention, or lock contention that
  leaves no time for the next tick loses the lease: ``on_lost(LEASE_LOST)`` runs once and the host
  interrupts the turn. After ``stop_refresh()`` or ``release()`` nothing counts as a loss.
- **Releasing.** ``release()`` deletes the row only while it is still this holder's for this
  generation (R06.2) and wakes waiters in this process.

Holders in this process share a table of per-session slots (wake-ups, waiter counts) capped at
``max_local`` (512) entries; only idle slots are evicted, so live ones may pass the cap briefly.

Store writes go through the store's writer executor (``StoreHandle.awrite``); the loop never
blocks on SQLite (D-04.1). Lease timings are the reference's (research 01 §3.23).
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Final, Literal, Protocol

from indus.foundation.clock import Clock
from indus.foundation.errors import StoreLocked
from indus.state import turn_leases
from indus.state.errors import is_lock_error
from indus.state.registry import StoreHandle
from indus.state.turn_leases import Acquired, LeaseHolder, LeaseRow, ProcessProbe
from indus.state.writer import Budget

__all__ = [
    "LEASE_LOST",
    "STILL_WAITING_TEXT",
    "TIMEOUT_TEXT",
    "WAIT_TEXT",
    "HeldLease",
    "LeaseSettings",
    "NotAcquired",
    "TurnLeases",
    "WaitInterrupt",
    "wait_text",
]

log = logging.getLogger(__name__)

LEASE_LOST: Final = "lease_lost"
WAIT_TEXT: Final = (
    "Another Indus Agent process is running this session; your turn starts as soon as it finishes."
)
STILL_WAITING_TEXT: Final = "Still waiting for the other process on this session ({seconds} s)."
TIMEOUT_TEXT: Final = (
    "Another process kept this session busy for too long, so your message was not run. Send it "
    "again once that run has finished."
)


def wait_text(elapsed: float) -> str:
    """The status line for a wait of ``elapsed`` seconds."""
    return WAIT_TEXT if elapsed < 1.0 else STILL_WAITING_TEXT.format(seconds=int(elapsed))


@dataclass(frozen=True)
class LeaseSettings:
    ttl: float = 300.0
    refresh_every: float = 60.0
    write_patience: float = 20.0
    expiry_margin: float = 2.0
    wait: float = 1800.0
    poll: float = 1.0
    notice_every: float = 15.0
    acquire_patience: float = 2.0
    max_local: int = 512


class Writes(Protocol):
    """Runs ``fn(conn)`` in one write transaction off the loop (``StoreHandle.awrite``)."""

    def __call__[T](
        self, fn: Callable[[sqlite3.Connection], T], budget: Budget, /
    ) -> Awaitable[T]: ...


class WaitInterrupt:
    """Ends a lease wait: ``stop()`` drops the waiting input, ``follow_up()`` carries it."""

    def __init__(self) -> None:
        self.kind: Literal["stop", "follow_up"] | None = None
        self._event = asyncio.Event()

    def stop(self) -> None:
        if self.kind is None:
            self.kind = "stop"
        self._event.set()

    def follow_up(self) -> None:
        if self.kind is None:
            self.kind = "follow_up"
        self._event.set()

    @property
    def is_set(self) -> bool:
        return self.kind is not None

    async def wait(self) -> None:
        await self._event.wait()


@dataclass(frozen=True)
class NotAcquired:
    """The lease was not taken: ``interrupted`` (a stop or a follow-up) or ``timeout``.

    ``waited``: another holder was seen (the person was told). ``carried``: the inputs the
    follow-up's turn must take over (empty after a stop or a timeout)."""

    reason: Literal["interrupted", "timeout"]
    waited: bool
    carried: tuple[str, ...] = ()


@dataclass
class _Slot:
    event: asyncio.Event = field(default_factory=asyncio.Event)
    waiters: int = 0
    held: int = 0
    last_used: float = 0.0

    @property
    def idle(self) -> bool:
        return self.waiters == 0 and self.held == 0

    def wake(self) -> None:
        old, self.event = self.event, asyncio.Event()
        old.set()


class _Slots:
    def __init__(self, cap: int) -> None:
        self.cap = max(1, cap)
        self.slots: dict[str, _Slot] = {}

    def get(self, session_id: str, now: float) -> _Slot:
        slot = self.slots.get(session_id)
        if slot is None:
            self._evict_idle()
            slot = self.slots[session_id] = _Slot()
        slot.last_used = now
        return slot

    def _evict_idle(self) -> None:
        overflow = len(self.slots) - self.cap + 1
        if overflow <= 0:
            return
        idle = sorted(
            (sid for sid, slot in self.slots.items() if slot.idle),
            key=lambda sid: self.slots[sid].last_used,
        )
        for sid in idle[:overflow]:
            del self.slots[sid]


class TurnLeases:
    """Acquires durable turn leases for one store (see the module notes)."""

    def __init__(
        self,
        store: StoreHandle,
        *,
        clock: Clock | None = None,
        settings: LeaseSettings | None = None,
        probe: ProcessProbe | None = None,
        write: Writes | None = None,
    ) -> None:
        self.store = store
        self.clock: Clock = clock if clock is not None else store.scope.clock
        self.settings = settings or LeaseSettings()
        self.probe = probe
        self._write: Writes = write if write is not None else self._awrite
        self._slots = _Slots(self.settings.max_local)

    async def _awrite[T](self, fn: Callable[[sqlite3.Connection], T], budget: Budget) -> T:
        return await self.store.awrite(fn, budget=budget)

    def wall(self) -> float:
        return self.clock.now().timestamp()

    @property
    def local_slot_count(self) -> int:
        return len(self._slots.slots)

    def _is_dead(self, row: LeaseRow) -> bool:
        return turn_leases.holder_is_dead(row, self.probe)

    async def acquire(
        self,
        session_id: str,
        *,
        generation: int | None = None,
        on_wait: Callable[[float], object] | None = None,
        interrupt: WaitInterrupt | None = None,
        pending_input: str | None = None,
        holder: LeaseHolder | None = None,
    ) -> HeldLease | NotAcquired:
        """Take the lease, waiting for another holder (see the module notes)."""
        me = holder if holder is not None else LeaseHolder.current()
        cfg = self.settings
        started = self.clock.monotonic()
        deadline = started + cfg.wait
        waited = False
        contended = False
        last_notice: float | None = None
        slot = self._slots.get(session_id, started)
        slot.waiters += 1
        try:
            while True:
                if interrupt is not None and interrupt.is_set:
                    return self._interrupted(interrupt, waited, pending_input, session_id)
                attempt = functools.partial(
                    turn_leases.try_acquire_in,
                    session_id=session_id,
                    holder=me,
                    now=self.wall(),
                    ttl=cfg.ttl,
                    generation=generation,
                    is_dead=self._is_dead,
                )
                try:
                    result = await self._write(
                        attempt, Budget("lease_acquire", cfg.acquire_patience)
                    )
                except (StoreLocked, sqlite3.OperationalError) as exc:
                    if isinstance(exc, sqlite3.OperationalError) and not is_lock_error(exc):
                        raise
                    # a busy store is not a holder: try again at once, no "waiting" notice
                    # (but the busy writer may have been a holder's last flush: reload after)
                    contended = True
                    if self.clock.monotonic() >= deadline:
                        return self._timed_out(session_id, waited)
                    await self._pause(slot, interrupt, 0.0)
                    continue
                if isinstance(result, Acquired):
                    contended = contended or result.reclaimed_from is not None
                    if result.reclaimed_from is not None:
                        log.warning(
                            "took over the turn lease of session %s from a holder that expired "
                            "or is gone (%s)",
                            session_id,
                            result.reclaimed_from,
                        )
                    slot.held += 1
                    return HeldLease(self, result.row, me, slot, waited=waited or contended)
                mono = self.clock.monotonic()
                if mono >= deadline:
                    return self._timed_out(session_id, waited)
                if on_wait is not None and (
                    last_notice is None or mono - last_notice >= cfg.notice_every
                ):
                    try:
                        on_wait(max(0.0, mono - started))
                    except Exception:
                        log.debug("lease wait notice failed", exc_info=True)
                    last_notice = mono
                waited = True
                await self._pause(slot, interrupt, min(cfg.poll, deadline - mono))
        finally:
            slot.waiters -= 1
            slot.last_used = self.clock.monotonic()

    def _interrupted(
        self,
        interrupt: WaitInterrupt,
        waited: bool,
        pending_input: str | None,
        session_id: str,
    ) -> NotAcquired:
        carried: tuple[str, ...] = ()
        if interrupt.kind == "follow_up" and pending_input:
            carried = (pending_input,)
        log.info("turn lease wait on session %s ended by a %s", session_id, interrupt.kind)
        return NotAcquired("interrupted", waited=waited, carried=carried)

    def _timed_out(self, session_id: str, waited: bool) -> NotAcquired:
        log.error(
            "gave up waiting %.0f s for the turn lease of session %s; the turn was not run",
            self.settings.wait,
            session_id,
        )
        return NotAcquired("timeout", waited=waited)

    async def _pause(self, slot: _Slot, interrupt: WaitInterrupt | None, delay: float) -> None:
        waits: list[asyncio.Future[Any]] = [
            asyncio.ensure_future(self.clock.sleep(max(0.0, delay))),
            asyncio.ensure_future(slot.event.wait()),
        ]
        if interrupt is not None:
            waits.append(asyncio.ensure_future(interrupt.wait()))
        try:
            await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in waits:
                task.cancel()
            for task in waits:
                with contextlib.suppress(BaseException):
                    await task


class HeldLease:
    """A lease this process holds (see the module notes)."""

    def __init__(
        self,
        owner: TurnLeases,
        row: LeaseRow,
        holder: LeaseHolder,
        slot: _Slot,
        *,
        waited: bool = False,
    ) -> None:
        self._owner = owner
        self.waited = waited
        """Another holder (or a busy store, or a reclaimed row) was met first: the session may
        have changed meanwhile, so the host reloads it before running the turn."""
        self.session_id = row.session_id
        self.holder = holder
        self.generation = row.generation
        self.deadline = row.expires_at
        """Wall-clock seconds: the committed expiry, never later than the row's."""
        self.lost_reason: str | None = None
        self.released = False
        self._slot = slot
        self._stopped = False
        self._on_lost: Callable[[str], object] | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def holder_id(self) -> str:
        return self.holder.holder_id

    @property
    def lost(self) -> bool:
        return self.lost_reason is not None

    def start(self, on_lost: Callable[[str], object] | None = None) -> None:
        """Start the renewal timer; ``on_lost(LEASE_LOST)`` runs once if the lease is lost."""
        self._on_lost = on_lost
        if self._task is None and not self._stopped:
            self._task = asyncio.get_running_loop().create_task(
                self._renew_loop(), name=f"turn lease {self.session_id}"
            )

    def stop_refresh(self) -> None:
        """Stop renewing (the turn is over or abandoned); the row then expires on its own."""
        self._stopped = True
        task, self._task = self._task, None
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    async def _renew_loop(self) -> None:
        every = self._owner.settings.refresh_every
        while not self._stopped:
            await self._owner.clock.sleep(every)
            if self._stopped or not await self.refresh_once():
                return

    async def refresh_once(self) -> bool:
        """One renewal; False when renewing should stop (lost, or stopped)."""
        if self._stopped:
            return False
        cfg = self._owner.settings
        started = self._owner.wall()
        patience = self.deadline - cfg.expiry_margin - started
        if patience <= 0:
            log.warning(
                "the turn lease of session %s could not be renewed before it expires",
                self.session_id,
            )
            self._lose()
            return False
        try:
            renewed = await self._owner._write(  # pyright: ignore[reportPrivateUsage]
                lambda conn: turn_leases.refresh_in(
                    conn,
                    self.session_id,
                    self.holder_id,
                    self.generation,
                    now=started,
                    ttl=cfg.ttl,
                ),
                Budget("lease_refresh", min(cfg.write_patience, patience)),
            )
        except Exception as exc:
            if self._stopped:
                return False
            locked = isinstance(exc, StoreLocked) or (
                isinstance(exc, sqlite3.OperationalError) and is_lock_error(exc)
            )
            if locked:
                if self._owner.wall() + cfg.refresh_every + cfg.expiry_margin < self.deadline:
                    log.warning(
                        "renewing the turn lease of session %s met a busy store; trying again",
                        self.session_id,
                    )
                    return True
                log.warning(
                    "the store stayed busy until the turn lease of session %s runs out",
                    self.session_id,
                )
            else:
                log.warning(
                    "renewing the turn lease of session %s failed", self.session_id, exc_info=True
                )
            self._lose()
            return False
        if renewed:
            self.deadline = started + cfg.ttl
            return True
        if self._stopped:
            return False
        log.error("lost the turn lease of session %s while its turn was running", self.session_id)
        self._lose()
        return False

    def _lose(self) -> None:
        if self.lost_reason is not None or self._stopped:
            return
        self.lost_reason = LEASE_LOST
        self._stopped = True
        callback, self._on_lost = self._on_lost, None
        if callback is not None:
            try:
                callback(LEASE_LOST)
            except Exception:
                log.warning("lease-loss callback failed", exc_info=True)

    async def release(self) -> bool:
        """Stop renewing and delete the row if it is still ours; idempotent."""
        if self.released:
            return False
        self.released = True
        task = self._task
        self.stop_refresh()
        if task is not None and task is not asyncio.current_task():
            with contextlib.suppress(BaseException):
                await task
        owner = self._owner
        try:
            removed = await owner._write(  # pyright: ignore[reportPrivateUsage]
                lambda conn: turn_leases.release_in(
                    conn, self.session_id, self.holder_id, self.generation
                ),
                Budget("lease_release", owner.settings.write_patience),
            )
        except Exception:
            log.error(
                "could not release the turn lease of session %s; it expires on its own",
                self.session_id,
                exc_info=True,
            )
            removed = False
        finally:
            self._slot.held = max(0, self._slot.held - 1)
            self._slot.last_used = owner.clock.monotonic()
            self._slot.wake()
        return removed
