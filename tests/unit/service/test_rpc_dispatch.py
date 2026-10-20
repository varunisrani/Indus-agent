"""P17-12: the shared JSON-RPC dispatcher every transport uses (stdio bridge, daemon WebSocket)."""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.errors import IndusError
from indus.foundation.scope import Scope, current_scope
from indus.protocol.base import ProtocolModel
from indus.protocol.bridge import ConfigGetResult, SessionListResult
from indus.protocol.envelope import Request
from indus.protocol.errors import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    SESSION_NOT_FOUND,
    TEXT_EMPTY,
    UNKNOWN_PARAM_KEY,
)
from indus.service import rpc
from indus.service.rpc import (
    DEADLINES,
    DEFAULT_DEADLINE_S,
    ContractViolation,
    Dispatcher,
    MethodSet,
    MethodTable,
    UndeclaredMethod,
    check_event,
    declared,
    decode_request,
    strict_from,
)


@dataclass
class Ctx:
    scope: Scope
    strict: bool = True


async def config_ok(ctx: Ctx, params: Any) -> ProtocolModel:
    return ConfigGetResult(key=params.key, value=None)


def table_with(**handlers: Any) -> MethodTable[Ctx]:
    table = MethodTable[Ctx]()
    for name, handler in handlers.items():
        table.add(name.replace("__", "."), handler)
    return table


