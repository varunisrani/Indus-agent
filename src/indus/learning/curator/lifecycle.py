"""Curator phase 1: age the skills the review wrote, by inactivity alone (P09-18).

When to run (``should_run``): the curator is enabled and not paused, the last run is at least
``interval_hours`` (168) old, and — when the caller measured it — the agent has been idle for
``min_idle_hours`` (2). The very first look only records "now" as the last run, so a fresh install
or an upgrade never moves anything until one interval has passed (R07.7). Hosts call
``maybe_run`` on CLI session start and from the service's hourly maintenance timer; a profile
whose gateway is running leaves it to the gateway.

What a run does (``apply_transitions``) for each managed skill (``archive.managed_skills``; with
``curator.prune_builtins`` also bundled ones, archive only):

- pinned, or named by a reference source (cron, R07.8): left alone;
- no activity and no creation time on record: its clock starts now (seeded), nothing else;
- never used and younger than ``stale_after_days``: stays (a stale one becomes active again);
- inactive for ``archive_after_days`` (30): moved into the archive;
- inactive for ``stale_after_days`` (14): marked ``stale``; active again once used.

"Inactive for" counts from ``last_activity_at`` (any use, view or patch), else ``created_at``.
When a reference source fails the protected set is unknown and nothing is archived that run.
``run_curator`` adds the optional consolidation pass (``consolidate``), the snapshot before it,
the state update and the per-run report.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from indus.foundation.scope import Scope, thread_with_scope
from indus.learning.curator import referenced_skills
from indus.learning.curator.archive import (
    Ownership,
    Refused,
    archive_skill,
    bundled_local,
    managed_skills,
    parse_time,
)
from indus.learning.curator.report import write_report
from indus.learning.curator.settings import CuratorSettings
from indus.learning.curator.state import CuratorState
from indus.learning.skills.usage import usage_for

__all__ = [
    "RunOutcome",
    "Transitions",
    "apply_transitions",
    "curator_check",
    "maybe_run",
    "run_curator",
    "run_in_background",
    "should_run",
]

log = logging.getLogger(__name__)

FIRST_LOOK = "first look: the curator counts from now and runs after one interval"


def _now(scope: Scope, now: datetime | None) -> datetime:
    return (now or scope.clock.now()).astimezone(UTC)


# -- when ---------------------------------------------------------------------------------------


def should_run(
    scope: Scope,
    now: datetime | None = None,
    *,
    idle_s: float | None = None,
    settings: CuratorSettings | None = None,
) -> bool:
    """Is an automatic run due? (see the module notes; the first look seeds and says no)."""
    config = settings or CuratorSettings.from_scope(scope)
    if not config.enabled:
        return False
    state = CuratorState(scope)
    current = state.load()
    if current.get("paused") is True:
        return False
    moment = _now(scope, now)
    last = parse_time(current.get("last_run_at"))
    if last is None:
        state.update(last_run_at=moment.isoformat(), last_summary=FIRST_LOOK)
        return False
    if moment - last < timedelta(hours=config.interval_hours):
        return False
    return idle_s is None or idle_s >= config.min_idle_hours * 3600.0


# -- what ---------------------------------------------------------------------------------------


@dataclass
class Transitions:
    checked: int = 0
    marked_stale: int = 0
    archived: int = 0
    reactivated: int = 0
    seeded: int = 0
    pinned: int = 0
    referenced: int = 0
    protection_unknown: bool = False
    changes: list[dict[str, str]] = field(default_factory=list[dict[str, str]])
    failures: list[dict[str, str]] = field(default_factory=list[dict[str, str]])

    def summary(self) -> str:
        parts = [
            f"{n} {label}"
            for n, label in (
                (self.marked_stale, "marked stale"),
                (self.archived, "archived"),
                (self.reactivated, "active again"),
            )
            if n
        ]
        return ", ".join(parts) or "no changes"

    def as_payload(self) -> dict[str, Any]:
        return {
            "checked": self.checked,
            "marked_stale": self.marked_stale,
            "archived": self.archived,
            "reactivated": self.reactivated,
            "seeded": self.seeded,
            "pinned": self.pinned,
            "referenced": self.referenced,
            "protection_unknown": self.protection_unknown,
        }


def _anchor(record: Mapping[str, Any]) -> datetime | None:
    return parse_time(record.get("last_activity_at")) or parse_time(record.get("created_at"))


def apply_transitions(
    scope: Scope,
    now: datetime | None = None,
    *,
    settings: CuratorSettings | None = None,
    dry_run: bool = False,
) -> Transitions:
    """Move managed skills between active, stale and archived; ``dry_run`` only counts."""
    config = settings or CuratorSettings.from_scope(scope)
    moment = _now(scope, now)
    stale_cut = moment - timedelta(days=config.stale_after_days)
    archive_cut = moment - timedelta(days=config.archive_after_days)
    protected = referenced_skills()
    out = Transitions(protection_unknown=protected is None)
    usage = usage_for(scope)
    owners: list[Ownership] = list(managed_skills(scope))
    if config.prune_builtins:
        owners += bundled_local(scope)

    def set_state(owner: Ownership, before: str, after: str) -> None:
        out.changes.append({"name": owner.name, "from": before, "to": after})
        if dry_run:
            return
        usage.update(
            owner.name,
            lambda rec, _now: rec.update(state=after, archived_at=None),
            force=owner.owner == "bundled",
        )

    for owner in owners:
        out.checked += 1
        record = owner.record
        name = owner.name
        if record.get("pinned") is True:
            out.pinned += 1
            continue
        if protected is not None and name in protected:
            out.referenced += 1
            continue
        anchor = _anchor(record)
        state = str(record.get("state") or "active")
        if anchor is None:
            out.seeded += 1
            if not dry_run:
                usage.update(
                    name,
                    lambda rec, now_text: rec.update(created_at=rec.get("created_at") or now_text),
                    force=owner.owner == "bundled",
                )
            continue
        never_used = not record.get("use_count")
        if never_used and anchor > stale_cut:
            if state == "stale":
                out.reactivated += 1
                set_state(owner, "stale", "active")
            continue
        if anchor <= archive_cut and protected is not None:
            out.changes.append({"name": name, "from": state, "to": "archived"})
            if dry_run:
                out.archived += 1
                continue
            try:
                archive_skill(scope, name, actor="curator", allow_bundled=config.prune_builtins)
            except (Refused, OSError) as exc:
                out.changes.pop()
                out.failures.append({"name": name, "error": str(exc)})
                log.warning("curator could not archive %s: %s", name, exc)
                continue
            out.archived += 1
        elif anchor <= stale_cut and state == "active":
            out.marked_stale += 1
            set_state(owner, "active", "stale")
        elif anchor > stale_cut and state == "stale":
            out.reactivated += 1
            set_state(owner, "stale", "active")
    return out


# -- a whole run ----------------------------------------------------------------------------------


@dataclass
class RunOutcome:
    started_at: datetime
    dry_run: bool
    transitions: Transitions
    consolidation: Any = None
    summary: str = ""
    report: str | None = None
    backup: str | None = None
    lines: list[str] = field(default_factory=list[str])


StepFactory = Callable[[Scope], Any]


def _consolidate(scope: Scope, step_factory: StepFactory | None, dry_run: bool) -> Any:
    from indus.learning.curator import consolidate

    step = None
    if step_factory is not None:
        try:
            step = step_factory(scope)
        except Exception as exc:
            log.warning("no model for the curator pass: %s", exc)
    return asyncio.run(consolidate.run_consolidation(scope, step, dry_run=dry_run))


def run_curator(
    scope: Scope,
    *,
    dry_run: bool = False,
    consolidate: bool | None = None,
    trigger: str = "manual",
    step_factory: StepFactory | None = None,
    now: datetime | None = None,
    on_line: Callable[[str], object] | None = None,
) -> RunOutcome:
    """One curator pass now (phase 1, then phase 2 when ``consolidate``); never raises.

    Must not be called from a running event loop (phase 2 runs its own); hosts use a thread
    (``run_in_background``). ``step_factory(scope)`` builds the phase 2 model step.
    """
    config = CuratorSettings.from_scope(scope)
    merge = config.consolidate if consolidate is None else consolidate
    started = _now(scope, now)
    clock_start = scope.clock.monotonic()
    state = CuratorState(scope)
    outcome = RunOutcome(started, dry_run, Transitions())

    def say(line: str) -> None:
        outcome.lines.append(line)
        if on_line is not None:
            try:
                on_line(line)
            except Exception:
                log.debug("curator line sink failed", exc_info=True)

    if not dry_run:
        # Only the consolidation pass rewrites skill text, so only it earns a snapshot; the
        # inactivity pass moves folders into the archive, which is its own undo. Old snapshots
        # age out either way.
        try:
            from indus.learning.curator.backup import prune_backups, snapshot

            if merge:
                snap = snapshot(scope, "before a curator run", settings=config)
                if snap is not None:
                    outcome.backup = snap.id
                    say(f"curator: snapshot {snap.id} taken")
            else:
                prune_backups(scope, keep=config.backup_keep)
        except Exception as exc:
            log.warning("skills snapshot before the curator run failed: %s", exc)
    try:
        outcome.transitions = apply_transitions(scope, started, settings=config, dry_run=dry_run)
    except Exception as exc:
        log.warning("curator lifecycle pass failed: %s: %s", type(exc).__name__, exc)
        outcome.transitions.failures.append({"name": "*", "error": str(exc)})
    auto = outcome.transitions.summary()
    prefix = "preview: " if dry_run else ""
    if not dry_run:
        state.change(
            lambda data: data.update(
                last_run_at=started.isoformat(),
                run_count=int(data.get("run_count") or 0) + 1,
                last_summary=f"{prefix}{auto}",
            )
        )
    if merge:
        result = _consolidate(scope, step_factory, dry_run)
        outcome.consolidation = result
        cons_payload = result.as_payload()
        summary = f"{prefix}{auto}; consolidation: {result.summary}"
        for line in result.rename_lines():
            say(line)
    else:
        cons_payload = {"ran": False, "summary": "skipped (consolidation is off)"}
        summary = f"{prefix}{auto}; consolidation: off"
    duration = max(0.0, scope.clock.monotonic() - clock_start)
    payload: dict[str, Any] = {
        "started_at": started.isoformat(),
        "duration_s": round(duration, 2),
        "dry_run": dry_run,
        "trigger": trigger,
        "backup": outcome.backup,
        "transitions": outcome.transitions.as_payload(),
        "state_changes": outcome.transitions.changes,
        "failures": outcome.transitions.failures,
        "consolidation": cons_payload,
    }
    folder = write_report(scope, started, payload)
    outcome.report = str(folder) if folder is not None else None
    outcome.summary = summary
    try:
        state.update(
            last_summary=summary if not dry_run else state.load().get("last_summary"),
            last_report=outcome.report,
            last_duration_s=round(duration, 2),
        )
    except Exception:
        log.warning("curator state not saved", exc_info=True)
    say(f"curator: {summary}")
    return outcome


def run_in_background(scope: Scope, **kwargs: Any) -> None:
    """Start ``run_curator`` on a daemon thread that keeps ``scope``."""
    thread = thread_with_scope(
        lambda: run_curator(scope, **kwargs), scope=scope, name="curator", daemon=True
    )
    thread.start()


def maybe_run(
    scope: Scope,
    *,
    idle_s: float | None = None,
    gateway_running: Callable[[], bool] | None = None,
    now: datetime | None = None,
    step_factory: StepFactory | None = None,
) -> RunOutcome | None:
    """Run an automatic pass when one is due and no other process holds the claim.

    Returns the outcome, or None when nothing ran. Never raises.
    """
    try:
        if gateway_running is not None and gateway_running():
            return None
        if not should_run(scope, now, idle_s=idle_s):
            return None
        state = CuratorState(scope)
        moment = _now(scope, now)
        if not state.claim(moment):
            return None
        try:
            return run_curator(scope, trigger="scheduled", now=moment, step_factory=step_factory)
        finally:
            state.release()
    except Exception:
        log.warning("automatic curator check failed", exc_info=True)
        return None


def curator_check(
    scope: Scope,
    *,
    idle_s: Callable[[], float | None] | None = None,
    gateway_running: Callable[[], bool] | None = None,
    step_factory: StepFactory | None = None,
) -> Callable[[], None]:
    """A maintenance-timer task for ``scope`` (see ``indus.service.maintenance``)."""

    def check() -> None:
        maybe_run(
            scope,
            idle_s=idle_s() if idle_s is not None else None,
            gateway_running=gateway_running,
            step_factory=step_factory,
        )

    return check
