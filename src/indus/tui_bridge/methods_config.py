"""Settings and model methods of the bridge (P12-31).

``config.get`` reads ``full`` (every setting the client shows, plus the session's live switches
when a session is named), ``mtime`` (the settings file's change stamp, for cheap polling) or one
key (a client key, or a dotted ``display.*`` / ``tui.*`` path).

``config.set`` changes one client key:

- ``busy``, ``theme``, ``indicator``, ``details``, ``statusbar``, ``mouse`` → saved to the profile;
- ``verbose`` (tool-progress mode ``off|new|all|verbose``, or a boolean) → the named session only,
  else saved as the default;
- ``yolo``, ``reasoning``, ``fast``, ``model`` → the named session (required). A model change while
  a turn runs waits for the turn to end (``deferred``); a model listed in ``tui.confirm_models``
  needs ``confirm: true`` (``CONFIRM_REQUIRED``).

``model.options`` lists the bundled providers with whether a key is stored; ``model.save_key`` /
``model.disconnect`` write or remove a provider's key in the profile's secret store (the value is
never logged). ``reload.mcp`` reloads the profile's MCP servers through ``indus.mcp.reload.reload``
(P13-08: the request is the confirmation; the running sessions keep their tool arrays until their
next compaction); without running servers it says so and reports ``reloaded: false``.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

from pydantic import JsonValue

from indus.foundation.blocking import run_blocking
from indus.foundation.config import loader, schema, writer
from indus.foundation.scope import Scope
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
    ProviderOption,
    ReloadMcpParams,
    ReloadMcpResult,
)
from indus.protocol.errors import CONFIRM_REQUIRED, INVALID_PARAMS, PROVIDER_UNAVAILABLE
from indus.protocol.methods import PROTOCOL_VERSION, ProtocolError
from indus.providers import bundled
from indus.providers.profile import ProviderProfile
from indus.providers.route import resolve
from indus.providers.types import normalize_effort
from indus.service.session_service import SessionService
from indus.tui_bridge.bridge import TOOL_MODES, SessionView, ToolMode

if TYPE_CHECKING:
    from indus.tui_bridge.bridge import Bridge
    from indus.tui_bridge.dispatch import MethodTable

__all__ = ["apply_deferred", "read_setting", "register", "session_settings", "tui_setting"]

log = logging.getLogger(__name__)

BUSY_MODES = ("interrupt", "queue", "steer")
MCP_RELOAD = "indus.mcp.reload:reload"
_SAVED: dict[ConfigKey, str] = {
    ConfigKey.BUSY: "display.busy_input_mode",
    ConfigKey.THEME: "tui.theme",
    ConfigKey.INDICATOR: "tui.indicator",
    ConfigKey.DETAILS: "tui.details",
    ConfigKey.STATUSBAR: "tui.statusbar",
    ConfigKey.MOUSE: "tui.mouse",
}
_SESSION_KEYS = frozenset({ConfigKey.YOLO, ConfigKey.REASONING, ConfigKey.FAST, ConfigKey.MODEL})
_READABLE_PREFIXES = ("display.", "tui.")


# -- reading ---------------------------------------------------------------------------------------


def read_setting(scope: Scope, key: str) -> Any:
    """``key`` from the profile's settings file, read fresh, else the built-in default."""
    value: Any = None
    try:
        value = loader.load(scope).get(key)
    except Exception:
        log.debug("settings unreadable", exc_info=True)
    return schema.default_for(key) if value is None else value


def tui_setting(scope: Scope, name: str) -> Any:
    """One ``tui.*`` setting."""
    return read_setting(scope, f"tui.{name}")


def _mtime(scope: Scope) -> float | None:
    try:
        return loader.config_path(scope).stat().st_mtime
    except OSError:
        return None


def _stored_setting(scope: Scope, key: str) -> Any:
    """``key`` only when the settings file sets it (``None`` otherwise, no built-in default)."""
    try:
        view = loader.load(scope)
        return view.get(key) if view.is_set(key) else None
    except Exception:
        log.debug("settings unreadable", exc_info=True)
        return None


