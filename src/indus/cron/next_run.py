"""When a schedule runs next (P14-02).

- Interval: ``(last_run_at or now) + minutes`` in UTC; an anchored interval returns the first
  ``anchor + k * minutes`` strictly after that base (no drift).
- Cron: matched on the zone's wall clock and converted to UTC. A repeated fall-back wall time is
  tried in both folds and the earliest instant strictly after the base wins, so a job never runs
  again in the repeated hour; a wall time inside a spring-forward gap maps to the end of the gap.
- Once: its instant until it has run.

A recurring job whose next run cannot be computed goes to the recoverable ``error`` state with
the reason in ``schedule_error`` — never ``completed`` (R07.12); ``plan_next`` gives the values.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from typing import TYPE_CHECKING, Literal

from indus.cron.errors import NextRunError, OneShotInPast
from indus.cron.grammar import CronSchedule, IntervalSchedule, OnceSchedule
from indus.cron.zones import effective_zone, first_instant_at_or_after, instants_of

if TYPE_CHECKING:
    from indus.cron.models import CronJob

__all__ = [
    "MAX_CRON_CANDIDATES",
    "ONESHOT_GRACE_S",
    "NextRunPlan",
    "check_oneshot_time",
    "compute_next_run",
    "following_slot",
    "next_after",
    "period_seconds",
    "plan_next",
]

AnySchedule = OnceSchedule | IntervalSchedule | CronSchedule

ONESHOT_GRACE_S = 120
# Wall-clock matches examined before giving up (an expression that never matches, or a zone with
# pathological transitions). Every real expression matches within a few hundred candidates.
MAX_CRON_CANDIDATES = 1000


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("naive datetime passed to the scheduler")
    return value.astimezone(UTC)


def _cron_after(expr: str, base: datetime, zone: tzinfo) -> datetime:
    try:
        from croniter import croniter  # pyright: ignore[reportMissingTypeStubs]
    except ImportError as exc:
        raise NextRunError("cron schedules need the croniter package (not installed)") from exc
    base_wall = base.astimezone(zone).replace(tzinfo=None)
    try:
        matches = croniter(expr, base_wall)
    except Exception as exc:  # croniter raises several error types for a bad expression
        raise NextRunError(f"{expr!r} is not a valid cron expression: {exc}") from exc
    for _ in range(MAX_CRON_CANDIDATES):
        try:
            wall = matches.get_next(datetime)
        except Exception as exc:  # e.g. an expression with no future match
            raise NextRunError(f"{expr!r} has no next run: {exc}") from exc
        found = instants_of(wall, zone)
        candidates = found if found else [first_instant_at_or_after(wall, zone)]
        for instant in candidates:
            if instant > base:
                return instant
    raise NextRunError(f"{expr!r} has no run after {base.isoformat()}")


def next_after(schedule: AnySchedule, base: datetime, tz: tzinfo | None) -> datetime:
    """The first run strictly after ``base`` (a once schedule returns its instant)."""
    base = _utc(base)
    match schedule:
        case OnceSchedule():
            return schedule.run_at
        case IntervalSchedule(anchor=None):
            return base + timedelta(minutes=schedule.minutes)
        case IntervalSchedule(anchor=anchor) if anchor is not None:
            if anchor > base:
                return anchor
            period = timedelta(minutes=schedule.minutes)
            steps = math.floor((base - anchor) / period) + 1
            return anchor + steps * period
        case CronSchedule():
            return _cron_after(schedule.expr, base, effective_zone(tz))
        case _:  # pragma: no cover - the union is closed
            raise NextRunError(f"unknown schedule kind {schedule!r}")


def compute_next_run(
    schedule: AnySchedule,
    *,
    now: datetime,
    tz: tzinfo | None,
    last_run_at: datetime | None = None,
) -> datetime | None:
    """The next run, or None when a once schedule has already run."""
    if isinstance(schedule, OnceSchedule):
        return None if last_run_at is not None else schedule.run_at
    return next_after(schedule, last_run_at or now, tz)


def check_oneshot_time(schedule: AnySchedule, now: datetime) -> None:
    """Refuse a once schedule more than 120 s in the past (create, update, resume)."""
    if not isinstance(schedule, OnceSchedule):
        return
    late = (_utc(now) - schedule.run_at).total_seconds()
    if late > ONESHOT_GRACE_S:
        raise OneShotInPast(
            f"the one-shot time {schedule.display} is {int(late)} s in the past "
            f"(at most {ONESHOT_GRACE_S} s is accepted)"
        )


def period_seconds(schedule: AnySchedule, *, tz: tzinfo | None, now: datetime) -> float | None:
    """The schedule's period: interval length, or the gap between the next two cron runs."""
    match schedule:
        case IntervalSchedule():
            return schedule.minutes * 60.0
        case CronSchedule():
            try:
                first = next_after(schedule, now, tz)
                second = next_after(schedule, first, tz)
            except NextRunError:
                return None
            gap = (second - first).total_seconds()
            return gap if gap > 0 else None
        case _:
            return None


@dataclass(frozen=True)
class NextRunPlan:
    next_run_at: datetime | None
    state: Literal["scheduled", "completed", "error"]
    schedule_error: str | None = None


def following_slot(
    schedule: CronSchedule | IntervalSchedule, slot: datetime, *, now: datetime, tz: tzinfo | None
) -> datetime:
    """The slot after a run of ``slot``: the next one after it, or after ``now`` if that passed.

    Stepping from the slot (not from when the run started) is what keeps a late run inside the
    repeated fall-back hour from scheduling the same wall time a second time.
    """
    following = next_after(schedule, slot, tz)
    return following if following > now else next_after(schedule, now, tz)


def plan_next(
    job: CronJob, *, now: datetime, tz: tzinfo | None, slot: datetime | None = None
) -> NextRunPlan:
    """What ``next_run_at`` / ``state`` should be after a run (or when healing an error).

    Cron steps from the run's slot (``slot``, else ``job.last_dispatch.scheduled_for``, else
    ``last_run_at``, else now); an interval from ``last_run_at`` (drift by lateness kept).
    """
    schedule = job.schedule
    if isinstance(schedule, OnceSchedule):
        if job.last_run_at is not None or job.repeat.exhausted:
            return NextRunPlan(None, "completed")
        return NextRunPlan(schedule.run_at, "scheduled")
    if job.repeat.exhausted:
        return NextRunPlan(None, "completed")
    try:
        if isinstance(schedule, CronSchedule):
            if slot is None and job.last_dispatch is not None:
                slot = job.last_dispatch.scheduled_for
            base = slot or job.last_run_at
            when = following_slot(schedule, base, now=now, tz=tz) if base else None
            when = when or next_after(schedule, now, tz)
        else:
            when = next_after(schedule, job.last_run_at or now, tz)
    except NextRunError as exc:
        return NextRunPlan(None, "error", str(exc))
    return NextRunPlan(when, "scheduled")
