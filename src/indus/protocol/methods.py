"""Method params and results, and the method catalogue.

Each method has a params model, a result model and the error codes it may return. Params refuse
unknown keys, so a misspelt option is an ``INVALID_PARAMS`` error that names the key.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Literal

from pydantic import Field, NonNegativeInt, ValidationError

from indus.protocol.attach import ATTACH_METHODS, ATTACH_NOTIFICATIONS
from indus.protocol.base import (
    TEXT_EMPTY_ERROR,
    MessageText,
    ProtocolModel,
    SessionId,
)
from indus.protocol.bots import BOT_METHODS
from indus.protocol.bridge import BRIDGE_METHODS, BRIDGE_NOTIFICATIONS, HistoryMessage
from indus.protocol.capabilities import CAPABILITY_METHODS
from indus.protocol.errors import (
    ADMISSION_CONFLICT,
    ANSWER_KIND_MISMATCH,
    COMMON_ERRORS,
    IDEMPOTENCY_CONFLICT,
    INVALID_PARAMS,
    LEASE_LOST,
    METHOD_NOT_FOUND,
    OUTCOME_UNKNOWN,
    POLICY_CONFLICT,
    PROVIDER_UNAVAILABLE,
    QUEUE_FULL,
    SERVER_REQUEST_NOT_FOUND,
    SERVER_REQUEST_UNSUPPORTED,
    SESSION_BUSY,
    SESSION_NOT_FOUND,
    SESSION_REFUSED,
    STORE_FAILED,
    TEXT_EMPTY,
    UNKNOWN_PARAM_KEY,
    ErrorCode,
)
from indus.protocol.groups import GROUP_METHODS
from indus.protocol.pets import PET_METHODS
from indus.protocol.projects import PROJECT_METHODS
from indus.protocol.server_requests import ServerRequest, ServerRequestAnswer
from indus.protocol.voice import VOICE_METHODS
from indus.protocol.wake import WAKE_DETECTED_EVENT, WAKE_METHODS, WakeDetected

PROTOCOL_VERSION = 1
"""Bumped when a change breaks existing clients."""


class SessionCreateParams(ProtocolModel):
    """Create a session."""

    title: str | None = None
    """A title for the session; absent leaves it untitled."""
    cwd: str | None = None
    """The working directory tools start in; absent uses the server's."""
    model: str | None = None
    """The model to use; absent uses the configured default."""
    toolsets: list[str] | None = None
    """Toolsets to enable; absent uses the surface's default selection."""
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)
    """Repeating a create with the same key returns the first session instead of a new one."""


class SessionCreateResult(ProtocolModel):
    """The created (or, for a repeated idempotency key, the existing) session."""

    session_id: str
    """The session's id."""
    created: bool
    """False when an earlier create with the same idempotency key is being returned."""


class SessionResumeParams(ProtocolModel):
    """Attach to an existing session."""

    session_id: SessionId
    """The session to resume."""


class SessionResumeResult(ProtocolModel):
    """What a client needs to pick a session back up."""

    session_id: str
    """The resumed session's id."""
    message_count: NonNegativeInt
    """Number of stored messages."""
    last_seq: NonNegativeInt
    """The seq of the last event sent so far; events after it are still to come."""
    pending_requests: list[ServerRequest] = Field(default_factory=list[ServerRequest])
    """Server requests still waiting for an answer, re-delivered after a reconnect."""
    title: str | None = None
    """The session's title, when a surface asked for the display projection."""
    messages: list[HistoryMessage] = Field(default_factory=list[HistoryMessage])
    """The stored conversation for display, when a surface asked for it (empty otherwise)."""


class BusyAction(StrEnum):
    """What a submit does while the session is running a turn."""

    REJECT = "reject"
    QUEUE = "queue"
    STEER = "steer"
    REDIRECT = "redirect"


class SubmitStatus(StrEnum):
    """What happened to a submitted message."""

    STREAMING = "streaming"
    QUEUED = "queued"
    STEERED = "steered"
    REDIRECTED = "redirected"


class PromptSubmitParams(ProtocolModel):
    """Send a user message and start a turn."""

    session_id: SessionId
    """The session to run the turn in."""
    text: MessageText
    """The user's message."""
    when_busy: BusyAction = BusyAction.REJECT
    """While a turn runs: refuse (``SESSION_BUSY``), queue it, steer or redirect the turn."""
    request_id: str | None = Field(default=None, min_length=1, max_length=200)
    """The client's id for this input (daemon): sending it again returns the first admission
    instead of running the text twice."""
    unattended: bool = False
    """A finite run nobody watches (``indus -z``; daemon): this input's turn auto-approves risky
    commands as the in-process run does (the hard floors still refuse). In-process hosts take the
    run's approval policy from its launch and ignore it."""


