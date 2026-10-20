"""The synchronous batch runner (P10-03): cap, order, failures, interrupts, liveness, truncation."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from indus.core.persist import MemoryStore
from indus.core.subagents.batch import (
    HEARTBEAT_S,
    STALE_IDLE_CYCLES,
    STALE_TOOL_CYCLES,
    ChildOutcome,
    Progress,
    clip_summary,
    result_entry,
    run_batch,
)
from indus.foundation.scope import Scope, scope_or_none
from indus.protocol.events import ExitReason
from tests.fakes.agent import eventually, run_driven
from tests.fakes.clock import FakeClock, checkpoint
from tests.fakes.model import Finish, Stall, Text, Turn, text_turn, tool_batch
from tests.fakes.subagents import RoutedModel, parent_agent
from tests.fakes.turn import check_alternation


class Probe:
    def __init__(self) -> None:
        self.now = 0
        self.peak = 0

    def enter(self) -> None:
        self.now += 1
        self.peak = max(self.peak, self.now)

    def leave(self) -> None:
        self.now -= 1


class FakeRun:
    """A child for the runner: works ``work_s`` fake seconds, or until interrupted."""

    def __init__(
        self,
        ident: str,
        clock: FakeClock,
        probe: Probe | None = None,
        *,
        work_s: float = 5.0,
        fail: bool = False,
        hang: bool = False,
        in_tool: bool = False,
    ) -> None:
        self.ident = ident
        self.clock = clock
        self.probe = probe or Probe()
        self.work_s = work_s
        self.fail = fail
        self.hang = hang
        self.in_tool = in_tool
        self.stopped = asyncio.Event()
        self.started = False
        self.interrupts = 0
        self.interrupted_at: float | None = None
        self.seen_scope: Scope | None = None
        self.ticks = 0

    @property
    def subagent_id(self) -> str:
        return self.ident

    @property
    def goal(self) -> str:
        return f"goal of {self.ident}"

    async def run(self) -> ChildOutcome:
        self.started = True
        self.seen_scope = scope_or_none()
        self.probe.enter()
        try:
            if self.fail:
                raise RuntimeError("the child broke")
            if self.hang:
                await self.stopped.wait()
                return ChildOutcome("interrupted", summary="partial")
            sleeper = asyncio.ensure_future(self.clock.sleep(self.work_s))
            stopper = asyncio.ensure_future(self.stopped.wait())
            done, pending = await asyncio.wait(
                {sleeper, stopper}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            if stopper in done:
                return ChildOutcome("interrupted", summary=f"{self.ident} partial")
            return ChildOutcome("completed", summary=f"summary of {self.ident}")
        finally:
            self.probe.leave()

    def interrupt(self) -> None:
        self.interrupts += 1
        if self.interrupted_at is None:
            self.interrupted_at = self.clock.monotonic()
        self.stopped.set()

    def progress(self) -> Progress:
        return Progress(("same", self.ticks), in_tool=self.in_tool)


def test_cap_is_respected_and_results_keep_task_order(scope: Scope, fake_clock: FakeClock) -> None:
    probe = Probe()
    # Later tasks finish first, so completion order is the reverse of task order.
    runs = [FakeRun(f"sa-{i}", fake_clock, probe, work_s=float(30 - i * 2)) for i in range(12)]

    async def main() -> list[ChildOutcome]:
        return await run_batch(runs, cap=10, clock=fake_clock, scope=scope)

    outcomes = asyncio.run(run_driven(main(), fake_clock))
    assert probe.peak == 10
    assert [o.summary for o in outcomes] == [f"summary of sa-{i}" for i in range(12)]
    assert all(o.status == "completed" for o in outcomes)
    # R01.16: each child ran with the batch's scope in its own context.
    assert all(r.seen_scope is scope for r in runs)


def test_one_child_raising_fails_alone(fake_clock: FakeClock) -> None:
    runs = [
        FakeRun("ok-1", fake_clock),
        FakeRun("bad", fake_clock, fail=True),
        FakeRun("ok-2", fake_clock),
    ]
    outcomes = asyncio.run(run_driven(run_batch(runs, cap=3, clock=fake_clock), fake_clock))
    assert [o.status for o in outcomes] == ["completed", "failed", "completed"]
    assert "the child broke" in outcomes[1].note


def test_parent_stop_interrupts_running_and_skips_queued(fake_clock: FakeClock) -> None:
    runs = [FakeRun(f"sa-{i}", fake_clock, hang=True) for i in range(3)]

    async def main() -> list[ChildOutcome]:
        stop = asyncio.Event()
        batch = asyncio.ensure_future(run_batch(runs, cap=2, clock=fake_clock, stop=stop))
        await eventually(lambda: runs[0].started and runs[1].started)
        stop.set()
        return await batch

    outcomes = asyncio.run(main())
    assert [o.status for o in outcomes] == ["interrupted", "interrupted", "interrupted"]
    assert runs[0].interrupts == 1 and runs[1].interrupts == 1
    assert not runs[2].started
    assert "never started" in outcomes[2].note


def test_quiet_child_goes_stale_after_450_s(fake_clock: FakeClock) -> None:
    quiet = FakeRun("quiet", fake_clock, hang=True)
    beats: list[float] = []

    async def main() -> list[ChildOutcome]:
        return await run_batch(
            [quiet],
            cap=1,
            clock=fake_clock,
            on_beat=lambda: beats.append(fake_clock.monotonic()),
        )

    start = fake_clock.monotonic()
    outcomes = asyncio.run(run_driven(main(), fake_clock, step=HEARTBEAT_S))
    assert outcomes[0].status == "stale"
    assert outcomes[0].summary == "partial"
    assert quiet.interrupts == 1
    # Stopped at the 15th quiet check: 450 s after the child started. The driver keeps moving the
    # fake clock while the runner winds down, so beats after the stop are not counted.
    assert quiet.interrupted_at is not None
    assert quiet.interrupted_at - start == STALE_IDLE_CYCLES * HEARTBEAT_S == 450
    assert beats[STALE_IDLE_CYCLES - 1] - start == 450


def test_a_child_inside_one_tool_gets_longer(fake_clock: FakeClock) -> None:
    busy = FakeRun("busy", fake_clock, hang=True, in_tool=True)

    async def main() -> tuple[int, list[ChildOutcome]]:
        batch = asyncio.ensure_future(run_batch([busy], cap=1, clock=fake_clock))
        for _ in range(STALE_IDLE_CYCLES + 2):  # past the idle limit: still running
            await _beat(fake_clock)
        early = busy.interrupts
        for _ in range(STALE_TOOL_CYCLES - STALE_IDLE_CYCLES):
            await _beat(fake_clock)
        return early, await batch

    early, outcomes = asyncio.run(main())
    assert early == 0
    assert outcomes[0].status == "stale"


def test_progress_resets_the_stale_count(fake_clock: FakeClock) -> None:
    lively = FakeRun("lively", fake_clock, hang=True)

    async def main() -> int:
        batch = asyncio.ensure_future(run_batch([lively], cap=1, clock=fake_clock))
        for _ in range(STALE_IDLE_CYCLES * 3):
            lively.ticks += 1
            await _beat(fake_clock)
        interrupts = lively.interrupts
        lively.interrupt()
        await batch
        return interrupts

    assert asyncio.run(main()) == 0


async def _beat(clock: FakeClock) -> None:
    for _ in range(5):
        await checkpoint()
    clock.advance(HEARTBEAT_S)
    for _ in range(5):
        await checkpoint()


# -- truncation -----------------------------------------------------------------------------------


def test_summary_over_the_cap_keeps_head_and_tail_with_a_marker() -> None:
    text = "HEAD" + "x" * 30_000 + "TAIL"
    clipped = clip_summary(text, 2_000)
    assert len(clipped) <= 2_000
    assert clipped.startswith("HEAD") and clipped.endswith("TAIL")
    found = re.search(
        r"\n\n\[\.\.\. ([\d,]+) characters of this summary were cut here \.\.\.\]\n\n", clipped
    )
    assert found is not None
    kept = len(clipped) - len(found.group(0))
    assert int(found.group(1).replace(",", "")) == len(text) - kept
    head = clipped[: found.start()]
    assert len(head) > len(clipped[found.end() :])  # the head gets the larger share
    assert clip_summary("short", 2_000) == "short"


def test_result_entry_shape(fake_clock: FakeClock) -> None:
    run = FakeRun("sa-0", fake_clock)
    outcome = ChildOutcome(
        "completed",
        summary="y" * 5_000,
        schema_valid=False,
        errors=("$.a: missing",),
        note="n",
        orphaned_processes=({"id": "proc_1", "command": "sleep", "action": "killed"},),
        worktree={"path": "/w", "branch": "b", "kept": True},
    )
    entry = result_entry(run, outcome, 2_000)
    assert entry["subagent_id"] == "sa-0" and entry["goal"] == "goal of sa-0"
    assert len(entry["summary"]) <= 2_000
    assert entry["schema_valid"] is False and entry["errors"] == ["$.a: missing"]
    assert entry["orphaned_processes"][0]["id"] == "proc_1"
    assert entry["worktree"]["kept"] is True
    bare = result_entry(run, ChildOutcome("completed", summary="s"), 2_000)
    assert set(bare) == {"subagent_id", "goal", "status", "summary"}


# -- real children --------------------------------------------------------------------------------


def stalled(text: str) -> Turn:
    return Turn((Text((text,)), Stall(100_000.0), Finish("stop")))


def test_parent_interrupt_reaches_every_child_and_transcripts_stay_valid(
    scope: Scope, fake_clock: FakeClock
) -> None:
    goals = ["first long job", "second long job"]
    model = RoutedModel(
        fake_clock,
        parent=[tool_batch(("delegate_task", {"tasks": [{"goal": g} for g in goals]}))],
        children={g: [stalled("working on it")] for g in goals},
    )
    store = MemoryStore()

    async def main() -> Any:
        parent = await parent_agent(scope, model, store=store)
        running = await parent.start_turn("split the work")
        await eventually(lambda: all(model.child(g).model_requests for g in goals))
        assert parent.interrupt()
        result = await running.task
        await parent.close()
        return result

    result = asyncio.run(main())
    assert result.exit_reason in (ExitReason.INTERRUPTED_BY_USER, ExitReason.INTERRUPTED_BY_SYSTEM)
    children = [sid for sid, row in store.sessions.items() if row.get("source") == "subagent"]
    assert len(children) == 2
    for sid in children:
        rows = store.rows.get(sid, [])
        assert rows and rows[0].role == "user"
        check_alternation(rows)
    check_alternation(store.rows["parent"])


def test_child_usage_rolls_up_to_the_parent(scope: Scope, fake_clock: FakeClock) -> None:
    model = RoutedModel(
        fake_clock,
        parent=[tool_batch(("delegate_task", {"tasks": [{"goal": "tiny"}]})), text_turn("ok")],
        children={"tiny": [text_turn("did it")]},
    )

    async def main() -> Any:
        parent = await parent_agent(scope, model)
        result = await parent.run_turn("go")
        await parent.close()
        return parent, result

    parent, result = asyncio.run(run_driven(main(), fake_clock))
    body = json.loads(next(r.content for r in result.rows if r.role == "tool"))
    assert body["results"][0]["status"] == "completed"
    assert parent.usage.side_calls.get("subagent") == 1


def test_one_shot_runs_wait_and_spend_a_total_budget(scope: Scope, fake_clock: FakeClock) -> None:
    model = RoutedModel(
        fake_clock,
        parent=[
            tool_batch(("delegate_task", {"tasks": [{"goal": "part one"}, {"goal": "part two"}]})),
            tool_batch(("delegate_task", {"goal": "part three"})),
            text_turn("done"),
        ],
        children={"part one": [text_turn("1")], "part two": [text_turn("2")]},
    )

    async def main() -> Any:
        parent = await parent_agent(scope, model)
        parent.runtime.run_context = "single_query"
        result = await parent.run_turn("do it")
        await parent.close()
        return result

    result = asyncio.run(run_driven(main(), fake_clock))
    first, second = [json.loads(r.content) for r in result.rows if r.role == "tool"]
    assert [e["status"] for e in first["results"]] == ["completed", "completed"]
    assert "may start 2 subagents in total" in second["error"]


def test_no_deeper_than_max_depth(scope: Scope, fake_clock: FakeClock) -> None:
    model = RoutedModel(
        fake_clock,
        parent=[tool_batch(("delegate_task", {"goal": "nested"})), text_turn("fine")],
    )

    async def main() -> Any:
        child_like = await parent_agent(scope, model, delegation_depth=1)
        result = await child_like.run_turn("go")
        await child_like.close()
        return result

    result = asyncio.run(run_driven(main(), fake_clock))
    body = json.loads(next(r.content for r in result.rows if r.role == "tool"))
    assert "depth limit" in body["error"]
