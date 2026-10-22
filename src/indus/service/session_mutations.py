# pyright: strict
"""``session.mutate`` on the daemon: an attached client changes one of its sessions (P19-04/05/06/
12/13, the daemon half of D-16.602).

``SessionMutations`` belongs to the ``DaemonSessionHost``. Each call names a session this
connection attached, an operation, its payload, the client's ``request_id`` and, optionally, the
``generation`` of the turn the client believes runs:

- ``editor_mcp`` — the editor's own MCP servers for this session (``service.editor_mcp``): checked
  against the session's record (a changed spec is refused, nothing changes), connected, and the
  agent rebuilt with them; while a turn runs the change waits for the turn's end (``deferred``).
- ``branch`` — a stored copy of the session; the child gets the named servers or a copy of the
  parent's, recorded at once, connected when the child is attached. Idle only.
- ``workspace`` — the session's folder (row and agent). Idle only.
- ``model`` — the session's route, also written to its row (``model``, billing provider and base
  URL) so a resume runs it; ``confirm`` when the target window cannot hold the context in use
  (until ``payload.confirm``); ``deferred`` while a turn runs.
- ``rewind`` / ``reset`` — user turns taken back / the whole conversation emptied, the agent
  rebuilt from the store. Idle only.
- ``rename`` — the title (the row is written first when no reply came yet), and ``session.title``.
- ``compress`` — the manual compression. Idle only.
- ``command`` — ``tools``, ``status``, ``usage``, ``version`` answered as text.

**Fencing.** Every operation runs under the session's gate. The host's turn runner holds the same
gate while it prepares and starts a claimed turn, and calls ``before_turn`` there: deferred changes
are applied, and a model pick a rebuild lost is bound again. So no agent is rebuilt under a running
turn and no turn starts on a half-made change. "Idle only" means the session's agent runs no turn
(``SESSION_BUSY`` otherwise; a claimed turn that has not started waits for the change). A
``generation`` that is not the running turn's answers ``refused`` (``stale_generation``).

**Idempotency.** The result of a ``request_id`` is kept (the newest 512): the same id for the same
session and operation answers it again, also while the first call still runs; for anything else it
is ``IDEMPOTENCY_CONFLICT``. A call that failed with an error is not kept.

**Agents.** ``shape`` is the service's ``agent_shaper``: before a session's agent is built it gives
an ACP session the editor toolset when the client chose none (``editor_mcp.acp_selection``, plus
the umbrella ``mcp`` toolset), leaves the profile's configured MCP servers out when
``platform_toolsets.acp`` says ``no_mcp`` (the service runs its MCP step before the shaper), its
stored folder and stored model (bound before its first turn), and — when it has editor servers —
their registry and toolsets. A session a desktop window created (source ``gui``, from the call or
its stored row) gets its own registry over the shared one with the preview tools
(``tools.builtin.preview``, P24-25: ``drive_preview`` / ``annotate_preview``, answered by the window
showing its preview) and the ``desktop-preview`` toolset in an explicit selection; no other
session sees them.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Final, cast

from indus.core.agent import Agent, AgentOptions
from indus.foundation.blocking import run_blocking
from indus.foundation.errors import IndusError
from indus.protocol.attach import SessionMutateParams, SessionMutateResult
from indus.protocol.errors import (
    IDEMPOTENCY_CONFLICT,
    INVALID_PARAMS,
    SESSION_BUSY,
    SESSION_NOT_FOUND,
)
from indus.protocol.events import Notice, NoticeLevel, SessionTitle
from indus.protocol.methods import ProtocolError
from indus.providers.route import RuntimeRoute
from indus.service import editor_mcp
from indus.service.editor_mcp import EditorRecord, EditorServers, SpecError
from indus.service.session_service import SessionService, usage_numbers
from indus.state import rewind, sessions, titles
from indus.state import usage as usage_rows
from indus.state.registry import StoreHandle
from indus.tools.builtin import preview
from indus.tools.session_registry import SessionRegistry

__all__ = [
    "ACP_SOURCE",
    "COMMANDS",
    "GUI_SOURCE",
    "SessionMutations",
    "stored_pick",
    "window_guard",
    "with_preview",
]

log = logging.getLogger(__name__)

ACP_SOURCE: Final = "acp"
GUI_SOURCE: Final = preview.DESKTOP_SURFACE
COMMANDS: Final = ("tools", "status", "usage", "version")
"""The information commands ``command`` answers."""
_KEPT: Final = 512
_KEPT_SOURCES: Final = 4096
_EDITOR_TOOLSET_PREFIX: Final = "mcp-" + editor_mcp.EDITOR_PREFIX

type Result = SessionMutateResult


def window_guard(used: int, window: int) -> str | None:
    """Why to ask first when the context in use does not fit the target model's window."""
    if window > 0 and used > window:
        return (
            f"This conversation uses about {used:,} tokens, more than the {window:,} the new "
            "model takes; its next message is compressed first."
        )
    return None


