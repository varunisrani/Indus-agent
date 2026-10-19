"""Schedule grammar: what a person types → a once, interval or cron schedule (P14-01).

Forms, tried in this order (case-insensitive):

1. day/time phrase, optional leading ``every``: a day spec (``day``/``daily``/``everyday``,
   ``weekday(s)``, ``weekend(s)``, or weekday names / three-letter abbreviations joined by spaces,
   commas or ``and``), an optional ``at``, a time (``9am``, ``9:30pm``, ``14:00``, bare hour ``7``,
   ``noon``/``midday``, ``midnight``) → 5-field cron;
2. ``every <N> day(s) at <time>`` → daily cron (N = 1), weekly cron (N = 7), else an interval of
   N days anchored on the next occurrence of that time (D-16.54);
3. any other ``every <duration>`` → interval;
4. five or more fields of ``[A-Za-z0-9*,-/]`` → cron (exactly five accepted);
5. ISO timestamp (contains ``T`` or starts ``YYYY-MM-DD``) → once; naive means the configured
   agent zone;
6. ``in <duration>`` → once, now + duration in UTC;
7. bare duration → **recurring** interval (P6);
8. otherwise ``ScheduleError`` listing the forms.

Every schedule has a canonical ``display`` that parses back to the same schedule.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from indus.cron.errors import ScheduleError
from indus.cron.zones import UtcDatetime, effective_zone, first_instant_at_or_after

__all__ = [
    "FORMS_HELP",
    "CronSchedule",
    "IntervalSchedule",
    "OnceSchedule",
    "Schedule",
    "format_minutes",
    "is_recurring",
    "parse_duration",
    "parse_schedule",
    "validate_cron",
]


class _ScheduleBase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class OnceSchedule(_ScheduleBase):
    kind: Literal["once"] = "once"
    run_at: UtcDatetime
    display: str


class IntervalSchedule(_ScheduleBase):
    kind: Literal["interval"] = "interval"
    minutes: int = Field(ge=1)
    # Set for "every N days at HH:MM": runs fall on anchor + k * minutes.
    anchor: UtcDatetime | None = None
    display: str


class CronSchedule(_ScheduleBase):
    kind: Literal["cron"] = "cron"
    expr: str
    display: str


Schedule = Annotated[OnceSchedule | IntervalSchedule | CronSchedule, Field(discriminator="kind")]


def is_recurring(schedule: OnceSchedule | IntervalSchedule | CronSchedule) -> bool:
    return schedule.kind != "once"


FORMS_HELP = """\
  30m, 2h, every 2h, every day    repeat at that interval
  in 30m, in 2h                   run once after that delay
  weekdays at 9am, mon,fri 18:30  repeat on those days at that time (also daily, weekends)
  every 3d at 09:00               every N days at that time
  0 9 * * 1-5                     a five-field cron expression
  2026-11-01T09:00                run once at that time (the agent's time zone unless an offset
                                  is given)"""

# -- durations ----------------------------------------------------------------------------------

_UNIT_MINUTES = {"m": 1, "h": 60, "d": 1440}
_DURATION = re.compile(
    r"^(\d+)?\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)$", re.IGNORECASE
)


def _duration_minutes(text: str) -> int | None:
    match = _DURATION.match(text.strip())
    if match is None:
        return None
    count = int(match.group(1)) if match.group(1) else 1
    return count * _UNIT_MINUTES[match.group(2)[0].lower()]


def parse_duration(text: str) -> int:
    """``30m`` → 30, ``2 hours`` → 120, ``day`` → 1440; zero or junk raises ``ScheduleError``."""
    minutes = _duration_minutes(text)
    if minutes is None:
        raise ScheduleError(f"{text.strip()!r} is not a duration (use 30m, 2h, 1d)")
    if minutes <= 0:
        raise ScheduleError("a duration must be at least one minute")
    return minutes


def format_minutes(minutes: int) -> str:
    """The shortest exact unit: 1440 → ``1d``, 120 → ``2h``, 90 → ``90m``."""
    if minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


# -- times and day specs ------------------------------------------------------------------------

_CLOCK = re.compile(r"^(\d{1,2})(?::(\d{2}))?(am|pm)?$")
_NAMED_TIMES = {"noon": (12, 0), "midday": (12, 0), "midnight": (0, 0)}

# Cron day-of-week numbers: 0 = Sunday.
_DAY_NAMES = ("sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday")
_DAY_NUMBER: dict[str, int] = {}
for _number, _name in enumerate(_DAY_NAMES):
    _DAY_NUMBER[_name] = _number
    _DAY_NUMBER[_name + "s"] = _number
    _DAY_NUMBER[_name[:3]] = _number

_DAY_SPECS = {
    "day": "*",
    "daily": "*",
    "everyday": "*",
    "weekday": "1-5",
    "weekdays": "1-5",
    "weekend": "0,6",
    "weekends": "0,6",
}
_SPEC_DISPLAY = {"*": "daily", "1-5": "weekdays", "0,6": "weekends"}


def _clock_time(text: str) -> tuple[int, int] | None:
    compact = text.replace(" ", "").lower()
    if compact in _NAMED_TIMES:
        return _NAMED_TIMES[compact]
    match = _CLOCK.match(compact)
    if match is None:
        return None
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    meridiem = match.group(3)
    if meridiem is not None:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if meridiem == "pm" else 0)
    if hour > 23 or minute > 59:
        return None
    return hour, minute


def _day_field(tokens: list[str]) -> tuple[str, int] | None:
    """The cron day-of-week field and how many tokens it used, or None."""
    if not tokens:
        return None
    spec = _DAY_SPECS.get(tokens[0])
    if spec is not None:
        return spec, 1
    days: set[int] = set()
    used = 0
    for token in tokens:
        if token == "and" and days:
            used += 1
            continue
        number = _DAY_NUMBER.get(token)
        if number is None:
            break
        days.add(number)
        used += 1
    if not days:
        return None
    if tokens[used - 1] == "and":
        return None  # "mon and" with nothing after the "and"
    return ",".join(str(d) for d in sorted(days)), used


def _day_display(field: str) -> str:
    if field in _SPEC_DISPLAY:
        return _SPEC_DISPLAY[field]
    return ",".join(_DAY_NAMES[int(d)][:3] for d in field.split(","))


def _phrase(text: str) -> CronSchedule | None:
    tokens = text.replace(",", " ").split()
    found = _day_field(tokens)
    if found is None:
        return None
    field, used = found
    rest = tokens[used:]
    if rest and rest[0] == "at":
        rest = rest[1:]
    if not rest:
        return None
    clock = _clock_time(" ".join(rest))
    if clock is None:
        return None
    hour, minute = clock
    return CronSchedule(
        expr=f"{minute} {hour} * * {field}",
        display=f"{_day_display(field)} at {hour:02d}:{minute:02d}",
    )


# -- every N days at a time ---------------------------------------------------------------------

_EVERY_DAYS_AT = re.compile(r"^(\d+)\s*(?:d|day|days)\s+(?:at\s+)?(.+)$")


def _next_wall_time(hour: int, minute: int, zone: tzinfo, now: datetime) -> datetime:
    local_day: date = now.astimezone(zone).date()
    for offset in range(0, 3):
        wall = datetime.combine(local_day + timedelta(days=offset), time(hour, minute))
        instant = first_instant_at_or_after(wall, zone)
        if instant > now:
            return instant
    raise ScheduleError("could not place the first run")  # pragma: no cover - 3 days always do


def _every_days_at(
    rest: str, zone: tzinfo, now: datetime
) -> CronSchedule | IntervalSchedule | None:
    match = _EVERY_DAYS_AT.match(rest)
    if match is None:
        return None
    clock = _clock_time(match.group(2))
    if clock is None:
        return None
    count = int(match.group(1))
    if count <= 0:
        raise ScheduleError("every 0 days is not a schedule")
    hour, minute = clock
    hhmm = f"{hour:02d}:{minute:02d}"
    if count == 1:
        return CronSchedule(expr=f"{minute} {hour} * * *", display=f"daily at {hhmm}")
    first = _next_wall_time(hour, minute, zone, now)
    if count == 7:
        weekday = (first.astimezone(zone).weekday() + 1) % 7  # Python Monday=0 → cron Monday=1
        return CronSchedule(
            expr=f"{minute} {hour} * * {weekday}",
            display=f"{_DAY_NAMES[weekday][:3]} at {hhmm}",
        )
    return IntervalSchedule(minutes=count * 1440, anchor=first, display=f"every {count}d at {hhmm}")


# -- cron ---------------------------------------------------------------------------------------

_CRON_FIELD = re.compile(r"^[A-Za-z0-9*,\-/]+$")


def validate_cron(expr: str) -> str:
    """The expression with single spaces, or ``ScheduleError`` (five fields, croniter syntax)."""
    fields = expr.split()
    if len(fields) != 5:
        raise ScheduleError(
            f"a cron expression has five fields (minute hour day month weekday); "
            f"{expr.strip()!r} has {len(fields)}"
        )
    normal = " ".join(fields)
    try:
        from croniter import croniter  # pyright: ignore[reportMissingTypeStubs]
    except ImportError as exc:
        raise ScheduleError("cron expressions need the croniter package") from exc
    try:
        croniter(normal, datetime(2000, 1, 1))
    except Exception as exc:  # croniter raises several error types for a bad expression
        raise ScheduleError(f"{normal!r} is not a valid cron expression: {exc}") from exc
    return normal


def _looks_like_cron(text: str) -> bool:
    fields = text.split()
    if len(fields) < 5 or not all(_CRON_FIELD.match(f) for f in fields):
        return False
    # Five plain words ("whenever you feel like it") are a sentence, not a cron expression.
    return any(ch.isdigit() or ch == "*" for ch in text)


# -- one-shots ----------------------------------------------------------------------------------

_DATE_START = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _once_display(run_at: datetime, zone: tzinfo) -> str:
    local = run_at.astimezone(zone)
    if local.second == 0 and local.microsecond == 0:
        return local.isoformat(timespec="minutes")
    return local.isoformat()


def _once_at(text: str, zone: tzinfo) -> OnceSchedule:
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ScheduleError(
            f"cannot read the schedule {text!r} as a timestamp ({exc}). Use one of:\n{FORMS_HELP}"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        instant = first_instant_at_or_after(parsed.replace(tzinfo=None), zone)
    else:
        instant = parsed.astimezone(UTC)
    return OnceSchedule(run_at=instant, display=_once_display(instant, zone))


# -- entry point --------------------------------------------------------------------------------


def _forms_error(text: str) -> ScheduleError:
    return ScheduleError(f"cannot read the schedule {text!r}. Use one of:\n{FORMS_HELP}")


def parse_schedule(
    text: str, tz: tzinfo | None, *, now: datetime | None = None
) -> OnceSchedule | IntervalSchedule | CronSchedule:
    """Parse ``text``; ``tz`` is the configured agent zone (None = system zone)."""
    original = text.strip()
    if not original:
        raise _forms_error(text)
    zone = effective_zone(tz)
    current = (now or datetime.now(UTC)).astimezone(UTC)
    lowered = " ".join(original.lower().split())

    has_every = lowered.startswith("every ")
    rest = lowered[len("every ") :].strip() if has_every else lowered

    phrase = _phrase(rest)
    if phrase is not None:
        return phrase
    if has_every:
        anchored = _every_days_at(rest, zone, current)
        if anchored is not None:
            return anchored
        minutes = _duration_minutes(rest)
        if minutes is None:
            raise _forms_error(original)
        if minutes <= 0:
            raise ScheduleError("an interval must be at least one minute")
        return IntervalSchedule(minutes=minutes, display=f"every {format_minutes(minutes)}")

    if _looks_like_cron(original):
        expr = validate_cron(original)
        return CronSchedule(expr=expr, display=expr)

    if "T" in original or _DATE_START.match(original):
        return _once_at(original, zone)

    if lowered.startswith("in "):
        minutes = _duration_minutes(lowered[3:])
        if minutes is None:
            raise _forms_error(original)
        if minutes <= 0:
            raise ScheduleError("a delay must be at least one minute")
        run_at = (current + timedelta(minutes=minutes)).replace(microsecond=0)
        return OnceSchedule(run_at=run_at, display=_once_display(run_at, zone))

    minutes = _duration_minutes(lowered)
    if minutes is not None:
        if minutes <= 0:
            raise ScheduleError("an interval must be at least one minute")
        return IntervalSchedule(minutes=minutes, display=f"every {format_minutes(minutes)}")
    raise _forms_error(original)
