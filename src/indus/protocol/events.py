"""Events a session streams to its clients.

On the wire an event is a JSON-RPC notification whose ``method`` is the event's ``type`` and
whose ``params`` is the whole event model. Every event names its session and carries ``seq``, a
per-session counter that only grows, so a client that reconnects can ask for what it missed.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, JsonValue, NonNegativeInt

from indus.protocol.base import ProtocolModel


class ExitReason(StrEnum):
    """Why a turn ended. Closed set: every exit path of the turn loop maps to one value."""

    COMPLETED = "completed"
    INTERRUPTED_BY_USER = "interrupted_by_user"
    INTERRUPTED_BY_SYSTEM = "interrupted_by_system"
    BUDGET_EXHAUSTED = "budget_exhausted"
    MAX_ITERATIONS = "max_iterations"
    INVALID_TOOL_STRIKES = "invalid_tool_strikes"
    PERSISTENCE_FAILED = "persistence_failed"
    PROVIDER_FAILED = "provider_failed"
    COMPRESSION_EXHAUSTED = "compression_exhausted"
    GUARDRAIL_HALT = "guardrail_halt"


class StatusKind(StrEnum):
    """What the session is doing, for a status line."""

    WORKING = "working"
    WAITING = "waiting"
    RETRYING = "retrying"
    COMPRESSING = "compressing"
    READY = "ready"


class NoticeLevel(StrEnum):
    """How loud a notice is."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class CancelReason(StrEnum):
    """Why an open server request was withdrawn."""

    INTERRUPTED = "interrupted"
    TURN_ENDED = "turn_ended"
    TIMED_OUT = "timed_out"
    ANSWERED_ELSEWHERE = "answered_elsewhere"
    SESSION_CLOSED = "session_closed"


class EventBase(ProtocolModel):
    """Fields every event carries."""

    session_id: str = Field(min_length=1)
    """The session the event belongs to."""
    seq: NonNegativeInt
    """Position in the session's event stream; strictly increasing."""


class TurnStarted(EventBase):
    """A turn began running."""

    type: Literal["turn.started"] = "turn.started"
    """Event type."""
    turn_id: str
    """The turn's id, as returned by ``prompt.submit``."""
    generation: NonNegativeInt
    """The turn's generation; interrupts naming an older one are ignored."""


class MessageDelta(EventBase):
    """A piece of the assistant's visible reply."""

    type: Literal["message.delta"] = "message.delta"
    """Event type."""
    turn_id: str
    """The turn this text belongs to."""
    text: str
    """Raw text to append; clients render it themselves."""


class ReasoningDelta(EventBase):
    """A piece of the model's reasoning, shown apart from the reply."""

    type: Literal["reasoning.delta"] = "reasoning.delta"
    """Event type."""
    turn_id: str
    """The turn this reasoning belongs to."""
    text: str
    """Raw reasoning text to append."""


class ToolStarted(EventBase):
    """A tool call began."""

    type: Literal["tool.started"] = "tool.started"
    """Event type."""
    turn_id: str
    """The turn making the call."""
    call_id: str
    """The tool call's id, unique within the turn."""
    name: str
    """The tool's name."""
    arguments: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])
    """The parsed arguments the tool runs with."""


class ToolProgress(EventBase):
    """A running tool reported progress."""

    type: Literal["tool.progress"] = "tool.progress"
    """Event type."""
    turn_id: str
    """The turn making the call."""
    call_id: str
    """The tool call's id."""
    message: str
    """A short progress line."""
    fraction: Annotated[float, Field(ge=0.0, le=1.0)] | None = None
    """Completed share between 0 and 1, when the tool knows it."""


class ToolCompleted(EventBase):
    """A tool call finished (successfully or not)."""

    type: Literal["tool.completed"] = "tool.completed"
    """Event type."""
    turn_id: str
    """The turn making the call."""
    call_id: str
    """The tool call's id."""
    name: str
    """The tool's name."""
    ok: bool
    """False when the tool failed, was skipped or was cancelled."""
    duration_ms: NonNegativeInt
    """Wall time the call took."""
    result: str
    """The result text the model sees."""


class Status(EventBase):
    """The session's activity changed."""

    type: Literal["status"] = "status"
    """Event type."""
    kind: StatusKind
    """What the session is doing."""
    text: str = ""
    """Optional detail for the status line."""


