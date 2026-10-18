"""D-04.1 criterion (d): a synchronous hook that never returns is abandoned at its 1 s deadline
through a bounded thread pool; other sessions keep streaming and the pool refuses extra work
instead of growing."""

import asyncio
import threading
import time

import pytest

from tests.trials.async_core.driver_parts import (
    BoundedHookPool,
    HookOutcome,
    PoolFull,
    Streamers,
)

DEADLINE = 1.0
P99_BUDGET_MS = 50.0


def hanging_hook(release: threading.Event) -> str:
    release.wait(60)  # "never" returns within the test; released at the end so threads exit
    return "late"


@pytest.mark.slow
def test_hanging_hook_abandoned_and_pool_bounded() -> None:
    release = threading.Event()
    pool = BoundedHookPool(workers=2)

    async def go() -> tuple[HookOutcome, float, HookOutcome, bool, int, Streamers]:
        stop = asyncio.Event()
        streamers = Streamers(count=10)
        background = asyncio.ensure_future(streamers.run(stop))
        try:
            start = time.perf_counter()
            first = await pool.run(lambda: hanging_hook(release), deadline=DEADLINE)
            continued_after = time.perf_counter() - start  # the turn goes on from here
            second = await pool.run(lambda: hanging_hook(release), deadline=DEADLINE)
            try:
                await pool.run(lambda: "quick", deadline=DEADLINE)
                refused = False
            except PoolFull:
                refused = True
            threads = pool.threads
        finally:
            stop.set()
            await background
        return first, continued_after, second, refused, threads, streamers

    try:
        first, continued_after, second, refused, threads, streamers = asyncio.run(go())
    finally:
        release.set()
        pool.shutdown()
    print(
        f"\n[D-04.1 d] hook abandoned after {first.waited:.3f}s, turn continued after "
        f"{continued_after:.3f}s; second abandoned after {second.waited:.3f}s; third refused="
        f"{refused}; pool threads={threads}; {streamers.summary()}"
    )
    assert first.status == "abandoned" and second.status == "abandoned"
    assert DEADLINE <= continued_after < DEADLINE + 0.2
    assert refused and threads == 2
    assert streamers.p99 < P99_BUDGET_MS


@pytest.mark.slow
def test_quick_hook_returns_its_value() -> None:
    pool = BoundedHookPool(workers=1)
    try:
        outcome = asyncio.run(pool.run(lambda: 42, deadline=DEADLINE))
    finally:
        pool.shutdown()
    assert outcome.status == "done" and outcome.value == 42


@pytest.mark.slow
def test_negative_control_without_deadline_is_flagged() -> None:
    release = threading.Event()
    pool = BoundedHookPool(workers=1)

    async def go() -> bool:
        turn = asyncio.ensure_future(pool.run(lambda: hanging_hook(release), deadline=None))
        done, _ = await asyncio.wait({turn}, timeout=DEADLINE + 0.5)
        stuck = turn not in done
        release.set()
        await turn
        return stuck

    try:
        stuck = asyncio.run(go())
    finally:
        release.set()
        pool.shutdown()
    print(
        f"\n[D-04.1 d control] no deadline: turn still waiting after {DEADLINE + 0.5:.1f}s={stuck}"
    )
    assert stuck
