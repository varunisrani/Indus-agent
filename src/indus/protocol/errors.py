"""The single protocol error-code table (D-16.27).

Every code has exactly one meaning across all methods, a stable symbolic name and an HTTP-like
status taken from its group. The five codes the JSON-RPC 2.0 specification defines keep their
numbers; our own codes use one positive band per group (1xxx invalid request, 2xxx not found,
3xxx conflict, 4xxx refused, 5xxx provider, 6xxx internal) so the group can be read from the
number. Codes are never renumbered or reused; a retired code stays in the table.

Two refused-band codes keep the number of the wire value they stand for: ``TICKET_INVALID`` (4401)
is also the WebSocket close code of a refused upgrade, ``NOT_SHOWN`` (4404) the code a viewer
answers a server request it does not show. Codes that never travel as a method's error answer are
listed in ``OUT_OF_BAND_ERRORS`` with where they travel instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from pydantic import JsonValue

from indus.protocol.envelope import ErrorDetail, ErrorObject


class ErrorGroup(StrEnum):
    """The family an error code belongs to; it decides the HTTP-like status."""

    INVALID_REQUEST = "invalid_request"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    REFUSED = "refused"
    PROVIDER = "provider"
    INTERNAL = "internal"


GROUP_STATUS: MappingProxyType[ErrorGroup, int] = MappingProxyType(
    {
        ErrorGroup.INVALID_REQUEST: 400,
        ErrorGroup.NOT_FOUND: 404,
        ErrorGroup.CONFLICT: 409,
        ErrorGroup.REFUSED: 403,
        ErrorGroup.PROVIDER: 502,
        ErrorGroup.INTERNAL: 500,
    }
)


@dataclass(frozen=True, slots=True)
class ErrorCode:
    """One row of the table."""

    code: int
    name: str
    group: ErrorGroup
    meaning: str

    @property
    def status(self) -> int:
        """The HTTP-like status of this code's group."""
        return GROUP_STATUS[self.group]


def _row(code: int, name: str, group: ErrorGroup, meaning: str) -> ErrorCode:
    return ErrorCode(code=code, name=name, group=group, meaning=meaning)


_G = ErrorGroup

