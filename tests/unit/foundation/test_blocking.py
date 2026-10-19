"""The D-04.1 blocking helper: pools, deadlines, abandonment, context, refusal, shutdown."""

from __future__ import annotations

import asyncio
import contextvars
import gc
import logging
import threading
import time
import traceback
from collections.abc import Callable
from typing import Any

import pytest

from indus.foundation import blocking
from indus.foundation.blocking import (
    POOLS,
    BlockingPool,
    DeadlineMissed,
    PoolClosed,
    PoolFull,
    run_blocking,
)
from indus.foundation.errors import ERROR_CODES, IndusError
from indus.foundation.scope import Scope, current_scope, use_scope
from tests.fakes.clock import FakeClock, checkpoint

STUCK_SECONDS = 30.0  # a "hung" call waits this long at most; every test releases it early

_marker: contextvars.ContextVar[str] = contextvars.ContextVar("test_blocking_marker", default="")


def until(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    """Poll from plain (non-loop) code without a banned sleep."""
    pause = threading.Event()
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            return False
        pause.wait(0.005)
    return True


async def until_async(predicate: Callable[[], bool], limit: int = 200_000) -> None:
    for _ in range(limit):
        if predicate():
            return
        await checkpoint()
    raise AssertionError("condition never became true")


class Hang:
    """A blocking call that waits until released; records that it started."""

    def __init__(self, result: object = "late", error: BaseException | None = None) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.result = result
        self.error = error

    def __call__(self) -> Any:
        self.started.set()
        self.release.wait(STUCK_SECONDS)
        if self.error is not None:
            raise type(self.error)(*self.error.args)  # a fresh one: the test keeps no reference
        return self.result


class Heartbeat:
    """A task that ticks once per loop iteration; it only advances while the loop is free."""

    def __init__(self) -> None:
        self.ticks = 0
        self._stop = False
        self._task: asyncio.Task[None] | None = None

    async def _beat(self) -> None:
        while not self._stop:
            self.ticks += 1
            await checkpoint()

    def start(self) -> None:
        self._task = asyncio.create_task(self._beat())

    async def stop(self) -> None:
        self._stop = True
        assert self._task is not None
        await self._task


@pytest.fixture
def pool() -> Any:
    made = BlockingPool("test", 2)
    yield made
    made.shutdown()


# -- basics ---------------------------------------------------------------------------------------


def test_runs_off_the_loop_and_passes_args(pool: BlockingPool) -> None:
    def work(a: int, b: str) -> tuple[str, bool]:
        return f"{a}{b}", threading.current_thread() is threading.main_thread()

    value, on_main = asyncio.run(pool.run(work, 4, "x"))
    assert (value, on_main) == ("4x", False)
    assert until(lambda: pool.busy == 0)


def test_scope_and_context_vars_travel_into_the_worker(pool: BlockingPool, scope: Scope) -> None:
    def work() -> tuple[str, str]:
        seen = (current_scope().profile_id, _marker.get())
        _marker.set("changed in worker")
        return seen

    async def go() -> tuple[tuple[str, str], str]:
        token = _marker.set("from caller")
        try:
            with use_scope(scope):
                seen = await pool.run(work)
            return seen, _marker.get()
        finally:
            _marker.reset(token)

    seen, after = asyncio.run(go())
    assert seen == ("work", "from caller")
    assert after == "from caller"  # the worker ran in a copy


def test_exception_propagates_with_its_traceback(pool: BlockingPool) -> None:
    def explode() -> None:
        raise ValueError("boom in worker")

    with pytest.raises(ValueError, match="boom in worker") as info:
        asyncio.run(pool.run(explode))
    frames = [frame.name for frame in traceback.extract_tb(info.value.__traceback__)]
    assert "explode" in frames
    assert until(lambda: pool.busy == 0)


# -- deadlines ------------------------------------------------------------------------------------


def test_deadline_on_fake_clock_abandons_while_loop_stays_responsive(
    pool: BlockingPool, fake_clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    hang = Hang()

    async def go() -> tuple[int, int]:
        beat = Heartbeat()
        beat.start()
        call = asyncio.create_task(pool.run(hang, deadline=2.0, clock=fake_clock, label="slow job"))
        await until_async(lambda: fake_clock.pending == 1 and hang.started.is_set())
        before = beat.ticks
        for _ in range(50):
            await checkpoint()
        during = beat.ticks - before  # the loop kept turning while the worker was stuck
        fake_clock.advance(1.9)
        await until_async(lambda: beat.ticks > before + 100)
        assert not call.done()
        fake_clock.advance(0.1)
        with pytest.raises(DeadlineMissed) as info:
            await call
        assert isinstance(info.value, TimeoutError)
        assert info.value.code == "blocking.deadline_missed"
        assert "slow job" in str(info.value)
        await beat.stop()
        return during, pool.busy

    try:
        with caplog.at_level(logging.WARNING, logger=blocking.__name__):
            during, busy = asyncio.run(go())
        assert during >= 40
        assert busy == 1  # abandoned, not killed: the thread still holds its worker
        assert not hang.release.is_set()
    finally:
        hang.release.set()
    assert until(lambda: pool.busy == 0)  # the worker comes back once the call returns
    warnings = [r for r in caplog.records if r.name == blocking.__name__]
    assert len(warnings) == 1
    assert "slow job" in warnings[0].getMessage() and "test pool" in warnings[0].getMessage()


def test_deadline_on_loop_time_and_late_result_after_loop_closed(
    pool: BlockingPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    thread_errors: list[threading.ExceptHookArgs] = []
    monkeypatch.setattr(threading, "excepthook", thread_errors.append)
    hang = Hang(error=RuntimeError("late failure"))

    async def go() -> int:
        beat = Heartbeat()
        beat.start()
        with pytest.raises(DeadlineMissed):
            await pool.run(hang, deadline=0.05)
        await beat.stop()
        return beat.ticks

    try:
        ticks = asyncio.run(go())  # the loop is closed when this returns
    finally:
        hang.release.set()
    assert ticks > 10
    assert until(lambda: pool.busy == 0)
    assert thread_errors == []  # the hand-back to a closed loop is dropped quietly


def test_result_before_deadline_is_returned(pool: BlockingPool, fake_clock: FakeClock) -> None:
    assert asyncio.run(pool.run(lambda: 7, deadline=1.0, clock=fake_clock)) == 7
    assert fake_clock.pending == 0  # the deadline timer was cancelled


def test_negative_deadline_refused_before_anything_runs(pool: BlockingPool) -> None:
    with pytest.raises(ValueError, match="deadline"):
        asyncio.run(pool.run(lambda: 1, deadline=-1.0))
    assert pool.busy == 0


# -- cancellation ---------------------------------------------------------------------------------


def test_cancelled_caller_abandons_and_late_error_is_drained(pool: BlockingPool) -> None:
    hang = Hang(error=ValueError("raised after the caller left"))
    reported: list[dict[str, Any]] = []

    async def go() -> None:
        asyncio.get_running_loop().set_exception_handler(lambda _loop, ctx: reported.append(ctx))
        call = asyncio.create_task(pool.run(hang))
        await until_async(hang.started.is_set)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        assert pool.busy == 1
        hang.release.set()
        await until_async(lambda: pool.busy == 0)
        for _ in range(5):
            await checkpoint()
        gc.collect()

    try:
        asyncio.run(go())
    finally:
        hang.release.set()
    assert reported == []


# -- pool bounds ----------------------------------------------------------------------------------


def test_worker_is_free_again_by_the_time_its_caller_resumes() -> None:
    """Found by D-04.1 criterion (d): a refusing pool must not count a worker as busy after its
    result reached the caller, or the caller's next call is refused with ``PoolFull``."""
    single = BlockingPool("one", 1)

    async def go() -> int:
        done = 0
        for n in range(2000):
            assert await single.run(int, str(n)) == n
            done += 1
        return done

    try:
        assert asyncio.run(go()) == 2000
    finally:
        single.shutdown()


def test_full_pool_refuses_and_never_grows(pool: BlockingPool) -> None:
    hangs = [Hang(), Hang()]

    async def go() -> list[str]:
        calls = [asyncio.create_task(pool.run(h)) for h in hangs]
        await until_async(lambda: all(h.started.is_set() for h in hangs))
        assert pool.busy == 2 and pool.threads == 2
        with pytest.raises(PoolFull) as info:
            await pool.run(lambda: "never runs")
        assert info.value.code == "blocking.pool_full"
        assert pool.threads == 2
        for h in hangs:
            h.release.set()
        return list(await asyncio.gather(*calls))

    try:
        assert asyncio.run(go()) == ["late", "late"]
    finally:
        for h in hangs:
            h.release.set()
    assert until(lambda: pool.busy == 0)
    assert asyncio.run(pool.run(lambda: "again")) == "again"
    assert pool.threads == 2  # idle workers were reused, none added


def test_waiting_pool_queues_in_order_then_refuses_past_its_limit() -> None:
    """D-04.1 criterion (c): many sessions reading at once wait for a worker instead of failing;
    the pool still never grows, and past the waiting limit it refuses."""
    pool = BlockingPool("waits", 2, queue_limit=2)
    hangs = [Hang(), Hang()]
    order: list[str] = []

    async def go() -> list[str]:
        stuck = [asyncio.create_task(pool.run(h)) for h in hangs]
        await until_async(lambda: all(h.started.is_set() for h in hangs))
        queued = [asyncio.create_task(pool.run(order.append, tag)) for tag in ("first", "second")]
        await until_async(lambda: pool.waiting == 2)
        assert pool.busy == 2 and pool.threads == 2
        with pytest.raises(PoolFull, match="2 calls are waiting"):
            await pool.run(lambda: "never runs")
        assert order == []
        hangs[0].release.set()
        await asyncio.gather(*queued)
        hangs[1].release.set()
        return list(await asyncio.gather(*stuck))

    try:
        assert asyncio.run(go()) == ["late", "late"]
    finally:
        for h in hangs:
            h.release.set()
        pool.shutdown()
    assert order == ["first", "second"]
    assert pool.threads <= 2


def test_waiting_call_past_its_deadline_or_cancelled_never_runs(fake_clock: FakeClock) -> None:
    pool = BlockingPool("waits", 1, queue_limit=4)
    hang = Hang()
    ran: list[str] = []

    async def go() -> None:
        stuck = asyncio.create_task(pool.run(hang))
        await until_async(hang.started.is_set)
        late = asyncio.create_task(
            pool.run(ran.append, "late", deadline=1.0, clock=fake_clock, label="queued read")
        )
        gone = asyncio.create_task(pool.run(ran.append, "cancelled"))
        await until_async(lambda: pool.waiting == 2 and fake_clock.pending == 1)
        fake_clock.advance(1.0)
        with pytest.raises(DeadlineMissed, match="did not start within 1 s") as missed:
            await late
        assert missed.value.started is False  # it never runs: nothing to count as stuck
        gone.cancel()
        with pytest.raises(asyncio.CancelledError):
            await gone
        hang.release.set()
        assert await stuck == "late"
        await until_async(lambda: pool.busy == 0 and pool.waiting == 0)
        assert await pool.run(ran.append, "after") is None

    try:
        asyncio.run(go())
    finally:
        hang.release.set()
        pool.shutdown()
    assert ran == ["after"]  # the two calls whose callers gave up never ran


def test_named_pools_queue_limits() -> None:
    limits = {name: p.queue_limit for name, p in POOLS.items()}
    assert limits == {"io": 256, "tools": 256, "hooks": 32}
    with pytest.raises(ValueError, match="queue_limit"):
        BlockingPool("bad", 1, queue_limit=-1)


def test_hung_hook_abandoned_without_blocking_a_second_call(fake_clock: FakeClock) -> None:
    """Criterion (d) shape: a sync hook that never returns is abandoned at its deadline; other
    hook calls go through meanwhile and afterwards; the stuck one keeps only its own worker."""
    hooks = BlockingPool("hooks-test", 2)
    hang = Hang()

    async def go() -> tuple[str, str]:
        stuck = asyncio.create_task(hooks.run(hang, deadline=1.0, clock=fake_clock, label="hook"))
        await until_async(lambda: hang.started.is_set() and fake_clock.pending == 1)
        meanwhile = await hooks.run(lambda: "second", deadline=1.0, clock=fake_clock)
        assert not stuck.done()
        fake_clock.advance(1.0)
        with pytest.raises(DeadlineMissed) as missed:
            await stuck
        assert missed.value.started is True  # it ran and is still running: abandoned
        after = await hooks.run(lambda: "third", deadline=1.0, clock=fake_clock)
        assert hooks.busy == 1
        return meanwhile, after

    try:
        assert asyncio.run(go()) == ("second", "third")
    finally:
        hang.release.set()
        hooks.shutdown()


# -- shutdown -------------------------------------------------------------------------------------


def test_shutdown_never_waits_on_abandoned_work() -> None:
    pool = BlockingPool("closing", 2)
    hang = Hang()

    async def go() -> None:
        assert await pool.run(lambda: "warm") == "warm"  # one idle worker
        with pytest.raises(DeadlineMissed):
            await pool.run(hang, deadline=0.01)

    try:
        asyncio.run(go())
        started = time.monotonic()
        pool.shutdown()
        assert time.monotonic() - started < 0.5
        assert pool.closed
        with pytest.raises(PoolClosed):
            asyncio.run(pool.run(lambda: "refused"))
        assert all(t.daemon for t in threading.enumerate() if t.name.startswith("indus-closing-"))
        assert not hang.release.is_set()
    finally:
        hang.release.set()
    assert until(lambda: pool.threads == 0)  # the stuck worker exits once its call returns


# -- named pools ----------------------------------------------------------------------------------


def test_named_pools_and_their_sizes() -> None:
    assert {name: p.workers for name, p in POOLS.items()} == {"io": 8, "tools": 8, "hooks": 8}
    name = asyncio.run(run_blocking(lambda: threading.current_thread().name, pool="hooks"))
    assert name.startswith("indus-hooks-")
    assert asyncio.run(run_blocking(str.upper, "abc")) == "ABC"  # default pool is io


def test_run_blocking_accepts_a_pool_and_refuses_unknown_names(pool: BlockingPool) -> None:
    assert asyncio.run(run_blocking(lambda: "mine", pool=pool)) == "mine"
    with pytest.raises(ValueError, match="no blocking pool named 'gpu'"):
        asyncio.run(run_blocking(lambda: None, pool="gpu"))


def test_error_codes_are_listed() -> None:
    for cls in (blocking.BlockingError, PoolFull, PoolClosed, DeadlineMissed):
        assert issubclass(cls, IndusError)
        assert cls.default_code in ERROR_CODES


def test_pool_needs_a_worker() -> None:
    with pytest.raises(ValueError):
        BlockingPool("empty", 0)
