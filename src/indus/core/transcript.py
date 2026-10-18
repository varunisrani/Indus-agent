"""Transcript rows: the live history of one session as the turn loop sees it.

A ``TranscriptRow`` is one message with everything the loop needs beside the provider fields:
the durable identity it was stored under, the ``api_content`` sidecar (the exact bytes a row was
sent with before, replayed instead of ``content`` so the prompt cache stays warm), how the row is
shown (``display_kind``; ``steer`` marks the one mid-loop user row nothing may be folded into) and
whether it is ``scaffold`` (a private nudge that is never kept in the durable snapshot).

Tool calls stay provider-neutral dicts ``{"id", "type": "function", "function": {"name",
"arguments"}}`` (the P03 message shape). ``reasoning_details`` are opaque vendor records kept in
arrival order and never merged into each other.

Rows convert at the edges: ``from_message``/``to_message`` for provider-shaped dicts,
``from_store``/``to_store`` for the state store (whose tool-name column is ``tool_name``).
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any, cast

from indus.state.messages import PARTS_PREFIX

__all__ = [
    "DISPLAY_STEER",
    "ROLES",
    "TranscriptRow",
    "call_arguments",
    "call_id",
    "call_name",
    "content_has_payload",
    "is_damaged_payload",
]

DISPLAY_STEER = "steer"
ROLES = frozenset({"system", "user", "assistant", "tool"})


def call_id(call: Mapping[str, Any]) -> str:
    """The pairing id of a tool call (``id``, else ``call_id``), stripped; ``""`` when none."""
    for key in ("id", "call_id"):
        value = call.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def call_name(call: Mapping[str, Any]) -> str | None:
    function = call.get("function")
    if isinstance(function, Mapping):
        name = cast(Mapping[str, Any], function).get("name")
        return name if isinstance(name, str) else None
    return None


def call_arguments(call: Mapping[str, Any]) -> str | None:
    function = call.get("function")
    if isinstance(function, Mapping):
        args = cast(Mapping[str, Any], function).get("arguments")
        return args if isinstance(args, str) else None
    return None


def is_damaged_payload(content: object) -> bool:
    """A string that still carries the parts prefix: its JSON did not decode."""
    return isinstance(content, str) and content.startswith(PARTS_PREFIX)


def content_has_payload(content: object) -> bool:
    """Does ``content`` carry anything a provider counts as non-empty?"""
    if content is None:
        return False
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        for part in cast(list[object], content):
            if isinstance(part, Mapping):
                block = cast(Mapping[str, Any], part)
                if block.get("type") != "text":
                    return True
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    return True
            elif part:
                return True
        return False
    return True


def _call_list(raw: object) -> list[dict[str, Any]] | None:
    if raw is None:
        return None
    if isinstance(raw, Mapping):
        return [copy.deepcopy(dict(cast(Mapping[str, Any], raw)))]
    if isinstance(raw, (list, tuple)):
        out: list[dict[str, Any]] = []
        for item in cast(list[object], list(cast(Any, raw))):
            if isinstance(item, Mapping):
                out.append(copy.deepcopy(dict(cast(Mapping[str, Any], item))))
        return out
    return None


def _details(raw: object) -> list[dict[str, Any]] | None:
    if raw is None:
        return None
    if isinstance(raw, Mapping):
        return [copy.deepcopy(dict(cast(Mapping[str, Any], raw)))]
    if isinstance(raw, (list, tuple)):
        return [
            copy.deepcopy(dict(cast(Mapping[str, Any], item)))
            for item in cast(list[object], list(cast(Any, raw)))
            if isinstance(item, Mapping)
        ]
    return None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


@dataclass(slots=True)
class TranscriptRow:
    role: str
    content: Any = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    name: str | None = None
    reasoning: str | None = None
    reasoning_details: list[dict[str, Any]] | None = None
    api_content: str | None = None
    display_kind: str | None = None
    scaffold: bool = False
    finish_reason: str | None = None
    message_uid: str | None = None
    tool_call_uids: dict[str, str] | None = None
    row_id: int | None = None
    timestamp: float | None = None
    extra: dict[str, Any] = field(default_factory=dict[str, Any])

    # -- views -----------------------------------------------------------------------------

    @property
    def is_steer(self) -> bool:
        return self.display_kind == DISPLAY_STEER

    def call_ids(self) -> list[str]:
        return [call_id(c) for c in self.tool_calls or ()]

    def has_payload(self) -> bool:
        """Text, parts, tool calls or reasoning: anything a provider counts as content."""
        return bool(
            content_has_payload(self.content)
            or self.tool_calls
            or (self.reasoning and self.reasoning.strip())
            or self.reasoning_details
        )

    def clone(self, **changes: Any) -> TranscriptRow:
        """A deep copy with ``changes`` applied (the original is never touched)."""
        return replace(copy.deepcopy(self), **changes)

    # -- conversions -------------------------------------------------------------------------

    @classmethod
    def from_message(cls, msg: Mapping[str, Any]) -> TranscriptRow:
        """A row from a provider-shaped or store-shaped dict (deep-copied)."""
        known = {
            "role",
            "content",
            "tool_calls",
            "tool_call_id",
            "name",
            "tool_name",
            "reasoning",
            "reasoning_details",
            "api_content",
            "display_kind",
            "scaffold",
            "finish_reason",
            "message_uid",
            "tool_call_uids",
            "id",
            "row_id",
            "timestamp",
        }
        uids = msg.get("tool_call_uids")
        row_id = msg.get("row_id", msg.get("id"))
        stamp = msg.get("timestamp")
        return cls(
            role=str(msg.get("role") or ""),
            content=copy.deepcopy(msg.get("content")),
            tool_calls=_call_list(msg.get("tool_calls")) or None,
            tool_call_id=_text(msg.get("tool_call_id")),
            name=_text(msg.get("name")) or _text(msg.get("tool_name")),
            reasoning=_text(msg.get("reasoning")),
            reasoning_details=_details(msg.get("reasoning_details")) or None,
            api_content=_text(msg.get("api_content")),
            display_kind=_text(msg.get("display_kind")),
            scaffold=bool(msg.get("scaffold")),
            finish_reason=_text(msg.get("finish_reason")),
            message_uid=_text(msg.get("message_uid")),
            tool_call_uids=(
                {str(k): str(v) for k, v in cast(Mapping[object, object], uids).items()}
                if isinstance(uids, Mapping)
                else None
            ),
            row_id=row_id if isinstance(row_id, int) and not isinstance(row_id, bool) else None,
            timestamp=float(stamp)
            if isinstance(stamp, (int, float)) and not isinstance(stamp, bool)
            else None,
            extra={k: copy.deepcopy(v) for k, v in msg.items() if k not in known},
        )

    @classmethod
    def from_store(cls, row: Mapping[str, Any]) -> TranscriptRow:
        """A row from ``indus.state.messages.get`` (content is decoded there; ``id`` → row id)."""
        return cls.from_message(row)

    def to_message(self) -> dict[str, Any]:
        """Provider-shaped dict with every live field (sidecar included; no storage ids)."""
        msg: dict[str, Any] = {"role": self.role, "content": copy.deepcopy(self.content)}
        if self.tool_calls:
            msg["tool_calls"] = copy.deepcopy(self.tool_calls)
        if self.tool_call_id is not None:
            msg["tool_call_id"] = self.tool_call_id
        if self.name is not None:
            msg["name"] = self.name
        if self.reasoning is not None:
            msg["reasoning"] = self.reasoning
        if self.reasoning_details:
            msg["reasoning_details"] = copy.deepcopy(self.reasoning_details)
        if self.api_content is not None:
            msg["api_content"] = self.api_content
        return msg

    def to_store(self) -> dict[str, Any]:
        """The dict ``indus.state.messages.append`` takes (no display kind or scaffold column)."""
        msg = self.to_message()
        if "name" in msg:
            msg["tool_name"] = msg.pop("name")
        for key, value in (
            ("finish_reason", self.finish_reason),
            ("message_uid", self.message_uid),
            ("tool_call_uids", copy.deepcopy(self.tool_call_uids)),
            ("timestamp", self.timestamp),
        ):
            if value is not None:
                msg[key] = value
        return msg