PARSE_ERROR = _row(-32700, "PARSE_ERROR", _G.INVALID_REQUEST, "the frame is not valid JSON")
INVALID_REQUEST = _row(
    -32600, "INVALID_REQUEST", _G.INVALID_REQUEST, "the JSON value is not a valid request frame"
)
METHOD_NOT_FOUND = _row(
    -32601, "METHOD_NOT_FOUND", _G.NOT_FOUND, "no method with this name is declared"
)
INVALID_PARAMS = _row(
    -32602,
    "INVALID_PARAMS",
    _G.INVALID_REQUEST,
    "the params do not match the method's model (unknown key, wrong type, missing field)",
)
INTERNAL_ERROR = _row(
    -32603, "INTERNAL_ERROR", _G.INTERNAL, "the server failed while handling a valid request"
)
TEXT_EMPTY = _row(
    1001, "TEXT_EMPTY", _G.INVALID_REQUEST, "the message text is empty or only whitespace"
)
UNKNOWN_PARAM_KEY = _row(
    1002,
    "UNKNOWN_PARAM_KEY",
    _G.INVALID_REQUEST,
    "the params carry a key the method does not declare (the path names it)",
)
SESSION_NOT_FOUND = _row(2001, "SESSION_NOT_FOUND", _G.NOT_FOUND, "no session has this id")
SERVER_REQUEST_NOT_FOUND = _row(
    2002,
    "SERVER_REQUEST_NOT_FOUND",
    _G.NOT_FOUND,
    "no open server request has this id (never sent, already answered or withdrawn)",
)
SLASH_NOT_OWNED = _row(
    2003,
    "SLASH_NOT_OWNED",
    _G.NOT_FOUND,
    "the server does not run this slash command; the client handles it, or a later release does",
)
UNSUPPORTED_COMMAND = _row(
    2004,
    "UNSUPPORTED_COMMAND",
    _G.NOT_FOUND,
    "the name is not a built-in, quick or skill command",
)
ADMISSION_NOT_FOUND = _row(
    2005,
    "ADMISSION_NOT_FOUND",
    _G.NOT_FOUND,
    "no input of this session has this admission id",
)
SESSION_BUSY = _row(
    3001,
    "SESSION_BUSY",
    _G.CONFLICT,
    "the session is running a turn or is held by another surface",
)
IDEMPOTENCY_CONFLICT = _row(
    3002,
    "IDEMPOTENCY_CONFLICT",
    _G.CONFLICT,
    "the idempotency key was already used with different parameters",
)
ANSWER_KIND_MISMATCH = _row(
    3003,
    "ANSWER_KIND_MISMATCH",
    _G.CONFLICT,
    "the answer's kind differs from the kind of the server request it answers",
)
CONFIRM_REQUIRED = _row(
    3004,
    "CONFIRM_REQUIRED",
    _G.CONFLICT,
    "the change needs an explicit confirmation; repeat the call with confirm set",
)
STALE_GENERATION = _row(
    3005,
    "STALE_GENERATION",
    _G.CONFLICT,
    "the call names a turn generation that is not the one it targets now; nothing changed",
)
ADMISSION_CONFLICT = _row(
    3006,
    "ADMISSION_CONFLICT",
    _G.CONFLICT,
    "the input's request id was already used with a different input",
)
QUEUE_FULL = _row(
    3007,
    "QUEUE_FULL",
    _G.CONFLICT,
    "the session already holds as many queued inputs as it takes; send again once it drains",
)
OUTCOME_UNKNOWN = _row(
    3008,
    "OUTCOME_UNKNOWN",
    _G.CONFLICT,
    "the turn's outcome is unknown (its daemon stopped or the connection dropped while it ran); "
    "it is never run again: do not resend, resolve it with admission.discard",
)
POLICY_CONFLICT = _row(
    3009,
    "POLICY_CONFLICT",
    _G.CONFLICT,
    "the launch settings differ from the ones frozen into the session (the fields are named)",
)
LEASE_LOST = _row(
    3010,
    "LEASE_LOST",
    _G.CONFLICT,
    "another process took over the session's turn lease; the turn was stopped",
)
SESSION_REFUSED = _row(
    4001,
    "SESSION_REFUSED",
    _G.REFUSED,
    "the session cannot be used: it was deleted or continued in another session",
)
SERVER_REQUEST_UNSUPPORTED = _row(
    4002,
    "SERVER_REQUEST_UNSUPPORTED",
    _G.REFUSED,
    "the client cannot answer server requests of this kind",
)
SHELL_REFUSED = _row(
    4003,
    "SHELL_REFUSED",
    _G.REFUSED,
    "the shell command is blocked by the dangerous-command rules and was not run",
)
PROFILE_MISMATCH = _row(
    4005,
    "PROFILE_MISMATCH",
    _G.REFUSED,
    "the daemon that answered serves another profile; the client stops before sending anything",
)
TICKET_INVALID = _row(
    4401,
    "TICKET_INVALID",
    _G.REFUSED,
    "the attach ticket is missing, malformed, already used, expired or bound to another purpose",
)
NOT_SHOWN = _row(
    4404,
    "NOT_SHOWN",
    _G.REFUSED,
    "a viewer does not show this server request here; the other viewers may still answer it",
)
PROVIDER_UNAVAILABLE = _row(
    5001,
    "PROVIDER_UNAVAILABLE",
    _G.PROVIDER,
    "no usable model provider could be resolved for the session",
)
STORE_FAILED = _row(
    6001, "STORE_FAILED", _G.INTERNAL, "the session store could not read or write the session"
)
SHELL_TIMEOUT = _row(
    6002,
    "SHELL_TIMEOUT",
    _G.INTERNAL,
    "the shell command did not finish within its time limit and was stopped",
)
SLASH_WORKER_FAILED = _row(
    6003,
    "SLASH_WORKER_FAILED",
    _G.INTERNAL,
    "the slash command's handler failed or did not answer in time",
)

