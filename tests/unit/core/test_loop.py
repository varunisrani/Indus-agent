"""The turn loop and its single exit funnel (P04-15)."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Callable, Mapping
from typing import Any

import pytest

from indus.core import loop_texts as texts
from indus.core import wire
from indus.core.budget import IterationBudget, max_iterations_from
from indus.core.exit_reasons import FAILING, ExitReason
from indus.core.guardrails import GuardrailConfig
from indus.core.persist import MemoryStore
from indus.core.turn_state import LoopOptions, TurnResult, TurnState
from indus.foundation.scope import Scope
from indus.protocol.events import MessageDelta, ToolStarted, TurnDone
from indus.providers.streaming import RouteChanged
from indus.providers.types import ChatRequest
from tests.fakes.clock import FakeClock
from tests.fakes.model import (
    Fail,
    Turn,
    load_script,
    malformed_args,
    text_turn,
    thinking_only,
    tool_batch,
)
from tests.fakes.store import RecordingStore
from tests.fakes.turn import TurnKit, check_alternation


def kit(scope: Scope, clock: FakeClock, turns: list[Turn], **kw: Any) -> TurnKit:
    return TurnKit(scope, clock, turns, **kw)


def assert_stamped(result: TurnResult, message: str) -> None:
    assert result.turn_id.startswith("turn-")
    user = result.rows[result.current_turn_user_index]
    assert user.role == "user" and message in str(user.content)


def test_plain_answer(scope: Scope, fake_clock: FakeClock) -> None:
    k = kit(scope, fake_clock, list(load_script("hello").turns))
    result = asyncio.run(k.turn("hi"))
    assert result.exit_reason is ExitReason.COMPLETED and result.final_text == "hello"
    assert result.failure is None and result.api_calls == 1
    assert_stamped(result, "hi")
    body = k.server.model_requests[0].body
    assert body["messages"][0] == {"role": "system", "content": "You are the test agent."}
    assert body["messages"][-1] == {"role": "user", "content": "hi"}
    assert [r.role for r in result.rows] == ["user", "assistant"]


def test_tool_loop_from_the_fixture_script(scope: Scope, fake_clock: FakeClock) -> None:
    k = kit(scope, fake_clock, list(load_script("tool_then_text").turns))

    async def reader(args: Mapping[str, Any]) -> str:
        return f"contents of {args.get('path')}"

    async def search(args: Mapping[str, Any]) -> str:
        return "found it"

    schema = {"parameters": {"type": "object"}}
    k.registry.register("read_file", "file", schema, reader)
    k.registry.register("search", "web", schema, search)
    result = asyncio.run(k.turn("when do we ship?"))
    assert result.exit_reason is ExitReason.COMPLETED
    assert result.final_text == "The notes say ship on Friday."
    assert result.api_calls == 3
    assert result.reasoning == "Need the file first."
    check_alternation(result.rows)
    first_call = next(r for r in result.rows if r.tool_calls)
    assert first_call.content == "Let me look at the notes."


def test_cap_reached_gets_one_toolless_grace_call(scope: Scope, fake_clock: FakeClock) -> None:
    turns = [tool_batch(("echo", {"text": str(n)})) for n in range(150)]
    turns.append(text_turn("<think>wrap up</think>I echoed 150 times."))
    k = kit(scope, fake_clock, turns)
    result = asyncio.run(k.turn("loop"))
    assert result.exit_reason is ExitReason.MAX_ITERATIONS
    assert result.final_text == "I echoed 150 times."
    assert len(k.server.model_requests) == 151
    grace = k.server.model_requests[-1].body
    assert "tools" not in grace
    assert grace["messages"][-1]["content"] == texts.GRACE_NUDGE
    assert all(texts.GRACE_NUDGE not in str(r.content) for r in result.rows)
    check_alternation(result.rows)


def test_empty_grace_twice_gives_the_limit_reply(scope: Scope, fake_clock: FakeClock) -> None:
    turns = [tool_batch(("echo", {})), tool_batch(("echo", {"x": 1})), text_turn(), text_turn()]
    k = kit(scope, fake_clock, turns, options=LoopOptions(max_iterations=2))
    result = asyncio.run(k.turn("loop"))
    assert result.exit_reason is ExitReason.MAX_ITERATIONS
    assert result.final_text == texts.LIMIT_REPLY.format(limit="2")
    assert len(k.server.model_requests) == 4
    assert result.rows[-1].content == result.final_text
    check_alternation(result.rows)


def test_shared_budget_exhausted(scope: Scope, fake_clock: FakeClock) -> None:
    k = kit(scope, fake_clock, [text_turn("summary only")])

    def spent(state: TurnState) -> None:
        state.budget = IterationBudget(1)
        state.budget.consume()

    result = asyncio.run(k.turn("go", prepare=spent))
    assert result.exit_reason is ExitReason.BUDGET_EXHAUSTED
    assert result.final_text == "summary only"


def test_unlimited_cap(scope: Scope, fake_clock: FakeClock) -> None:
    turns = [tool_batch(("echo", {"n": n})) for n in range(5)] + [text_turn("done")]
    k = kit(scope, fake_clock, turns, options=LoopOptions(max_iterations=0))
    result = asyncio.run(k.turn("go"))
    assert result.exit_reason is ExitReason.COMPLETED and result.api_calls == 6
    assert max_iterations_from(0) == 0 and max_iterations_from(-1) == 150
    assert max_iterations_from(True) == 150 and IterationBudget(0).remaining == float("inf")


class RaisingCaller:
    def __init__(self, inner: Any, times: int, error: BaseException) -> None:
        self.inner = inner
        self.times = times
        self.error = error
        self.calls = 0

    @property
    def model(self) -> str:
        return self.inner.model

    async def call(self, request: ChatRequest, **kw: Any) -> Any:
        self.calls += 1
        if self.calls <= self.times:
            raise self.error
        return await self.inner.call(request, **kw)


def test_eight_escaped_errors_fail_the_turn(scope: Scope, fake_clock: FakeClock) -> None:
    k = kit(scope, fake_clock, [text_turn("never")])

    def flaky(state: TurnState) -> None:
        state.runtime.caller = RaisingCaller(state.runtime.caller, 99, RuntimeError("glitch"))

    result = asyncio.run(k.turn("go", prepare=flaky))
    assert result.exit_reason is ExitReason.PROVIDER_FAILED
    assert result.failure is not None and result.failure.reason == "loop_error"
    assert result.failure.retryable and result.api_calls == 8


def test_seven_escapes_then_success(scope: Scope, fake_clock: FakeClock) -> None:
    k = kit(scope, fake_clock, [text_turn("finally")])

    def flaky(state: TurnState) -> None:
        state.runtime.caller = RaisingCaller(state.runtime.caller, 7, RuntimeError("glitch"))

    result = asyncio.run(k.turn("go", prepare=flaky))
    assert result.exit_reason is ExitReason.COMPLETED and result.final_text == "finally"


def test_a_local_processing_bug_is_not_retried(
    scope: Scope, fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object, **kw: object) -> object:
        raise TypeError("cannot shape this history")

    monkeypatch.setattr(wire, "build_wire_copy", broken)
    k = kit(scope, fake_clock, [text_turn("never")])
    result = asyncio.run(k.turn("go"))
    assert result.exit_reason is ExitReason.PROVIDER_FAILED
    assert result.failure is not None and result.failure.reason == "local_error"
    assert not result.failure.retryable and result.api_calls == 1


def test_open_calls_are_closed_when_an_error_escapes(
    scope: Scope, fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    import indus.core.tool_round as round_module

    async def explode(*args: object, **kw: object) -> None:
        raise RuntimeError("executor died")

    monkeypatch.setattr(round_module, "_execute", explode)
    k = kit(scope, fake_clock, [tool_batch(("echo", {}))], options=LoopOptions(max_outer_errors=1))
    result = asyncio.run(k.turn("go"))
    assert result.exit_reason is ExitReason.PROVIDER_FAILED
    check_alternation(result.rows)
    assert isinstance(k.store, MemoryStore)
    check_alternation(k.store.rows["s1"])


class RouteMoving:
    def __init__(self, inner: Any) -> None:
        self.inner = inner

    @property
    def model(self) -> str:
        return self.inner.model

    async def call(self, request: ChatRequest, **kw: Any) -> Any:
        return RouteChanged(None, 1, 1)


def test_restart_cap(scope: Scope, fake_clock: FakeClock) -> None:
    k = kit(scope, fake_clock, [text_turn("never")])

    def moving(state: TurnState) -> None:
        state.runtime.caller = RouteMoving(state.runtime.caller)

    result = asyncio.run(k.turn("go", prepare=moving))
    assert result.exit_reason is ExitReason.PROVIDER_FAILED
    assert result.failure is not None and result.failure.reason == "restart_limit"
    assert result.api_calls == 0  # every restart was given back
    assert k.last_state is not None and k.last_state.restart_count == 9


def test_provider_failure_carries_the_classifier_verdict(
    scope: Scope, fake_clock: FakeClock
) -> None:
    k = kit(scope, fake_clock, [Turn((Fail(401, '{"error": {"message": "bad key"}}'),))])
    result = asyncio.run(k.turn("go"))
    assert result.exit_reason is ExitReason.PROVIDER_FAILED
    assert result.failure is not None and result.failure.reason == "auth"
    assert result.failure.retryable is False
    check_alternation(result.rows)


def test_rate_limit_after_retries_is_retryable(scope: Scope, fake_clock: FakeClock) -> None:
    fail = Turn((Fail(429, '{"error": {"message": "slow down"}}', {"retry-after": "2"}),))
    k = kit(scope, fake_clock, [fail] * 4)
    result = asyncio.run(k.turn("go"))
    assert result.exit_reason is ExitReason.PROVIDER_FAILED
    assert result.failure is not None and result.failure.retryable
    assert len(k.server.model_requests) == 4


def test_length_ladder_continues_and_stitches(scope: Scope, fake_clock: FakeClock) -> None:
    turns = [text_turn("The answer ", finish="length"), text_turn("is 42.")]
    k = kit(scope, fake_clock, turns)
    result = asyncio.run(k.turn("q"))
    assert result.exit_reason is ExitReason.COMPLETED
    assert result.final_text == "The answer is 42."
    assert [r.role for r in result.rows] == ["user", "assistant"]
    second = k.server.model_requests[1].body["messages"]
    assert second[-1]["role"] == "user" and "cut off" in second[-1]["content"]


def test_empty_after_tools_gets_one_nudge(scope: Scope, fake_clock: FakeClock) -> None:
    turns = [tool_batch(("echo", {})), text_turn(), text_turn("now with words")]
    k = kit(scope, fake_clock, turns)
    result = asyncio.run(k.turn("q"))
    assert result.final_text == "now with words"
    check_alternation(result.rows)
    assert all(r.content != "(empty)" for r in result.rows)


def test_thinking_only_is_fed_back_as_prefill(scope: Scope, fake_clock: FakeClock) -> None:
    turns = [thinking_only("plan it"), text_turn("planned")]
    k = kit(scope, fake_clock, turns)
    result = asyncio.run(k.turn("q"))
    assert result.final_text == "planned"
    assert [r.role for r in result.rows] == ["user", "assistant"]


def test_intent_ack_gets_a_nudge(scope: Scope, fake_clock: FakeClock) -> None:
    turns = [text_turn("I'll check the file now."), text_turn("It says hello.")]
    k = kit(scope, fake_clock, turns)
    result = asyncio.run(k.turn("q"))
    assert result.final_text == "It says hello."
    assert len(k.server.model_requests) == 2


def test_one_log_line_per_turn(
    scope: Scope, fake_clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    k = kit(scope, fake_clock, [text_turn("hi"), Turn((Fail(401, "{}"),))])
    with caplog.at_level(logging.INFO, logger="indus.core.finalize"):
        asyncio.run(k.turn("a"))
        asyncio.run(k.turn("b"))
    lines = [r for r in caplog.records if r.message.startswith("turn ended")]
    assert [r.levelno for r in lines] == [logging.INFO, logging.WARNING]


# -- every exit path through the funnel ------------------------------------------------------------


def _interrupt_soon(state: TurnState) -> None:
    asyncio.get_running_loop().call_soon(state.control.interrupt, state.generation)


def _system_interrupt_soon(state: TurnState) -> None:
    loop = asyncio.get_running_loop()
    loop.call_soon(lambda: state.control.interrupt(state.generation, by_system=True))


def _spend_budget(state: TurnState) -> None:
    state.budget = IterationBudget(1)
    state.budget.consume()


def _scenario(reason: ExitReason, scope: Scope, clock: FakeClock) -> tuple[TurnKit, Any]:
    prepare: Callable[[TurnState], None] | None = None
    store = None
    options = LoopOptions()
    match reason:
        case ExitReason.COMPLETED:
            turns = [text_turn("fine")]
        case ExitReason.INTERRUPTED_BY_USER:
            turns = [text_turn("never")]
            prepare = _interrupt_soon
        case ExitReason.INTERRUPTED_BY_SYSTEM:
            turns = [text_turn("never")]
            prepare = _system_interrupt_soon
        case ExitReason.BUDGET_EXHAUSTED:
            turns = [text_turn("summary")]
            prepare = _spend_budget
        case ExitReason.MAX_ITERATIONS:
            turns = [tool_batch(("echo", {})), text_turn("summary")]
            options = LoopOptions(max_iterations=1)
        case ExitReason.INVALID_TOOL_STRIKES:
            turns = [tool_batch(("nope", {}))] * 3
        case ExitReason.PERSISTENCE_FAILED:
            turns = [text_turn("never")]
            store = RecordingStore(MemoryStore())
            store.fail_at("user")
        case ExitReason.PROVIDER_FAILED:
            turns = [Turn((Fail(400, '{"error": {"message": "bad request"}}'),))]
        case ExitReason.COMPRESSION_EXHAUSTED:
            turns = [load_script("mid_stream_overflow").turns[0]]
        case ExitReason.GUARDRAIL_HALT:
            turns = [tool_batch(("web_search", {"q": 1}), ("web_search", {"q": 2}))]
            options = LoopOptions(guardrails=GuardrailConfig(max_web_searches=1))
    k = kit(scope, clock, turns, store=store, options=options)
    return k, prepare


@pytest.mark.parametrize("reason", list(ExitReason))
def test_every_exit_passes_the_funnel(
    reason: ExitReason, scope: Scope, fake_clock: FakeClock
) -> None:
    k, prepare = _scenario(reason, scope, fake_clock)
    result = asyncio.run(k.turn("the message", prepare=prepare))
    assert result.exit_reason is reason, (result.exit_reason, result.failure, result.final_text)
    assert_stamped(result, "the message")
    if reason in FAILING:
        assert result.failure is not None and result.failure.reason
        assert isinstance(result.failure.retryable, bool)
        assert result.failure_reason == result.failure.reason
    done = [e for e in k.events if isinstance(e, TurnDone)]
    assert len(done) == 1 and done[0].reason is reason
    assert (done[0].failure is not None) == (result.failure is not None)
    check_alternation(result.rows)
    assert k.last_state is not None and k.last_state.control.phase == "done"


def test_malformed_args_count_as_iterations(scope: Scope, fake_clock: FakeClock) -> None:
    turns = [malformed_args("echo"), text_turn("ok")]
    k = kit(scope, fake_clock, turns)
    result = asyncio.run(k.turn("go"))
    assert result.api_calls == 2 and result.final_text == "ok"


# -- seeded random turns (property table; hypothesis is not installed) ----------------------------


def _random_turn(rng: random.Random, n: int) -> Turn:
    kind = rng.randrange(7)
    if kind == 0:
        return text_turn(f"answer {n}")
    if kind == 1:
        return text_turn(f"cut {n} ", finish="length")
    if kind == 2:
        return text_turn()
    if kind == 3:
        return malformed_args("echo")
    if kind == 4:
        return thinking_only("hmm")
    tools = ["echo", "effect", "slow", "boom", "lookup", "nope", "web_search"]
    calls = []
    for i in range(rng.randint(1, 3)):
        name = rng.choice(tools)
        args: dict[str, object] = {"seconds": rng.randint(1, 20), "tag": str(i)}
        calls.append((name, args))
    return tool_batch(*calls)


@pytest.mark.parametrize("seed", range(40))
def test_random_turns_always_alternate(seed: int, scope: Scope, fake_clock: FakeClock) -> None:
    rng = random.Random(seed)
    turns = [_random_turn(rng, n) for n in range(10)] + [text_turn("wrap")] * 4
    store = MemoryStore()
    k = kit(scope, fake_clock, turns, store=store, options=LoopOptions(max_iterations=8))
    fired: list[str] = []

    def chaos(event: object, state: TurnState) -> None:
        roll = rng.random()
        control = state.control
        if isinstance(event, ToolStarted) and roll < 0.15:
            fired.append("interrupt")
            control.interrupt(state.generation, hard=rng.random() < 0.5)
        elif isinstance(event, ToolStarted) and roll < 0.35:
            fired.append("steer")
            control.steer(f"steer {roll:.2f}")
        elif isinstance(event, MessageDelta) and roll < 0.1:
            fired.append("redirect")
            asyncio.get_running_loop().call_soon(control.redirect, "redirected")

    result = asyncio.run(k.turn("start", on_event=chaos))
    assert result.exit_reason in set(ExitReason)
    check_alternation(result.rows, user_index=result.current_turn_user_index)
    check_alternation(store.rows["s1"], user_index=result.current_turn_user_index)
    nxt = asyncio.run(k.turn("again"))
    check_alternation(store.rows["s1"], user_index=nxt.current_turn_user_index)
