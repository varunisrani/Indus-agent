"""D-04.1 criteria (a) and (c) again, now with the real P03 path: the fake model's
``MockTransport``, the SSE parser, the transports and the stream driver (P03-19)."""

import asyncio
from typing import Any

import pytest

from tests.trials.async_core.driver_parts import driver_source
from tests.trials.async_core.harness import (
    InterruptResult,
    Session,
    SourceProbe,
    SurfaceConsumer,
    _interrupt_result,  # pyright: ignore[reportPrivateUsage]
    budget_violations,
    percentile,
)

BUDGET_MS = 200.0
RUNS = 20
SESSIONS = 50
DELTAS = 200
P99_BUDGET_MS = 50.0
MODES = ["chat_completions", "anthropic_messages"]


async def interrupt_once(api_mode: str) -> InterruptResult:
    session = Session("real")
    probe = SourceProbe()
    consumer = SurfaceConsumer(session)
    consuming = asyncio.ensure_future(consumer.run())
    store: list[str] = []
    turn = session.start_turn(
        "hello", driver_source(probe, deltas=100, api_mode=api_mode, hang=True, store=store)
    )
    await consumer.seen(20)
    assert session.interrupt(session.generation)
    record = await turn
    await consuming
    return _interrupt_result(session, record, probe)


@pytest.mark.slow
@pytest.mark.parametrize("api_mode", MODES)
def test_interrupt_closes_the_http_stream_within_200ms(api_mode: str) -> None:
    results = [asyncio.run(interrupt_once(api_mode)) for _ in range(RUNS)]
    close = [r.close_ms for r in results]
    exit_ = [r.exit_ms for r in results]
    print(
        f"\n[D-04.1 a, {api_mode}] runs={RUNS} close p50={percentile(close, 50):.3f} ms "
        f"max={max(close):.3f} ms; exit p50={percentile(exit_, 50):.3f} ms max={max(exit_):.3f} ms"
    )
    for result in results:
        assert result.record.outcome == "interrupted"
        assert result.problems == ()
    assert budget_violations(results, BUDGET_MS) == []


async def fanout() -> tuple[list[float], float]:
    loop = asyncio.get_running_loop()
    started = loop.time()
    consumers: list[SurfaceConsumer] = []
    tasks: list[asyncio.Future[Any]] = []
    store: list[str] = []
    for i in range(SESSIONS):
        session = Session(f"s{i}")
        consumer = SurfaceConsumer(session)
        consumers.append(consumer)
        tasks.append(asyncio.ensure_future(consumer.run()))
        source = driver_source(
            SourceProbe(), deltas=DELTAS, api_mode=MODES[i % 2], hang=False, store=store
        )
        tasks.append(session.start_turn("go", source))
    await asyncio.gather(*tasks)
    assert len(store) == SESSIONS * DELTAS
    latencies = [ns / 1e6 for c in consumers for ns in c.latencies_ns]
    return latencies, (loop.time() - started) * 1000


@pytest.mark.slow
@pytest.mark.perf  # wall-clock p99 budget: runs in the serial perf lane (LEAD-NOTES)
def test_fifty_sessions_through_the_driver_p99() -> None:
    latencies, wall_ms = asyncio.run(fanout())
    print(
        f"\n[D-04.1 c, real path] sessions={SESSIONS} deltas/session={DELTAS} "
        f"events={len(latencies)} p50={percentile(latencies, 50):.3f} ms "
        f"p99={percentile(latencies, 99):.3f} ms max={max(latencies):.3f} ms wall={wall_ms:.1f} ms"
    )
    assert len(latencies) == SESSIONS * DELTAS
    assert percentile(latencies, 99) < P99_BUDGET_MS
