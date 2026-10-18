"""D-04.1 criterion (e): synchronous code (a REPL key handler on the main thread) starts and
interrupts a streaming turn on a loop running in a background thread; the HTTP stream is closed
within 200 ms of the interrupt."""

import threading
import time

import pytest

from tests.trials.async_core.driver_parts import (
    LoopThread,
    interrupt_turn,
    start_driven_turn,
    stream_closed_after,
)
from tests.trials.async_core.harness import percentile

BUDGET = 0.2
RUNS = 20


@pytest.mark.slow
def test_sync_caller_interrupts_within_200ms() -> None:
    loop = LoopThread()
    latencies: list[float] = []
    try:
        for _ in range(RUNS):
            turn = loop.submit(start_driven_turn()).result(timeout=5)
            assert turn.first_deltas.wait(5), "the turn never streamed"
            start = time.perf_counter()
            loop.submit(interrupt_turn(turn)).result(timeout=5)  # the key handler's call
            closed = stream_closed_after(turn, start)
            assert closed is not None, "the stream was not closed"
            latencies.append(closed)
    finally:
        loop.close()
    ms = [x * 1000 for x in latencies]
    print(
        f"\n[D-04.1 e] runs={RUNS} interrupt -> stream closed p50={percentile(ms, 50):.3f} ms "
        f"max={max(ms):.3f} ms"
    )
    assert max(latencies) < BUDGET


@pytest.mark.slow
def test_negative_control_unhonoured_interrupt_is_flagged() -> None:
    loop = LoopThread()
    try:
        turn = loop.submit(start_driven_turn(honour_interrupt=False)).result(timeout=5)
        assert turn.first_deltas.wait(5)
        start = time.perf_counter()
        loop.submit(interrupt_turn(turn)).result(timeout=5)
        threading.Event().wait(BUDGET + 0.1)  # observation window, measured only
        closed = stream_closed_after(turn, start)
    finally:
        loop.close()
    print(
        f"\n[D-04.1 e control] interrupt ignored: stream closed within budget={closed is not None}"
    )
    assert closed is None or closed > BUDGET
