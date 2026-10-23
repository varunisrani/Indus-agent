"""Settings and model methods of the bridge (P12-31).

``config.get`` reads ``full`` (every setting the client shows, plus the session's live switches
when a session is named), ``mtime`` (the settings file's change stamp, for cheap polling) or one
key (a client key, or a dotted ``display.*`` / ``tui.*`` path); the reading is
``indus.service.client_settings``, which the daemon's WebSocket answers with too (W48-b).

``config.set`` changes one client key:

- ``busy``, ``theme``, ``indicator``, ``details``, ``statusbar``, ``mouse`` → saved to the profile;
- ``verbose`` (tool-progress mode ``off|new|all|verbose``, or a boolean) → the named session only,
  else saved as the default;
- ``yolo``, ``reasoning``, ``fast``, ``model`` → the named session (required). A model change while
  a turn runs waits for the turn to end (``deferred``); a model listed in ``tui.confirm_models``
  needs ``confirm: true`` (``CONFIRM_REQUIRED``).

``model.options`` lists the bundled providers with whether a key is stored, and for the current
provider when it is a custom or local endpoint the model ids its ``/models`` lists (cached per base
URL like setup's list; a failure leaves the list empty); ``model.save_key`` /
``model.disconnect`` write or remove a provider's key in the profile's secret store (the value is
never logged). ``reload.mcp`` reloads the profile's MCP servers through ``indus.mcp.reload.reload``
(P13-08: the request is the confirmation; the running sessions keep their tool arrays until their
next compaction); without running servers it says so and reports ``reloaded: false``.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from indus.foundation.blocking import run_blocking
from indus.protocol.bridge import (
    ConfigApplied,
    ConfigGetParams,
    ConfigGetResult,
    ConfigKey,
    ConfigSetParams,
    ConfigSetResult,
    GatewayPingParams,
    GatewayPingResult,
    ModelDisconnectParams,
    ModelDisconnectResult,
    ModelOptionsParams,
    ModelOptionsResult,
    ModelSaveKeyParams,
    ModelSaveKeyResult,
    ReloadMcpParams,
    ReloadMcpResult,
)
from indus.protocol.errors import INVALID_PARAMS, PROVIDER_UNAVAILABLE
from indus.protocol.methods import PROTOCOL_VERSION, ProtocolError
from indus.providers import bundled
from indus.providers.profile import ProviderProfile
from indus.providers.route import resolve
from indus.service import client_settings, config_writes, picker_reads
from indus.service.client_settings import SESSION_KEYS, read_setting, tui_setting
from indus.service.session_service import SessionService
from indus.tui_bridge.bridge import SessionView

if TYPE_CHECKING:
    from indus.tui_bridge.bridge import Bridge
    from indus.tui_bridge.dispatch import MethodTable

__all__ = ["apply_deferred", "read_setting", "register", "session_settings", "tui_setting"]

log = logging.getLogger(__name__)

MCP_RELOAD = "indus.mcp.reload:reload"


# -- reading ---------------------------------------------------------------------------------------


def session_settings(bridge: Bridge, service: SessionService, session_id: str) -> dict[str, Any]:
    """The session's live switches, with the tool-progress mode of this bridge's view of it."""
    view = bridge.view(session_id)
    verbose = view.tool_mode if view is not None else bridge.default_tool_mode()
    return client_settings.session_switches(bridge.scope, service, session_id, verbose)


async def get(bridge: Bridge, params: ConfigGetParams) -> ConfigGetResult:
    live: dict[str, Any] | None = None
    if params.session_id is not None and client_settings.needs_session(params):
        live = session_settings(bridge, await bridge.service(), params.session_id)
    return client_settings.read_config(bridge.scope, params, live)


# -- writing ---------------------------------------------------------------------------------------
# The checks and the profile writes are ``indus.service.config_writes`` (shared with the daemon's
# WebSocket, W50-a); this bridge keeps how a session's model is bound and its viewer's mode.


async def _set_model(bridge: Bridge, service: SessionService, session_id: str, wanted: str) -> None:
    provider, _, model = wanted.partition(":") if ":" in wanted else ("", "", wanted)
    try:
        route = resolve(bridge.scope, model=model.strip(), provider=provider.strip() or None)
    except Exception as exc:
        raise ProtocolError(PROVIDER_UNAVAILABLE, f"cannot use {wanted}: {exc}") from exc
    binding = service.agent(session_id).binding
    try:
        await binding.apply(route)
    except Exception as exc:
        raise ProtocolError(PROVIDER_UNAVAILABLE, f"cannot switch to {wanted}: {exc}") from exc


async def apply_deferred(bridge: Bridge, service: SessionService, view: SessionView) -> None:
    """After a turn: apply a model change that waited for it."""
    wanted, view.pending_model = view.pending_model, None
    if wanted is None:
        return
    try:
        await _set_model(bridge, service, view.session_id, wanted)
    except ProtocolError as exc:
        log.warning("deferred model switch of %s failed: %s", view.session_id, exc.message)
        return
    bridge.projector.info(view)


async def _set_session(
    bridge: Bridge, params: ConfigSetParams, service: SessionService, session_id: str
) -> ConfigSetResult:
    from indus.state import sessions

    key, value = params.key, params.value
    agent = service.agent(session_id)
    sid = agent.session_id
    view = bridge.view(sid)
    applied = ConfigApplied.NOW
    if key is ConfigKey.YOLO:
        enabled = config_writes.yolo_value(value)
        handle = bridge.store_handle()

        def persist(on: bool) -> object:
            if handle is None:
                return False
            return sessions.set_yolo(handle, sid, on)

        config_writes.switch_auto_approve(bridge.scope, sid, enabled, persist)
    elif key is ConfigKey.FAST:
        value = config_writes.fast_value(value)
        config_writes.switch_fast(bridge.scope, sid, value)
    elif key is ConfigKey.REASONING:
        effort = config_writes.reasoning_value(value)
        binding = agent.binding
        try:
            await binding.apply(binding.state.route, requested_effort=effort)
        except Exception as exc:
            raise config_writes.bad_value(f"reasoning not changed: {exc}") from exc
        value = binding.state.reasoning_effort or "none"
    else:  # model
        wanted = config_writes.model_value(value)
        config_writes.check_confirm(bridge.scope, wanted, params.confirm)
        if agent.current is not None:
            if view is not None:
                view.pending_model = wanted
            applied = ConfigApplied.DEFERRED
        else:
            await _set_model(bridge, service, sid, wanted)
        value = wanted
    if view is not None and applied is ConfigApplied.NOW:
        bridge.projector.info(view)
    return ConfigSetResult(key=key, value=value, applied=applied, saved=False)


async def set_value(bridge: Bridge, params: ConfigSetParams) -> ConfigSetResult:
    key, value = params.key, params.value
    if key in SESSION_KEYS:
        if params.session_id is None:
            raise config_writes.needs_session(key)
        service = await bridge.service()
        return await _set_session(bridge, params, service, params.session_id)
    if key is ConfigKey.VERBOSE:
        if params.session_id is not None:
            mode = config_writes.tool_mode(value)
            await bridge.service()
            view = bridge.watch(params.session_id)
            view.tool_mode = mode
            bridge.projector.info(view)
            return ConfigSetResult(key=key, value=mode, applied=ConfigApplied.NOW, saved=False)
        return await config_writes.save_tool_mode(bridge.scope, value)
    return await config_writes.save_setting(bridge.scope, key, value)


# -- models ----------------------------------------------------------------------------------------


def _profile(slug: str) -> ProviderProfile:
    found = bundled.by_name().get(slug.strip().lower())
    if found is None:
        raise ProtocolError(INVALID_PARAMS, f"unknown provider: {slug}", path="slug")
    return found


async def model_options(bridge: Bridge, params: ModelOptionsParams) -> ModelOptionsResult:
    if params.session_id is not None:
        choice = picker_reads.session_choice(await bridge.service(), params.session_id)
    else:
        choice = picker_reads.profile_choice(bridge.scope)
    return await picker_reads.provider_options(bridge.scope, choice)


async def save_key(bridge: Bridge, params: ModelSaveKeyParams) -> ModelSaveKeyResult:
    profile = _profile(params.slug)
    if profile.auth_type != "api_key" or not profile.env_vars:
        raise ProtocolError(
            INVALID_PARAMS, f"{profile.name} does not sign in with an API key", path="slug"
        )
    saver: Any = getattr(bridge.scope.secrets, "set", None)
    if saver is None:
        raise ProtocolError(
            INVALID_PARAMS, "this profile has no writable secret store", path="slug"
        )
    await run_blocking(saver, profile.env_vars[0], params.api_key.strip(), label="key save")
    current = ""
    service = bridge.built
    if service is not None:
        current = service.route.provider
    return ModelSaveKeyResult(provider=picker_reads.provider_option(bridge.scope, profile, current))


async def disconnect(bridge: Bridge, params: ModelDisconnectParams) -> ModelDisconnectResult:
    profile = _profile(params.slug)
    deleter: Any = getattr(bridge.scope.secrets, "delete", None)
    removed = False
    if deleter is not None:
        for name in profile.env_vars:
            removed = bool(await run_blocking(deleter, name, label="key delete")) or removed
    return ModelDisconnectResult(removed=removed)


async def reload_mcp(bridge: Bridge, params: ReloadMcpParams) -> ReloadMcpResult:
    module_name, _, attr = MCP_RELOAD.partition(":")
    try:
        reload: Any = getattr(importlib.import_module(module_name), attr)
    except (ImportError, AttributeError):
        return ReloadMcpResult(reloaded=False, message="No MCP servers are set up to reload yet.")
    outcome: Mapping[str, Any] | Any = await reload(bridge.scope)
    reloaded = bool(getattr(outcome, "reloaded", True))
    message = str(getattr(outcome, "message", "") or "MCP servers reloaded.")
    return ReloadMcpResult(reloaded=reloaded, message=message)


async def ping(bridge: Bridge, params: GatewayPingParams) -> GatewayPingResult:
    return GatewayPingResult(protocol_version=PROTOCOL_VERSION)


def register(table: MethodTable) -> None:
    table.add("gateway.ping", ping)
    table.add("config.get", get)
    table.add("config.set", set_value)
    table.add("model.options", model_options)
    table.add("model.save_key", save_key)
    table.add("model.disconnect", disconnect)
    table.add("reload.mcp", reload_mcp)
