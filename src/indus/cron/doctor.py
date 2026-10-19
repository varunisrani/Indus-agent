"""``indus cron doctor``: a read-only health check of the active jobs (P14-19).

Per active job (enabled, not paused, not completed) it reports: the last run failed; the last
result was not delivered, or a platform did not confirm it; the last fire was late or a catch-up
(when the ticker recorded ``last_dispatch``); the next run is missing or more than 15 minutes in
the past (the ticker is probably not running); the script is missing or resolves outside
``<home>/scripts/``; a script-only job has no script; the working folder is gone. A store that
cannot be written is a finding too. Nothing is changed; the command exits 1 while any finding
stands.

``overdue_seconds`` and ``OVERDUE_GRACE_S`` are shared with ``list`` and ``status`` so the three
agree on what "overdue" means.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from indus.cron import ports
from indus.foundation.scope import Scope

__all__ = [
    "OVERDUE_GRACE_S",
    "DoctorReport",
    "Finding",
    "duration_label",
    "findings_for",
    "is_active",
    "is_overdue",
    "overdue_seconds",
    "run_doctor",
]

OVERDUE_GRACE_S = 15 * 60
SUCCESS_STATUSES = frozenset({"ok", "silent", "no_change", "delivery_failed", "skipped"})


@dataclass(frozen=True)
class Finding:
    job_id: str
    name: str
    message: str


@dataclass(frozen=True)
class DoctorReport:
    checked: int
    findings: list[Finding] = field(default_factory=list[Finding])
    store_problem: str | None = None

    @property
    def ok(self) -> bool:
        return not self.findings and self.store_problem is None


def is_active(job: Any) -> bool:
    return bool(ports.field(job, "enabled", True)) and ports.field(job, "state") not in (
        "paused",
        "completed",
    )


def overdue_seconds(job: Any, now: datetime) -> float | None:
    """How far ``next_run_at`` lies in the past (negative while upcoming); None if unreadable."""
    when = ports.parse_instant(ports.field(job, "next_run_at"))
    return None if when is None else (now - when).total_seconds()


def is_overdue(job: Any, now: datetime) -> bool:
    late = overdue_seconds(job, now)
    return is_active(job) and late is not None and late > OVERDUE_GRACE_S


def duration_label(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    return f"{hours // 24}d {hours % 24}h"


def _script_issue(scripts: Path, name: str) -> str | None:
    raw = Path(name).expanduser()
    try:
        root = scripts.resolve()
        resolved = raw.resolve() if raw.is_absolute() else (scripts / raw).resolve()
    except OSError as exc:
        return f"script {name!r} cannot be resolved: {exc}"
    if not resolved.is_relative_to(root):
        return f"script {name!r} resolves outside {scripts}/"
    if not resolved.is_file():
        return f"script {name!r} is missing from {scripts}/"
    return None


def findings_for(job: Any, *, now: datetime, scripts: Path) -> list[str]:
    """What looks wrong with one job (see the module notes); empty when healthy."""
    issues: list[str] = []
    status = str(ports.field(job, "last_status", "") or "").strip().lower()
    if status and status not in SUCCESS_STATUSES:
        detail = str(ports.field(job, "last_error", "") or status).strip()
        issues.append(f"last run failed ({status}): {detail[:200]}")
    delivery_error = str(ports.field(job, "last_delivery_error", "") or "").strip()
    if delivery_error:
        issues.append(f"last result was not delivered: {delivery_error[:200]}")
    unverified = ports.field(job, "last_delivery_unverified")
    if unverified:
        listed = (
            ", ".join(str(t) for t in unverified)
            if isinstance(unverified, list)
            else str(unverified)
        )  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
        issues.append(f"last delivery was not confirmed by: {listed}")
    dispatch = ports.field(job, "last_dispatch")  # the ticker's stamp (models.DispatchStamp)
    kind = str(ports.field(dispatch, "lateness", "") or "") if dispatch is not None else ""
    if kind in ("late", "catch_up"):
        late = ports.field(dispatch, "lateness_s", 0) or 0
        label = "a catch-up for missed runs" if kind == "catch_up" else "late"
        issues.append(
            f"last run was {label} ({duration_label(float(late))} after its slot); this clears "
            "after the next on-time run"
        )
    if is_active(job):
        late = overdue_seconds(job, now)
        if not ports.field(job, "next_run_at"):
            issues.append("active job has no next run time")
        elif late is None:
            issues.append(f"next run time is not readable: {ports.field(job, 'next_run_at')!r}")
        elif late > OVERDUE_GRACE_S:
            issues.append(
                f"next run is {duration_label(late)} overdue: the job is not firing (is the "
                "ticker running? see `indus cron status`)"
            )
    script = str(ports.field(job, "script", "") or "").strip()
    if ports.field(job, "no_agent") and not script:
        issues.append("script-only job has no script")
    if script and (problem := _script_issue(scripts, script)):
        issues.append(problem)
    watcher = str(ports.field(job, "monitor_script", "") or "").strip()
    if watcher and (problem := _script_issue(scripts, watcher)):
        issues.append(f"monitor {problem}")
    workdir = str(ports.field(job, "workdir", "") or "").strip()
    if workdir and not Path(workdir).expanduser().is_dir():
        issues.append(f"working folder {workdir} no longer exists")
    return issues


def run_doctor(backend: ports.CronBackend, scope: Scope) -> DoctorReport:
    health = backend.store_health()
    store_problem = None if health.writable else (health.reason or "the cron store is not writable")
    jobs = [j for j in backend.list_jobs(include_disabled=False) if is_active(j)]
    now = scope.clock.now()
    scripts = ports.scripts_dir(scope)
    findings = [
        Finding(str(ports.field(j, "id", "?")), str(ports.field(j, "name", "") or ""), issue)
        for j in jobs
        for issue in findings_for(j, now=now, scripts=scripts)
    ]
    return DoctorReport(len(jobs), findings, store_problem)
