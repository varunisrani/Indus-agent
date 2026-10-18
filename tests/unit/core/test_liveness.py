"""Liveness watchdog and run budget (P08-16, A63, A34)."""

from __future__ import annotations

import asyncio
import math
import threading
from typing import Any

import pytest

from indus.core.exit_reasons import ExitReason
from indus.core.liveness import (
    DEFAULT_LIVENESS_S,
    WRAP_UP_NOTICE,
    LivenessSettings,
    RunBudget,
    Watchdog,
    cap_timeout,
    run_budget_from,
)
from indus.core.prompt.lifecycle import PromptLifecycle
from indus.core.tool_exec import ExecLimits
from indus.core.transcript import TranscriptRow
from indus.core.turn_state import LoopOptions, SessionRuntime, TurnState, new_turn_state
from indus.foundation.scope import Scope
from indus.protocol.events import Notice
from tests.fakes.clock import FakeClock
from tests.fakes.model import text_turn, tool_batch
from tests.fakes.turn import StubBuilder, TurnKit, check_alternation


class _NoCaller:
    model = "none"

    async def call(self, request: Any, *, on_event: Any, on_wait: Any = None) -> Any:
        raise AssertionError("not called in these tests")


def _state(scope: Scope, clock: FakeClock, generation: int = 1) -> TurnState:
    from indus.core.prompt.builder import BuildContext

    runtime = SessionRuntime(
        session_id="s1",
        caller=_NoCaller(),
        clock=clock,
        prompts=PromptLifecycle(StubBuilder()),
        prompt_context=BuildContext(scope=scope, model="none", provider="fake", platform="cli"),
        scope=scope,
    )
    events: list[Any] = []
    state = new_turn_state(
        runtime, message="hi", turn_id="t1", generation=generation, sink=events.append
    )
    state.control.phase = "tools"
    state.touch("tool round")
    state.events = events  # type: ignore[attr-defined]
    return state


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, DEFAULT_LIVENESS_S),
        (600, 600.0),
        ("120", 120.0),
        (0, None),
        (-5, None),
        ("soon", DEFAULT_LIVENESS_S),
        (math.nan, DEFAULT_LIVENESS_S),
        (math.inf, DEFAULT_LIVENESS_S),
        (True, DEFAULT_LIVENESS_S),
        ([1], DEFAULT_LIVENESS_S),
    ],
)
def test_settings_table(raw: object, expected: float | None) -> None:
    assert LivenessSettings.from_value(raw).timeout_s == expected


def test_idle_turn_aborted_at_600_and_activity_at_599_keeps_it(
    scope: Scope, fake_clock: FakeClock
) -> None:
    state = _state(scope, fake_clock)
    dog = Watchdog(state, LivenessSettings())
    fake_clock.advance(599)
    assert dog.tick() and not state.ended
    state.touch("tool heartbeat")
    fake_clock.advance(599)
    assert dog.tick() and not state.ended
    fake_clock.advance(1)
    assert not dog.tick()
    assert state.exit is ExitReason.INTERRUPTED_BY_SYSTEM
    assert state.failure is not None and state.failure.reason == "liveness"
    assert state.control.interrupted and state.control.by_system and state.control.stop.is_set()
    notices = [e for e in state.events if isinstance(e, Notice)]  # type: ignore[attr-defined]
    assert len(notices) == 1 and "600 s" in notices[0].text
    assert dog.fired == "liveness"


def test_stale_generation_never_aborted(scope: Scope, fake_clock: FakeClock) -> None:
    state = _state(scope, fake_clock, generation=1)
    dog = Watchdog(state, LivenessSettings(timeout_s=10))
    state.control.generation = 2  # a newer turn took over this control
    fake_clock.advance(1000)
    assert not dog.tick()
    assert not state.ended and not state.control.interrupted


def test_touch_between_observe_and_commit_vetoes(scope: Scope, fake_clock: FakeClock) -> None:
    state = _state(scope, fake_clock)
    dog = Watchdog(state, LivenessSettings(timeout_s=10))
    fake_clock.advance(50)
    seen = dog.observe()
    assert seen is not None and seen.idle_s >= 10
    dog.heartbeat("stream chunk")
    assert not dog.commit(seen, "liveness")
    assert not state.ended and not state.control.interrupted
    assert state.activity_note == "stream chunk"


def test_ended_turn_is_left_alone(scope: Scope, fake_clock: FakeClock) -> None:
    state = _state(scope, fake_clock)
    dog = Watchdog(state, LivenessSettings(timeout_s=10))
    state.end(ExitReason.COMPLETED, final_text="done")
    fake_clock.advance(100)
    assert not dog.tick()
    assert state.exit is ExitReason.COMPLETED


def test_threaded_heartbeat_keeps_the_turn_alive(scope: Scope, fake_clock: FakeClock) -> None:
    state = _state(scope, fake_clock)
    dog = Watchdog(state, LivenessSettings(timeout_s=10))
    fake_clock.advance(9)
    worker = threading.Thread(target=dog.heartbeat, args=("worker",))
    worker.start()
    worker.join()
    fake_clock.advance(9)
    assert dog.tick() and not state.ended


