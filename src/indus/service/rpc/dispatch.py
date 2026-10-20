"""The one JSON-RPC dispatcher every transport uses (P17-12).

The stdio bridge (``tui_bridge``, the TUI child and ``--local``) and the daemon's WebSocket
endpoint (P17-13) answer requests through this module, so every transport refuses the same
things in the same way. It sits in ``service`` because the gateway may not import ``tui_bridge``.

Declaring handlers: a ``MethodTable`` maps method names declared in ``indus.protocol`` to
``handler(context, params model) -> result model``. ``MethodTable.add`` refuses any other name,
and ``MethodSet.method(name)`` refuses it while the handler module is being imported, so a module
that serves an undeclared method cannot even be loaded.

``Dispatcher.handle(request)`` answers one request frame:

1. an undeclared method, or a declared one this table does not serve → ``-32601``;
2. params validated against the method's model: an unknown key is ``UNKNOWN_PARAM_KEY`` with the
   dotted key path, blank text ``TEXT_EMPTY``, anything else ``-32602`` with the path;
3. the handler runs under the context's scope (R05.14) within its deadline (``DEFAULT_DEADLINE_S``
   unless ``DEADLINES`` names the method); a ``ProtocolError`` becomes its error, a product
   ``IndusError`` is mapped through the protocol table, a missed deadline and any other crash are
   logged with the method name only and answered ``-32603``; the caller's loop goes on;
4. the result is checked against the method's result model: a mismatch raises
   ``ContractViolation`` in strict mode and is logged otherwise. Transports check event payloads
   the same way with ``check_event``.

Strict mode is the test flag ``INDUS_PROTOCOL_STRICT`` (``strict_from``). Params are never
written to a log: they may hold a password or a secret answer.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from types import MappingProxyType
from typing import Any, Protocol, cast

from pydantic import ValidationError

from indus.foundation.errors import IndusError
from indus.foundation.scope import Scope, use_scope
from indus.protocol.base import ProtocolModel
from indus.protocol.envelope import (
    ErrorObject,
    ErrorResponse,
    FrameError,
    Request,
    SuccessResponse,
    parse_frame,
)
from indus.protocol.errors import (
    INTERNAL_ERROR,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    make_error,
)
from indus.protocol.events import EVENTS
from indus.protocol.methods import METHODS, ProtocolError, validate_params
from indus.service.session_service import protocol_error_for

__all__ = [
    "DEADLINES",
    "DEFAULT_DEADLINE_S",
    "STRICT_ENV",
    "ContractViolation",
    "Dispatcher",
    "Handler",
    "MethodSet",
    "MethodTable",
    "RpcContext",
    "UndeclaredMethod",
    "check_event",
    "check_payload",
    "decode_request",
    "declared",
    "error_frame",
    "strict_from",
]

log = logging.getLogger(__name__)

STRICT_ENV = "INDUS_PROTOCOL_STRICT"
DEFAULT_DEADLINE_S = 120.0
DEADLINES: Mapping[str, float] = MappingProxyType(
    {
        # A submit may wait for the session's turn lease before it is admitted.
        "prompt.submit": 1800.0,
        "prompt.background": 1800.0,
        # Compression is a model call; the client gives up at 660 s.
        "session.compress": 600.0,
        # A change may compress or start an editor's MCP servers (session.mutate).
        "session.mutate": 600.0,
    }
)
"""Per-method deadlines (seconds) that differ from ``DEFAULT_DEADLINE_S``."""


class ContractViolation(AssertionError):
    """A result or event that does not match its declared model (raised in strict mode only)."""


class UndeclaredMethod(ValueError):
    """A handler for a method name ``indus.protocol`` does not declare."""


class RpcContext(Protocol):
    """What the dispatcher needs from a transport's per-connection state."""

    @property
    def scope(self) -> Scope: ...

    @property
    def strict(self) -> bool: ...


type Handler[C] = Callable[[C, Any], Awaitable[ProtocolModel]]
"""``handler(context, params model) -> result model``."""


def declared(name: str) -> str:
    """``name`` when ``indus.protocol`` declares it; ``UndeclaredMethod`` otherwise."""
    if name not in METHODS:
        raise UndeclaredMethod(f"{name!r} is not declared in indus.protocol")
    return name


class MethodTable[C]:
    """Declared method name → handler."""

    def __init__(self) -> None:
        self._handlers: dict[str, Handler[C]] = {}

    def add(self, name: str, handler: Handler[C]) -> None:
        declared(name)
        if name in self._handlers:
            raise ValueError(f"{name!r} has a handler already")
        self._handlers[name] = handler

    def get(self, name: str) -> Handler[C] | None:
        return self._handlers.get(name)

    def names(self) -> list[str]:
        return list(self._handlers)


class MethodSet[C]:
    """Handlers a module collects with ``@methods.method(name)`` at import time; the name is
    checked right there, so an undeclared one fails the import. ``register(table)`` adds them."""

    def __init__(self) -> None:
        self._entries: list[tuple[str, Handler[C]]] = []

    def method(self, name: str) -> Callable[[Handler[C]], Handler[C]]:
        declared(name)
        if any(existing == name for existing, _ in self._entries):
            raise ValueError(f"{name!r} is declared twice in this module")

        def keep(handler: Handler[C]) -> Handler[C]:
            self._entries.append((name, handler))
            return handler

        return keep

    def names(self) -> list[str]:
        return [name for name, _ in self._entries]

    def register(self, table: MethodTable[C]) -> None:
        for name, handler in self._entries:
            table.add(name, handler)


# -- frames ---------------------------------------------------------------------------------------


