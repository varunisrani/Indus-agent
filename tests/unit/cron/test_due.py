"""Due detection, lateness and catch-up (P14-06) on a fake clock."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from indus.cron.due import (
    LATE_TOLERANCE_S,
    classify_lateness,
    collect_due,
    evaluate,
    evaluate_all,
    grace_seconds,
)
from indus.cron.grammar import CronSchedule, IntervalSchedule, OnceSchedule, parse_schedule
from indus.cron.models import CronJob, DispatchStamp
from indus.cron.next_run import plan_next

NOW = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)  # a Monday
NY = ZoneInfo("America/New_York")


class Slots:
    def __init__(self, *taken: tuple[str, datetime]) -> None:
        self.taken = set(taken)

    def slot_attempted(self, job_id: str, scheduled_for: datetime) -> bool:
        return (job_id, scheduled_for) in self.taken


NONE = Slots()


def job(schedule: str | Any, **fields: Any) -> CronJob:
    parsed = parse_schedule(schedule, UTC, now=NOW) if isinstance(schedule, str) else schedule
    data: dict[str, Any] = {
        "id": fields.pop("id", "a1b2c3d4e5f6"),
        "created_at": NOW - timedelta(days=30),
        "prompt": "do it",
        "schedule": parsed,
        "schedule_display": parsed.display,
    }
    data.update(fields)
    return CronJob.model_validate(data)


def ev(j: CronJob, now: datetime = NOW, **kw: Any):  # noqa: ANN201
    return evaluate(j, now=now, tz=UTC, slots=kw.pop("slots", NONE), **kw)


# -- grace --------------------------------------------------------------------------------------


def test_grace_values() -> None:
    assert grace_seconds(parse_schedule("every 5m", UTC, now=NOW), tz=UTC, now=NOW) == 150
    assert grace_seconds(parse_schedule("every 1h", UTC, now=NOW), tz=UTC, now=NOW) == 1800
    assert grace_seconds(parse_schedule("every 1m", UTC, now=NOW), tz=UTC, now=NOW) == 120
    assert grace_seconds(parse_schedule("daily at 9am", UTC, now=NOW), tz=UTC, now=NOW) == 7200
    assert grace_seconds(parse_schedule("in 1h", UTC, now=NOW), tz=UTC, now=NOW) == 120
    broken = CronSchedule(expr="nope", display="nope")
    assert grace_seconds(broken, tz=UTC, now=NOW) == 120


def test_lateness_classes() -> None:
    assert classify_lateness(299, 1800) == "on_time"
    assert classify_lateness(LATE_TOLERANCE_S, 1800) == "on_time"
    assert classify_lateness(301, 1800) == "late"
    assert classify_lateness(1801, 1800) == "catch_up"
    assert classify_lateness(200, 150) == "catch_up"  # beyond a short grace even if < 300 s


@pytest.mark.parametrize(("late", "kind"), [(299, "on_time"), (301, "late"), (1799, "late")])
def test_hourly_job_lateness(late: int, kind: str) -> None:
    slot = NOW - timedelta(seconds=late)
    d = ev(job("every 1h", next_run_at=slot))
    assert d.fire and d.lateness == kind and d.scheduled_for == slot
    stamp = d.updates["last_dispatch"]
    assert isinstance(stamp, DispatchStamp) and stamp.lateness == kind
    assert stamp.lateness_s == pytest.approx(late)


# -- the plan's table ---------------------------------------------------------------------------


def test_hourly_three_hours_behind_runs_once() -> None:
    slot = NOW - timedelta(hours=3)
    j = job("every 1h", next_run_at=slot)
    d = ev(j)
    assert d.fire and d.lateness == "catch_up" and d.scheduled_for == slot
    after = j.model_copy(update=dict(d.updates))
    assert after.next_run_at is not None and after.next_run_at > NOW
    # Next tick a minute later: nothing more to catch up.
    assert ev(after, NOW + timedelta(minutes=1)).fire is False


def test_cron_hourly_behind_collapses_to_one() -> None:
    j = job("0 * * * *", next_run_at=NOW - timedelta(hours=3))
    d = ev(j)
    assert d.fire and d.updates["next_run_at"] == NOW + timedelta(hours=1)


def test_five_minute_job_grace_150() -> None:
    j = job("every 5m", next_run_at=NOW - timedelta(seconds=149))
    assert ev(j).lateness == "on_time"
    j2 = job("every 5m", next_run_at=NOW - timedelta(seconds=151))
    assert ev(j2).lateness == "catch_up"


def test_daily_grace_capped() -> None:
    j = job("daily at 10:00", next_run_at=NOW - timedelta(seconds=7199))
    assert ev(j).lateness == "late"
    j2 = job("daily at 10:00", next_run_at=NOW - timedelta(seconds=7201))
    assert ev(j2).lateness == "catch_up"


def test_catch_up_off_skips() -> None:
    slot = NOW - timedelta(hours=3)
    d = ev(job("every 1h", next_run_at=slot), catch_up_missed=False)
    assert d.fire is False
    assert d.updates["next_run_at"] > NOW
    assert d.note and "skipped" in d.note and "catch-up is off" in d.note
    # Within grace it still runs.
    d2 = ev(job("every 1h", next_run_at=NOW - timedelta(minutes=10)), catch_up_missed=False)
    assert d2.fire and d2.lateness == "late"


def test_paused_two_days_then_resumed_runs_once() -> None:
    slot = NOW - timedelta(days=2)
    paused = job("every 1h", next_run_at=slot, enabled=False, state="paused", paused_at=slot)
    for hours in range(0, 48, 6):
        assert ev(paused, slot + timedelta(hours=hours)).fire is False
    resumed = paused.model_copy(update={"enabled": True, "state": "scheduled", "paused_at": None})
    d = ev(resumed)
    assert d.fire and d.lateness == "catch_up"
    after = resumed.model_copy(update=dict(d.updates))
    assert ev(after, NOW + timedelta(minutes=5)).fire is False
    off = ev(resumed, catch_up_missed=False)
    assert off.fire is False and off.note and "skipped" in off.note


# -- other gates --------------------------------------------------------------------------------


def test_not_due_yet() -> None:
    d = ev(job("every 1h", next_run_at=NOW + timedelta(seconds=1)))
    assert d.fire is False and not d.updates


def test_exactly_due() -> None:
    assert ev(job("every 1h", next_run_at=NOW)).fire


def test_disabled_and_half_paused_and_completed() -> None:
    past = NOW - timedelta(minutes=1)
    assert ev(job("every 1h", next_run_at=past, enabled=False)).fire is False
    assert ev(job("every 1h", next_run_at=past, paused_at=past)).fire is False
    assert ev(job("every 1h", next_run_at=past, state="paused")).fire is False
    assert ev(job("every 1h", next_run_at=past, state="completed")).fire is False


def test_quota_hold() -> None:
    past = NOW - timedelta(minutes=1)
    held = job("every 1h", next_run_at=past, quota_hold_until=NOW + timedelta(minutes=5))
    assert ev(held).fire is False
    expired = job("every 1h", next_run_at=past, quota_hold_until=NOW - timedelta(seconds=1))
    assert ev(expired).fire is True


def test_attempted_slot_skipped() -> None:
    slot = NOW - timedelta(minutes=1)
    j = job("every 1h", next_run_at=slot)
    d = ev(j, slots=Slots((j.id, slot)))
    assert d.fire is False and d.updates["next_run_at"] > NOW
    once = job("in 1h", next_run_at=slot, id="000000000001")
    d2 = ev(once, slots=Slots((once.id, slot)))
    assert d2.fire is False and d2.updates == {"state": "completed"}


def test_oneshot_grace_and_missed() -> None:
    sched = OnceSchedule(run_at=NOW - timedelta(seconds=100), display="x")
    on_time = job(sched, next_run_at=sched.run_at)
    d = ev(on_time)
    assert d.fire and d.scheduled_for == sched.run_at and "next_run_at" not in d.updates
    late = OnceSchedule(run_at=NOW - timedelta(seconds=121), display="x")
    d2 = ev(job(late, next_run_at=late.run_at))
    assert d2.fire is False
    assert d2.updates["state"] == "completed" and d2.updates["last_status"] == "missed"


def test_oneshot_covered_by_outage() -> None:
    sched = OnceSchedule(run_at=NOW - timedelta(hours=2), display="x")
    j = job(sched, next_run_at=sched.run_at)
    d = ev(j, outage_since=NOW - timedelta(hours=3))
    assert d.fire and d.lateness == "catch_up"
    # Due well before the outage began: still missed.
    assert ev(j, outage_since=NOW - timedelta(minutes=30)).fire is False


def test_missing_next_run_repaired_without_firing() -> None:
    d = ev(job("every 1h", next_run_at=None))
    assert d.fire is False and d.updates["next_run_at"] == NOW + timedelta(hours=1)
    sched = OnceSchedule(run_at=NOW + timedelta(hours=1), display="x")
    d2 = ev(job(sched, next_run_at=None))
    assert d2.updates == {"next_run_at": sched.run_at}
    d3 = ev(job(sched, next_run_at=None, last_run_at=NOW))
    assert d3.updates == {"state": "completed"}
    broken = job(CronSchedule(expr="0 9 * * nope", display="x"), next_run_at=None)
    d4 = ev(broken)
    assert d4.updates["state"] == "error" and "not a valid" in d4.updates["schedule_error"]


def test_error_state_self_heals() -> None:  # R07.12
    broken = job(
        CronSchedule(expr="0 9 * * nope", display="x"),
        state="error",
        schedule_error="old reason",
        next_run_at=None,
    )
    d = ev(broken)
    assert d.fire is False and "not a valid" in d.updates["schedule_error"]
    same = broken.model_copy(update={"schedule_error": d.updates["schedule_error"]})
    assert ev(same).updates == {}  # unchanged reason: nothing to write
    fixed = broken.model_copy(update={"schedule": CronSchedule(expr="0 13 * * *", display="x")})
    healed = ev(fixed)
    assert healed.updates["state"] == "scheduled" and healed.updates["schedule_error"] is None
    assert healed.updates["next_run_at"] == NOW + timedelta(hours=1)
    once_error = job("in 1h", state="error")
    assert ev(once_error).updates == {}


def test_late_run_in_repeated_hour_not_doubled() -> None:
    # 30 1 * * * in New York, fall-back night: slot 01:30 EDT, ticker late into the second 01:xx.
    sched = CronSchedule(expr="30 1 * * *", display="x")
    slot = datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    now = datetime(2026, 11, 1, 6, 10, tzinfo=UTC)
    j = job(sched, next_run_at=slot)
    d = evaluate(j, now=now, tz=NY, slots=NONE)
    assert d.fire and d.updates["next_run_at"] == datetime(2026, 11, 2, 6, 30, tzinfo=UTC)
    ran = j.model_copy(update={**dict(d.updates), "last_run_at": now})
    assert plan_next(ran, now=now, tz=NY).next_run_at == datetime(2026, 11, 2, 6, 30, tzinfo=UTC)


def test_interval_bookkeeping_anchors_on_last_run() -> None:
    sched = IntervalSchedule(minutes=30, display="every 30m")
    j = job(sched, last_run_at=NOW - timedelta(minutes=2))
    assert plan_next(j, now=NOW, tz=UTC).next_run_at == NOW + timedelta(minutes=28)


def test_collect_due_orders_oldest_first() -> None:
    jobs = [
        job("every 1h", id="bbbbbbbbbbbb", next_run_at=NOW - timedelta(minutes=1)),
        job("every 1h", id="aaaaaaaaaaaa", next_run_at=NOW - timedelta(minutes=5)),
        job("every 1h", id="cccccccccccc", next_run_at=NOW + timedelta(minutes=5)),
        job("every 1h", id="dddddddddddd", next_run_at=None),
    ]
    decisions = evaluate_all(jobs, now=NOW, tz=UTC, slots=NONE)
    assert [d.job_id for d in collect_due(decisions)] == ["aaaaaaaaaaaa", "bbbbbbbbbbbb"]
    assert [d.job_id for d in decisions if d.updates and not d.fire] == ["dddddddddddd"]


def test_daemon_every_30m_fires_twice_per_hour() -> None:
    """A minute-by-minute pass over an hour with bookkeeping: two runs, each on time."""
    j = job("every 30m", next_run_at=NOW + timedelta(minutes=30))
    fired: list[datetime] = []
    now = NOW
    for _ in range(60):
        now += timedelta(minutes=1)
        d = evaluate(j, now=now, tz=UTC, slots=NONE)
        if d.fire:
            assert d.lateness == "on_time"
            fired.append(now)
            j = j.model_copy(update={**dict(d.updates), "last_run_at": now})
            j = j.model_copy(update={"next_run_at": plan_next(j, now=now, tz=UTC).next_run_at})
    assert fired == [NOW + timedelta(minutes=30), NOW + timedelta(minutes=60)]
