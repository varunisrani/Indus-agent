"""Due detection, lateness and catch-up (P14-06): pure decisions over (job, now, ledger view).

- Due = enabled, no pause marker, not finished, not in a quota hold, ``next_run_at <= now``.
- Grace = half the schedule period clamped to [120 s, 7,200 s]; one-shots 120 s.
- Lateness: <= 300 s on time; <= grace late; beyond grace catch-up — the missed slots collapse
  into one run, identified by the oldest missed slot. ``cron.catch_up_missed: false`` skips it
  instead (next run moves past now, the skip is noted).
- A slot the ledger already has an attempt for is never run again (R07.13).
- A one-shot past its grace is closed as missed, unless it came due during a store outage.
- A recurring job in the ``error`` state is re-planned (self-heal, R07.12).

Decisions carry ``updates`` (scheduler fields to write); a firing decision's updates hold the
provisional ``next_run_at`` and the ``last_dispatch`` stamp, written by the ticker together with
its claim.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo
from typing import Any, Literal, Protocol

from indus.cron.errors import NextRunError
from indus.cron.grammar import CronSchedule, IntervalSchedule, OnceSchedule
from indus.cron.models import CronJob, DispatchStamp
from indus.cron.next_run import (
    ONESHOT_GRACE_S,
    compute_next_run,
    following_slot,
    next_after,
    period_seconds,
)

__all__ = [
    "LATE_TOLERANCE_S",
    "MAX_GRACE_S",
    "MIN_GRACE_S",
    "DueDecision",
    "Lateness",
    "SlotView",
    "classify_lateness",
    "collect_due",
    "evaluate",
    "evaluate_all",
    "grace_seconds",
]

Lateness = Literal["on_time", "late", "catch_up"]

LATE_TOLERANCE_S = 300
MIN_GRACE_S = 120
MAX_GRACE_S = 7200


class SlotView(Protocol):
    def slot_attempted(self, job_id: str, scheduled_for: datetime) -> bool: ...


@dataclass(frozen=True)
class DueDecision:
    job_id: str
    fire: bool
    scheduled_for: datetime | None = None
    lateness: Lateness | None = None
    lateness_s: float = 0.0
    updates: Mapping[str, Any] = field(default_factory=dict[str, Any])
    note: str | None = None


def grace_seconds(
    schedule: OnceSchedule | IntervalSchedule | CronSchedule, *, tz: tzinfo | None, now: datetime
) -> int:
    if isinstance(schedule, OnceSchedule):
        return ONESHOT_GRACE_S
    period = period_seconds(schedule, tz=tz, now=now)
    if not period:
        return MIN_GRACE_S
    return max(MIN_GRACE_S, min(int(period) // 2, MAX_GRACE_S))


def classify_lateness(lateness_s: float, grace_s: float) -> Lateness:
    if lateness_s > grace_s:
        return "catch_up"
    if lateness_s > LATE_TOLERANCE_S:
        return "late"
    return "on_time"


def _skip(job: CronJob, note: str | None = None, **updates: Any) -> DueDecision:
    return DueDecision(job.id, False, updates=updates, note=note)


def _heal(job: CronJob, *, now: datetime, tz: tzinfo | None) -> DueDecision:
    try:
        when = compute_next_run(job.schedule, now=now, tz=tz)
    except NextRunError as exc:
        if job.schedule_error == str(exc):
            return _skip(job)
        return _skip(job, f"still cannot plan the next run: {exc}", schedule_error=str(exc))
    return _skip(
        job,
        f"next run can be planned again ({when.isoformat() if when else 'none'})",
        state="scheduled",
        next_run_at=when,
        schedule_error=None,
    )


def _repair_missing_next(job: CronJob, *, now: datetime, tz: tzinfo | None) -> DueDecision:
    if isinstance(job.schedule, OnceSchedule):
        if job.last_run_at is not None:
            return _skip(job, "one-shot already ran", state="completed")
        return _skip(job, "restored the one-shot time", next_run_at=job.schedule.run_at)
    try:
        when = compute_next_run(job.schedule, now=now, tz=tz)
    except NextRunError as exc:
        return _skip(job, str(exc), state="error", next_run_at=None, schedule_error=str(exc))
    return _skip(job, "planned a missing next run", next_run_at=when)


def evaluate(
    job: CronJob,
    *,
    now: datetime,
    tz: tzinfo | None,
    slots: SlotView,
    catch_up_missed: bool = True,
    outage_since: datetime | None = None,
) -> DueDecision:
    """Does ``job`` fire now, and what should be written about it?"""
    if not job.enabled or job.has_pause_marker or job.state == "completed":
        return _skip(job)
    if job.state == "error":
        if not job.recurring:
            return _skip(job)
        return _heal(job, now=now, tz=tz)
    if job.quota_hold_until is not None and job.quota_hold_until > now:
        return _skip(job)
    slot = job.next_run_at
    if slot is None:
        return _repair_missing_next(job, now=now, tz=tz)
    if slot > now:
        return _skip(job)

    if slots.slot_attempted(job.id, slot):
        note = f"the run due at {slot.isoformat()} was already attempted; not running it again"
        if not job.recurring:
            return _skip(job, note, state="completed")
        try:
            return _skip(job, note, next_run_at=next_after(job.schedule, now, tz))
        except NextRunError as exc:
            return _skip(job, str(exc), state="error", next_run_at=None, schedule_error=str(exc))

    lateness_s = (now - slot).total_seconds()
    grace = grace_seconds(job.schedule, tz=tz, now=now)

    if isinstance(job.schedule, OnceSchedule):
        covered = outage_since is not None and slot >= outage_since - timedelta(seconds=grace)
        if lateness_s > grace and not covered:
            return _skip(
                job,
                f"one-shot due at {slot.isoformat()} was missed by more than {grace} s",
                state="completed",
                last_status="missed",
                last_error=f"missed: due at {slot.isoformat()}, not run within {grace} s",
            )
        kind = classify_lateness(lateness_s, grace) if not covered else "catch_up"
        stamp = DispatchStamp(
            scheduled_for=slot, dispatched_at=now, lateness_s=lateness_s, lateness=kind
        )
        return DueDecision(job.id, True, slot, kind, lateness_s, updates={"last_dispatch": stamp})

    kind = classify_lateness(lateness_s, grace)
    try:
        following = following_slot(job.schedule, slot, now=now, tz=tz)
    except NextRunError as exc:
        return _skip(job, str(exc), state="error", next_run_at=None, schedule_error=str(exc))
    if kind == "catch_up" and not catch_up_missed:
        return _skip(
            job,
            f"skipped the run due at {slot.isoformat()} (missed by {int(lateness_s)} s, "
            f"catch-up is off); next run {following.isoformat()}",
            next_run_at=following,
        )
    note = None
    if kind == "late":
        note = f"running {int(lateness_s)} s late (slot {slot.isoformat()})"
    elif kind == "catch_up":
        note = f"catching up once for runs missed since {slot.isoformat()}"
    stamp = DispatchStamp(
        scheduled_for=slot, dispatched_at=now, lateness_s=lateness_s, lateness=kind
    )
    return DueDecision(
        job.id,
        True,
        slot,
        kind,
        lateness_s,
        updates={"next_run_at": following, "last_dispatch": stamp},
        note=note,
    )


def evaluate_all(
    jobs: Iterable[CronJob],
    *,
    now: datetime,
    tz: tzinfo | None,
    slots: SlotView,
    catch_up_missed: bool = True,
    outage_since: datetime | None = None,
) -> list[DueDecision]:
    return [
        evaluate(
            job,
            now=now,
            tz=tz,
            slots=slots,
            catch_up_missed=catch_up_missed,
            outage_since=outage_since,
        )
        for job in jobs
    ]


def collect_due(decisions: Iterable[DueDecision]) -> list[DueDecision]:
    """The firing decisions, oldest slot first."""
    firing = [d for d in decisions if d.fire and d.scheduled_for is not None]
    return sorted(firing, key=lambda d: (d.scheduled_for, d.job_id))
