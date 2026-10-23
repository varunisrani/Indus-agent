"""``config.set``: one client setting changed, checked the same way on every transport (W50-a).

The in-process bridge (``--local``) and the daemon's WebSocket (an attached TUI) both answer
``config.set``; this module holds what they share, so a value refused in one is refused in the
other with the same message (``INVALID_PARAMS``, path ``value``):

- the profile keys (``busy``, ``theme``, ``indicator``, ``details``, ``statusbar``, ``mouse``):
  checked and saved to the profile's settings file (``save_setting``); ``verbose`` without a
  session is saved as ``tui.tool_progress`` (``save_tool_mode``);
- the session switches' values (``tool_mode``, ``yolo_value``, ``fast_value``,
  ``reasoning_value``, ``model_value``), the confirmation a model in ``tui.confirm_models`` needs
  (``check_confirm``) and the two switches that are plain flags of a session
  (``switch_auto_approve``, ``switch_fast``).

What each transport keeps for itself: which session it may name (the daemon checks that the
connection attached it), how a model switch is applied (the bridge binds it, deferring while a
turn runs; the daemon goes through ``session.mutate``), and where a session's ``verbose`` lives
(one viewer's mode in both).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Final, cast

from pydantic import JsonValue

from indus.foundation.blocking import run_blocking
from indus.foundation.config import writer
from indus.foundation.scope import Scope
from indus.protocol.bridge import ConfigApplied, ConfigKey, ConfigSetResult
from indus.protocol.errors import CONFIRM_REQUIRED, INVALID_PARAMS
from indus.protocol.methods import ProtocolError
from indus.providers.types import normalize_effort
from indus.service.client_settings import TOOL_MODES, ToolMode, tui_setting

if TYPE_CHECKING:
    from indus.core.fast_mode import Mode

__all__ = [
    "BUSY_MODES",
    "SAVED_KEYS",
    "bad_value",
    "check_confirm",
    "fast_value",
    "flag",
    "model_value",
    "needs_session",
    "reasoning_value",
    "save_setting",
    "save_tool_mode",
    "switch_auto_approve",
    "switch_fast",
    "tool_mode",
    "yolo_value",
]

BUSY_MODES: Final[tuple[str, ...]] = ("interrupt", "queue", "steer")
SAVED_KEYS: Final[dict[ConfigKey, str]] = {
    ConfigKey.BUSY: "display.busy_input_mode",
    ConfigKey.THEME: "tui.theme",
    ConfigKey.INDICATOR: "tui.indicator",
    ConfigKey.DETAILS: "tui.details",
    ConfigKey.STATUSBAR: "tui.statusbar",
    ConfigKey.MOUSE: "tui.mouse",
}
"""Profile keys → the dotted setting they are saved as."""


def bad_value(message: str) -> ProtocolError:
    return ProtocolError(INVALID_PARAMS, message, path="value")


def needs_session(key: ConfigKey) -> ProtocolError:
    """The refusal for a session switch named without a session."""
    return ProtocolError(INVALID_PARAMS, f"{key.value} needs a session", path="session_id")


def _choice(value: JsonValue, allowed: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value.strip().lower() not in allowed:
        raise bad_value(f"value must be one of {', '.join(allowed)}")
    return value.strip().lower()


def flag(value: JsonValue) -> bool:
    if not isinstance(value, bool):
        raise bad_value("value must be true or false")
    return value


def tool_mode(value: JsonValue) -> ToolMode:
    """A tool-progress mode, or a boolean (``true`` → ``verbose``, ``false`` → ``all``)."""
    if isinstance(value, bool):
        return "verbose" if value else "all"
    return cast(ToolMode, _choice(value, TOOL_MODES))


def yolo_value(value: JsonValue) -> bool:
    return flag(value)


def fast_value(value: JsonValue) -> Mode:
    """A fast mode (``fast_mode.parse_mode``)."""
    from indus.core import fast_mode

    mode = fast_mode.parse_mode(value)
    if mode is None:
        raise bad_value(f"value must be one of {', '.join(fast_mode.MODES)}")
    return mode


def reasoning_value(value: JsonValue) -> str | None:
    """The requested reasoning effort (normalised)."""
    if not isinstance(value, str) or not value.strip():
        raise bad_value("value must be a reasoning level")
    return normalize_effort(value.strip().lower())


def model_value(value: JsonValue) -> str:
    """``provider:model`` or a bare model, trimmed."""
    if not isinstance(value, str) or not value.strip():
        raise bad_value("value must be a model name")
    return value.strip()


def check_confirm(scope: Scope, wanted: str, confirm: bool) -> None:
    """``CONFIRM_REQUIRED`` for a model listed in ``tui.confirm_models`` until ``confirm``
    (blocking: it reads the settings file)."""
    if confirm:
        return
    listed: Any = tui_setting(scope, "confirm_models") or []
    marked = {str(m).strip() for m in cast(list[Any], listed)}
    if wanted in marked or wanted.partition(":")[2] in marked:
        raise ProtocolError(
            CONFIRM_REQUIRED, f"{wanted} is marked as expensive; confirm the switch", path="confirm"
        )


def switch_auto_approve(
    scope: Scope, session_id: str, enabled: bool, persist: Callable[[bool], object] | None
) -> None:
    """The session's auto-approve (``/yolo``); ``persist`` writes the session row first."""
    from indus.safety.approvals.decision import APPROVALS, session_key

    APPROVALS.set_auto_approve(session_key(scope, session_id), enabled, persist=persist)


def switch_fast(scope: Scope, session_id: str, mode: Mode) -> None:
    """The session's fast mode (``/fast``)."""
    from indus.core import fast_mode

    fast_mode.FAST_MODES.set(fast_mode.session_key(scope, session_id), mode)


def _nested(dotted: str, value: Any) -> dict[str, Any]:
    head, _, rest = dotted.partition(".")
    return {head: _nested(rest, value) if rest else value}


async def _save(scope: Scope, dotted: str, value: Any) -> None:
    await run_blocking(writer.save, scope, _nested(dotted, value), label="settings write")


async def save_tool_mode(scope: Scope, value: JsonValue) -> ConfigSetResult:
    """``verbose`` without a session: the profile's default tool-progress mode."""
    mode = tool_mode(value)
    await _save(scope, "tui.tool_progress", mode)
    return ConfigSetResult(key=ConfigKey.VERBOSE, value=mode, applied=ConfigApplied.NOW, saved=True)


async def save_setting(scope: Scope, key: ConfigKey, value: JsonValue) -> ConfigSetResult:
    """A profile key (``SAVED_KEYS``), checked and saved."""
    dotted = SAVED_KEYS[key]
    clean: Any
    if key is ConfigKey.BUSY:
        clean = _choice(value, BUSY_MODES)
    elif key is ConfigKey.INDICATOR:
        clean = _choice(value, ("spinner", "dots", "none"))
    elif key is ConfigKey.DETAILS:
        clean = _choice(value, ("collapsed", "expanded"))
    elif key in (ConfigKey.STATUSBAR, ConfigKey.MOUSE):
        clean = flag(value)
    else:  # theme
        if not isinstance(value, str) or not value.strip():
            raise bad_value("value must be a theme name")
        clean = value.strip()
    await _save(scope, dotted, clean)
    return ConfigSetResult(key=key, value=clean, applied=ConfigApplied.NOW, saved=True)
