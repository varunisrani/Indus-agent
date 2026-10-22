"""``indus acp --local``: the ACP backend over an in-process ``SessionService`` (P19-03).

The service is built the way the TUI bridge builds one (the profile's route, the session store,
built-in tools, MCP servers and plugins, the shared terminal runtime), off the event loop, on first
use. Sessions it creates have source ``acp`` and the platform's toolset; the editor policy is the
same as in attached mode (ask before edits; the editor's MCP specs held in memory only).

The working folder is per session: ``LocalHost`` wraps the service's agent factory and puts the
session's folder into every agent it builds, so a workspace move is "store the new folder, rebuild
the agent" (``SessionService.reload``), allowed only while the session is idle.

Admissions are this process's own: a submit with an ``input_id`` seen before for the session
returns its first admission (the editor's retry never starts a second turn); receipts come from
the turns this process saw end. A restarted process resumes sessions from the store with no open
turn, so the ``unknown`` state never occurs here.

After every turn the host sends ``message.complete`` (the final text) and ``session.usage`` on the
session's stream, the pair the daemon host sends, so the editor projection settles a reply the same
way in both modes (P19-09/10).

Session changes (P19-12): ``model`` resolves the route and rebinds the session's agent (idle), or
keeps the pick for the next turn boundary while a turn runs (a pick refused there is announced as a
notice); a target whose window is smaller than the context in use answers ``confirm`` until the
call says ``confirm: true``. ``rewind`` takes user turns back (``/undo``, ``/retry``), ``reset``
empties the conversation; both rebuild the agent from the store. ``run_command`` answers the
information commands an editor may run in process (``tools``, ``status``, ``usage``, ``version``).
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, Final, cast

from indus.acp import store_view, toolset
from indus.acp.backend import (
    Admission,
    BackendError,
    CancelOutcome,
    CommandReply,
    CreateRequest,
    EditorPolicy,
    EventSink,
    HostMode,
    ListedSession,
    LostTurn,
    MutationResult,
    PendingPrompt,
    SessionSnapshot,
    Settled,
    UsageInfo,
)
from indus.core.agent import Agent, AgentOptions
from indus.core.events import EventSink as AgentSink
from indus.core.persist import StateStore
from indus.core.session_scheduler import TurnEnd
from indus.foundation.blocking import run_blocking
from indus.foundation.errors import IndusError
from indus.foundation.scope import Scope
from indus.protocol.errors import SESSION_BUSY, SESSION_NOT_FOUND
from indus.protocol.events import (
    EventBase,
    ExitReason,
    MessageComplete,
    Notice,
    NoticeLevel,
    SessionUsage,
    TurnDone,
    TurnStarted,
)
from indus.protocol.methods import ProtocolError, SessionCreateParams
from indus.protocol.server_requests import ServerRequest, ServerRequestBase
from indus.providers.route import RuntimeRoute
from indus.service.broker import ServerRequestBroker
from indus.service.session_mutations import stored_pick
from indus.service.session_service import AgentFactory, SessionService, usage_numbers
from indus.state import rewind, sessions, titles
from indus.state import usage as usage_rows

__all__ = ["PLATFORM", "SOURCE", "LocalHost", "ServiceBuilder", "build_service", "window_guard"]

log = logging.getLogger(__name__)

PLATFORM: Final = "acp"
SOURCE: Final = "acp"
DEFAULT_TOOLSET: Final = "indus-acp"
_KEPT_ADMISSIONS: Final = 256

_CREATING: contextvars.ContextVar[EditorPolicy | None] = contextvars.ContextVar(
    "acp_creating_editor", default=None
)
"""The editor policy of the session ``create`` is building (its id is not known yet)."""

type ServiceBuilder = Callable[[AgentFactory], SessionService]
"""Builds the service (blocking) with the given agent factory installed."""


def _outcome(reason: ExitReason) -> str:
    if reason is ExitReason.COMPLETED:
        return "completed"
    if reason in (ExitReason.INTERRUPTED_BY_USER, ExitReason.INTERRUPTED_BY_SYSTEM):
        return "cancelled"
    return "failed"


def window_guard(used: int, window: int) -> str | None:
    """Ask first when the conversation in use does not fit the target model's window."""
    if window > 0 and used > window:
        return (
            f"This conversation uses about {used:,} tokens, more than the {window:,} the new "
            "model can take; the next message will be compressed first."
        )
    return None


