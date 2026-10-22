"""Questions the agent asks a person through the client, and their answers.

A server request reaches the client as a ``server_request`` notification carrying one of the
request models below; the client answers with the ``server_request.answer`` method. Requests and
answers share a ``kind`` so a mismatched answer is refused. A withdrawn request is announced with
a ``server_request.cancelled`` event. Secret values (passwords, keys) travel as plain strings so
the models round-trip; transports and logs must never print them.

Two kinds are asked of a window rather than a person (P24-25): ``preview.act`` and
``preview.read`` drive the page in a session's desktop preview pane. Only the window that shows
that session's preview answers them; any other window declines (``4404`` not shown). They are
listed in ``PREVIEW_REQUEST_KINDS``; ``PERSON_REQUEST_KINDS`` are the ones a person answers.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, JsonValue, PositiveFloat, model_validator

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
    reason: str | None = None
    """Why the person denied, in their words; passed to the model unchanged."""


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


class PreviewPin(ProtocolModel):
    """One note to put on a page element."""

    selector: str = Field(min_length=1, max_length=500)
    """A CSS selector on the page."""
    note: str = Field(min_length=1, max_length=500)
    """What the note says."""


class PreviewActRequest(ServerRequestBase):
    """Act on the page in the session's desktop preview pane (asked of the window showing it)."""

    kind: Literal["preview.act"] = "preview.act"
    """Request kind."""
    action: Literal["navigate", "click", "type", "scroll", "annotate"]
    """What to do."""
    url: str | None = None
    """For navigate: an http(s) or ``file:///`` address."""
    selector: str | None = Field(default=None, max_length=500)
    """For click, type and scroll: a CSS selector on the page."""
    text: str | None = Field(default=None, max_length=4_000)
    """For type: the text to enter."""
    submit: bool | None = None
    """For type: press Enter after the text."""
    dx: int | None = None
    """For scroll without a selector: pixels to the right."""
    dy: int | None = None
    """For scroll without a selector: pixels down."""
    pins: list[PreviewPin] | None = Field(default=None, max_length=20)
    """For annotate: the notes, in order."""


class PreviewReadRequest(ServerRequestBase):
    """Read the page in the session's desktop preview pane (asked of the window showing it)."""

    kind: Literal["preview.read"] = "preview.read"
    """Request kind."""
    what: Literal["snapshot"] = "snapshot"
    """What to read: the title, address, visible text and interactive elements."""


class PreviewActAnswer(ProtocolModel):
    """What the page did."""

    kind: Literal["preview.act"] = "preview.act"
    """Answer kind."""
    ok: bool
    """Whether the action ran."""
    error: str | None = None
    """Why it did not, in a few words."""
    result: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])
    """What the page reported (for navigate the address); secret values are never included."""


class PreviewReadAnswer(ProtocolModel):
    """What the page shows."""

    kind: Literal["preview.read"] = "preview.read"
    """Answer kind."""
    ok: bool
    """Whether the page could be read."""
    error: str | None = None
    """Why it could not, in a few words."""
    result: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])
    """The snapshot: title, address, visible text, interactive elements (secrets redacted)."""


type ServerRequest = Annotated[
    ApprovalRequest
    | ClarifyRequest
    | SudoRequest
    | SecretRequest
    | PreviewActRequest
    | PreviewReadRequest,
    Field(discriminator="kind"),
]
"""Any server request, told apart by ``kind``."""

type ServerRequestAnswer = Annotated[
    ApprovalAnswer
    | ClarifyAnswer
    | SudoAnswer
    | SecretAnswer
    | PreviewActAnswer
    | PreviewReadAnswer,
    Field(discriminator="kind"),
]
"""Any answer, told apart by ``kind``."""

SERVER_REQUEST_KINDS: dict[str, tuple[type[ServerRequestBase], type[ProtocolModel]]] = {
    "approval": (ApprovalRequest, ApprovalAnswer),
    "clarify": (ClarifyRequest, ClarifyAnswer),
    "sudo": (SudoRequest, SudoAnswer),
    "secret": (SecretRequest, SecretAnswer),
    "preview.act": (PreviewActRequest, PreviewActAnswer),
    "preview.read": (PreviewReadRequest, PreviewReadAnswer),
}
"""Kind → (request model, answer model)."""

PREVIEW_REQUEST_KINDS: frozenset[str] = frozenset({"preview.act", "preview.read"})
"""Kinds a desktop window answers for the session preview it shows (never a person)."""

PERSON_REQUEST_KINDS: tuple[str, ...] = tuple(
    kind for kind in SERVER_REQUEST_KINDS if kind not in PREVIEW_REQUEST_KINDS
)
"""Kinds a person answers (cards, prompts): what a terminal or editor surface can render."""
