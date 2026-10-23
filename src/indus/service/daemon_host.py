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
  server requests to the asking viewer. After a turn's ``turn.done`` the host adds what the TUI's
  own bridge sends at a turn's end: ``message.complete`` (the final reply and why the turn ended;
  a client settles its streamed text on it) and ``session.usage`` (the session's counters), so an
  attached client sees the same turn end as an in-process one (P17-26). The status bar's header
  travels the same way (W48-h, D-16.550): ``announce_info`` puts a ``session.info``
  (``service.session_header``, the stored title, the profile's tool-progress mode: the daemon keeps
  none per viewer) into the session's events when a viewer attaches and when a model or folder
  change applies; after a turn a stored title that changed (the automatic title) is sent as
  ``session.title``, as the bridge does. Only the session's viewers receive them.
- **Input.** ``submit`` admits the text into the session's durable FIFO (``AdmissionService``) and
  returns its receipt; every move of an input after that (queued, claimed, settled, lost) is
  appended to the session's events as ``admission.state``. An input submitted ``unattended`` (a
  finite ``-z`` run) runs its turn with the session's auto-approve switched on for that turn only
  (the hard floors still refuse), as the in-process ``-z`` run approves for its run. An input
  submitted ``single_query`` (a finite ``chat -q`` or ``-z``) runs its turn with the run context
  ``single_query`` and asks nobody (the session's broker answers no kind), for that turn only, as
  the in-process one-shot run does (D-16.487). When a turn
  runs and the caller names no busy mode, the submitting surface decides (R05.18): the TUI
  queues, every other surface interrupts the running turn — after the new input is admitted, so
  it is never lost. Turns the service would start on its own (host
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
- **Switches.** ``set_auto_approve`` is a person's ``/yolo`` over ``config.set`` (W50-a): stored
  on the session row, then switched, and an unattended turn that switched it on leaves it as the
  person set it. ``queued_inputs`` counts the inputs waiting in a session's queue (``/status``).
- **Policy.** ``create`` freezes the launch policy (P17-09) into the session row; ``resume`` with a
  policy that changes a frozen value is refused (``PolicyConflict``).
- **Changes.** ``mutations`` (``service.session_mutations``) serves ``session.mutate``: an editor's
  own MCP servers, fork, folder, model, title and history changes, under a per-session gate the
  turn runner holds while it prepares and starts a claimed turn (deferred changes apply there,
  ``before_turn``). It also shapes each
  agent before it is built (the service's ``agent_shaper``), and a session's editor servers close
  with it. The host advertises ``ACP_EDITOR_POLICY``.

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
from indus.core.session_scheduler import TurnEnd
from indus.core.turn_state import TurnResult
from indus.foundation.blocking import run_blocking
from indus.protocol.attach import ACP_EDITOR_POLICY
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
from indus.safety.approvals.decision import APPROVALS, session_key
from indus.service import client_settings, session_header
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
from indus.service.request_redelivery import OpenRequest, RequestBook
from indus.service.session_mutations import SessionMutations
from indus.service.session_service import SessionService, usage_numbers
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
from indus.state import sessions, titles
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
    ACP_EDITOR_POLICY,
)
"""What the host offers attached clients (``describe``): replay from a cursor, every kind of
server request (offered and re-offered to the viewers that render it), the 4404 decline, and the
ACP editor policy (``session.mutate``).
Names reuse the client feature vocabulary (``indus.protocol.methods``); P17-25 owns the table."""
_INTERRUPTED_EXITS: Final = frozenset(
    {ExitReason.INTERRUPTED_BY_USER, ExitReason.INTERRUPTED_BY_SYSTEM}
)
_PERSON_STOPS: Final = frozenset({CANCELLED, INTERRUPTED})
ADMISSION_EVENT: Final = "admission.state"
UNATTENDED: Final = "unattended"
"""Payload flag of an input from a finite unattended run (``-z``): auto-approve for its turn."""
SINGLE_QUERY: Final = "single_query"
"""Payload flag of an input from a finite run (``chat -q``, ``-z``): its turn runs one-shot."""


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
        self._done: dict[str, tuple[str, str]] = {}
        self._auto: dict[str, set[str]] = {}
        """Session ids whose auto-approve this host switched on for an unattended turn, by key."""
        self._one_shot: dict[str, str | None] = {}
        """Keys whose running turn is one-shot, with the run context to put back after it."""
        self._closed = False
        self._titles: dict[str, str | None] = {}
        """The title the session's viewers were last sent, by key."""
        self.mutations = SessionMutations(
            service, store, running=self._running_generation, key_of=self._any_key
        )
        self.mutations.on_change = self.announce_info
        service.broker_factory = self._make_broker
        service.turn_door = self._door
        service.agent_shaper = self.mutations.shape

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
        host._undo.append(service.on_turn_end(host._turn_ended))
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
                on_state=host._admission_moved,
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

    def _running_generation(self, key: str) -> int | None:
        """The generation of the turn the session's agent runs now, or None (a claimed turn
        that has not started waits at the mutation gate; a finished one is only settling)."""
        if self._admissions is None:
            return None
        ticket = self._admissions.running(key)
        if ticket is None:
            return None
        try:
            agent = self.service.agent(self.service.current_id(key))
        except ProtocolError:
            return None
        return ticket.generation if agent.current is not None else None

    def running_generation(self, key: str) -> int | None:
        """The generation of the turn the session's agent runs now, or None."""
        return self._running_generation(key)

    def _any_key(self, session_id: str) -> str:
        """The host key of ``session_id``, also while it is being attached (else the id)."""
        return (
            self._keys.get(session_id)
            or self._keys.get(self.service.current_id(session_id))
            or session_id
        )

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

    def open_requests(self, session_id: str) -> list[OpenRequest]:
        """The session's server requests still waiting for an answer, oldest first (a plain
        read; the MCP bridge's approval queue lists them, P19-17)."""
        return self._require(session_id).book.open_requests()

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
        # An attached surface's session is stored under that surface (``cli``, ``tui``, ...) as
        # its in-process twin would be, so its own listings and ``-c`` find it; the gateway's own
        # sessions keep the service's source.
        row_source = source if source in CREATABLE_SOURCES else None
        result = await self.service.create(params, source=row_source)
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
        unattended: bool = False,
        single_query: bool = False,
    ) -> Receipt:
        """Admit ``text`` into the session's queue; the receipt says where it stands. Refusals
        leave as ``LedgerRefusal``. ``unattended``: the input's turn auto-approves (floors still
        apply), for that turn only. ``single_query``: the input's turn runs one-shot (run context
        ``single_query``, nobody asked), for that turn only."""
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
        payload: dict[str, Any] = {"text": text}
        if unattended:
            payload[UNATTENDED] = True
        if single_query:
            payload[SINGLE_QUERY] = True
        receipt = await self.admissions.admit(principal, key, request_id, payload)
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

    async def claimed(self, admission_id: str, timeout: float) -> Receipt:
        """The receipt once a queued input is claimed (bounded by ``timeout`` seconds)."""
        return await self.admissions.claimed(admission_id, timeout)

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

    async def queued_inputs(self, session_id: str) -> int:
        """How many inputs wait in the session's queue (not the running one; read off the loop)."""
        key = self._require(session_id).key
        rows = await self._off(lambda: ledger.pending(self.store, key))
        return sum(1 for row in rows if row.state == "queued")

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
        kind = _field(event, "type")
        ticket = self._tickets.get(key)
        if ticket is not None:
            ticket.touch(kind)
        if kind == "turn.done":
            self._done[key] = (_field(event, "turn_id"), _field(event, "reason"))
        elif kind == "session.title":
            self._titles[key] = _field(event, "title") or None
        self.event_log.append(key, event)

    def _admission_moved(self, key: str, receipt: Receipt) -> None:
        """An input of a hosted session moved: ``admission.state`` into its events."""
        if key not in self._live or self._closed:
            return
        event: dict[str, Any] = {
            "type": ADMISSION_EVENT,
            "session_id": self.service.current_id(key),
            "admission_id": receipt.admission_id,
            "state": receipt.state,
        }
        if receipt.generation is not None:
            event["generation"] = receipt.generation
        if receipt.state == "queued" and receipt.position is not None:
            event["position"] = receipt.position
        if receipt.state == "terminal" and receipt.outcome is not None:
            event["outcome"] = receipt.outcome
        self.event_log.append(key, event)

    def _turn_ended(self, ended: TurnEnd) -> None:
        """The turn's end as a client settles it: ``message.complete`` then ``session.usage``,
        after the ``turn.done`` the turn itself emitted (the bridge's turn-end pair)."""
        key = self.key_of(ended.session_id)
        if key is None:
            return
        turn_id, reason = self._done.pop(key, ("", ""))
        if turn_id != ended.turn_id or not reason:
            reason = (
                ExitReason.COMPLETED.value
                if ended.completed
                else ExitReason.INTERRUPTED_BY_SYSTEM.value
            )
        current = ended.session_id
        self._on_event(
            key,
            {
                "type": "message.complete",
                "session_id": current,
                "turn_id": ended.turn_id,
                "text": ended.final_text,
                "reason": reason,
            },
        )
        self._spawn(self._settle_title(key))
        try:
            usage = usage_numbers(self.service, current)
        except Exception:
            log.debug("no usage for %s", current, exc_info=True)
            return
        self._on_event(key, {"type": "session.usage", "session_id": current, "usage": usage})

    # -- the session header (W48-h) ----------------------------------------------------------------

    def announce_info(self, key: str) -> None:
        """Put a ``session.info`` for the hosted session ``key`` into its events (soon: the title
        and the settings are read off the loop)."""
        if key in self._live and not self._closed:
            self._spawn(self._send_info(key))

    async def _stored_title(self, session_id: str) -> str | None:
        try:
            return await self._off(lambda: titles.get_title(self.store, session_id))
        except Exception:
            log.debug("title of %s unreadable", session_id, exc_info=True)
            return None

    async def _send_info(self, key: str) -> None:
        if key not in self._live:
            return
        sid = self.service.current_id(key)
        title = await self._stored_title(sid)
        mode = await self._off(lambda: client_settings.default_tool_mode(self.store.scope))
        if key not in self._live:
            return
        try:
            fields = session_header.session_info(self.service, sid, title=title, tool_progress=mode)
        except Exception:
            log.debug("no session info for %s", key, exc_info=True)
            return
        self._titles[key] = title
        shown = {name: value for name, value in fields.items() if value is not None}
        self._on_event(key, {"type": "session.info", "session_id": sid, **shown})

    async def _settle_title(self, key: str) -> None:
        """After a turn: a stored title that changed (the automatic title) as ``session.title``."""
        if key not in self._live:
            return
        sid = self.service.current_id(key)
        stored = await self._stored_title(sid)
        if key in self._live and stored is not None and stored != self._titles.get(key):
            self._on_event(key, {"type": "session.title", "session_id": sid, "title": stored})

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
            with contextlib.suppress(Exception):
                await self.mutations.forget(key)
            self._live.pop(key, None)
            for undo in live.undo:
                with contextlib.suppress(Exception):
                    undo()
            self.fences.forget(key)
            self._done.pop(key, None)
            self._titles.pop(key, None)
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
        with contextlib.suppress(Exception):
            await self.mutations.aclose()
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
            # The session's mutation gate is held until the turn runs: no change rebuilds the
            # agent while it is prepared, and changes deferred to this boundary apply first.
            async with self.mutations.gate(key):
                if key not in self._live:
                    await self.resume(key)
                if ticket.lease_waited:
                    await self.service.reload(key)
                await self.mutations.before_turn(key)
                if ticket.stop_reason is not None:
                    return _not_started(ticket)
                if ticket.admission.payload.get(UNATTENDED) is True:
                    self._auto_approve(key, self.service.current_id(key))
                if ticket.admission.payload.get(SINGLE_QUERY) is True:
                    self._begin_one_shot(key)
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
            self._end_auto_approve(key)
            self._end_one_shot(key)
            if self._tickets.get(key) is ticket:
                del self._tickets[key]
            self._spawn(self._after_turn(key, ticket.admission.admission_id))

    def _auto_approve(self, key: str, session_id: str) -> None:
        """Switch the session's auto-approve on for this turn, unless it already is (a person's
        ``/yolo`` stays as they set it)."""
        approval_key = session_key(self.store.scope, session_id)
        if APPROVALS.auto_approve(approval_key):
            return
        APPROVALS.set_auto_approve(approval_key, True)
        self._auto.setdefault(key, set()).add(session_id)

    def _end_auto_approve(self, key: str) -> None:
        for session_id in self._auto.pop(key, set()):
            APPROVALS.set_auto_approve(session_key(self.store.scope, session_id), False)

    async def set_auto_approve(self, key: str, enabled: bool) -> None:
        """A person's ``/yolo`` (``config.set``, W50-a) for the hosted session ``key``: written to
        its row first (a resume keeps it), then switched. An unattended turn that switched it on
        no longer switches it off at its end: the person's choice stays."""
        sid = self.service.current_id(key)
        self._auto.get(key, set()).discard(sid)

        def remember() -> bool:
            return sessions.set_yolo(self.store, sid, enabled)

        try:
            await self._off(remember)
        except Exception:
            log.warning("could not store the auto-approve switch of %s", sid, exc_info=True)
        APPROVALS.set_auto_approve(session_key(self.store.scope, sid), enabled)

    def _begin_one_shot(self, key: str) -> None:
        """Run this turn as the in-process one-shot run does: the run context ``single_query``
        (approvals follow ``approvals.single_query_mode``, sudo stays headless) and a broker that
        asks nobody (clarify, secrets and the rest are unsupported)."""
        runtime = self.service.agent(self.service.current_id(key)).runtime
        self._one_shot.setdefault(key, runtime.run_context)
        runtime.run_context = "single_query"
        live = self._live.get(key)
        if live is not None:
            live.broker.asks_nobody = True

    def _end_one_shot(self, key: str) -> None:
        if key not in self._one_shot:
            return
        before = self._one_shot.pop(key)
        live = self._live.get(key)
        if live is not None:
            live.broker.asks_nobody = False
        with contextlib.suppress(Exception):  # the session may be gone with its turn
            self.service.agent(self.service.current_id(key)).runtime.run_context = before

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
        if key in self._auto:  # the unattended turn goes on under the child id
            self._auto_approve(key, new)
        if key in self._one_shot:  # so does a one-shot turn
            with contextlib.suppress(Exception):
                self.service.agent(new).runtime.run_context = "single_query"
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


def _field(event: EventBase | Mapping[str, Any], name: str) -> str:
    value = event.get(name) if isinstance(event, Mapping) else getattr(event, name, "")
    return str(value or "")


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