class PromptSubmitResult(ProtocolModel):
    """The ticket for the started turn."""

    turn_id: str
    """The turn's id; its events carry it. For a queued or steered message: the running turn.
    From the daemon: the input's admission id."""
    generation: NonNegativeInt | None = None
    """The turn's generation, for interrupts. Left out only by the daemon for an input it has not
    claimed yet (``queued`` with no turn running): follow it by ``turn.started`` or
    ``admission.state``."""

    def require_generation(self) -> int:
        """The generation an in-process host always answers; ``ValueError`` when it is left out
        (only the daemon does that, for an input it has not claimed yet)."""
        if self.generation is None:
            raise ValueError("the submit answer names no generation")
        return self.generation

    status: SubmitStatus = SubmitStatus.STREAMING
    """``streaming`` when this message started the turn."""


class SessionInterruptParams(ProtocolModel):
    """Stop the running turn."""

    session_id: SessionId
    """The session whose turn to stop."""
    generation: NonNegativeInt | None = None
    """Only interrupt this generation; a stale one makes the call a no-op."""
    hard: bool = False
    """Also drop queued steer text."""


class SessionInterruptResult(ProtocolModel):
    """Whether a turn was interrupted."""

    interrupted: bool
    """False when no turn (or no turn of that generation) was running."""


class SessionSteerParams(ProtocolModel):
    """Add guidance the running turn picks up after its current step."""

    session_id: SessionId
    """The session with the running turn."""
    text: MessageText
    """The guidance."""
    generation: NonNegativeInt | None = None
    """The turn the guidance is for; the daemon requires it and ignores another turn's steer."""


class SessionSteerResult(ProtocolModel):
    """Whether the guidance was queued for the running turn."""

    queued: bool
    """False when no turn is running; the client should send it as a new prompt."""
    ticket: str | None = None
    """Identifies the queued text so it can be withdrawn."""


class RedirectOutcome(StrEnum):
    """What a redirect did."""

    REDIRECTED = "redirected"
    STEERED = "steered"
    REJECTED = "rejected"


class SessionRedirectParams(ProtocolModel):
    """Correct the running turn: cancel the model call in flight and retry with this text."""

    session_id: SessionId
    """The session with the running turn."""
    text: MessageText
    """The correction."""


class SessionRedirectResult(ProtocolModel):
    """What happened to the correction."""

    outcome: RedirectOutcome
    """``steered`` when tools were running, ``rejected`` when no turn was live."""


class ServerRequestAnswerParams(ProtocolModel):
    """Answer an open server request."""

    session_id: SessionId
    """The session that asked."""
    request_id: str = Field(min_length=1)
    """The request being answered."""
    answer: ServerRequestAnswer
    """The answer; its kind must match the request's."""
    generation: NonNegativeInt | None = None
    """The turn that asked (as offered); the daemon requires it and refuses another turn's."""


class ServerRequestAnswerResult(ProtocolModel):
    """Confirmation that the answer was delivered."""

    accepted: bool
    """True when the waiting agent received the answer."""


class ClientCapabilitiesParams(ProtocolModel):
    """Tell the server which optional features this client supports."""

    features: list[str] = Field(default_factory=list[str])
    """Feature names; names the server does not know are ignored."""
    surface: Literal["cli", "tui", "gui", "acp", "web"] | None = None
    """The kind of client attaching to the daemon (decides the busy default and presence)."""


class ClientCapabilitiesResult(ProtocolModel):
    """The features both sides support (none are defined yet)."""

    features: list[str]
    """The enabled feature names."""


SERVER_REQUESTS_FEATURE = "server_requests"
"""The client shows every kind of server request; ``server_requests.<kind>`` names one kind."""

KNOWN_CLIENT_FEATURES: frozenset[str] = frozenset(
    {SERVER_REQUESTS_FEATURE}
    | {f"{SERVER_REQUESTS_FEATURE}.{kind}" for kind in ("approval", "clarify", "sudo", "secret")}
)
"""Features a client may turn on (D-16 §2.3)."""


@dataclass(frozen=True, slots=True)
class MethodSpec:
    """One catalogue entry."""

    name: str
    params: type[ProtocolModel]
    result: type[ProtocolModel]
    errors: tuple[ErrorCode, ...]

    @property
    def summary(self) -> str:
        """The first line of the params model's docstring."""
        return (self.params.__doc__ or "").strip().splitlines()[0]


def _spec(
    name: str,
    params: type[ProtocolModel],
    result: type[ProtocolModel],
    *errors: ErrorCode,
) -> MethodSpec:
    return MethodSpec(name=name, params=params, result=result, errors=COMMON_ERRORS + errors)


_SESSION_ERRORS = (SESSION_NOT_FOUND, SESSION_REFUSED, STORE_FAILED)

