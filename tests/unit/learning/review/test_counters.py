"""P09-14: review counters, the trigger, and the finalize hook step."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from indus.core.exit_reasons import INTERRUPTED
from indus.core.loop import run_turn
from indus.core.transcript import TranscriptRow
from indus.core.turn_state import TurnState, new_turn_state
from indus.foundation.errors import ProviderError
from indus.foundation.scope import Scope
from indus.learning.review.counters import (
    ReviewCounters,
    ReviewRequest,
    ReviewTrigger,
    TurnFacts,
    review_enabled,
)
from tests.unit.learning._helpers_a import with_config
from tests.unit.learning.review.helpers import ScriptedCaller, calls, make_parent, text


def facts(rounds: list[set[str]] | None = None, **values: Any) -> TurnFacts:
    values.setdefault("memory_available", True)
    values.setdefault("skills_available", True)
    return TurnFacts(rounds=tuple(frozenset(r) for r in rounds or []), **values)


# -- counters -------------------------------------------------------------------------------------


def test_ten_user_turns_make_one_memory_review() -> None:
    counters = ReviewCounters()
    due = [counters.observe(facts()) for _ in range(10)]
    assert due[:9] == [frozenset()] * 9
    assert due[9] == {"memory"}
    assert counters.memory_turns == 0


def test_nine_user_turns_make_none() -> None:
    counters = ReviewCounters()
    assert all(not counters.observe(facts()) for _ in range(9))


def test_memory_call_resets() -> None:
    counters = ReviewCounters()
    seen: list[frozenset[str]] = []
    for turn in range(1, 16):
        rounds = [{"memory"}] if turn == 5 else []
        seen.append(counters.observe(facts(rounds)))
    due_turns = [n for n, kinds in enumerate(seen, start=1) if "memory" in kinds]
    assert due_turns == [15]


def test_memory_counter_needs_the_tool() -> None:
    counters = ReviewCounters(memory_interval=1)
    assert counters.observe(facts(memory_available=False)) == frozenset()


def test_ten_tool_iterations_make_a_skill_review() -> None:
    counters = ReviewCounters(memory_interval=0)
    assert counters.observe(facts([{"read_file"}] * 9)) == frozenset()
    assert counters.skill_steps == 9
    assert counters.observe(facts([{"terminal"}])) == {"skills"}
    assert counters.skill_steps == 0


def test_skill_manage_call_resets() -> None:
    counters = ReviewCounters(memory_interval=0)
    rounds = [{"read_file"}] * 6 + [{"skill_manage", "read_file"}] + [{"read_file"}] * 4
    assert counters.observe(facts(rounds)) == frozenset()
    assert counters.skill_steps == 4


def test_skill_counter_needs_skill_manage() -> None:
    counters = ReviewCounters(memory_interval=0, skills_interval=1)
    assert counters.observe(facts([{"read_file"}] * 3, skills_available=False)) == frozenset()


def test_zero_interval_turns_a_counter_off() -> None:
    counters = ReviewCounters(memory_interval=0, skills_interval=0)
    assert all(not counters.observe(facts([{"read_file"}] * 20)) for _ in range(30))


def test_resume_hydrates() -> None:
    counters = ReviewCounters()
    counters.hydrate(23)
    assert counters.memory_turns == 3
    counters.hydrate(99)  # once only
    assert counters.memory_turns == 3
    due = [counters.observe(facts(prior_user_turns=23 + n)) for n in range(7)]
    assert due[-1] == {"memory"} and not any(due[:-1])


def test_facts_from_rows_counts_this_turns_tool_rounds() -> None:
    def call(name: str) -> dict[str, Any]:
        return {
            "id": f"c-{name}",
            "type": "function",
            "function": {"name": name, "arguments": "{}"},
        }

    rows = [
        TranscriptRow(role="user", content="earlier"),
        TranscriptRow(role="assistant", content=None, tool_calls=[call("terminal")]),
        TranscriptRow(role="tool", tool_call_id="c-terminal", content="ok"),
        TranscriptRow(role="assistant", content="done"),
        TranscriptRow(role="user", content="now"),
        TranscriptRow(role="assistant", content=None, tool_calls=[call("memory")]),
        TranscriptRow(role="tool", tool_call_id="c-memory", content="ok"),
        TranscriptRow(role="assistant", content="saved"),
    ]
    got = TurnFacts.from_rows(rows, 4, {"memory", "skill_manage"})
    assert got.prior_user_turns == 1
    assert got.rounds == (frozenset({"memory"}),)
    assert got.called_memory and got.memory_available and got.skills_available


# -- the enabled flag -----------------------------------------------------------------------------


class _Broken:
    def get(self, key: str, default: Any = None) -> Any:
        raise RuntimeError("config file unreadable")


def test_unreadable_enabled_fails_open(scope: Scope, caplog: pytest.LogCaptureFixture) -> None:
    assert review_enabled(scope.with_config(_Broken()))  # pyright: ignore[reportArgumentType]
    assert "unreadable" in caplog.text
    assert review_enabled(with_config(scope, {"learning": {"review": {"enabled": True}}}))
    assert not review_enabled(with_config(scope, {"learning": {"review": {"enabled": False}}}))


# -- the trigger on real turns --------------------------------------------------------------------


def _trigger(scope: Scope, parent: Any, **values: Any) -> list[ReviewRequest]:
    started: list[ReviewRequest] = []
    trigger = ReviewTrigger(scope, started.append, **values)
    parent.runtime.after_turn.append(trigger.after_turn)
    return started


def test_trigger_after_ten_turns_hands_over_a_deep_copy(scope: Scope) -> None:
    async def body() -> None:
        caller = ScriptedCaller([text(f"answer {n}") for n in range(10)])
        parent = make_parent(scope, caller)
        started = _trigger(scope, parent)
        for n in range(9):
            await parent.turn(f"question {n}")
        assert started == []
        result = await parent.turn("question 9")
        assert [r.kinds for r in started] == [frozenset({"memory"})]
        snap = started[0].rows
        assert snap[-1].content == "answer 9" and len(snap) == 20
        assert snap[-1] is not result.rows[-1]
        snap[-1].content = "changed"
        assert result.rows[-1].content == "answer 9"

    asyncio.run(body())


def test_skill_review_due_after_ten_tool_iterations(scope: Scope) -> None:
    async def body() -> None:
        rounds = [calls(("read_file", {"path": f"f{n}"})) for n in range(10)]
        caller = ScriptedCaller([*rounds, text("all read")])
        parent = make_parent(scope, caller)
        started = _trigger(with_config(scope, {"memory": {"review_interval": 0}}), parent)
        await parent.turn("read everything")
        assert [r.kinds for r in started] == [frozenset({"skills"})]

    asyncio.run(body())


def test_interrupted_turn_starts_nothing(scope: Scope) -> None:
    async def body() -> None:
        quick = with_config(scope, {"memory": {"review_interval": 1}})
        parent = make_parent(quick, ScriptedCaller([text("hi")]))
        started = _trigger(quick, parent)
        state = new_turn_state(parent.runtime, message="hello", turn_id="t1", generation=1)
        state.control.interrupt(1)
        result = await run_turn(state)
        assert result.exit_reason in INTERRUPTED
        assert started == []
        assert parent.runtime.after_turn  # counted, but nothing started

    asyncio.run(body())


@pytest.mark.parametrize("context", ["single_query", "cron"])
def test_no_review_contexts(scope: Scope, context: str) -> None:
    async def body() -> None:
        quick = with_config(scope, {"memory": {"review_interval": 1}})
        parent = make_parent(quick, ScriptedCaller([text("hi")]), run_context=context)
        started = _trigger(quick, parent)
        await parent.turn("hello")
        assert started == []

    asyncio.run(body())


def test_host_no_review_flag(scope: Scope) -> None:
    async def body() -> None:
        quick = with_config(scope, {"memory": {"review_interval": 1}})
        parent = make_parent(quick, ScriptedCaller([text("hi")]))
        started = _trigger(quick, parent, no_review=True)
        await parent.turn("hello")
        assert started == []

    asyncio.run(body())


def test_disabled_reviews_start_nothing(scope: Scope) -> None:
    async def body() -> None:
        off = with_config(
            scope, {"memory": {"review_interval": 1}, "learning": {"review": {"enabled": False}}}
        )
        parent = make_parent(off, ScriptedCaller([text("hi")]))
        started = _trigger(off, parent)
        await parent.turn("hello")
        assert started == []

    asyncio.run(body())


def test_failed_turn_starts_nothing(scope: Scope) -> None:
    async def body() -> None:
        quick = with_config(scope, {"memory": {"review_interval": 1}})
        parent = make_parent(quick, ScriptedCaller([ProviderError("model fell over")]))
        started = _trigger(quick, parent)
        result = await parent.turn("hello")
        assert result.failure is not None
        assert started == []

    asyncio.run(body())


# -- the finalize step ----------------------------------------------------------------------------


def test_after_turn_hooks_run_after_persist_and_failures_are_contained(
    scope: Scope,
) -> None:
    async def body() -> None:
        parent = make_parent(scope, ScriptedCaller([text("hi")]))
        seen: list[int] = []

        def stored(state: TurnState) -> None:
            seen.append(sum(1 for r in state.rows if r.row_id is None))

        def broken(state: TurnState) -> None:
            raise RuntimeError("hook bug")

        async def later(state: TurnState) -> None:
            seen.append(-1)

        parent.runtime.after_turn.extend([stored, broken, later])
        result = await parent.turn("hello")
        assert seen == [0, -1]  # every row was stored before the hooks ran; later hooks still ran
        assert result.final_text == "hi"
        assert any("after_turn" in e for e in result.cleanup_errors)

    asyncio.run(body())
