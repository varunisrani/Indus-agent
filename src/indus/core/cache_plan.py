"""Prompt-cache markers: who gets them, where they go, and for how long (P08-07).

Providers with explicit cache breakpoints (Anthropic's ``cache_control``) reuse a request prefix
only up to a marked block, and a request may carry at most four marks. The plan is made last, on
the request copy that is about to leave for one route (``wire.finish_request``), so a change to
the transcript, a retry or a fallback route always gets a fresh plan and the stored history never
holds a marker.

Two layouts:

- with a **static system prefix** the system text starts with (and goes on past): the system
  message becomes two text parts, the prefix and the rest, both marked, plus the two newest
  messages that can carry a mark;
- otherwise one mark on the system message and the three newest carriers.

On one message: string content becomes a one-part list holding the mark; list content marks its
last part; empty content, or a tool message on the native Anthropic wire, carries the mark on the
message itself (the Anthropic transport moves it onto the right block). On the aggregator wire a
message whose content cannot hold a part mark is skipped, so the budget goes to an older one.

TTL: ``5m`` (default), ``1h``, or ``auto`` — one hour when a person paces the session, five
minutes for automated sources, whose calls come seconds apart anyway.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any, Final, cast

__all__ = [
    "AGGREGATOR_HOSTS",
    "AUTOMATED_SOURCES",
    "MARKER_KEY",
    "MAX_MARKERS",
    "NATIVE_WIRE",
    "TTL_AUTO",
    "TTL_DEFAULT",
    "TTL_LONG",
    "cache_hit_percent",
    "count_markers",
    "decide",
    "make_marker",
    "plan_messages",
    "resolve_ttl",
    "strip_markers",
    "strip_tool_markers",
]

log = logging.getLogger(__name__)

type Message = dict[str, Any]

MARKER_KEY: Final = "cache_control"
MAX_MARKERS: Final = 4
TTL_DEFAULT: Final = "5m"
TTL_LONG: Final = "1h"
TTL_AUTO: Final = "auto"
NATIVE_WIRE: Final = "anthropic_messages"
# Aggregators that pass ``cache_control`` on content parts through to Claude models.
AGGREGATOR_HOSTS: Final = frozenset({"openrouter.ai"})
# Session sources no person paces (``auto`` gives them the five-minute tier).
AUTOMATED_SOURCES: Final = frozenset(
    {
        "subagent",
        "cron",
        "oneshot",
        "one-shot",
        "webhook",
        "api",
        "api_server",
        "batch",
        "tool",
        "kanban",
    }
)


# -- policy ---------------------------------------------------------------------------------------


def decide(
    *, api_mode: str, model: str, host: str, profile: object, entry: Mapping[str, Any]
) -> tuple[bool, str]:
    """``(markers, reason)`` for a route. ``entry`` is the provider's config entry: its
    ``prompt_caching`` (true/false) wins; then a profile that declares ``prompt_cache``; then the
    built-in rule — Claude models on the native wire or on an aggregator that passes marks on."""
    setting = entry.get("prompt_caching")
    if isinstance(setting, bool):
        return setting, "config"
    if getattr(profile, "prompt_cache", False) is True:
        return True, "profile"
    claude = "claude" in model.lower()
    return claude and (api_mode == NATIVE_WIRE or host in AGGREGATOR_HOSTS), "wire"


def resolve_ttl(setting: object, source: str | None) -> str:
    """The tier to send: ``5m`` or ``1h`` (``auto`` decided by who drives the session)."""
    text = setting.strip().lower() if isinstance(setting, str) else ""
    if not text:
        return TTL_DEFAULT
    if text == TTL_AUTO:
        who = (source or "").strip().lower()
        return TTL_DEFAULT if who in AUTOMATED_SOURCES else TTL_LONG
    if text in (TTL_DEFAULT, TTL_LONG):
        return text
    log.debug("prompt_caching.cache_ttl %r is not 5m, 1h or auto; using 5m", setting)
    return TTL_DEFAULT


def make_marker(ttl: str) -> dict[str, str]:
    """The marker object for a tier (the five-minute tier is the API default and has no field)."""
    return {"type": "ephemeral", "ttl": TTL_LONG} if ttl == TTL_LONG else {"type": "ephemeral"}


def cache_hit_percent(prompt_tokens: int, cache_read_tokens: int) -> float | None:
    """Share of the prompt served from cache, in percent; None when there was no prompt."""
    if prompt_tokens <= 0:
        return None
    return round(100.0 * min(cache_read_tokens, prompt_tokens) / prompt_tokens, 1)


# -- stripping ------------------------------------------------------------------------------------


def _parts(content: object) -> list[Any] | None:
    return cast(list[Any], content) if isinstance(content, list) else None


def _strip_one(message: Mapping[str, Any]) -> Message | None:
    """A copy of ``message`` without markers, or None when it carries none."""
    parts = _parts(message.get("content"))
    marked_parts = parts is not None and any(isinstance(p, dict) and MARKER_KEY in p for p in parts)
    if MARKER_KEY not in message and not marked_parts:
        return None
    out: Message = {k: v for k, v in message.items() if k != MARKER_KEY}
    if parts is not None and marked_parts:
        out["content"] = [
            {k: v for k, v in cast(dict[str, Any], p).items() if k != MARKER_KEY}
            if isinstance(p, dict)
            else p
            for p in parts
        ]
    return out


def strip_markers(messages: Sequence[Mapping[str, Any]]) -> list[Message]:
    """The messages with every marker removed; messages without one are passed through as is
    (the input is never changed)."""
    out: list[Message] = []
    for message in messages:
        cleaned = _strip_one(message)
        out.append(cleaned if cleaned is not None else cast(Message, message))
    return out


def strip_tool_markers(tools: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {k: v for k, v in tool.items() if k != MARKER_KEY}
        if MARKER_KEY in tool
        else cast(dict[str, Any], tool)
        for tool in tools
    ]


def count_markers(messages: Sequence[Mapping[str, Any]]) -> int:
    """Markers on envelopes and on content parts."""
    total = 0
    for message in messages:
        if MARKER_KEY in message:
            total += 1
        parts = _parts(message.get("content"))
        if parts is not None:
            total += sum(1 for p in parts if isinstance(p, dict) and MARKER_KEY in p)
    return total


# -- planning -------------------------------------------------------------------------------------


def _text_part(text: str, marker: Mapping[str, str] | None = None) -> dict[str, Any]:
    part: dict[str, Any] = {"type": "text", "text": text}
    if marker is not None:
        part[MARKER_KEY] = dict(marker)
    return part


def _can_carry(message: Mapping[str, Any], *, native: bool) -> bool:
    if native:
        return True
    content = message.get("content")
    if isinstance(content, str):
        return content != ""
    parts = _parts(content)
    return bool(parts) and isinstance(parts[-1], dict) if parts is not None else False


def _mark(message: Message, marker: Mapping[str, str], *, native: bool) -> None:
    """Put one marker on ``message`` (a copy the caller owns)."""
    content = message.get("content")
    if (message.get("role") == "tool" and native) or content is None or content == "":
        message[MARKER_KEY] = dict(marker)
        return
    if isinstance(content, str):
        message["content"] = [_text_part(content, marker)]
        return
    parts = _parts(content)
    if parts and isinstance(parts[-1], dict):
        last = dict(cast(dict[str, Any], parts[-1]))
        last[MARKER_KEY] = dict(marker)
        message["content"] = [*parts[:-1], last]
        return
    if native:
        message[MARKER_KEY] = dict(marker)


def _mark_system(
    message: Message, marker: Mapping[str, str], static_prefix: str | None, *, native: bool
) -> int:
    content = message.get("content")
    if static_prefix and isinstance(content, str) and content.startswith(static_prefix):
        rest = content[len(static_prefix) :]
        if rest.strip():
            message["content"] = [_text_part(static_prefix, marker), _text_part(rest, marker)]
            return 2
    if not _can_carry(message, native=native):
        return 0
    _mark(message, marker, native=native)
    return 1


def plan_messages(
    messages: Sequence[Mapping[str, Any]],
    *,
    ttl: str,
    native: bool,
    static_prefix: str | None = None,
) -> list[Message]:
    """A new message list carrying at most ``MAX_MARKERS`` markers (see the module notes)."""
    out = strip_markers(messages)
    marker = make_marker(ttl)
    used = 0
    if out and out[0].get("role") == "system":
        first = dict(out[0])
        used = _mark_system(first, marker, static_prefix, native=native)
        out[0] = first
    carriers = [
        index
        for index, message in enumerate(out)
        if message.get("role") != "system" and _can_carry(message, native=native)
    ]
    budget = MAX_MARKERS - used
    for index in carriers[-budget:] if budget > 0 else []:
        copy = dict(out[index])
        _mark(copy, marker, native=native)
        out[index] = copy
    return out