def _busy(what: str) -> ProtocolError:
    return ProtocolError(SESSION_BUSY, f"a turn is running in this session; {what} when it ends")


def _refused(reason: str, message: str | None = None, **fields: Any) -> Result:
    return SessionMutateResult(status="refused", reason=reason, message=message, **fields)


def _error_text(exc: BaseException) -> str:
    if isinstance(exc, IndusError):
        return exc.message
    return type(exc).__name__


@dataclass
class _State:
    """What the mutations hold for one session (by host key)."""

    acp: bool = False
    cwd: str | None = None
    servers: EditorServers | None = None
    record: EditorRecord | None = None
    route: RuntimeRoute | None = None
    deferred: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])


def stored_pick(
    row: Mapping[str, Any] | None, route: RuntimeRoute
) -> tuple[str, str | None] | None:
    """``(model, provider)`` a stored session row names when it differs from ``route`` (a switch
    made earlier, ``model`` / ``billing_provider``); None when the row names nothing else."""
    if row is None:
        return None
    model = row.get("model")
    if not isinstance(model, str) or not model.strip():
        return None
    raw = row.get("billing_provider")
    provider = raw.strip() if isinstance(raw, str) and raw.strip() else None
    if model == route.model and provider in (None, route.provider):
        return None
    return model, provider


@dataclass
class _Kept:
    key: str
    operation: str
    result: asyncio.Future[Result]


