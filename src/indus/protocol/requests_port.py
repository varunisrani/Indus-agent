"""The port through which agent code asks a person something: the server-request broker's face.

The broker itself lives in ``indus.service.broker``; the turn loop (``core``) and tools that need
an approval or an answer sit below ``service`` in the layer order, so they depend on this small
typing ``Protocol`` and on the outcome errors defined here instead.

An ask ends in one of three ways: the answer model comes back; ``RequestUnsupported`` is raised
at once because the client cannot answer that kind (nothing was sent); or ``RequestWithdrawn`` is
raised because the request timed out, the turn was interrupted or ended, or the session closed.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from indus.protocol.envelope import ErrorObject
from indus.protocol.events import CancelReason
from indus.protocol.server_requests import (
    ApprovalAnswer,
    ClarifyAnswer,
    PreviewActAnswer,
    PreviewReadAnswer,
    SecretAnswer,
    SudoAnswer,
)

__all__ = [
    "NOT_SHOWN_INDUS_CODE",
    "AnswerModel",
    "RequestNotAnswered",
    "RequestUnsupported",
    "RequestWithdrawn",
    "ServerRequestPort",
]

NOT_SHOWN_INDUS_CODE = "server_request.not_shown"
"""``error.data.indus_code`` of a ``RequestUnsupported`` whose request every attached viewer
declined as not shown there (a desktop window that does not show the session's preview, P24-25)."""

type AnswerModel = (
    ApprovalAnswer
    | ClarifyAnswer
    | SudoAnswer
    | SecretAnswer
    | PreviewActAnswer
    | PreviewReadAnswer
)
"""What a successful ask returns: the answer model of the asked kind."""


class RequestNotAnswered(Exception):
    """An ask that ended without an answer."""


class RequestWithdrawn(RequestNotAnswered):
    """The request was withdrawn before anyone answered it."""

    def __init__(self, request_id: str, reason: CancelReason) -> None:
        super().__init__(f"server request {request_id} was withdrawn: {reason.value}")
        self.request_id = request_id
        self.reason = reason


class RequestUnsupported(RequestNotAnswered):
    """The client cannot answer requests of this kind; nothing was sent (R05.3)."""

    def __init__(self, kind: str, error: ErrorObject) -> None:
        super().__init__(error.message)
        self.kind = kind
        self.error = error


class ServerRequestPort(Protocol):
    """What the loop and tools may do with the session's server-request broker."""

    async def ask(
        self,
        kind: str,
        payload: Mapping[str, Any],
        *,
        timeout: float | None = None,
        turn_id: str | None = None,
    ) -> AnswerModel:
        """Send a ``kind`` request built from ``payload`` and wait for its answer."""
        ...

    def cancel_all(self, reason: CancelReason) -> int:
        """Withdraw every open request (one cancelled event each); returns how many."""
        ...

    def human_wait_seconds(self) -> float:
        """Total time so far with at least one request waiting on a person."""
        ...
