"""Desktop-only preview tools (P24-25): ``drive_preview`` and ``annotate_preview``.

The desktop app's preview pane shows a web page or a local file next to a session. These tools let
the agent use it: ``drive_preview`` navigates, clicks, types, scrolls or reads a snapshot of the
page; ``annotate_preview`` puts numbered notes on elements for the person to see. Each call
becomes one server request — ``preview.act`` or ``preview.read`` — answered by the desktop window
that shows this session's preview. A window that does not show it declines with ``not_shown``;
every action runs inside the page's own sandbox there, never in the app.

The tools exist only in sessions a desktop window created: ``register_tools(registry,
surface=...)`` registers them for the ``gui`` surface and does nothing otherwise (the daemon does
it in that session's own registry, ``service.session_mutations``). The handler gets the session's
preview channel as the ``preview`` keyword (``PreviewPort``), else builds ``BrokerPreview`` over the
session's server-request broker (the ``broker`` keyword); without either it says so.

``PreviewPort.request(kind, payload, timeout)`` returns the window's answer as a mapping (``ok``,
``error`` and what the page reported), or raises ``PreviewDeclined`` (every attached window
declined: none shows the preview), ``PreviewUndelivered`` (no desktop window is attached, or the
request was withdrawn) or ``TimeoutError``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, Literal, Protocol, cast

from indus.protocol.events import CancelReason
from indus.protocol.requests_port import (
    NOT_SHOWN_INDUS_CODE,
    RequestUnsupported,
    RequestWithdrawn,
    ServerRequestPort,
)
from indus.tools.calls import ToolResult
from indus.tools.registry import ToolRegistry, ToolTraits

__all__ = [
    "ACTIONS",
    "ANNOTATE_SCHEMA",
    "BrokerPreview",
    "DESKTOP_SURFACE",
    "DRIVE_SCHEMA",
    "MAX_PINS",
    "PREVIEW_TIMEOUT_S",
    "PREVIEW_TOOLSET",
    "PreviewDeclined",
    "PreviewPort",
    "PreviewUndelivered",
    "annotate_preview",
    "drive_preview",
    "register_tools",
]

PREVIEW_TOOLSET = "desktop-preview"
DESKTOP_SURFACE = "gui"
PREVIEW_TIMEOUT_S = 30.0
MAX_PINS = 20
MAX_TEXT = 4_000
MAX_SELECTOR = 500
MAX_NOTE = 500

type Kind = Literal["preview.act", "preview.read"]

ACTIONS: tuple[str, ...] = ("navigate", "click", "type", "scroll", "snapshot")
_URL = re.compile(r"^(https?://|file:///)", re.IGNORECASE)


class PreviewDeclined(Exception):
    """The window answering does not show this session's preview (``not_shown``)."""

    def __init__(self, reason: str = "not_shown") -> None:
        super().__init__(reason)
        self.reason = reason


class PreviewUndelivered(Exception):
    """No desktop window is attached to the session any more."""


class PreviewPort(Protocol):
    async def request(
        self, kind: Kind, payload: Mapping[str, Any], *, timeout: float
    ) -> Mapping[str, Any]: ...


class BrokerPreview:
    """``PreviewPort`` over the session's server-request broker (the daemon offers the request to
    the attached desktop windows; the one showing the preview answers)."""

    def __init__(self, broker: ServerRequestPort, *, turn_id: str | None = None) -> None:
        self.broker = broker
        self.turn_id = turn_id

    async def request(
        self, kind: Kind, payload: Mapping[str, Any], *, timeout: float
    ) -> Mapping[str, Any]:
        try:
            answer = await self.broker.ask(kind, payload, timeout=timeout, turn_id=self.turn_id)
        except RequestUnsupported as exc:
            data = exc.error.data
            code = data.get("indus_code") if isinstance(data, Mapping) else None
            if code == NOT_SHOWN_INDUS_CODE:
                raise PreviewDeclined() from exc
            raise PreviewUndelivered(str(exc)) from exc
        except RequestWithdrawn as exc:
            if exc.reason is CancelReason.TIMED_OUT:
                raise TimeoutError(str(exc)) from exc
            raise PreviewUndelivered(str(exc)) from exc
        body = answer.model_dump(mode="json")
        result = body.get("result")
        found: dict[str, Any] = (
            dict(cast(Mapping[str, Any], result)) if isinstance(result, Mapping) else {}
        )
        found["ok"] = body.get("ok") is True
        if body.get("error"):
            found["error"] = body["error"]
        return found


def _port(
    preview: PreviewPort | None, broker: ServerRequestPort | None, turn_id: str | None
) -> PreviewPort | None:
    if preview is not None:
        return preview
    return BrokerPreview(broker, turn_id=turn_id) if broker is not None else None


def _text(args: Mapping[str, Any], key: str, limit: int) -> str | None:
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be text")
    if len(value) > limit:
        raise ValueError(f"{key} is longer than {limit:,} characters")
    return value


def _number(args: Mapping[str, Any], key: str) -> int:
    value = args.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be a number of pixels")
    return max(-20_000, min(20_000, int(value)))


def _drive_payload(args: Mapping[str, Any]) -> tuple[Kind, dict[str, Any]]:
    action = str(args.get("action") or "").strip().lower()
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {', '.join(ACTIONS)}")
    if action == "snapshot":
        return "preview.read", {"what": "snapshot"}
    payload: dict[str, Any] = {"action": action}
    if action == "navigate":
        url = (_text(args, "url", 2_048) or "").strip()
        if not _URL.match(url):
            raise ValueError("url must be an http(s) address or a file:/// path")
        payload["url"] = url
    elif action in ("click", "type"):
        selector = (_text(args, "selector", MAX_SELECTOR) or "").strip()
        if not selector:
            raise ValueError(f"{action} needs a CSS selector")
        payload["selector"] = selector
        if action == "type":
            payload["text"] = _text(args, "text", MAX_TEXT) or ""
            payload["submit"] = args.get("submit") is True
    else:  # scroll
        selector = (_text(args, "selector", MAX_SELECTOR) or "").strip()
        if selector:
            payload["selector"] = selector
        else:
            payload["dx"] = _number(args, "dx")
            payload["dy"] = _number(args, "dy")
    return "preview.act", payload


