"""Rules of the trial harness itself (fast, no timing assertions)."""

import asyncio
from pathlib import Path

from tests.trials.async_core.harness import (
    Message,
    Session,
    SourceProbe,
    SurfaceConsumer,
    TextPiece,
    ToolCallRecord,
    check_alternation,
    percentile,
    scripted_stream,
    text_pieces,
)

REPO = Path(__file__).resolve().parents[3]


def test_stale_interrupt_does_not_cancel_the_next_turn() -> None:
    async def main() -> None:
        session = Session("s")
        consumer = SurfaceConsumer(session)
        consuming = asyncio.create_task(consumer.run(turns=2))
        first = session.start_turn("one", scripted_stream(SourceProbe(), text_pieces(3)))
        assert (await first).outcome == "completed"
        second = session.start_turn("two", scripted_stream(SourceProbe(), text_pieces(50)))
        assert session.interrupt(1) is False  # aimed at the finished first turn
        record = await second
        assert record.outcome == "completed"
        await consuming
        assert check_alternation(session.history) == []

    asyncio.run(main())


def test_foreign_cancel_still_seals_history_then_propagates() -> None:
    async def main() -> Session:
        session = Session("s")
        consumer = SurfaceConsumer(session)
        asyncio.create_task(consumer.run())
        probe = SourceProbe()
        turn = session.start_turn("go", scripted_stream(probe, [TextPiece("a")], hang_at_end=True))
        await consumer.seen(1)
        turn.cancel()  # not an interrupt: e.g. shutdown
        try:
            await turn
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("foreign cancellation was swallowed")
        assert probe.closed_ns is not None
        return session

    session = asyncio.run(main())
    assert session.turns[0].outcome == "interrupted"
    assert check_alternation(session.history) == []


def test_alternation_checker_finds_problems() -> None:
    call = ToolCallRecord("c1", "f", "{}")
    assert check_alternation([Message("assistant", "x")]) != []
    assert check_alternation([Message("user", "a"), Message("user", "b")]) != []
    dangling = [Message("user", "a"), Message("assistant", "", tool_calls=(call,))]
    assert any("unanswered" in p for p in check_alternation(dangling))
    orphan = [
        Message("user", "a"),
        Message("assistant", "x"),
        Message("tool", "r", tool_call_id="c9"),
    ]
    assert any("no open call" in p for p in check_alternation(orphan))
    good = [*dangling, Message("tool", "r", tool_call_id="c1"), Message("user", "b")]
    assert check_alternation(good) == []


def test_percentile_nearest_rank() -> None:
    values = list(range(1, 101))
    assert percentile(values, 50) == 50
    assert percentile(values, 99) == 99
    assert percentile([7], 99) == 7


def test_product_code_does_not_import_the_trial() -> None:
    offenders = [
        str(p.relative_to(REPO))
        for p in (REPO / "src").rglob("*.py")
        if "trials" in p.read_text(encoding="utf-8")
    ]
    assert offenders == []
