"""The bridge's method table and dispatcher (P12-28).

Only methods declared in ``indus.protocol`` can be served: ``MethodTable.add`` refuses any other
name when the handler modules register. Each module in ``HANDLER_MODULES`` exposes
``register(table)``; later tasks add their module to that tuple.

``Dispatcher.handle(request)`` answers one request frame:

1. an undeclared method, or a declared one no module serves here → ``-32601``;
2. params validated against the method's model: an unknown key is ``UNKNOWN_PARAM_KEY`` with the
   dotted path, blank text ``TEXT_EMPTY``, anything else ``-32602``;
3. the handler runs under the process scope (R05.14); a ``ProtocolError`` becomes its error, a
   product ``IndusError`` is mapped through the protocol table, anything else is logged and
   answered ``-32603`` — the loop goes on;
4. the result is checked against the method's result model: a mismatch raises
   ``ContractViolation`` in strict mode and is logged otherwise.

Params are never written to the log: they may hold a password or a secret answer.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import ValidationError

from indus.foundation.errors import IndusError
from indus.foundation.scope import use_scope
from indus.protocol.base import ProtocolModel
from indus.protocol.envelope import ErrorResponse, Request, SuccessResponse
from indus.protocol.errors import INTERNAL_ERROR, METHOD_NOT_FOUND, make_error
from indus.protocol.methods import METHODS, ProtocolError, validate_params
from indus.service.session_service import protocol_error_for
from indus.tui_bridge.bridge import Bridge, ContractViolation

__all__ = [
    "HANDLER_MODULES",
    "Dispatcher",
    "Handler",
    "MethodTable",
    "build_table",
    "error_frame",
]

log = logging.getLogger(__name__)

type Handler = Callable[[Bridge, Any], Awaitable[ProtocolModel]]
"""``handler(bridge, params model) -> result model``."""

HANDLER_MODULES: tuple[str, ...] = (
    "indus.tui_bridge.methods_session",  # P12-29
    "indus.tui_bridge.methods_prompt",  # P12-29
    "indus.tui_bridge.server_requests",  # P12-30
    "indus.tui_bridge.methods_complete",  # P12-31
    "indus.tui_bridge.methods_slash",  # P12-31
    "indus.tui_bridge.methods_shell",  # P12-31
    "indus.tui_bridge.methods_config",  # P12-31
    "indus.tui_bridge.methods_composer",  # P12-31
)
"""Modules whose ``register(table)`` adds handlers, in order."""


class MethodTable:
    """Declared method name → handler."""

    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}

    def add(self, name: str, handler: Handler) -> None:
        if name not in METHODS:
            raise ValueError(f"{name!r} is not declared in indus.protocol")
        if name in self._handlers:
            raise ValueError(f"{name!r} has a handler already")
        self._handlers[name] = handler

    def get(self, name: str) -> Handler | None:
        return self._handlers.get(name)

    def names(self) -> list[str]:
        return list(self._handlers)


def build_table(modules: tuple[str, ...] = HANDLER_MODULES) -> MethodTable:
    """A table with every handler module registered."""
    table = MethodTable()
    for name in modules:
        importlib.import_module(name).register(table)
    return table


def error_frame(request_id: Any, error: ProtocolError) -> dict[str, Any]:
    """The wire form of an error answer."""
    body = make_error(error.error, error.message, path=error.path)
    return ErrorResponse(id=request_id, error=body).to_wire()


class Dispatcher:
    """Answers request frames from ``table`` (see the module notes)."""

    def __init__(self, bridge: Bridge, table: MethodTable | None = None) -> None:
        self.bridge = bridge
        self.table = table if table is not None else build_table()

    async def handle(self, request: Request) -> dict[str, Any]:
        """The response frame (as a dict) for ``request``."""
        method = request.method
        handler = self.table.get(method)
        if method not in METHODS:
            return error_frame(
                request.id, ProtocolError(METHOD_NOT_FOUND, f"unknown method: {method}")
            )
        if handler is None:
            return error_frame(
                request.id,
                ProtocolError(METHOD_NOT_FOUND, f"{method} is not served by this bridge"),
            )
        try:
            params = validate_params(method, request.params)
            with use_scope(self.bridge.scope):
                result = await handler(self.bridge, params)
        except ProtocolError as exc:
            return error_frame(request.id, exc)
        except IndusError as exc:
            return error_frame(request.id, protocol_error_for(exc))
        except ContractViolation:
            raise
        except Exception as exc:
            log.error("handler for %s failed", method, exc_info=exc)
            return error_frame(
                request.id, ProtocolError(INTERNAL_ERROR, f"handler error: {type(exc).__name__}")
            )
        self.check_result(method, result)
        if not isinstance(result, ProtocolModel):
            return error_frame(
                request.id, ProtocolError(INTERNAL_ERROR, f"handler error: {method} gave no model")
            )
        return SuccessResponse(id=request.id, result=result.to_wire()).to_wire()

    def check_result(self, method: str, result: object) -> None:
        """Raise (strict) or log when ``result`` does not fit the method's result model."""
        expected = METHODS[method].result
        problem: str | None = None
        if not isinstance(result, expected):
            problem = f"{method} returned {type(result).__name__}, not {expected.__name__}"
        else:
            try:
                expected.model_validate(result.to_wire())
            except ValidationError as exc:
                problem = f"{method} result does not validate: {exc.errors()[0]['msg']}"
        if problem is None:
            return
        if self.bridge.strict:
            raise ContractViolation(problem)
        log.warning("contract: %s", problem)
