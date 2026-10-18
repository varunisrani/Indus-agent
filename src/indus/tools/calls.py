"""The values that cross the dispatch boundary: a call, its context, and its result.

A handler returns text (normally JSON) or exactly the multimodal envelope
``{"multimodal": True, "content": [part, ...], "text_summary": str}``. Errors become a JSON
object ``{"error": ...}`` whose text is bounded (``MAX_ERROR_CHARS`` plus a marker) so a runaway
exception cannot flood the context on every retry.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, cast

from indus.foundation.scope import Scope

MAX_ERROR_CHARS = 2048
MAX_LOGGED_ERROR_CHARS = 8192
ERROR_CUT_MARKER = " [error text cut]"

ENVELOPE_KEYS = frozenset({"multimodal", "content", "text_summary"})

_EMPTY: Mapping[str, Any] = MappingProxyType({})


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: Mapping[str, Any] = field(default_factory=lambda: _EMPTY)
    call_id: str = ""


@dataclass(frozen=True)
class ToolContext:
    """Who a call runs for. ``extras`` are further keyword values a handler may ask for."""

    scope: Scope | None = None
    task_id: str | None = None
    session_id: str | None = None
    turn_id: str | None = None
    extras: Mapping[str, Any] = field(default_factory=lambda: _EMPTY)

    def handler_kwargs(self, call: ToolCall) -> dict[str, Any]:
        """Every context keyword on offer; dispatch narrows it to the handler's signature."""
        offered: dict[str, Any] = {
            "task_id": self.task_id,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "tool_call_id": call.call_id or None,
            "scope": self.scope,
        }
        offered.update(self.extras)
        return offered


def is_envelope(value: object) -> bool:
    """True for exactly the multimodal envelope shape and nothing else."""
    if not isinstance(value, Mapping):
        return False
    env = cast(Mapping[str, Any], value)
    return (
        frozenset(env) == ENVELOPE_KEYS
        and env["multimodal"] is True
        and isinstance(env["content"], list)
        and isinstance(env["text_summary"], str)
    )


def bound_error(text: str) -> str:
    if len(text) <= MAX_ERROR_CHARS:
        return text
    return text[:MAX_ERROR_CHARS] + ERROR_CUT_MARKER


def bound_error_field(text: str) -> str:
    """Trim an oversized ``error`` string inside a handler's own JSON result."""
    if len(text) <= MAX_ERROR_CHARS or '"error"' not in text:
        return text
    try:
        payload = json.loads(text)
    except ValueError:
        return text
    if not isinstance(payload, dict):
        return text
    body = cast(dict[str, Any], payload)
    message = body.get("error")
    if not isinstance(message, str) or len(message) <= MAX_ERROR_CHARS:
        return text
    body["error"] = bound_error(message)
    return json.dumps(body, ensure_ascii=False)


_ERROR_PREFIX = re.compile(r'\s*\{\s*"error"\s*:')


def _looks_like_error(text: str) -> bool:
    if not _ERROR_PREFIX.match(text):
        return False
    try:
        payload = json.loads(text)
    except ValueError:
        return False
    return isinstance(payload, dict) and bool(cast(dict[str, Any], payload).get("error"))


@dataclass(frozen=True)
class ToolResult:
    content: str | Mapping[str, Any]
    is_error: bool = False
    error_type: str | None = None

    @property
    def text(self) -> str:
        """The text a size budget counts: the string, or the envelope's summary."""
        if isinstance(self.content, str):
            return self.content
        return str(self.content.get("text_summary", ""))

    @property
    def multimodal(self) -> bool:
        return not isinstance(self.content, str)

    @classmethod
    def ok(cls, content: str | Mapping[str, Any]) -> ToolResult:
        if isinstance(content, str):
            return cls(content, is_error=_looks_like_error(content))
        return cls(MappingProxyType(dict(content)))

    @classmethod
    def error(cls, message: str, *, error_type: str | None = None, **extra: Any) -> ToolResult:
        body: dict[str, Any] = {"error": bound_error(message)}
        if error_type is not None:
            body["error_type"] = error_type
        body.update(extra)
        return cls(json.dumps(body, ensure_ascii=False), is_error=True, error_type=error_type)