def _failure(exc: BaseException) -> str:
    if isinstance(exc, PreviewDeclined):
        return ToolResult.error(
            "This session's preview is not open in a desktop window; ask the person to open "
            "the preview pane, then try again."
        ).text
    if isinstance(exc, PreviewUndelivered):
        return ToolResult.error("No desktop window is attached to this session now.").text
    if isinstance(exc, TimeoutError):
        return ToolResult.error(f"The preview did not answer within {PREVIEW_TIMEOUT_S:g} s.").text
    return ToolResult.error(f"The preview could not do that: {exc}").text


async def _ask(preview: PreviewPort | None, kind: Kind, payload: dict[str, Any]) -> str:
    if preview is None:
        return ToolResult.error("There is no desktop preview in this session.").text
    try:
        answer = await preview.request(kind, payload, timeout=PREVIEW_TIMEOUT_S)
    except (PreviewDeclined, PreviewUndelivered, TimeoutError) as exc:
        return _failure(exc)
    if answer.get("ok") is False:
        return ToolResult.error(str(answer.get("error") or "the page refused the action")).text
    return json.dumps(dict(answer), ensure_ascii=False)


async def drive_preview(
    args: Mapping[str, Any],
    *,
    preview: PreviewPort | None = None,
    broker: ServerRequestPort | None = None,
    turn_id: str | None = None,
) -> str:
    """The ``drive_preview`` handler (see the module notes)."""
    try:
        kind, payload = _drive_payload(args)
    except ValueError as exc:
        return ToolResult.error(str(exc)).text
    return await _ask(_port(preview, broker, turn_id), kind, payload)


async def annotate_preview(
    args: Mapping[str, Any],
    *,
    preview: PreviewPort | None = None,
    broker: ServerRequestPort | None = None,
    turn_id: str | None = None,
) -> str:
    """The ``annotate_preview`` handler: numbered notes on page elements, shown to the person."""
    preview = _port(preview, broker, turn_id)
    raw: object = args.get("pins")
    if not isinstance(raw, list) or not raw:
        return ToolResult.error("pins must be a non-empty list of {selector, note}.").text
    items = cast(list[object], raw)
    if len(items) > MAX_PINS:
        return ToolResult.error(f"at most {MAX_PINS} pins fit in one call.").text
    pins: list[dict[str, str]] = []
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            return ToolResult.error(f"pins[{index}] must be an object.").text
        entry = cast(Mapping[str, Any], item)
        try:
            selector = (_text(entry, "selector", MAX_SELECTOR) or "").strip()
            note = (_text(entry, "note", MAX_NOTE) or "").strip()
        except ValueError as exc:
            return ToolResult.error(f"pins[{index}]: {exc}").text
        if not selector or not note:
            return ToolResult.error(f"pins[{index}] needs a selector and a note.").text
        pins.append({"selector": selector, "note": note})
    return await _ask(preview, "preview.act", {"action": "annotate", "pins": pins})


DRIVE_SCHEMA: dict[str, Any] = {
    "name": "drive_preview",
    "description": (
        "Use the page shown in this session's desktop preview pane. action: navigate (url), "
        "click (selector), type (selector, text, submit?), scroll (selector, or dx/dy pixels) "
        "or snapshot (title, address, visible text and the interactive elements). Works only "
        "while the person has the preview open; secret field values are never returned."
    ),
    "parameters": {
        "type": "object",
        "required": ["action"],
        "properties": {
            "action": {"type": "string", "enum": list(ACTIONS)},
            "url": {"type": "string", "description": "For navigate: http(s) or file:/// only."},
            "selector": {"type": "string", "description": "A CSS selector on the page."},
            "text": {"type": "string", "description": "For type: the text to enter."},
            "submit": {"type": "boolean", "description": "For type: press Enter after."},
            "dx": {"type": "integer", "description": "For scroll: pixels to the right."},
            "dy": {"type": "integer", "description": "For scroll: pixels down."},
        },
    },
}

ANNOTATE_SCHEMA: dict[str, Any] = {
    "name": "annotate_preview",
    "description": (
        "Point things out on the page in this session's desktop preview: each pin puts a "
        "numbered note on the element its CSS selector matches, for the person to read."
    ),
    "parameters": {
        "type": "object",
        "required": ["pins"],
        "properties": {
            "pins": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_PINS,
                "items": {
                    "type": "object",
                    "required": ["selector", "note"],
                    "properties": {
                        "selector": {"type": "string"},
                        "note": {"type": "string"},
                    },
                },
            }
        },
    },
}


def register_tools(registry: ToolRegistry, *, surface: str | None = None) -> bool:
    """Register both tools for a desktop session (``surface == "gui"``); True when registered."""
    if surface != DESKTOP_SURFACE:
        return False
    registry.register(
        "drive_preview", PREVIEW_TOOLSET, DRIVE_SCHEMA, drive_preview, traits=ToolTraits()
    )
    registry.register(
        "annotate_preview",
        PREVIEW_TOOLSET,
        ANNOTATE_SCHEMA,
        annotate_preview,
        traits=ToolTraits(),
    )
    return True
