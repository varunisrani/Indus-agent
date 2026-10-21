import asyncio
import threading
from datetime import UTC, datetime, timedelta, timezone

import pytest

from indus.foundation.blocking import BlockingPool, awaiting_off_loop
from tests.fakes import clock as clock_module
from tests.fakes.clock import FakeClock, checkpoint, hold_for_off_loop, may_advance, quiesce

START = datetime(2026, 1, 1, tzinfo=UTC)


def test_rejects_naive_or_non_utc_start() -> None:
    with pytest.raises(ValueError):
        FakeClock(datetime(2026, 1, 1))
    with pytest.raises(ValueError):
        FakeClock(datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=5))))


def test_now_and_monotonic_move_with_advance() -> None:
    clock = FakeClock(START, start_mono=100.0)
    assert clock.now() == START
    assert clock.monotonic() == 100.0
    assert clock.advance(2.5) == 0
    assert clock.now() == START + timedelta(seconds=2.5)
    assert clock.monotonic() == 102.5
    assert clock.now().tzinfo is UTC


def test_sleepers_wake_in_deadline_order_ties_fifo() -> None:
    async def main() -> list[str]:
        clock = FakeClock(START)
        woke: list[str] = []

        async def sleeper(name: str, seconds: float) -> None:
            await clock.sleep(seconds)
            woke.append(name)

        tasks = [
            asyncio.create_task(sleeper("five", 5)),
            asyncio.create_task(sleeper("one-a", 1)),
            asyncio.create_task(sleeper("one-b", 1)),
        ]
        await clock.run_until_idle()
        assert clock.pending == 3
        assert clock.advance(5) == 3
        await asyncio.gather(*tasks)
        return woke

    assert asyncio.run(main()) == ["one-a", "one-b", "five"]


def test_short_advance_wakes_none() -> None:
    async def main() -> None:
        clock = FakeClock(START)
        task = asyncio.create_task(clock.sleep(1))
        await clock.run_until_idle()
        assert clock.advance(0.5) == 0
        await clock.run_until_idle()
        assert not task.done()
        assert await clock.advance_and_settle(0.5) == 1
        assert task.done()

    asyncio.run(main())


def test_cancelled_sleeper_is_removed() -> None:
    async def main() -> None:
        clock = FakeClock(START)
        doomed = asyncio.create_task(clock.sleep(1))
        kept = asyncio.create_task(clock.sleep(1))
        await clock.run_until_idle()
        doomed.cancel()
        await clock.run_until_idle()
        assert doomed.cancelled()
        assert clock.pending == 1
        assert clock.advance(1) == 1
        await kept

    asyncio.run(main())


def test_zero_sleep_does_not_park() -> None:
    async def main() -> None:
        clock = FakeClock(START)
        await clock.sleep(0)
        await checkpoint()
        assert clock.pending == 0

    asyncio.run(main())


def test_negative_values_rejected() -> None:
    clock = FakeClock(START)
    with pytest.raises(ValueError):
        clock.advance(-1)
    with pytest.raises(ValueError):
        asyncio.run(clock.sleep(-1))


def test_sync_sleep_refuses_without_threaded_advance() -> None:
    with pytest.raises(RuntimeError, match="another thread"):
        FakeClock(START).sleep_sync(1)


def test_sync_sleep_released_from_another_thread() -> None:
    clock = FakeClock(START)
    clock.allow_threaded_advance()
    done = threading.Event()

    def worker() -> None:
        clock.sleep_sync(3)
        done.set()

    thread = threading.Thread(target=worker)
    thread.start()
    assert clock.wait_for_sleepers(1)
    assert clock.advance(2) == 0
    assert clock.advance(1) == 1
    thread.join(5)
    assert done.is_set()


def test_advance_from_foreign_thread_wakes_async_sleeper() -> None:
    async def main() -> None:
        clock = FakeClock(START)
        task = asyncio.create_task(clock.sleep(1))
        await clock.run_until_idle()
        released = await asyncio.to_thread(clock.advance, 1)
        assert released == 1
        await asyncio.wait_for(task, 5)

    asyncio.run(main())


# -- holding the clock for off-loop work ---------------------------------------------------------


def test_may_advance_is_false_while_an_awaited_call_runs() -> None:
    clock = FakeClock(START)
    assert may_advance(clock)
    with awaiting_off_loop():
        assert not may_advance(clock)
    assert may_advance(clock)


def test_a_thread_parked_on_the_clock_does_not_hold_it() -> None:
    clock = FakeClock(START)
    clock.allow_threaded_advance()
    with awaiting_off_loop():
        thread = threading.Thread(target=clock.sleep_sync, args=(5,))
        thread.start()
        assert clock.wait_for_sleepers(1)
        assert may_advance(clock)  # the awaited work waits on the clock itself
        clock.advance(5)
        thread.join(5)


def test_hold_waits_for_the_call_and_its_caller_to_park_again() -> None:
    clock = FakeClock(START)
    pool = BlockingPool("clock-hold", 1)
    order: list[str] = []

    async def caller() -> None:
        await pool.run(threading.Event().wait, 0.2)  # real work on the worker
        order.append("back")
        await clock.sleep(60)  # the caller's next wait, on the clock

    async def main() -> None:
        task = asyncio.ensure_future(caller())
        await checkpoint()
        await quiesce(clock)
        order.append("held until here")
        assert clock.pending == 1  # parked on its clock sleep before anyone moves the clock
        clock.advance(60)
        await task

    try:
        asyncio.run(main())
    finally:
        pool.shutdown()
    assert order == ["back", "held until here"]


def test_a_stuck_call_fails_the_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock(START)
    monkeypatch.setattr(clock_module, "HOLD_POLL_S", 0.001)

    async def main() -> None:
        with awaiting_off_loop():
            await hold_for_off_loop(clock, limit=0.05)

    with pytest.raises(AssertionError, match="kept the fake clock still"):
        asyncio.run(main())