def _backend_error(exc: BaseException) -> BackendError:
    if isinstance(exc, BackendError):
        return exc
    if isinstance(exc, ProtocolError):
        if exc.error.code == SESSION_NOT_FOUND.code:
            return BackendError("not_found", exc.message or "no such session")
        if exc.error.code == SESSION_BUSY.code:
            return BackendError("session_busy", exc.message or "the session is busy")
        return BackendError("backend_failed", exc.message or exc.error.name)
    if isinstance(exc, IndusError):
        hint = f" ({exc.hint})" if exc.hint else ""
        return BackendError("backend_failed", exc.message + hint)
    return BackendError("backend_failed", f"{type(exc).__name__}: the session call failed")


class _Turn:
    __slots__ = ("admission", "turn_id")

    def __init__(self, admission: Admission, turn_id: str) -> None:
        self.admission = admission
        self.turn_id = turn_id


class LocalHost:
    """``AcpBackend`` in this process (see the module notes)."""

    def __init__(
        self,
        scope: Scope,
        builder: ServiceBuilder,
        *,
        offload: Callable[[Callable[[], SessionService]], Awaitable[SessionService]] | None = None,
    ) -> None:
        self.scope = scope
        self._builder = builder
        self._offload = offload
        self._service: SessionService | None = None
        self._building: asyncio.Lock = asyncio.Lock()
        self._cwds: dict[str, str] = {}
        self._editors: dict[str, EditorPolicy] = {}
        self._inputs: OrderedDict[tuple[str, str], str] = OrderedDict()
        self._turns: OrderedDict[str, _Turn] = OrderedDict()
        self._by_turn: dict[str, str] = {}
        self._watch: dict[str, Callable[[], None]] = {}
        self._reasons: OrderedDict[str, ExitReason] = OrderedDict()
        self._picks: dict[str, dict[str, Any]] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self.model_guard: Callable[[int, int], str | None] = window_guard
        """``(context in use, target window) -> why to ask first`` (None: no need)."""
        self.resolve_route: Callable[[str, str | None], RuntimeRoute] = self._resolve
        """``(model, provider) -> route`` for a model change (blocking)."""
        self.mcp_opener: Any = None
        """The P13 opener editor MCP servers connect with (tests pass a scripted one)."""
        self.no_mcp = False
        self._editor_mcp: toolset.EditorServers | None = None
        self._connected: dict[str, list[str]] = {}

    @property
    def mode(self) -> HostMode:
        return "local"

    # -- the service ------------------------------------------------------------------------------

    async def service(self) -> SessionService:
        if self._service is not None:
            return self._service
        async with self._building:
            if self._service is None:
                factory: AgentFactory = self._factory

                def build() -> SessionService:
                    return self._builder(factory)

                try:
                    if self._offload is not None:
                        self._service = await self._offload(build)
                    else:
                        self._service = await run_blocking(build, label="acp local service")
                except BaseException as exc:
                    raise _backend_error(exc) from None
                built = self._service
                built.on_turn_end(self._turn_ended)
                self.no_mcp = self.no_mcp or _no_mcp(self.scope)
                return built
        return self._service

    async def _factory(
        self,
        session_id: str,
        sink: AgentSink,
        broker: ServerRequestBroker,
        options: AgentOptions,
        selection: Sequence[str] | None,
    ) -> Agent:
        service = self._service
        assert service is not None, "agents are built by the service this host made"
        cwd = self._cwds.get(session_id)
        if cwd:
            fields = {**options.session_fields, "cwd": cwd}
            options = replace(options, cwd=Path(cwd), session_fields=fields)
        editor = self._editors.get(session_id) or _CREATING.get()
        connected: list[str] = []
        if editor is not None and editor.mcp_servers:
            connected = await self._connect_editor(service, session_id, editor)
        chosen = toolset.session_selection(
            selection,
            config_servers=self._config_servers(service),
            editor_servers=connected,
            no_mcp=self.no_mcp,
        )
        agent = await service._make_agent(  # pyright: ignore[reportPrivateUsage]
            session_id, sink, broker, options, chosen
        )
        await self._restore_route(agent, session_id)
        return agent

    async def _restore_route(self, agent: Agent, session_id: str) -> None:
        """A stored session comes back on the model its row names (a switch made before the
        editor restarted); a new session has no row yet."""
        try:
            row = await run_blocking(
                store_view.session_row, self.scope, session_id, label="acp session row"
            )
            pick = stored_pick(row, agent.binding.state.route)
            if pick is None:
                return
            model, provider = pick
            route = await run_blocking(
                lambda: self.resolve_route(model, provider), label="acp model route"
            )
            await agent.binding.apply(route)
        except Exception as exc:
            log.info("the stored model of %s is not used (%s)", session_id, type(exc).__name__)

    # -- editor MCP servers (P19-13) --------------------------------------------------------------

    @staticmethod
    def _config_servers(service: SessionService) -> list[str]:
        upkeep = service.mcp
        host = getattr(upkeep, "host", None) if upkeep is not None else None
        handles = getattr(host, "handles", None)
        return sorted(handles) if isinstance(handles, dict) else []

    async def _connect_editor(
        self, service: SessionService, session_id: str, editor: EditorPolicy
    ) -> list[str]:
        """Connect the editor's servers through the P13 client; the names whose tools arrived
        (a listing that changed after it was frozen is announced as a notice)."""
        registry = service.options.registry
        if registry is None:
            log.warning("no tool registry: the editor's MCP servers are not connected")
            return []
        if self._editor_mcp is None:
            self._editor_mcp = toolset.EditorServers(self.scope, registry, opener=self.mcp_opener)
        try:
            connected, changed = await self._editor_mcp.attach(editor.mcp_servers)
        except Exception:
            log.warning("the editor's MCP servers could not be connected", exc_info=True)
            return []
        self._connected[session_id] = connected
        if changed:
            service.notify(
                session_id, Notice, level=NoticeLevel.INFO, text=", ".join(changed),
                key="tools_changed",
            )  # fmt: skip
        return connected

    async def _rebind_editor(self, service: SessionService, sid: str) -> None:
        """New editor servers for an attached session: rebuilt now when idle, else from the
        agent's next tool rebuild; either way a notice says the list changes."""
        if sid not in service.attached():
            return
        servers = ", ".join(self._editors[sid].server_names) or "none"
        if service.is_idle(sid):
            await self._reload(service, sid)
        else:
            editor = self._editors[sid]
            connected = await self._connect_editor(service, sid, editor)
            agent = service.agent(sid)
            agent.tools_changed(
                toolset.session_selection(
                    agent.toolset_selection,
                    config_servers=self._config_servers(service),
                    editor_servers=connected,
                    no_mcp=self.no_mcp,
                )
            )
        service.notify(sid, Notice, level=NoticeLevel.INFO, text=servers, key="tools_changed")

    def _handle(self, service: SessionService) -> Any:
        store = service.store
        if not isinstance(store, StateStore):
            raise BackendError("mutation_unavailable", "this host has no session store")
        return store.handle

    # -- sessions ---------------------------------------------------------------------------------

    async def create(self, request: CreateRequest) -> SessionSnapshot:
        service = await self.service()
        model = request.launch.fields.get("model")
        params = SessionCreateParams(
            cwd=request.cwd or None,
            model=model if isinstance(model, str) and model else None,
            idempotency_key=request.request_id,
        )
        token = _CREATING.set(request.editor)
        try:
            created = await service.create(params)
        except BaseException as exc:
            raise _backend_error(exc) from None
        finally:
            _CREATING.reset(token)
        sid = created.session_id
        if request.cwd:
            self._cwds[sid] = request.cwd
        self._editors[sid] = request.editor
        self._follow_turns(service, sid)
        return SessionSnapshot(
            session_id=sid,
            cwd=request.cwd or None,
            model=params.model or service.route.model,
            provider=service.route.provider,
            replay_epoch=service.replay_epoch,
            created=created.created,
        )

    async def resume(
        self, session_id: str, *, editor: EditorPolicy | None = None
    ) -> SessionSnapshot:
        service = await self.service()
        target = session_id
        if session_id not in service.attached():
            _root, tip = await run_blocking(
                store_view.resolve, self.scope, session_id, label="acp lineage"
            )
            target = service.current_id(tip)
        if target not in service.attached():
            row = await run_blocking(
                store_view.session_row, self.scope, target, label="acp session row"
            )
            cwd = row.get("cwd") if row else None
            if isinstance(cwd, str) and cwd:
                self._cwds.setdefault(target, cwd)
            if editor is not None and editor.mcp_servers:
                self._editors[target] = editor  # the agent built now gets the editor's servers
        try:
            resumed = await service.resume(target)
            rows = await service.store.load(resumed.session_id)
        except BaseException as exc:
            raise _backend_error(exc) from None
        sid = resumed.session_id
        if editor is not None and editor.mcp_servers:
            self._editors[sid] = editor
        else:
            self._editors.setdefault(sid, EditorPolicy())
        self._follow_turns(service, sid)
        row = await run_blocking(store_view.session_row, self.scope, sid, label="acp session row")
        cwd = self._cwds.get(sid) or (row.get("cwd") if row else None)
        title = row.get("title") if row else None
        model = row.get("model") if row else None
        return SessionSnapshot(
            session_id=sid,
            cwd=cwd if isinstance(cwd, str) and cwd else None,
            title=title if isinstance(title, str) else None,
            model=model if isinstance(model, str) and model else service.route.model,
            provider=service.route.provider,
            history=tuple(rows),
            pending=tuple(PendingPrompt(r) for r in resumed.pending_requests),
            replay_epoch=service.replay_epoch,
            last_seq=resumed.last_seq,
        )

    def editor_policy(self, session_id: str) -> EditorPolicy | None:
        """The editor policy this host holds for a session (memory only)."""
        service = self._service
        key = service.current_id(session_id) if service is not None else session_id
        return self._editors.get(key)

    async def fork(self, session_id: str, *, editor: EditorPolicy | None = None) -> str:
        service = await self.service()
        handle = self._handle(service)
        parent = service.current_id(session_id)
        if parent not in service.attached():
            _root, parent = await run_blocking(
                store_view.resolve, self.scope, session_id, label="acp lineage"
            )
        if parent in service.attached() and not service.is_idle(parent):
            raise BackendError("session_busy", "the session is running a turn; fork it when idle")
        try:
            child = await run_blocking(lambda: rewind.branch(handle, parent), label="acp fork")
        except rewind.BranchRefused as exc:
            raise BackendError("not_found", str(exc)) from None
        except BaseException as exc:
            raise _backend_error(exc) from None
        chosen = editor if editor is not None and editor.mcp_servers else None
        self._editors[child] = chosen or self._editors.get(parent, EditorPolicy())
        if parent in self._cwds:
            self._cwds[child] = self._cwds[parent]
        return child

    async def mutate(
        self, session_id: str, operation: str, payload: Mapping[str, Any]
    ) -> MutationResult:
        service = await self.service()
        sid = service.current_id(session_id)
        if operation == "workspace":
            return await self._move(service, sid, str(payload.get("cwd") or ""))
        if operation == "editor_mcp":
            specs = payload.get("mcp_servers")
            servers = tuple(dict(s) for s in specs) if isinstance(specs, list) else ()
            self._editors[sid] = EditorPolicy(mcp_servers=servers)  # pyright: ignore[reportUnknownArgumentType]
            await self._rebind_editor(service, sid)
            return MutationResult("applied")
        if operation == "branch":
            servers = payload.get("mcp_servers")
            editor = (
                EditorPolicy(mcp_servers=tuple(dict(s) for s in servers))  # pyright: ignore[reportUnknownArgumentType]
                if isinstance(servers, list) and servers
                else None
            )
            return MutationResult("applied", {"session_id": await self.fork(sid, editor=editor)})
        if operation == "rename":
            title = str(payload.get("title") or "").strip()
            handle = self._handle(service)
            clean = titles.sanitize(title)
            try:
                written = await run_blocking(
                    lambda: titles.set_title(handle, sid, clean), label="acp title"
                )
                if not written and sid in service.attached():
                    # No row before the first reply: write it now, titled (P19-12).
                    options = service.agent(sid).options
                    await service.store.ensure_session(
                        sid, source=SOURCE, system_prompt="", fields=options.session_fields
                    )
                    written = await run_blocking(
                        lambda: titles.set_title(handle, sid, clean), label="acp title"
                    )
            except titles.TitleTaken as exc:
                return MutationResult("refused", {"reason": str(exc)})
            return MutationResult("applied" if written else "deferred", {"title": clean})
        if operation == "compress":
            focus = payload.get("focus")
            try:
                outcome = await service.compress(sid, focus if isinstance(focus, str) else None)
            except BaseException as exc:
                raise _backend_error(exc) from None
            return MutationResult(
                "applied",
                {"status": outcome.status, "session_id": service.current_id(sid)},
            )
        if operation == "model":
            return await self._model(service, sid, payload)
        if operation == "rewind":
            return await self._rewind(service, sid, payload)
        if operation == "reset":
            return await self._reset(service, sid)
        raise BackendError(
            "mutation_unavailable", f"the {operation} change is not available in --local mode"
        )

    # -- model, rewind, reset (P19-12) ------------------------------------------------------------

    def _resolve(self, model: str, provider: str | None) -> RuntimeRoute:
        from indus.providers.route import resolve

        return resolve(self.scope, model=model, provider=provider)

    def _attached_agent(self, service: SessionService, sid: str) -> Agent:
        try:
            return service.agent(sid)
        except ProtocolError as exc:
            raise _backend_error(exc) from None

    async def _model(
        self, service: SessionService, sid: str, payload: Mapping[str, Any]
    ) -> MutationResult:
        model = str(payload.get("model") or "").strip()
        provider = str(payload.get("provider") or "").strip() or None
        if not model:
            return MutationResult("refused", {"reason": "no model named"})
        if sid not in service.attached():
            await self._ensure_attached(service, sid)
        if not service.is_idle(sid):
            self._picks[sid] = {"model": model, "provider": provider}
            return MutationResult("deferred", {"model": model, "provider": provider})
        return await self._switch(service, sid, model, provider, bool(payload.get("confirm")))

    async def _ensure_attached(self, service: SessionService, sid: str) -> None:
        try:
            await service.resume(sid)
        except BaseException as exc:
            raise _backend_error(exc) from None

    async def _switch(
        self, service: SessionService, sid: str, model: str, provider: str | None, confirmed: bool
    ) -> MutationResult:
        agent = self._attached_agent(service, sid)
        try:
            route = await run_blocking(
                lambda: self.resolve_route(model, provider), label="acp model route"
            )
        except Exception as exc:
            reason = exc.message if isinstance(exc, IndusError) else str(exc)
            return MutationResult("refused", {"reason": reason or type(exc).__name__})
        binding = agent.binding
        try:
            after = await binding.apply(route)
        except Exception as exc:
            reason = exc.message if isinstance(exc, IndusError) else str(exc)
            return MutationResult("refused", {"reason": reason or type(exc).__name__})
        used = max(int(agent.usage.last_prompt_tokens or 0), 0)
        warning = None if confirmed else self.model_guard(used, after.window.tokens)
        if warning is not None:
            await binding.rollback()
            return MutationResult(
                "confirm", {"message": warning, "model": after.model, "provider": after.provider}
            )
        await self._record_route(service, sid, after.route)
        return MutationResult("applied", {"model": after.model, "provider": after.provider})

    async def _record_route(self, service: SessionService, sid: str, route: RuntimeRoute) -> None:
        """The session row names the switched route, so a later resume runs it."""
        try:
            handle = self._handle(service)
            await run_blocking(
                lambda: usage_rows.set_route(
                    handle,
                    sid,
                    billing_provider=route.provider,
                    billing_base_url=route.base_url,
                    model=route.model,
                ),
                label="acp session route",
            )
        except Exception:
            log.warning("the new model of %s was not written to its row", sid, exc_info=True)

    async def _apply_pick(self, sid: str) -> None:
        """The turn ended: apply a model picked while it ran (refusals become a notice)."""
        service = self._service
        pick = self._picks.pop(sid, None)
        if service is None or pick is None:
            return
        model, provider = str(pick["model"]), pick.get("provider")
        try:
            done = await self._switch(service, sid, model, provider, confirmed=False)
        except BackendError as exc:
            done = MutationResult("refused", {"reason": exc.message})
        if done.status == "applied":
            return
        reason = done.data.get("reason") or done.data.get("message") or done.status
        service.notify(
            sid,
            Notice,
            level=NoticeLevel.WARNING,
            text=f"The model switch to {model} was not applied ({reason}); the session keeps "
            "its current model.",
            key="model_switch_refused",
        )

    async def _rewind(
        self, service: SessionService, sid: str, payload: Mapping[str, Any]
    ) -> MutationResult:
        count = payload.get("count", 1)
        turns = count if isinstance(count, int) and not isinstance(count, bool) else 1
        attached = sid in service.attached()
        if attached and not service.is_idle(sid):
            raise BackendError("session_busy", "a reply is running; take turns back when idle")
        handle = self._handle(service)
        have = await run_blocking(
            rewind.recent_user_messages, handle, sid, max(turns, 1), label="acp rewind check"
        )
        if not have:
            return MutationResult("applied", {"removed": 0, "text": ""})
        try:
            taken = await run_blocking(
                rewind.rewind, handle, sid, -min(max(turns, 1), len(have)), None, label="acp rewind"
            )
        except rewind.RewindUnavailable:
            return MutationResult("applied", {"removed": 0, "text": ""})
        if attached:
            await self._reload(service, sid)
        return MutationResult("applied", {"removed": taken.removed, "text": taken.target_text})

    async def _reset(self, service: SessionService, sid: str) -> MutationResult:
        attached = sid in service.attached()
        if attached and not service.is_idle(sid):
            raise BackendError("session_busy", "a reply is running; start over when idle")
        handle = self._handle(service)
        removed = await run_blocking(lambda: rewind.clear(handle, sid), label="acp reset")
        if attached:
            await self._reload(service, sid)
        return MutationResult("applied", {"removed": removed})

    async def _reload(self, service: SessionService, sid: str) -> None:
        try:
            await service.reload(sid)
        except BaseException as exc:
            raise _backend_error(exc) from None

    async def _move(self, service: SessionService, sid: str, cwd: str) -> MutationResult:
        if not cwd:
            return MutationResult("refused", {"reason": "no folder given"})
        attached = sid in service.attached()
        if attached and not service.is_idle(sid):
            raise BackendError("session_busy", "the session is running a turn; move it when idle")
        handle = self._handle(service)
        await run_blocking(lambda: sessions.set_cwd(handle, sid, cwd), label="acp workspace")
        self._cwds[sid] = cwd
        if attached:
            try:
                await service.reload(sid)
            except BaseException as exc:
                raise _backend_error(exc) from None
        return MutationResult("applied", {"cwd": cwd})

    async def list_sessions(self, *, limit: int = 1000) -> list[ListedSession]:
        return await run_blocking(
            lambda: store_view.listed(self.scope, limit=limit), label="acp session list"
        )

    # -- turns ------------------------------------------------------------------------------------

    def _follow_turns(self, service: SessionService, sid: str) -> None:
        if sid in self._watch:
            return
        self._watch[sid] = service.subscribe(sid, self._on_event)

    def _on_event(self, event: EventBase) -> None:
        if isinstance(event, TurnStarted):
            return
        if isinstance(event, TurnDone):
            self._reasons[event.turn_id] = event.reason
            while len(self._reasons) > _KEPT_ADMISSIONS:
                self._reasons.popitem(last=False)
            admission_id = self._by_turn.get(event.turn_id)
            if admission_id is None:
                return
            held = self._turns.get(admission_id)
            if held is not None:
                held.admission = replace(
                    held.admission, state="terminal", outcome=_outcome(event.reason)
                )

    async def submit(self, session_id: str, text: str, *, input_id: str) -> Admission:
        service = await self.service()
        sid = service.current_id(session_id)
        known = self._inputs.get((sid, input_id))
        if known is not None and known in self._turns:
            return self._turns[known].admission
        try:
            started = await service.submit(sid, text)
        except BaseException as exc:
            raise _backend_error(exc) from None
        admission = Admission(started.turn_id, started.require_generation(), "started")
        self._turns[started.turn_id] = _Turn(admission, started.turn_id)
        self._by_turn[started.turn_id] = started.turn_id
        self._inputs[(sid, input_id)] = started.turn_id
        self._follow_turns(service, service.current_id(sid))
        while len(self._turns) > _KEPT_ADMISSIONS:
            old, _ = self._turns.popitem(last=False)
            self._by_turn.pop(old, None)
        while len(self._inputs) > _KEPT_ADMISSIONS:
            self._inputs.popitem(last=False)
        return admission

    async def cancel(self, session_id: str, admission_id: str) -> CancelOutcome:
        service = await self.service()
        held = self._turns.get(admission_id)
        if held is None:
            return "not_found"
        generation = held.admission.generation
        if held.admission.state == "terminal" or generation is None:
            return "settled"
        result = service.interrupt(session_id, generation)
        return "interrupted" if result.interrupted else "settled"

    async def receipt(self, session_id: str, admission_id: str) -> Admission:
        held = self._turns.get(admission_id)
        if held is None:
            raise BackendError("not_found", "no such admission in this process")
        return held.admission

    def _turn_ended(self, ended: TurnEnd) -> None:
        """Every turn of the service: ``message.complete`` + ``session.usage`` on its stream
        (module notes), then a model picked while it ran."""
        service = self._service
        if service is None:
            return
        sid = ended.session_id
        reason = self._reasons.get(ended.turn_id) or (
            ExitReason.COMPLETED if ended.completed else ExitReason.INTERRUPTED_BY_SYSTEM
        )
        service.notify(
            sid, MessageComplete, turn_id=ended.turn_id, text=ended.final_text, reason=reason
        )
        try:
            numbers = usage_numbers(service, sid)
        except Exception:
            log.debug("no usage numbers for %s", sid, exc_info=True)
        else:
            service.notify(sid, SessionUsage, usage=numbers)
        if sid in self._picks:
            task = asyncio.ensure_future(self._apply_pick(sid))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def wait(self, session_id: str, admission_id: str) -> Settled:
        service = await self.service()
        try:
            result = await service.wait(session_id, admission_id)
        except BaseException as exc:
            raise _backend_error(exc) from None
        return Settled(admission_id, _outcome(result.exit_reason), result.failure_reason)

    async def lost(self, session_id: str) -> list[LostTurn]:
        return []  # this process's own turns are never lost (module notes)

    async def steer(self, session_id: str, text: str, *, admission_id: str | None) -> bool:
        service = await self.service()
        held = self._turns.get(admission_id) if admission_id else None
        if held is None or held.admission.state == "terminal":
            return False
        sid = service.current_id(session_id)
        try:
            if service.is_idle(sid) or service.generation(sid) != held.admission.generation:
                return False
            return service.steer(sid, text).queued
        except ProtocolError:
            return False

    async def run_command(self, session_id: str, name: str, arg: str) -> CommandReply:
        service = await self.service()
        sid = service.current_id(session_id)
        if sid not in service.attached():
            await self._ensure_attached(service, sid)
        agent = self._attached_agent(service, sid)
        if name == "tools":
            names = sorted(agent.tool_names)
            if not names:
                return CommandReply("No tools are enabled in this session.")
            return CommandReply(f"{len(names)} tools: " + ", ".join(names))
        if name == "status":
            state = agent.binding.state
            folder = self._cwds.get(sid) or agent.options.session_fields.get("cwd") or "-"
            running = "A reply is running." if agent.current is not None else "Idle."
            return CommandReply(
                f"Session: {sid}\nModel: {state.model} ({state.provider})\nFolder: {folder}\n"
                + running
            )
        if name == "usage":
            n = usage_numbers(service, sid)
            return CommandReply(
                f"Model calls: {n['api_calls']}\nTokens: {n['input_tokens']} in, "
                f"{n['output_tokens']} out, {n['cache_read_tokens']} from cache\n"
                f"Cost: {n['cost_label'] or 'unknown'}"
            )
        if name == "version":
            from indus import __version__

            return CommandReply(f"Indus Agent {__version__}")
        raise BackendError("unsupported_command", f"/{name} is not available in an editor session")

    async def answer(
        self,
        session_id: str,
        request_id: str,
        generation: int | None,
        answer: Mapping[str, Any],
    ) -> bool:
        service = await self.service()
        try:
            result = service.answer_server_request(session_id, request_id, answer)
        except ProtocolError:
            return False
        return result.accepted

    async def usage(self, session_id: str) -> UsageInfo | None:
        service = await self.service()
        try:
            agent = service.agent(session_id)
            size = agent.binding.state.window.tokens
            used = agent.usage.last_prompt_tokens
        except Exception:
            return None
        if not isinstance(size, int) or size <= 0:
            return None
        return UsageInfo(size=size, used=max(int(used or 0), 0))

    async def show_reasoning(self, session_id: str) -> None:
        return None  # in process the agent's sink carries reasoning deltas already

    def subscribe(self, session_id: str, sink: EventSink) -> Callable[[], None]:
        service = self._service
        if service is None:
            raise BackendError("not_found", "no session is attached in this process")
        key = session_id
        try:
            off_events = service.subscribe(session_id, lambda e: sink.on_event(key, e))
            off_requests = service.on_request(session_id, lambda r: _request(sink, key, r))
        except ProtocolError as exc:
            raise _backend_error(exc) from None

        def off() -> None:
            off_events()
            off_requests()

        return off

    def current_id(self, session_id: str) -> str:
        service = self._service
        return service.current_id(session_id) if service is not None else session_id

    async def close_session(self, session_id: str) -> None:
        service = self._service
        if service is None:
            return
        sid = service.current_id(session_id)
        undo = self._watch.pop(sid, None)
        if undo is not None:
            undo()
        await service.close(sid)

    async def aclose(self) -> None:
        service, self._service = self._service, None
        for undo in self._watch.values():
            undo()
        self._watch.clear()
        editor_mcp, self._editor_mcp = self._editor_mcp, None
        if editor_mcp is not None:
            await editor_mcp.aclose()
        if service is not None:
            await service.aclose()