class SessionMutations:
    """See the module notes."""

    def __init__(
        self,
        service: SessionService,
        store: StoreHandle,
        *,
        running: Callable[[str], int | None],
        key_of: Callable[[str], str],
    ) -> None:
        self.service = service
        self.store = store
        self.scope = service.scope
        self._running = running
        self._key_of = key_of
        self.opener: Any = None
        """The MCP opener editor servers connect with (None: the OSV-guarded default)."""
        self.resolve_route: Callable[[str, str | None], RuntimeRoute] = self._resolve
        """``(model, provider) -> route`` for a model change (blocking)."""
        self.model_guard: Callable[[int, int], str | None] = window_guard
        self._states: dict[str, _State] = {}
        self._gates: dict[str, asyncio.Lock] = {}
        self._kept: OrderedDict[str, _Kept] = OrderedDict()
        self._sources: dict[str, str] = {}
        self._closed = False

    # -- views ------------------------------------------------------------------------------------

    def servers(self, key: str) -> list[dict[str, Any]]:
        """The editor specs held for a session (memory only; tests and diagnostics)."""
        state = self._states.get(key)
        if state is None or state.servers is None:
            return []
        return state.servers.spec_list()

    def connected(self, key: str) -> list[str]:
        state = self._states.get(key)
        if state is None or state.servers is None:
            return []
        return state.servers.connected()

    def _gate(self, key: str) -> asyncio.Lock:
        gate = self._gates.get(key)
        if gate is None:
            gate = self._gates[key] = asyncio.Lock()
        return gate

    def _state(self, key: str) -> _State:
        state = self._states.get(key)
        if state is None:
            state = self._states[key] = _State()
        return state

    # -- the call ---------------------------------------------------------------------------------

    async def mutate(self, key: str, params: SessionMutateParams) -> Result:
        """One ``session.mutate`` for the hosted session ``key`` (see the module notes)."""
        if self._closed:
            raise ProtocolError(SESSION_NOT_FOUND, "the daemon is closing", path="session_id")
        kept = self._kept.get(params.request_id)
        if kept is not None:
            if (kept.key, kept.operation) != (key, params.operation):
                raise ProtocolError(
                    IDEMPOTENCY_CONFLICT,
                    "this request_id was used for another change",
                    path="request_id",
                )
            return await asyncio.shield(kept.result)
        future: asyncio.Future[Result] = asyncio.get_running_loop().create_future()
        self._kept[params.request_id] = _Kept(key, params.operation, future)
        while len(self._kept) > _KEPT:
            self._kept.popitem(last=False)
        try:
            async with self._gate(key):
                result = await self._apply(key, params)
        except BaseException as exc:
            if self._kept.get(params.request_id) is not None:
                del self._kept[params.request_id]
            if isinstance(exc, asyncio.CancelledError):
                future.cancel()
            else:
                future.set_exception(exc)
                future.exception()  # retrieved: a lone caller must not leave it unobserved
            raise
        future.set_result(result)
        return result

    async def _apply(self, key: str, params: SessionMutateParams) -> Result:
        sid = self.service.current_id(key)
        running = self._running(key)
        wanted = params.generation
        if wanted is not None and running is not None and running != wanted:
            return _refused("stale_generation", f"turn {running} runs now, not turn {wanted}")
        payload = cast(dict[str, Any], dict(params.payload))
        operation = params.operation
        if operation == "editor_mcp":
            return await self._editor_mcp(key, sid, payload, running)
        if operation == "model":
            return await self._model(key, sid, payload, running)
        if operation == "rename":
            return await self._rename(sid, payload)
        if operation == "command":
            return await self._command(key, sid, payload)
        if running is not None:
            raise _busy(f"run {operation}")
        if operation == "branch":
            return await self._branch(key, sid, payload)
        if operation == "workspace":
            return await self._workspace(key, sid, payload)
        if operation == "rewind":
            return await self._rewind(key, sid, payload)
        if operation == "reset":
            return await self._reset(key, sid)
        return await self._compress(sid, payload)

    # -- the turn boundary ------------------------------------------------------------------------

    def gate(self, key: str) -> asyncio.Lock:
        """The session's mutation gate; the host's turn runner holds it while it prepares and
        starts a claimed turn."""
        return self._gate(key)

    async def before_turn(self, key: str) -> None:
        """Called by the turn runner, holding the gate, right before a claimed turn starts:
        deferred changes are applied, and a model pick a rebuild lost is bound again."""
        state = self._states.get(key)
        if state is None:
            return
        sid = self.service.current_id(key)
        try:
            specs = state.deferred.pop("editor_mcp", None)
            if specs is not None:
                await self._set_servers(key, sid, list(specs["servers"]))
            pick = state.deferred.pop("model", None)
            if pick is not None:
                await self._apply_pick(key, sid, pick)
            await self._repin(key, sid)
        except Exception:
            log.warning("a change waiting for the turn of %s failed", key, exc_info=True)

    async def _apply_pick(self, key: str, sid: str, pick: Mapping[str, Any]) -> None:
        model = str(pick.get("model") or "")
        provider = pick.get("provider")
        try:
            done = await self._switch(
                key,
                sid,
                model,
                provider if isinstance(provider, str) else None,
                confirmed=bool(pick.get("confirm")),
            )
        except ProtocolError as exc:
            done = _refused("model_unavailable", exc.message)
        if done.status == "applied":
            return
        why = done.message or done.reason or done.status
        self.service.notify(
            sid,
            Notice,
            level=NoticeLevel.WARNING,
            text=f"The switch to {model} was not applied ({why}); the session keeps its model.",
            key="model_switch_refused",
        )

    async def _repin(self, key: str, sid: str) -> None:
        state = self._states.get(key)
        if state is None or state.route is None:
            return
        try:
            binding = self.service.agent(sid).binding
            if binding.state.route == state.route:
                return
            await binding.apply(state.route)
        except Exception:
            log.warning("the model chosen for %s could not be bound again", key, exc_info=True)

    # -- agents -----------------------------------------------------------------------------------

    async def shape(
        self, session_id: str, options: AgentOptions, selection: Sequence[str] | None
    ) -> tuple[AgentOptions, list[str] | None]:
        """The service's ``agent_shaper`` (see the module notes)."""
        window = options.source == GUI_SOURCE
        if window:  # created by a window now; its row may not be written yet
            if len(self._sources) >= _KEPT_SOURCES:
                self._sources.clear()
            self._sources[session_id] = GUI_SOURCE
        options, chosen = await self._shape(session_id, options, selection)
        if window or self._sources.get(session_id) == GUI_SOURCE:
            options, chosen = with_preview(options, chosen)
        return options, chosen

    async def _shape(
        self, session_id: str, options: AgentOptions, selection: Sequence[str] | None
    ) -> tuple[AgentOptions, list[str] | None]:
        chosen = list(selection) if selection is not None else None
        key = self._key_of(session_id)
        state = self._states.get(key)
        acp = options.source == ACP_SOURCE or (state is not None and state.acp)
        row: dict[str, Any] | None = None
        if not acp:
            known = self._sources.get(session_id)
            if known is None:
                row = await self._row(session_id)
                found = row.get("source") if row is not None else None
                known = found if isinstance(found, str) else ""
                if len(self._sources) >= _KEPT_SOURCES:
                    self._sources.clear()
                self._sources[session_id] = known
            acp = known == ACP_SOURCE
        if not acp and state is None:
            return options, chosen
        state = self._state(key)
        state.acp = state.acp or acp
        if acp and chosen is None:
            chosen = editor_mcp.acp_selection(self.scope)
            upkeep = self.service.mcp
            if upkeep is not None:
                chosen = upkeep.selection(chosen)
        if acp and chosen is not None and editor_mcp.acp_no_mcp(self.scope):
            chosen = editor_mcp.without_config_servers(chosen)
        cwd = state.cwd
        if cwd is None and acp and options.source != ACP_SOURCE:
            row = row if row is not None else await self._row(session_id)
            stored = row.get("cwd") if row is not None else None
            cwd = stored if isinstance(stored, str) and stored else None
        if cwd:
            fields = {**options.session_fields, "cwd": cwd}
            options = replace(options, cwd=Path(cwd), session_fields=fields)
        if acp and options.source != ACP_SOURCE and state.route is None:
            # A stored ACP session comes back on the model its row names (a switch made before a
            # restart); ``before_turn`` binds it, as it does a pick a rebuild lost.
            row = row if row is not None else await self._row(session_id)
            await self._restore_route(state, row)
        base = (
            [n for n in chosen if not n.startswith(_EDITOR_TOOLSET_PREFIX)]
            if chosen is not None
            else None
        )
        registry = options.registry
        if isinstance(registry, SessionRegistry):
            registry = registry.base
        servers = state.servers
        if servers is None or not servers.specs:
            return replace(options, registry=registry), base
        if servers.registry.base is not registry and registry is not None:
            servers.registry.base = registry
        folder = str(options.cwd) if options.cwd is not None else None
        try:
            await servers.connect(folder)
        except Exception:
            log.warning("the editor MCP servers of %s could not be connected", key, exc_info=True)
        self._freeze(state)
        options = replace(options, registry=servers.registry)
        if base is not None:
            base = list(dict.fromkeys([*base, *servers.toolsets()]))
        return options, base

    async def _restore_route(self, state: _State, row: Mapping[str, Any] | None) -> None:
        pick = stored_pick(row, self.service.route)
        if pick is None:
            return
        model, provider = pick
        try:
            state.route = await run_blocking(
                lambda: self.resolve_route(model, provider), label="model route"
            )
        except Exception as exc:
            log.info("the stored model %s is not used (%s)", model, type(exc).__name__)

    def _freeze(self, state: _State) -> list[str]:
        """Frozen schemas for first listings; the names whose listing differs."""
        servers = state.servers
        if servers is None:
            return []
        record = state.record if state.record is not None else EditorRecord()
        state.record = record
        changed: list[str] = []
        for name, tools in servers.listings.items():
            _first, differs = record.freeze(name, tools)
            if differs:
                changed.append(name)
        return changed

    async def _row(self, session_id: str) -> dict[str, Any] | None:
        try:
            return await run_blocking(sessions.get, self.store, session_id, label="session row")
        except Exception:
            log.debug("session row %s unreadable", session_id, exc_info=True)
            return None

    async def _reload(self, key: str, sid: str) -> None:
        if sid not in self.service.attached():
            return
        await self.service.reload(sid)
        await self._repin(key, sid)

    # -- editor MCP -------------------------------------------------------------------------------

    async def _record(self, key: str, sid: str) -> EditorRecord:
        state = self._state(key)
        if state.record is None:
            found = await run_blocking(
                editor_mcp.load_record, self.store, sid, label="editor record"
            )
            state.record = found if found is not None else EditorRecord()
        return state.record

    async def _save(self, key: str, sid: str) -> None:
        state = self._state(key)
        record = state.record
        if record is None:
            return
        await run_blocking(
            lambda: editor_mcp.save_record(self.store, sid, record, source=ACP_SOURCE),
            label="editor record",
        )

    def _require_acp(self, key: str) -> None:
        state = self._states.get(key)
        if state is None or not state.acp:
            raise ProtocolError(
                INVALID_PARAMS, "editor MCP servers belong to ACP sessions", path="operation"
            )

    @staticmethod
    def _specs(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        try:
            return editor_mcp.valid_specs(payload.get("mcp_servers"))
        except SpecError as exc:
            raise ProtocolError(INVALID_PARAMS, str(exc), path="payload.mcp_servers") from None

    async def _editor_mcp(
        self, key: str, sid: str, payload: Mapping[str, Any], running: int | None
    ) -> Result:
        specs = self._specs(payload)
        self._require_acp(key)
        policy = payload.get("edit_approval_policy", "ask")
        if policy not in (None, "ask"):
            return _refused(
                "edit_policy_unavailable", "editor sessions ask before every edit on this daemon"
            )
        record = await self._record(key, sid)
        changed = record.refused(specs)
        if changed:
            return _refused(
                "editor_mcp_changed",
                "an MCP server differs from the one this session was started with; send the "
                "same server settings, or start a new session",
                refused_servers=changed,
            )
        state = self._state(key)
        held = state.servers
        if held is not None and held.same(specs):
            return SessionMutateResult(status="applied", servers=held.connected(), changed=[])
        if running is not None:
            state.deferred["editor_mcp"] = {"servers": specs}
            return SessionMutateResult(status="deferred", servers=[str(s["name"]) for s in specs])
        changed_listing = await self._set_servers(key, sid, specs)
        return SessionMutateResult(
            status="applied", servers=self.connected(key), changed=changed_listing
        )

    async def _set_servers(self, key: str, sid: str, specs: list[dict[str, Any]]) -> list[str]:
        """Hold ``specs`` for the session, record them, rebuild its agent with them."""
        state = self._state(key)
        record = await self._record(key, sid)
        record.admit(specs)
        if state.servers is None:
            if specs:
                base = self._base_registry(sid)
                state.servers = EditorServers(self.scope, base, specs, opener=self.opener)
        else:
            state.servers.replace(specs)
        await self._reload(key, sid)
        changed = self._freeze(state)
        if not specs and state.servers is not None:
            servers, state.servers = state.servers, None
            await servers.aclose()
        await self._save(key, sid)
        return changed

    def _base_registry(self, sid: str) -> Any:
        try:
            registry = self.service.agent(sid).options.registry
        except ProtocolError:
            registry = self.service.options.registry
        if isinstance(registry, SessionRegistry):
            return registry.base
        return registry if registry is not None else self.service.options.registry

    # -- branch, workspace, history ---------------------------------------------------------------

    async def _branch(self, key: str, sid: str, payload: Mapping[str, Any]) -> Result:
        named = self._specs(payload)
        if named:
            self._require_acp(key)
        state = self._states.get(key)
        held = state.servers.spec_list() if state is not None and state.servers else []
        specs = named or held
        try:
            child = await run_blocking(lambda: rewind.branch(self.store, sid), label="branch")
        except rewind.BranchRefused as exc:
            raise ProtocolError(SESSION_NOT_FOUND, str(exc), path="session_id") from None
        if specs or (state is not None and state.acp):
            fresh = self._state(child)
            fresh.acp = state is not None and state.acp
            fresh.cwd = state.cwd if state is not None else None
            if specs:
                fresh.record = EditorRecord()
                fresh.record.admit(specs)
                base = self._base_registry(sid)
                fresh.servers = EditorServers(self.scope, base, specs, opener=self.opener)
                await self._save(child, child)
        return SessionMutateResult(
            status="applied", session_id=child, servers=[str(s["name"]) for s in specs]
        )

    async def _workspace(self, key: str, sid: str, payload: Mapping[str, Any]) -> Result:
        cwd = payload.get("cwd")
        if not isinstance(cwd, str) or not cwd.strip():
            return _refused("no_folder", "name the folder to move the session to")
        folder = cwd.strip()
        await run_blocking(lambda: sessions.set_cwd(self.store, sid, folder), label="workspace")
        self._state(key).cwd = folder
        await self._reload(key, sid)
        return SessionMutateResult(status="applied", cwd=folder)

    async def _rewind(self, key: str, sid: str, payload: Mapping[str, Any]) -> Result:
        raw = payload.get("count", 1)
        count = raw if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0 else 1
        have = await run_blocking(
            rewind.recent_user_messages, self.store, sid, count, label="rewind check"
        )
        if not have:
            return SessionMutateResult(status="applied", removed=0, text="")
        try:
            taken = await run_blocking(
                rewind.rewind, self.store, sid, -min(count, len(have)), None, label="rewind"
            )
        except rewind.RewindUnavailable:
            return SessionMutateResult(status="applied", removed=0, text="")
        await self._reload(key, sid)
        return SessionMutateResult(status="applied", removed=taken.removed, text=taken.target_text)

    async def _reset(self, key: str, sid: str) -> Result:
        removed = await run_blocking(lambda: rewind.clear(self.store, sid), label="reset")
        await self._reload(key, sid)
        return SessionMutateResult(status="applied", removed=removed)

    async def _compress(self, sid: str, payload: Mapping[str, Any]) -> Result:
        focus = payload.get("focus")
        outcome = await self.service.compress(sid, focus if isinstance(focus, str) else None)
        return SessionMutateResult(
            status="applied",
            outcome=outcome.status,
            session_id=self.service.current_id(sid),
            message=outcome.reason or None,
        )

    # -- title ------------------------------------------------------------------------------------

    async def _rename(self, sid: str, payload: Mapping[str, Any]) -> Result:
        raw = payload.get("title")
        clean = titles.sanitize(raw if isinstance(raw, str) else None)
        if not clean:
            return _refused("no_title", "name the title")
        try:
            written = await run_blocking(
                lambda: titles.set_title(self.store, sid, clean), label="title"
            )
            if not written and sid in self.service.attached():
                options = self.service.agent(sid).options
                await self.service.store.ensure_session(
                    sid,
                    source=options.source or options.platform,
                    system_prompt="",
                    fields=options.session_fields,
                )
                written = await run_blocking(
                    lambda: titles.set_title(self.store, sid, clean), label="title"
                )
        except titles.TitleTaken as exc:
            return _refused("title_taken", str(exc))
        if not written:
            return _refused("no_session_row", "the session has no stored row to title")
        self.service.notify(sid, SessionTitle, title=clean)
        return SessionMutateResult(status="applied", title=clean)

    # -- model ------------------------------------------------------------------------------------

    def _resolve(self, model: str, provider: str | None) -> RuntimeRoute:
        from indus.providers.route import resolve

        return resolve(self.scope, model=model, provider=provider)

    async def _model(
        self, key: str, sid: str, payload: Mapping[str, Any], running: int | None
    ) -> Result:
        model = str(payload.get("model") or "").strip()
        raw = payload.get("provider")
        provider = (raw.strip() or None) if isinstance(raw, str) else None
        if not model:
            return _refused("no_model", "name the model")
        if running is not None:
            pick = {"model": model, "provider": provider, "confirm": bool(payload.get("confirm"))}
            self._state(key).deferred["model"] = pick
            return SessionMutateResult(status="deferred", model=model, provider=provider)
        return await self._switch(key, sid, model, provider, confirmed=bool(payload.get("confirm")))

    async def _switch(
        self, key: str, sid: str, model: str, provider: str | None, *, confirmed: bool
    ) -> Result:
        agent: Agent = self.service.agent(sid)
        try:
            route = await run_blocking(
                lambda: self.resolve_route(model, provider), label="model route"
            )
        except Exception as exc:
            return _refused("model_unavailable", f"{model} cannot be used: {_error_text(exc)}")
        binding = agent.binding
        try:
            after = await binding.apply(route)
        except Exception as exc:
            return _refused("model_unavailable", f"{model} cannot be used: {_error_text(exc)}")
        used = max(int(agent.usage.last_prompt_tokens or 0), 0)
        warning = None if confirmed else self.model_guard(used, after.window.tokens)
        if warning is not None:
            await binding.rollback()
            return SessionMutateResult(
                status="confirm", message=warning, model=after.model, provider=after.provider
            )
        self._state(key).route = after.route
        fields = {**agent.options.session_fields, "model": after.model}
        agent.options = replace(agent.options, session_fields=fields)
        await self._record_route(sid, after.route)
        return SessionMutateResult(status="applied", model=after.model, provider=after.provider)

    async def _record_route(self, sid: str, route: RuntimeRoute) -> None:
        """The session row names the switched route, so a later resume runs it (a row not
        written yet takes the model from the session fields)."""

        def write() -> bool:
            return usage_rows.set_route(
                self.store,
                sid,
                billing_provider=route.provider,
                billing_base_url=route.base_url,
                model=route.model,
            )

        try:
            await run_blocking(write, label="session route")
        except Exception:
            log.warning("the new model of %s was not written to its row", sid, exc_info=True)

    # -- commands ---------------------------------------------------------------------------------

    async def _command(self, key: str, sid: str, payload: Mapping[str, Any]) -> Result:
        name = str(payload.get("name") or "").strip().lstrip("/").lower()
        if name not in COMMANDS:
            return _refused("unsupported_command", f"/{name} is not available in this session")
        agent = self.service.agent(sid)
        if name == "tools":
            names = sorted(agent.tool_names)
            text = f"{len(names)} tools: " + ", ".join(names) if names else "No tools are on."
        elif name == "status":
            state = agent.binding.state
            folder = agent.options.session_fields.get("cwd") or agent.options.cwd or "-"
            busy = "A reply is running." if self._running(key) is not None else "Idle."
            text = (
                f"Session: {sid}\nModel: {state.model} ({state.provider})\nFolder: {folder}\n{busy}"
            )
        elif name == "usage":
            n = usage_numbers(self.service, sid)
            text = (
                f"Model calls: {n.get('api_calls', 0)}\nTokens: {n.get('input_tokens', 0)} in, "
                f"{n.get('output_tokens', 0)} out, {n.get('cache_read_tokens', 0)} from cache\n"
                f"Cost: {n.get('cost_label') or 'unknown'}"
            )
        else:
            from indus import __version__

            text = f"Indus Agent {__version__}"
        return SessionMutateResult(status="applied", text=text)

    # -- closing ----------------------------------------------------------------------------------

    async def forget(self, key: str) -> None:
        """The session ended here: close its editor servers and drop what was held for it."""
        state = self._states.pop(key, None)
        self._gates.pop(key, None)
        if state is not None and state.servers is not None:
            await state.servers.aclose()

    async def aclose(self) -> None:
        self._closed = True
        for key in list(self._states):
            await self.forget(key)
        self._kept.clear()


def with_preview(
    options: AgentOptions, selection: list[str] | None
) -> tuple[AgentOptions, list[str] | None]:
    """A desktop window's session: the preview tools in its own registry (see the module notes)."""
    registry = options.registry
    if registry is None:
        return options, selection
    own = registry if isinstance(registry, SessionRegistry) else SessionRegistry(registry)
    if own.get("drive_preview") is None:
        preview.register_tools(own, surface=GUI_SOURCE)
    if selection is not None and preview.PREVIEW_TOOLSET not in selection:
        selection = [*selection, preview.PREVIEW_TOOLSET]
    return replace(options, registry=own), selection
