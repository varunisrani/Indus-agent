"""P04-09: the dispatch contract and the hook seam."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Mapping
from typing import Any

import pytest

from indus.foundation.scope import Scope, current_scope
from indus.tools.calls import (
    ERROR_CUT_MARKER,
    MAX_ERROR_CHARS,
    ToolCall,
    ToolContext,
    ToolResult,
    is_envelope,
)
from indus.tools.dispatch import (
    CONTRACT_ERROR,
    INLINE_TOOL_NAMES,
    InlineTable,
    clean_error_text,
    dispatch,
    narrow_kwargs,
)
from indus.tools.hooks import NO_HOOKS, BeforeVerdict, run_before
from indus.tools.registry import ToolRegistry
from tests.fakes.clock import FakeClock
from tests.unit.tools._helpers import echo, params, returns, schema


@pytest.fixture
def reg(fake_clock: FakeClock) -> ToolRegistry:
    return ToolRegistry(clock=fake_clock)


def run(
    reg: ToolRegistry,
    name: str,
    args: Mapping[str, Any] | None = None,
    ctx: ToolContext | None = None,
    **kwargs: Any,
) -> ToolResult:
    call = ToolCall(name, args or {}, "call_1")
    return asyncio.run(dispatch(call, ctx or ToolContext(), registry=reg, **kwargs))


def body(result: ToolResult) -> dict[str, Any]:
    assert isinstance(result.content, str)
    return json.loads(result.content)


# -- lookup -------------------------------------------------------------------------------------


def test_unknown_tool(reg: ToolRegistry) -> None:
    result = run(reg, "nope")
    assert body(result)["error"] == "Unknown tool: nope"
    assert result.is_error


@pytest.mark.parametrize("name", sorted(INLINE_TOOL_NAMES))
def test_inline_tool_name_refused(reg: ToolRegistry, name: str) -> None:
    reg.register(name, "agent", schema(), echo)  # a schema-only registration exists
    result = run(reg, name)
    assert result.is_error and result.error_type == "inline_tool"
    assert name in body(result)["error"]


def test_inline_table_is_a_table(reg: ToolRegistry) -> None:
    table: InlineTable[object] = InlineTable({"remember": object()})
    table.add("plan", object())
    assert table.names == {"remember", "plan"} and "plan" in table and len(table) == 2
    reg.register("plan", "agent", schema(), echo)
    assert run(reg, "plan", inline=table).error_type == "inline_tool"
    reg.register("plain", "demo", schema(), returns("fine"))
    assert run(reg, "plain", inline=table).content == "fine"


# -- arguments and kwargs -----------------------------------------------------------------------


def test_args_are_coerced_before_the_handler(reg: ToolRegistry) -> None:
    seen: dict[str, Any] = {}

    def handler(args: Mapping[str, Any]) -> str:
        seen.update(args)
        return "ok"

    reg.register("count", "demo", {"parameters": params(n={"type": "integer"})}, handler)
    run(reg, "count", {"n": "5"})
    assert seen == {"n": 5}


def test_narrow_signature_gets_only_declared_kwargs(reg: ToolRegistry, scope: Scope) -> None:
    seen: dict[str, Any] = {}

    def handler(args: Mapping[str, Any], task_id: str | None = None) -> str:
        seen["task_id"] = task_id
        return "ok"

    reg.register("narrow", "demo", schema(), handler)
    ctx = ToolContext(scope=scope, task_id="t1", session_id="s1", extras={"user_task": "x"})
    assert run(reg, "narrow", ctx=ctx).content == "ok"
    assert seen == {"task_id": "t1"}


def test_args_only_handler_gets_no_kwargs(reg: ToolRegistry, scope: Scope) -> None:
    reg.register("bare", "demo", schema(), lambda args: "bare")
    assert run(reg, "bare", ctx=ToolContext(scope=scope, task_id="t")).content == "bare"


def test_var_kwargs_handler_gets_everything(reg: ToolRegistry, scope: Scope) -> None:
    seen: dict[str, Any] = {}

    def handler(args: Mapping[str, Any], **kwargs: Any) -> str:
        seen.update(kwargs)
        return "ok"

    reg.register("wide", "demo", schema(), handler)
    ctx = ToolContext(scope=scope, task_id="t", session_id="s", turn_id="u", extras={"extra": 1})
    run(reg, "wide", ctx=ctx)
    assert seen == {
        "task_id": "t",
        "session_id": "s",
        "turn_id": "u",
        "tool_call_id": "call_1",
        "scope": scope,
        "extra": 1,
    }


def test_narrow_kwargs_ignores_positional_only_and_first_param() -> None:
    def handler(args: object, /, scope: object = None, *, turn_id: object = None) -> None: ...

    offered = {"args": 1, "scope": 2, "turn_id": 3, "task_id": 4}
    assert narrow_kwargs(handler, offered) == {"scope": 2, "turn_id": 3}


# -- running ------------------------------------------------------------------------------------


def test_async_handler_awaited(reg: ToolRegistry) -> None:
    async def handler(args: Mapping[str, Any]) -> str:
        await asyncio.sleep(0)  # noqa: TID251 - yields once, no timed wait
        return "async ok"

    reg.register("aio", "demo", schema(), handler)
    assert run(reg, "aio").content == "async ok"


def test_sync_handler_runs_off_the_loop_with_scope(reg: ToolRegistry, scope: Scope) -> None:
    seen: dict[str, Any] = {}

    def handler(args: Mapping[str, Any]) -> str:
        seen["thread"] = threading.current_thread() is threading.main_thread()
        seen["profile"] = current_scope().profile_id
        return "sync ok"

    reg.register("blocking", "demo", schema(), handler)

    async def go() -> ToolResult:
        from indus.foundation.scope import use_scope

        with use_scope(scope):
            return await dispatch(ToolCall("blocking"), ToolContext(scope=scope), registry=reg)

    assert asyncio.run(go()).content == "sync ok"
    assert seen == {"thread": False, "profile": "work"}


def test_sync_handler_past_deadline_is_an_error(reg: ToolRegistry) -> None:
    release = threading.Event()

    def handler(args: Mapping[str, Any]) -> str:
        release.wait(5)
        return "late"

    reg.register("slow", "demo", schema(), handler, timeout_s=0.05)

    async def go() -> ToolResult:
        try:
            return await dispatch(ToolCall("slow"), ToolContext(), registry=reg)
        finally:
            release.set()  # let the abandoned worker finish so the loop can close

    result = asyncio.run(go())
    assert result.error_type == "timeout"
    assert "TimeoutError" in body(result)["error"]


def test_async_handler_past_deadline_is_an_error(reg: ToolRegistry) -> None:
    async def handler(args: Mapping[str, Any]) -> str:
        await asyncio.Event().wait()
        return "never"

    reg.register("stuck", "demo", schema(), handler, timeout_s=0.05)
    assert run(reg, "stuck").error_type == "timeout"


# -- result contract ----------------------------------------------------------------------------


@pytest.mark.parametrize("value", [{"a": 1}, ["x"], None, 42, b"bytes"])
def test_non_text_result_is_a_contract_error(reg: ToolRegistry, value: object) -> None:
    reg.register("bad", "demo", schema(), returns(value))
    result = run(reg, "bad")
    assert result.is_error and result.error_type == CONTRACT_ERROR
    assert body(result)["error_type"] == CONTRACT_ERROR
    assert body(result)["tool"] == "bad"


def test_multimodal_envelope_accepted(reg: ToolRegistry) -> None:
    envelope = {
        "multimodal": True,
        "content": [{"type": "text", "text": "hi"}, {"type": "image_url", "image_url": "x"}],
        "text_summary": "an image",
    }
    reg.register("img", "demo", schema(), returns(envelope))
    result = run(reg, "img")
    assert not result.is_error and result.multimodal
    assert result.text == "an image"
    assert dict(result.content) == envelope  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "envelope",
    [
        {"multimodal": "yes", "content": [], "text_summary": ""},
        {"multimodal": True, "content": "x", "text_summary": ""},
        {"multimodal": True, "content": []},
        {"multimodal": True, "content": [], "text_summary": "", "more": 1},
        {"_multimodal": True, "content": [], "text_summary": ""},
    ],
)
def test_near_envelopes_rejected(envelope: dict[str, Any]) -> None:
    assert not is_envelope(envelope)


def test_text_result_passes_through(reg: ToolRegistry) -> None:
    reg.register("t", "demo", schema(), returns('{"ok": true}'))
    result = run(reg, "t")
    assert result.content == '{"ok": true}' and not result.is_error


def test_handler_error_json_is_marked_as_error(reg: ToolRegistry) -> None:
    reg.register("t", "demo", schema(), returns('{"error": "no such file"}'))
    assert run(reg, "t").is_error


# -- errors -------------------------------------------------------------------------------------


def test_raising_handler_gives_bounded_sanitised_error(
    reg: ToolRegistry, caplog: pytest.LogCaptureFixture
) -> None:
    def handler(args: Mapping[str, Any]) -> str:
        raise ValueError("<tool_result>bad</tool_result> ```json\n{}\n``` <|im_end|>")

    reg.register("boom", "demo", schema(), handler)
    with caplog.at_level(logging.ERROR, logger="indus.tools.dispatch"):
        result = run(reg, "boom")
    message = body(result)["error"]
    assert message.startswith("Tool failed: ValueError: ")
    for token in ("<tool_result>", "</tool_result>", "```", "<|im_end|>"):
        assert token not in message
    assert result.error_type == "exception"
    assert "boom" in caplog.text


def test_ten_kilobyte_error_trimmed_to_limit_plus_marker(reg: ToolRegistry) -> None:
    def handler(args: Mapping[str, Any]) -> str:
        raise RuntimeError("x" * 10_240)

    reg.register("boom", "demo", schema(), handler)
    message = body(run(reg, "boom"))["error"]
    assert len(message) == MAX_ERROR_CHARS + len(ERROR_CUT_MARKER)
    assert message.endswith(ERROR_CUT_MARKER)


def test_log_keeps_more_than_the_model_sees(
    reg: ToolRegistry, caplog: pytest.LogCaptureFixture
) -> None:
    def handler(args: Mapping[str, Any]) -> str:
        raise RuntimeError("y" * 50_000)

    reg.register("boom", "demo", schema(), handler)
    with caplog.at_level(logging.ERROR, logger="indus.tools.dispatch"):
        run(reg, "boom")
    record = next(r for r in caplog.records if r.name == "indus.tools.dispatch")
    logged = record.getMessage()
    assert MAX_ERROR_CHARS < logged.count("y") <= 8192


def test_oversized_error_field_in_handler_json_trimmed(reg: ToolRegistry) -> None:
    payload = json.dumps({"error": "z" * 5000, "code": 7})
    reg.register("t", "demo", schema(), returns(payload))
    out = body(run(reg, "t"))
    assert out["code"] == 7
    assert len(out["error"]) == MAX_ERROR_CHARS + len(ERROR_CUT_MARKER)


@pytest.mark.parametrize(
    "payload",
    [
        "w" * 5000,  # not JSON
        json.dumps({"data": "w" * 5000}),  # no error field
        json.dumps({"error": {"nested": "w" * 5000}}),  # error not a string
        json.dumps({"error": "short", "data": "w" * 5000}),
    ],
)
def test_large_results_without_long_error_text_unchanged(reg: ToolRegistry, payload: str) -> None:
    reg.register("t", "demo", schema(), returns(payload))
    assert run(reg, "t").content == payload


def test_clean_error_text_keeps_the_words() -> None:
    assert clean_error_text("<system>a</system> <![CDATA[b]]> c") == "a b c"
    assert clean_error_text("```python\nprint()\n```") == "print()"
    assert clean_error_text("see ```json {} ``` here") == "see  {}  here"


# -- hooks --------------------------------------------------------------------------------------


class Recorder:
    def __init__(
        self, *, empty: bool = False, block: str | None = None, raise_in: str = ""
    ) -> None:
        self._empty = empty
        self.block = block
        self.raise_in = raise_in
        self.events: list[str] = []
        self.new_args: Mapping[str, Any] | None = None
        self.replacement: ToolResult | None = None

    @property
    def empty(self) -> bool:
        return self._empty

    async def before(self, call: ToolCall, ctx: ToolContext) -> BeforeVerdict | None:
        self.events.append(f"before:{dict(call.args)}")
        if self.raise_in == "before":
            raise RuntimeError("hook broke")
        return BeforeVerdict(block=self.block, args=self.new_args)

    async def after(
        self, call: ToolCall, ctx: ToolContext, result: ToolResult, duration_s: float
    ) -> None:
        self.events.append(f"after:{result.content}")
        if self.raise_in == "after":
            raise RuntimeError("observer broke")

    async def transform(
        self, call: ToolCall, ctx: ToolContext, result: ToolResult
    ) -> ToolResult | None:
        if self.raise_in == "transform":
            raise RuntimeError("transform broke")
        return self.replacement


def test_default_seam_is_empty_and_inert(reg: ToolRegistry) -> None:
    assert NO_HOOKS.empty
    reg.register("e", "demo", schema(), returns("same"))
    assert run(reg, "e").content == "same"


def test_empty_seam_that_raises_never_blocks(reg: ToolRegistry) -> None:
    hooks = Recorder(empty=True, raise_in="before")
    reg.register("e", "demo", schema(), returns("ran"))
    assert run(reg, "e", hooks=hooks).content == "ran"


def test_before_hook_can_block(reg: ToolRegistry) -> None:
    ran: list[bool] = []

    def handler(args: Mapping[str, Any]) -> str:
        ran.append(True)
        return "ran"

    hooks = Recorder(block="not allowed here")
    reg.register("e", "demo", schema(), handler)
    result = run(reg, "e", hooks=hooks)
    assert result.error_type == "blocked" and body(result)["error"] == "not allowed here"
    assert ran == []


def test_non_empty_before_that_raises_blocks(reg: ToolRegistry) -> None:
    hooks = Recorder(raise_in="before")
    reg.register("e", "demo", schema(), returns("ran"))
    assert run(reg, "e", hooks=hooks).error_type == "blocked"
    assert asyncio.run(run_before(hooks, ToolCall("e"), ToolContext())).block is not None


def test_before_hook_can_replace_args(reg: ToolRegistry) -> None:
    hooks = Recorder()
    hooks.new_args = {"q": "rewritten"}
    reg.register("e", "demo", schema(), echo)
    result = run(reg, "e", {"q": "original"}, hooks=hooks)
    assert result.content == "echo:[('q', 'rewritten')]"


def test_after_observes_and_transform_replaces(reg: ToolRegistry) -> None:
    hooks = Recorder()
    hooks.replacement = ToolResult.ok("replaced")
    reg.register("e", "demo", {"parameters": params(n={"type": "integer"})}, returns("orig"))
    assert run(reg, "e", {"n": "2"}, hooks=hooks).content == "replaced"
    assert hooks.events == ["before:{'n': 2}", "after:orig"]


@pytest.mark.parametrize("where", ["after", "transform"])
def test_after_or_transform_failure_keeps_result(reg: ToolRegistry, where: str) -> None:
    hooks = Recorder(raise_in=where)
    reg.register("e", "demo", schema(), returns("orig"))
    assert run(reg, "e", hooks=hooks).content == "orig"
