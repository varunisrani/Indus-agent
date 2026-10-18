"""D-04.1 criterion (b): a child process with a 2 s timeout is gone by timeout + 2 s while ten
other sessions keep streaming through the P03 driver with p99 event latency under 50 ms."""

import asyncio

import pytest

from tests.trials.async_core.driver_parts import (
    ChildResult,
    Streamers,
    child_violations,
    run_with_deadline,
)

TIMEOUT = 2.0
BACKSTOP = 2.0
P99_BUDGET_MS = 50.0


@pytest.mark.slow
def test_child_killed_by_backstop_while_sessions_stream() -> None:
    async def go() -> tuple[ChildResult, ChildResult, Streamers]:
        stop = asyncio.Event()
        streamers = Streamers(count=10)
        background = asyncio.ensure_future(streamers.run(stop))
        try:
            worst = await run_with_deadline(timeout=TIMEOUT, ignore_term=True, backstop=BACKSTOP)
            plain = await run_with_deadline(timeout=TIMEOUT, ignore_term=False, backstop=BACKSTOP)
        finally:
            stop.set()
            await background
        return worst, plain, streamers

    worst, plain, streamers = asyncio.run(go())
    assert worst.ended_after is not None and plain.ended_after is not None
    print(
        f"\n[D-04.1 b] SIGTERM-ignoring child ended after {worst.ended_after:.3f}s ({worst.how}); "
        f"plain child ended after {plain.ended_after:.3f}s ({plain.how}); {streamers.summary()}"
    )
    assert worst.how == "killed" and child_violations(worst, backstop=BACKSTOP) == []
    assert worst.ended_after >= TIMEOUT + BACKSTOP - 0.05  # the backstop, not luck, ended it
    assert plain.how == "terminated" and TIMEOUT - 0.05 <= plain.ended_after < TIMEOUT + 0.5
    assert streamers.turns > 10
    assert streamers.p99 < P99_BUDGET_MS


@pytest.mark.slow
def test_negative_control_without_backstop_is_flagged() -> None:
    result = asyncio.run(
        run_with_deadline(timeout=1.0, ignore_term=True, backstop=None, observe=2.5)
    )
    print(f"\n[D-04.1 b control] no backstop: child {result.how} after {1.0 + 2.5:.1f}s")
    assert result.how == "alive"
    assert child_violations(result, backstop=BACKSTOP) != []
