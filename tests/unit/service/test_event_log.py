"""P17-06: per-session seq, replay epoch, bounded replay buffer, delta coalescing."""

from __future__ import annotations

import asyncio
import random
from datetime import UTC, datetime
from typing import Any

import pytest

from indus.protocol.events import MessageDelta, TurnStarted
from indus.service.event_log import (
    COALESCE_WINDOW_S,
    EventLog,
    LoggedEvent,
    Replay,
    ReplayGap,
    ReplayLimits,
)
from tests.fakes.clock import FakeClock


def make_log(**kw: Any) -> tuple[EventLog, FakeClock]:
    clock = FakeClock(datetime(2026, 10, 19, 9, 0, tzinfo=UTC))
    return EventLog(clock=clock, epoch="ep-1", **kw), clock


def notice(text: str, session: str = "s1") -> dict[str, Any]:
    return {"type": "notice", "session_id": session, "seq": 999, "level": "info", "text": text}


def delta(text: str, turn: str = "t1", kind: str = "message.delta") -> dict[str, Any]:
    return {"type": kind, "session_id": "s1", "seq": 0, "turn_id": turn, "text": text}


def replay(result: Replay | ReplayGap) -> Replay:
    assert isinstance(result, Replay), result
    return result


# -- numbering and replay ------------------------------------------------------------------------


def test_seq_starts_at_one_per_session_and_ignores_incoming_seq() -> None:
    elog, _ = make_log()
    (a,) = elog.append("s1", notice("a"))
    (b,) = elog.append("s1", notice("b"))
    (c,) = elog.append("s2", notice("c", "s2"))
    assert (a.seq, b.seq, c.seq) == (1, 2, 1)
    wire = b.to_wire()
    assert wire["seq"] == 2 and wire["replay_epoch"] == "ep-1" and wire["text"] == "b"
    assert elog.last_seq("s1") == 2 and elog.last_seq("nobody") == 0


def test_protocol_models_are_accepted() -> None:
    elog, _ = make_log()
    elog.append("s1", TurnStarted(session_id="s1", seq=40, turn_id="t1", generation=3))
    (entry,) = elog.append("s1", notice("x"))
    events = replay(elog.since("s1", "ep-1", 0)).events
    assert [e["type"] for e in events] == ["turn.started", "notice"]
    assert events[0]["generation"] == 3 and events[0]["seq"] == 1
    assert entry.type == "notice"


def test_since_returns_missing_events_in_order() -> None:
    elog, _ = make_log()
    for i in range(5):
        elog.append("s1", notice(str(i)))
    result = replay(elog.since("s1", "ep-1", 2))
    assert [e["seq"] for e in result.events] == [3, 4, 5]
    assert result.last_seq == 5 and result.replay_epoch == "ep-1"
    assert replay(elog.since("s1", "ep-1", 5)).events == ()
    assert replay(elog.since("fresh", "ep-1", 0)).events == ()


def test_epoch_change_is_a_gap() -> None:
    elog, _ = make_log()
    elog.append("s1", notice("a"))
    gap = elog.since("s1", "ep-old", 1)
    assert isinstance(gap, ReplayGap) and gap.reason == "epoch_changed"
    assert gap.to_wire() == {
        "type": "session.replay_gap",
        "session_id": "s1",
        "replay_epoch": "ep-1",
        "last_seq": 1,
        "reason": "epoch_changed",
    }
    missing = elog.since("s1", None, 0)
    assert isinstance(missing, ReplayGap) and missing.reason == "epoch_changed"


def test_cursor_ahead_is_a_gap_and_negative_cursor_is_an_error() -> None:
    elog, _ = make_log()
    elog.append("s1", notice("a"))
    gap = elog.since("s1", "ep-1", 7)
    assert isinstance(gap, ReplayGap) and gap.reason == "ahead"
    with pytest.raises(ValueError):
        elog.since("s1", "ep-1", -1)


def test_fresh_epochs_differ_between_logs() -> None:
    clock = FakeClock(datetime(2026, 10, 19, tzinfo=UTC))
    assert EventLog(clock=clock).epoch != EventLog(clock=clock).epoch


# -- caps and eviction ---------------------------------------------------------------------------


def test_event_cap_evicts_oldest_and_old_cursor_gets_a_gap() -> None:
    elog, _ = make_log(limits=ReplayLimits(events_per_session=5))
    for i in range(8):
        elog.append("s1", notice(str(i)))
    gap = elog.since("s1", "ep-1", 2)
    assert isinstance(gap, ReplayGap) and gap.reason == "evicted" and gap.last_seq == 8
    assert [e["seq"] for e in replay(elog.since("s1", "ep-1", 3)).events] == [4, 5, 6, 7, 8]
    assert elog.stats()["events"] == 5


def test_byte_cap_evicts_oldest() -> None:
    elog, _ = make_log()
    size = len(elog.append("probe", notice("x" * 100))[0].payload)
    elog, _ = make_log(limits=ReplayLimits(bytes_per_session=size * 3))
    for _ in range(6):
        elog.append("s1", notice("x" * 100))
    assert elog.stats()["bytes"] <= size * 3
    assert [e["seq"] for e in replay(elog.since("s1", "ep-1", 3)).events] == [4, 5, 6]
    assert isinstance(elog.since("s1", "ep-1", 2), ReplayGap)