def _client_settings(scope: Scope) -> dict[str, Any]:
    return {
        # Unset stays null: the TUI's own default (queue) differs from the classic one (R05.18).
        "busy": _stored_setting(scope, "display.busy_input_mode"),
        "show_reasoning": read_setting(scope, "display.show_reasoning"),
        "theme": tui_setting(scope, "theme"),
        "indicator": tui_setting(scope, "indicator"),
        "details": tui_setting(scope, "details"),
        "statusbar": tui_setting(scope, "statusbar"),
        "mouse": tui_setting(scope, "mouse"),
        "verbose": tui_setting(scope, "tool_progress"),
    }


def session_settings(bridge: Bridge, service: SessionService, session_id: str) -> dict[str, Any]:
    from indus.core import fast_mode
    from indus.safety.approvals.decision import APPROVALS, session_key

    agent = service.agent(session_id)
    state = agent.binding.state
    view = bridge.view(session_id)
    return {
        "model": state.model,
        "provider": state.provider,
        "reasoning": state.reasoning_effort,
        "fast": fast_mode.FAST_MODES.get(fast_mode.session_key(bridge.scope, agent.session_id)),
        "yolo": APPROVALS.auto_approve(session_key(bridge.scope, agent.session_id)),
        "verbose": view.tool_mode if view is not None else bridge.default_tool_mode(),
    }


async def get(bridge: Bridge, params: ConfigGetParams) -> ConfigGetResult:
    scope = bridge.scope
    key = params.key
    if key == "mtime":
        return ConfigGetResult(key=key, value=_mtime(scope))
    if key == "full":
        values = _client_settings(scope)
        if params.session_id is not None:
            values.update(session_settings(bridge, await bridge.service(), params.session_id))
        return ConfigGetResult(key=key, value=cast(JsonValue, values))
    if key in {k.value for k in ConfigKey}:
        live_key = ConfigKey(key) in _SESSION_KEYS or key == ConfigKey.VERBOSE.value
        if live_key and params.session_id is not None:
            live = session_settings(bridge, await bridge.service(), params.session_id)
            return ConfigGetResult(key=key, value=cast(JsonValue, live.get(key)))
        return ConfigGetResult(key=key, value=cast(JsonValue, _client_settings(scope).get(key)))
    if key.startswith(_READABLE_PREFIXES):
        return ConfigGetResult(key=key, value=cast(JsonValue, read_setting(scope, key)))
    raise ProtocolError(INVALID_PARAMS, f"{key} is not a setting the client can read", path="key")


# -- writing ---------------------------------------------------------------------------------------


def _nested(dotted: str, value: Any) -> dict[str, Any]:
    head, _, rest = dotted.partition(".")
    return {head: _nested(rest, value) if rest else value}


async def _save(scope: Scope, dotted: str, value: Any) -> None:
    await run_blocking(writer.save, scope, _nested(dotted, value), label="settings write")


def _bad(message: str) -> ProtocolError:
    return ProtocolError(INVALID_PARAMS, message, path="value")


