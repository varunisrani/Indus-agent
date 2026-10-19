"""P13-17: the hooks bus — deadlines, suppression, fail-closed pre-tool checks, merging rules —
and the event bus caps."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Iterator, Mapping
from typing import Any

import pytest

from indus.foundation.blocking import BlockingPool
from indus.foundation.scope import Scope
from indus.plugins.hooks import (
    HOOK_NAMES,
    EventBus,
    HookBus,
    PluginToolHooks,
    hook_timeout,
    with_llm_context,
)
from indus.tools.calls import ToolCall, ToolContext
from indus.tools.dispatch import dispatch
from indus.tools.registry import ToolRegistry
from tests.fakes.agent import eventually
from tests.fakes.clock import FakeClock
from tests.unit.plugins._helpers import with_config


@pytest.fixture
def pool() -> Iterator[BlockingPool]:
    made = BlockingPool("hooks-test", 6, queue_limit=4)
    yield made
    made.shutdown()


@pytest.fixture
def bus(fake_clock: FakeClock, pool: BlockingPool) -> HookBus:
    return HookBus(clock=fake_clock, pool=pool)


class Gate:
    """A sync callback that hangs for the tool names in ``hang_on`` until released."""

    def __init__(self, *hang_on: str) -> None:
        self.hang_on = set(hang_on)
        self.calls: list[str] = []
        self.release = threading.Event()

    def __call__(self, tool_name: str, **_: Any) -> None:
        self.calls.append(tool_name)
        if tool_name in self.hang_on:
            self.release.wait()


def recording_registry(ran: list[Mapping[str, Any]]) -> ToolRegistry:
    registry = ToolRegistry()

    def probe(args: Mapping[str, Any]) -> str:
        ran.append(dict(args))
        return "ran with " + json.dumps(dict(args), sort_keys=True)

    registry.register("probe", "test", {"description": "p", "parameters": {}}, probe)
    return registry


async def timed_out(bus: HookBus, clock: FakeClock, gate: Gate, tool: str) -> Any:
    """Start a pre_tool_call for ``tool``, let it hang, and pass its deadline."""
    before = len(gate.calls)
    task = asyncio.ensure_future(bus.pre_tool_call(tool, {}))
    await eventually(lambda: len(gate.calls) > before and clock.pending >= 1)
    clock.advance(bus.timeout_s)
    return await task


def test_hook_names_are_generic_and_complete() -> None:
    assert len(HOOK_NAMES) == 37
    for name in ("pre_tool_call", "pre_llm_call", "transform_tool_result", "subagent_stop"):
        assert name in HOOK_NAMES


def test_timeout_setting(scope: Scope) -> None:
    def value(raw: object) -> float:
        return hook_timeout(with_config(scope, {"plugins": {"hook_callback_timeout": raw}}))

    assert hook_timeout(scope) == 30.0
    assert value(0) == 0.0
    assert value(5) == 5.0
    assert value(10_000) == 600.0
    assert value(-3) == 30.0
    assert (
        HookBus.from_scope(with_config(scope, {"plugins": {"hook_callback_timeout": 7}})).timeout_s
        == 7
    )


def test_hanging_pre_tool_call_blocks_then_is_suppressed(
    bus: HookBus, fake_clock: FakeClock
) -> None:
    gate = Gate("slow")
    bus.register("pre_tool_call", gate, owner="guard")

    async def go() -> tuple[Any, Any, Any]:
        first = await timed_out(bus, fake_clock, gate, "slow")
        second = await bus.pre_tool_call("fast", {})  # within 60 s: skipped, still blocked
        fake_clock.advance(61)
        third = await bus.pre_tool_call("fast", {})
        return first, second, third

    try:
        first, second, third = asyncio.run(go())
    finally:
        gate.release.set()
    assert first.action == "block" and "timed out after 30 s" in first.message
    assert "guard" in first.message
    assert second.action == "block" and "timed out recently" in second.message
    assert third.action is None
    assert gate.calls == ["slow", "fast"]  # the suppressed call never reached the callback


def test_hung_pre_tool_call_blocks_the_tool_through_dispatch(
    bus: HookBus, fake_clock: FakeClock
) -> None:
    gate = Gate("probe")
    bus.register("pre_tool_call", gate, owner="guard")
    ran: list[Mapping[str, Any]] = []
    registry = recording_registry(ran)
    hooks = PluginToolHooks(bus)

    async def go() -> Any:
        task = asyncio.ensure_future(
            dispatch(
                ToolCall("probe", {"q": 1}, "c1"), ToolContext(), registry=registry, hooks=hooks
            )
        )
        await eventually(lambda: bool(gate.calls) and fake_clock.pending >= 1)
        fake_clock.advance(30)
        return await task

    try:
        result = asyncio.run(go())
    finally:
        gate.release.set()
    assert result.is_error and result.error_type == "blocked"
    assert "timed out after 30 s" in json.loads(result.text)["error"]
    assert ran == []


def test_fourth_abandoned_worker_refused(bus: HookBus, fake_clock: FakeClock) -> None:
    gate = Gate("t1", "t2", "t3", "t4")
    bus.register("pre_tool_call", gate, owner="guard")

    async def go() -> Any:
        for tool in ("t1", "t2", "t3"):
            directive = await timed_out(bus, fake_clock, gate, tool)
            assert directive.action == "block"
            fake_clock.advance(61)  # past the suppression window
        return await bus.pre_tool_call("t4", {})

    try:
        fourth = asyncio.run(go())
    finally:
        gate.release.set()
    assert fourth.action == "block"
    assert "3 of its calls are still stuck" in fourth.message
    assert gate.calls == ["t1", "t2", "t3"]


def test_abandoned_count_drops_when_a_worker_returns(
    bus: HookBus, fake_clock: FakeClock, pool: BlockingPool
) -> None:
    gate = Gate("t1", "t2", "t3")
    bus.register("pre_tool_call", gate, owner="guard")

    async def go() -> Any:
        for tool in ("t1", "t2", "t3"):
            await timed_out(bus, fake_clock, gate, tool)
            fake_clock.advance(61)
        gate.release.set()
        await eventually(lambda: pool.busy == 0)
        return await bus.pre_tool_call("t5", {})

    try:
        after = asyncio.run(go())
    finally:
        gate.release.set()
    assert after.action is None
    assert gate.calls[-1] == "t5"


def test_async_pre_tool_call_timeout_is_cancelled_and_blocks(
    bus: HookBus, fake_clock: FakeClock
) -> None:
    cancelled: list[bool] = []

    async def forever(**_: Any) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    bus.register("pre_tool_call", forever, owner="slowpoke")

    async def go() -> Any:
        task = asyncio.ensure_future(bus.pre_tool_call("x", {}))
        await eventually(lambda: fake_clock.pending >= 1)
        fake_clock.advance(30)
        return await task

    directive = asyncio.run(go())
    assert directive.action == "block" and "timed out" in directive.message
    assert cancelled == [True]


def test_raising_pre_tool_call_blocks(bus: HookBus) -> None:
    def guard(**_: Any) -> None:
        raise RuntimeError("guard crashed")

    bus.register("pre_tool_call", guard, owner="guard")
    directive = asyncio.run(bus.pre_tool_call("x", {}))
    assert directive.action == "block" and "raised RuntimeError" in str(directive.message)


def test_raising_post_tool_call_is_ignored(bus: HookBus) -> None:
    seen: list[str] = []

    def crash(**_: Any) -> None:
        raise RuntimeError("observer crashed")

    def fine(tool_name: str, result: str, **_: Any) -> None:
        seen.append(f"{tool_name}:{result}")

    bus.register("post_tool_call", crash, owner="a")
    bus.register("post_tool_call", fine, owner="b")
    ran: list[Mapping[str, Any]] = []
    result = asyncio.run(
        dispatch(
            ToolCall("probe", {"q": 2}),
            ToolContext(),
            registry=recording_registry(ran),
            hooks=PluginToolHooks(bus),
        )
    )
    assert not result.is_error
    assert seen == ['probe:ran with {"q": 2}']


def test_hanging_observer_fails_open(bus: HookBus, fake_clock: FakeClock) -> None:
    release = threading.Event()
    bus.register("post_llm_call", lambda **_: release.wait(), owner="slow")

    async def go() -> list[Any]:
        task = asyncio.ensure_future(bus.invoke("post_llm_call", text="x"))
        await eventually(lambda: fake_clock.pending >= 1)
        fake_clock.advance(30)
        return await task

    try:
        assert asyncio.run(go()) == []
    finally:
        release.set()


def test_modify_merges_across_hooks(bus: HookBus) -> None:
    bus.register("pre_tool_call", lambda **_: {"action": "modify", "args": {"a": 1, "b": 1}})
    bus.register("pre_tool_call", lambda **_: {"action": "modify", "args": {"b": 2, "c": 3}})
    bus.register("pre_tool_call", lambda **_: {"action": "modify", "args": "not a mapping"})
    ran: list[Mapping[str, Any]] = []
    result = asyncio.run(
        dispatch(
            ToolCall("probe", {"q": 0, "b": 0}),
            ToolContext(),
            registry=recording_registry(ran),
            hooks=PluginToolHooks(bus),
        )
    )
    assert not result.is_error
    assert ran == [{"q": 0, "a": 1, "b": 2, "c": 3}]


def test_block_beats_approve_beats_modify(bus: HookBus) -> None:
    bus.register(
        "pre_tool_call", lambda **_: {"action": "approve", "message": "check", "rule_key": "k"}
    )
    bus.register("pre_tool_call", lambda **_: {"action": "modify", "args": {"z": 1}})
    bus.register("pre_tool_call", lambda **_: {"action": "block"})  # no message: ignored
    directive = asyncio.run(bus.pre_tool_call("x", {}))
    assert (directive.action, directive.message, directive.rule_key) == ("approve", "check", "k")
    assert directive.args == {"z": 1}
    bus.register("pre_tool_call", lambda **_: {"action": "block", "message": "no way"})
    directive = asyncio.run(bus.pre_tool_call("x", {}))
    assert (directive.action, directive.message) == ("block", "no way")


@pytest.mark.parametrize(
    ("answer", "blocked"),
    [(None, True), (True, False), (False, True), (RuntimeError("gate down"), True)],
)
def test_approve_goes_to_the_human_gate(bus: HookBus, answer: Any, blocked: bool) -> None:
    asked: list[tuple[str, str, str]] = []

    async def ask(tool_name: str, message: str, rule_key: str) -> bool:
        asked.append((tool_name, message, rule_key))
        if isinstance(answer, Exception):
            raise answer
        return bool(answer)

    bus.register("pre_tool_call", lambda **_: {"action": "approve", "message": "sure?"})
    hooks = PluginToolHooks(bus, ask_approval=None if answer is None else ask)
    ran: list[Mapping[str, Any]] = []
    result = asyncio.run(
        dispatch(
            ToolCall("probe", {}), ToolContext(), registry=recording_registry(ran), hooks=hooks
        )
    )
    assert result.is_error is blocked
    assert (ran == []) is blocked
    if answer is not None:
        assert asked == [("probe", "sure?", "probe")]


def test_pre_llm_call_goes_into_the_user_message(bus: HookBus) -> None:
    bus.register("pre_llm_call", lambda **_: "alpha", owner="one")
    bus.register("pre_llm_call", lambda **_: {"context": "beta"}, owner="two")
    bus.register("pre_llm_call", lambda **_: None, owner="three")
    bus.register("pre_llm_call", lambda **_: {"other": 1}, owner="four")
    context = asyncio.run(bus.pre_llm_call(session_id="s", user_message="hi"))
    assert context == "alpha\n\nbeta"
    system = {"role": "system", "content": "You are Indus Agent."}
    user = {"role": "user", "content": "hi"}
    system_bytes = json.dumps(system, sort_keys=True).encode()
    staged = with_llm_context(user, context)
    assert staged == {"role": "user", "content": "hi", "api_content": "hi\n\nalpha\n\nbeta"}
    assert json.dumps(system, sort_keys=True).encode() == system_bytes
    parts = with_llm_context({"role": "user", "content": [{"type": "text", "text": "look"}]}, "ctx")
    assert parts["api_content"] == [
        {"type": "text", "text": "look"},
        {"type": "text", "text": "ctx"},
    ]
    assert with_llm_context(user, None) == user


def test_transform_first_non_none_wins(bus: HookBus) -> None:
    calls: list[str] = []

    def first(**_: Any) -> None:
        calls.append("first")

    def second(**_: Any) -> str:
        calls.append("second")
        return "rewritten"

    def third(**_: Any) -> str:
        calls.append("third")
        return "ignored"

    for cb in (first, second, third):
        bus.register("transform_tool_result", cb)
    result = asyncio.run(
        dispatch(
            ToolCall("probe", {}),
            ToolContext(),
            registry=recording_registry([]),
            hooks=PluginToolHooks(bus),
        )
    )
    assert result.text == "rewritten"
    assert calls == ["first", "second", "third"]


def test_pre_transcription_last_writer_wins(bus: HookBus) -> None:
    bus.register("pre_transcription", lambda **_: {"prompt": "a", "language": "en"})
    bus.register("pre_transcription", lambda **_: {"prompt": "b", "file_path": "/elsewhere"})
    assert asyncio.run(bus.pre_transcription(file_path="/in.wav")) == {
        "prompt": "b",
        "language": "en",
    }


def test_error_classification_first_valid(bus: HookBus) -> None:
    bus.register("transform_api_error_classification", lambda **_: {"retryable": True})
    bus.register(
        "transform_api_error_classification",
        lambda **_: {
            "reason": "rate_limit",
            "retryable": True,
            "should_compress": "yes",
            "message": "m" * 600,
        },
    )
    bus.register("transform_api_error_classification", lambda **_: {"reason": "other"})
    found = asyncio.run(bus.classify_api_error(status_code=429))
    assert found == {"reason": "rate_limit", "retryable": True, "message": "m" * 500}


def test_verify_and_gateway_directives(bus: HookBus) -> None:
    bus.register("pre_verify", lambda **_: {"action": "stop"})
    bus.register("pre_verify", lambda **_: {"action": "continue", "message": " run the tests "})
    assert asyncio.run(bus.pre_verify(attempt=0)) == "run the tests"
    bus.register("pre_gateway_dispatch", lambda **_: "allow")
    bus.register("pre_gateway_dispatch", lambda **_: {"action": "rewrite", "text": "hello"})
    assert asyncio.run(bus.pre_gateway_dispatch(event="e")) == {
        "action": "rewrite",
        "text": "hello",
    }


def test_narrow_signatures_get_only_what_they_declare(bus: HookBus) -> None:
    seen: list[Any] = []

    def narrow(tool_name: str) -> None:
        seen.append(("narrow", tool_name))

    def wide(**kwargs: Any) -> None:
        seen.append(("wide", sorted(kwargs)))

    bus.register("post_tool_call", narrow)
    bus.register("post_tool_call", wide)
    asyncio.run(bus.observe("post_tool_call", tool_name="t", args={}, result="r"))
    assert seen == [("narrow", "t"), ("wide", ["args", "result", "tool_name"])]


def test_signature_mismatch_warned_once(bus: HookBus, caplog: pytest.LogCaptureFixture) -> None:
    def wrong(tool_data: str) -> None:
        return None

    bus.register("post_tool_call", wrong, owner="old")
    with caplog.at_level(logging.DEBUG, logger="indus.plugins.hooks"):
        for _ in range(3):
            asyncio.run(bus.observe("post_tool_call", tool_name="t"))
    warnings = [
        r for r in caplog.records if r.levelno == logging.WARNING and "wrong" in r.getMessage()
    ]
    debugs = [
        r for r in caplog.records if r.levelno == logging.DEBUG and "raised again" in r.getMessage()
    ]
    assert len(warnings) == 1 and "provides: tool_name" in warnings[0].getMessage()
    assert len(debugs) == 2


def test_unknown_hook_warns_once_and_is_kept(
    bus: HookBus, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        bus.register("on_full_moon", lambda **_: "howl", owner="wolf")
        bus.register("on_full_moon", lambda **_: "again", owner="wolf")
    assert caplog.text.count("on_full_moon") == 1
    assert asyncio.run(bus.invoke("on_full_moon")) == ["howl", "again"]


def test_subagent_stop_runs_in_the_caller(bus: HookBus) -> None:
    threads: dict[str, str] = {}

    def stop(**_: Any) -> None:
        threads["subagent_stop"] = threading.current_thread().name

    def tool(**_: Any) -> None:
        threads["pre_tool_call"] = threading.current_thread().name

    bus.register("subagent_stop", stop)
    bus.register("pre_tool_call", tool)

    async def go() -> None:
        await bus.observe("subagent_stop", child="c")
        await bus.pre_tool_call("x", {})

    asyncio.run(go())
    assert threads["subagent_stop"] == threading.main_thread().name
    assert threads["pre_tool_call"].startswith("indus-hooks-test")


def test_full_pool_fails_closed_for_pre_tool_and_open_for_observers(fake_clock: FakeClock) -> None:
    tiny = BlockingPool("hooks-tiny", 1)
    bus = HookBus(clock=fake_clock, pool=tiny)
    release = threading.Event()
    started = threading.Event()

    def occupy() -> None:
        started.set()
        release.wait()

    bus.register("pre_tool_call", lambda **_: None, owner="g")
    bus.register("post_tool_call", lambda **_: "seen", owner="o")

    async def go() -> tuple[Any, list[Any]]:
        busy = asyncio.ensure_future(tiny.run(occupy))
        await eventually(started.is_set)
        directive = await bus.pre_tool_call("x", {})
        observed = await bus.invoke("post_tool_call")
        release.set()
        await busy
        return directive, observed

    try:
        directive, observed = asyncio.run(go())
    finally:
        release.set()
        tiny.shutdown()
    assert directive.action == "block" and "no hook worker was free" in directive.message
    assert observed == []


def test_zero_timeout_means_no_deadline(fake_clock: FakeClock, pool: BlockingPool) -> None:
    bus = HookBus(clock=fake_clock, pool=pool, timeout_s=0)
    bus.register("pre_tool_call", lambda **_: {"action": "modify", "args": {"x": 1}})
    directive = asyncio.run(bus.pre_tool_call("t", {}))
    assert directive.args == {"x": 1}
    assert fake_clock.pending == 0


def test_seam_is_empty_until_a_tool_hook_exists(bus: HookBus) -> None:
    hooks = PluginToolHooks(bus)
    assert hooks.empty
    bus.register("post_llm_call", lambda **_: None)
    assert hooks.empty
    handle = bus.register("transform_tool_result", lambda **_: None)
    assert not hooks.empty
    handle.dispose()
    assert hooks.empty


def test_drop_owner_removes_hooks_and_listeners(bus: HookBus) -> None:
    bus.register("pre_tool_call", lambda **_: None, owner="gone")
    bus.register("pre_tool_call", lambda **_: None, owner="kept")
    bus.events.listen("indus.x", lambda **_: None, owner="gone")
    assert bus.drop_owner("gone") == 2
    assert bus.names() == ["pre_tool_call"]
    assert bus.names(owner="gone") == []


# -- event bus ----------------------------------------------------------------------------------


def test_emit_depth_nine_refused(pool: BlockingPool, caplog: pytest.LogCaptureFixture) -> None:
    events = EventBus(pool=pool)
    deliveries: list[int] = []

    def again(n: int) -> None:
        deliveries.append(n)
        events.emit("demo.loop", {"n": n + 1})

    events.listen("demo.loop", again)

    async def go() -> None:
        assert events.emit("demo.loop", {"n": 1}) == 1
        await eventually(lambda: len(deliveries) >= 8 and events.pending == 0)

    with caplog.at_level(logging.WARNING):
        asyncio.run(go())
    assert deliveries == [1, 2, 3, 4, 5, 6, 7, 8]
    assert "9 levels deep" in caplog.text


def test_pending_cap(pool: BlockingPool) -> None:
    events = EventBus(pool=pool)
    got: list[int] = []

    async def go() -> list[int]:
        gate = asyncio.Event()

        async def slow(n: int) -> None:
            await gate.wait()
            got.append(n)

        events.listen("demo.slow", slow)
        counts = [events.emit("demo.slow", {"n": n}) for n in range(70)]
        gate.set()
        assert await events.drain()
        return counts

    counts = asyncio.run(go())
    assert counts == [1] * 64 + [0] * 6
    assert sorted(got) == list(range(64))


def test_listeners_get_their_own_copy(pool: BlockingPool) -> None:
    events = EventBus(pool=pool)
    seen: list[Any] = []

    async def mutate(data: dict[str, Any]) -> None:
        data["x"] = "changed"
        seen.append(data)

    async def read(data: dict[str, Any]) -> None:
        seen.append(dict(data))

    events.listen("indus.e", mutate)
    events.listen("indus.e", read)

    async def go() -> None:
        events.emit("indus.e", {"data": {"x": "original"}})
        assert await events.drain()

    asyncio.run(go())
    assert seen == [{"x": "changed"}, {"x": "original"}]


def test_emit_without_listeners_or_loop(pool: BlockingPool) -> None:
    events = EventBus(pool=pool)
    assert events.emit("nobody.listens") == 0
    events.listen("demo.x", lambda **_: None)
    assert events.emit("demo.x") == 0  # no loop has run the bus yet: dropped