def test_process_byte_cap_takes_from_the_quietest_session() -> None:
    elog, _ = make_log()
    size = len(elog.append("probe", notice("y" * 50, "probe"))[0].payload)
    elog, _ = make_log(limits=ReplayLimits(bytes_process=size * 4))
    for _ in range(2):
        elog.append("quiet", notice("y" * 50, "quiet"))
    for _ in range(2):
        elog.append("busy", notice("y" * 50, "busy"))
    elog.append("busy", notice("y" * 50, "busy"))  # over the process cap by one event
    assert isinstance(elog.since("quiet", "ep-1", 0), ReplayGap)
    assert [e["seq"] for e in replay(elog.since("quiet", "ep-1", 1)).events] == [2]
    assert len(replay(elog.since("busy", "ep-1", 0)).events) == 3
    assert elog.stats()["bytes"] <= size * 4


def test_session_cap_drops_quietest_buffer_but_keeps_its_counter() -> None:
    elog, _ = make_log(limits=ReplayLimits(sessions=2))
    elog.append("a", notice("1", "a"))
    elog.append("b", notice("1", "b"))
    elog.append("a", notice("2", "a"))  # b is now the quietest
    elog.append("c", notice("1", "c"))
    gap = elog.since("b", "ep-1", 0)
    assert isinstance(gap, ReplayGap) and gap.reason == "evicted"
    assert elog.stats()["sessions"] == 2
    (back,) = elog.append("b", notice("2", "b"))
    assert back.seq == 2  # never restarts at 1 under a cursor a client still holds
    assert [e["seq"] for e in replay(elog.since("b", "ep-1", 1)).events] == [2]


def test_oversized_event_is_not_kept() -> None:
    elog, _ = make_log(limits=ReplayLimits(bytes_per_session=200))
    elog.append("s1", notice("small"))
    (big,) = elog.append("s1", notice("z" * 500))
    elog.append("s1", notice("after"))
    assert big.seq == 2
    assert isinstance(elog.since("s1", "ep-1", 1), ReplayGap)
    assert [e["text"] for e in replay(elog.since("s1", "ep-1", 2)).events] == ["after"]


def test_forget_drops_buffer_keeps_counter() -> None:
    elog, _ = make_log()
    elog.append("s1", notice("a"))
    elog.append("s1", delta("held"))
    elog.forget("s1")
    assert elog.last_seq("s1") == 2  # the held piece was numbered first
    assert isinstance(elog.since("s1", "ep-1", 0), ReplayGap)
    assert replay(elog.since("s1", "ep-1", 2)).events == ()
    assert elog.append("s1", notice("b"))[0].seq == 3


def test_buffer_is_frozen_against_later_mutation() -> None:
    elog, _ = make_log()
    event = notice("before")
    elog.append("s1", event)
    event["text"] = "after" * 1000
    assert replay(elog.since("s1", "ep-1", 0)).events[0]["text"] == "before"


def test_event_without_type_is_refused() -> None:
    elog, _ = make_log()
    with pytest.raises(ValueError):
        elog.append("s1", {"text": "x"})
    with pytest.raises(ValueError):
        elog.append("", notice("x"))


# -- coalescing ----------------------------------------------------------------------------------


def test_deltas_within_the_window_are_joined() -> None:
    elog, clock = make_log()
    assert elog.append("s1", delta("Hel")) == []
    clock.advance(0.010)
    assert elog.append("s1", delta("lo")) == []
    clock.advance(0.010)
    elog.append("s1", delta(", world"))
    (joined,) = elog.flush()
    assert joined.seq == 1 and joined.to_wire()["text"] == "Hello, world"
    assert joined.to_wire()["turn_id"] == "t1"


def test_window_over_starts_a_new_event() -> None:
    elog, clock = make_log()
    elog.append("s1", delta("a"))
    clock.advance(COALESCE_WINDOW_S)
    (first,) = elog.append("s1", delta("b"))
    assert first.to_wire()["text"] == "a"
    (second,) = elog.flush("s1")
    assert second.to_wire()["text"] == "b" and second.seq == 2


def test_other_event_flushes_held_text_first() -> None:
    elog, _ = make_log()
    elog.append("s1", delta("partial"))
    done = elog.append("s1", notice("tool"))
    assert [e.to_wire()["type"] for e in done] == ["message.delta", "notice"]
    assert [e.seq for e in done] == [1, 2]


def test_different_turn_or_kind_is_not_joined() -> None:
    elog, _ = make_log()
    elog.append("s1", delta("a", turn="t1"))
    (first,) = elog.append("s1", delta("b", turn="t2"))
    (second,) = elog.append("s1", delta("c", turn="t2", kind="reasoning.delta"))
    (third,) = elog.flush()
    texts = [e.to_wire()["text"] for e in (first, second, third)]
    assert texts == ["a", "b", "c"]
    assert third.to_wire()["type"] == "reasoning.delta"


