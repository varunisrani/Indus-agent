"""P14-14: repeat, failure streak, unreachable re-runs and the quota hold."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from indus.cron import ports
from indus.cron.bookkeeping import (
    RunOutcome,
    Settings,
    failure_nudge,
    hold_notice,
    is_model_unreachable,
    is_retry_fire,
    settle_run,
    usage_window_seconds,
)
from indus.cron.errors import NextRunError
from indus.cron.grammar import CronSchedule, IntervalSchedule, OnceSchedule
from indus.cron.models import CronJob, Repeat
from indus.cron.next_run import next_after
from indus.foundation.errors import ProviderHTTPError
from indus.foundation.scope import Scope

T0 = datetime(2026, 3, 2, 10, 0, tzinfo=UTC)


def Interval(minutes: int) -> IntervalSchedule:  # noqa: N802 - reads like the schedule it makes
    return IntervalSchedule(minutes=minutes, display=f"every {minutes}m")


def Cron(expr: str) -> CronSchedule:  # noqa: N802
    return CronSchedule(expr=expr, display=expr)


def Once(at: datetime) -> OnceSchedule:  # noqa: N802
    return OnceSchedule(run_at=at, display=f"once at {at.isoformat()}")


def next_run(schedule: Any, base: datetime) -> datetime | None:
    """The store's next-run rule in UTC (a bad expression → None)."""
    if isinstance(schedule, OnceSchedule):
        return None
    try:
        return next_after(schedule, base, UTC)
    except NextRunError:
        return None


def job(**fields: Any) -> CronJob:
    fields.setdefault("schedule", Interval(60))
    fields.setdefault("created_at", T0 - timedelta(days=1))
    return CronJob(id=fields.pop("id", "abcdef123456"), name=fields.pop("name", "build"), **fields)


def apply(j: CronJob, changes: dict[str, Any]) -> None:
    for key, value in changes.items():
        setattr(j, key, value)


def settle(j: CronJob, outcome: RunOutcome, now: datetime = T0, **settings: Any) -> Any:
    return settle_run(j, outcome, now=now, settings=Settings(**settings), next_run=next_run)


OK = RunOutcome(succeeded=True, status="ok")
FAILED = RunOutcome(succeeded=False, status="error", error="boom", model_calls=1)
UNREACHABLE = RunOutcome(succeeded=False, status="error", error="dns", model_unreachable=True)


def test_repeat_three_completes_after_three_runs_and_keeps_the_record() -> None:
    j = job(repeat=Repeat(times=3))
    for n in range(1, 4):
        s = settle(j, OK, T0 + timedelta(hours=n))
        apply(j, s.changes)
        assert j.repeat.completed == n
    assert s.completed and j.state == "completed" and j.next_run_at is None


def test_success_schedules_the_next_slot() -> None:
    j = job()
    s = settle(j, OK)
    assert s.changes["next_run_at"] == ports.iso(T0 + timedelta(hours=1))
    assert s.changes["state"] == "scheduled" and s.changes["last_status"] == "ok"
    assert s.changes["fire_claim"] is None and s.changes["last_run_at"] == ports.iso(T0)


def test_one_shot_completes() -> None:
    j = job(schedule=Once(T0), repeat=Repeat(times=1))
    assert settle(j, OK).completed


def test_recurring_job_whose_next_run_fails_goes_to_error_never_completed() -> None:
    j = job(schedule=Cron("bad"))
    s = settle(j, OK)
    assert not s.completed
    assert s.changes["state"] == "error" and "could not be worked out" in s.changes["last_error"]
    fixed = job(schedule=Cron("0 * * * *"), state="error")
    assert settle(fixed, OK).changes["state"] == "scheduled"


def test_paused_job_stays_paused() -> None:
    j = job(state="paused", enabled=False)
    assert "state" not in settle(j, OK).changes


def test_streak_three_adds_the_nudge_and_success_resets() -> None:
    j = job(name="nightly")
    nudges = []
    for _ in range(3):
        s = settle(j, FAILED)
        nudges.append(s.nudge)
        apply(j, s.changes)
    assert nudges[:2] == ["", ""] and "failed 3 runs in a row" in nudges[2]
    assert "indus cron pause nightly" in nudges[2]
    assert j.failure_streak == 3
    s = settle(j, OK)
    assert s.changes["failure_streak"] == 0 and s.nudge == ""


def test_nudge_threshold_zero_and_one_shots() -> None:
    j = job(failure_streak=5)
    assert failure_nudge(j, Settings(nudge_threshold=0)) == ""
    once = job(schedule=Once(T0), failure_streak=5)
    assert failure_nudge(once, Settings()) == ""
    assert "failed 6 runs" in failure_nudge(j, Settings())


def test_dns_failure_reruns_after_5_15_30_minutes_then_the_natural_slot() -> None:
    j = job(schedule=Interval(24 * 60), repeat=Repeat(times=None, completed=4))
    now = T0
    rungs = []
    outcome = UNREACHABLE
    for _ in range(3):
        s = settle(j, outcome, now)
        assert not s.notify and s.retry_at is not None
        rungs.append(s.retry_at - now)
        apply(j, s.changes)
        assert is_retry_fire(j, s.retry_at)
        now = s.retry_at
        outcome = RunOutcome(**{**UNREACHABLE.__dict__, "retry_run": True})
    assert rungs == [timedelta(minutes=5), timedelta(minutes=15), timedelta(minutes=30)]
    s = settle(j, outcome, now)  # ladder exhausted: back to the schedule
    assert s.retry_at is None and s.notify
    assert s.changes["unreachable_retry"] is None
    assert s.changes["next_run_at"] == ports.iso(now + timedelta(days=1))
    # re-runs did not count toward repeat: the first run counted once, the later ones were rungs
    apply(j, s.changes)
    assert j.repeat.completed == 5


