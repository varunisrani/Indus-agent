"""The daemon session host: every session of the profile, for every surface, in one place (P17-08).

``DaemonSessionHost`` runs inside the per-profile daemon. It composes the P04 ``SessionService``
(agents, transcripts, titles, compaction, plugins, MCP) with the P17 runtime pieces, and exposes
the same call shapes with the daemon's rules:

- **Viewers.** A surface creates or resumes a session and attaches as a viewer (``Viewer``:
  connection id, surface kind, capabilities). A session has zero or more viewers. Closing a client
  only detaches it (``detach`` / ``detach_connection``): a running turn finishes and is stored, the
  queue keeps draining. A session whose source is ``acp`` ends when its last viewer leaves and
  nothing runs or waits in its queue (its row is ended ``acp_close``); with work left it ends
  when the queue drains, unless a viewer comes back first. ``aclose`` unloads every session.
- **Events.** Every event of a session passes through the daemon's one ``EventLog`` (seq per
  session, ``replay_epoch`` per daemon start) and is handed to each viewer of that session in log
  order. ``events_since`` replays from a cursor (or reports a gap) and re-offers the still-open
  server requests to the asking viewer.
- **Input.** ``submit`` admits the text into the session's durable FIFO (``AdmissionService``) and
  returns its receipt. When a turn runs and the caller names no busy mode, the submitting surface
  decides (R05.18): the TUI queues, every other surface interrupts the running turn — after the new
  input is admitted, so it is never lost. Turns the service would start on its own (host
  injections, background-subagent results, undelivered steer text) are admitted the same way.
- **Turns.** The host is the admission service's runner: it starts the agent's turn under the
  ledger's generation, binds the ticket's interrupt and steer hooks (a stop that came first means
  the turn never starts), reloads the session when the lease was won only after waiting for
  another process, feeds the liveness meter from the session's events and maps the turn's exit
  onto the ledger outcome. A hard interrupt (and a fenced ``stop``) also stops the session's
  background subagents.
- **Controls.** ``interrupt`` / ``steer`` / ``respond`` / ``stop`` go through the generation fences
  (P17-05) and return their result values (``StaleGeneration`` for a control aimed at another
  turn). Server requests live in one ``RequestBook`` per session (P17-07), handed to the session's
  broker; ``refuse`` records a viewer's error answer.
- **Policy.** ``create`` freezes the launch policy (P17-09) into the session row; ``resume`` with a
  policy that changes a frozen value is refused (``PolicyConflict``).

Sessions are keyed by the id they were first attached under: the ledger, the fences, the event log
and the viewers stay addressed by it when a rotating compaction moves the session to a child id
(the old id keeps working for every call, and events carry the current id).

Every method runs on the daemon's event loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import sqlite3
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from indus.core.events import EventSink
from indus.core.turn_state import TurnResult
from indus.foundation.blocking import run_blocking
from indus.protocol.errors import SESSION_NOT_FOUND, TEXT_EMPTY
from indus.protocol.events import EventBase, ExitReason
from indus.protocol.methods import (
    SERVER_REQUESTS_FEATURE,
    ProtocolError,
    SessionCreateParams,
    SessionCreateResult,
    SessionResumeResult,
)
from indus.protocol.requests_port import AnswerModel
from indus.protocol.server_requests import SERVER_REQUEST_KINDS
from indus.service.admissions import (
    CANCELLED,
    INTERRUPTED,
    AdmissionService,
    Offload,
    Receipt,
    TerminalWrite,
    TurnOutcome,
    TurnTicket,
)
from indus.service.broker import ServerRequestBroker
from indus.service.creation_policy import (
    CreationPolicy,
    PolicyRefused,
    check_launch,
    policy_patch,
    require_same,
    stored_policy,
)
from indus.service.event_log import EventLog, LoggedEvent, Replay, ReplayGap
from indus.service.fences import (
    ControlApplied,
    GenerationFences,
    RequestNotOpen,
    Responded,
    StaleGeneration,
    StopResult,
)
from indus.service.request_redelivery import RequestBook
from indus.service.session_service import SessionService
from indus.service.turn_lease import TurnLeases
from indus.service.turn_liveness import LivenessSettings
from indus.service.viewers import (
    BUSY_MODES,
    DECLINE_CAPABILITY,
    BusyMode,
    Viewer,
    ViewerRegistry,
    busy_default,
)
from indus.state import runtime_ledger as ledger
from indus.state import sessions
from indus.state.registry import StoreHandle
from indus.state.runtime_ledger import AdmissionRow, LedgerRefusal

__all__ = [
    "ACP_END_REASON",
    "ACP_SOURCE",
    "CREATABLE_SOURCES",
    "HOST_CAPABILITIES",
    "INTERNAL_PRINCIPAL",
    "Attachment",
    "DaemonSessionHost",
    "SessionState",
]

log = logging.getLogger(__name__)

ACP_SOURCE: Final = "acp"
"""Sessions of this source end when their last viewer leaves an idle queue."""
ACP_END_REASON: Final = "acp_close"
"""The end reason stamped on the row of an ACP session ended that way."""
INTERNAL_PRINCIPAL: Final = "internal"
"""Principal prefix of inputs the host admits on its own (``internal:<origin>``)."""
CREATABLE_SOURCES: Final[tuple[str, ...]] = ("cli", "tui", "gui", "acp", "web")
"""Surface kinds a client may create sessions as (``describe``). ``messaging`` sessions are
created by the gateway's own pipeline, never by an attached client."""
REPLAY_CAPABILITY: Final = "events.since"
"""``events_since`` replays a session from a cursor, or reports a gap."""
HOST_CAPABILITIES: Final[tuple[str, ...]] = (
    REPLAY_CAPABILITY,
    SERVER_REQUESTS_FEATURE,
    *(f"{SERVER_REQUESTS_FEATURE}.{kind}" for kind in SERVER_REQUEST_KINDS),
    DECLINE_CAPABILITY,
)
"""What the host offers attached clients (``describe``): replay from a cursor, every kind of
server request (offered and re-offered to the viewers that render it), and the 4404 decline.
Names reuse the client feature vocabulary (``indus.protocol.methods``); P17-25 owns the table."""
_INTERRUPTED_EXITS: Final = frozenset(
    {ExitReason.INTERRUPTED_BY_USER, ExitReason.INTERRUPTED_BY_SYSTEM}
)
_PERSON_STOPS: Final = frozenset({CANCELLED, INTERRUPTED})


