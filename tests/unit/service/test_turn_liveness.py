"""P17-04: the turn liveness watchdog of hosted turns (fake clock, race-safe abort)."""

from __future__ import annotations

from collections.abc import Callable

from indus.protocol.events import ExitReason, MessageDelta, TurnDone
from indus.service.turn_liveness import (
    STALLED,
    Activity,
    ActivityMeter,
    LivenessSettings,
    LivenessWatchdog,
    stall_failure,
    stall_message,
)
from tests.conftest import FAKE_CLOCK_START
from tests.fakes.clock import FakeClock
from tests.unit.backends._aio import aio

ON = LivenessSettings(timeout_s=600.0, poll_s=15.0)


class Rig:
    def __init__(
        self,
        settings: LivenessSettings = ON,
        *,
        accept: Callable[[Activity], bool] | None = None,
        waiting: Callable[[], bool] | None = None,
    ) -> None:
        self.clock = FakeClock(FAKE_CLOCK_START)
        self.meter = ActivityMeter(self.clock)
        self.aborts: list[Activity] = []
        self.deactivated = 0
        self._accept = accept

        def abort(seen: Activity) -> bool:
            self.aborts.append(seen)
            return True if self._accept is None else self._accept(seen)

        def deactivate() -> None:
            self.deactivated += 1

        self.watchdog = LivenessWatchdog(
            self.meter,
            settings,
            abort=abort,
            clock=self.clock,
            waiting=waiting,
            deactivate=deactivate,
            label="s1",
        )
        self.meter.begin(1)


def test_idle_600_s_aborts_once() -> None:
    rig = Rig()
    rig.clock.advance(599)
    assert rig.watchdog.tick() and rig.aborts == []
    rig.clock.advance(1)
    assert not rig.watchdog.tick()
    assert rig.aborts == [Activity(1, 0.0, 600.0)] and rig.watchdog.fired == rig.aborts[0]
    assert rig.deactivated == 1
    rig.clock.advance(600)
    assert not rig.watchdog.tick() and len(rig.aborts) == 1 and rig.deactivated == 1


def test_activity_at_599_s_resets() -> None:
    rig = Rig()
    rig.clock.advance(599)
    rig.meter.touch("model delta")
    rig.clock.advance(1)
    assert rig.watchdog.tick() and rig.aborts == []
    rig.clock.advance(598)
    assert rig.watchdog.tick() and rig.aborts == []
    rig.clock.advance(1)
    assert not rig.watchdog.tick()
    assert rig.aborts == [Activity(1, 599.0, 600.0)]


def test_activity_between_observe_and_act_vetoes_the_abort() -> None:
    rig = Rig()
    observe = rig.meter.observe

    def observe_then_touch() -> Activity | None:
        seen = observe()
        rig.meter.touch("tool heartbeat")  # another thread lands between observe and act
        return seen

    rig.meter.observe = observe_then_touch  # type: ignore[method-assign]
    rig.clock.advance(700)
    assert rig.watchdog.tick()  # still watching
    assert rig.aborts == [] and rig.deactivated == 0 and rig.watchdog.fired is None


def test_a_newer_generation_or_an_ended_turn_vetoes_the_abort() -> None:
    rig = Rig()
    rig.clock.advance(700)
    seen = rig.meter.observe()
    assert seen is not None
    rig.meter.begin(2)
    assert not rig.watchdog.commit(seen)
    rig.meter.end(2)
    assert not rig.watchdog.commit(seen)
    assert rig.aborts == []
    assert not rig.watchdog.tick()  # no turn running: the watchdog is done


def test_long_approval_wait_is_not_aborted() -> None:
    asking = [True]
    rig = Rig(waiting=lambda: asking[0])
    for _ in range(240):  # an hour of a person deciding
        rig.clock.advance(15)
        assert rig.watchdog.tick()
    assert rig.aborts == []
    asking[0] = False  # answered: idle counts from the end of the wait
    rig.clock.advance(585)
    assert rig.watchdog.tick() and rig.aborts == []
    rig.clock.advance(15)
    assert not rig.watchdog.tick() and len(rig.aborts) == 1


def test_a_refused_abort_keeps_watching() -> None:
    rig = Rig(accept=lambda seen: False)
    rig.clock.advance(600)
    assert rig.watchdog.tick() and rig.watchdog.fired is None and rig.deactivated == 0

    def boom(seen: Activity) -> bool:
        raise RuntimeError("turn already gone")

    rig2 = Rig(accept=boom)
    rig2.clock.advance(600)
    assert rig2.watchdog.tick() and rig2.watchdog.fired is None


def test_settings_off_means_no_watchdog() -> None:
    off = LivenessSettings.from_value(0)
    assert off.timeout_s is None
    rig = Rig(off)
    rig.clock.advance(10_000)
    assert not rig.watchdog.tick() and rig.aborts == []
    assert LivenessSettings.from_value("nan").timeout_s == 600.0
    assert LivenessSettings.from_value(None).timeout_s == 600.0


def test_events_count_as_activity_but_turn_done_does_not() -> None:
    rig = Rig()
    rig.clock.advance(300)
    rig.meter.observe_event(MessageDelta(session_id="s1", seq=1, turn_id="t", text="hi"))
    assert rig.meter.stamp == 300.0 and rig.meter.note == "message.delta"
    rig.clock.advance(300)
    rig.meter.observe_event(
        TurnDone(session_id="s1", seq=2, turn_id="t", reason=ExitReason.COMPLETED)
    )
    assert rig.meter.stamp == 300.0


def test_stall_failure_and_message() -> None:
    failure = stall_failure()
    assert failure.reason == STALLED == "stalled" and failure.retryable
    assert "600 s" in stall_message(600.4)


@aio
async def test_the_polling_task_aborts_on_the_clock() -> None:
    rig = Rig()
    task = rig.watchdog.start()
    assert task is not None
    await rig.clock.run_until_idle()
    for _ in range(39):
        await rig.clock.advance_and_settle(15)
    assert rig.aborts == [] and not task.done()
    await rig.clock.advance_and_settle(15)  # 600 s
    assert len(rig.aborts) == 1 and task.done()
    assert Rig(LivenessSettings(timeout_s=None)).watchdog.start() is None


@aio
async def test_stop_cancels_the_polling_task() -> None:
    rig = Rig()
    task = rig.watchdog.start()
    assert task is not None
    await rig.clock.run_until_idle()
    rig.watchdog.stop()
    await rig.clock.run_until_idle()
    assert task.cancelled()
