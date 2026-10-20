"""The session service: the one API every surface drives a session through (P04-21).

``SessionService`` keeps, per attached session, the agent (P04-20), its event sink, its
server-request broker and the per-session advisory lock (D-16.69). Every method runs on the
service's event loop; ``InProcessHost`` marshals calls from other threads.

- ``create`` mints a session and attaches it (a ``title`` writes the row at once and stores the
  title with user provenance); ``resume`` attaches an existing one (a session this
  service already has attached is simply described again — a reconnect).
- Admission: attaching takes the session's lock without waiting. Another surface holding it, in
  this process or another, gives ``SESSION_BUSY``; so does ``submit`` while a turn runs. A
  ``submit`` for a stored session that is not attached yet attaches it first.
- ``submit`` expands ``@`` context references (P08-19) — the typed text is stored as the row's
  content, the expansion as its wire text, refused references become a notice — then starts a
  turn and returns its ticket (turn id + generation). When the turn ends its
  open server requests are withdrawn (``turn_ended``); steer text it never delivered starts the
  next turn, unless the turn was interrupted.
- ``interrupt`` / ``steer`` / ``redirect`` reach the running turn; ``answer_server_request`` goes
  to the broker; ``events(session, since)`` replays the kept events after ``since``.
- Every failure leaves as a ``ProtocolError`` from the protocol's table; product errors are mapped
  through ``error_for_indus_code``.

After a session's first completed exchange, an ``aux`` client (P08-17), when given, makes its
title on the side (``llm`` provenance); the turn never waits for it. The same client is handed to
every agent (``AgentOptions.aux``) for its compression summaries.

Between turns (P10): ``inject`` starts a turn with an ordinary user message on the host's own
initiative (goal continuations, loop ticks, heartbeats) — only while the session is idle, which
means no running turn and nothing waiting in the surface's input queue (``set_input_probe``), so a
message the person sent always wins. ``on_turn_end`` listeners hear every finished turn;
``notify`` sends a host event (``goal.update`` and friends) on the session's stream. The service is
the ``SessionHost`` of ``core.session_scheduler``.

``compress`` runs a manual ``/compress`` between turns (P08-05). A rotating compaction continues a
conversation in a child session: the attached session moves to the child id, the old id keeps
working as an alias for every call, and the session lock moves with it.

Background subagents (P10-05): their results arrive through ``deliver_internal`` and enter the
conversation between turns — at once when the session is idle, else when the running turn ends
(after a steer follow-up, if one starts). ``stop_subagents`` (also run by a hard ``interrupt``
and by ``close``) stops the session's detached children.

Generations are counted per attached session, in process, until P17's turn lease carries them.

A ``maintenance`` timer (P09-18, ``service.maintenance``), when given, starts with the first
attached session and stops at ``aclose``; its chores run off the event loop.

``execute_code`` kernels (P10-10/11, ``service.code_host``): ``close`` disposes the closed
session's kernels (its current id and every id it continued from). ``kernels``, when given, is
the host's share: its idle reaper starts with the first attached session and ``aclose`` shuts both
kernel pools down — pass it only from the host that owns the process.

MCP servers (P13-05..12, ``service.mcp_host``): ``mcp``, when given, starts the profile's servers
before the first agent is built (discovery runs once), adds the umbrella ``mcp`` toolset to each
agent's selection, tells every attached agent when the tool surface changes (rebuilt at its next
compaction) and closes the servers at ``aclose``.

Plugins and shell hooks (P13-13..23, ``service.plugin_host``): ``plugins``, when given, loads the
profile's allowed plugins and the agreed shell hooks before the first agent is built, adds the
plugins' toolsets to each selection, gives each top-level agent the hooks-bus seams and the frozen
plugin prompt sections, runs ``on_session_start`` after a session is attached and
``on_session_end`` before it is closed, and takes every registration back at ``aclose``.

Daemon seams (P17-08, ``service.daemon_host``): ``broker_factory`` builds each session's broker
(the daemon hands it the session's ``RequestBook``); with a ``turn_door`` every turn the service
would start on its own — host injections, background-subagent results, undelivered steer text —
is handed to the door (the daemon's admission ledger) instead; ``start_admitted`` starts a turn
the door admitted under the ledger's generation; ``reload`` rebuilds a session's agent from the
store (another process ran it meanwhile); an ``agent_shaper``, when set, adjusts a session's
options and toolset selection right before its agent is built, on attach and on reload (the daemon
gives an editor session its own tool registry and folder there, ``service.session_mutations``).
``events_since`` and ``receipt`` have trivial in-process forms: one replay epoch per service over
the kept events, and no admissions.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from collections import OrderedDict
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from indus.core import references, subagents
from indus.core.agent import Agent, AgentBusy, AgentOptions, RunningTurn
from indus.core.compression.engine import Outcome
from indus.core.events import EventListener, EventSink, RequestListener
from indus.core.persist import MemoryStore, StateStore, TranscriptStore
from indus.core.session_scheduler import TurnEnd, TurnEndListener
from indus.core.turn_state import TurnResult
from indus.cron import tool as cron_tool
from indus.foundation.blocking import run_blocking
from indus.foundation.errors import IndusError
from indus.foundation.ids import new_session_id
from indus.foundation.scope import Scope
from indus.protocol.errors import (
    IDEMPOTENCY_CONFLICT,
    INTERNAL_ERROR,
    SESSION_BUSY,
    SESSION_NOT_FOUND,
    TEXT_EMPTY,
    error_for_indus_code,
)
from indus.protocol.events import CancelReason, EventBase, ExitReason, Notice, NoticeLevel
from indus.protocol.methods import (
    PromptSubmitResult,
    ProtocolError,
    RedirectOutcome,
    ServerRequestAnswerResult,
    SessionCreateParams,
    SessionCreateResult,
    SessionInterruptResult,
    SessionRedirectResult,
    SessionResumeResult,
    SessionSteerResult,
)
from indus.protocol.requests_port import AnswerModel
from indus.providers.auxiliary import AuxClient, auto_title, generate_title
from indus.providers.route import RuntimeRoute
from indus.providers.types import Usage
from indus.service import code_host
from indus.service.admissions import Receipt
from indus.service.broker import ServerRequestBroker
from indus.service.code_host import KernelUpkeep
from indus.service.event_log import Replay, ReplayGap, new_replay_epoch
from indus.service.maintenance import MaintenanceTimer
from indus.service.mcp_host import McpUpkeep
from indus.service.plugin_host import PluginUpkeep
from indus.state import lineage, sessions, titles
from indus.state.runtime_ledger import LedgerRefusal
from indus.state.session_lock import (
    LOCK_DIR_NAME,
    LockBusy,
    SessionLock,
    lock_dir_for,
    try_lock_session,
)

__all__ = [
    "BUSY_COPY",
    "AgentFactory",
    "AgentShaper",
    "BrokerFactory",
    "SessionService",
    "TurnDoor",
    "protocol_error_for",
    "usage_numbers",
]

log = logging.getLogger(__name__)

# User-facing copy for a busy session; mirrored by ``errors.session.busy`` in locales/en.yaml
# (a test keeps them equal).
BUSY_COPY: dict[str, str] = {
    "what": (
        "This conversation is busy: a reply is still running, or another Indus Agent window "
        "has it open."
    ),
    "todo": "Wait for the reply to finish, or close the other window, then try again.",
}

KEPT_RESULTS = 20
"""Turn results kept per session for ``wait``."""

_INTERRUPTED = frozenset({ExitReason.INTERRUPTED_BY_USER, ExitReason.INTERRUPTED_BY_SYSTEM})

type AgentFactory = Callable[
    [str, EventSink, ServerRequestBroker, AgentOptions, Sequence[str] | None], Awaitable[Agent]
]
"""Builds the agent of one session: ``(session_id, sink, broker, options, toolsets)``."""

type AgentShaper = Callable[
    [str, AgentOptions, Sequence[str] | None],
    Awaitable[tuple[AgentOptions, list[str] | None]],
]
"""Adjusts one session's agent before it is built: ``(session_id, options, toolsets)`` → the
options and toolsets to build it with."""

type BrokerFactory = Callable[[str, EventSink], ServerRequestBroker]
"""Builds the server-request broker of one session: ``(session_id, sink)``."""

type TurnDoor = Callable[[str, str, str], Awaitable[bool]]
"""Takes a turn the service would start itself: ``(session_id, text, origin)`` → accepted."""


def protocol_error_for(exc: BaseException) -> ProtocolError:
    """``exc`` as a ``ProtocolError``: itself, a mapped product error, or an internal error."""
    if isinstance(exc, ProtocolError):
        return exc
    if isinstance(exc, IndusError):
        return ProtocolError(error_for_indus_code(exc.code), exc.message)
    return ProtocolError(INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")


def _reference_budget(agent: Agent) -> int | None:
    """Characters ``@`` references may add (P08-19): the room under the compression trigger
    (P08-02) after the last reported prompt; a quarter of the window when compression is off."""
    try:
        engine = agent.context_engine
        if engine is not None and engine.trigger > 0:
            return references.budget_for_trigger(engine.trigger, agent.usage.last_prompt_tokens)
        return references.budget_for_window(agent.binding.state.window.tokens)
    except Exception:
        log.debug("no reference budget known", exc_info=True)
        return None


def _busy(detail: str) -> ProtocolError:
    log.info("busy: %s", detail)
    return ProtocolError(SESSION_BUSY, f"{BUSY_COPY['what']} {BUSY_COPY['todo']}")


def _check_text(text: str, path: str = "text") -> str:
    if not text or not text.strip():
        raise ProtocolError(TEXT_EMPTY, path=path)
    return text


@contextlib.asynccontextmanager
async def _mapped() -> AsyncGenerator[None]:
    try:
        yield
    except (ProtocolError, asyncio.CancelledError):
        raise
    except Exception as exc:
        if not isinstance(exc, IndusError):
            log.error("session service call failed", exc_info=exc)
        raise protocol_error_for(exc) from exc


@dataclass
class _Attached:
    """One session attached to this service."""

    session_id: str
    agent: Agent
    sink: EventSink
    broker: ServerRequestBroker
    lock: SessionLock
    turn: RunningTurn | None = None
    results: OrderedDict[str, TurnResult] = field(default_factory=OrderedDict[str, TurnResult])
    steers: dict[str, str] = field(default_factory=dict[str, str])
    settled: set[str] = field(default_factory=set[str])
    internal: list[str] = field(default_factory=list[str])  # waiting for the turn to end
    undo: list[Callable[[], None]] = field(default_factory=list[Callable[[], None]])

    @property
    def running(self) -> RunningTurn | None:
        turn = self.turn
        return turn if turn is not None and not turn.done else None


class SessionService:
    """Sessions of one profile, run in this process (see the module notes)."""

    def __init__(
        self,
        scope: Scope,
        route: RuntimeRoute,
        *,
        toolsets: Sequence[str] | None = None,
        options: AgentOptions | None = None,
        store: TranscriptStore | None = None,
        lock_dir: Path | None = None,
        answerable_kinds: Iterable[str] = (),
        owner: str = "service",
        agent_factory: AgentFactory | None = None,
        aux: AuxClient | None = None,
        maintenance: MaintenanceTimer | None = None,
        kernels: KernelUpkeep | None = None,
        mcp: McpUpkeep | None = None,
        plugins: PluginUpkeep | None = None,
        broker_factory: BrokerFactory | None = None,
        turn_door: TurnDoor | None = None,
    ) -> None:
        self.scope = scope
        self.route = route
        self.toolsets = list(toolsets) if toolsets is not None else None
        self.options = options or AgentOptions()
        if aux is not None and self.options.aux is None:
            self.options = replace(self.options, aux=aux)
        self.store: TranscriptStore = store if store is not None else MemoryStore()
        self.lock_dir = lock_dir if lock_dir is not None else self._default_lock_dir()
        self.owner = owner
        self._answerable = tuple(answerable_kinds)
        self._factory = agent_factory or self._make_agent
        self._attached: dict[str, _Attached] = {}
        self._created: set[str] = set()
        self._idempotency: dict[str, tuple[SessionCreateParams, str]] = {}
        self._closed = False
        self.aux = aux
        self._titling: set[str] = set()
        self._aliases: dict[str, str] = {}
        self.maintenance = maintenance
        self._maintenance_task: asyncio.Task[None] | None = None
        self.kernels = kernels
        self.mcp = mcp
        self.plugins = plugins
        self._input_probe: Callable[[str], bool] | None = None
        self._turn_end: list[TurnEndListener] = []
        self._moved: list[Callable[[str, str], object]] = []
        self.broker_factory = broker_factory
        self.turn_door = turn_door
        self.agent_shaper: AgentShaper | None = None
        self.replay_epoch = new_replay_epoch()
        """The in-process replay epoch (``events_since``); the daemon's event log has its own."""

    def _default_lock_dir(self) -> Path:
        if isinstance(self.store, StateStore):
            return lock_dir_for(self.store.handle.path)
        return self.scope.home / LOCK_DIR_NAME

    # -- configuration ----------------------------------------------------------------------------

    @property
    def answerable_kinds(self) -> tuple[str, ...]:
        return self._answerable

    def set_answerable_kinds(self, kinds: Iterable[str]) -> None:
        """The server-request kinds the attached client can answer (R05.3)."""
        self._answerable = tuple(kinds)
        for attached in self._attached.values():
            attached.broker.set_answerable_kinds(self._answerable)

    def attached(self) -> list[str]:
        return list(self._attached)

    def current_id(self, session_id: str) -> str:
        """The id ``session_id`` is attached under now (a rotating compaction moves it)."""
        seen: set[str] = set()
        while session_id in self._aliases and session_id not in seen:
            seen.add(session_id)
            session_id = self._aliases[session_id]
        return session_id

    def agent(self, session_id: str) -> Agent:
        return self._get(session_id).agent

    # -- host side (P10: goals, loops, heartbeats) ------------------------------------------------

    def set_input_probe(self, probe: Callable[[str], bool] | None) -> None:
        """``probe(session_id)`` true means the surface holds input for that session that has not
        been sent yet (a queued message): the session is not idle then. ``None`` removes it."""
        self._input_probe = probe

    def _input_waiting(self, session_id: str) -> bool:
        probe = self._input_probe
        if probe is None:
            return False
        try:
            return bool(probe(session_id))
        except Exception:
            log.warning("the input probe failed for session %s", session_id, exc_info=True)
            return True  # unknown: do not start a turn over it

    def is_idle(self, session_id: str) -> bool:
        """Attached, no turn running and nothing waiting in its input queue."""
        session_id = self.current_id(session_id)
        attached = self._attached.get(session_id)
        if attached is None or attached.running is not None or attached.agent.closed:
            return False
        return not self._input_waiting(session_id)

    def generation(self, session_id: str) -> int:
        """Turns the session's agent started in this process (0 before the first)."""
        return self._get(session_id).agent.generation

    async def inject(
        self, session_id: str, text: str, *, origin: str, if_generation: int | None = None
    ) -> bool:
        """Start a turn with ``text`` as an ordinary user message when the session is idle (and,
        with ``if_generation``, no other turn started since). False otherwise; never raises for a
        busy or detached session."""
        if self._closed or not text.strip():
            return False
        session_id = self.current_id(session_id)
        attached = self._attached.get(session_id)
        if attached is None or not self.is_idle(session_id):
            return False
        if if_generation is not None and attached.agent.generation != if_generation:
            return False
        if self.turn_door is not None:
            return await self._through_door(attached, text, origin)
        try:
            await self._start(attached, text)
        except ProtocolError as exc:
            log.info("%s message for session %s not started: %s", origin, session_id, exc)
            return False
        log.info("started a %s turn in session %s", origin, session_id)
        return True

    def notify(self, session_id: str, event: type[EventBase], **fields: Any) -> None:
        """Send one host event on the session's stream; a failure is logged, never raised."""
        attached = self._attached.get(self.current_id(session_id))
        if attached is None:
            return
        try:
            sink = attached.sink
            sink.emit(event(session_id=attached.session_id, seq=sink.next_seq(), **fields))
        except Exception:
            log.warning("could not send a %s event", event.__name__, exc_info=True)

    def on_turn_end(self, listener: TurnEndListener) -> Callable[[], None]:
        """Call ``listener(TurnEnd)`` after each turn of every attached session (coroutines run
        as the agent's tasks); returns the undo."""
        self._turn_end.append(listener)
        return lambda: self._turn_end.remove(listener) if listener in self._turn_end else None

    def on_session_moved(self, listener: Callable[[str, str], object]) -> Callable[[], None]:
        """Call ``listener(old id, new id)`` when a rotating compaction moves an attached session
        (coroutines run as the agent's tasks); returns the undo."""
        self._moved.append(listener)
        return lambda: self._moved.remove(listener) if listener in self._moved else None

    def _tell_moved(self, attached: _Attached, old: str, new: str) -> None:
        for listener in list(self._moved):
            try:
                outcome = listener(old, new)
                if asyncio.iscoroutine(outcome):
                    attached.agent.spawn(outcome, name="session-moved listener")
            except Exception:
                log.warning("a session-moved listener failed", exc_info=True)

    def _tell_turn_end(self, attached: _Attached, running: RunningTurn, result: TurnResult) -> None:
        if not self._turn_end:
            return
        ended = TurnEnd(
            session_id=attached.session_id,
            turn_id=running.turn_id,
            final_text=result.final_text or "",
            completed=result.exit_reason is ExitReason.COMPLETED,
            generation=running.generation,
        )
        for listener in list(self._turn_end):
            try:
                outcome = listener(ended)
                if asyncio.iscoroutine(outcome):
                    attached.agent.spawn(outcome, name="turn-end listener")
            except Exception:
                log.warning("a turn-end listener failed", exc_info=True)

    # -- create / resume --------------------------------------------------------------------------

    async def create(
        self, params: SessionCreateParams | None = None, *, source: str | None = None
    ) -> SessionCreateResult:
        """Mint a session and attach it. ``source`` names the surface the session is created for
        when this service hosts several (the daemon: ``cli``, ``tui``, ...); its row is stored
        under that source, as an in-process surface's would be."""
        params = params or SessionCreateParams()
        async with _mapped():
            self._check_open()
            key = params.idempotency_key
            if key is not None and key in self._idempotency:
                first, session_id = self._idempotency[key]
                if first != params:
                    raise ProtocolError(IDEMPOTENCY_CONFLICT, path="idempotency_key")
                return SessionCreateResult(session_id=session_id, created=False)
            session_id = new_session_id(self.scope.clock)
            options, selection = self._options_for(params, source)
            await self._attach(session_id, options=options, selection=selection)
            self._start_maintenance()
            if params.title:
                try:
                    await self._store_title(session_id, params.title, options)
                except BaseException:
                    await self.close(session_id)
                    raise
            self._created.add(session_id)
            if key is not None:
                self._idempotency[key] = (params, session_id)
            return SessionCreateResult(session_id=session_id, created=True)

    async def resume(self, session_id: str) -> SessionResumeResult:
        """Attach an existing session (or describe it again when it already is attached)."""
        async with _mapped():
            self._check_open()
            session_id = self.current_id(session_id)
            attached = self._attached.get(session_id)
            if attached is None:
                attached = await self._attach_existing(session_id)
            self._start_maintenance()
            rows = await self.store.load(session_id)
            return SessionResumeResult(
                session_id=session_id,
                message_count=len(rows),
                last_seq=max(attached.sink.last_seq, 0),
                pending_requests=list(attached.broker.pending_requests()),
            )

    async def _through_door(self, attached: _Attached, text: str, origin: str) -> bool:
        assert self.turn_door is not None
        try:
            accepted = await self.turn_door(attached.session_id, text, origin)
        except Exception:
            log.warning("the turn door refused a %s message", origin, exc_info=True)
            return False
        if accepted:
            log.info("handed a %s turn of session %s to the door", origin, attached.session_id)
        return accepted

    async def _store_title(self, session_id: str, title: str, options: AgentOptions) -> None:
        """A title given at create: the row is written now (otherwise the first turn writes it)
        and titled with the user's authority, so automatic titling never replaces it."""
        store = self.store
        await store.ensure_session(
            session_id,
            source=options.source or options.platform,
            system_prompt="",
            fields=options.session_fields,
        )
        if isinstance(store, StateStore):
            await run_blocking(
                titles.set_title, store.handle, session_id, title, label="session title"
            )
        elif isinstance(store, MemoryStore):
            store.sessions.setdefault(session_id, {})["title"] = title

    def _options_for(
        self, params: SessionCreateParams, source: str | None = None
    ) -> tuple[AgentOptions, list[str] | None]:
        options = self.options
        if source:
            options = replace(options, source=source)
        fields = dict(options.session_fields)
        if params.cwd:
            options = replace(options, cwd=Path(params.cwd))
            fields["cwd"] = params.cwd
        if params.model:
            fields["model"] = params.model
        options = replace(options, session_fields=fields)
        selection = list(params.toolsets) if params.toolsets is not None else self.toolsets
        return options, selection

    async def _exists(self, session_id: str) -> bool:
        if session_id in self._created:
            return True
        store = self.store
        if isinstance(store, StateStore):
            row = await run_blocking(sessions.get, store.handle, session_id, label="session row")
            return row is not None
        if isinstance(store, MemoryStore):
            return session_id in store.sessions or session_id in store.rows
        return bool(await store.load(session_id))

    async def _attach_existing(self, session_id: str) -> _Attached:
        if not session_id or not await self._exists(session_id):
            raise ProtocolError(SESSION_NOT_FOUND, f"no session {session_id}", path="session_id")
        return await self._attach(session_id, options=self.options, selection=self.toolsets)

    async def _attach(
        self, session_id: str, *, options: AgentOptions, selection: list[str] | None
    ) -> _Attached:
        taken = try_lock_session(self.lock_dir, session_id, owner=self.owner)
        if isinstance(taken, LockBusy):
            raise _busy(taken.describe())
        if taken.recovered_from:
            log.warning(
                "session %s was held by a process that died (%s); taking it over",
                session_id,
                taken.recovered_from,
            )
        try:
            sink = EventSink(session_id)
            broker = self._new_broker(session_id, sink)
            if self.agent_shaper is not None:
                options, selection = await self.agent_shaper(session_id, options, selection)
            if self.mcp is not None:
                await self.mcp.start()
                selection = self.mcp.selection(selection)
            if self.plugins is not None:
                await self.plugins.start()
                selection = self.plugins.selection(selection)
                options = self.plugins.options(options, session_id)
            agent = await self._factory(session_id, sink, broker, options, selection)
            if self.mcp is not None:
                self.mcp.attach(agent)
        except BaseException:
            taken.release()
            raise
        attached = _Attached(session_id, agent, sink, broker, taken)
        self._attached[session_id] = attached
        self._wire_agent(attached)
        if self.plugins is not None:
            await self.plugins.session_started(session_id, options)
        # `cronjob_manage run` results (P14-18) arrive the same way as background subagents'.
        attached.undo.append(
            cron_tool.register_result_sink(
                session_id, lambda text: self.deliver_internal(attached, text)
            )
        )
        return attached

    def _new_broker(self, session_id: str, sink: EventSink) -> ServerRequestBroker:
        if self.broker_factory is not None:
            return self.broker_factory(session_id, sink)
        return ServerRequestBroker(
            session_id=session_id,
            deliver=sink.request,
            emit=sink.emit,
            next_seq=sink.next_seq,
            clock=self.scope.clock,
            answerable_kinds=self._answerable,
        )

    def _wire_agent(self, attached: _Attached) -> None:
        agent = attached.agent
        agent.on_session_moved(lambda old, new: self._follow(attached, old, new))
        subagents.set_delivery(agent, lambda text: self.deliver_internal(attached, text))

    async def reload(self, session_id: str) -> str:
        """Rebuild the session's agent from the store (another process ran the session while
        this one waited for its lease); follows the lineage tip when that process compacted it.
        Busy while a turn runs. Returns the id the session is attached under now."""
        async with _mapped():
            self._check_open()
            attached = self._get(session_id)
            if attached.running is not None:
                raise _busy(f"session {attached.session_id} is running a turn")
            old = attached.agent
            tip = await self._lineage_tip(attached.session_id)
            if tip != attached.session_id:
                previous = attached.session_id
                attached.sink.session_id = tip
                self._follow(attached, previous, tip)
            options, selection = old.options, old.toolset_selection
            if self.agent_shaper is not None:
                options, selection = await self.agent_shaper(
                    attached.session_id, options, selection
                )
            agent = await self._factory(
                attached.session_id, attached.sink, attached.broker, options, selection
            )
            if self.mcp is not None:
                self.mcp.attach(agent)
            attached.agent = agent
            attached.turn = None
            self._wire_agent(attached)
            try:
                await old.close()
            except Exception:
                log.warning("closing the replaced agent of %s failed", session_id, exc_info=True)
            log.info("session %s reloaded from the store", attached.session_id)
            return attached.session_id

    async def _lineage_tip(self, session_id: str) -> str:
        store = self.store
        if not isinstance(store, StateStore):
            return session_id

        def tip() -> str:
            with store.handle.read() as conn:
                return lineage.tip_in(conn, session_id)

        try:
            return await run_blocking(tip, label="session lineage")
        except Exception:
            log.warning("could not follow the lineage of %s; keeping it", session_id, exc_info=True)
            return session_id

    def _follow(self, attached: _Attached, old: str, new: str) -> None:
        """A rotating compaction moved ``attached`` from ``old`` to ``new`` (see the module notes).
        The lock moves when the child's can be taken; else the parent's stays held."""
        if self._attached.get(old) is attached:
            del self._attached[old]
        self._attached[new] = attached
        self._aliases[old] = new
        attached.session_id = new
        attached.broker.session_id = new
        # `cronjob_manage run` results follow the session to its new id (P14-18).
        attached.undo.append(
            cron_tool.register_result_sink(new, lambda text: self.deliver_internal(attached, text))
        )
        if old in self._titling:
            self._titling.add(new)
        self._tell_moved(attached, old, new)
        taken = try_lock_session(self.lock_dir, new, owner=self.owner)
        if isinstance(taken, LockBusy):
            log.warning("session %s continues in %s, whose lock is held: %s", old, new, taken)
            return
        attached.lock.release()
        attached.lock = taken
        log.info("session %s continues in %s", old, new)

    async def _make_agent(
        self,
        session_id: str,
        sink: EventSink,
        broker: ServerRequestBroker,
        options: AgentOptions,
        selection: Sequence[str] | None,
    ) -> Agent:
        route = self.route
        model = options.session_fields.get("model")
        if isinstance(model, str) and model:
            route = replace(route, model=model)
        full = replace(options, session_id=session_id, store=self.store, broker=broker)
        return await Agent.start(self.scope, route, selection, full, sink)

    # -- turns ------------------------------------------------------------------------------------

    async def submit(self, session_id: str, text: str) -> PromptSubmitResult:
        """Start a turn with ``text``; returns its ticket. Busy while a turn runs."""
        async with _mapped():
            self._check_open()
            _check_text(text)
            attached = self._attached.get(self.current_id(session_id))
            if attached is None:
                attached = await self._attach_existing(session_id)
            if attached.running is not None:
                raise _busy(f"session {attached.session_id} is running a turn")
            message = await self._expand(attached, text)
            running = await self._start(attached, text, message=message)
            return PromptSubmitResult(turn_id=running.turn_id, generation=running.generation)

    async def start_admitted(self, session_id: str, text: str, *, generation: int) -> RunningTurn:
        """Start a turn the daemon's ledger admitted, under the ledger's ``generation``; attaches a
        stored session first. Busy while a turn runs."""
        async with _mapped():
            self._check_open()
            _check_text(text)
            attached = self._attached.get(self.current_id(session_id))
            if attached is None:
                attached = await self._attach_existing(session_id)
            if attached.running is not None:
                raise _busy(f"session {attached.session_id} is running a turn")
            message = await self._expand(attached, text)
            return await self._start(attached, text, message=message, generation=generation)

    async def _expand(self, attached: _Attached, text: str) -> str:
        """``text`` with its ``@`` references expanded (P08-19); refusals become a notice. A
        failure here never blocks the message: it is sent as typed."""
        if "@" not in text:
            return text
        agent = attached.agent
        cwd = agent.options.cwd or Path(str(agent.options.session_fields.get("cwd") or "."))
        try:
            expansion = await references.expand(
                text, self.scope, cwd.resolve(), budget_chars=_reference_budget(agent)
            )
        except Exception:
            log.warning(
                "could not expand the references in a message; sent as typed", exc_info=True
            )
            return text
        if expansion.refusals:
            lines = "; ".join(f"{r.token}: {r.reason}" for r in expansion.refusals)
            attached.sink.emit(
                Notice(
                    session_id=attached.session_id,
                    seq=attached.sink.next_seq(),
                    level=NoticeLevel.WARNING,
                    text=f"Not included: {lines}",
                    key="references_refused",
                )
            )
        return expansion.text

    async def compress(
        self,
        session_id: str,
        focus: str | None = None,
        *,
        keep_last: int | None = None,
        preview: bool = False,
    ) -> Outcome:
        """Manual ``/compress [focus] [here N] [--preview]`` between turns (P08-05). Busy while a
        turn runs; a rotating compaction moves the session (see the module notes)."""
        async with _mapped():
            self._check_open()
            attached = self._get(session_id)
            if attached.running is not None:
                raise _busy(f"session {attached.session_id} is running a turn")
            try:
                return await attached.agent.compress(focus, keep_last=keep_last, preview=preview)
            except AgentBusy as exc:
                raise _busy(str(exc)) from exc

    async def _start(
        self,
        attached: _Attached,
        text: str,
        *,
        message: str | None = None,
        generation: int | None = None,
    ) -> RunningTurn:
        if attached.running is not None:
            raise _busy(f"session {attached.session_id} is running a turn")
        try:
            if message is not None and message != text:
                running = await attached.agent.start_turn(
                    message, persist_text=text, generation=generation
                )
            else:
                running = await attached.agent.start_turn(text, generation=generation)
        except AgentBusy as exc:
            raise _busy(str(exc)) from exc
        attached.turn = running
        attached.steers = {}
        running.task.add_done_callback(lambda _: self._turn_ended(attached, running))
        return running

    def _turn_ended(self, attached: _Attached, running: RunningTurn) -> None:
        """End-of-turn bookkeeping; runs once per turn (the done callback or ``wait``)."""
        if running.turn_id in attached.settled:
            return
        attached.settled.add(running.turn_id)
        if len(attached.settled) > KEPT_RESULTS * 2:
            attached.settled = {running.turn_id}
        attached.broker.cancel_all(CancelReason.TURN_ENDED)
        task = running.task
        if task.cancelled() or task.exception() is not None:
            log.error(
                "turn %s of session %s ended without a result",
                running.turn_id,
                attached.session_id,
                exc_info=None if task.cancelled() else task.exception(),
            )
            return
        result = task.result()
        attached.results[running.turn_id] = result
        while len(attached.results) > KEPT_RESULTS:
            attached.results.popitem(last=False)
        self._maybe_title(attached, result)
        self._tell_turn_end(attached, running, result)
        control = running.state.control
        left = [text for ticket, text in attached.steers.items() if control.withdraw_steer(ticket)]
        if result.pending_steer:
            left.append(result.pending_steer)
        attached.steers = {}
        if attached.agent.closed or self._closed:
            return
        if left and result.exit_reason in _INTERRUPTED:
            log.info(
                "turn %s was interrupted; %d undelivered steer text(s) dropped",
                running.turn_id,
                len(left),
            )
            left = []
        if left:
            attached.agent.spawn(self._follow_up(attached, "\n".join(left)), name="steer follow-up")
            return
        if attached.internal:
            attached.agent.spawn(self._internal_turn(attached), name="internal message")

    def _maybe_title(self, attached: _Attached, result: TurnResult) -> None:
        """Start auto-titling after the session's first completed exchange (P08-17)."""
        session_id = attached.session_id
        aux = attached.agent.aux or self.aux
        if aux is None or session_id in self._titling or attached.agent.closed:
            return
        if result.exit_reason is not ExitReason.COMPLETED or not (result.final_text or "").strip():
            return
        users = [r for r in result.rows if r.role == "user" and not r.is_steer]
        if len(users) != 1 or not isinstance(users[0].content, str):
            return
        self._titling.add(session_id)
        attached.agent.spawn(
            self._title(attached, users[0].content, result.final_text or ""), name="auto title"
        )

    async def _title(self, attached: _Attached, first_user: str, first_reply: str) -> None:
        aux, store = attached.agent.aux or self.aux, self.store
        if aux is None:
            return
        ledger = attached.agent.usage

        async def record(
            task: str, route: RuntimeRoute, usage: Usage | None, latency: float
        ) -> None:
            await ledger.record(usage, route=route, latency_s=latency, task=task)

        try:
            if isinstance(store, StateStore):
                title = await auto_title(
                    aux, store.handle, attached.session_id, first_user, first_reply, record=record
                )
            else:
                title = await generate_title(aux, first_user, first_reply, record=record)
                if title and isinstance(store, MemoryStore):
                    row = store.sessions.setdefault(attached.session_id, {})
                    row.setdefault("title", title)
            log.info("session %s titled %r", attached.session_id, title)
        except Exception as exc:
            log.info("no automatic title for session %s: %s", attached.session_id, exc)

    async def _follow_up(self, attached: _Attached, text: str) -> None:
        if self.turn_door is not None:
            if not attached.agent.closed:
                await self._through_door(attached, text, "steer follow-up")
            return
        if attached.running is not None or attached.agent.closed:
            return
        try:
            await self._start(attached, text)
        except Exception:
            log.warning("could not start the steer follow-up turn", exc_info=True)

    # -- internal messages (P10-05) ---------------------------------------------------------------

    def deliver_internal(self, attached: _Attached, text: str) -> None:
        """Queue ``text`` (background subagent results) for the conversation, between turns."""
        if attached.agent.closed or self._closed:
            log.info("internal message for %s dropped: the session is closed", attached.session_id)
            return
        attached.internal.append(text)
        if attached.running is None:
            attached.agent.spawn(self._internal_turn(attached), name="internal message")

    async def _internal_turn(self, attached: _Attached) -> None:
        """Deliver the queued results through ``inject``: only while the session is idle, so a
        running turn or a message the person queued goes first; the next turn's end retries."""
        if attached.running is not None or attached.agent.closed or not attached.internal:
            return  # the running turn's end delivers it
        text = "\n\n".join(attached.internal)
        attached.internal.clear()
        try:
            started = await self.inject(attached.session_id, text, origin="subagent result")
        except Exception:
            started = False
            log.warning("could not start the turn for an internal message", exc_info=True)
        if not started:
            attached.internal.insert(0, text)

    async def stop_subagents(self, session_id: str) -> int:
        """Stop every background subagent of the session; how many were still working."""
        attached = self._attached.get(self.current_id(session_id))
        if attached is None:
            return 0
        attached.internal.clear()
        return await subagents.stop_detached(attached.agent)

    async def wait(self, session_id: str, turn_id: str | None = None) -> TurnResult:
        """The result of ``turn_id`` (default: the newest turn), waiting while it runs."""
        attached = self._get(session_id)
        turn = attached.turn
        if turn is not None and (turn_id is None or turn.turn_id == turn_id):
            result = await asyncio.shield(turn.task)
            self._turn_ended(attached, turn)  # settled before the caller looks again
            return result
        if turn_id is not None and turn_id in attached.results:
            return attached.results[turn_id]
        raise ProtocolError(SESSION_NOT_FOUND, f"no turn {turn_id} in session {session_id}")

    # -- control of the running turn ---------------------------------------------------------------

    def interrupt(
        self,
        session_id: str,
        generation: int | None = None,
        *,
        hard: bool = False,
        by_system: bool = False,
    ) -> SessionInterruptResult:
        """Stop the running turn (only ``generation`` when given; a stale one is a no-op)."""
        attached = self._get(session_id)
        done = attached.agent.interrupt(generation, hard=hard, by_system=by_system)
        if done:
            attached.broker.cancel_all(CancelReason.INTERRUPTED)
        if hard:  # a hard stop also ends the session's background subagents (P10-05)
            attached.agent.spawn(self.stop_subagents(session_id), name="stop subagents")
        return SessionInterruptResult(interrupted=done)

    def steer(self, session_id: str, text: str) -> SessionSteerResult:
        """Queue guidance for the running turn; ``queued`` False when none runs."""
        attached = self._get(session_id)
        _check_text(text)
        ticket = "st_" + secrets.token_hex(6)
        if not attached.agent.steer(text, ticket=ticket):
            return SessionSteerResult(queued=False)
        attached.steers[ticket] = text.strip()
        return SessionSteerResult(queued=True, ticket=ticket)

    def redirect(self, session_id: str, text: str) -> SessionRedirectResult:
        """Correct the running model call (steer during tools; rejected with no live turn)."""
        attached = self._get(session_id)
        _check_text(text)
        return SessionRedirectResult(outcome=RedirectOutcome(attached.agent.redirect(text)))

    def answer_server_request(
        self, session_id: str, request_id: str, answer: Mapping[str, Any] | AnswerModel
    ) -> ServerRequestAnswerResult:
        """Hand a client's answer to the waiting request."""
        attached = self._get(session_id)
        try:
            return attached.broker.answer(request_id, answer)
        except ValidationError as exc:  # pragma: no cover - the broker maps these itself
            raise protocol_error_for(exc) from exc

    # -- events -----------------------------------------------------------------------------------

    def events(self, session_id: str, since: int | None = None) -> list[EventBase]:
        """Kept events of the session with a seq above ``since`` (all kept ones for None)."""
        return self._get(session_id).sink.since(since)

    def events_since(self, session_id: str, epoch: str | None, cursor: int) -> Replay | ReplayGap:
        """The kept events after ``cursor`` in the in-process form of the daemon's replay: a gap
        when ``epoch`` is not this service's, the cursor is ahead, or events after it are gone.
        In-process seqs start at 0, so a client that saw nothing yet passes -1."""
        if cursor < -1:
            raise ValueError("cursor must be >= -1")
        sink = self._get(session_id).sink
        last = sink.last_seq
        if epoch != self.replay_epoch:
            return ReplayGap(session_id, self.replay_epoch, last, "epoch_changed")
        if cursor > last:
            return ReplayGap(session_id, self.replay_epoch, last, "ahead")
        kept = sink.since(None)
        if cursor < last and (not kept or kept[0].seq > cursor + 1):
            return ReplayGap(session_id, self.replay_epoch, last, "evicted")
        wire: list[dict[str, Any]] = []
        for event in kept:
            if event.seq > cursor:
                data = event.to_wire()
                data["replay_epoch"] = self.replay_epoch
                wire.append(data)
        return Replay(session_id, self.replay_epoch, tuple(wire), last)

    async def receipt(self, admission_id: str) -> Receipt:
        """In-process turns have no admissions: every id is unknown here (the daemon host
        answers from its ledger)."""
        raise LedgerRefusal("not_found", field="admission_id")

    def subscribe(self, session_id: str, listener: EventListener) -> Callable[[], None]:
        """Live events (called on the service loop); returns the unsubscribe callable."""
        return self._get(session_id).sink.subscribe(listener)

    def on_request(self, session_id: str, listener: RequestListener) -> Callable[[], None]:
        """Server requests as they are sent (called on the service loop)."""
        return self._get(session_id).sink.on_request(listener)

    # -- closing ----------------------------------------------------------------------------------

    async def close(self, session_id: str) -> None:
        """Detach a session: stop its turn, withdraw its requests, close its agent, release the
        lock. Unknown ids are ignored."""
        session_id = self.current_id(session_id)
        attached = self._attached.pop(session_id, None)
        if attached is None:
            return
        if self.plugins is not None:
            await self.plugins.session_ended(session_id, attached.agent.options)
        try:
            await attached.agent.close()
        except Exception:
            log.warning("closing the agent of %s failed", session_id, exc_info=True)
        finally:
            for undo in attached.undo:
                undo()
            attached.broker.close()
            attached.lock.release()
            await code_host.end_session(session_id, *self._ids_leading_to(session_id))

    async def aclose(self) -> None:
        """Detach every session; the service accepts no more calls."""
        self._closed = True
        await self._stop_maintenance()
        for session_id in list(self._attached):
            await self.close(session_id)
        if self.plugins is not None:
            await self.plugins.shutdown()
        if self.mcp is not None:
            await self.mcp.shutdown()
        if self.kernels is not None:
            await self.kernels.shutdown()

    def _ids_leading_to(self, session_id: str) -> list[str]:
        """The older ids that continue (after compression) as ``session_id``."""
        return [old for old in self._aliases if self.current_id(old) == session_id]

    def _start_maintenance(self) -> None:
        if self.kernels is not None and not self._closed:
            self.kernels.start()  # the kernel reaper of a long-lived host (P10-10)
        if self.maintenance is None or self._maintenance_task is not None or self._closed:
            return
        self._maintenance_task = asyncio.get_running_loop().create_task(
            self.maintenance.run(), name="maintenance timer"
        )

    async def _stop_maintenance(self) -> None:
        task, self._maintenance_task = self._maintenance_task, None
        if self.maintenance is not None:
            self.maintenance.stop()
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    # -- helpers ----------------------------------------------------------------------------------

    def _check_open(self) -> None:
        if self._closed:
            raise ProtocolError(INTERNAL_ERROR, "the session service is closed")

    def _get(self, session_id: str) -> _Attached:
        attached = self._attached.get(self.current_id(session_id))
        if attached is None:
            raise ProtocolError(
                SESSION_NOT_FOUND, f"session {session_id} is not attached", path="session_id"
            )
        return attached


def usage_numbers(service: SessionService, session_id: str) -> dict[str, Any]:
    """The session's counters in ``UsageNumbers`` shape (``session.usage``), the one definition
    the in-process bridge and the daemon host both send: the token counters, the last prompt size
    as the context in use, and the model's window when known."""
    agent = service.agent(session_id)
    numbers: dict[str, Any] = dict(agent.usage.snapshot().as_dict())
    for name in ("prompt_tokens", "total_tokens", "cost_status"):
        numbers.pop(name, None)
    numbers["context_tokens"] = agent.usage.last_prompt_tokens
    try:
        numbers["context_window"] = agent.binding.state.window.tokens
    except Exception:
        numbers["context_window"] = None
    return numbers