def test_budget_spent_ends_budget_exhausted(scope: Scope, fake_clock: FakeClock) -> None:
    state = _state(scope, fake_clock)
    budget = RunBudget(100, fake_clock)
    dog = Watchdog(state, LivenessSettings(timeout_s=None), budget=budget)
    fake_clock.advance(99)
    state.touch("busy")
    assert dog.tick()
    fake_clock.advance(1)
    assert not dog.tick()
    assert state.exit is ExitReason.BUDGET_EXHAUSTED and state.control.interrupted
    assert dog.fired == "budget"


def _tool(content: object, *, stored: bool = False) -> TranscriptRow:
    row = TranscriptRow(role="tool", tool_call_id="c1", content=content)
    row.row_id = 7 if stored else None
    return row


def test_wrap_up_only_on_unstored_tool_row(fake_clock: FakeClock) -> None:
    budget = RunBudget(100, fake_clock)
    rows = [TranscriptRow(role="user", content="go"), _tool("old", stored=True), _tool("new")]
    fake_clock.advance(79)
    assert not budget.wrap_up(rows)
    fake_clock.advance(1)
    assert budget.wrap_up(rows)
    assert rows[2].content == f"new\n\n{WRAP_UP_NOTICE}" and rows[1].content == "old"
    assert not budget.wrap_up(rows)  # once only
    assert str(rows[2].content).count(WRAP_UP_NOTICE) == 1


def test_wrap_up_waits_when_the_newest_tool_row_is_stored(fake_clock: FakeClock) -> None:
    budget = RunBudget(10, fake_clock)
    fake_clock.advance(9)
    rows = [_tool("stored", stored=True)]
    assert not budget.wrap_up(rows) and not budget.wrapped_up
    rows.append(_tool([{"type": "text", "text": "parts"}]))
    assert budget.wrap_up(rows)
    assert rows[1].content == [
        {"type": "text", "text": "parts"},
        {"type": "text", "text": WRAP_UP_NOTICE},
    ]


@pytest.mark.parametrize(
    ("seconds", "budget_s", "elapsed", "explicit", "expected"),
    [
        (1800.0, None, 0, False, 1800.0),
        (1800.0, 1000.0, 0, False, 500.0),
        (1800.0, 1000.0, 900, False, 60.0),
        (30.0, 1000.0, 900, False, 30.0),
        (1800.0, 1000.0, 900, True, 1800.0),
        (1800.0, 4000.0, 1000, False, 1500.0),
    ],
)
def test_cap_timeout_table(
    fake_clock: FakeClock,
    seconds: float,
    budget_s: float | None,
    elapsed: float,
    explicit: bool,
    expected: float,
) -> None:
    budget = RunBudget(budget_s, fake_clock) if budget_s is not None else None
    fake_clock.advance(elapsed)
    assert cap_timeout(seconds, budget, explicit=explicit) == expected


def test_run_budget_from_config_values(fake_clock: FakeClock) -> None:
    assert run_budget_from(None, fake_clock) is None
    assert run_budget_from(0, fake_clock) is None
    assert run_budget_from("bad", fake_clock) is None
    budget = run_budget_from(90, fake_clock)
    assert budget is not None and budget.seconds == 90


# -- through the real loop ---------------------------------------------------------------------


# A wedged tool: no deadline and no executor heartbeat, so nothing marks activity.
WEDGED = LoopOptions(
    exec_limits=ExecLimits(batch_deadline_s=None, call_deadline_s=None, heartbeat_s=1e9)
)


def test_hung_tool_turn_is_stopped_by_the_watchdog(scope: Scope, fake_clock: FakeClock) -> None:
    turns = [tool_batch(("slow", {"seconds": 5000}))]
    kit = TurnKit(scope, fake_clock, turns, options=WEDGED, step=5.0)
    result = asyncio.run(kit.turn("go"))
    assert result.exit_reason is ExitReason.INTERRUPTED_BY_SYSTEM
    assert result.failure is not None and result.failure.reason == "liveness"
    assert 600 <= fake_clock.monotonic() < 700
    check_alternation(result.rows)


def test_quiet_tool_under_the_limit_keeps_the_turn(scope: Scope, fake_clock: FakeClock) -> None:
    # the model call before the tool takes a few fake seconds; the tool is quiet for 590 s
    turns = [tool_batch(("slow", {"seconds": 590})), text_turn("done")]
    kit = TurnKit(scope, fake_clock, turns, options=WEDGED)
    result = asyncio.run(kit.turn("go"))
    assert result.exit_reason is ExitReason.COMPLETED and result.final_text == "done"


def test_run_budget_wraps_up_once_then_ends_the_run(scope: Scope, fake_clock: FakeClock) -> None:
    turns = [
        tool_batch(("slow", {"seconds": 50})),
        tool_batch(("slow", {"seconds": 35})),
        tool_batch(("slow", {"seconds": 5})),
        tool_batch(("slow", {"seconds": 500})),
    ]
    kit = TurnKit(scope, fake_clock, turns, options=WEDGED)

    def prepare(state: TurnState) -> None:
        state.runtime.run_budget = RunBudget(100, fake_clock)

    result = asyncio.run(kit.turn("go", prepare=prepare))
    tools = [r for r in result.rows if r.role == "tool"]
    marked = [r for r in tools if WRAP_UP_NOTICE in str(r.content)]
    assert len(marked) == 1 and marked[0] is tools[1]
    assert result.exit_reason is ExitReason.BUDGET_EXHAUSTED
    check_alternation(result.rows)
