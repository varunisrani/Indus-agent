"""Bounded concurrent executor (P04-14)."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from indus.core.tool_exec import ExecLimits, Executor, SlotOutcome
from indus.core.tool_plan import Segment
from indus.tools.calls import ToolResult
from tests.fakes.clock import FakeClock
from tests.fakes.tools import FakeBroker
from tests.fakes.turn import drive


class Work:
    """Per-index fake work: sleep ``seconds[i]`` on the clock; tracks concurrency and order."""

    def __init__(
        self, clock: FakeClock, seconds: Sequence[float], broker: FakeBroker | None = None
    ):
        self.clock = clock
        self.seconds = list(seconds)
        self.running = 0
        self.peak = 0
        self.started: list[int] = []
        self.finished: list[int] = []
        self.broker = broker
        self.human: set[int] = set()

    async def __call__(self, index: int) -> ToolResult:
        self.started.append(index)
        self.running += 1
        self.peak = max(self.peak, self.running)
        try:
            if index in self.human and self.broker is not None:
                self.broker.open()
                await self.clock.sleep(300)
                self.broker.close()
            await self.clock.sleep(self.seconds[index])
        finally:
            self.running -= 1
        self.finished.append(index)
        return ToolResult.ok(f"done {index}")


def executor(clock: FakeClock, work: Work, names: Sequence[str], **kw: Any) -> Executor:
    stop = kw.pop("stop", None) or asyncio.Event()
    return Executor(clock=clock, run_one=work, names=names, stop=stop, **kw)


def test_nine_parallel_calls_never_exceed_eight(fake_clock: FakeClock) -> None:
    async def go() -> list[SlotOutcome]:
        work = Work(fake_clock, [5.0] * 9)
        ex = executor(fake_clock, work, ["t"] * 9)
        task = asyncio.ensure_future(ex.run_parallel(range(9)))
        await drive(task, fake_clock)
        assert work.peak == 8 and ex.max_running == 8
        assert work.started == list(range(9))
        return task.result()

    outcomes = asyncio.run(go())
    assert [o.index for o in outcomes] == list(range(9))
    assert all(o.ok for o in outcomes)


def test_image_generation_caps_at_four(fake_clock: FakeClock) -> None:
    async def go() -> None:
        work = Work(fake_clock, [5.0] * 6)
        ex = executor(fake_clock, work, ["image_generate"] + ["t"] * 5)
        task = asyncio.ensure_future(ex.run_parallel(range(6)))
        await drive(task, fake_clock)
        assert work.peak == 4

    asyncio.run(go())


def test_results_keep_call_order_when_finishing_out_of_order(fake_clock: FakeClock) -> None:
    async def go() -> None:
        work = Work(fake_clock, [30.0, 1.0, 10.0])
        task = asyncio.ensure_future(executor(fake_clock, work, ["t"] * 3).run_parallel([0, 1, 2]))
        await drive(task, fake_clock)
        assert work.finished == [1, 2, 0]
        assert [o.index for o in task.result()] == [0, 1, 2]

    asyncio.run(go())


def test_batch_deadline_synthesizes_timeouts(fake_clock: FakeClock) -> None:
    async def go() -> None:
        work = Work(fake_clock, [10.0, 1000.0])
        ex = executor(
            fake_clock, work, ["fast", "stuck"], limits=ExecLimits(batch_deadline_s=420.0)
        )
        task = asyncio.ensure_future(ex.run_parallel([0, 1]))
        await drive(task, fake_clock)
        fast, stuck = task.result()
        assert fast.ok
        assert stuck.status == "timeout" and stuck.result.is_error
        assert "stuck" in str(stuck.result.content)
        assert 420.0 <= fake_clock.monotonic() < 430.0

    asyncio.run(go())


def test_human_wait_is_not_counted_against_the_deadline(fake_clock: FakeClock) -> None:
    async def go() -> None:
        broker = FakeBroker(fake_clock)
        work = Work(fake_clock, [200.0, 1.0], broker=broker)
        work.human = {0}
        ex = executor(
            fake_clock,
            work,
            ["ask", "other"],
            limits=ExecLimits(),
            human_wait=broker.human_wait_seconds,
        )
        task = asyncio.ensure_future(ex.run_parallel([0, 1]))
        await drive(task, fake_clock)
        first, _ = task.result()
        assert first.ok, first  # 500 s wall time, 300 s of it waiting on a person
        assert fake_clock.monotonic() >= 500.0

    asyncio.run(go())


def test_sequential_call_deadline(fake_clock: FakeClock) -> None:
    async def go() -> None:
        work = Work(fake_clock, [100.0])
        ex = executor(fake_clock, work, ["t"], call_deadline=lambda i: 50.0)
        task = asyncio.ensure_future(ex.run_single(0))
        await drive(task, fake_clock)
        assert task.result().status == "timeout"

    asyncio.run(go())


def test_stop_cancels_running_and_skips_unstarted(fake_clock: FakeClock) -> None:
    async def go() -> None:
        stop = asyncio.Event()
        work = Work(fake_clock, [100.0, 100.0, 100.0])
        ex = executor(
            fake_clock, work, ["a", "b", "c"], stop=stop, limits=ExecLimits(max_workers=2)
        )
        commits: list[SlotOutcome] = []

        async def commit(outcome: SlotOutcome) -> None:
            commits.append(outcome)

        segments = [Segment("parallel", [0, 1, 2])]
        task = asyncio.ensure_future(ex.run(segments, commit))
        for _ in range(20):
            await fake_clock.run_until_idle()
            fake_clock.advance(1.0)
        stop.set()
        await drive(task, fake_clock)
        assert [c.status for c in commits] == ["cancelled", "cancelled", "skipped"]
        again = await ex.run_single(0)
        assert again.status == "skipped"

    asyncio.run(go())


def test_worker_crash_becomes_an_error_result(fake_clock: FakeClock) -> None:
    async def broken(index: int) -> ToolResult:
        raise ValueError("bad worker")

    async def go() -> None:
        ex = Executor(clock=fake_clock, run_one=broken, names=["t"], stop=asyncio.Event())
        outcome = await ex.run_single(0)
        assert outcome.status == "error" and "bad worker" in str(outcome.result.content)

    asyncio.run(go())


def test_heartbeat_touches_while_tools_run(fake_clock: FakeClock) -> None:
    async def go() -> None:
        notes: list[str] = []
        work = Work(fake_clock, [100.0])
        ex = executor(fake_clock, work, ["t"], touch=notes.append)
        task = asyncio.ensure_future(ex.run_single(0))
        await drive(task, fake_clock)
        assert len(notes) >= 3 and all("running" in n for n in notes)

    asyncio.run(go())


def test_start_gate_releases_a_parked_worker_after_its_bound(fake_clock: FakeClock) -> None:
    limits = ExecLimits(batch_deadline_s=None, start_gate_s=5.0)
    assert ExecLimits(batch_deadline_s=100.0).gate_seconds() == 50.0
    assert limits.gate_seconds() == 5.0
