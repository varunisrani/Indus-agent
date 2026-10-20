"""The fake-clock driver holds time still while a blocking-pool call runs (tests/fakes/turn.drive).

Real work on a worker thread leaves the loop looking idle; before the hold the driver moved the
clock about 2000 fake seconds per real second meanwhile, so deadlines raced against that work
expired at once on a loaded machine (commits 077ef96, 0e9a538).
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator

import pytest

from indus.core.agent import Agent
from indus.core.exit_reasons import ExitReason
from indus.foundation.blocking import BlockingPool, DeadlineMissed, awaited_calls
from indus.foundation.scope import Scope
from indus.tools.registry import ToolRegistry
from tests.fakes import turn
from tests.fakes.agent import fake_options, run_driven
from tests.fakes.clock import FakeClock
from tests.fakes.model import FakeModelServer, fake_route, script_of, text_turn, tool_batch

# Real time a "slow" job spends on its worker thread; the old driver moved the clock some 250
# fake seconds meanwhile on an idle machine.
REAL_WORK_S = 0.3
# Real time the probe tool spends: on the old driver, well past its 420 s deadline on any machine.
PROBE_WORK_S = 1.0


def real_work(seconds: float = REAL_WORK_S) -> None:
    threading.Event().wait(seconds)


@pytest.fixture
def pool() -> Iterator[BlockingPool]:
    made = BlockingPool("drive-probe", 2)
    yield made
    made.shutdown()


def test_the_clock_holds_while_a_pool_call_runs(fake_clock: FakeClock, pool: BlockingPool) -> None:
    seen: list[float] = []

    def job() -> str:
        seen.append(fake_clock.monotonic())
        real_work()
        seen.append(fake_clock.monotonic())
        return "done"

    async def main() -> str:
        return await pool.run(job, deadline=420.0, clock=fake_clock)

    start = fake_clock.monotonic()
    assert asyncio.run(run_driven(main(), fake_clock)) == "done"
    assert seen[0] == seen[1]  # not one fake second passed while the thread worked
    assert fake_clock.monotonic() - start < 420.0
    assert awaited_calls() == 0


def test_the_clock_moves_when_nothing_runs_off_the_loop(fake_clock: FakeClock) -> None:
    async def main() -> None:
        await fake_clock.sleep(30.0)

    start = fake_clock.monotonic()
    asyncio.run(run_driven(main(), fake_clock))
    assert fake_clock.monotonic() - start >= 30.0


def test_the_clock_moves_between_pool_calls(fake_clock: FakeClock, pool: BlockingPool) -> None:
    stamps: list[float] = []

    async def main() -> None:
        for _ in range(3):
            await pool.run(lambda: stamps.append(fake_clock.monotonic()) or real_work(0.05))
            await fake_clock.sleep(10.0)

    asyncio.run(run_driven(main(), fake_clock))
    assert [b - a for a, b in zip(stamps, stamps[1:], strict=False)] == [10.0, 10.0]


def test_a_pool_call_parked_on_the_fake_clock_does_not_hold_it(
    fake_clock: FakeClock, pool: BlockingPool
) -> None:
    fake_clock.allow_threaded_advance()

    async def main() -> None:
        await pool.run(fake_clock.sleep_sync, 5.0)

    start = fake_clock.monotonic()
    asyncio.run(run_driven(main(), fake_clock))
    assert fake_clock.monotonic() - start >= 5.0


def test_an_abandoned_call_no_longer_holds_the_clock(
    fake_clock: FakeClock, pool: BlockingPool
) -> None:
    release = threading.Event()

    async def main() -> None:
        with pytest.raises(DeadlineMissed):
            await pool.run(release.wait, 30.0, deadline=0.05)  # loop time: a real deadline
        assert awaited_calls() == 0  # its thread runs on, nobody waits for it
        await fake_clock.sleep(60.0)

    start = fake_clock.monotonic()
    try:
        asyncio.run(run_driven(main(), fake_clock))
    finally:
        release.set()
    assert fake_clock.monotonic() - start >= 60.0


def test_a_stuck_pool_call_fails_after_the_hold_limit(
    fake_clock: FakeClock, pool: BlockingPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(turn, "HOLD_LIMIT_S", 0.2)
    release = threading.Event()

    async def main() -> None:
        await pool.run(release.wait, 30.0, deadline=420.0, clock=fake_clock)

    start = fake_clock.monotonic()
    try:
        with pytest.raises(AssertionError, match="kept the fake clock still"):
            asyncio.run(run_driven(main(), fake_clock))
    finally:
        release.set()
    assert fake_clock.monotonic() == start


def test_a_sync_tool_that_takes_real_time_beats_its_420_s_deadline(
    scope: Scope, fake_clock: FakeClock
) -> None:
    """The failure mode of 0e9a538 through a real turn: a sync tool on the ``tools`` pool spends
    real time while the executor's 420 s call deadline runs on the driven clock. Before the hold
    the deadline won every time (the tool was abandoned, "did not finish within 420 s")."""
    registry = ToolRegistry(clock=fake_clock)
    ran: list[float] = []

    def grind(args: object) -> str:
        ran.append(fake_clock.monotonic())
        real_work(PROBE_WORK_S)
        ran.append(fake_clock.monotonic())
        return "ground"

    registry.register(
        "grind", "probe", {"description": "grind", "parameters": {"type": "object"}}, grind
    )
    server = FakeModelServer(
        script_of(tool_batch(("grind", {})), text_turn("finished")), clock=fake_clock
    )

    async def main() -> tuple[ExitReason, list[str]]:
        agent = await Agent.start(
            scope,
            fake_route(server),
            ["probe"],
            fake_options(server, registry=registry),
            None,
        )
        result = await run_driven(agent.run_turn("grind it"), fake_clock)
        await agent.close()
        return result.exit_reason, [str(r.content) for r in result.rows if r.role == "tool"]

    reason, tool_rows = asyncio.run(main())
    assert reason is ExitReason.COMPLETED
    assert tool_rows == ["ground"], tool_rows
    assert ran[0] == ran[1]
