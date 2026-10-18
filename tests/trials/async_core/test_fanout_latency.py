"""D-04.1 criterion (c): 50 sessions in one loop keep p99 event latency under 50 ms."""

import asyncio

import pytest

from tests.trials.async_core.harness import run_fanout

SESSIONS = 50
DELTAS = 200
P99_BUDGET_MS = 50.0


@pytest.mark.slow
def test_fifty_sessions_p99_event_latency() -> None:
    result = asyncio.run(run_fanout(SESSIONS, DELTAS, every=2))
    print(
        f"\n[D-04.1 c] sessions={result.sessions} deltas/session={result.deltas_per_session} "
        f"events={len(result.latencies_ms)} p50={result.p50:.3f} ms p99={result.p99:.3f} ms "
        f"max={result.max:.3f} ms wall={result.wall_ms:.1f} ms"
    )
    assert len(result.latencies_ms) == SESSIONS * DELTAS
    assert result.p99 < P99_BUDGET_MS
