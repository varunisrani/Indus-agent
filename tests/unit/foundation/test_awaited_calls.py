"""``awaited_calls``: off-loop calls whose caller still waits (the fake-clock driver's hold)."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator

import pytest

from indus.foundation.blocking import (
    BlockingPool,
    DeadlineMissed,
    PoolFull,
    awaited_calls,
    awaiting_off_loop,
)
from tests.fakes.agent import eventually


@pytest.fixture
def pool() -> Iterator[BlockingPool]:
    made = BlockingPool("awaited", 1, queue_limit=4)
    yield made
    made.shutdown()


def test_a_pool_call_counts_while_its_caller_waits(pool: BlockingPool) -> None:
    inside: list[int] = []

    async def main() -> None:
        base = awaited_calls()
        await pool.run(lambda: inside.append(awaited_calls() - base))
        assert awaited_calls() == base

    asyncio.run(main())
    assert inside == [1]


def test_a_raising_call_leaves_the_count(pool: BlockingPool) -> None:
    def boom() -> None:
        raise ValueError("no")

    async def main() -> None:
        base = awaited_calls()
        with pytest.raises(ValueError):
            await pool.run(boom)
        assert awaited_calls() == base

    asyncio.run(main())


def test_abandoned_work_is_not_counted(pool: BlockingPool) -> None:
    release = threading.Event()

    async def main() -> None:
        base = awaited_calls()
        with pytest.raises(DeadlineMissed):
            await pool.run(release.wait, 30.0, deadline=0.01)
        assert awaited_calls() == base  # the thread still runs; nobody waits for it

        waiter = asyncio.ensure_future(pool.run(release.wait, 30.0))  # queued behind the first
        await eventually(lambda: awaited_calls() == base + 1)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert awaited_calls() == base

    try:
        asyncio.run(main())
    finally:
        release.set()


def test_refused_work_is_not_counted() -> None:
    full = BlockingPool("awaited-full", 1)
    release = threading.Event()

    async def main() -> None:
        base = awaited_calls()
        first = asyncio.ensure_future(full.run(release.wait, 30.0))
        await eventually(lambda: full.busy == 1)
        with pytest.raises(PoolFull):
            await full.run(release.wait, 30.0)
        assert awaited_calls() == base + 1
        release.set()
        await first
        assert awaited_calls() == base

    try:
        asyncio.run(main())
    finally:
        release.set()
        full.shutdown()


def test_an_off_loop_wait_counts_until_released_once() -> None:
    base = awaited_calls()
    waiting = awaiting_off_loop()
    with waiting:
        assert awaited_calls() == base + 1
        worker = threading.Thread(target=waiting.release)
        worker.start()
        worker.join()
        assert awaited_calls() == base
    assert awaited_calls() == base  # the exit after an early release takes nothing off
    with awaiting_off_loop():
        assert awaited_calls() == base + 1
    assert awaited_calls() == base