def error_frame(request_id: Any, error: ProtocolError) -> dict[str, Any]:
    """The wire form of an error answer."""
    body = make_error(error.error, error.message, path=error.path)
    return ErrorResponse(id=request_id, error=body).to_wire()


def _usable_id(value: object) -> str | int | None:
    if isinstance(value, bool) or not isinstance(value, str | int):
        return None
    return value


def decode_request(line: bytes | str) -> Request | dict[str, Any] | None:
    """One inbound text frame → the ``Request`` to dispatch, an error frame to send back, or
    None (a blank line, or a response / notification from the client, which is dropped)."""
    text = line.strip()
    if not text:
        return None
    try:
        value: object = json.loads(text)
    except ValueError:
        return error_frame(None, ProtocolError(PARSE_ERROR, "parse error"))
    try:
        frame = parse_frame(value)
    except FrameError as exc:
        request_id = (
            _usable_id(cast(dict[str, object], value).get("id"))
            if isinstance(value, dict)
            else None
        )
        return error_frame(request_id, ProtocolError(INVALID_REQUEST, str(exc)))
    if not isinstance(frame, Request):
        log.debug("dropped a %s frame from the client", type(frame).__name__)
        return None
    return frame


# -- contract checks ------------------------------------------------------------------------------


def strict_from(scope: Scope) -> bool:
    """Contract checks raise when ``INDUS_PROTOCOL_STRICT`` is set (the test flag)."""
    return scope.env.get(STRICT_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _report(problem: str, strict: bool) -> None:
    if strict:
        raise ContractViolation(problem)
    log.warning("contract: %s", problem)


def check_payload(model: type[ProtocolModel], wire: Mapping[str, Any], *, strict: bool) -> bool:
    """True when ``wire`` validates against ``model``; strict mode raises otherwise, production
    logs and answers False."""
    try:
        model.model_validate(dict(wire))
    except ValidationError as exc:
        _report(f"{model.__name__} payload does not validate: {exc.errors()[0]['msg']}", strict)
        return False
    return True


def check_event(method: str, params: Mapping[str, Any], *, strict: bool) -> bool:
    """Check an outgoing event notification against its declared model (an undeclared event name
    is the protocol tests' business, not the emitter's)."""
    model = EVENTS.get(method)
    if model is None:
        return True
    return check_payload(model, params, strict=strict)


# -- the dispatcher -------------------------------------------------------------------------------


class Dispatcher[C: RpcContext]:
    """Answers request frames from ``table`` for one connection's ``context`` (see the module
    notes)."""

    def __init__(
        self,
        context: C,
        table: MethodTable[C],
        *,
        deadline: float = DEFAULT_DEADLINE_S,
        deadlines: Mapping[str, float] = DEADLINES,
    ) -> None:
        self.context = context
        self.table = table
        self.deadline = deadline
        self.deadlines = deadlines

    def deadline_for(self, method: str) -> float:
        return self.deadlines.get(method, self.deadline)

    async def handle(self, request: Request) -> dict[str, Any]:
        """The response frame (as a dict) for ``request``."""
        method = request.method
        if method not in METHODS:
            return error_frame(
                request.id, ProtocolError(METHOD_NOT_FOUND, f"unknown method: {method}")
            )
        handler = self.table.get(method)
        if handler is None:
            return error_frame(
                request.id,
                ProtocolError(METHOD_NOT_FOUND, f"{method} is not served here"),
            )
        seconds = self.deadline_for(method)
        timer = asyncio.timeout(seconds)
        try:
            params = validate_params(method, request.params)
            with use_scope(self.context.scope):
                async with timer:
                    result = await handler(self.context, params)
        except ProtocolError as exc:
            return error_frame(request.id, exc)
        except IndusError as exc:
            return error_frame(request.id, protocol_error_for(exc))
        except ContractViolation:
            raise
        except TimeoutError as exc:
            if not timer.expired():  # the handler's own timeout, not ours: a crash
                log.error("handler for %s failed", method, exc_info=exc)
                return error_frame(
                    request.id, ProtocolError(INTERNAL_ERROR, "handler error: TimeoutError")
                )
            log.warning("handler for %s passed its %g s deadline", method, seconds)
            return self._deadline_frame(request, seconds)
        except Exception as exc:
            log.error("handler for %s failed", method, exc_info=exc)
            return error_frame(
                request.id, ProtocolError(INTERNAL_ERROR, f"handler error: {type(exc).__name__}")
            )
        self.check_result(method, result)
        produced = cast(object, result)  # typed handlers can still return junk at run time
        if not isinstance(produced, ProtocolModel):
            return error_frame(
                request.id, ProtocolError(INTERNAL_ERROR, f"handler error: {method} gave no model")
            )
        return SuccessResponse(id=request.id, result=produced.to_wire()).to_wire()

    def _deadline_frame(self, request: Request, seconds: float) -> dict[str, Any]:
        body: ErrorObject = make_error(
            INTERNAL_ERROR,
            f"handler error: {request.method} passed its {seconds:g} s deadline",
            retryable=True,
        )
        return ErrorResponse(id=request.id, error=body).to_wire()

    def check_result(self, method: str, result: object) -> None:
        """Raise (strict) or log when ``result`` does not fit the method's result model."""
        expected = METHODS[method].result
        if not isinstance(result, expected):
            _report(
                f"{method} returned {type(result).__name__}, not {expected.__name__}",
                self.context.strict,
            )
            return
        try:
            expected.model_validate(result.to_wire())
        except ValidationError as exc:
            _report(
                f"{method} result does not validate: {exc.errors()[0]['msg']}",
                self.context.strict,
            )