async def _io_pool[T](fn: Callable[[], T]) -> T:
    return await run_blocking(fn, label="daemon host")


@dataclass(frozen=True, slots=True)
class Attachment:
    """What a viewer learns when it attaches."""

    session_id: str
    """The id the session runs under now."""
    key: str
    """The host's key for the session (stable across compactions; the replay cursor's scope)."""
    replay_epoch: str
    last_seq: int
    """The newest seq numbered for the session: replay from here on."""
    reoffered: int
    """Open server requests offered to this viewer again."""


@dataclass(frozen=True, slots=True)
class SessionState:
    """A session as the host holds it (for snapshots and presence)."""

    key: str
    session_id: str
    source: str
    replay_epoch: str
    last_seq: int
    running: int | None
    """The generation running now, or None."""
    viewers: tuple[str, ...]
    """Connection ids watching it, in attach order."""
    open_requests: tuple[str, ...]
    close_pending: bool


@dataclass(slots=True)
class _Fresh:
    """What the broker factory built for a session the service is attaching."""

    book: RequestBook
    broker: ServerRequestBroker
    undo: list[Callable[[], None]]


@dataclass(slots=True)
class _Live:
    key: str
    source: str
    book: RequestBook
    broker: ServerRequestBroker
    policy: CreationPolicy | None = None
    undo: list[Callable[[], None]] = field(default_factory=list[Callable[[], None]])
    close_pending: bool = False


