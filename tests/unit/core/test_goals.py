"""Persistent goals (P10-24): judge flow, gates, failure counters, budget, precedence of the
person's messages, restart, commands and parsing."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest

from indus.core import goals
from indus.core.session_scheduler import MemoryMeta, StoreMeta
from indus.foundation.scope import Scope
from indus.protocol.events import GoalUpdate, Notice
from indus.state import registry, session_meta, sessions
from tests.fakes.clock import FakeClock
from tests.fakes.turn import drive
from tests.unit.core._session_host import FakeHost

NOT_DONE = '{"done": false, "reason": "still working"}'
DONE = '{"done": true, "reason": "all green"}'


@dataclass
class FakeJudge:
    """Plays ``answers`` in order (a str is the reply, an exception is raised); keeps calls."""

    answers: list[Any] = field(default_factory=list[Any])
    default: str = NOT_DONE
    clock: FakeClock | None = None
    stall: float = 0.0
    calls: list[tuple[str, list[str], str]] = field(default_factory=list)

    async def __call__(self, objective: str, subgoals: Sequence[str], response: str) -> str:
        self.calls.append((objective, list(subgoals), response))
        if self.stall and self.clock is not None:
            await self.clock.sleep(self.stall)
        step = self.answers.pop(0) if self.answers else self.default
        if isinstance(step, BaseException):
            raise step
        return str(step)


@dataclass
class FakeGates:
    runs: list[goals.GateRun] = field(default_factory=list[goals.GateRun])
    calls: list[tuple[str, int]] = field(default_factory=list)

    async def __call__(self, command: str, *, timeout_s: int, session_id: str) -> goals.GateRun:
        self.calls.append((command, timeout_s))
        return self.runs.pop(0) if self.runs else goals.GateRun(0, "ok")


def make(
    clock: FakeClock,
    judge: FakeJudge,
    gates: FakeGates | None = None,
    meta: MemoryMeta | None = None,
    host: FakeHost | None = None,
) -> tuple[goals.GoalDriver, FakeHost, MemoryMeta]:
    meta = meta if meta is not None else MemoryMeta()
    host = host if host is not None else FakeHost()
    driver = goals.GoalDriver(host, meta, clock, judge=judge, gates=gates)
    driver.attach()
    return driver, host, meta


def run(coro: Any) -> Any:
    return asyncio.run(coro)


async def set_goal(driver: goals.GoalDriver, host: FakeHost, args: str) -> goals.CommandReply:
    reply = await driver.goal_command("s1", args)
    if reply.submit is not None:
        host.submit("s1")  # the surface sends the objective as the first turn
    return reply


def test_done_on_turn_three_completes_with_no_fourth_continuation(fake_clock: FakeClock) -> None:
    judge = FakeJudge([NOT_DONE, NOT_DONE, DONE])
    driver, host, meta = make(fake_clock, judge)

    async def main() -> None:
        reply = await set_goal(driver, host, "ship the release")
        assert reply.submit == "ship the release"
        for i in range(3):
            await host.end_turn("s1", f"reply {i}")

    run(main())
    assert len(host.injected) == 2 and all(o == "goal" for _s, _t, o in host.injected)
    assert "Goal: ship the release" in host.texts()[0]
    assert [c[2] for c in judge.calls] == ["reply 0", "reply 1", "reply 2"]
    state = run(goals.GoalStore(meta).load("s1"))
    assert state.status == "done" and state.turns_used == 3
    updates = host.of_type(GoalUpdate)
    assert updates[-1].status == "done" and updates[-1].turns_used == 3
    assert any(n.text == "Goal reached: all green" for n in host.of_type(Notice))
    run(host.end_turn("s1", "more"))  # a later turn: the goal is done, nothing more happens
    assert len(judge.calls) == 3 and len(host.injected) == 2


def test_three_unreadable_verdicts_pause(fake_clock: FakeClock) -> None:
    judge = FakeJudge(["sure thing", "{not json", '{"done": "yes"}'])
    driver, host, meta = make(fake_clock, judge)

    async def main() -> None:
        await set_goal(driver, host, "x")
        for _ in range(3):
            await host.end_turn("s1", "r")

    run(main())
    state = run(goals.GoalStore(meta).load("s1"))
    assert state.status == "paused" and "unreadable 3 times" in state.paused_reason
    assert len(host.injected) == 2  # the first two failed open
    assert "auxiliary.goal_judge" in host.of_type(Notice)[-1].text


def test_five_transport_failures_pause(fake_clock: FakeClock) -> None:
    judge = FakeJudge([TimeoutError("judge timed out")] * 5)
    driver, host, meta = make(fake_clock, judge)

    async def main() -> None:
        await set_goal(driver, host, "x")
        for _ in range(5):
            await host.end_turn("s1", "r")

    run(main())
    state = run(goals.GoalStore(meta).load("s1"))
    assert state.status == "paused" and state.transport_failures == 5
    assert len(host.injected) == 4


def test_a_readable_verdict_resets_the_counters(fake_clock: FakeClock) -> None:
    judge = FakeJudge(["junk", "junk", NOT_DONE, "junk", "junk"])
    driver, host, meta = make(fake_clock, judge)

    async def main() -> None:
        await set_goal(driver, host, "x")
        for _ in range(5):
            await host.end_turn("s1", "r")

    run(main())
    assert run(goals.GoalStore(meta).load("s1")).status == "active"


def test_a_failing_gate_is_the_next_message_and_the_judge_waits(fake_clock: FakeClock) -> None:
    gates = FakeGates([goals.GateRun(1, "FAILED test_a\n2 failed"), goals.GateRun(0, "ok")])
    judge = FakeJudge([DONE])
    driver, host, meta = make(fake_clock, judge, gates)

    async def main() -> None:
        reply = await set_goal(driver, host, 'make the tests pass --gate "pytest -q"')
        assert reply.lines[-1] == "Check before done: $ pytest -q"
        await host.end_turn("s1", "I fixed it")
        assert judge.calls == []
        await host.end_turn("s1", "now really fixed")

    run(main())
    first = host.texts()[0]
    assert "$ pytest -q" in first and "2 failed" in first and "exit 1" in first
    assert gates.calls == [("pytest -q", goals.GATE_TIMEOUT_S)] * 2
    assert run(goals.GoalStore(meta).load("s1")).status == "done"


def test_a_gate_that_keeps_failing_pauses(fake_clock: FakeClock) -> None:
    gates = FakeGates([goals.GateRun(2, "boom")] * 10)
    driver, host, meta = make(fake_clock, FakeJudge(), gates)

    async def main() -> None:
        await set_goal(driver, host, 'x --gate "make check"')
        for _ in range(goals.GATE_RETRIES + 1):
            await host.end_turn("s1", "r")

    run(main())
    state = run(goals.GoalStore(meta).load("s1"))
    assert state.status == "paused" and state.paused_reason == "check failing: make check"
    assert len(host.injected) == goals.GATE_RETRIES


def test_the_turn_budget_pauses_with_a_notice(fake_clock: FakeClock) -> None:
    judge = FakeJudge()
    driver, host, meta = make(fake_clock, judge)

    async def main() -> None:
        await set_goal(driver, host, "endless")
        for _ in range(goals.DEFAULT_MAX_TURNS):
            await host.end_turn("s1", "r")

    run(main())
    state = run(goals.GoalStore(meta).load("s1"))
    assert state.status == "paused" and state.turns_used == 20
    assert len(host.injected) == 19
    assert host.of_type(Notice)[-1].text.startswith("Goal paused: 20/20 turns used")


def test_max_turns_flag(fake_clock: FakeClock) -> None:
    driver, host, meta = make(fake_clock, FakeJudge())

    async def main() -> None:
        await set_goal(driver, host, "short --max-turns 2")
        await host.end_turn("s1", "r")
        await host.end_turn("s1", "r")

    run(main())
    state = run(goals.GoalStore(meta).load("s1"))
    assert state.objective == "short" and state.max_turns == 2 and state.status == "paused"


def test_a_message_from_the_person_wins_over_a_queued_continuation(fake_clock: FakeClock) -> None:
    judge = FakeJudge(clock=fake_clock, stall=10.0)
    driver, host, _meta = make(fake_clock, judge)

    async def main() -> None:
        await set_goal(driver, host, "x")
        evaluation = asyncio.ensure_future(host.end_turn("s1", "r"))
        await fake_clock.run_until_idle()
        host.submit("s1")  # typed while the judge was thinking
        await drive(evaluation, fake_clock)
        assert host.injected == []
        host.waiting.add("s1")  # input queued in the surface
        await drive(asyncio.ensure_future(host.end_turn("s1", "answer to them")), fake_clock)
        assert host.injected == []
        host.waiting.clear()
        await drive(asyncio.ensure_future(host.end_turn("s1", "once more")), fake_clock)

    run(main())
    assert len(host.injected) == 1


def test_a_verdict_is_dropped_when_the_goal_changed_meanwhile(fake_clock: FakeClock) -> None:
    judge = FakeJudge(clock=fake_clock, stall=10.0)
    driver, host, meta = make(fake_clock, judge)

    async def main() -> None:
        await set_goal(driver, host, "x")
        evaluation = asyncio.ensure_future(host.end_turn("s1", "r"))
        await fake_clock.run_until_idle()
        reply = await driver.goal_command("s1", "pause")
        assert reply.lines == ("Goal paused: x",)
        await drive(evaluation, fake_clock)

    run(main())
    state = run(goals.GoalStore(meta).load("s1"))
    assert state.status == "paused" and state.turns_used == 0
    assert host.injected == []


def test_an_interrupted_turn_is_not_judged(fake_clock: FakeClock) -> None:
    judge = FakeJudge()
    driver, host, _meta = make(fake_clock, judge)

    async def main() -> None:
        await set_goal(driver, host, "x")
        await host.end_turn("s1", "", completed=False)

    run(main())
    assert judge.calls == [] and host.injected == []


def test_the_goal_survives_a_restart(fake_clock: FakeClock) -> None:
    meta = MemoryMeta()
    driver, host, _ = make(fake_clock, FakeJudge(), meta=meta)
    run(set_goal(driver, host, "keep going"))
    run(host.end_turn("s1", "r"))
    # a new process: new host and driver over the same store
    judge = FakeJudge([DONE])
    driver2, host2, _ = make(fake_clock, judge, meta=meta)
    status = run(driver2.goal_command("s1", "status"))
    assert status.lines == ("Goal active (1/20 turns): keep going",)
    run(host2.end_turn("s1", "done now"))
    assert run(goals.GoalStore(meta).load("s1")).status == "done"


def test_pause_resume_clear_and_status(fake_clock: FakeClock) -> None:
    driver, host, meta = make(fake_clock, FakeJudge())

    async def main() -> list[goals.CommandReply]:
        out = [await driver.goal_command("s1", "status")]
        await set_goal(driver, host, "x")
        await host.end_turn("s1", "r")
        out.append(await driver.goal_command("s1", "pause"))
        out.append(await driver.goal_command("s1", "status"))
        out.append(await driver.goal_command("s1", "resume"))
        out.append(await driver.goal_command("s1", "clear"))
        out.append(await driver.goal_command("s1", "status"))
        return out

    none, paused, status, resumed, cleared, after = run(main())
    assert none.lines[0] == "No goal is set."
    assert paused.lines == ("Goal paused: x",)
    assert status.lines == ("Goal paused (1/20 turns — paused by you): x",)
    assert resumed.lines == ("Goal resumed: x",) and resumed.submit is not None
    assert "Goal: x" in resumed.submit
    assert cleared.lines == ("Goal cleared.",)
    assert after.lines[0] == "No goal is set."
    assert [u.status for u in host.of_type(GoalUpdate)][-1] == "cleared"


def test_subgoals_reach_the_judge_and_the_continuation(fake_clock: FakeClock) -> None:
    judge = FakeJudge()
    driver, host, _meta = make(fake_clock, judge)

    async def main() -> list[goals.CommandReply]:
        none = await driver.subgoal_command("s1", "add early")
        await set_goal(driver, host, "write the docs")
        out = [none]
        out.append(await driver.subgoal_command("s1", "add cover the CLI"))
        out.append(await driver.subgoal_command("s1", "add cover the TUI"))
        out.append(await driver.subgoal_command("s1", "done 2"))
        out.append(await driver.subgoal_command("s1", "list"))
        out.append(await driver.subgoal_command("s1", "done 9"))
        await host.end_turn("s1", "r")
        return out

    none, added, _second, met, listed, bad = run(main())
    assert none.lines[0].startswith("Set a goal first")
    assert added.lines == ("Criterion 1 added.",)
    assert met.lines == ("Criterion met and removed: cover the TUI",)
    assert listed.lines == ("1. cover the CLI",)
    assert bad.lines == ("Give a criterion number from 1 to 1.",)
    assert judge.calls[0][1] == ["cover the CLI"]
    assert "- 1. cover the CLI" in host.texts()[0]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ship it", ("ship it", None, [])),
        ('make tests pass --gate "pytest -q" --max-turns 5', ("make tests pass", 5, ["pytest -q"])),
        ("a --gate 'ruff check .' --gate make b", ("a b", None, ["ruff check .", "make"])),
        ("--max-turns=3 do it", ("do it", 3, [])),
        ("x --max-turns zero", "--max-turns needs a whole number above 0, not 'zero'"),
        ("--gate pytest", goals.parse_goal_args("")),
    ],
)
def test_parse_goal_args(text: str, expected: object) -> None:
    assert goals.parse_goal_args(text) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (DONE, goals.Verdict(True, "all green")),
        (
            '```json\n{"done": false, "reason": "no", "next": "run tests"}\n```',
            goals.Verdict(False, "no", "run tests"),
        ),
        ('Verdict: {"done": true} — fine', goals.Verdict(True, "")),
        ('{"done": "true"}', None),
        ("nothing here", None),
        ("", None),
    ],
)
def test_parse_verdict(raw: str, expected: goals.Verdict | None) -> None:
    assert goals.parse_verdict(raw) == expected


def test_continuation_hint_from_the_judge(fake_clock: FakeClock) -> None:
    judge = FakeJudge(['{"done": false, "reason": "r", "next": "run the linter"}'])
    driver, host, _ = make(fake_clock, judge)
    run(set_goal(driver, host, "x"))
    run(host.end_turn("s1", "r"))
    assert "Suggested next step: run the linter" in host.texts()[0]


def test_aux_judge_asks_the_goal_judge_task() -> None:
    from tests.fakes.aux_model import FakeAuxModel

    aux = FakeAuxModel(DONE)
    judge = goals.AuxJudge(aux)
    raw = asyncio.run(judge("obj", ["c1"], "y" * 9_000))
    assert raw == DONE
    (call,) = aux.calls
    assert call.task == goals.JUDGE_TASK
    assert call.options["max_tokens"] == goals.JUDGE_MAX_TOKENS
    assert call.options["timeout_s"] == goals.JUDGE_TIMEOUT_S
    user = call.messages[1]["content"]
    assert "- 1. c1" in user and user.endswith("y" * goals.RESPONSE_CHARS)
    assert len(user) < goals.RESPONSE_CHARS + 300


@pytest.fixture
def handle(scope: Scope) -> Iterator[registry.StoreHandle]:
    opened = registry.open_unshared(scope)
    yield opened
    opened.close()


def test_store_meta_creates_the_session_row_and_restores(
    handle: registry.StoreHandle, fake_clock: FakeClock
) -> None:
    meta = StoreMeta(handle)
    driver, host, _ = make(fake_clock, FakeJudge(), meta=MemoryMeta())
    store_driver = goals.GoalDriver(host, meta, fake_clock, judge=FakeJudge())
    run(store_driver.goal_command("fresh", "stored goal"))
    assert sessions.get(handle, "fresh") is not None
    raw = session_meta.get(handle, "fresh", goals.NAMESPACE, "goal")
    assert raw["objective"] == "stored goal" and raw["status"] == "active"
    again = goals.GoalDriver(FakeHost(), StoreMeta(handle), fake_clock, judge=FakeJudge())
    assert run(again.goal_command("fresh", "status")).lines[0].endswith(": stored goal")
    del driver


def test_a_reply_without_text_is_not_judged(fake_clock: FakeClock) -> None:
    judge = FakeJudge()
    driver, host, meta = make(fake_clock, judge)

    async def main() -> None:
        await set_goal(driver, host, "x")
        await host.end_turn("s1", "   ")

    run(main())
    assert judge.calls == [] and len(host.injected) == 1
    state = run(goals.GoalStore(meta).load("s1"))
    assert state.turns_used == 1 and state.parse_failures == state.transport_failures == 0
