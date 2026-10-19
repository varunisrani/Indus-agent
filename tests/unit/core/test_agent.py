"""The agent object and its event sink (P04-20)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from indus.core.agent import PHASES, Agent, AgentBusy, AgentClosed
from indus.core.cleanup import CleanupRegistry
from indus.core.events import EventSink, as_sink
from indus.core.exit_reasons import ExitReason
from indus.core.turn_state import TurnResult
from indus.foundation.scope import Scope, current_scope, make_scope, use_scope
from indus.protocol.events import (
    EventBase,
    MessageDelta,
    NoticeLevel,
    Status,
    StatusKind,
    ToolCompleted,
    ToolStarted,
    TurnDone,
    TurnStarted,
)
from indus.providers.retry import DEFAULT_MAX_RETRIES
from indus.providers.route import RuntimeRoute
from indus.tools.registry import ToolRegistry
from tests.fakes.agent import DictConfig, eventually, fake_options, run_driven
from tests.fakes.clock import FakeClock, checkpoint
from tests.fakes.model import (
    Fail,
    FakeModelServer,
    Finish,
    Stall,
    Text,
    Turn,
    fake_route,
    script_of,
    text_turn,
    tool_batch,
)
from tests.fakes.tools import FakeTools
from tests.fakes.turn import check_alternation


def server_for(clock: FakeClock, *turns: Turn) -> FakeModelServer:
    return FakeModelServer(script_of(*turns), clock=clock)


def stalled(text: str) -> Turn:
    return Turn((Text((text,)), Stall(1000.0), Finish("stop")))


async def open_agent(
    scope: Scope,
    server: FakeModelServer,
    *,
    selection: list[str] | None = None,
    sink: object = None,
    **values: object,
) -> Agent:
    return await Agent.start(
        scope,
        fake_route(server),
        selection if selection is not None else [],
        fake_options(server, **values),  # type: ignore[arg-type]
        sink,  # type: ignore[arg-type]
    )


# -- construction ----------------------------------------------------------------------------------


def test_phases_run_in_the_documented_order(scope: Scope, fake_clock: FakeClock) -> None:
    async def main() -> Agent:
        agent = await open_agent(scope, server_for(fake_clock, text_turn("hi")))
        await agent.open()  # a second open changes nothing
        await agent.close()
        return agent

    agent = asyncio.run(main())
    assert agent.phases == list(PHASES)
    assert agent.context_engine is not None
    assert agent.runtime.context is agent.context_engine
    assert agent.runtime.options.context_length == 200_000
    assert agent.session_id == "s1"


def test_api_max_retries_reaches_the_model_caller(scope: Scope, fake_clock: FakeClock) -> None:
    configured = scope.with_config(DictConfig({"agent": {"api_max_retries": 5}}))

    async def main() -> tuple[int, int, int]:
        server = server_for(fake_clock, text_turn("hi"))
        from_config = await open_agent(configured, server)
        default = await open_agent(scope, server)
        explicit = await open_agent(configured, server, max_retries=7)
        caller = from_config.caller.caller_for(from_config.binding.state, 0)
        for agent in (from_config, default, explicit):
            await agent.close()
        return caller.max_retries, default.caller.max_retries, explicit.caller.max_retries

    assert asyncio.run(main()) == (5, DEFAULT_MAX_RETRIES, 7)


def test_a_new_session_id_is_minted_when_none_is_given(scope: Scope, fake_clock: FakeClock) -> None:
    async def main() -> str:
        agent = await open_agent(scope, server_for(fake_clock), session_id=None)
        await agent.close()
        return agent.session_id

    minted = asyncio.run(main())
    assert minted.startswith(f"{fake_clock.now():%Y%m%d_}")


# -- events ----------------------------------------------------------------------------------------


def test_deltas_arrive_in_order_with_growing_seq(scope: Scope, fake_clock: FakeClock) -> None:
    seen: list[EventBase] = []

    async def main() -> str:
        agent = await open_agent(
            scope, server_for(fake_clock, text_turn("Hel", "lo", " there")), sink=seen.append
        )
        text = await run_driven(agent.chat("hi"), fake_clock)
        await agent.close()
        return text

    assert asyncio.run(main()) == "Hello there"
    assert isinstance(seen[0], TurnStarted) and isinstance(seen[-1], TurnDone)
    deltas = [e.text for e in seen if isinstance(e, MessageDelta)]
    assert "".join(deltas) == "Hello there"
    seqs = [e.seq for e in seen]
    assert seqs == sorted(set(seqs))
    assert all(e.session_id == "s1" for e in seen)


def test_a_raising_sink_never_fails_the_turn(
    scope: Scope, fake_clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(event: EventBase) -> None:
        raise RuntimeError("screen is gone")

    async def main() -> tuple[ExitReason, str | None, int]:
        agent = await open_agent(
            scope, server_for(fake_clock, text_turn("still fine")), sink=broken
        )
        result = await run_driven(agent.run_turn("hi"), fake_clock)
        await agent.close()
        return result.exit_reason, result.final_text, agent.sink.failures

    reason, text, failures = asyncio.run(main())
    assert reason is ExitReason.COMPLETED and text == "still fine"
    assert failures >= 3
    assert "event listener failed" in caplog.text


def test_sink_helpers_build_typed_events_and_replay() -> None:
    got: list[EventBase] = []
    sink = EventSink("s9", listeners=(got.append,))
    sink.status(StatusKind.WORKING, "thinking")
    sink.text("t1", "")  # empty text sends nothing
    sink.text("t1", "a")
    sink.tool_started("t1", "c1", "echo", {"x": 1})
    sink.tool_progress("t1", "c1", "half way", 0.5)
    sink.tool_completed("t1", "c1", "echo", ok=True, duration_ms=-3, result="ok")
    sink.notice(NoticeLevel.INFO, "note", key="k")
    assert sink.tool_progress("t1", "c1", "too far", 2.0) is None  # bad field: logged, not raised
    assert [type(e).__name__ for e in got] == [
        "Status",
        "MessageDelta",
        "ToolStarted",
        "ToolProgress",
        "ToolCompleted",
        "Notice",
    ]
    assert isinstance(got[0], Status) and got[0].kind is StatusKind.WORKING
    assert isinstance(got[4], ToolCompleted) and got[4].duration_ms == 0
    assert [e.seq for e in sink.since(2)] == [e.seq for e in got if e.seq > 2]
    assert len(sink.since()) == len(got)
    assert sink.failures == 1


def test_sink_listeners_and_request_delivery() -> None:
    sink = EventSink("s1", keep=2)
    first: list[EventBase] = []
    remove = sink.subscribe(first.append)
    requests: list[object] = []
    sink.on_request(requests.append)
    sink.status(StatusKind.READY)
    remove()
    sink.status(StatusKind.READY)
    sink.status(StatusKind.READY)
    assert len(first) == 1
    assert len(sink.since()) == 2  # bounded replay buffer
    marker = Status(session_id="s1", seq=99, kind=StatusKind.READY)
    sink.request(marker)
    assert requests == [marker]
    assert as_sink("s1", sink) is sink
    with pytest.raises(ValueError, match="belongs to session"):
        as_sink("other", sink)


# -- scope in background work (R01.16) -------------------------------------------------------------


def test_background_task_keeps_the_agents_scope(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    other_home = tmp_path / "home" / "profiles" / "other"
    other_home.mkdir(parents=True)
    other = make_scope(other_home, clock=fake_clock)

    async def seen_scope() -> str:
        await checkpoint()
        return current_scope().profile_id

    async def main() -> tuple[str, str]:
        agent = await open_agent(scope, server_for(fake_clock))
        with use_scope(other):
            task = agent.spawn(seen_scope())
            here = current_scope().profile_id
        got = await task
        await agent.close()
        return got, here

    got, here = asyncio.run(main())
    assert got == scope.profile_id == "work"
    assert here == "other"


def test_the_turn_task_runs_under_the_agents_scope(scope: Scope, fake_clock: FakeClock) -> None:
    profiles: list[str] = []
    registry = ToolRegistry()

    async def whoami(args: object) -> str:
        profiles.append(current_scope().profile_id)
        return "checked"

    registry.register(
        "whoami", "probe", {"description": "who", "parameters": {"type": "object"}}, whoami
    )

    async def main() -> None:
        server = server_for(fake_clock, tool_batch(("whoami", {})), text_turn("ok"))
        agent = await open_agent(scope, server, selection=["probe"], registry=registry)
        await run_driven(agent.run_turn("go"), fake_clock)
        await agent.close()

    asyncio.run(main())
    assert profiles == ["work"]


# -- turns -----------------------------------------------------------------------------------------


def test_tool_round_end_to_end(scope: Scope, fake_clock: FakeClock) -> None:
    tools = FakeTools(fake_clock)
    registry = tools.register(ToolRegistry(clock=fake_clock))
    seen: list[EventBase] = []

    async def main() -> tuple[TurnResult, Any]:
        server = server_for(fake_clock, tool_batch(("echo", {"text": "x"})), text_turn("echoed"))
        agent = await open_agent(
            scope, server, selection=["fake"], registry=registry, sink=seen.append
        )
        assert "echo" in agent.tool_names
        result = await run_driven(agent.run_turn("please echo"), fake_clock)
        sent = server.model_requests[0].body
        await agent.close()
        return result, sent

    result, sent = asyncio.run(main())
    assert result.exit_reason is ExitReason.COMPLETED
    check_alternation(result.rows)
    names = {t["function"]["name"] for t in sent["tools"]}
    assert "echo" in names
    assert [type(e) for e in seen if isinstance(e, ToolStarted | ToolCompleted)] == [
        ToolStarted,
        ToolCompleted,
    ]


def test_a_second_turn_while_one_runs_is_refused(scope: Scope, fake_clock: FakeClock) -> None:
    async def main() -> ExitReason:
        agent = await open_agent(scope, server_for(fake_clock, stalled("slow")))
        running = await agent.start_turn("one")
        with pytest.raises(AgentBusy):
            await agent.start_turn("two")
        assert agent.interrupt(running.generation)
        result = await run_driven(asyncio.shield(running.task), fake_clock)  # type: ignore[arg-type]
        await agent.close()
        with pytest.raises(AgentClosed):
            await agent.start_turn("three")
        return result.exit_reason

    assert asyncio.run(main()) is ExitReason.INTERRUPTED_BY_USER


def test_interrupt_mid_stream_through_the_agent(scope: Scope, fake_clock: FakeClock) -> None:
    holder: list[Agent] = []

    def on_event(event: EventBase) -> None:
        if isinstance(event, MessageDelta):
            asyncio.get_running_loop().call_soon(holder[0].interrupt)

    async def main() -> tuple[ExitReason, list[str], bool]:
        server = server_for(fake_clock, stalled("Partial"))
        agent = await open_agent(scope, server, sink=on_event)
        holder.append(agent)
        result = await run_driven(agent.run_turn("tell me"), fake_clock)
        stale = agent.interrupt()  # nothing runs any more
        await agent.close()
        return result.exit_reason, [r.role for r in result.rows], stale

    reason, roles, stale = asyncio.run(main())
    assert reason is ExitReason.INTERRUPTED_BY_USER
    assert roles == ["user", "assistant"]
    assert stale is False


def test_steer_and_redirect_need_a_live_turn(scope: Scope, fake_clock: FakeClock) -> None:
    async def main() -> tuple[bool, str, bool]:
        agent = await open_agent(scope, server_for(fake_clock))
        out = agent.steer("more"), agent.redirect("other"), agent.withdraw_steer("t")
        await agent.close()
        return out

    assert asyncio.run(main()) == (False, "rejected", False)


def test_route_switch_between_turns_uses_the_new_model(scope: Scope, fake_clock: FakeClock) -> None:
    async def main() -> tuple[list[str], str]:
        server = server_for(fake_clock, text_turn("from a"), text_turn("from b"))
        agent = await open_agent(scope, server)
        await run_driven(agent.run_turn("one"), fake_clock)
        await agent.binding.apply(_other_model(server))
        result = await run_driven(agent.run_turn("two"), fake_clock)
        models = [str(r.body["model"]) for r in server.model_requests]
        prompt = agent.runtime.prompts.prompt or ""
        await agent.close()
        assert result.final_text == "from b"
        return models, prompt

    models, _ = asyncio.run(main())
    assert models == ["fake-model-1", "model-b"]


def test_a_retry_is_dropped_when_the_route_moves(scope: Scope, fake_clock: FakeClock) -> None:
    """R01.5: the retry after a failure is not replayed on the old route."""

    async def main() -> tuple[list[str], str | None, int]:
        server = server_for(fake_clock, Turn((Fail(500, "upstream broke"),)), text_turn("from b"))
        agent = await open_agent(scope, server)
        task = asyncio.ensure_future(agent.run_turn("hi"))
        switched = False
        for _ in range(100_000):
            done, _ = await asyncio.wait({task}, timeout=0.0005)
            if done:
                break
            if not switched and len(server.model_requests) == 1:
                # Let the failure reach the retry wait (on the fake clock), then switch.
                for _ in range(20):
                    await asyncio.wait({asyncio.ensure_future(checkpoint())}, timeout=0.001)
                await agent.binding.apply(_other_model(server))
                switched = True
                continue
            fake_clock.advance(1.0)
        result = task.result()
        models = [str(r.body["model"]) for r in server.model_requests]
        await agent.close()
        return models, result.final_text, result.api_calls

    models, text, calls = asyncio.run(main())
    assert models == ["fake-model-1", "model-b"]
    assert text == "from b"
    assert calls == 1  # the dropped attempt was given back


def _other_model(server: FakeModelServer) -> RuntimeRoute:
    from dataclasses import replace

    return replace(fake_route(server), model="model-b")


# -- closing ---------------------------------------------------------------------------------------


def test_close_runs_every_cleanup_once(scope: Scope, fake_clock: FakeClock) -> None:
    calls: list[str] = []
    registry = CleanupRegistry()
    registry.register("terminal", lambda task_id: calls.append(f"terminal:{task_id}"))

    async def browser(task_id: str) -> None:
        calls.append(f"browser:{task_id}")

    def broken(task_id: str) -> None:
        raise RuntimeError("sandbox already gone")

    registry.register("browser", browser)
    registry.register("broken", broken)

    async def main() -> tuple[list[str], list[str], bool]:
        agent = await open_agent(scope, server_for(fake_clock), cleanups=registry)
        agent.on_close(lambda: calls.append("closer"), name="mine")
        errors = await agent.close()
        again = await agent.close()
        client_closed = all(c.is_closed for c in [agent.binding.state.client])
        return errors, again, client_closed

    errors, again, client_closed = asyncio.run(main())
    assert calls == ["closer", "terminal:s1", "browser:s1"]
    assert len(errors) == 1 and "sandbox already gone" in errors[0]
    assert again == []
    assert client_closed


def test_cleanup_registry_replaces_and_removes() -> None:
    registry = CleanupRegistry()
    seen: list[str] = []
    registry.register("a", lambda t: seen.append("first"))
    remove = registry.register("a", lambda t: seen.append("second"))
    assert registry.names() == ["a"]
    asyncio.run(registry.run("t"))
    remove()
    assert registry.names() == []
    assert seen == ["second"]
    with pytest.raises(ValueError):
        registry.register("", lambda t: None)


def test_close_interrupts_a_running_turn(scope: Scope, fake_clock: FakeClock) -> None:
    async def main() -> ExitReason:
        agent = await open_agent(scope, server_for(fake_clock, stalled("half")))
        running = await agent.start_turn("go")
        await eventually(lambda: running.state.streamed_text)
        await run_driven(agent.close(), fake_clock)
        return running.task.result().exit_reason

    assert asyncio.run(main()) is ExitReason.INTERRUPTED_BY_SYSTEM


def test_before_turn_hooks_run_first_and_never_block(scope: Scope, fake_clock: FakeClock) -> None:
    seen: list[str] = []

    async def cancel_review() -> None:
        seen.append("async hook")

    def broken() -> None:
        seen.append("broken hook")
        raise RuntimeError("hook failed")

    async def main() -> str:
        agent = await open_agent(scope, server_for(fake_clock, text_turn("hi")))
        agent.runtime.before_turn.extend([cancel_review, broken])
        reply = await agent.chat("hello")
        await agent.close()
        return reply

    assert asyncio.run(run_driven(main(), fake_clock)) == "hi"
    assert seen == ["async hook", "broken hook"]
