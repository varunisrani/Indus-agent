"""Messaging on the shared host: the gateway intake's bridge to the daemon session host (P17-22).

The gateway daemon hosts its chat sessions on one ``DaemonSessionHost`` (P17-08), the same host
attached CLI and TUI clients use over the WebSocket. ``MessagingHost`` is what the intake
(``AdmissionPipeline``) and boot recovery see of it:

- **Sessions.** ``create`` / ``resume`` register a chat session with the host under the
  ``messaging`` source; ``close`` first cancels the session's queued admissions (a ``/new`` must
  never answer an old message later in the abandoned conversation), then ends it.
- **Input.** ``submit`` admits a chat message into the session's durable FIFO with busy mode
  ``queue`` (the intake has already applied its own busy rules) under the principal
  ``messaging:<platform>:<sender>`` and a request id the intake chose (its crash-marker token);
  ``wait`` waits until the admission is settled and reads the row back (``Settled``); ``stop``
  cancels a queued admission or stops exactly its running turn.
- **Requests.** ``watch`` attaches the intake as a ``messaging`` viewer of the session (one per
  session, connection id ``messaging:<key>``) that renders approval and clarify prompts; the host's
  request book offers those to it (and to every other viewer that renders them).
- **Turn ends.** ``owner_of(session, generation)`` maps a finished turn back to its admission: the
  request id (the intake's reply anchor), the admission id (its final gate) and whether the final
  is posted to the chat (``TurnOwner.deliver``: chat and host-initiated turns always; a turn an
  attached surface submitted only when its payload says ``deliver: true``).
- **Unknown turns.** ``unknown_rows`` / ``unknown_of`` / ``discard`` serve boot recovery;
  ``on_blocked`` (the admission service's callback) holds the notices raised before recovery ran
  until ``release_blocked`` hands them, still unknown, to the recovery's handler.

Every method runs on the daemon's loop; ledger reads run off it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from indus.foundation.blocking import run_blocking
from indus.protocol.methods import ProtocolError, SessionCreateParams
from indus.service.admissions import AdmissionService, Receipt
from indus.service.daemon_host import INTERNAL_PRINCIPAL, DaemonSessionHost
from indus.service.request_redelivery import Offer
from indus.service.viewers import Viewer
from indus.state import runtime_ledger as ledger
from indus.state.registry import StoreHandle
from indus.state.runtime_ledger import AdmissionRow, LedgerRefusal

__all__ = [
    "DELIVER_KEY",
    "MESSAGING_PRINCIPAL",
    "MESSAGING_SURFACE",
    "UNKNOWN_PAUSE_REASON",
    "MessagingHost",
    "Settled",
    "TurnOwner",
    "principal_for",
]

log = logging.getLogger(__name__)

MESSAGING_SURFACE: Final = "messaging"
MESSAGING_PRINCIPAL: Final = "messaging"
"""Principal prefix of chat input (``messaging:<platform>:<sender>``)."""
DELIVER_KEY: Final = "deliver"
"""Admission payload key: a turn another surface submitted into a chat's session is posted to the
chat only when it is true."""
UNKNOWN_PAUSE_REASON: Final = "unknown_turn"
"""``resume_reason`` of a chat route paused behind a turn whose outcome is unknown."""
_HELD_CAP: Final = 256

type BlockedHandler = Callable[[str, AdmissionRow], Awaitable[None]]
type OfferHandler = Callable[[str, Offer], object]


def principal_for(platform: str, sender: str) -> str:
    """The admission principal of a chat message (who wrote it, never what it may do)."""
    return f"{MESSAGING_PRINCIPAL}:{platform.lower()}:{sender}"


@dataclass(frozen=True, slots=True)
class Settled:
    """An admission as it ended (``state`` ``terminal``, or ``unknown`` after a restart)."""

    admission_id: str
    state: str
    outcome: str | None
    generation: int | None
    final_text: str | None
    detail: str | None


@dataclass(frozen=True, slots=True)
class TurnOwner:
    """The admission a finished turn ran for."""

    admission_id: str
    request_id: str
    principal_id: str
    deliver: bool


def _deliver(row: AdmissionRow) -> bool:
    principal = row.principal_id
    if principal.startswith((f"{MESSAGING_PRINCIPAL}:", f"{INTERNAL_PRINCIPAL}:")):
        return True
    return row.payload.get(DELIVER_KEY) is True


def _first_id(store: StoreHandle, sql: str, params: tuple[Any, ...]) -> str | None:
    with store.read() as conn:
        found = conn.execute(sql, params).fetchone()
    return None if found is None else str(found[0])


def _row_for_generation(
    store: StoreHandle, session_ids: tuple[str, ...], generation: int
) -> AdmissionRow | None:
    marks = ", ".join("?" for _ in session_ids)
    admission_id = _first_id(
        store,
        f"SELECT admission_id FROM runtime_admissions WHERE session_id IN ({marks}) "
        "AND generation = ? ORDER BY seq LIMIT 1",
        (*session_ids, generation),
    )
    return None if admission_id is None else ledger.get(store, admission_id)


def _row_for_request(store: StoreHandle, request_id: str) -> AdmissionRow | None:
    admission_id = _first_id(
        store,
        "SELECT admission_id FROM runtime_admissions WHERE request_id = ? "
        "ORDER BY seq DESC LIMIT 1",
        (request_id,),
    )
    return None if admission_id is None else ledger.get(store, admission_id)


def _unknown_rows(store: StoreHandle) -> list[AdmissionRow]:
    with store.read() as conn:
        ids = [
            str(r[0])
            for r in conn.execute(
                "SELECT admission_id FROM runtime_admissions WHERE state = 'unknown' ORDER BY seq"
            )
        ]
    rows = [ledger.get(store, admission_id) for admission_id in ids]
    return [row for row in rows if row is not None]


class MessagingHost:
    """The intake's view of the daemon session host (see the module notes)."""

    def __init__(self, store: StoreHandle) -> None:
        self.store = store
        self._host: DaemonSessionHost | None = None
        self._held: list[tuple[str, AdmissionRow]] = []
        self._handler: BlockedHandler | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    def bind(self, host: DaemonSessionHost) -> None:
        self._host = host

    @property
    def host(self) -> DaemonSessionHost:
        if self._host is None:
            raise RuntimeError("the messaging host is not bound to a session host")
        return self._host

    @property
    def admissions(self) -> AdmissionService:
        return self.host.admissions

    async def _off[T](self, fn: Callable[[], T]) -> T:
        return await run_blocking(fn, label="runtime ledger")

    # -- sessions ---------------------------------------------------------------------------------

    def hosted(self, session_id: str) -> bool:
        return self.host.key_of(session_id) is not None

    def key_of(self, session_id: str) -> str:
        """The host's key for ``session_id`` (the id itself when the host does not hold it)."""
        return self.host.key_of(session_id) or session_id

    async def create(self, params: SessionCreateParams) -> str:
        created = await self.host.create(params, source=MESSAGING_SURFACE)
        return created.session_id

    async def resume(self, session_id: str) -> str:
        """Attach a stored session to the host (or describe a hosted one); its current id."""
        resumed = await self.host.resume(session_id, source=MESSAGING_SURFACE)
        return resumed.session_id

    async def close(self, session_id: str) -> None:
        """Cancel the session's queued admissions, then end it on the host."""
        host = self.host
        key = host.key_of(session_id)
        if key is None:
            return
        rows = await self._off(lambda: ledger.pending(self.store, key))
        for row in rows:
            if row.state != "queued":
                continue
            with contextlib.suppress(LedgerRefusal, RuntimeError):
                await host.admissions.cancel(row.admission_id)
        await host.close_session(key)

    # -- viewer -----------------------------------------------------------------------------------

    def watch(self, session_id: str, kinds: tuple[str, ...], offer: OfferHandler) -> bool:
        """Attach the intake as the session's ``messaging`` viewer (once per session); False when
        the host does not hold the session."""
        host = self.host
        key = host.key_of(session_id)
        if key is None:
            return False
        connection = f"{MESSAGING_SURFACE}:{key}"
        if host.viewers.find(key, connection) is not None:
            return True
        viewer = Viewer.of(
            connection,
            MESSAGING_SURFACE,
            kinds,
            send=_ignore,
            offer=lambda found: offer(host.service.current_id(key), found),
        )
        host.attach(key, viewer)
        return True

    # -- input ------------------------------------------------------------------------------------

    async def submit(
        self, session_id: str, text: str, *, principal: str, request_id: str
    ) -> Receipt:
        return await self.host.submit(
            session_id,
            text,
            principal=principal,
            request_id=request_id,
            surface=MESSAGING_SURFACE,
            busy="queue",
        )

    async def wait(self, admission_id: str) -> Settled:
        receipt = await self.host.wait(admission_id)
        row = await self._off(lambda: ledger.get(self.store, admission_id))
        if row is None:
            state, outcome, generation = receipt.state, receipt.outcome, receipt.generation
            return Settled(admission_id, state, outcome, generation, None, None)
        return _settled(row)

    async def stop(self, session_id: str, admission_id: str) -> None:
        """Cancel a queued admission, or stop exactly its running turn (best effort)."""
        host = self.host
        try:
            if host.key_of(session_id) is not None:
                await host.stop(session_id, admission_id)
                return
            await host.admissions.cancel(admission_id)
        except (LedgerRefusal, ProtocolError, RuntimeError) as exc:
            log.info("could not stop admission %s: %s", admission_id, exc)

    def running_admission(self, session_id: str, generation: int) -> str | None:
        """The admission whose turn ``generation`` the session runs now (a plain read: right at
        a turn's end, before its admission settles, it still names that admission)."""
        key = self.host.key_of(session_id)
        ticket = None if key is None else self.host.admissions.running(key)
        if ticket is None or ticket.generation != generation:
            return None
        return ticket.admission.admission_id

    def running_generation(self, session_id: str) -> int | None:
        key = self.host.key_of(session_id)
        return None if key is None else self.host.fences.running(key)

    # -- turn ends --------------------------------------------------------------------------------

    async def owner_of(self, session_id: str, generation: int) -> TurnOwner | None:
        """The admission the turn ``generation`` of ``session_id`` ran for, or None."""
        ids = tuple(dict.fromkeys((self.key_of(session_id), session_id)))
        try:
            row = await self._off(lambda: _row_for_generation(self.store, ids, generation))
        except Exception:
            log.warning("could not read the admission of turn %s", generation, exc_info=True)
            return None
        if row is None:
            return None
        return TurnOwner(row.admission_id, row.request_id, row.principal_id, _deliver(row))

    # -- recovery ---------------------------------------------------------------------------------

    async def by_request(self, request_id: str) -> AdmissionRow | None:
        return await self._off(lambda: _row_for_request(self.store, request_id))

    async def unknown_rows(self) -> list[AdmissionRow]:
        return await self._off(lambda: _unknown_rows(self.store))

    async def unknown_of(self, session_id: str) -> AdmissionRow | None:
        rows = await self._off(lambda: ledger.pending(self.store, session_id))
        return next((row for row in rows if row.state == "unknown"), None)

    async def discard(self, row: AdmissionRow) -> bool:
        """Resolve an unknown admission (its own generation); the session's FIFO moves on."""
        if row.generation is None:
            return False
        try:
            await self.admissions.discard_unknown(row.admission_id, row.generation)
        except (LedgerRefusal, RuntimeError) as exc:
            log.warning("could not discard the lost turn %s: %s", row.admission_id, exc)
            return False
        return True

    def on_blocked(self, session_id: str, row: AdmissionRow) -> None:
        """The admission service found input queued behind an unknown turn. Before recovery has
        run the notice is held; afterwards the recovery's handler tells the chat."""
        handler = self._handler
        if handler is None:
            if len(self._held) < _HELD_CAP:
                self._held.append((session_id, row))
            return
        self._spawn(_guarded(handler(session_id, row)))

    async def release_blocked(self, handler: BlockedHandler) -> int:
        """Hand the held notices (those still unknown) to ``handler``; later ones go straight
        to it. How many were handed."""
        self._handler = handler
        held, self._held = self._held, []
        count = 0
        for session_id, row in held:
            current = await self._off(lambda r=row: ledger.get(self.store, r.admission_id))
            if current is None or current.state != "unknown":
                continue
            await _guarded(handler(session_id, current))
            count += 1
        return count

    # -- helpers ----------------------------------------------------------------------------------

    def _spawn(self, coro: Awaitable[None]) -> None:
        task: asyncio.Task[None] = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def aclose(self) -> None:
        """Stop handing on notices; cancel those still being sent."""
        self._handler = None
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(BaseException):
                await task


def _settled(row: AdmissionRow) -> Settled:
    return Settled(
        row.admission_id, row.state, row.outcome, row.generation, row.final_text, row.detail
    )


def _ignore(event: Mapping[str, Any]) -> None:
    del event


async def _guarded(coro: Awaitable[None]) -> None:
    try:
        await coro
    except asyncio.CancelledError:
        raise
    except Exception:
        log.warning("the blocked-session notice failed", exc_info=True)
