"""Side questions (P10-23): the cache-parity fork, tool refusal, the transcript fallback and
running beside a live turn."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from indus.core import side_question as sq
from indus.core.agent import Agent
from indus.core.exit_reasons import ExitReason
from indus.core.persist import MemoryStore
from indus.core.transcript import TranscriptRow
from indus.foundation.scope import Scope
from indus.providers.auxiliary import AuxClient
from indus.tools.registry import ToolRegistry
from tests.fakes.agent import fake_options, run_driven
from tests.fakes.aux_model import FakeAuxModel
from tests.fakes.clock import FakeClock
from tests.fakes.model import (
    FakeModelServer,
    Finish,
    Stall,
    Text,
    ToolCall,
    Turn,
    fake_route,
    script_of,
    text_turn,
    tool_batch,
)
from tests.fakes.tools import FakeTools


def as_aux(fake: object) -> AuxClient:
    """A fake auxiliary model where the real client is expected (duck-typed)."""
    return cast(AuxClient, fake)


def rows_of(agent: Agent) -> list[TranscriptRow]:
    store = agent.runtime.store
    assert isinstance(store, MemoryStore)
    return store.rows.get(agent.session_id, [])


def test_fork_answers_from_the_warm_prefix_and_writes_nothing(
    scope: Scope, fake_clock: FakeClock
) -> None:
    server = FakeModelServer(
        script_of(text_turn("we chose blue"), text_turn("You decided on blue.")), clock=fake_clock
    )
    tasks: list[str] = []

    async def main() -> tuple[Agent, sq.SideAnswer, int]:
        agent = await Agent.start(scope, fake_route(server), [], fake_options(server))

        async def heard(_usage: Any, task: str) -> None:
            tasks.append(task)

        agent.usage.add_listener(heard)
        await agent.run_turn("which colour?")
        before = len(rows_of(agent))
        answer = await sq.answer_side_question(
            "what did we decide?", session_id=agent.session_id, agent=agent
        )
        return agent, answer, before

    agent, answer, before = asyncio.run(run_driven(main(), fake_clock))
    assert answer == sq.SideAnswer("You decided on blue.", "fork")
    assert len(rows_of(agent)) == before == 2
    parent, fork = (r.body for r in server.model_requests)
    # same system prompt, same history bytes, same tools: the provider's prefix cache is warm
    assert fork["messages"][:2] == parent["messages"]
    assert fork["messages"][2]["content"] == "we chose blue"
    assert fork["messages"][-1]["content"].endswith("Question: what did we decide?")
    assert fork.get("tools") == parent.get("tools")
    assert tasks == ["", sq.TASK]
    asyncio.run(agent.close())


def test_tools_are_refused_and_no_fourth_call_is_made(scope: Scope, fake_clock: FakeClock) -> None:
    calls = [tool_batch(ToolCall("echo", {"text": f"try {i}"}, id=f"c{i}")) for i in range(3)]
    server = FakeModelServer(script_of(text_turn("ok"), *calls), clock=fake_clock)
    tools = FakeTools(fake_clock)
    registry = tools.register(ToolRegistry(clock=fake_clock))

    async def main() -> tuple[Agent, sq.SideAnswer]:
        options = fake_options(server, registry=registry)
        agent = await Agent.start(scope, fake_route(server), ["fake"], options)
        assert "echo" in agent.tool_names
        await agent.run_turn("hi")
        answer = await sq.answer_side_question("anything?", session_id="s1", agent=agent)
        return agent, answer

    agent, answer = asyncio.run(run_driven(main(), fake_clock))
    assert answer.text == sq.NO_ANSWER and answer.path == "fork"
    assert len(server.model_requests) == 4  # the parent's one and the fork's three
    assert server.turns_left == 0
    assert tools.effects == [] and tools.events == []
    refused = [m for m in server.model_requests[3].body["messages"] if m["role"] == "tool"]
    assert refused and all(sq.TOOL_REFUSED in m["content"] for m in refused)
    assert len(rows_of(agent)) == 2
    asyncio.run(agent.close())


def test_runs_beside_a_turn_in_flight_without_interrupting_it(
    scope: Scope, fake_clock: FakeClock
) -> None:
    slow = Turn((Stall(30.0), Text(("second answer",)), Finish("stop")))
    server = FakeModelServer(
        script_of(text_turn("first answer"), slow, text_turn("side answer")), clock=fake_clock
    )

    async def main() -> tuple[Any, sq.SideAnswer]:
        agent = await Agent.start(scope, fake_route(server), [], fake_options(server))
        await agent.run_turn("one")
        running = await agent.start_turn("two")
        while len(server.model_requests) < 2:
            await fake_clock.run_until_idle()
        answer = await sq.answer_side_question("status?", session_id="s1", agent=agent)
        assert not running.done
        result = await running.task
        await agent.close()
        return result, answer

    result, answer = asyncio.run(run_driven(main(), fake_clock))
    assert answer.text == "side answer"
    assert result.exit_reason is ExitReason.COMPLETED and result.final_text == "second answer"
    fork = server.model_requests[2].body["messages"]
    # the in-flight turn's stored user row is cut off: the history ends on a finished answer
    assert [m["role"] for m in fork] == ["system", "user", "assistant", "user"]
    assert fork[2]["content"] == "first answer"


def test_no_live_agent_falls_back_to_the_transcript(scope: Scope) -> None:
    store = MemoryStore()
    rows: list[TranscriptRow] = []
    for i in range(40):
        rows.append(TranscriptRow(role="user", content=f"q{i} " + "u" * 3_000))
        rows.append(TranscriptRow(role="assistant", content=f"a{i} " + "a" * 300))
    store.rows["old"] = rows
    aux = FakeAuxModel("we talked about q39")
    answer = asyncio.run(
        sq.answer_side_question("what was last?", session_id="old", store=store, aux=as_aux(aux))
    )
    assert answer == sq.SideAnswer("we talked about q39", "transcript")
    (call,) = aux.calls
    assert call.task == sq.TASK
    text = call.messages[1]["content"]
    assert text.startswith("Transcript:\n" + sq.OLDER_LEFT_OUT)
    assert "a39 " in text and "q0 " not in text
    transcript = text.split("\n\nSide question:")[0]
    assert len(transcript) <= sq.TRANSCRIPT_CHARS + 100
    assert all(len(line) <= sq.MESSAGE_CHARS + 20 for line in transcript.splitlines())


def test_render_transcript_rules() -> None:
    rows = [
        TranscriptRow(role="user", content="find it"),
        TranscriptRow(
            role="assistant",
            content="",
            tool_calls=[{"id": "c", "function": {"name": "search_files", "arguments": "{}"}}],
        ),
        TranscriptRow(role="tool", content="found", tool_call_id="c"),
        TranscriptRow(role="assistant", content="It is in a.txt"),
    ]
    assert sq.render_transcript(rows) == (
        "User: find it\nAssistant used tools: search_files\nTool result: found\n"
        "Assistant: It is in a.txt"
    )
    assert sq.render_transcript([]) == "(the conversation is empty so far)"
    assert sq.trim_to_answer(rows[:3]) == []


def test_routed_side_question_uses_the_aux_task(scope: Scope) -> None:
    class Routed(FakeAuxModel):
        def task_config(self, task: str) -> dict[str, str]:
            return {"provider": "other", "model": "small"}

    store = MemoryStore()
    store.rows["s"] = [TranscriptRow(role="user", content="hi")]
    aux = Routed("short answer")
    answer = asyncio.run(
        sq.answer_side_question("q?", session_id="s", store=store, aux=as_aux(aux))
    )
    assert answer.path == "routed" and answer.text == "short answer"


def test_empty_question_and_no_way_to_answer() -> None:
    with pytest.raises(ValueError):
        asyncio.run(sq.answer_side_question("  ", session_id="s"))
    with pytest.raises(sq.SideQuestionUnavailable):
        asyncio.run(sq.answer_side_question("why?", session_id="s"))


def test_a_failing_fork_falls_back_to_the_transcript(
    scope: Scope, fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = FakeModelServer(script_of(text_turn("blue it is")), clock=fake_clock)
    aux = FakeAuxModel("from the transcript")

    async def broken(*_args: Any, **_kwargs: Any) -> str:
        raise RuntimeError("fork broke")

    monkeypatch.setattr(sq, "ask_fork", broken)

    async def main() -> sq.SideAnswer:
        agent = await Agent.start(scope, fake_route(server), [], fake_options(server))
        await agent.run_turn("colour?")
        answer = await sq.answer_side_question(
            "which?",
            session_id=agent.session_id,
            agent=agent,
            aux=as_aux(aux),
        )
        await agent.close()
        return answer

    answer = asyncio.run(run_driven(main(), fake_clock))
    assert answer == sq.SideAnswer("from the transcript", "transcript")
    assert "Assistant: blue it is" in aux.calls[0].messages[1]["content"]
