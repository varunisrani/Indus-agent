"""Provider-neutral request, response and stream-event types.

Messages everywhere in the runtime are OpenAI-shaped dicts (``role``, ``content``, ``tool_calls``,
``tool_call_id``, ``name``) plus two reasoning fields an assistant row may carry: ``reasoning``
(readable text) and ``reasoning_details`` (opaque vendor records, kept in arrival order and never
merged or rewritten). Transports translate to and from each wire format; nothing here knows a
vendor.

A streaming reply is a sequence of small events; ``assemble`` folds any such sequence into the
same ``NormalizedResponse`` a non-streaming reply produces.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

type Message = dict[str, Any]
type FinishReason = Literal["stop", "tool_calls", "length", "content_filter", "error"]

FINISH_REASONS: frozenset[str] = frozenset(
    {"stop", "tool_calls", "length", "content_filter", "error"}
)

# The reasoning-effort ladder, weakest first (A53). ``none`` (and its spellings) means "off".
EFFORT_LADDER: tuple[str, ...] = ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")
EFFORT_OFF: frozenset[str] = frozenset({"none", "off", "false", "disabled", "0"})


def normalize_effort(value: str | None) -> str | None:
    """Ladder spelling of ``value``; ``None`` when unset or switched off; unknown text kept."""
    if value is None:
        return None
    text = str(value).strip().lower().replace("-", "").replace("_", "")
    if not text or text in EFFORT_OFF:
        return None
    return text


def clamp_effort(requested: str | None, supported: Sequence[str]) -> str | None:
    """The strongest supported level not above ``requested``.

    When every supported level is stronger than the request, the weakest supported one is used
    (the nearest available). Unknown requested text maps to ``medium``. ``None`` when reasoning is
    off or nothing is supported.
    """
    level = normalize_effort(requested)
    if level is None:
        return None
    ordered = [lvl for lvl in EFFORT_LADDER if lvl in {s.lower() for s in supported}]
    if not ordered:
        return None
    rank = EFFORT_LADDER.index(level) if level in EFFORT_LADDER else EFFORT_LADDER.index("medium")
    weaker = [lvl for lvl in ordered if EFFORT_LADDER.index(lvl) <= rank]
    return weaker[-1] if weaker else ordered[0]


@dataclass(frozen=True)
class ToolCall:
    """One tool call. ``arguments`` is JSON text exactly as the server sent it."""

    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class Usage:
    """Token counts. ``prompt_tokens`` is the whole prompt, cached parts included."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0


@dataclass(frozen=True)
class NormalizedResponse:
    content: str | None
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: FinishReason = "stop"
    reasoning: str | None = None
    reasoning_details: tuple[dict[str, Any], ...] = ()
    usage: Usage | None = None
    provider_data: dict[str, Any] = field(default_factory=dict[str, Any])


@dataclass(frozen=True)
class ChatRequest:
    """What the runtime asks a model for, before any wire format is applied."""

    model: str
    messages: Sequence[Message]
    tools: Sequence[Mapping[str, Any]] = ()
    max_tokens: int | None = None
    temperature: float | None = None
    reasoning_effort: str | None = None
    stream: bool = True
    extra_body: Mapping[str, Any] = field(default_factory=dict[str, Any])
    tool_choice: str | Mapping[str, Any] | None = None


# -- stream events ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ReasoningDelta:
    text: str


@dataclass(frozen=True)
class ReasoningDetail:
    """One opaque reasoning record (a signed thinking block, an aggregator detail entry)."""

    detail: dict[str, Any]


@dataclass(frozen=True)
class ToolCallStart:
    """A tool call at ``index`` begins. A repeat for the same index fills in a late id or name."""

    index: int
    id: str | None
    name: str


@dataclass(frozen=True)
class ToolCallArgsDelta:
    index: int
    fragment: str


@dataclass(frozen=True)
class UsageReport:
    """The cumulative usage so far; the last report wins."""

    usage: Usage


@dataclass(frozen=True)
class Finish:
    reason: FinishReason


@dataclass(frozen=True)
class KeepAlive:
    """A frame that proves the connection is alive but carries no content (comment, ping)."""

    text: str = ""


type StreamEvent = (
    TextDelta
    | ReasoningDelta
    | ReasoningDetail
    | ToolCallStart
    | ToolCallArgsDelta
    | UsageReport
    | Finish
    | KeepAlive
)

CONTENT_EVENTS: tuple[type, ...] = (
    TextDelta,
    ReasoningDelta,
    ReasoningDetail,
    ToolCallStart,
    ToolCallArgsDelta,
    UsageReport,
)


def map_finish_reason(raw: str | None, table: Mapping[str, str]) -> FinishReason:
    """Translate a vendor stop reason with ``table``; unknown or missing → ``stop``."""
    if raw is None:
        return "stop"
    mapped = table.get(raw)
    if mapped is None or mapped not in FINISH_REASONS:
        return "stop"
    return mapped  # type: ignore[return-value]


@dataclass
class _CallSlot:
    id: str | None = None
    name: str = ""
    parts: list[str] = field(default_factory=list[str])


def assemble(events: Iterable[StreamEvent]) -> NormalizedResponse:
    """Fold stream events into one response (order of tool calls = order of first start)."""
    text: list[str] = []
    reasoning: list[str] = []
    details: list[dict[str, Any]] = []
    calls: dict[int, _CallSlot] = {}
    usage: Usage | None = None
    finish: FinishReason | None = None
    for event in events:
        match event:
            case TextDelta(text=t):
                text.append(t)
            case ReasoningDelta(text=t):
                reasoning.append(t)
            case ReasoningDetail(detail=d):
                details.append(d)
            case ToolCallStart(index=i, id=call_id, name=name):
                slot = calls.setdefault(i, _CallSlot())
                if call_id:
                    slot.id = call_id
                if name:
                    slot.name = name
            case ToolCallArgsDelta(index=i, fragment=frag):
                calls.setdefault(i, _CallSlot()).parts.append(frag)
            case UsageReport(usage=u):
                usage = u
            case Finish(reason=r):
                finish = r
            case KeepAlive():
                pass
    tool_calls = tuple(
        ToolCall(
            id=slot.id or f"call_{index}",
            name=slot.name,
            arguments="".join(slot.parts) if "".join(slot.parts).strip() else "{}",
        )
        for index, slot in sorted(calls.items())
    )
    if finish is None:
        finish = "tool_calls" if tool_calls else "stop"
    return NormalizedResponse(
        content="".join(text) if text else None,
        tool_calls=tool_calls,
        finish_reason=finish,
        reasoning="".join(reasoning) if reasoning else None,
        reasoning_details=tuple(details),
        usage=usage,
    )


def arguments_object(arguments: str) -> dict[str, Any] | None:
    """The tool arguments as a JSON object, or ``None`` when they are not one."""
    try:
        value = json.loads(arguments) if arguments.strip() else {}
    except ValueError:
        return None
    return value if isinstance(value, dict) else None