def test_rung_never_passes_the_next_natural_slot() -> None:
    j = job(schedule=Interval(10))
    s = settle(j, UNREACHABLE)
    assert s.retry_at == T0 + timedelta(minutes=5)
    apply(j, s.changes)
    s = settle(
        j, RunOutcome(**{**UNREACHABLE.__dict__, "retry_run": True}), T0 + timedelta(minutes=5)
    )
    assert s.retry_at is None  # +15 min would pass the slot 10 min later
    assert s.changes["next_run_at"] == ports.iso(T0 + timedelta(minutes=15))


def test_ladder_off_one_shots_and_model_reached() -> None:
    assert settle(job(), UNREACHABLE, retry_unreachable=False).retry_at is None
    once = job(schedule=Once(T0 + timedelta(hours=1)), repeat=Repeat(times=2))
    assert settle(once, UNREACHABLE).retry_at is None
    j = job(unreachable_retry={"attempt": 1, "at": ports.iso(T0), "expr": None})
    s = settle(j, FAILED)
    assert s.retry_at is None and s.changes["unreachable_retry"] is None


def test_retry_fire_needs_the_same_schedule() -> None:
    at = T0 + timedelta(minutes=5)
    j = job(
        schedule=Cron("0 9 * * *"),
        unreachable_retry={"attempt": 1, "at": ports.iso(at), "expr": "0 9 * * *"},
    )
    assert is_retry_fire(j, at) and is_retry_fire(j, ports.iso(at))
    assert not is_retry_fire(j, at + timedelta(seconds=1))
    j.schedule = Cron("0 10 * * *")
    assert not is_retry_fire(j, at)


def test_quota_hold_parks_until_retry_after_and_coalesces_cron_slots() -> None:
    j = job(schedule=Cron("0 * * * *"))
    out = RunOutcome(succeeded=False, status="error", error="quota", quota_hold_s=3 * 3600)
    s = settle(j, out)
    # window ends 13:01; hourly slots 11:00..13:00 collapse into 14:00
    assert s.held_until == datetime(2026, 3, 2, 14, 0, tzinfo=UTC)
    assert s.changes["quota_hold_until"] == s.changes["next_run_at"] == ports.iso(s.held_until)
    assert s.notify
    assert "held until 2026-03-02 14:00 UTC" in hold_notice(s.held_until)
    apply(j, s.changes)
    again = settle(j, out, s.held_until)
    assert again.held_until is not None and not again.notify  # already parked: no second alert
    interval = job(schedule=Interval(30))
    s = settle(interval, out)
    assert s.held_until == T0 + timedelta(hours=3, seconds=60)


def test_sparse_cron_takes_one_recovery_run_at_the_boundary() -> None:
    j = job(schedule=Cron("0 9 * * *"))  # daily 09:00; now 10:00 → natural tomorrow 09:00
    out = RunOutcome(succeeded=False, status="error", error="quota", quota_hold_s=3600)
    s = settle(j, out)
    assert s.held_until == T0 + timedelta(hours=1, seconds=60)
    manual = RunOutcome(**{**out.__dict__, "manual": True})
    assert settle(job(schedule=Cron("0 9 * * *")), manual).held_until is None
    dense = job(schedule=Cron("0 * * * *"))
    short = RunOutcome(**{**out.__dict__, "quota_hold_s": 2340})
    assert settle(dense, short).held_until is None  # 11:00 is not half an hour past 10:40
    shorter = RunOutcome(**{**out.__dict__, "quota_hold_s": 60})
    assert settle(dense, shorter).held_until == T0 + timedelta(minutes=2)  # 58 min before 11:00


def test_mid_run_429_and_one_shots_are_not_held() -> None:
    mid = RunOutcome(succeeded=False, status="error", error="429", quota_hold_s=3600, model_calls=2)
    assert settle(job(schedule=Cron("0 * * * *")), mid).held_until is None
    once = job(schedule=Once(T0 + timedelta(hours=1)), repeat=Repeat(times=2))
    out = RunOutcome(succeeded=False, status="error", error="q", quota_hold_s=3600)
    assert settle(once, out).held_until is None


def _http_error(status: int, headers: dict[str, str], body: object) -> ProviderHTTPError:
    return ProviderHTTPError(f"HTTP {status}", status=status, headers=headers, body=body)


def test_unreachable_and_usage_window_classification() -> None:
    request = httpx.Request("POST", "https://api.example.test/v1/chat")
    dns = httpx.ConnectError("[Errno -2] Name or service not known", request=request)
    assert is_model_unreachable(dns, 0)
    assert not is_model_unreachable(dns, 1)
    assert not is_model_unreachable(httpx.ReadTimeout("slow", request=request), 0)
    assert not is_model_unreachable(ValueError("bad"), 0)
    assert not is_model_unreachable(_http_error(503, {}, "down"), 0)  # a server answered
    wrapped = RuntimeError("run failed")
    wrapped.__cause__ = dns
    assert is_model_unreachable(wrapped, 0)


@pytest.mark.parametrize("model_calls", [0, 3])
def test_usage_window_seconds_reads_retry_after(model_calls: int) -> None:
    err = _http_error(429, {"retry-after": "120"}, {"error": {"message": "usage limit reached"}})
    seconds = usage_window_seconds(err, now=T0, model_calls=model_calls)
    assert seconds == (120 if model_calls == 0 else None)
    assert usage_window_seconds(ValueError("x"), now=T0) is None


def test_settings_from_scope(scope: Scope) -> None:
    assert Settings.from_scope(scope) == Settings(nudge_threshold=3, retry_unreachable=True)