ERROR_TABLE: tuple[ErrorCode, ...] = (
    PARSE_ERROR,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    INVALID_PARAMS,
    INTERNAL_ERROR,
    TEXT_EMPTY,
    UNKNOWN_PARAM_KEY,
    SESSION_NOT_FOUND,
    SERVER_REQUEST_NOT_FOUND,
    SLASH_NOT_OWNED,
    UNSUPPORTED_COMMAND,
    ADMISSION_NOT_FOUND,
    SESSION_BUSY,
    IDEMPOTENCY_CONFLICT,
    ANSWER_KIND_MISMATCH,
    CONFIRM_REQUIRED,
    STALE_GENERATION,
    ADMISSION_CONFLICT,
    QUEUE_FULL,
    OUTCOME_UNKNOWN,
    POLICY_CONFLICT,
    LEASE_LOST,
    SESSION_REFUSED,
    SERVER_REQUEST_UNSUPPORTED,
    SHELL_REFUSED,
    PROFILE_MISMATCH,
    TICKET_INVALID,
    NOT_SHOWN,
    PROVIDER_UNAVAILABLE,
    STORE_FAILED,
    SHELL_TIMEOUT,
    SLASH_WORKER_FAILED,
)
"""Every code, in table order."""

BY_CODE: MappingProxyType[int, ErrorCode] = MappingProxyType({e.code: e for e in ERROR_TABLE})
BY_NAME: MappingProxyType[str, ErrorCode] = MappingProxyType({e.name: e for e in ERROR_TABLE})

# Codes every method can return: any frame can be malformed and any handler can crash.
COMMON_ERRORS: tuple[ErrorCode, ...] = (
    PARSE_ERROR,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    INVALID_PARAMS,
    INTERNAL_ERROR,
    UNKNOWN_PARAM_KEY,
)

OUT_OF_BAND_ERRORS: MappingProxyType[ErrorCode, str] = MappingProxyType(
    {
        STALE_GENERATION: "a control's answer that nothing changed (the daemon's controls), the "
        "reason in admission.discard's result, and product code runtime.stale_generation",
        PROFILE_MISMATCH: "the client's own refusal of a describe or gateway.ready naming another "
        "profile (the WebSocket close code it leaves with), and the control socket's refusal",
        TICKET_INVALID: "the WebSocket close code of a refused upgrade (missing, used, expired or "
        "wrong-purpose ticket)",
        NOT_SHOWN: "a viewer's error answer to a server request it does not show (only from a "
        "viewer that advertised request.decline)",
    }
)
"""Codes that travel outside a method's error answer, with where each one travels."""

# Product error codes (``indus.foundation.errors.ERROR_CODES``) with a specific protocol code.
_INDUS_EXACT: MappingProxyType[str, ErrorCode] = MappingProxyType(
    {
        "store.session_not_found": SESSION_NOT_FOUND,
        "store.tombstoned": SESSION_REFUSED,
        "store.compression_closed": SESSION_REFUSED,
        "store.session_guarded": SESSION_BUSY,
        "store.turn_lease_lost": LEASE_LOST,
        "session.busy": SESSION_BUSY,
        "runtime.stale_generation": STALE_GENERATION,
        "runtime.admission_conflict": ADMISSION_CONFLICT,
        "runtime.queue_full": QUEUE_FULL,
        "runtime.unknown_execution": OUTCOME_UNKNOWN,
        "runtime.policy_conflict": POLICY_CONFLICT,
        "runtime.policy_refused": INVALID_PARAMS,
        "runtime.invalid_params": INVALID_PARAMS,
        "runtime.retired": SESSION_REFUSED,
        "runtime.not_found": ADMISSION_NOT_FOUND,
        "runtime.admission_started": SESSION_BUSY,
        "runtime.stale_epoch": SESSION_BUSY,
    }
)
# Fallbacks by code family (the part before the first dot).
_INDUS_FAMILY: MappingProxyType[str, ErrorCode] = MappingProxyType(
    {
        "store": STORE_FAILED,
        "provider": PROVIDER_UNAVAILABLE,
    }
)


def error_for_indus_code(indus_code: str) -> ErrorCode:
    """Map a product error code onto the protocol table (unknown codes are internal errors)."""
    exact = _INDUS_EXACT.get(indus_code)
    if exact is not None:
        return exact
    return _INDUS_FAMILY.get(indus_code.partition(".")[0], INTERNAL_ERROR)


def make_error(
    error: ErrorCode,
    message: str | None = None,
    *,
    path: str | None = None,
    indus_code: str | None = None,
    retryable: bool | None = None,
) -> ErrorObject:
    """Build the ``error`` member for ``error``; the message defaults to the table's meaning."""
    detail = ErrorDetail(name=error.name, path=path, indus_code=indus_code, retryable=retryable)
    data: JsonValue = detail.to_wire()
    return ErrorObject(code=error.code, message=message or error.meaning, data=data)
