"""D-04.1 criterion (a): an interrupt mid-stream closes the stream within 200 ms."""

import asyncio
from datetime import UTC, datetime

import pytest

from tests.fakes.clock import FakeClock
from tests.trials.async_core.harness import (
    SYNTHETIC_INTERRUPTED,
    InterruptResult,
    budget_violations,
    percentile,
    run_interrupt_once,
    run_stubborn_control,
    text_pieces,
    tool_call_pieces,
)

BUDGET_MS = 200.0
RUNS = 20


def _report(label: str, results: list[InterruptResult]) -> None:
    close = [r.close_ms for r in results]
    exit_ = [r.exit_ms for r in results]
    print(
        f"\n[D-04.1 a] {label}: runs={len(results)} "
        f"close p50={percentile(close, 50):.3f} ms max={max(close):.3f} ms; "
        f"exit p50={percentile(exit_, 50):.3f} ms max={max(exit_):.3f} ms"
    )


def _check_sealed(result: InterruptResult) -> None:
    assert result.record.outcome == "interrupted"
    assert result.problems == ()
    assistant = [m for m in result.history if m.role == "assistant"]
    assert len(assistant) == 1 and assistant[0].partial


@pytest.mark.slow
@pytest.mark.parametrize(
    ("label", "await_between_deltas", "every", "reason"),
    [
        ("cancel lands in the source", False, 2, "cancelled"),
        ("cancel lands in the turn, source closed by aclose", True, 0, "aclose"),
    ],
)
def test_interrupt_mid_text_stream(
    label: str, await_between_deltas: bool, every: int, reason: str
) -> None:
    async def many() -> list[InterruptResult]:
        return [
            await run_interrupt_once(
                text_pieces(10_000),
                interrupt_after=20,
                every=every,
                await_between_deltas=await_between_deltas,
            )
            for _ in range(RUNS)
        ]

    results = asyncio.run(many())
    _report(label, results)
    assert budget_violations(results, BUDGET_MS) == []
    for result in results:
        _check_sealed(result)
        assert result.probe.close_reason == reason
        assert result.probe.produced < 10_000  # really mid-stream
        assert result.history[-1].content.startswith("w0 w1 ")


@pytest.mark.slow
def test_interrupt_with_pending_delta_after_tool_calls() -> None:
    async def many() -> list[InterruptResult]:
        pieces = tool_call_pieces()
        return [
            await run_interrupt_once(pieces, interrupt_after=len(pieces), hang_at_end=True)
            for _ in range(RUNS)
        ]

    results = asyncio.run(many())
    _report("pending delta after a finished and a cut tool call", results)
    assert budget_violations(results, BUDGET_MS) == []
    for result in results:
        _check_sealed(result)
        user, assistant, tool = result.history
        assert user.role == "user"
        assert [c.call_id for c in assistant.tool_calls] == ["call-1"]
        assert assistant.tool_calls[0].arguments == '{"path": "notes.txt"}'
        assert tool.tool_call_id == "call-1" and tool.synthetic
        assert tool.content == SYNTHETIC_INTERRUPTED
        assert result.record.dropped_calls == ("call-2",)


def test_negative_control_source_holding_cancellation_is_flagged() -> None:
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    result = asyncio.run(run_stubborn_control(clock, hold_seconds=1.0))
    print(f"\n[D-04.1 a] negative control: close={result.close_ms:.1f} ms (virtual time)")
    assert result.close_ms == pytest.approx(1000.0)
    violations = budget_violations([result], BUDGET_MS)
    assert any("source closed after 1000.000 ms" in v for v in violations)
    assert any("exit funnel" in v for v in violations)
    # Even a slow source ends in a sealed, valid history.
    _check_sealed(result)
