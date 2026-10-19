"""The dispatcher (P12-28): declared methods only, param errors with paths, error mapping, crash
answers without leaking params, and result checks (strict raises, lenient logs)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from indus.foundation.errors import IndusError
from indus.foundation.scope import Scope, current_scope
from indus.protocol.base import ProtocolModel
from indus.protocol.bridge import ConfigGetResult
from indus.protocol.envelope import Request
from indus.protocol.errors import (
    INTERNAL_ERROR,
    METHOD_NOT_FOUND,
    SESSION_NOT_FOUND,
    TEXT_EMPTY,
    UNKNOWN_PARAM_KEY,
)
from indus.protocol.methods import METHODS
from indus.service.session_service import SessionService
from indus.tui_bridge.bridge import Bridge, ContractViolation
from indus.tui_bridge.dispatch import HANDLER_MODULES, Dispatcher, MethodTable, build_table


class Quiet:
    def notify(self, method: str, params: Any) -> bool:
        return True


def no_service(bridge: Bridge) -> SessionService:
    raise RuntimeError("not needed")


def dispatcher(
    scope: Scope, table: MethodTable | None = None, *, strict: bool = True
) -> Dispatcher:
    return Dispatcher(Bridge(scope, Quiet(), make_service=no_service, strict=strict), table)


def handle(d: Dispatcher, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return asyncio.run(d.handle(Request(id=1, method=method, params=params)))


def test_table_serves_every_declared_method_and_nothing_else() -> None:
    table = build_table()
    assert sorted(table.names()) == sorted(METHODS)
    assert len(HANDLER_MODULES) == 8
    with pytest.raises(ValueError, match="not declared"):
        table.add("session.explode", lambda bridge, params: None)  # type: ignore[arg-type, return-value]
    with pytest.raises(ValueError, match="already"):
        table.add("config.get", lambda bridge, params: None)  # type: ignore[arg-type, return-value]


def test_unknown_and_unserved_methods(scope: Scope) -> None:
    assert handle(dispatcher(scope), "session.explode")["error"]["code"] == METHOD_NOT_FOUND.code
    empty = handle(dispatcher(scope, MethodTable()), "config.get", {"key": "theme"})
    assert empty["error"]["code"] == METHOD_NOT_FOUND.code
    assert "not served" in empty["error"]["message"]


def test_unknown_key_names_its_path(scope: Scope) -> None:
    answer = handle(dispatcher(scope), "session.list", {"limit": 3, "colour": "red"})
    assert answer["error"]["code"] == UNKNOWN_PARAM_KEY.code
    assert answer["error"]["data"] == {"name": "UNKNOWN_PARAM_KEY", "path": "colour"}
    blank = handle(dispatcher(scope), "prompt.submit", {"session_id": "s", "text": "  "})
    assert blank["error"]["code"] == TEXT_EMPTY.code and blank["error"]["data"]["path"] == "text"


def test_handler_crash_is_answered(scope: Scope, caplog: pytest.LogCaptureFixture) -> None:
    secret = "pw-" + "Hx72kq9"

    async def crash(bridge: Bridge, params: Any) -> ProtocolModel:
        raise ZeroDivisionError("boom")

    table = MethodTable()
    table.add("server_request.answer", crash)
    params = {"session_id": "s", "request_id": "r", "answer": {"kind": "sudo", "password": secret}}
    with caplog.at_level(logging.DEBUG):
        answer = handle(dispatcher(scope, table), "server_request.answer", params)
    assert answer["error"]["code"] == INTERNAL_ERROR.code
    assert answer["error"]["message"] == "handler error: ZeroDivisionError"
    assert secret not in caplog.text and "server_request.answer" in caplog.text


def test_product_errors_are_mapped(scope: Scope) -> None:
    async def gone(bridge: Bridge, params: Any) -> ProtocolModel:
        raise IndusError("store.session_not_found", "no such session")

    table = MethodTable()
    table.add("config.get", gone)
    answer = handle(dispatcher(scope, table), "config.get", {"key": "theme"})
    assert answer["error"]["code"] == SESSION_NOT_FOUND.code


def test_handlers_run_under_the_bridge_scope(scope: Scope) -> None:
    seen: list[Scope] = []

    async def where(bridge: Bridge, params: Any) -> ProtocolModel:
        seen.append(current_scope())
        return ConfigGetResult(key="x", value=None)

    table = MethodTable()
    table.add("config.get", where)
    answer = handle(dispatcher(scope, table), "config.get", {"key": "x"})
    assert answer["result"] == {"key": "x", "value": None}
    assert seen == [scope]


def test_bad_result_raises_in_strict_mode(scope: Scope) -> None:
    async def wrong(bridge: Bridge, params: Any) -> ProtocolModel:
        return ConfigGetResult(key="x", value=1)

    table = MethodTable()
    table.add("session.list", wrong)
    with pytest.raises(ContractViolation, match="session.list returned ConfigGetResult"):
        handle(dispatcher(scope, table), "session.list", {})


def test_bad_result_is_logged_when_lenient(scope: Scope, caplog: pytest.LogCaptureFixture) -> None:
    async def wrong(bridge: Bridge, params: Any) -> ProtocolModel:
        return ConfigGetResult(key="x", value=1)

    table = MethodTable()
    table.add("session.list", wrong)
    with caplog.at_level(logging.WARNING):
        answer = handle(dispatcher(scope, table, strict=False), "session.list", {})
    assert answer["result"] == {"key": "x", "value": 1}
    assert "contract: session.list returned ConfigGetResult" in caplog.text