class DaemonSessionHost:
    """Sessions of one profile hosted by its daemon (see the module notes)."""

    def __init__(
        self,
        service: SessionService,
        store: StoreHandle,
        *,
        event_log: EventLog | None = None,
        fences: GenerationFences | None = None,
        viewers: ViewerRegistry | None = None,
        offload: Offload | None = None,
    ) -> None:
        if service.attached():
            raise ValueError("the daemon host must own its session service from the start")
        self.service = service
        self.store = store
        self.event_log = event_log if event_log is not None else EventLog(clock=store.scope.clock)
        self.fences = fences if fences is not None else GenerationFences()
        self.viewers = viewers if viewers is not None else ViewerRegistry()
        self._off: Offload = offload if offload is not None else _io_pool
        self._admissions: AdmissionService | None = None
        self._live: dict[str, _Live] = {}
        self._keys: dict[str, str] = {}
        self._fresh: dict[str, _Fresh] = {}
        self._tickets: dict[str, TurnTicket] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._flusher: asyncio.Task[None] | None = None
        self._undo: list[Callable[[], None]] = []
        self._closed = False
        service.broker_factory = self._make_broker
        service.turn_door = self._door

    @classmethod
    async def open(
        cls,
        service: SessionService,
        store: StoreHandle,
        *,
        instance_id: str,
        leases: TurnLeases | None = None,
        liveness: LivenessSettings | None = None,
        gate: Callable[[sqlite3.Connection], None] | None = None,
        on_blocked: Callable[[str, AdmissionRow], object] | None = None,
        event_log: EventLog | None = None,
        offload: Offload | None = None,
    ) -> DaemonSessionHost:
        """Build the host, begin the owner epoch (the caller holds the profile ownership lock),
        start the event-log flusher and restart the queues left by the previous daemon."""
        host = cls(service, store, event_log=event_log, offload=offload)
        host._undo.append(host.event_log.add_listener(host._fan_out))
        host._undo.append(service.on_session_moved(host._moved))
        host._flusher = asyncio.get_running_loop().create_task(
            host.event_log.run_flusher(), name="event-log flusher"
        )
        try:
            admissions = await AdmissionService.open(
                store,
                instance_id=instance_id,
                runner=host._run_turn,
                offload=offload,
                leases=leases,
                liveness=liveness,
                waiting=host._waiting,
                on_blocked=on_blocked,
                gate=gate,
                fences=host.fences,
            )
        except BaseException:
            await host._stop_flusher()
            raise
        host._admissions = admissions
        host.fences.bind_admissions(admissions)
        return host

    # -- views ------------------------------------------------------------------------------------

    @property
    def admissions(self) -> AdmissionService:
        if self._admissions is None:
            raise RuntimeError("the daemon host is not open")
        return self._admissions

    @property
    def replay_epoch(self) -> str:
        """The daemon's replay epoch (``gateway.ready.replay_epoch``)."""
        return self.event_log.epoch

    def creatable_sources(self) -> tuple[str, ...]:
        """Surface kinds a client may create sessions as (a plain read: ``describe`` answers
        while turns run)."""
        return CREATABLE_SOURCES

    def capabilities(self) -> tuple[str, ...]:
        """Capability names the host offers attached clients (a plain read)."""
        return HOST_CAPABILITIES

    def hosted(self) -> list[str]:
        """Keys of the sessions the host holds."""
        return list(self._live)

    def key_of(self, session_id: str) -> str | None:
        """The host's key for ``session_id`` (any id it ran under), or None."""
        key = self._keys.get(session_id)
        if key is None:
            key = self._keys.get(self.service.current_id(session_id))
        return key if key in self._live else None

    def state(self, session_id: str) -> SessionState:
        """The session as the host holds it (a snapshot's running state and presence)."""
        live = self._require(session_id)
        key = live.key
        return SessionState(
            key=key,
            session_id=self.service.current_id(key),
            source=live.source,
            replay_epoch=self.event_log.epoch,
            last_seq=self.event_log.last_seq(key),
            running=self.fences.running(key),
            viewers=tuple(v.connection_id for v in self.viewers.viewers(key)),
            open_requests=tuple(r.request_id for r in live.book.open_requests()),
            close_pending=live.close_pending,
        )

    # -- create / resume / attach -----------------------------------------------------------------

    async def create(
        self,
        params: SessionCreateParams | None = None,
        *,
        source: str = "cli",
        policy: CreationPolicy | None = None,
        viewer: Viewer | None = None,
    ) -> SessionCreateResult:
        """Create a session (``policy`` frozen into it) and attach ``viewer`` when given."""
        self._check_open()
        params = params or SessionCreateParams()
        if policy is not None:
            check_launch(policy)
            params = _params_with(params, policy)
        result = await self.service.create(params)
        live = self._register(result.session_id, source=source)
        if result.created and policy is not None:
            live.policy = policy
            await self._freeze(result.session_id, source, policy)
        if viewer is not None:
            self.attach(result.session_id, viewer)
        return result

    async def resume(
        self,
        session_id: str,
        *,
        policy: CreationPolicy | None = None,
        source: str | None = None,
        viewer: Viewer | None = None,
    ) -> SessionResumeResult:
        """Attach an existing session (a hosted one is described again). A ``policy`` may repeat
        the frozen launch values; a different value raises ``PolicyConflict``."""
        self._check_open()
        if policy is not None:
            require_same(await self._frozen(session_id), policy)
        result = await self.service.resume(session_id)
        origin = source or (viewer.surface if viewer is not None else "unknown")
        self._register(result.session_id, source=origin)
        if viewer is not None:
            self.attach(result.session_id, viewer)
        return result

    def attach(self, session_id: str, viewer: Viewer) -> Attachment:
        """Add ``viewer`` to a hosted session (the same connection again replaces itself); every
        open server request it can render is offered to it again."""
        live = self._require(session_id)
        key = live.key
        self.viewers.attach(key, viewer)
        live.close_pending = False
        reoffered = live.book.attach(viewer.request_viewer())
        return Attachment(
            session_id=self.service.current_id(key),
            key=key,
            replay_epoch=self.event_log.epoch,
            last_seq=self.event_log.last_seq(key),
            reoffered=reoffered,
        )

    async def detach(self, session_id: str, connection_id: str) -> bool:
        """Remove one viewer; the session and any running turn carry on."""
        key = self.key_of(session_id)
        if key is None:
            return False
        removed = self.viewers.detach(key, connection_id) is not None
        self._live[key].book.detach(connection_id)
        if removed:
            await self._after_detach(key)
        return removed

    async def detach_connection(self, connection_id: str) -> list[str]:
        """A client connection closed: detach it from every session it watched (their keys)."""
        keys = self.viewers.detach_connection(connection_id)
        for key in keys:
            live = self._live.get(key)
            if live is None:
                continue
            live.book.detach(connection_id)
            await self._after_detach(key)
        return keys

    async def _after_detach(self, key: str) -> None:
        live = self._live.get(key)
        if live is None or live.source != ACP_SOURCE or self.viewers.count(key):
            return
        await self._close_when_idle(key)

    async def _close_when_idle(self, key: str) -> None:
        live = self._live.get(key)
        if live is None:
            return
        if await self._idle(key):
            if self.viewers.count(key) == 0 and self._live.get(key) is live:
                log.info("session %s ends: its last viewer left and nothing is queued", key)
                current = self.service.current_id(key)
                await self.close_session(key)
                await self._off(lambda: sessions.end(self.store, current, ACP_END_REASON))
            return
        live.close_pending = True

    async def _idle(self, key: str) -> bool:
        if self.admissions.running(key) is not None or key in self._tickets:
            return False
        rows = await self._off(lambda: ledger.pending(self.store, key))
        return not any(row.state in ("queued", "started") for row in rows)

    # -- input ------------------------------------------------------------------------------------

    async def submit(
        self,
        session_id: str,
        text: str,
        *,
        principal: str,
        request_id: str,
        connection_id: str | None = None,
        surface: str | None = None,
        busy: BusyMode | None = None,
    ) -> Receipt:
        """Admit ``text`` into the session's queue; the receipt says where it stands. Refusals
        leave as ``LedgerRefusal``."""
        self._check_open()
        if not text or not text.strip():
            raise ProtocolError(TEXT_EMPTY, path="text")
        if busy is not None and busy not in BUSY_MODES:
            raise ValueError(f"unknown busy mode {busy!r}")
        live = self._live.get(self.key_of(session_id) or "")
        if live is None:
            await self.resume(session_id, source=surface)
            live = self._require(session_id)
        key = live.key
        mode = busy or self._busy_mode(key, connection_id, surface, live)
        running = self.admissions.running(key)
        receipt = await self.admissions.admit(principal, key, request_id, {"text": text})
        if mode == "interrupt" and running is not None and receipt.state == "queued":
            applied = self.fences.interrupt(key, running.generation)
            log.info("input for %s interrupts turn %s: %s", key, running.generation, applied)
        return receipt

    def _busy_mode(
        self, key: str, connection_id: str | None, surface: str | None, live: _Live
    ) -> BusyMode:
        if surface is not None:
            return busy_default(surface)
        if connection_id is not None:
            viewer = self.viewers.find(key, connection_id)
            if viewer is not None:
                return viewer.busy_default
        return busy_default(live.source)

    async def receipt(self, admission_id: str) -> Receipt:
        return await self.admissions.receipt(admission_id)

    async def wait(self, admission_id: str) -> Receipt:
        return await self.admissions.wait(admission_id)

    async def discard_unknown(
        self,
        admission_id: str,
        expected_generation: int,
        *,
        terminal_write: TerminalWrite | None = None,
    ) -> Receipt:
        return await self.admissions.discard_unknown(
            admission_id, expected_generation, terminal_write=terminal_write
        )

    async def _door(self, session_id: str, text: str, origin: str) -> bool:
        """Turns the service would start itself enter the queue like any other input."""
        key = self.key_of(session_id)
        if key is None or self._closed:
            return False
        slug = "-".join(origin.split()) or "host"
        try:
            await self.admissions.admit(
                f"{INTERNAL_PRINCIPAL}:{slug}",
                key,
                f"{slug}-{secrets.token_hex(8)}",
                {"text": text, "origin": origin},
            )
        except (LedgerRefusal, RuntimeError) as exc:
            log.warning("a %s message for %s was not admitted: %s", origin, key, exc)
            return False
        return True

    # -- controls ---------------------------------------------------------------------------------

    def interrupt(
        self, session_id: str, generation: int, *, hard: bool = False
    ) -> ControlApplied | StaleGeneration:
        """Stop turn ``generation`` of the session, and only that turn."""
        return self.fences.interrupt(self._require(session_id).key, generation, hard=hard)

    def steer(
        self, session_id: str, generation: int, text: str
    ) -> ControlApplied | StaleGeneration:
        if not text.strip():
            raise ProtocolError(TEXT_EMPTY, path="text")
        return self.fences.steer(self._require(session_id).key, generation, text)

    def respond(
        self,
        session_id: str,
        request_id: str,
        generation: int,
        answer: Mapping[str, Any] | AnswerModel,
    ) -> Responded | RequestNotOpen | StaleGeneration:
        """Answer a server request asked by turn ``generation`` (the first answer wins)."""
        return self.fences.respond(self._require(session_id).key, request_id, generation, answer)

    def refuse(self, session_id: str, connection_id: str, request_id: str, code: int) -> bool:
        """A viewer answered a server request with an error (``-32601``, ``4404`` …)."""
        return self._require(session_id).book.refuse(connection_id, request_id, code)

    async def stop(self, session_id: str, admission_id: str) -> StopResult:
        """Cancel a queued admission or stop exactly its running turn."""
        return await self.fences.stop(self._require(session_id).key, admission_id)

    # -- events -----------------------------------------------------------------------------------

    def events_since(
        self, session_id: str, epoch: str | None, cursor: int, *, connection_id: str | None = None
    ) -> Replay | ReplayGap:
        """The session's events after ``cursor`` (or a gap: reload a snapshot); the asking
        viewer is offered the still-open server requests again. A connection replays only a
        session it is attached to (anything else reads as an unknown session)."""
        live = self._require(session_id)
        if connection_id is not None and self.viewers.find(live.key, connection_id) is None:
            raise ProtocolError(
                SESSION_NOT_FOUND, f"session {session_id} is not attached here", path="session_id"
            )
        result = self.event_log.since(live.key, epoch, cursor)
        if connection_id is not None:
            live.book.reoffer(connection_id)
        return result

    def _on_event(self, key: str, event: EventBase | Mapping[str, Any]) -> None:
        ticket = self._tickets.get(key)
        if ticket is not None:
            kind: object = (
                getattr(event, "type", "") if isinstance(event, EventBase) else event.get("type")
            )
            ticket.touch(str(kind or ""))
        self.event_log.append(key, event)

    def _fan_out(self, entry: LoggedEvent) -> None:
        if self.viewers.count(entry.session_id):
            self.viewers.deliver(entry.session_id, entry.to_wire())

    # -- closing ----------------------------------------------------------------------------------

    async def close_session(self, session_id: str) -> None:
        """End a session now: its turn is stopped, its requests withdrawn, its viewers dropped."""
        key = self.key_of(session_id)
        if key is None:
            return
        live = self._live[key]
        try:
            await self.service.close(key)
        finally:
            self._live.pop(key, None)
            for undo in live.undo:
                with contextlib.suppress(Exception):
                    undo()
            self.fences.forget(key)
            self.event_log.forget(key)
            self.viewers.forget(key)
            for name in [n for n, k in self._keys.items() if k == key]:
                del self._keys[name]

    async def aclose(self) -> None:
        """Stop taking input, stop running turns, end every session and the service."""
        if self._closed:
            return
        self._closed = True
        if self._admissions is not None:
            await self._admissions.aclose()
        for key in list(self._live):
            try:
                await self.close_session(key)
            except Exception:
                log.warning("closing session %s failed", key, exc_info=True)
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(BaseException):
                await task
        await self.service.aclose()
        await self._stop_flusher()
        self.event_log.flush()
        for undo in self._undo:
            undo()
        self._undo.clear()

    async def _stop_flusher(self) -> None:
        task, self._flusher = self._flusher, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    # -- the turn runner --------------------------------------------------------------------------

    async def _run_turn(self, ticket: TurnTicket) -> TurnOutcome:
        key = ticket.session_id
        if ticket.stop_reason is not None:
            return _not_started(ticket)
        self._tickets[key] = ticket
        try:
            if key not in self._live:
                await self.resume(key)
            if ticket.lease_waited:
                await self.service.reload(key)
            if ticket.stop_reason is not None:
                return _not_started(ticket)
            running = await self.service.start_admitted(
                key, ticket.text, generation=ticket.generation
            )
            generation = ticket.generation
            ticket.bind(
                interrupt=lambda reason: self._stop_turn(key, generation, reason),
                steer=lambda text: self._steer_turn(key, text),
            )
            if ticket.stop_reason is not None:  # stopped between the start and the bind
                self._stop_turn(key, generation, ticket.stop_reason)
            try:
                result = await asyncio.shield(running.task)
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    raise
                return TurnOutcome("interrupted", detail=ticket.stop_reason or "cancelled")
            return _outcome(ticket, result)
        finally:
            if self._tickets.get(key) is ticket:
                del self._tickets[key]
            self._spawn(self._after_turn(key, ticket.admission.admission_id))

    def _stop_turn(self, key: str, generation: int, reason: str) -> None:
        """The ticket's interrupt hook: soft for a person's interrupt, hard (subagents too) for a
        cancel or stop, by the system for the watchdog, a lost lease or shutdown."""
        try:
            self.service.interrupt(
                key,
                generation,
                hard=reason == CANCELLED,
                by_system=reason not in _PERSON_STOPS,
            )
        except ProtocolError:
            log.info("turn %s of %s was gone before its interrupt (%s)", generation, key, reason)

    def _steer_turn(self, key: str, text: str) -> bool:
        try:
            return self.service.steer(key, text).queued
        except ProtocolError:
            return False

    async def _after_turn(self, key: str, admission_id: str) -> None:
        """Once the turn's admission is settled, end an ACP session waiting for an idle queue."""
        live = self._live.get(key)
        if live is None or not live.close_pending:
            return
        with contextlib.suppress(LedgerRefusal):
            await self.admissions.wait(admission_id)
        live = self._live.get(key)
        if live is None or not live.close_pending or self.viewers.count(key):
            return
        await self._close_when_idle(key)

    def _waiting(self, key: str) -> bool:
        live = self._live.get(key)
        return live is not None and live.broker.waiting

    # -- wiring -----------------------------------------------------------------------------------

    def _make_broker(self, session_id: str, sink: EventSink) -> ServerRequestBroker:
        """The service's broker factory: one request book per session, events into the log."""
        key = session_id
        fences = self.fences
        book = RequestBook(
            session_id,
            emit=lambda event: self._on_event(key, event),
            generation=lambda: fences.running(key),
        )
        broker = ServerRequestBroker(
            session_id=session_id,
            deliver=sink.request,
            emit=sink.emit,
            next_seq=sink.next_seq,
            clock=self.store.scope.clock,
            book=book,
        )
        fences.bind_requests(key, book, broker.answer)
        undo = sink.subscribe(lambda event: self._on_event(key, event))
        self._fresh[key] = _Fresh(book, broker, [undo])
        self._keys[key] = key
        return broker

    def _register(self, session_id: str, *, source: str) -> _Live:
        key = self._keys.get(session_id) or self._keys.get(self.service.current_id(session_id))
        if key is not None and key in self._live:
            return self._live[key]
        key = key or session_id
        fresh = self._fresh.pop(key, None)
        if fresh is None:
            raise RuntimeError(f"session {session_id} was attached without the daemon's broker")
        live = _Live(key, source, fresh.book, fresh.broker, undo=fresh.undo)
        self._live[key] = live
        return live

    def _moved(self, old: str, new: str) -> None:
        """A rotating compaction moved a session: its key stays, its book names the child."""
        key = self._keys.get(old)
        if key is None:
            return
        self._keys[new] = key
        live = self._live.get(key)
        book = live.book if live is not None else None
        if book is None and key in self._fresh:
            book = self._fresh[key].book
        if book is not None:
            book.session_id = new

    async def _frozen(self, session_id: str) -> CreationPolicy | None:
        key = self.key_of(session_id)
        if key is not None and self._live[key].policy is not None:
            return self._live[key].policy
        target = self.service.current_id(session_id)
        row = await self._off(lambda: sessions.get(self.store, target))
        return stored_policy(row)

    async def _freeze(self, session_id: str, source: str, policy: CreationPolicy) -> None:
        """Write the policy into the session row now (the row is created when missing)."""
        patch = policy_patch(policy)
        now = self.store.scope.clock.now().timestamp()
        profile = self.store.scope.profile_id

        def write() -> None:
            def apply(conn: sqlite3.Connection) -> None:
                sessions.create_in(conn, session_id, source, now=now, profile_id=profile)
                sessions.patch_model_config_in(conn, session_id, patch)

            self.store.write(apply)

        await self._off(write)

    def _require(self, session_id: str) -> _Live:
        key = self.key_of(session_id)
        if key is None:
            raise ProtocolError(
                SESSION_NOT_FOUND, f"session {session_id} is not hosted", path="session_id"
            )
        return self._live[key]

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("the daemon host is closed")

    def _spawn(self, coro: Awaitable[None]) -> None:
        if self._closed:
            if asyncio.iscoroutine(coro):
                coro.close()
            return
        task: asyncio.Task[None] = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


