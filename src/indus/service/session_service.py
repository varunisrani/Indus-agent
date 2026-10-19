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

``compress`` runs a manual ``/compress`` between turns (P08-05). A rotating compaction continues a
conversation in a child session: the attached session moves to the child id, the old id keeps
working as an alias for every call, and the session lock moves with it.

Generations are counted per attached session, in process, until P17's turn lease carries them.

A ``maintenance`` timer (P09-18, ``service.maintenance``), when given, starts with the first
attached session and stops at ``aclose``; its chores run off the event loop.
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

from indus.core import references
from indus.core.agent import Agent, AgentBusy, AgentOptions, RunningTurn
from indus.core.compression.engine import Outcome
from indus.core.events import EventListener, EventSink, RequestListener
from indus.core.persist import MemoryStore, StateStore, TranscriptStore
from indus.core.turn_state import TurnResult
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
from indus.service.broker import ServerRequestBroker
from indus.service.maintenance import MaintenanceTimer
from indus.state import sessions, titles
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
    "SessionService",
    "protocol_error_for",
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

    # -- create / resume --------------------------------------------------------------------------

    async def create(self, params: SessionCreateParams | None = None) -> SessionCreateResult:
        """Mint a session and attach it."""
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
            options, selection = self._options_for(params)
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

    def _options_for(self, params: SessionCreateParams) -> tuple[AgentOptions, list[str] | None]:
        options = self.options
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
            broker = ServerRequestBroker(
                session_id=session_id,
                deliver=sink.request,
                emit=sink.emit,
                next_seq=sink.next_seq,
                clock=self.scope.clock,
                answerable_kinds=self._answerable,
            )
            agent = await self._factory(session_id, sink, broker, options, selection)
        except BaseException:
            taken.release()
            raise
        attached = _Attached(session_id, agent, sink, broker, taken)
        self._attached[session_id] = attached
        agent.on_session_moved(lambda old, new: self._follow(attached, old, new))
        return attached

    def _follow(self, attached: _Attached, old: str, new: str) -> None:
        """A rotating compaction moved ``attached`` from ``old`` to ``new`` (see the module notes).
        The lock moves when the child's can be taken; else the parent's stays held."""
        if self._attached.get(old) is attached:
            del self._attached[old]
        self._attached[new] = attached
        self._aliases[old] = new
        attached.session_id = new
        attached.broker.session_id = new
        if old in self._titling:
            self._titling.add(new)
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
        self, attached: _Attached, text: str, *, message: str | None = None
    ) -> RunningTurn:
        if attached.running is not None:
            raise _busy(f"session {attached.session_id} is running a turn")
        try:
            if message is not None and message != text:
                running = await attached.agent.start_turn(message, persist_text=text)
            else:
                running = await attached.agent.start_turn(text)
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
        control = running.state.control
        left = [text for ticket, text in attached.steers.items() if control.withdraw_steer(ticket)]
        if result.pending_steer:
            left.append(result.pending_steer)
        attached.steers = {}
        if not left or attached.agent.closed or self._closed:
            return
        if result.exit_reason in _INTERRUPTED:
            log.info(
                "turn %s was interrupted; %d undelivered steer text(s) dropped",
                running.turn_id,
                len(left),
            )
            return
        attached.agent.spawn(self._follow_up(attached, "\n".join(left)), name="steer follow-up")

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
        if attached.running is not None or attached.agent.closed:
            return
        try:
            await self._start(attached, text)
        except Exception:
            log.warning("could not start the steer follow-up turn", exc_info=True)

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
        self, session_id: str, generation: int | None = None, *, hard: bool = False
    ) -> SessionInterruptResult:
        """Stop the running turn (only ``generation`` when given; a stale one is a no-op)."""
        attached = self._get(session_id)
        done = attached.agent.interrupt(generation, hard=hard)
        if done:
            attached.broker.cancel_all(CancelReason.INTERRUPTED)
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
        try:
            await attached.agent.close()
        except Exception:
            log.warning("closing the agent of %s failed", session_id, exc_info=True)
        finally:
            attached.broker.close()
            attached.lock.release()

    async def aclose(self) -> None:
        """Detach every session; the service accepts no more calls."""
        self._closed = True
        await self._stop_maintenance()
        for session_id in list(self._attached):
            await self.close(session_id)

    def _start_maintenance(self) -> None:
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
