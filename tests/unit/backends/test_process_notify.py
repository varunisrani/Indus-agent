"""Watch-pattern limits, the global breaker, heartbeats and delivery routing (P05-13).

Pure state machines driven by explicit time values (the registry passes its clock's time)."""

from __future__ import annotations

from indus.backends.process_notify import (
    GLOBAL_COOLDOWN_S,
    GLOBAL_MAX_PER_WINDOW,
    HEARTBEAT_CHARS,
    WATCH_LIFETIME_MAX,
    WATCH_SPACING_S,
    Deliveries,
    GlobalBreaker,
    HeartbeatState,
    ProcessEvent,
    WatchState,
    completion_text,
    heartbeat_text,
)


def _event(session: str | None = "s1", home: str = "/h") -> ProcessEvent:
    return ProcessEvent("completion", "proc_abc", home, session, "make", "done")


# -- deliveries ---------------------------------------------------------------------------------


def test_events_reach_only_the_registered_session() -> None:
    deliveries = Deliveries()
    got: list[ProcessEvent] = []
    undo = deliveries.register("/h", "s1", got.append)
    assert deliveries.deliverable("/h", "s1")
    assert not deliveries.deliverable("/h", "s2")
    assert not deliveries.deliverable("/other", "s1")  # another profile (R02.8)
    assert not deliveries.deliverable("/h", None)
    assert deliveries.deliver(_event())
    assert not deliveries.deliver(_event(session="s2"))
    assert len(got) == 1
    undo()
    assert not deliveries.deliverable("/h", "s1")
    assert not deliveries.deliver(_event())


def test_a_failing_sink_does_not_raise() -> None:
    deliveries = Deliveries()

    def broken(event: ProcessEvent) -> None:
        raise RuntimeError("surface gone")

    deliveries.register("/h", "s1", broken)
    assert deliveries.deliver(_event()) is False


# -- watch patterns -----------------------------------------------------------------------------


def test_watch_delivers_first_match_then_holds_the_window() -> None:
    watch = WatchState(("READY",))
    first = watch.offer(["booting", "READY on :8000"], now=0.0)
    assert first.deliver and first.pattern == "READY" and first.lines == ("READY on :8000",)
    held = watch.offer(["READY again"], now=WATCH_SPACING_S - 1)
    assert not held.deliver
    later = watch.offer(["READY once more"], now=WATCH_SPACING_S + 0.1)
    assert later.deliver and later.suppressed == 1


def test_watch_ignores_lines_without_a_pattern() -> None:
    watch = WatchState(("ERROR",))
    assert not watch.offer(["all fine"], now=0.0).deliver
    assert watch.delivered == 0


def test_watch_three_strike_windows_switch_it_off() -> None:
    watch = WatchState(("x",))
    now = 0.0
    switched = ""
    for _ in range(3):
        assert watch.offer(["x"], now).deliver  # opens a window
        verdict = watch.offer(["x", "x"], now + 1)  # dropped inside it: a strike
        switched = verdict.switched_off
        now += WATCH_SPACING_S + 1
    assert watch.off
    assert "3 windows in a row" in switched
    assert not watch.offer(["x"], now + 100).deliver


def test_watch_calm_window_resets_the_strikes() -> None:
    watch = WatchState(("x",))
    watch.offer(["x"], 0.0)
    watch.offer(["x"], 1.0)  # strike 1
    watch.offer(["x"], 16.0)  # delivered; window 2 opens
    watch.offer(["x"], 17.0)  # strike 2
    watch.offer(["x"], 32.0)  # delivered; window 3 opens, no drops in it
    watch.offer(["x"], 48.0)  # delivered: window 3 was calm, the run is broken
    assert watch.strikes == 0
    assert not watch.off


def test_watch_lifetime_cap_after_eight_notices() -> None:
    watch = WatchState(("hit",))
    delivered = 0
    last = None
    for i in range(WATCH_LIFETIME_MAX + 3):
        last = watch.offer(["hit"], now=i * (WATCH_SPACING_S + 1))
        delivered += last.deliver
        if watch.off:
            break
    assert delivered == WATCH_LIFETIME_MAX
    assert last is not None and last.deliver and "8 notices" in last.switched_off


# -- global breaker -----------------------------------------------------------------------------


def test_global_breaker_trips_at_sixteen() -> None:
    breaker = GlobalBreaker()
    verdicts = [breaker.admit(0.5 + i * 0.1) for i in range(GLOBAL_MAX_PER_WINDOW + 1)]
    assert all(v.admit for v in verdicts[:GLOBAL_MAX_PER_WINDOW])
    assert not verdicts[-1].admit and verdicts[-1].tripped
    # silent through the cooldown, counting what it drops
    assert not breaker.admit(5.0).admit
    assert not breaker.admit(20.0).admit
    after = breaker.admit(2.1 + GLOBAL_COOLDOWN_S)
    assert after.admit and after.resumed == 3


def test_global_breaker_window_slides() -> None:
    breaker = GlobalBreaker()
    for i in range(GLOBAL_MAX_PER_WINDOW):
        assert breaker.admit(i * 0.1).admit
    assert breaker.admit(10.5).admit  # a new 10 s window


# -- heartbeats ---------------------------------------------------------------------------------


def test_heartbeat_quiet_tick_is_skipped() -> None:
    beat = HeartbeatState(every_s=60, last_at=0.0)
    assert not beat.due(59.0)
    assert beat.due(60.0)
    assert beat.take(60.0, "", 0) is None
    assert beat.seq == 0


def test_heartbeat_carries_only_new_output_capped() -> None:
    beat = HeartbeatState(every_s=60, last_at=0.0)
    first = beat.take(60.0, "abc", 3)
    assert first == ("abc", 0)
    buffer = "abc" + "y" * (HEARTBEAT_CHARS + 500)
    second = beat.take(120.0, buffer, len(buffer))
    assert second is not None
    text, omitted = second
    assert len(text) == HEARTBEAT_CHARS and omitted == 500
    assert beat.seq == 2
    assert beat.take(180.0, buffer, len(buffer)) is None  # nothing new


def test_message_texts_name_the_process_and_command() -> None:
    done = completion_text("proc_1", "make", "ok", exit_code=0, reason="exited")
    assert "proc_1" in done and "finished successfully" in done and "make" in done
    failed = completion_text("proc_1", "make", "", exit_code=2, reason="exited", cut=10)
    assert "exit code 2" in failed and "10 earlier characters" in failed
    killed = completion_text("proc_1", "make", "", exit_code=-15, reason="killed", source="kill")
    assert "was stopped" in killed
    beat = heartbeat_text("proc_1", "make", "x", seq=2, elapsed_s=130, every_s=60, omitted=4)
    assert "update 2" in beat and "2m10s" in beat and "4 earlier characters" in beat
