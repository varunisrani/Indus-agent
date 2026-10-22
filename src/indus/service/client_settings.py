"""``config.get``: the client's settings, read the same way on every transport (W48-b).

The in-process bridge (``--local``) and the daemon's WebSocket (an attached TUI) both answer
``config.get`` with ``read_config``, so an attached TUI starts with what ``--local`` shows:

- ``mtime`` → the settings file's change stamp (cheap polling);
- ``full`` → every setting the client shows, plus the session's live switches when one is named;
- a client key (``busy``, ``theme``, ``verbose``, ``model``, …) → that value; the session keys
  (``model``, ``reasoning``, ``fast``, ``yolo``, ``verbose``) read the named session's live value;
- a dotted ``display.*`` / ``tui.*`` path → that setting, read fresh, else its built-in default.

Anything else is refused (``INVALID_PARAMS``): this is not a general settings reader. Writing
(``config.set``) stays with the bridge. Who may name which session is the transport's rule: the
daemon checks that the connection attached it before it calls in here.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal, cast

from pydantic import JsonValue

from indus.foundation.config import loader, schema
from indus.foundation.scope import Scope
from indus.protocol.bridge import ConfigGetParams, ConfigGetResult, ConfigKey
from indus.protocol.errors import INVALID_PARAMS
from indus.protocol.methods import ProtocolError

if TYPE_CHECKING:
    from indus.service.session_service import SessionService

__all__ = [
    "SESSION_KEYS",
    "TOOL_MODES",
    "ToolMode",
    "client_settings",
    "default_tool_mode",
    "needs_session",
    "read_config",
    "read_setting",
    "session_switches",
    "settings_mtime",
    "tui_setting",
]

log = logging.getLogger(__name__)

type ToolMode = Literal["off", "new", "all", "verbose"]
TOOL_MODES: tuple[ToolMode, ...] = ("off", "new", "all", "verbose")
SESSION_KEYS = frozenset({ConfigKey.YOLO, ConfigKey.REASONING, ConfigKey.FAST, ConfigKey.MODEL})
"""Keys whose value is the named session's (``verbose`` too, when a session is named)."""
_READABLE_PREFIXES = ("display.", "tui.")


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


def default_tool_mode(scope: Scope) -> ToolMode:
    """The profile's tool-progress mode (``all`` when unset or unknown)."""
    value = str(tui_setting(scope, "tool_progress") or "all").strip().lower()
    return value if value in TOOL_MODES else "all"


def settings_mtime(scope: Scope) -> float | None:
    """The settings file's change stamp (None when it does not exist)."""
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


def client_settings(scope: Scope) -> dict[str, Any]:
    """The settings the client shows (no session)."""
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


def session_switches(
    scope: Scope, service: SessionService, session_id: str, verbose: ToolMode
) -> dict[str, Any]:
    """A live session's model, provider, reasoning, fast and yolo, plus its ``verbose`` mode."""
    from indus.core import fast_mode
    from indus.safety.approvals.decision import APPROVALS, session_key

    agent = service.agent(session_id)
    state = agent.binding.state
    return {
        "model": state.model,
        "provider": state.provider,
        "reasoning": state.reasoning_effort,
        "fast": fast_mode.FAST_MODES.get(fast_mode.session_key(scope, agent.session_id)),
        "yolo": APPROVALS.auto_approve(session_key(scope, agent.session_id)),
        "verbose": verbose,
    }


def needs_session(params: ConfigGetParams) -> bool:
    """Whether answering ``params`` reads the named session's live switches."""
    if params.session_id is None:
        return False
    if params.key == "full":
        return True
    known = {k.value for k in ConfigKey}
    return params.key in known and (
        ConfigKey(params.key) in SESSION_KEYS or params.key == ConfigKey.VERBOSE.value
    )


def read_config(
    scope: Scope, params: ConfigGetParams, live: Mapping[str, Any] | None = None
) -> ConfigGetResult:
    """``config.get`` (blocking: it reads the settings file); ``live`` is the named session's
    switches (``session_switches``), read by the caller when ``needs_session`` says so."""
    key = params.key
    if key == "mtime":
        return ConfigGetResult(key=key, value=settings_mtime(scope))
    if key == "full":
        values = client_settings(scope)
        if live is not None:
            values.update(live)
        return ConfigGetResult(key=key, value=cast(JsonValue, values))
    if key in {k.value for k in ConfigKey}:
        if live is not None and needs_session(params):
            return ConfigGetResult(key=key, value=cast(JsonValue, live.get(key)))
        return ConfigGetResult(key=key, value=cast(JsonValue, client_settings(scope).get(key)))
    if key.startswith(_READABLE_PREFIXES):
        return ConfigGetResult(key=key, value=cast(JsonValue, read_setting(scope, key)))
    raise ProtocolError(INVALID_PARAMS, f"{key} is not a setting the client can read", path="key")
