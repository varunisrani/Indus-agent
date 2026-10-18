"""The P08 seams of the agent: usage reaches the context engine through the ledger only, the
auxiliary client is bound to the session, a rotating compaction moves the agent, and a response
the provider cancelled ends the turn as interrupted."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from indus.core.agent import Agent
from indus.core.compression.engine import BuiltinEngine
from indus.core.exit_reasons import ExitReason
from indus.core.persist import MemoryStore, StateStore
from indus.core.turn_state import TurnState
from indus.foundation.scope import Scope
from indus.providers.auxiliary import AuxClient
from indus.providers.classify import ClassifiedError
from indus.providers.retry import RetryFailed
from indus.providers.transports.responses import ResponseCancelled
from indus.providers.types import ChatRequest, Usage
from indus.state import registry
from tests.fakes.agent import DictConfig, fake_options, run_driven
from tests.fakes.clock import FakeClock
from tests.fakes.model import (
    FakeModelServer,
    Finish,
    Text,
    Turn,
    fake_route,
    script_of,
    text_turn,
)
from tests.fakes.model import Usage as UsageStep
from tests.fakes.turn import TurnKit, check_alternation
from tests.unit.core.compression.test_engine import history, is_handoff


def usage_turn(text: str, prompt: int, completion: int = 5) -> Turn:
    return Turn((Text((text,)), UsageStep(prompt, completion), Finish("stop")))


def server_for(clock: FakeClock, *turns: Turn) -> FakeModelServer:
    return FakeModelServer(script_of(*turns), clock=clock)


def engine_of(agent: Agent) -> BuiltinEngine:
    engine = agent.context_engine
    assert isinstance(engine, BuiltinEngine)
    return engine


def test_usage_reaches_the_engine_through_the_ledger(scope: Scope, fake_clock: FakeClock) -> None:
    server = server_for(fake_clock, usage_turn("hi", 1_234))

    async def main() -> Agent:
        agent = await Agent.start(scope, fake_route(server), [], fake_options(server))
        await agent.run_turn("hello")
        return agent

    agent = asyncio.run(run_driven(main(), fake_clock))
    engine = engine_of(agent)
    assert agent.usage.snapshot().api_calls == 1
    assert engine.meter.last_real_prompt == 1_234 and engine.meter.anchor is not None
    # a side task's usage is the ledger's, never the transcript's anchor
    asyncio.run(
        agent.usage.record(
            Usage(prompt_tokens=99_999), route=agent.binding.state.route, latency_s=0, task="title"
        )
    )
    assert engine.meter.last_real_prompt == 1_234
    # a call nobody announced (no request of the loop) leaves the meter alone too
    asyncio.run(
        agent.usage.record(Usage(prompt_tokens=7), route=agent.binding.state.route, latency_s=0)
    )
    assert engine.meter.last_real_prompt == 1_234
    asyncio.run(agent.close())


def test_usage_less_answers_reach_the_meter(scope: Scope, fake_clock: FakeClock) -> None:
    server = server_for(fake_clock, text_turn("no usage here"))

    async def main() -> Agent:
        agent = await Agent.start(scope, fake_route(server), [], fake_options(server))
        await agent.run_turn("hello")
        return agent

    agent = asyncio.run(run_driven(main(), fake_clock))
    assert engine_of(agent).meter.usage_less
    asyncio.run(agent.close())


def test_aux_client_is_bound_to_the_session(scope: Scope, fake_clock: FakeClock) -> None:
    server = server_for(fake_clock, text_turn("ok"))

    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "side"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 30, "completion_tokens": 2},
            },
        )

    host_aux = AuxClient(
        scope,
        fake_route(server),
        client_factory=lambda _r: httpx.AsyncClient(transport=httpx.MockTransport(answer)),
    )

    async def main() -> Agent:
        agent = await Agent.start(scope, fake_route(server), [], fake_options(server, aux=host_aux))
        assert agent.aux is not None and agent.aux is not host_aux
        assert agent.aux.main is agent.binding.state.route
        await agent.aux.call("compression", [{"role": "user", "content": "x"}])
        return agent

    agent = asyncio.run(run_driven(main(), fake_clock))
    snap = agent.usage.snapshot()
    assert snap.side_calls == {"compression": 1} and snap.api_calls == 0
    asyncio.run(agent.close())


def test_main_route_summary_is_recorded_under_the_compression_tag(
    scope: Scope, fake_clock: FakeClock
) -> None:
    server = server_for(fake_clock, usage_turn("## Goal\nmain-route notes", 900))
    store = MemoryStore()

    async def main() -> Agent:
        agent = await Agent.start(scope, fake_route(server), [], fake_options(server, store=store))
        await store.ensure_session("s1", source="cli", system_prompt="p", fields={})
        for row in history():
            await store.append("s1", row)
        outcome = await agent.compress()
        assert outcome.status == "compressed", outcome
        return agent

    agent = asyncio.run(run_driven(main(), fake_clock))
    snap = agent.usage.snapshot()
    assert snap.side_calls == {"compression": 1} and snap.api_calls == 0
    assert engine_of(agent).meter.last_real_prompt == 0  # the summary is not the transcript
    asyncio.run(agent.close())


def test_rotating_compaction_moves_the_agent(scope: Scope, fake_clock: FakeClock) -> None:
    rotating = scope.with_config(DictConfig({"compression": {"in_place": False}}))
    server = server_for(fake_clock, text_turn("## Goal\nnotes"), text_turn("carried on"))
    handle = registry.open_unshared(scope)
    store = StateStore(handle)
    moved: list[tuple[str, str]] = []

    async def main() -> tuple[Agent, str]:
        agent = await Agent.start(
            rotating, fake_route(server), [], fake_options(server, store=store)
        )
        agent.on_session_moved(lambda old, new: moved.append((old, new)))
        await store.ensure_session("s1", source="cli", system_prompt="p", fields={})
        for row in history():
            await store.append("s1", row)
        outcome = await agent.compress()
        assert outcome.status == "compressed", outcome
        child = agent.session_id
        result = await agent.run_turn("next step")
        assert result.session_id == child and result.exit_reason is ExitReason.COMPLETED
        return agent, child

    try:
        agent, child = asyncio.run(run_driven(main(), fake_clock))
        assert child != "s1" and moved == [("s1", child)]
        assert agent.usage.session_id == child and agent.runtime.session_id == child
        assert engine_of(agent).meter.session_id == child
        loaded = asyncio.run(store.load(child))
        assert sum(1 for r in loaded if is_handoff(r)) == 1
        assert loaded[-1].content == "carried on"
        check_alternation(loaded)
        asyncio.run(agent.close())
    finally:
        handle.close()


def _cancelled() -> ResponseCancelled:
    return ResponseCancelled(
        "the provider cancelled this response", body={"error": {"code": "response_cancelled"}}
    )


class _CancellingCaller:
    """Raises what the Responses transport raises for ``status: cancelled`` (P08-09), bare or
    after P03's retry gave up on it."""

    model = "test-model"

    def __init__(self, wrapped: bool) -> None:
        self.wrapped = wrapped

    async def call(self, request: ChatRequest, **_: Any) -> Any:
        error = _cancelled()
        if self.wrapped:
            raise RetryFailed(ClassifiedError("unknown"), error=error, attempts=1, waits=())
        raise error


@pytest.mark.parametrize("wrapped", [False, True], ids=["bare", "after-retry"])
def test_provider_cancelled_response_ends_the_turn_interrupted(
    scope: Scope, fake_clock: FakeClock, wrapped: bool
) -> None:
    kit = TurnKit(scope, fake_clock, [text_turn("never")])

    def prepare(state: TurnState) -> None:
        state.runtime.caller = _CancellingCaller(wrapped)

    result = asyncio.run(kit.turn("hello", prepare=prepare))
    assert result.exit_reason is ExitReason.INTERRUPTED_BY_SYSTEM
    assert result.failure is None and result.api_calls == 1
