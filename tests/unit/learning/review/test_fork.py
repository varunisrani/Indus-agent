"""P09-15: the review fork — whitelist, limits, digest, cache parity, cancellation, routing."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from indus.core.transcript import TranscriptRow
from indus.core.usage import RecordingCaller, UsageLedger
from indus.foundation.scope import Scope
from indus.learning import pending
from indus.learning.memory.store import MemoryStore
from indus.learning.review.counters import Kind, ReviewRequest, ReviewTrigger
from indus.learning.review.fork import (
    ASSISTANT_CLIP,
    DIGEST_TAIL,
    USER_CLIP,
    ReviewRunner,
    allowed_tools,
    digest_rows,
    input_budget,
)
from indus.learning.review.prompts import NOTHING
from indus.providers.route import RuntimeRoute
from tests.fakes.agent import run_driven
from tests.fakes.clock import FakeClock, checkpoint
from tests.unit.learning._helpers_a import with_config
from tests.unit.learning.review.helpers import (
    MAIN_ROUTE,
    FakeAux,
    FakeBroker,
    Parent,
    ScriptedCaller,
    Stall,
    calls,
    make_parent,
    text,
)
from tests.unit.learning.skills.helpers import make_skill


async def _parent_with_history(scope: Scope, turns: int = 1) -> Parent:
    parent = make_parent(scope, ScriptedCaller([text(f"answer {n}") for n in range(turns)]))
    for n in range(turns):
        await parent.turn(f"question {n}")
    return parent


async def _rows(parent: Parent) -> tuple[TranscriptRow, ...]:
    store = parent.runtime.store
    assert store is not None
    return tuple(await store.load("s1"))


def _request(rows: tuple[TranscriptRow, ...], *kinds: Kind, **values: Any) -> ReviewRequest:
    chosen: tuple[Kind, ...] = kinds or ("memory", "skills")
    return ReviewRequest(kinds=frozenset(chosen), rows=rows, session_id="s1", **values)


def _tool_texts(rows: tuple[TranscriptRow, ...]) -> list[str]:
    return [str(r.content) for r in rows if r.role == "tool"]


# -- pure pieces ----------------------------------------------------------------------------------


def test_input_budget() -> None:
    assert input_budget(None, 200_000) == 150_000
    assert input_budget(None, 2_000_000) == 600_000
    assert input_budget(None, None) == 120_000
    assert input_budget(None, 0) == 120_000
    assert input_budget(5_000, 200_000) == 5_000
    assert input_budget(0, 200_000) is None
    assert input_budget(-1, None) is None


def test_allowed_tools_only_what_the_parent_offers() -> None:
    offered = {"memory", "skill_manage", "skill_view", "read_file", "terminal", "ask_human"}
    assert allowed_tools({"skills"}, offered) == {"skill_manage", "skill_view", "read_file"}
    assert "memory" in allowed_tools({"memory"}, offered)
    assert allowed_tools({"skills"}, offered, ["ask_human", "web_search"]) == {
        "skill_manage",
        "skill_view",
        "read_file",
        "ask_human",
    }
    assert "memory" not in allowed_tools({"skills"}, offered, ["memory"])


def _row(role: str, content: str = "", **values: Any) -> TranscriptRow:
    return TranscriptRow(role=role, content=content, **values)


def _call(ident: str, name: str) -> dict[str, Any]:
    return {"id": ident, "type": "function", "function": {"name": name, "arguments": "{}"}}


def test_digest_keeps_the_tail_and_lists_older_turns() -> None:
    rows: list[TranscriptRow] = []
    for n in range(20):
        rows.append(_row("user", f"question {n} " + "u" * 400))
        rows.append(
            _row("assistant", "", tool_calls=[_call(f"c{n}", "read_file"), _call(f"d{n}", "patch")])
        )
        rows.append(_row("tool", "out", tool_call_id=f"c{n}"))
        rows.append(_row("tool", "out", tool_call_id=f"d{n}"))
        rows.append(_row("assistant", f"answer {n} " + "a" * 400))
    digest = digest_rows(rows)
    assert digest[0].role == "user"
    tail = digest[1:]
    assert len(tail) >= DIGEST_TAIL and tail[0].role == "assistant"
    assert [r.content for r in tail] == [r.content for r in rows[-len(tail) :]]
    body = str(digest[0].content)
    assert "Assistant used tools: read_file, patch" in body
    user_line = next(line for line in body.splitlines() if line.startswith("User: question 0"))
    assert len(user_line) == len("User: ") + USER_CLIP + 1  # clipped, with the mark
    reply_line = next(line for line in body.splitlines() if line.startswith("Assistant: answer 0"))
    assert len(reply_line) == len("Assistant: ") + ASSISTANT_CLIP + 1


def test_digest_never_starts_on_a_tool_row() -> None:
    rows = [_row("user", "go"), _row("assistant", "", tool_calls=[_call("c", "read_file")])]
    rows += [_row("tool", "out", tool_call_id="c") for _ in range(DIGEST_TAIL + 3)]
    rows += [_row("assistant", "done")]
    digest = digest_rows(rows)
    kept = digest[1:]
    assert kept[0].role == "assistant" and kept[0].tool_calls


def test_short_history_is_replayed_whole() -> None:
    rows = [_row("user", "hi"), _row("assistant", "hello")]
    assert [r.content for r in digest_rows(rows)] == ["hi", "hello"]


# -- the whitelist --------------------------------------------------------------------------------


def test_terminal_is_refused_and_never_runs(scope: Scope) -> None:
    async def body() -> None:
        parent = await _parent_with_history(scope)
        fork = ScriptedCaller([calls(("terminal", {"command": "rm -rf build"})), text(NOTHING)])
        runner = ReviewRunner(parent.review_parent(caller=fork))
        outcome = await runner.run(_request(await _rows(parent), "skills"))
        assert outcome.status == "done"
        assert outcome.denied == ("terminal",)
        assert all(name != "terminal" for name, _ in parent.log.runs)
        assert "not available in this background review" in _tool_texts(outcome.rows)[0]
        # advertised is not permitted: the fork still sends the parent's whole tool array
        assert fork.requests[0].tools == parent.runtime.tools

    asyncio.run(body())


def test_skill_review_has_no_memory_tool(scope: Scope) -> None:
    async def body() -> None:
        parent = await _parent_with_history(scope)
        add = {"action": "add", "target": "memory", "content": "Builds use make."}
        fork = ScriptedCaller([calls(("memory", add)), text(NOTHING)])
        runner = ReviewRunner(parent.review_parent(caller=fork))
        outcome = await runner.run(_request(await _rows(parent), "skills"))
        assert outcome.denied == ("memory",)
        assert MemoryStore.for_scope(scope).entries("memory") == []

        fork2 = ScriptedCaller([calls(("memory", add)), text("saved")])
        runner2 = ReviewRunner(parent.review_parent(caller=fork2))
        outcome2 = await runner2.run(_request(await _rows(parent), "memory"))
        assert outcome2.summary_lines == ("Memory updated",)
        assert MemoryStore.for_scope(scope).entries("memory") == ["Builds use make."]

    asyncio.run(body())


def test_approval_prompt_never_reaches_the_surface(scope: Scope) -> None:
    extra = with_config(scope, {"learning": {"review": {"extra_tools": ["ask_human"]}}})

    async def body() -> None:
        parent = make_parent(extra, ScriptedCaller([text("hi")]))
        broker = FakeBroker()
        await parent.turn("hello", broker=broker)
        fork = ScriptedCaller([calls(("ask_human", {})), text(NOTHING)])
        runner = ReviewRunner(parent.review_parent(caller=fork))
        await runner.run(_request(await _rows(parent), "skills"))
        assert parent.log.broker_seen == [None]
        assert broker.asked == []

    asyncio.run(body())


# -- limits ---------------------------------------------------------------------------------------


def test_seventeenth_request_is_never_sent(scope: Scope) -> None:
    async def body() -> None:
        parent = await _parent_with_history(scope)
        steps: list[Any] = []
        for n in range(30):
            tool = "read_file" if n % 2 else "search_files"
            key = "path" if tool == "read_file" else "pattern"
            steps.append(calls((tool, {key: f"x{n}"})))
        fork = ScriptedCaller(steps)
        runner = ReviewRunner(parent.review_parent(caller=fork))
        outcome = await runner.run(_request(await _rows(parent), "skills"))
        assert len(fork.requests) == 16
        assert outcome.requests == 16
        assert outcome.stopped == "iterations"

    asyncio.run(body())


def test_budget_stops_before_crossing(scope: Scope) -> None:
    capped = with_config(scope, {"learning": {"review": {"max_input_tokens": 1000}}})

    async def body() -> None:
        parent = make_parent(capped, ScriptedCaller([text("hi")]))
        await parent.turn("hello")
        steps = [calls(("read_file", {"path": f"f{n}"}), prompt_tokens=300) for n in range(10)]
        fork = ScriptedCaller(steps)
        runner = ReviewRunner(parent.review_parent(caller=fork))
        outcome = await runner.run(_request(await _rows(parent), "skills"))
        assert len(fork.requests) == 3
        assert outcome.input_tokens == 900 <= 1000
        assert outcome.stopped == "budget"

    asyncio.run(body())


# -- cache parity, usage, routing -----------------------------------------------------------------


def test_same_model_request_prefix_matches_the_parent(scope: Scope) -> None:
    async def body() -> None:
        parent_caller = ScriptedCaller(
            [calls(("read_file", {"path": "README"})), text("It is a CLI.")]
        )
        parent = make_parent(scope, parent_caller)
        await parent.turn("what is this repo?")
        last = parent_caller.requests[-1]
        fork = ScriptedCaller([text(NOTHING)])
        runner = ReviewRunner(parent.review_parent(caller=fork))
        await runner.run(_request(await _rows(parent), "memory", "skills"))
        first = fork.requests[0]
        assert first.model == last.model
        assert first.tools == last.tools
        assert first.reasoning_effort == last.reasoning_effort == "medium"
        assert first.max_tokens == last.max_tokens
        assert list(first.messages[: len(last.messages)]) == list(last.messages)
        assert first.messages[len(last.messages)]["content"] == "It is a CLI."

    asyncio.run(body())


def test_usage_lands_on_the_parent_ledger_as_review(scope: Scope, fake_clock: FakeClock) -> None:
    async def body() -> None:
        parent = await _parent_with_history(scope)
        ledger = UsageLedger(clock=fake_clock, session_id="s1")
        fork = ScriptedCaller([calls(("read_file", {"path": "a"})), text(NOTHING)])
        recording = RecordingCaller(fork, ledger, lambda: MAIN_ROUTE, task="review")
        runner = ReviewRunner(parent.review_parent(caller=recording))
        await runner.run(_request(await _rows(parent), "skills"))
        assert ledger.side_calls == {"review": 2}
        assert ledger.api_calls == 0  # nothing counted as a main-model turn

    asyncio.run(body())


def test_same_model_ignores_a_configured_effort_once(
    scope: Scope, caplog: pytest.LogCaptureFixture
) -> None:
    effort = with_config(scope, {"auxiliary": {"review": {"reasoning_effort": "high"}}})

    async def body() -> None:
        parent = make_parent(effort, ScriptedCaller([text("hi")]))
        await parent.turn("hello")
        fork = ScriptedCaller([text(NOTHING), text(NOTHING)])
        runner = ReviewRunner(parent.review_parent(caller=fork))
        rows = await _rows(parent)
        await runner.run(_request(rows, "skills"))
        await runner.run(_request(rows, "skills"))
        assert [r.reasoning_effort for r in fork.requests] == ["medium", "medium"]
        assert caplog.text.count("reasoning_effort (high) is ignored") == 1

    asyncio.run(body())


def test_routed_review_replays_a_digest_through_the_aux_client(scope: Scope) -> None:
    effort = with_config(
        scope,
        {
            "auxiliary": {"review": {"reasoning_effort": "low"}},
            "learning": {"review": {"defer": "never"}},
        },
    )
    other = RuntimeRoute(
        provider="other", model="small-model", api_mode="chat_completions",
        base_url="https://other.invalid/v1",
    )  # fmt: skip

    async def body() -> None:
        parent = make_parent(effort, ScriptedCaller([text(f"a{n}") for n in range(20)]))
        for n in range(20):
            await parent.turn(f"q{n}")
        aux = FakeAux(other, [calls(("read_file", {"path": "x"})), text(NOTHING)], effort="low")
        unused = ScriptedCaller([])
        runner = ReviewRunner(parent.review_parent(caller=unused, aux=aux))
        outcome = await runner.run(_request(await _rows(parent), "skills"))
        assert outcome.routed and unused.requests == []
        assert [c["task"] for c in aux.calls] == ["review", "review"]
        first = aux.calls[0]
        assert first["reasoning_effort"] == "low"
        assert first["tools"] == parent.runtime.tools
        messages = first["messages"]
        assert messages[0]["role"] == "system"
        assert str(messages[1]["content"]).startswith("[Summary of the earlier part")
        # system, digest, the tail (24 rows, extended back to an assistant row), review prompt
        assert len(messages) == 1 + 1 + (DIGEST_TAIL + 1) + 1
        assert messages[2]["role"] == "assistant"

    asyncio.run(body())


# -- isolation ------------------------------------------------------------------------------------


def test_parent_views_do_not_count_as_review_reads(scope: Scope) -> None:
    async def body() -> None:
        make_skill(scope.home / "skills", "deploy", body="Run make deploy.\n")
        parent_caller = ScriptedCaller([calls(("skill_view", {"name": "deploy"})), text("ok")])
        parent = make_parent(scope, parent_caller)
        await parent.turn("deploy please")
        patch = {
            "action": "patch",
            "name": "deploy",
            "old_string": "make deploy",
            "new_string": "make test deploy",
        }
        fork = ScriptedCaller(
            [
                calls(("skill_manage", patch)),
                calls(("skill_view", {"name": "deploy"})),
                calls(("skill_manage", patch)),
                text("patched"),
            ]
        )
        runner = ReviewRunner(parent.review_parent(caller=fork))
        outcome = await runner.run(_request(await _rows(parent), "skills"))
        results = [json.loads(t) for t in _tool_texts(outcome.rows)]
        assert results[0].get("success") is not True and "skill_view" in results[0]["error"]
        assert results[2]["success"] is True
        assert "make test deploy" in (scope.home / "skills/deploy/SKILL.md").read_text()
        assert outcome.summary_lines == ("Skill deploy improved",)
        assert outcome.fork_session_id.startswith("s1:review:")

    asyncio.run(body())


def test_review_writes_nothing_to_the_parent_transcript_and_never_reviews_itself(
    scope: Scope,
) -> None:
    quick = with_config(scope, {"memory": {"review_interval": 1}, "skills": {"review_interval": 1}})

    async def body() -> None:
        parent = make_parent(quick, ScriptedCaller([text("hi")]))
        started: list[ReviewRequest] = []
        parent.runtime.after_turn.append(ReviewTrigger(quick, started.append).after_turn)
        await parent.turn("hello")
        assert len(started) == 1
        before = await _rows(parent)
        fork = ScriptedCaller([calls(("read_file", {"path": "a"})), text(NOTHING)])
        runner = ReviewRunner(parent.review_parent(caller=fork))
        await runner.run(started[0])
        assert await _rows(parent) == before
        assert len(started) == 1  # the fork has no post-turn hooks

    asyncio.run(body())


def test_unattended_memory_remove_is_staged(scope: Scope) -> None:
    async def body() -> None:
        MemoryStore.for_scope(scope).apply("memory", [_add("Old fact.")])
        parent = await _parent_with_history(scope)
        remove = {"action": "remove", "target": "memory", "old_text": "Old fact"}
        fork = ScriptedCaller([calls(("memory", remove)), text("done")])
        runner = ReviewRunner(parent.review_parent(caller=fork))
        outcome = await runner.run(_request(await _rows(parent), "memory"))
        assert MemoryStore.for_scope(scope).entries("memory") == ["Old fact."]
        assert pending.PendingStore(scope.home, scope.clock).count("memory") == 1
        assert outcome.summary_lines == ()  # staged writes are never reported as done

    asyncio.run(body())


def _add(content: str) -> Any:
    from indus.learning.memory.store import Op

    return Op("add", content)


# -- cancellation ---------------------------------------------------------------------------------


def test_live_turn_cancels_the_fork_within_two_seconds(scope: Scope, fake_clock: FakeClock) -> None:
    async def body() -> None:
        parent = await _parent_with_history(scope)
        fork = ScriptedCaller([Stall()], clock=fake_clock)
        runner = ReviewRunner(parent.review_parent(caller=fork))
        task = runner.start(_request(await _rows(parent), "skills"))
        assert task is not None and runner.busy
        while not fork.requests:
            await checkpoint()
        started = fake_clock.monotonic()
        assert await runner.cancel_for_live_turn() is True
        assert task.done()
        assert fake_clock.monotonic() - started < 2.0
        outcome = task.result()
        assert outcome.status == "cancelled"
        assert len(fork.requests) == 1

    asyncio.run(run_driven(body(), fake_clock, step=0.1))


def test_a_second_review_is_skipped_while_one_runs(scope: Scope, fake_clock: FakeClock) -> None:
    async def body() -> None:
        parent = await _parent_with_history(scope)
        fork = ScriptedCaller([Stall()], clock=fake_clock)
        runner = ReviewRunner(parent.review_parent(caller=fork))
        rows = await _rows(parent)
        assert runner.start(_request(rows, "skills")) is not None
        assert runner.start(_request(rows, "skills")) is None
        await runner.close()
        assert not runner.busy

    asyncio.run(run_driven(body(), fake_clock, step=0.1))


def test_no_history_or_prompt_is_skipped(scope: Scope) -> None:
    async def body() -> None:
        parent = make_parent(scope, ScriptedCaller([]))
        runner = ReviewRunner(parent.review_parent())
        outcome = await runner.run(_request((), "skills"))
        assert outcome.status == "skipped"

    asyncio.run(body())
