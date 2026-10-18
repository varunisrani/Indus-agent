"""The single protocol error-code table (D-16.27).

Every code has exactly one meaning across all methods, a stable symbolic name and an HTTP-like
status taken from its group. The five codes the JSON-RPC 2.0 specification defines keep their
numbers; our own codes use one positive band per group (1xxx invalid request, 2xxx not found,
3xxx conflict, 4xxx refused, 5xxx provider, 6xxx internal) so the group can be read from the
number. Codes are never renumbered or reused; a retired code stays in the table.
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
SESSION_NOT_FOUND = _row(2001, "SESSION_NOT_FOUND", _G.NOT_FOUND, "no session has this id")
SERVER_REQUEST_NOT_FOUND = _row(
    2002,
    "SERVER_REQUEST_NOT_FOUND",
    _G.NOT_FOUND,
    "no open server request has this id (never sent, already answered or withdrawn)",
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
PROVIDER_UNAVAILABLE = _row(
    5001,
    "PROVIDER_UNAVAILABLE",
    _G.PROVIDER,
    "no usable model provider could be resolved for the session",
)
STORE_FAILED = _row(
    6001, "STORE_FAILED", _G.INTERNAL, "the session store could not read or write the session"
)

ERROR_TABLE: tuple[ErrorCode, ...] = (
    PARSE_ERROR,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    INVALID_PARAMS,
    INTERNAL_ERROR,
    TEXT_EMPTY,
    SESSION_NOT_FOUND,
    SERVER_REQUEST_NOT_FOUND,
    SESSION_BUSY,
    IDEMPOTENCY_CONFLICT,
    ANSWER_KIND_MISMATCH,
    SESSION_REFUSED,
    SERVER_REQUEST_UNSUPPORTED,
    PROVIDER_UNAVAILABLE,
    STORE_FAILED,
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
)

# Product error codes (``indus.foundation.errors.ERROR_CODES``) with a specific protocol code.
_INDUS_EXACT: MappingProxyType[str, ErrorCode] = MappingProxyType(
    {
        "store.session_not_found": SESSION_NOT_FOUND,
        "store.tombstoned": SESSION_REFUSED,
        "store.compression_closed": SESSION_REFUSED,
        "store.session_guarded": SESSION_BUSY,
        "store.turn_lease_lost": SESSION_BUSY,
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
