"""Questions the agent asks a person through the client, and their answers.

A server request reaches the client as a ``server_request`` notification carrying one of the
request models below; the client answers with the ``server_request.answer`` method. Requests and
answers share a ``kind`` so a mismatched answer is refused. A withdrawn request is announced with
a ``server_request.cancelled`` event. Secret values (passwords, keys) travel as plain strings so
the models round-trip; transports and logs must never print them.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, PositiveFloat, model_validator

from indus.protocol.base import ProtocolModel

SERVER_REQUEST_METHOD = "server_request"
"""The notification method that carries a server request."""


class ApprovalChoice(StrEnum):
    """How a person answered an approval prompt."""

    ONCE = "once"
    SESSION = "session"
    ALWAYS = "always"
    DENY = "deny"


class ServerRequestBase(ProtocolModel):
    """Fields every server request carries."""

    request_id: str = Field(min_length=1)
    """Unique id; the answer and any cancellation name it."""
    session_id: str = Field(min_length=1)
    """The session asking."""
    turn_id: str | None = None
    """The turn waiting on the answer, if any."""
    deadline_s: PositiveFloat | None = None
    """Seconds the agent waits before withdrawing the request; absent means no deadline."""


class ApprovalRequest(ServerRequestBase):
    """Ask whether a risky action (usually a shell command) may run."""

    kind: Literal["approval"] = "approval"
    """Request kind."""
    command: str
    """The action to approve, already redacted for display."""
    description: str
    """Why the action needs approval."""
    choices: list[ApprovalChoice] = Field(
        default_factory=lambda: list(ApprovalChoice), min_length=1
    )
    """The answers the person may pick, in display order; ``deny`` is always allowed."""

    @model_validator(mode="after")
    def _deny_offered(self) -> ApprovalRequest:
        if ApprovalChoice.DENY not in self.choices:
            raise ValueError("choices must include deny")
        if len(set(self.choices)) != len(self.choices):
            raise ValueError("choices must not repeat")
        return self


class ApprovalAnswer(ProtocolModel):
    """The person's decision on an approval request."""

    kind: Literal["approval"] = "approval"
    """Answer kind."""
    choice: ApprovalChoice
    """The picked choice."""


class ClarifyQuestion(ProtocolModel):
    """One question inside a clarify request."""

    id: str = Field(min_length=1)
    """Question id, unique within the request."""
    text: str
    """The question."""
    choices: list[str] | None = None
    """Offered answers; absent means free text only."""
    multi_select: bool = False
    """Whether more than one choice may be picked."""


class ClarifyRequest(ServerRequestBase):
    """Ask the person one or more questions before the agent goes on."""

    kind: Literal["clarify"] = "clarify"
    """Request kind."""
    questions: list[ClarifyQuestion] = Field(min_length=1)
    """The questions, in display order."""

    @model_validator(mode="after")
    def _unique_ids(self) -> ClarifyRequest:
        ids = [q.id for q in self.questions]
        if len(set(ids)) != len(ids):
            raise ValueError("question ids must be unique")
        return self


class ClarifyAnswer(ProtocolModel):
    """The person's answers to a clarify request."""

    kind: Literal["clarify"] = "clarify"
    """Answer kind."""
    answers: dict[str, list[str]]
    """Question id → picked choices or typed text; an empty list means the question was skipped."""


class SudoRequest(ServerRequestBase):
    """Ask for the administrator password a command needs."""

    kind: Literal["sudo"] = "sudo"
    """Request kind."""
    reason: str
    """What the password is for."""


class SudoAnswer(ProtocolModel):
    """The password, or nothing when the person cancelled."""

    kind: Literal["sudo"] = "sudo"
    """Answer kind."""
    password: str | None = None
    """The password; absent when cancelled."""


class SecretRequest(ServerRequestBase):
    """Ask for a secret (an API key or token) to store under a variable name."""

    kind: Literal["secret"] = "secret"
    """Request kind."""
    name: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    """The variable the value is stored as."""
    prompt: str
    """What to tell the person about the secret."""


class SecretAnswer(ProtocolModel):
    """The secret value, or nothing when the person skipped it."""

    kind: Literal["secret"] = "secret"
    """Answer kind."""
    value: str | None = None
    """The value; absent or empty when skipped."""


type ServerRequest = Annotated[
    ApprovalRequest | ClarifyRequest | SudoRequest | SecretRequest,
    Field(discriminator="kind"),
]
"""Any server request, told apart by ``kind``."""

type ServerRequestAnswer = Annotated[
    ApprovalAnswer | ClarifyAnswer | SudoAnswer | SecretAnswer,
    Field(discriminator="kind"),
]
"""Any answer, told apart by ``kind``."""

SERVER_REQUEST_KINDS: dict[str, tuple[type[ServerRequestBase], type[ProtocolModel]]] = {
    "approval": (ApprovalRequest, ApprovalAnswer),
    "clarify": (ClarifyRequest, ClarifyAnswer),
    "sudo": (SudoRequest, SudoAnswer),
    "secret": (SecretRequest, SecretAnswer),
}
"""Kind → (request model, answer model)."""