def test_sessions_hold_text_separately() -> None:
    elog, _ = make_log()
    elog.append("s1", delta("one"))
    elog.append("s2", {**delta("two"), "session_id": "s2"})
    assert elog.stats()["held"] == 2
    assert [e.session_id for e in elog.flush()] == ["s1", "s2"]


def test_flush_due_respects_the_window() -> None:
    elog, clock = make_log()
    assert elog.next_flush_at() is None
    start = clock.monotonic()
    elog.append("s1", MessageDelta(session_id="s1", seq=0, turn_id="t1", text="x"))
    assert elog.next_flush_at() == pytest.approx(start + COALESCE_WINDOW_S)
    clock.advance(0.020)
    assert elog.flush_due() == []
    clock.advance(0.013)
    (entry,) = elog.flush_due()
    assert entry.to_wire()["text"] == "x"
    assert elog.next_flush_at() is None


def test_run_flusher_flushes_after_the_window() -> None:
    async def body() -> None:
        elog, clock = make_log()
        seen: list[LoggedEvent] = []
        elog.add_listener(seen.append)
        task = asyncio.ensure_future(elog.run_flusher())
        await clock.run_until_idle()
        elog.append("s1", delta("tok"))
        await clock.run_until_idle()
        assert seen == []
        await clock.advance_and_settle(COALESCE_WINDOW_S)
        assert [e.to_wire()["text"] for e in seen] == ["tok"]
        elog.append("s1", delta("more"))
        await clock.run_until_idle()
        await clock.advance_and_settle(COALESCE_WINDOW_S)
        assert [e.to_wire()["text"] for e in seen] == ["tok", "more"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(body())


# -- listeners -----------------------------------------------------------------------------------


def test_listeners_hear_events_in_order_and_failures_are_skipped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    elog, _ = make_log()
    heard: list[int] = []

    def broken(_: LoggedEvent) -> None:
        raise RuntimeError("screen gone")

    elog.add_listener(broken)
    remove = elog.add_listener(lambda e: heard.append(e.seq))
    elog.append("s1", delta("a"))
    elog.append("s1", notice("b"))
    elog.append("s1", notice("c"))
    assert heard == [1, 2, 3]
    assert "event-log listener failed" in caplog.text
    remove()
    elog.append("s1", notice("d"))
    assert heard == [1, 2, 3]


# -- properties (seeded random tables; hypothesis is not installed) ------------------------------


def _random_run(seed: int, limits: ReplayLimits) -> None:
    rng = random.Random(seed)
    elog, clock = make_log(limits=limits)
    sessions = ["s1", "s2", "s3"]
    live: dict[str, list[int]] = {s: [] for s in sessions}
    elog.add_listener(lambda e: live[e.session_id].append(e.seq))
    pieces: dict[tuple[str, str, str], list[str]] = {}
    cursors = dict.fromkeys(sessions, 0)
    received: dict[str, list[int]] = {s: [] for s in sessions}
    texts: dict[tuple[str, str, str], list[str]] = {}
    gaps = 0

    def read(session: str) -> None:
        nonlocal gaps
        result = elog.since(session, "ep-1", cursors[session])
        if isinstance(result, ReplayGap):
            gaps += 1
            assert result.reason == "evicted"
            cursors[session] = result.last_seq  # the client reloads a snapshot
            return
        seqs = [e["seq"] for e in result.events]
        assert seqs == list(range(cursors[session] + 1, result.last_seq + 1))
        for event in result.events:
            if event["type"].endswith(".delta"):
                key = (session, event["turn_id"], event["type"])
                texts.setdefault(key, []).append(event["text"])
        received[session].extend(seqs)
        cursors[session] = result.last_seq

    for _ in range(400):
        session = rng.choice(sessions)
        roll = rng.random()
        if roll < 0.55:
            turn = rng.choice(["t1", "t2"])
            kind = rng.choice(["message.delta", "message.delta", "reasoning.delta"])
            text = "".join(rng.choice("abcxyz ") for _ in range(rng.randint(1, 8)))
            pieces.setdefault((session, turn, kind), []).append(text)
            elog.append(session, {**delta(text, turn, kind), "session_id": session})
        elif roll < 0.70:
            elog.append(session, notice("n", session))
        elif roll < 0.85:
            clock.advance(rng.choice([0.0, 0.005, 0.02, 0.05]))
            elog.flush_due()
        else:
            read(session)
    elog.flush()
    for session in sessions:
        read(session)
        assert live[session] == list(range(1, elog.last_seq(session) + 1))
        assert received[session] == sorted(set(received[session]))
    if gaps == 0:
        for key, parts in pieces.items():
            assert "".join(texts.get(key, [])) == "".join(parts), key


@pytest.mark.parametrize("seed", range(25))
def test_random_interleavings_replay_without_gaps_or_duplicates(seed: int) -> None:
    _random_run(seed, ReplayLimits())


@pytest.mark.parametrize("seed", range(15))
def test_random_interleavings_under_tight_caps_report_gaps_not_holes(seed: int) -> None:
    _random_run(seed, ReplayLimits(events_per_session=6, bytes_per_session=900, sessions=2))