def _choice(value: JsonValue, allowed: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value.strip().lower() not in allowed:
        raise _bad(f"value must be one of {', '.join(allowed)}")
    return value.strip().lower()


def _flag(value: JsonValue) -> bool:
    if not isinstance(value, bool):
        raise _bad("value must be true or false")
    return value


def _tool_mode(value: JsonValue) -> ToolMode:
    if isinstance(value, bool):
        return "verbose" if value else "all"
    return cast(ToolMode, _choice(value, TOOL_MODES))


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
    from indus.core import fast_mode
    from indus.safety.approvals.decision import APPROVALS, session_key
    from indus.state import sessions

    key, value = params.key, params.value
    agent = service.agent(session_id)
    sid = agent.session_id
    view = bridge.view(sid)
    applied = ConfigApplied.NOW
    if key is ConfigKey.YOLO:
        enabled = _flag(value)
        handle = bridge.store_handle()

        def persist(on: bool) -> object:
            if handle is None:
                return False
            return sessions.patch_model_config(handle, sid, {sessions.YOLO_KEY: bool(on)})

        APPROVALS.set_auto_approve(session_key(bridge.scope, sid), enabled, persist=persist)
    elif key is ConfigKey.FAST:
        mode = fast_mode.parse_mode(value)
        if mode is None:
            raise _bad(f"value must be one of {', '.join(fast_mode.MODES)}")
        fast_mode.FAST_MODES.set(fast_mode.session_key(bridge.scope, sid), mode)
        value = mode
    elif key is ConfigKey.REASONING:
        if not isinstance(value, str) or not value.strip():
            raise _bad("value must be a reasoning level")
        effort = normalize_effort(value.strip().lower())
        binding = agent.binding
        try:
            await binding.apply(binding.state.route, requested_effort=effort)
        except Exception as exc:
            raise _bad(f"reasoning not changed: {exc}") from exc
        value = binding.state.reasoning_effort or "none"
    else:  # model
        if not isinstance(value, str) or not value.strip():
            raise _bad("value must be a model name")
        wanted = value.strip()
        confirm = {str(m).strip() for m in tui_setting(bridge.scope, "confirm_models") or []}
        if (wanted in confirm or wanted.partition(":")[2] in confirm) and not params.confirm:
            raise ProtocolError(
                CONFIRM_REQUIRED,
                f"{wanted} is marked as expensive; confirm the switch",
                path="confirm",
            )
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
    if key in _SESSION_KEYS:
        if params.session_id is None:
            raise ProtocolError(INVALID_PARAMS, f"{key.value} needs a session", path="session_id")
        service = await bridge.service()
        return await _set_session(bridge, params, service, params.session_id)
    if key is ConfigKey.VERBOSE:
        mode = _tool_mode(value)
        if params.session_id is not None:
            await bridge.service()
            view = bridge.watch(params.session_id)
            view.tool_mode = mode
            bridge.projector.info(view)
            return ConfigSetResult(key=key, value=mode, applied=ConfigApplied.NOW, saved=False)
        await _save(bridge.scope, "tui.tool_progress", mode)
        return ConfigSetResult(key=key, value=mode, applied=ConfigApplied.NOW, saved=True)
    dotted = _SAVED[key]
    if key is ConfigKey.BUSY:
        clean: Any = _choice(value, BUSY_MODES)
    elif key is ConfigKey.INDICATOR:
        clean = _choice(value, ("spinner", "dots", "none"))
    elif key is ConfigKey.DETAILS:
        clean = _choice(value, ("collapsed", "expanded"))
    elif key in (ConfigKey.STATUSBAR, ConfigKey.MOUSE):
        clean = _flag(value)
    else:  # theme
        if not isinstance(value, str) or not value.strip():
            raise _bad("value must be a theme name")
        clean = value.strip()
    await _save(bridge.scope, dotted, clean)
    return ConfigSetResult(key=key, value=clean, applied=ConfigApplied.NOW, saved=True)


# -- models ----------------------------------------------------------------------------------------


def _stored(bridge: Bridge, profile: ProviderProfile) -> bool:
    secrets = bridge.scope.secrets
    for name in profile.env_vars:
        try:
            if secrets is not None and secrets.get(name):
                return True
        except Exception:
            log.debug("secret store unreadable", exc_info=True)
        if bridge.scope.env.get(name, "").strip():
            return True
    return False


def _option(bridge: Bridge, profile: ProviderProfile, current: str) -> ProviderOption:
    return ProviderOption(
        slug=profile.name,
        name=profile.display_name or profile.name,
        auth_type=profile.auth_type,
        # A provider whose key is optional (local servers, custom endpoints) works without one.
        authenticated=profile.auth_type in ("none", "api_key_optional") or _stored(bridge, profile),
        current=profile.name == current,
    )


def _profile(slug: str) -> ProviderProfile:
    found = bundled.by_name().get(slug.strip().lower())
    if found is None:
        raise ProtocolError(INVALID_PARAMS, f"unknown provider: {slug}", path="slug")
    return found


async def model_options(bridge: Bridge, params: ModelOptionsParams) -> ModelOptionsResult:
    provider, model = "", ""
    if params.session_id is not None:
        service = await bridge.service()
        state = service.agent(params.session_id).binding.state
        provider, model = state.provider, state.model
    else:
        try:
            route = resolve(bridge.scope)
            provider, model = route.provider, route.model
        except Exception:
            log.debug("no route resolves for this profile", exc_info=True)
    options = [_option(bridge, p, provider) for p in bundled.PROFILES if not p.hidden]
    return ModelOptionsResult(provider=provider, model=model, providers=options)


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
    return ModelSaveKeyResult(provider=_option(bridge, profile, current))


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