def handle(d: Dispatcher[Ctx], method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return asyncio.run(d.handle(Request(id=1, method=method, params=params)))


# -- declarations ---------------------------------------------------------------------------------


def test_table_refuses_undeclared_and_duplicate_names() -> None:
    table = MethodTable[Ctx]()
    table.add("config.get", config_ok)
    with pytest.raises(UndeclaredMethod, match="not declared"):
        table.add("session.explode", config_ok)
    with pytest.raises(ValueError, match="already"):
        table.add("config.get", config_ok)
    assert declared("config.get") == "config.get"
    methods = MethodSet[Ctx]()
    methods.method("config.get")(config_ok)
    with pytest.raises(ValueError, match="twice"):
        methods.method("config.get")
    with pytest.raises(UndeclaredMethod):
        methods.method("session.explode")


MODULE_SOURCE = """
from indus.service.rpc import MethodSet

methods = MethodSet()


@methods.method({name!r})
async def handler(ctx, params):
    return None
"""


def import_from(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


def test_undeclared_method_fails_at_import(tmp_path: Path) -> None:
    bad = tmp_path / "rpc_bad_handlers.py"
    bad.write_text(MODULE_SOURCE.format(name="session.explode"), encoding="utf-8")
    with pytest.raises(UndeclaredMethod, match="session.explode"):
        import_from(bad, "rpc_bad_handlers")
    good = tmp_path / "rpc_good_handlers.py"
    good.write_text(MODULE_SOURCE.format(name="config.get"), encoding="utf-8")
    module = import_from(good, "rpc_good_handlers")
    table = MethodTable[Any]()
    module.methods.register(table)
    assert table.names() == ["config.get"]


# -- answering ------------------------------------------------------------------------------------


def test_unknown_and_unserved_methods(scope: Scope) -> None:
    d = Dispatcher(Ctx(scope), table_with(config__get=config_ok))
    unknown = handle(d, "session.explode")
    assert unknown["error"]["code"] == METHOD_NOT_FOUND.code and unknown["id"] == 1
    unserved = handle(d, "session.list", {})
    assert unserved["error"]["code"] == METHOD_NOT_FOUND.code
    assert "not served" in unserved["error"]["message"]


def test_unknown_key_names_its_path(scope: Scope) -> None:
    async def never(ctx: Ctx, params: Any) -> ProtocolModel:
        raise AssertionError("params must be refused before the handler runs")

    d = Dispatcher(Ctx(scope), table_with(session__list=never, server_request__answer=never))
    top = handle(d, "session.list", {"limit": 3, "colour": "red"})
    assert top["error"]["code"] == UNKNOWN_PARAM_KEY.code
    assert top["error"]["data"] == {"name": "UNKNOWN_PARAM_KEY", "path": "colour"}
    secret = "pw-" + "Qz81"
    nested = handle(
        d,
        "server_request.answer",
        {
            "session_id": "s",
            "request_id": "r",
            "answer": {"kind": "sudo", "password": secret, "extra": 1},
        },
    )
    assert nested["error"]["code"] == UNKNOWN_PARAM_KEY.code
    assert nested["error"]["data"]["path"] == "answer.sudo.extra"
    assert secret not in str(nested)
    typed = handle(d, "session.list", {"limit": "many"})
    assert (
        typed["error"]["code"] == INVALID_PARAMS.code and typed["error"]["data"]["path"] == "limit"
    )


def test_blank_text(scope: Scope) -> None:
    async def never(ctx: Ctx, params: Any) -> ProtocolModel:
        raise AssertionError

    d = Dispatcher(Ctx(scope), table_with(prompt__submit=never))
    blank = handle(d, "prompt.submit", {"session_id": "s", "text": "  "})
    assert blank["error"]["code"] == TEXT_EMPTY.code and blank["error"]["data"]["path"] == "text"


def test_handler_crash_is_answered_and_the_next_call_served(
    scope: Scope, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "pw-" + "Hx72kq9"
    calls: list[str] = []

    async def crash(ctx: Ctx, params: Any) -> ProtocolModel:
        calls.append("crash")
        raise ZeroDivisionError("boom")

    d = Dispatcher(Ctx(scope), table_with(server_request__answer=crash, config__get=config_ok))
    params = {"session_id": "s", "request_id": "r", "answer": {"kind": "sudo", "password": secret}}

    async def both() -> tuple[dict[str, Any], dict[str, Any]]:
        first = await d.handle(Request(id=1, method="server_request.answer", params=params))
        second = await d.handle(Request(id=2, method="config.get", params={"key": "theme"}))
        return first, second

    with caplog.at_level(logging.DEBUG):
        first, second = asyncio.run(both())
    assert first["error"]["code"] == INTERNAL_ERROR.code
    assert first["error"]["message"] == "handler error: ZeroDivisionError"
    assert second == {"jsonrpc": "2.0", "id": 2, "result": {"key": "theme", "value": None}}
    assert calls == ["crash"]
    assert secret not in caplog.text and "server_request.answer" in caplog.text


def test_a_hanging_handler_is_answered_at_its_deadline(scope: Scope) -> None:
    async def hang(ctx: Ctx, params: Any) -> ProtocolModel:
        await asyncio.Event().wait()
        raise AssertionError

    d = Dispatcher(Ctx(scope), table_with(session__list=hang, config__get=config_ok), deadline=0.05)

    async def both() -> tuple[dict[str, Any], dict[str, Any]]:
        first = await d.handle(Request(id=1, method="session.list", params={}))
        second = await d.handle(Request(id=2, method="config.get", params={"key": "x"}))
        return first, second

    first, second = asyncio.run(both())
    assert first["error"]["code"] == INTERNAL_ERROR.code
    assert first["error"]["message"] == "handler error: session.list passed its 0.05 s deadline"
    assert first["error"]["data"] == {"name": "INTERNAL_ERROR", "retryable": True}
    assert second["result"] == {"key": "x", "value": None}


def test_per_method_deadlines(scope: Scope) -> None:
    async def hang(ctx: Ctx, params: Any) -> ProtocolModel:
        await asyncio.Event().wait()
        raise AssertionError

    d = Dispatcher(
        Ctx(scope), table_with(config__get=hang), deadline=100.0, deadlines={"config.get": 0.05}
    )
    assert d.deadline_for("config.get") == 0.05 and d.deadline_for("session.list") == 100.0
    answer = handle(d, "config.get", {"key": "x"})
    assert "deadline" in answer["error"]["message"]
    assert DEFAULT_DEADLINE_S == 120.0
    assert DEADLINES == {
        "prompt.submit": 1800.0,
        "prompt.background": 1800.0,
        "session.compress": 600.0,
    }


def test_a_handlers_own_timeout_is_a_crash_not_a_deadline(scope: Scope) -> None:
    async def own(ctx: Ctx, params: Any) -> ProtocolModel:
        raise TimeoutError

    d = Dispatcher(Ctx(scope), table_with(config__get=own))
    answer = handle(d, "config.get", {"key": "x"})
    assert answer["error"]["message"] == "handler error: TimeoutError"
    assert "retryable" not in answer["error"]["data"]


def test_product_errors_are_mapped(scope: Scope) -> None:
    async def gone(ctx: Ctx, params: Any) -> ProtocolModel:
        raise IndusError("store.session_not_found", "no such session")

    d = Dispatcher(Ctx(scope), table_with(config__get=gone))
    assert handle(d, "config.get", {"key": "x"})["error"]["code"] == SESSION_NOT_FOUND.code


def test_handlers_run_under_the_context_scope(scope: Scope) -> None:
    seen: list[Scope] = []

    async def where(ctx: Ctx, params: Any) -> ProtocolModel:
        seen.append(current_scope())
        return ConfigGetResult(key="x", value=None)

    d = Dispatcher(Ctx(scope), table_with(config__get=where))
    assert handle(d, "config.get", {"key": "x"})["result"] == {"key": "x", "value": None}
    assert seen == [scope]


# -- contract checks ------------------------------------------------------------------------------


async def wrong(ctx: Ctx, params: Any) -> ProtocolModel:
    return ConfigGetResult(key="x", value=1)


def test_bad_result_raises_in_strict_mode(scope: Scope) -> None:
    d = Dispatcher(Ctx(scope), table_with(session__list=wrong))
    with pytest.raises(ContractViolation, match="session.list returned ConfigGetResult"):
        handle(d, "session.list", {})


def test_bad_result_is_logged_when_lenient(scope: Scope, caplog: pytest.LogCaptureFixture) -> None:
    async def nothing(ctx: Ctx, params: Any) -> Any:
        return {"sessions": []}

    d = Dispatcher(Ctx(scope, strict=False), table_with(session__list=wrong, config__get=nothing))
    with caplog.at_level(logging.WARNING):
        answer = handle(d, "session.list", {})
        bare = handle(d, "config.get", {"key": "x"})
    assert answer["result"] == {"key": "x", "value": 1}
    assert "contract: session.list returned ConfigGetResult" in caplog.text
    assert bare["error"]["message"] == "handler error: config.get gave no model"
    assert SessionListResult  # the declared model the lenient answer did not match


def test_event_payloads_are_checked(caplog: pytest.LogCaptureFixture) -> None:
    good = {"type": "message.delta", "session_id": "s", "seq": 1, "turn_id": "t", "text": "hi"}
    bad = {**good, "seq": -1}
    assert check_event("message.delta", good, strict=True)
    with pytest.raises(ContractViolation, match="MessageDelta"):
        check_event("message.delta", bad, strict=True)
    with caplog.at_level(logging.WARNING):
        assert check_event("message.delta", bad, strict=False) is False
    assert "contract: MessageDelta" in caplog.text
    assert check_event("gateway.ready", {"anything": 1}, strict=True)


# -- frames ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "expect"),
    [
        (b"", None),
        (b"   \n", None),
        (b"{nope", ("error", PARSE_ERROR.code, None)),
        (b'{"jsonrpc":"1.0","id":4,"method":"x"}', ("error", INVALID_REQUEST.code, 4)),
        (b'{"jsonrpc":"2.0","id":true,"method":""}', ("error", INVALID_REQUEST.code, None)),
        (b"[1, 2]", ("error", INVALID_REQUEST.code, None)),
        (b'{"jsonrpc":"2.0","id":9,"result":{}}', None),
        (b'{"jsonrpc":"2.0","method":"gateway.ping"}', None),
        ('{"jsonrpc":"2.0","id":"a","method":"config.get"}', ("request", "a", "config.get")),
    ],
)
def test_decode_request(line: bytes | str, expect: tuple[Any, ...] | None) -> None:
    decoded = decode_request(line)
    if expect is None:
        assert decoded is None
    elif expect[0] == "request":
        assert isinstance(decoded, Request)
        assert (decoded.id, decoded.method) == expect[1:]
    else:
        assert isinstance(decoded, dict)
        assert decoded["error"]["code"] == expect[1] and decoded["id"] == expect[2]


def test_strict_flag(scope: Scope) -> None:
    assert (
        strict_from(scope.with_env(scope.env.with_overlay({"INDUS_PROTOCOL_STRICT": "1"}))) is True
    )
    assert (
        strict_from(scope.with_env(scope.env.with_overlay({"INDUS_PROTOCOL_STRICT": "off"})))
        is False
    )


def test_the_p12_names_are_thin_re_exports() -> None:
    from indus.tui_bridge import bridge, dispatch, stdio_server

    assert bridge.ContractViolation is rpc.ContractViolation
    assert issubclass(dispatch.Dispatcher, rpc.Dispatcher)
    assert dispatch.MethodTable is rpc.MethodTable
    assert dispatch.error_frame is rpc.error_frame
    assert stdio_server.Dispatcher is dispatch.Dispatcher