class Notice(EventBase):
    """A message for the user that is not part of the conversation."""

    type: Literal["notice"] = "notice"
    """Event type."""
    level: NoticeLevel
    """How loud the notice is."""
    text: str
    """The notice text."""
    key: str | None = None
    """A stable key; a later notice with the same key replaces this one."""


class ServerRequestCancelled(EventBase):
    """An open server request was withdrawn; clients close its prompt."""

    type: Literal["server_request.cancelled"] = "server_request.cancelled"
    """Event type."""
    request_id: str
    """The withdrawn request's id."""
    reason: CancelReason
    """Why it was withdrawn."""


class TurnFailure(ProtocolModel):
    """Why a turn failed and whether trying again may help."""

    reason: str
    """A short, human-readable failure reason."""
    retryable: bool
    """Whether submitting again later may succeed."""
    resets_at: datetime | None = None
    """When a rate limit or quota resets, if known."""


class TurnDone(EventBase):
    """A turn ended. Sent exactly once per turn, after its last row is stored."""

    type: Literal["turn.done"] = "turn.done"
    """Event type."""
    turn_id: str
    """The turn that ended."""
    reason: ExitReason
    """Why it ended."""
    failure: TurnFailure | None = None
    """Set when the turn ended in a failure."""


class LearningReviewDone(EventBase):
    """A background review of the conversation finished (memory and skills, P09-16)."""

    type: Literal["learning.review.done"] = "learning.review.done"
    """Event type."""
    kinds: list[str]
    """What the review covered: ``memory``, ``skills`` or both."""
    summary_lines: list[str] = Field(default_factory=list[str])
    """One short line per saved change, ready to show; empty when nothing was saved."""


class GoalUpdate(EventBase):
    """The session's standing goal changed (set, judged, continued, paused, done, cleared)."""

    type: Literal["goal.update"] = "goal.update"
    """Event type."""
    status: Literal["active", "paused", "done", "cleared"]
    """Where the goal stands."""
    goal: str
    """The objective."""
    turns_used: NonNegativeInt
    """Turns counted against the budget so far."""
    max_turns: NonNegativeInt
    """The turn budget."""
    reason: str = ""
    """Why it changed: the judge's reason, a failing gate, a pause cause."""


class LoopUpdate(EventBase):
    """One of the session's loops changed (set, ticked, finished, paused, stopped)."""

    type: Literal["loop.update"] = "loop.update"
    """Event type."""
    loop_id: str
    """The loop's id within the session."""
    status: Literal["active", "paused", "done", "stopped"]
    """Where the loop stands."""
    prompt: str
    """The repeated prompt."""
    ticks: NonNegativeInt
    """Runs fired so far."""
    next_due_at: datetime | None = None
    """When the next run is due, while active."""
    reason: str = ""
    """Why it changed, when it stopped or paused."""


class HeartbeatFired(EventBase):
    """A heartbeat sent its recurring instruction into the session (shown as a "wake" row)."""

    type: Literal["heartbeat.fired"] = "heartbeat.fired"
    """Event type."""
    heartbeat_id: str
    """The heartbeat's id within the session."""
    prompt: str
    """The recurring instruction."""
    fire_count: NonNegativeInt
    """How many times it has fired, this one included."""


type Event = Annotated[
    TurnStarted
    | MessageDelta
    | ReasoningDelta
    | ToolStarted
    | ToolProgress
    | ToolCompleted
    | Status
    | Notice
    | ServerRequestCancelled
    | TurnDone
    | LearningReviewDone
    | GoalUpdate
    | LoopUpdate
    | HeartbeatFired,
    Field(discriminator="type"),
]
"""Any event, told apart by ``type``."""

EVENT_MODELS: tuple[type[EventBase], ...] = (
    TurnStarted,
    MessageDelta,
    ReasoningDelta,
    ToolStarted,
    ToolProgress,
    ToolCompleted,
    Status,
    Notice,
    ServerRequestCancelled,
    TurnDone,
    LearningReviewDone,
    GoalUpdate,
    LoopUpdate,
    HeartbeatFired,
)


def event_type(model: type[EventBase]) -> str:
    """The ``type`` value (and notification method) of an event model."""
    default = model.model_fields["type"].default
    assert isinstance(default, str)
    return default


EVENTS: dict[str, type[EventBase]] = {event_type(m): m for m in EVENT_MODELS}
"""Event type → model."""
