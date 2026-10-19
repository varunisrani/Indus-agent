"""After a run: counts, failure streak, the next run, unreachable re-runs and the quota hold.

``settle_run(job, outcome, now=…, settings=…, next_run=…)`` is pure: it reads the job record and
the run's outcome and returns a ``Settlement`` whose ``changes`` the caller writes onto the record
(inside the store's locked update). It can be called before delivering, to learn whether the
failure notice should go out now (``notify``) and which nudge or hold line to add, and again with
the delivery result to fill the delivery fields.

Rules (P14-14):

- ``repeat.completed`` grows by one per run, except for an unreachable-model re-run (its slot was
  already counted); a finite repeat that is reached ends the job (``state=completed``, record kept);
- the next run comes from the schedule (the host's ``plan``, else ``next_run(schedule, now)``);
  a recurring job whose next run cannot be computed goes to the recoverable ``error`` state, never
  ``completed`` (R07.12);
- ``failure_streak`` counts failed runs in a row (pre-agent failures included) and resets on
  success; a delivery failure is recorded in the delivery fields only (R07.17);
- a recurring job reaching ``cron.failure_nudge_threshold`` failures in a row gets a review nudge
  in its failure notice (never one-shots; 0 switches it off);
- unreachable model (network or DNS failure with no answered model call): recurring jobs re-run
  after 5, 15 and 30 minutes (never at or past the next natural slot), the notice is held while a
  re-run is pending, and a run that reaches the model ends the ladder (``cron.retry_unreachable``);
- quota hold: the provider said its usage window is closed for a known time and the whole
  fallback chain was unusable before any answered model call — a recurring job is parked until the
  window reopens (plus a minute), the cron slots inside the window collapse into the first one
  after it, and a sparse cron schedule may take one recovery run at the reopening boundary when its
  next slot is at least half a period later. One-shots are never retried or held.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from indus.cron import ports
from indus.foundation.scope import Scope

__all__ = [
    "HOLD_SLACK_S",
    "RETRY_DELAYS_S",
    "RunOutcome",
    "Settings",
    "Settlement",
    "failure_nudge",
    "hold_notice",
    "is_model_unreachable",
    "is_retry_fire",
    "settle_run",
    "usage_window_seconds",
]

RETRY_DELAYS_S: tuple[int, ...] = (300, 900, 1800)
HOLD_SLACK_S = 60.0

type NextRun = Callable[[Any, datetime], datetime | None]
type Plan = Callable[[dict[str, Any]], datetime | None]


@dataclass(frozen=True)
class Settings:
    nudge_threshold: int = 3
    retry_unreachable: bool = True

    @classmethod
    def from_scope(cls, scope: Scope) -> Settings:
        threshold = ports.number_setting(scope, "failure_nudge_threshold", 3)
        return cls(
            nudge_threshold=max(0, int(threshold)),
            retry_unreachable=ports.flag_setting(scope, "retry_unreachable", True),
        )


@dataclass(frozen=True)
class RunOutcome:
    """What the runner knows when a run ends.

    ``status``: ``ok``/``silent``/``no_change`` for successes, ``error``/``blocked_config``/…
    for failures. ``model_calls``: answered model calls in this run. ``model_unreachable``: the
    run failed on a network/DNS error before any answered model call (see
    ``is_model_unreachable``). ``quota_hold_s``: seconds the provider's usage window stays closed
    when the whole fallback chain was unusable (see ``usage_window_seconds``). ``retry_run``: this
    fire was an unreachable-model re-run. ``manual``: started by a person, not the schedule.
    ``delivery``: the ``DeliveryResult`` once known (anything with ``failed``, ``reason`` and
    ``unverified_targets``).
    """

    succeeded: bool
    status: str = ""
    error: str | None = None
    model_calls: int = 0
    model_unreachable: bool = False
    quota_hold_s: float | None = None
    retry_run: bool = False
    manual: bool = False
    delivery: Any = None


@dataclass(frozen=True)
class Settlement:
    changes: dict[str, Any]
    completed: bool = False
    retry_at: datetime | None = None
    held_until: datetime | None = None
    nudge: str = ""
    notify: bool = True
    """False when this failure's notice should be held (a re-run is pending, or the job was
    already parked by a quota hold)."""


# -- the main step ------------------------------------------------------------------------------


def settle_run(
    job: Any,
    outcome: RunOutcome,
    *,
    now: datetime,
    settings: Settings,
    next_run: NextRun,
    plan: Plan | None = None,
) -> Settlement:
    """The record changes for one finished run (see the module notes).

    ``plan`` (the host passes the store's next-run planner) gets the changes so far
    (``last_run_at``, ``repeat``) and gives the natural next run; it steps a cron job from the slot
    it ran for, so a late run inside a repeated fall-back hour never plans that wall time twice.
    Without it the next run comes from ``next_run(schedule, now)``."""
    changes: dict[str, Any] = {"last_run_at": ports.iso(now), "fire_claim": None}
    _status_fields(job, outcome, changes)
    nudge = "" if outcome.succeeded else failure_nudge(job, settings)
    times, done = ports.repeat_of(job)
    if not outcome.retry_run:
        done += 1
    raw_repeat = ports.field(job, "repeat")
    changes["repeat"] = {"times": ports.field(raw_repeat, "times"), "completed": done}
    if times is not None and done >= times:
        changes.update(
            state="completed", next_run_at=None, unreachable_retry=None, quota_hold_until=None
        )
        return Settlement(changes, completed=True, nudge=nudge)
    schedule = ports.field(job, "schedule")
    natural = _safe_plan(plan, changes) if plan is not None else _safe_next(next_run, schedule, now)
    paused = ports.field(job, "state") == "paused"
    recurring = ports.is_recurring(job)
    if natural is None:
        if recurring:
            changes["state"] = "error"
            changes["next_run_at"] = None
            if outcome.succeeded or not changes.get("last_error"):
                changes["last_error"] = (
                    "the next run of this recurring schedule could not be worked out; fix the "
                    "schedule (or the missing cron support) and the job is scheduled again"
                )
            changes.update(unreachable_retry=None, quota_hold_until=None)
            return Settlement(changes, nudge=nudge)
        changes.update(
            state="completed", next_run_at=None, unreachable_retry=None, quota_hold_until=None
        )
        return Settlement(changes, completed=True, nudge=nudge)
    changes["next_run_at"] = ports.iso(natural)
    if not paused:
        changes["state"] = "scheduled"
    retry_at = None
    if not outcome.succeeded and outcome.model_unreachable and recurring and not paused:
        retry_at = _plan_retry(job, now, natural, settings)
    if retry_at is not None:
        attempt = _ladder_attempt(job) + 1
        changes["next_run_at"] = ports.iso(retry_at)
        changes["unreachable_retry"] = {
            "attempt": attempt,
            "at": ports.iso(retry_at),
            "expr": _expr(schedule),
        }
    else:
        changes["unreachable_retry"] = None
    held_until = None
    already_held = bool(ports.field(job, "quota_hold_until"))
    if (
        not outcome.succeeded
        and outcome.quota_hold_s
        and outcome.quota_hold_s > 0
        and outcome.model_calls == 0
        and recurring
        and not paused
    ):
        held_until = _plan_hold(job, outcome, now, natural, next_run, already_held)
    if held_until is not None:
        changes["next_run_at"] = ports.iso(held_until)
        changes["quota_hold_until"] = ports.iso(held_until)
    else:
        changes["quota_hold_until"] = None
    notify = retry_at is None and not (held_until is not None and already_held)
    return Settlement(changes, retry_at=retry_at, held_until=held_until, nudge=nudge, notify=notify)


def _status_fields(job: Any, outcome: RunOutcome, changes: dict[str, Any]) -> None:
    result = outcome.delivery
    delivery_failed = bool(result is not None and getattr(result, "failed", False))
    if outcome.succeeded:
        status = "delivery_failed" if delivery_failed else (outcome.status or "ok")
        changes["failure_streak"] = 0
        changes["last_error"] = None
    else:
        status = outcome.status or "error"
        changes["failure_streak"] = int(ports.field(job, "failure_streak", 0) or 0) + 1
        changes["last_error"] = outcome.error or status
    changes["last_status"] = status
    if result is not None:
        changes["last_delivery_error"] = (
            getattr(result, "reason", None) if delivery_failed else None
        )
        unverified = tuple(getattr(result, "unverified_targets", ()) or ())
        changes["last_delivery_unverified"] = list(unverified) or None


def _safe_next(next_run: NextRun, schedule: Any, base: datetime) -> datetime | None:
    if schedule is None:
        return None
    try:
        return next_run(schedule, base)
    except Exception:
        return None


def _safe_plan(plan: Plan, changes: dict[str, Any]) -> datetime | None:
    try:
        return plan(dict(changes))
    except Exception:
        return None


def _expr(schedule: Any) -> str | None:
    value = ports.field(schedule, "expr") if schedule is not None else None
    return str(value) if value is not None else None


# -- failure streak nudge -------------------------------------------------------------------------


def failure_nudge(job: Any, settings: Settings) -> str:
    """The review line for a recurring job whose streak (this run included) hits the threshold."""
    if settings.nudge_threshold <= 0 or not ports.is_recurring(job):
        return ""
    streak = int(ports.field(job, "failure_streak", 0) or 0) + 1
    if streak < settings.nudge_threshold:
        return ""
    ref = str(ports.field(job, "name") or ports.field(job, "id") or "this job")
    if " " in ref:
        ref = str(ports.field(job, "id") or ref)
    return (
        f"\n\nThis job has failed {streak} runs in a row. Please look at it: fix its prompt or "
        f"settings, pause it (`indus cron pause {ref}`) or remove it (`indus cron remove {ref}`)."
    )


# -- unreachable model ladder ---------------------------------------------------------------------


def _ladder_attempt(job: Any) -> int:
    state = ports.field(job, "unreachable_retry")
    try:
        return int(ports.field(state, "attempt", 0) or 0) if state is not None else 0
    except (TypeError, ValueError):
        return 0


def _plan_retry(job: Any, now: datetime, natural: datetime, settings: Settings) -> datetime | None:
    attempt = _ladder_attempt(job)
    if not settings.retry_unreachable or attempt >= len(RETRY_DELAYS_S):
        return None
    rung = now + timedelta(seconds=RETRY_DELAYS_S[attempt])
    if natural <= rung:
        return None  # the next natural slot comes first: it is the re-run
    return rung


def is_retry_fire(job: Any, scheduled_for: datetime | str | None) -> bool:
    """True when ``scheduled_for`` is the re-run instant the ladder parked for this schedule."""
    state = ports.field(job, "unreachable_retry")
    if state is None or scheduled_for is None:
        return False
    at = ports.parse_instant(ports.field(state, "at"))
    when = ports.parse_instant(scheduled_for)
    if at is None or when is None or at != when:
        return False
    return ports.field(state, "expr") == _expr(ports.field(job, "schedule"))


def _causes(error: BaseException) -> list[BaseException]:
    seen: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and all(current is not s for s in seen) and len(seen) < 16:
        seen.append(current)
        current = current.__cause__ or current.__context__
    return seen


def is_model_unreachable(error: BaseException | None, model_calls: int) -> bool:
    """A network or name-resolution failure before any answered model call."""
    if error is None or model_calls > 0:
        return False
    from indus.providers.classify import classify

    verdicts = [classify(e) for e in _causes(error)]
    if any(v.status is not None for v in verdicts):
        return False  # a server answered
    return any(v.reason == "connection" for v in verdicts)


# -- quota hold -----------------------------------------------------------------------------------


class _At:
    def __init__(self, when: datetime) -> None:
        self._when = when

    def now(self) -> datetime:
        return self._when

    def monotonic(self) -> float:
        return 0.0

    async def sleep(self, seconds: float) -> None:  # pragma: no cover - never awaited here
        return None


def usage_window_seconds(
    error: BaseException | None, *, now: datetime, model_calls: int = 0
) -> float | None:
    """How long the provider's usage window stays closed, when ``error`` says so before any
    answered model call; None otherwise (mid-run 429s are not held)."""
    if error is None or model_calls > 0:
        return None
    from indus.providers.classify import classify

    for cause in _causes(error):
        verdict = classify(cause, clock=_At(now))
        if verdict.reason not in ("rate_limit", "billing"):
            continue
        if verdict.reset_at is not None:
            seconds = (verdict.reset_at - now).total_seconds()
            if seconds > 0:
                return seconds
        if verdict.retry_after is not None and verdict.retry_after > 0:
            return float(verdict.retry_after)
    return None


def _period_after(next_run: NextRun, schedule: Any, first: datetime) -> float | None:
    following = _safe_next(next_run, schedule, first)
    return (following - first).total_seconds() if following is not None else None


def _plan_hold(
    job: Any,
    outcome: RunOutcome,
    now: datetime,
    natural: datetime,
    next_run: NextRun,
    already_held: bool,
) -> datetime | None:
    window_end = now + timedelta(seconds=float(outcome.quota_hold_s or 0) + HOLD_SLACK_S)
    schedule = ports.field(job, "schedule")
    kind = ports.schedule_kind(job)
    if natural < window_end:
        if kind == "cron":
            return _safe_next(next_run, schedule, window_end) or window_end
        return window_end
    if kind != "cron" or outcome.manual or already_held:
        return None
    period = _period_after(next_run, schedule, natural)
    if period and (natural - window_end).total_seconds() >= period / 2:
        return window_end  # one recovery run at the reopening boundary
    return None


def hold_notice(held_until: datetime | None) -> str:
    """The line added to the one alert sent when a job enters a quota hold."""
    if held_until is None:
        return ""
    stamp = held_until.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    return (
        "\n\nThe model provider says its usage limit is reached and no fallback could take over. "
        f"This job is held until {stamp} and runs again then; no further alerts are sent while "
        "it waits."
    )