def _params_with(params: SessionCreateParams, policy: CreationPolicy) -> SessionCreateParams:
    """``params`` filled from the policy; a value given in both must agree."""
    changes: dict[str, Any] = {}
    pairs: tuple[tuple[str, object], ...] = (
        ("cwd", policy.cwd),
        ("model", policy.model),
        ("toolsets", list(policy.toolsets) if policy.toolsets is not None else None),
    )
    for name, value in pairs:
        if value is None:
            continue
        given = getattr(params, name)
        if given is None:
            changes[name] = value
        elif given != value:
            raise PolicyRefused("invalid_value", name)
    return params.model_copy(update=changes) if changes else params


def _not_started(ticket: TurnTicket) -> TurnOutcome:
    reason = ticket.stop_reason or INTERRUPTED
    if reason == CANCELLED:
        return TurnOutcome("cancelled", detail=CANCELLED)
    return TurnOutcome("interrupted", detail=reason)


def _outcome(ticket: TurnTicket, result: TurnResult) -> TurnOutcome:
    """The ledger outcome of a finished turn."""
    reason = result.exit_reason
    text = result.final_text
    if reason is ExitReason.COMPLETED:
        return TurnOutcome("completed", text)
    if reason in _INTERRUPTED_EXITS:
        stop = ticket.stop_reason
        if stop == CANCELLED:
            return TurnOutcome("cancelled", text, CANCELLED)
        return TurnOutcome("interrupted", text, stop or reason.value)
    return TurnOutcome("failed", text, result.failure_reason or reason.value)
