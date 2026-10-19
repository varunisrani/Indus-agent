"""Next-run computation (P14-02): DST folds and gaps on injected zones, anchors, grace, errors."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from indus.cron import next_run as nr
from indus.cron.errors import NextRunError, OneShotInPast
from indus.cron.grammar import CronSchedule, IntervalSchedule, OnceSchedule, parse_schedule
from indus.cron.models import CronJob, Repeat
from indus.cron.next_run import (
    check_oneshot_time,
    compute_next_run,
    following_slot,
    next_after,
    period_seconds,
    plan_next,
)
from indus.cron.zones import effective_zone, first_instant_at_or_after, instants_of

NY = ZoneInfo("America/New_York")
LONDON = ZoneInfo("Europe/London")
LORD_HOWE = ZoneInfo("Australia/Lord_Howe")  # 30-minute DST shift
UTCZ = ZoneInfo("UTC")


def cron(expr: str) -> CronSchedule:
    return CronSchedule(expr=expr, display=expr)


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)  # type: ignore[misc]


def local(zone: ZoneInfo, *args: int, fold: int = 0) -> datetime:
    return datetime(*args, tzinfo=zone, fold=fold)  # type: ignore[misc]


# -- fall back: 2026-11-01 in New York, 01:00–02:00 happens twice ------------------------------


def test_fall_back_fires_once_from_first_fold() -> None:
    s = cron("30 1 * * *")
    base = local(NY, 2026, 11, 1, 0, 10)  # EDT
    first = next_after(s, base, NY)
    # 01:30 EDT (fold 0). Compared in UTC: Python never equates a repeated wall time across zones.
    assert first == utc(2026, 11, 1, 5, 30)
    assert first.astimezone(NY).utcoffset() == timedelta(hours=-4)
    # After that run, the repeated 01:30 (EST, 06:30 UTC) is not a second slot.
    second = next_after(s, first, NY)
    assert second == local(NY, 2026, 11, 2, 1, 30)
    # Nor when the run started late, inside the repeated hour: the next slot steps from the slot.
    late_start = utc(2026, 11, 1, 6, 10)  # 01:10 EST
    assert following_slot(s, first, now=late_start, tz=NY) == second
    job = _job(s, last_run_at=late_start)
    assert plan_next(job, now=late_start, tz=NY, slot=first).next_run_at == second


def test_fall_back_base_in_second_fold() -> None:
    s = cron("30 1 * * *")
    base = local(NY, 2026, 11, 1, 1, 10, fold=1)  # 01:10 EST, first pass already over
    assert base.astimezone(UTC) == utc(2026, 11, 1, 6, 10)
    nxt = next_after(s, base, NY)
    assert nxt == utc(2026, 11, 1, 6, 30)  # 01:30 EST, still strictly after the base
    assert next_after(s, nxt, NY) == local(NY, 2026, 11, 2, 1, 30)


def test_fall_back_hourly_does_not_repeat_hour() -> None:
    s = cron("0 * * * *")
    runs: list[datetime] = []
    base = local(NY, 2026, 11, 1, 0, 30)
    for _ in range(4):
        base = next_after(s, base, NY)
        runs.append(base)
    assert [r.astimezone(UTC).hour for r in runs] == [5, 7, 8, 9]  # 01:00 EDT, then 02:00 EST


# -- spring forward: 2026-03-08 in New York, 02:00–03:00 does not exist ------------------------


def test_spring_forward_gap_fires_at_three_once() -> None:
    s = cron("30 2 * * *")
    base = local(NY, 2026, 3, 8, 0, 0)
    first = next_after(s, base, NY)
    assert first == local(NY, 2026, 3, 8, 3, 0)  # 07:00 UTC, the end of the gap
    assert first == utc(2026, 3, 8, 7, 0)
    second = next_after(s, first, NY)
    assert second == local(NY, 2026, 3, 9, 2, 30)


def test_spring_forward_quarter_hours_collapse() -> None:
    s = cron("*/15 * * * *")
    base = local(NY, 2026, 3, 8, 1, 50)
    seen = []
    for _ in range(3):
        base = next_after(s, base, NY)
        seen.append(base.astimezone(NY).strftime("%H:%M"))
    assert seen == ["03:00", "03:15", "03:30"]


def test_london_and_lord_howe_shifts() -> None:
    # Europe/London jumps 01:00 → 02:00 on 2026-03-29.
    s = cron("30 1 * * *")
    assert next_after(s, local(LONDON, 2026, 3, 29, 0, 0), LONDON) == utc(2026, 3, 29, 1, 0)
    # Lord Howe moves 30 minutes at 02:00 on 2026-10-04 (02:00 → 02:30).
    s2 = cron("15 2 * * *")
    got = next_after(s2, local(LORD_HOWE, 2026, 10, 4, 0, 0), LORD_HOWE)
    assert got.astimezone(LORD_HOWE).strftime("%H:%M") == "02:30"
    assert next_after(s2, got, LORD_HOWE).astimezone(LORD_HOWE).day == 5


def test_leap_day_cron() -> None:
    s = cron("0 9 29 2 *")
    assert next_after(s, utc(2026, 3, 1), UTCZ) == utc(2028, 2, 29, 9, 0)


def test_cron_never_matching_raises() -> None:
    with pytest.raises(NextRunError):
        next_after(cron("0 0 30 2 *"), utc(2026, 1, 1), UTCZ)


def test_bad_expression_raises() -> None:
    with pytest.raises(NextRunError, match="not a valid cron"):
        next_after(cron("nope"), utc(2026, 1, 1), UTCZ)


def test_missing_croniter_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "croniter":
            raise ImportError("no croniter")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(NextRunError, match="croniter"):
        next_after(cron("0 9 * * *"), utc(2026, 1, 1), UTCZ)


# -- intervals ----------------------------------------------------------------------------------


def test_interval_anchored_on_last_run() -> None:
    s = IntervalSchedule(minutes=30, display="every 30m")
    now = utc(2026, 1, 1, 12, 0)
    assert compute_next_run(s, now=now, tz=None) == utc(2026, 1, 1, 12, 30)
    last = utc(2026, 1, 1, 11, 47)
    assert compute_next_run(s, now=now, tz=None, last_run_at=last) == utc(2026, 1, 1, 12, 17)


def test_interval_is_utc_arithmetic_across_dst() -> None:
    s = IntervalSchedule(minutes=120, display="every 2h")
    base = local(NY, 2026, 11, 1, 0, 30)
    assert next_after(s, base, NY) - base == timedelta(hours=2)


def test_anchored_interval_lattice() -> None:
    anchor = utc(2026, 1, 1, 9, 0)
    s = IntervalSchedule(minutes=3 * 1440, anchor=anchor, display="every 3d at 09:00")
    assert next_after(s, utc(2025, 12, 30), None) == anchor
    assert next_after(s, anchor, None) == utc(2026, 1, 4, 9, 0)
    # A late run does not drift the lattice.
    assert next_after(s, utc(2026, 1, 4, 9, 47), None) == utc(2026, 1, 7, 9, 0)
    assert next_after(s, utc(2026, 1, 20, 0, 0), None) == utc(2026, 1, 22, 9, 0)


# -- one-shots ----------------------------------------------------------------------------------


def test_once_until_run() -> None:
    s = OnceSchedule(run_at=utc(2026, 1, 2), display="x")
    now = utc(2026, 1, 1)
    assert compute_next_run(s, now=now, tz=None) == utc(2026, 1, 2)
    assert compute_next_run(s, now=now, tz=None, last_run_at=utc(2026, 1, 2)) is None


@pytest.mark.parametrize(("late", "ok"), [(119, True), (120, True), (121, False), (-3600, True)])
def test_oneshot_grace(late: int, ok: bool) -> None:
    now = utc(2026, 1, 1, 12, 0)
    s = OnceSchedule(run_at=now - timedelta(seconds=late), display="x")
    if ok:
        check_oneshot_time(s, now)
    else:
        with pytest.raises(OneShotInPast):
            check_oneshot_time(s, now)


def test_recurring_never_past_checked() -> None:
    check_oneshot_time(cron("0 9 * * *"), utc(2026, 1, 1))


# -- plan_next: error state and self-heal (R07.12) ---------------------------------------------


def _job(schedule: CronSchedule | IntervalSchedule | OnceSchedule, **extra: object) -> CronJob:
    return CronJob.model_validate(
        {
            "id": "abcdef012345",
            "created_at": utc(2026, 1, 1),
            "prompt": "check",
            "schedule": schedule,
            "schedule_display": schedule.display,
            **extra,
        }
    )


def test_broken_expression_goes_to_error_then_heals() -> None:
    now = utc(2026, 1, 1, 8, 0)
    broken = _job(cron("0 9 * * nope"))
    plan = plan_next(broken, now=now, tz=UTCZ)
    assert plan.state == "error" and plan.next_run_at is None
    assert plan.schedule_error and "not a valid cron" in plan.schedule_error
    fixed = broken.model_copy(update={"schedule": cron("0 9 * * *")})
    plan = plan_next(fixed, now=now, tz=UTCZ)
    assert plan.state == "scheduled" and plan.next_run_at == utc(2026, 1, 1, 9, 0)


def test_plan_completes_only_when_runs_are_used() -> None:
    now = utc(2026, 1, 1, 8, 0)
    once = _job(OnceSchedule(run_at=now, display="x"), repeat=Repeat(times=1))
    assert plan_next(once, now=now, tz=None).state == "scheduled"
    ran = once.model_copy(update={"last_run_at": now, "repeat": Repeat(times=1, completed=1)})
    assert plan_next(ran, now=now, tz=None).state == "completed"
    rec = _job(cron("0 9 * * *"), repeat=Repeat(times=3, completed=3))
    assert plan_next(rec, now=now, tz=UTCZ).state == "completed"
    forever = _job(cron("0 9 * * *"), repeat=Repeat(times=None, completed=900))
    assert plan_next(forever, now=now, tz=UTCZ).state == "scheduled"


def test_period_seconds() -> None:
    now = utc(2026, 1, 1)
    assert period_seconds(cron("0 9 * * *"), tz=UTCZ, now=now) == 86400
    assert period_seconds(cron("*/5 * * * *"), tz=UTCZ, now=now) == 300
    assert period_seconds(IntervalSchedule(minutes=7, display="x"), tz=None, now=now) == 420
    assert period_seconds(OnceSchedule(run_at=now, display="x"), tz=None, now=now) is None
    assert period_seconds(cron("nope nope nope nope nope"), tz=UTCZ, now=now) is None


# -- zone helpers -------------------------------------------------------------------------------


def test_instants_of_fold_and_gap() -> None:
    assert len(instants_of(datetime(2026, 11, 1, 1, 30), NY)) == 2
    assert instants_of(datetime(2026, 3, 8, 2, 30), NY) == []
    assert first_instant_at_or_after(datetime(2026, 3, 8, 2, 30), NY) == utc(2026, 3, 8, 7, 0)
    assert len(instants_of(datetime(2026, 7, 1, 12, 0), NY)) == 1


def test_effective_zone_reads_localtime(tmp_path: Path) -> None:
    assert effective_zone(NY) is NY
    missing = effective_zone(None, localtime=tmp_path / "nothing")
    assert missing.utcoffset(datetime.now()) is not None


# -- property: strictly after the base (seeded; hypothesis is not installed) -------------------


def test_next_run_strictly_after_base() -> None:
    rng = random.Random(2_0261_019)
    zones = [NY, LONDON, LORD_HOWE, UTCZ, ZoneInfo("Asia/Kolkata"), ZoneInfo("Europe/Berlin")]
    exprs = [
        "30 1 * * *",
        "30 2 * * *",
        "0 * * * *",
        "*/15 * * * *",
        "0 9 * * 1-5",
        "45 23 * * 0,6",
        "0 2 1 * *",
        "5 1,2,3 * * *",
    ]
    # Bases cluster around transitions so folds and gaps get hit.
    hot = [utc(2026, 3, 8, 6), utc(2026, 11, 1, 5), utc(2026, 3, 29, 0), utc(2026, 10, 25, 0),
           utc(2026, 10, 3, 15), utc(2026, 4, 4, 14)]  # fmt: skip
    for _ in range(600):
        zone = rng.choice(zones)
        base = rng.choice(hot) + timedelta(seconds=rng.randint(-4 * 3600, 4 * 3600))
        roll = rng.randrange(3)
        if roll == 0:
            s: CronSchedule | IntervalSchedule = cron(rng.choice(exprs))
        elif roll == 1:
            s = IntervalSchedule(minutes=rng.randint(1, 3000), display="x")
        else:
            anchor = base + timedelta(minutes=rng.randint(-5000, 5000))
            s = IntervalSchedule(minutes=rng.randint(1, 5000), anchor=anchor, display="x")
        got = next_after(s, base, zone)
        assert got > base, (s, base, zone)
        if isinstance(s, CronSchedule):
            # And no slot was skipped: nothing valid lies strictly between base and got.
            assert next_after(s, base, zone) == got


def test_parsed_schedules_have_next_runs() -> None:
    now = utc(2026, 10, 20, 12)
    for text in ["30m", "weekdays at 9am", "every 3d at 09:00", "0 0 29 2 *", "in 2h"]:
        s = parse_schedule(text, NY, now=now)
        when = compute_next_run(s, now=now, tz=NY)
        assert when is not None and when > now


def test_max_candidates_constant() -> None:
    assert nr.MAX_CRON_CANDIDATES >= 500