METHODS: Mapping[str, MethodSpec] = MappingProxyType(
    {
        spec.name: spec
        for spec in (
            _spec(
                "session.create",
                SessionCreateParams,
                SessionCreateResult,
                IDEMPOTENCY_CONFLICT,
                PROVIDER_UNAVAILABLE,
                STORE_FAILED,
                POLICY_CONFLICT,
            ),
            _spec(
                "session.resume",
                SessionResumeParams,
                SessionResumeResult,
                *_SESSION_ERRORS,
                SESSION_BUSY,
                POLICY_CONFLICT,
            ),
            _spec(
                "prompt.submit",
                PromptSubmitParams,
                PromptSubmitResult,
                *_SESSION_ERRORS,
                TEXT_EMPTY,
                SESSION_BUSY,
                PROVIDER_UNAVAILABLE,
                ADMISSION_CONFLICT,
                QUEUE_FULL,
                OUTCOME_UNKNOWN,
                LEASE_LOST,
            ),
            _spec(
                "session.interrupt",
                SessionInterruptParams,
                SessionInterruptResult,
                SESSION_NOT_FOUND,
            ),
            _spec(
                "session.steer",
                SessionSteerParams,
                SessionSteerResult,
                SESSION_NOT_FOUND,
                TEXT_EMPTY,
            ),
            _spec(
                "session.redirect",
                SessionRedirectParams,
                SessionRedirectResult,
                SESSION_NOT_FOUND,
                TEXT_EMPTY,
            ),
            _spec(
                "server_request.answer",
                ServerRequestAnswerParams,
                ServerRequestAnswerResult,
                SESSION_NOT_FOUND,
                SERVER_REQUEST_NOT_FOUND,
                SERVER_REQUEST_UNSUPPORTED,
                ANSWER_KIND_MISMATCH,
            ),
            _spec("client.capabilities", ClientCapabilitiesParams, ClientCapabilitiesResult),
            *(
                _spec(name, params, result, *errors)
                for name, params, result, errors in BRIDGE_METHODS
            ),
            *(
                _spec(name, params, result, *errors)
                for name, params, result, errors in ATTACH_METHODS
            ),
            # P24: projects, pet, wake word, capabilities, bots, group rooms and voice
            *(
                _spec(name, params, result, *errors)
                for group in (
                    PROJECT_METHODS,
                    PET_METHODS,
                    WAKE_METHODS,
                    CAPABILITY_METHODS,
                    BOT_METHODS,
                    GROUP_METHODS,
                    VOICE_METHODS,
                )
                for name, params, result, errors in group
            ),
        )
    }
)
"""Method name → spec, in declaration order."""


NOTIFICATIONS: Mapping[str, type[ProtocolModel]] = MappingProxyType(
    {**BRIDGE_NOTIFICATIONS, **ATTACH_NOTIFICATIONS, WAKE_DETECTED_EVENT: WakeDetected}
)
"""Notifications that are neither events nor server requests (``gateway.ready`` and the daemon's
unnumbered frames): method → params model."""


class ProtocolError(Exception):
    """A call that must be answered with an error from the table."""

    def __init__(self, error: ErrorCode, message: str | None = None, *, path: str | None = None):
        super().__init__(message or error.meaning)
        self.error = error
        self.message = message or error.meaning
        self.path = path


def validate_params(method: str, params: Mapping[str, Any] | None) -> ProtocolModel:
    """Validate ``params`` for ``method``.

    Raises ``ProtocolError`` with ``METHOD_NOT_FOUND`` for an undeclared method, ``TEXT_EMPTY``
    for blank message text, ``UNKNOWN_PARAM_KEY`` for a key the model does not declare and
    ``INVALID_PARAMS`` for anything else; the last two carry the dotted key path.
    """
    spec = METHODS.get(method)
    if spec is None:
        raise ProtocolError(METHOD_NOT_FOUND, f"unknown method: {method}")
    try:
        return spec.params.model_validate(dict(params or {}))
    except ValidationError as exc:
        found = exc.errors()
        unknown = [e for e in found if e["type"] == "extra_forbidden"]
        first = unknown[0] if unknown else found[0]
        path = ".".join(str(part) for part in first["loc"]) or None
        if unknown:
            raise ProtocolError(
                UNKNOWN_PARAM_KEY, f"unknown key: {path or 'params'}", path=path
            ) from exc
        if first["type"] == TEXT_EMPTY_ERROR:
            raise ProtocolError(TEXT_EMPTY, path=path) from exc
        raise ProtocolError(
            INVALID_PARAMS, f"{path or 'params'}: {first['msg']}", path=path
        ) from exc


def negotiate_features(requested: list[str]) -> list[str]:
    """The requested features this server supports, sorted (unknown names are dropped)."""
    return sorted(set(requested) & KNOWN_CLIENT_FEATURES)