def _request(sink: EventSink, key: str, request: object) -> None:
    if isinstance(request, ServerRequestBase):
        sink.on_request(key, cast(ServerRequest, request), None)


# -- the production service -----------------------------------------------------------------------


def build_service(scope: Scope, factory: AgentFactory) -> SessionService:
    """The profile's in-process service for ACP sessions (see the module notes). Blocking."""
    from indus.core.turn_state import LoopOptions
    from indus.providers.auxiliary import AuxClient
    from indus.providers.route import resolve
    from indus.service import terminal_host
    from indus.service.code_host import KernelUpkeep
    from indus.service.host_inprocess import ALL_KINDS
    from indus.service.lsp_host import LspUpkeep
    from indus.service.mcp_host import McpUpkeep
    from indus.service.plugin_host import PluginUpkeep
    from indus.state import registry
    from indus.tools.builtin import load_builtins, terminal
    from indus.tools.registry import ToolRegistry

    route = resolve(scope)
    handle = registry.acquire(scope)
    store = StateStore(handle)
    cwd = Path.cwd()
    terminal_host.install(handle, terminal.runtime().processes)
    tool_registry = ToolRegistry(clock=scope.clock)
    load_builtins(tool_registry)
    aux = AuxClient(scope, route)
    options = AgentOptions(
        platform=PLATFORM,
        source=SOURCE,
        cwd=cwd,
        loop=LoopOptions.from_scope(scope, platform=PLATFORM),
        session_fields={"cwd": str(cwd), "model": route.model},
        store=store,
        registry=tool_registry,
    )
    return SessionService(
        scope,
        route,
        toolsets=_toolsets(scope),
        options=options,
        store=store,
        owner=SOURCE,
        answerable_kinds=ALL_KINDS,
        agent_factory=factory,
        aux=aux,
        kernels=KernelUpkeep(scope, reaper=True),
        mcp=McpUpkeep(scope, tool_registry, aux=aux, watch=True),
        plugins=PluginUpkeep(scope, tool_registry, surface="acp", cwd=str(cwd)),
        lsp=LspUpkeep(scope),
    )


def _no_mcp(scope: Scope) -> bool:
    from indus.foundation.config import load

    try:
        return toolset.no_mcp(scope, load(scope))
    except Exception:
        return False


def _toolsets(scope: Scope) -> list[str]:
    """``platform_toolsets.acp`` when set, else the ACP bundle (``toolset.selection``)."""
    from indus.foundation.config import load

    try:
        view = load(scope)
    except Exception:
        return [DEFAULT_TOOLSET]
    return toolset.selection(scope, view)
