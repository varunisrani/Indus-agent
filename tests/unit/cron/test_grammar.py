"""Schedule grammar (P14-01): the accepted forms, their order, errors and display round-trips."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from indus.cron.errors import ScheduleError
from indus.cron.grammar import (
    FORMS_HELP,
    CronSchedule,
    IntervalSchedule,
    OnceSchedule,
    format_minutes,
    is_recurring,
    parse_duration,
    parse_schedule,
)

BERLIN = ZoneInfo("Europe/Berlin")
NEW_YORK = ZoneInfo("America/New_York")
# Tuesday 2026-10-20 12:00 UTC = 14:00 in Berlin.
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def parse(text: str, tz: ZoneInfo = BERLIN, now: datetime = NOW):  # noqa: ANN201
    return parse_schedule(text, tz, now=now)


# -- the plan's table ---------------------------------------------------------------------------


def test_bare_duration_is_recurring() -> None:  # P6, R07.9
    s = parse("30m")
    assert isinstance(s, IntervalSchedule) and s.minutes == 30 and s.anchor is None
    assert is_recurring(s)
    assert s.display == "every 30m"


def test_every_duration_is_interval() -> None:
    assert parse("every 30m") == parse("30m")


def test_in_duration_is_once() -> None:
    s = parse("in 30m")
    assert isinstance(s, OnceSchedule)
    assert s.run_at == NOW + timedelta(minutes=30)
    assert not is_recurring(s)


def test_weekdays_at_9am() -> None:
    s = parse("weekdays at 9am")
    assert isinstance(s, CronSchedule) and s.expr == "0 9 * * 1-5"


def test_day_list_with_commas_and_and() -> None:
    s = parse("mon, wed and fri 7")
    assert isinstance(s, CronSchedule) and s.expr == "0 7 * * 1,3,5"


def test_every_1d_at_is_daily() -> None:  # D-16.54, R07.10
    s = parse("every 1d at 09:00")
    assert isinstance(s, CronSchedule) and s.expr == "0 9 * * *"
    assert s.display == "daily at 09:00"


def test_naive_iso_uses_configured_zone() -> None:  # R07.11
    s = parse("2026-11-01T09:00", BERLIN)
    assert isinstance(s, OnceSchedule)
    assert s.run_at == datetime(2026, 11, 1, 8, 0, tzinfo=UTC)


def test_noon_and_midnight() -> None:
    assert parse("daily at noon") == parse("every day at 12:00")
    assert isinstance(n := parse("daily at noon"), CronSchedule) and n.expr == "0 12 * * *"
    assert isinstance(m := parse("every day at midnight"), CronSchedule) and m.expr == "0 0 * * *"
    assert isinstance(md := parse("weekends midday"), CronSchedule) and md.expr == "0 12 * * 0,6"


def test_garbage_lists_the_forms() -> None:
    with pytest.raises(ScheduleError) as info:
        parse("whenever you feel like it")
    text = str(info.value)
    assert FORMS_HELP in text and "\n" in text
    assert info.value.code == "cron.bad_schedule"


# -- more phrases -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expr"),
    [
        ("every monday 9am", "0 9 * * 1"),
        ("every weekday at 9:30pm", "30 21 * * 1-5"),
        ("weekends at 10", "0 10 * * 0,6"),
        ("daily 14:00", "0 14 * * *"),
        ("everyday at 6:05am", "5 6 * * *"),
        ("Tuesday 9am", "0 9 * * 2"),
        ("sun sat 23:59", "59 23 * * 0,6"),
        ("mondays and fridays at 12am", "0 0 * * 1,5"),
        ("tue,thu 12pm", "0 12 * * 2,4"),
        ("every day at 9 am", "0 9 * * *"),
        ("Every Wednesday At Noon", "0 12 * * 3"),
    ],
)
def test_phrases(text: str, expr: str) -> None:
    s = parse(text)
    assert isinstance(s, CronSchedule) and s.expr == expr


@pytest.mark.parametrize(
    ("text", "minutes"),
    [
        ("every 2h", 120),
        ("every 2 hours", 120),
        ("every day", 1440),
        ("every hour", 60),
        ("hour", 60),
        ("90 min", 90),
        ("2d", 2880),
        ("every 15 minutes", 15),
        ("1hr", 60),
    ],
)
def test_durations(text: str, minutes: int) -> None:
    s = parse(text)
    assert isinstance(s, IntervalSchedule) and s.minutes == minutes


@pytest.mark.parametrize(
    ("text", "expr"),
    [
        ("0 9 * * 1-5", "0 9 * * 1-5"),
        ("*/15  *  * * *", "*/15 * * * *"),
        ("0 9 * * MON-FRI", "0 9 * * MON-FRI"),
        ("30 2 1,15 * *", "30 2 1,15 * *"),
        ("0 0 29 2 *", "0 0 29 2 *"),
    ],
)
def test_cron_expressions(text: str, expr: str) -> None:
    s = parse(text)
    assert isinstance(s, CronSchedule) and s.expr == expr == s.display


def test_every_n_days_anchored_interval() -> None:  # D-16.54
    s = parse("every 3d at 09:00")
    assert isinstance(s, IntervalSchedule)
    assert s.minutes == 3 * 1440
    # 14:00 in Berlin now → first run tomorrow 09:00 CEST = 07:00 UTC.
    assert s.anchor == datetime(2026, 10, 21, 7, 0, tzinfo=UTC)
    assert s.display == "every 3d at 09:00"


def test_every_7_days_is_weekly_cron() -> None:
    s = parse("every 7 days at 08:15")
    # Next 08:15 Berlin after Tuesday 14:00 is Wednesday.
    assert isinstance(s, CronSchedule) and s.expr == "15 8 * * 3"
    assert s.display == "wed at 08:15"


def test_every_n_days_today_when_time_not_passed() -> None:
    s = parse("every 2d at 18:00")
    assert isinstance(s, IntervalSchedule)
    assert s.anchor == datetime(2026, 10, 20, 16, 0, tzinfo=UTC)


def test_iso_with_offset_kept() -> None:
    s = parse("2026-11-01T09:00+05:30")
    assert isinstance(s, OnceSchedule)
    assert s.run_at == datetime(2026, 11, 1, 3, 30, tzinfo=UTC)


def test_iso_date_only_and_space_form() -> None:
    s = parse("2026-11-02 07:30")
    assert isinstance(s, OnceSchedule) and s.run_at == datetime(2026, 11, 2, 6, 30, tzinfo=UTC)


def test_naive_iso_in_spring_gap_moves_to_gap_end() -> None:
    s = parse("2026-03-08T02:30", NEW_YORK)
    assert isinstance(s, OnceSchedule)
    assert s.run_at == datetime(2026, 3, 8, 7, 0, tzinfo=UTC)  # 03:00 EDT


def test_naive_iso_in_repeated_hour_takes_first() -> None:
    s = parse("2026-11-01T01:30", NEW_YORK)
    assert isinstance(s, OnceSchedule)
    assert s.run_at == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)  # 01:30 EDT


def test_in_duration_across_dst_is_elapsed_time() -> None:
    before = datetime(2026, 10, 25, 0, 30, tzinfo=UTC)  # 02:30 CEST, an hour before the change
    s = parse("in 2h", BERLIN, before)
    assert isinstance(s, OnceSchedule) and s.run_at == before + timedelta(hours=2)


def test_system_zone_when_none() -> None:
    s = parse_schedule("2026-11-01T09:00+00:00", None, now=NOW)
    assert isinstance(s, OnceSchedule) and s.run_at.hour == 9


# -- errors -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "every tuesday",
        "every",
        "0m",
        "every 0h",
        "in 0m",
        "in forever",
        "daily at 25:00",
        "mon 13pm",
        "weekdays at 9:75",
        "Tomorrow",
        "mon and",
        "every 0d at 09:00",
    ],
)
def test_rejected(text: str) -> None:
    with pytest.raises(ScheduleError):
        parse(text)


def test_six_field_cron_refused() -> None:
    with pytest.raises(ScheduleError, match="five fields"):
        parse("0 0 9 * * *")


def test_invalid_cron_refused() -> None:
    with pytest.raises(ScheduleError, match="not a valid cron"):
        parse("61 * * * *")


def test_parse_duration_direct() -> None:
    assert parse_duration("45m") == 45
    assert parse_duration("hours") == 60
    with pytest.raises(ScheduleError):
        parse_duration("soon")
    with pytest.raises(ScheduleError):
        parse_duration("0d")
    assert [format_minutes(m) for m in (30, 60, 90, 1440, 2880, 1500)] == [
        "30m", "1h", "90m", "1d", "2d", "25h",
    ]  # fmt: skip


def test_schedule_models_reject_unknown_keys() -> None:
    with pytest.raises(ValueError):
        IntervalSchedule.model_validate({"minutes": 5, "display": "x", "extra": 1})
    with pytest.raises(ValueError):
        IntervalSchedule.model_validate({"minutes": 0, "display": "x"})


# -- property: display round-trips (seeded; hypothesis is not installed) ------------------------


def _generated_texts(rng: random.Random) -> list[str]:
    days = ["daily", "weekdays", "weekend", "mon", "tue wed", "fri, sat and sun", "every thursday"]
    times = ["9am", "12pm", "12am", "7", "23:59", "noon", "midnight", "6:30pm", "00:00"]
    units = ["m", "min", "h", "hours", "d", "days"]
    out: list[str] = []
    for _ in range(80):
        roll = rng.randrange(7)
        if roll == 0:
            out.append(f"{rng.choice(days)} {rng.choice(['at ', ''])}{rng.choice(times)}")
        elif roll == 1:
            out.append(f"every {rng.randint(1, 500)}{rng.choice(units)}")
        elif roll == 2:
            out.append(f"{rng.randint(1, 500)}{rng.choice(units)}")
        elif roll == 3:
            out.append(f"in {rng.randint(1, 900)}{rng.choice(units)}")
        elif roll == 4:
            minute = rng.choice(["*", "0", "*/5", "15,45", str(rng.randint(0, 59))])
            hour = rng.choice(["*", "9", "1-5", str(rng.randint(0, 23))])
            dow = rng.choice(["*", "1-5", "0,6", "MON-FRI", "3"])
            out.append(f"{minute} {hour} * * {dow}")
        elif roll == 5:
            out.append(
                f"every {rng.randint(1, 30)}d at {rng.randint(0, 23):02d}:{rng.randint(0, 59):02d}"
            )
        else:
            day = NOW + timedelta(days=rng.randint(0, 400), minutes=rng.randint(0, 1440))
            out.append(day.astimezone(BERLIN).replace(tzinfo=None).isoformat(timespec="minutes"))
    return out


@pytest.mark.parametrize("zone", [BERLIN, NEW_YORK, ZoneInfo("Asia/Kolkata"), ZoneInfo("UTC")])
def test_display_round_trips(zone: ZoneInfo) -> None:
    rng = random.Random(1409)
    for text in _generated_texts(rng):
        first = parse(text, zone)
        again = parse(first.display, zone)
        assert again == first, (text, first.display)
