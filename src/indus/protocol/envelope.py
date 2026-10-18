"""JSON-RPC 2.0 framing.

Shapes follow the JSON-RPC 2.0 specification: a request carries ``id``, ``method`` and optional
``params``; a notification is a request without ``id``; a response carries exactly one of
``result`` and ``error`` and echoes the request's ``id`` (``null`` only when the request could
not be read). Our methods take parameters by name, so ``params`` is always an object.
"""

from __future__ import annotations

from typing import Any, Literal, cast

from pydantic import Field, JsonValue, StrictInt, StrictStr, ValidationError

from indus.protocol.base import ProtocolModel

JSONRPC_VERSION = "2.0"

type RequestId = StrictInt | StrictStr
"""A request id: a string or an integer (floats and booleans are refused)."""


class ErrorObject(ProtocolModel):
    """The ``error`` member of a failed response."""

    code: StrictInt
    """A number from the protocol error table."""
    message: str
    """A short, human-readable description of the error."""
    data: JsonValue = None
    """Extra machine-readable detail; see ``ErrorDetail`` for the keys we send."""


class ErrorDetail(ProtocolModel):
    """What we put in ``error.data``: the symbolic name plus optional context."""

    name: str
    """The symbolic name of the code, e.g. ``SESSION_NOT_FOUND``."""
    path: str | None = None
    """For invalid params: the dotted path of the offending key."""
    indus_code: str | None = None
    """The product error code behind this error, when there is one."""
    retryable: bool | None = None
    """Whether repeating the same call later may succeed."""


class Request(ProtocolModel):
    """A call that expects a response."""

    jsonrpc: Literal["2.0"] = JSONRPC_VERSION
    """Always the string ``2.0``."""
    id: RequestId
    """Chosen by the caller and echoed in the response."""
    method: str = Field(min_length=1)
    """The method name, e.g. ``prompt.submit``."""
    params: dict[str, JsonValue] | None = None
    """Named parameters; omitted when the method takes none."""


class Notification(ProtocolModel):
    """A one-way message: no ``id`` and never answered (events, server requests)."""

    jsonrpc: Literal["2.0"] = JSONRPC_VERSION
    """Always the string ``2.0``."""
    method: str = Field(min_length=1)
    """The event type or ``server_request``."""
    params: dict[str, JsonValue] | None = None
    """The payload as named fields."""


class SuccessResponse(ProtocolModel):
    """The answer to a request that worked."""

    jsonrpc: Literal["2.0"] = JSONRPC_VERSION
    """Always the string ``2.0``."""
    id: RequestId
    """The id of the request this answers."""
    result: JsonValue
    """The method's result; ``null`` is a valid result and stays on the wire."""

    def to_wire(self) -> dict[str, Any]:
        """Return the JSON-ready form, keeping a ``null`` result."""
        return self.model_dump(mode="json")


class ErrorResponse(ProtocolModel):
    """The answer to a request that failed, or to a frame that could not be read."""

    jsonrpc: Literal["2.0"] = JSONRPC_VERSION
    """Always the string ``2.0``."""
    id: RequestId | None
    """The id of the failed request; ``null`` when it could not be determined."""
    error: ErrorObject
    """What went wrong."""

    def to_wire(self) -> dict[str, Any]:
        """Return the JSON-ready form, keeping a ``null`` id."""
        body = self.model_dump(mode="json", exclude_none=True)
        body["id"] = self.id
        return body


type Frame = Request | Notification | SuccessResponse | ErrorResponse


class FrameError(ValueError):
    """A JSON value that is not a valid JSON-RPC 2.0 frame."""


def parse_frame(value: object) -> Frame:
    """Classify and validate one decoded JSON value as a frame.

    Raises ``FrameError`` for anything that is not an object, a batch (we do not use batches) or
    an object that fails its frame model.
    """
    if not isinstance(value, dict):
        raise FrameError("a frame must be a JSON object")
    frame = cast(dict[str, object], value)
    keys = set(frame)
    if frame.get("jsonrpc") != JSONRPC_VERSION:
        raise FrameError('a frame must carry "jsonrpc": "2.0"')
    model: type[Frame]
    if "method" in keys:
        model = Request if "id" in keys else Notification
    elif "error" in keys and "result" in keys:
        raise FrameError("a response carries exactly one of result and error")
    elif "error" in keys:
        model = ErrorResponse
    elif "result" in keys:
        model = SuccessResponse
    else:
        raise FrameError("a frame needs method, result or error")
    try:
        return model.model_validate(frame)
    except ValidationError as exc:
        raise FrameError(str(exc)) from exc
