"""Method params and results, and the method catalogue.

Each method has a params model, a result model and the error codes it may return. Params refuse
unknown keys, so a misspelt option is an ``INVALID_PARAMS`` error that names the key.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Any

from pydantic import AfterValidator, Field, NonNegativeInt, ValidationError
from pydantic_core import PydanticCustomError

from indus.protocol.base import ProtocolModel
from indus.protocol.errors import (
    ANSWER_KIND_MISMATCH,
    COMMON_ERRORS,
    IDEMPOTENCY_CONFLICT,
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    PROVIDER_UNAVAILABLE,
    SERVER_REQUEST_NOT_FOUND,
    SERVER_REQUEST_UNSUPPORTED,
    SESSION_BUSY,
    SESSION_NOT_FOUND,
    SESSION_REFUSED,
    STORE_FAILED,
    TEXT_EMPTY,
    ErrorCode,
)
from indus.protocol.server_requests import ServerRequest, ServerRequestAnswer

PROTOCOL_VERSION = 1
"""Bumped when a change breaks existing clients."""

TEXT_EMPTY_ERROR = "text_empty"
"""The pydantic error type raised for blank message text."""


def _not_blank(value: str) -> str:
    if not value.strip():
        raise PydanticCustomError(TEXT_EMPTY_ERROR, "text must not be empty")
    return value


type MessageText = Annotated[str, AfterValidator(_not_blank)]
"""Message text: any string with at least one non-whitespace character."""

type SessionId = Annotated[str, Field(min_length=1)]
"""A session id."""


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


class PromptSubmitParams(ProtocolModel):
    """Send a user message and start a turn."""

    session_id: SessionId
    """The session to run the turn in."""
    text: MessageText
    """The user's message."""


class PromptSubmitResult(ProtocolModel):
    """The ticket for the started turn."""

    turn_id: str
    """The turn's id; its events carry it."""
    generation: NonNegativeInt
    """The turn's generation, for interrupts."""


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


class ServerRequestAnswerResult(ProtocolModel):
    """Confirmation that the answer was delivered."""

    accepted: bool
    """True when the waiting agent received the answer."""


class ClientCapabilitiesParams(ProtocolModel):
    """Tell the server which optional features this client supports."""

    features: list[str] = Field(default_factory=list[str])
    """Feature names; names the server does not know are ignored."""


class ClientCapabilitiesResult(ProtocolModel):
    """The features both sides support (none are defined yet)."""

    features: list[str]
    """The enabled feature names."""


KNOWN_CLIENT_FEATURES: frozenset[str] = frozenset()
"""Features a client may turn on; none exist yet (D-16 §2.3)."""


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
            ),
            _spec(
                "session.resume",
                SessionResumeParams,
                SessionResumeResult,
                *_SESSION_ERRORS,
                SESSION_BUSY,
            ),
            _spec(
                "prompt.submit",
                PromptSubmitParams,
                PromptSubmitResult,
                *_SESSION_ERRORS,
                TEXT_EMPTY,
                SESSION_BUSY,
                PROVIDER_UNAVAILABLE,
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
        )
    }
)
"""Method name → spec, in declaration order."""


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
    for blank message text and ``INVALID_PARAMS`` (with the dotted key path) for anything else.
    """
    spec = METHODS.get(method)
    if spec is None:
        raise ProtocolError(METHOD_NOT_FOUND, f"unknown method: {method}")
    try:
        return spec.params.model_validate(dict(params or {}))
    except ValidationError as exc:
        first = exc.errors()[0]
        path = ".".join(str(part) for part in first["loc"]) or None
        if first["type"] == TEXT_EMPTY_ERROR:
            raise ProtocolError(TEXT_EMPTY, path=path) from exc
        raise ProtocolError(
            INVALID_PARAMS, f"{path or 'params'}: {first['msg']}", path=path
        ) from exc


def negotiate_features(requested: list[str]) -> list[str]:
    """The requested features this server supports, sorted (unknown names are dropped)."""
    return sorted(set(requested) & KNOWN_CLIENT_FEATURES)
