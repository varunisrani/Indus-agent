"""The cron job store: ``<home>/cron/jobs.json`` behind ``<home>/cron/jobs.lock`` (P14-03).

Every write takes the cross-process lock (at most 30 s, then ``CronStoreBusy``), re-reads the
file, changes it and replaces it atomically, so concurrent writers never lose a job. The folder is
owner-only, the file 0600, and a root writer gives a rewritten file back to its previous owner.

Loading repairs a hand-edited file: non-object entries and records that do not validate are
dropped (kept in ``jobs.rejected.json``), invalid counters are reset, and one warning names only
field names and value types — never content. The repair is written under the lock.

The store backs the curator's first ``SkillReferenceSource`` (R07.8, adapter in
``indus.service.cron_refs`` because cron and learning are sibling layers): every job's skills are
protected, and consolidation renames are applied to job skill lists.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
from collections.abc import Callable, Collection, Iterator, Mapping
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any, cast

from pydantic import ValidationError

from indus.cron.errors import AmbiguousJobRef, CronStoreBusy, InvalidJob, JobNotFound, NextRunError
from indus.cron.grammar import CronSchedule, IntervalSchedule, OnceSchedule, parse_schedule
from indus.cron.models import (
    AUTHORED_FIELDS,
    CronJob,
    JobSpec,
    Repeat,
    derive_label,
    merge_authored,
    new_job_id,
)
from indus.cron.next_run import check_oneshot_time, compute_next_run, plan_next
from indus.foundation.clock import resolve_zone
from indus.foundation.fsio import FileLock, LockTimeout, atomic_write_json
from indus.foundation.home import secure_file
from indus.foundation.profiles import mkdir_in_profile
from indus.foundation.scope import Scope

__all__ = [
    "CREATED_PAUSED_REASON",
    "FILE_VERSION",
    "LOCK_TIMEOUT_S",
    "CronPaths",
    "CronStore",
]

log = logging.getLogger(__name__)

FILE_VERSION = 1
LOCK_TIMEOUT_S = 30.0
CREATED_PAUSED_REASON = "created paused, waiting for approval"
SKILLS_ARCHIVED_REASON = "paused: every skill it used was archived"

AnySchedule = OnceSchedule | IntervalSchedule | CronSchedule


class CronPaths:
    """Where one profile's cron files live."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self.cron_dir = home / "cron"
        self.jobs_file = self.cron_dir / "jobs.json"
        self.lock_file = self.cron_dir / "jobs.lock"
        self.rejected_file = self.cron_dir / "jobs.rejected.json"
        self.output_dir = self.cron_dir / "output"
        self.executions_db = self.cron_dir / "executions.db"
        self.tick_lock = self.cron_dir / ".tick.lock"


# -- reading and repairing ----------------------------------------------------------------------


class _Repair:
    """What a load had to fix; ``summary`` holds names and types only."""

    def __init__(self) -> None:
        self.notes: list[str] = []
        self.rejected: list[dict[str, Any]] = []

    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    def __bool__(self) -> bool:
        return bool(self.notes)

    def summary(self) -> str:
        return "; ".join(self.notes)


def _type_name(value: object) -> str:
    return "null" if value is None else type(value).__name__


def _fix_counter(record: dict[str, Any], path: tuple[str, ...], repair: _Repair) -> None:
    parent: dict[str, Any] = record
    for part in path[:-1]:
        child = parent.get(part)
        if not isinstance(child, dict):
            return
        parent = cast(dict[str, Any], child)
    key = path[-1]
    if key not in parent:
        return
    value = parent[key]
    if type(value) is int and value >= 0:
        return
    parent[key] = 0
    repair.note(f"reset {'.'.join(path)} ({_type_name(value)})")


def _fix_times(record: dict[str, Any], repair: _Repair) -> None:
    repeat = record.get("repeat")
    if not isinstance(repeat, dict) or "times" not in repeat:
        return
    rep = cast(dict[str, Any], repeat)
    value = rep["times"]
    if value is None or type(value) is int:
        return
    rep["times"] = None
    repair.note(f"reset repeat.times ({_type_name(value)})")


def _validation_fields(exc: ValidationError) -> str:
    parts: list[str] = []
    for error in exc.errors(include_url=False, include_input=False):
        loc = ".".join(str(p) for p in error.get("loc", ())) or "record"
        parts.append(f"{loc}: {error.get('type', 'invalid')}")
    return ", ".join(dict.fromkeys(parts))


def _parse_records(data: object, source: Path) -> tuple[list[CronJob], _Repair]:
    repair = _Repair()
    if isinstance(data, list):
        entries = cast(list[object], data)
        repair.note("wrapped a bare list of jobs")
    elif isinstance(data, dict) and isinstance(data.get("jobs"), list):
        entries = cast(list[object], cast(dict[str, Any], data)["jobs"])
    else:
        raise InvalidJob(
            f"{source} does not hold a job list (found {_type_name(data)})",
            hint="fix the file by hand or move it away; it was left as it is",
        )
    jobs: list[CronJob] = []
    seen: set[str] = set()
    dropped_types: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            dropped_types.append(_type_name(entry))
            continue
        record = cast(dict[str, Any], entry)
        _fix_counter(record, ("repeat", "completed"), repair)
        _fix_counter(record, ("failure_streak",), repair)
        _fix_times(record, repair)
        try:
            job = CronJob.model_validate(record)
        except ValidationError as exc:
            repair.note(f"dropped an invalid job record ({_validation_fields(exc)})")
            repair.rejected.append(record)
            continue
        if job.id in seen:
            repair.note("dropped a record with a duplicate id")
            repair.rejected.append(record)
            continue
        seen.add(job.id)
        jobs.append(job)
    if dropped_types:
        kinds = ", ".join(sorted(set(dropped_types)))
        repair.note(f"dropped {len(dropped_types)} non-object entries ({kinds})")
    return jobs, repair


# -- the store ----------------------------------------------------------------------------------


def _normalize_skills(skills: Collection[str]) -> list[str]:
    cleaned = (s.strip() for s in skills if isinstance(s, str))  # pyright: ignore[reportUnnecessaryIsInstance]
    return list(dict.fromkeys(s for s in cleaned if s))


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


def _check_modes(job: CronJob) -> None:
    if job.monitor_script and job.monitor_url:
        raise InvalidJob("monitor_script and monitor_url exclude each other; pick one source")
    if (job.monitor_script or job.monitor_url) and job.no_agent:
        raise InvalidJob("a monitor decides whether the agent runs; it cannot go with no_agent")
    if job.no_agent and not job.script:
        raise InvalidJob("no_agent needs a script: without the agent the script is the whole job")
    if not ((job.prompt or "").strip() or job.script or job.skills):
        raise InvalidJob("a job needs a prompt, a script or at least one skill")


class CronStore:
    """One profile's job store. Thread- and process-safe; every write is one locked transaction."""

    def __init__(
        self,
        scope: Scope,
        *,
        lock_timeout: float = LOCK_TIMEOUT_S,
        lock_wait: Callable[[float], object] | None = None,
        script_check: Callable[[str], None] | None = None,
    ) -> None:
        self.scope = scope
        self.paths = CronPaths(scope.home)
        self.clock = scope.clock
        self.lock_timeout = lock_timeout
        self._lock_wait = lock_wait
        self._script_check = script_check
        self._remove_hooks: list[Callable[[str], object]] = []
        self._in_txn = threading.local()

    # -- environment -----------------------------------------------------------------------------

    @property
    def tz(self) -> tzinfo | None:
        return resolve_zone(self.scope)

    def _now(self) -> datetime:
        return self.clock.now()

    def ensure_dirs(self) -> None:
        mkdir_in_profile(self.scope.home, "cron")

    def add_remove_hook(self, fn: Callable[[str], object]) -> None:
        """``fn(job_id)`` runs after a removal landed (notepad, output folder)."""
        self._remove_hooks.append(fn)

    # -- locking and file I/O --------------------------------------------------------------------

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        self.ensure_dirs()
        lock = FileLock(
            self.paths.lock_file, timeout=self.lock_timeout, clock=self.clock, wait=self._lock_wait
        )
        try:
            lock.acquire()
        except LockTimeout as exc:
            who = f"process {exc.holder_pid}" if exc.holder_pid is not None else "another process"
            raise CronStoreBusy(
                f"the cron job store {self.paths.jobs_file} is locked by {who} "
                f"(waited {self.lock_timeout:g} s)",
                hint="try again in a moment",
            ) from exc
        try:
            yield
        finally:
            lock.release()

    def _read_file(self) -> tuple[list[CronJob], _Repair]:
        try:
            text = self.paths.jobs_file.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            return [], _Repair()
        except OSError as exc:
            raise InvalidJob(f"cannot read {self.paths.jobs_file}: {exc}") from exc
        if not text.strip():
            return [], _Repair()
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise InvalidJob(
                f"{self.paths.jobs_file} is not valid JSON (line {exc.lineno})",
                hint="fix the file by hand or move it away; it was left as it is",
            ) from exc
        return _parse_records(data, self.paths.jobs_file)

    def _keep_rejected(self, rejected: list[dict[str, Any]]) -> None:
        if not rejected:
            return
        path = self.paths.rejected_file
        existing: list[Any] = []
        with contextlib.suppress(OSError, json.JSONDecodeError):
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                existing = cast(list[Any], loaded)
        atomic_write_json(path, existing + rejected)

    def _write(self, jobs: list[CronJob]) -> None:
        path = self.paths.jobs_file
        try:
            before = path.stat()
        except OSError:
            before = None
        payload = {"version": FILE_VERSION, "jobs": [job.to_record() for job in jobs]}
        atomic_write_json(path, payload)
        with contextlib.suppress(OSError):
            secure_file(path, home=self.scope.home, env=self.scope.env)
        _restore_owner(path, before)

    def _load_locked(self) -> list[CronJob]:
        jobs, repair = self._read_file()
        if repair:
            log.warning("repaired %s: %s", self.paths.jobs_file, repair.summary())
            self._keep_rejected(repair.rejected)
            self._write(jobs)
        return jobs

    # -- reads -----------------------------------------------------------------------------------

    def load(self) -> list[CronJob]:
        """Every job; a file that needs repair is repaired under the lock first."""
        jobs, repair = self._read_file()
        if not repair:
            return jobs
        with self._locked():
            return self._load_locked()

    def get(self, job_id: str) -> CronJob | None:
        return next((job for job in self.load() if job.id == job_id), None)

    def resolve(self, ref: str) -> CronJob:
        return _find(self.load(), ref)

    # -- transactions ----------------------------------------------------------------------------

    @contextlib.contextmanager
    def transaction(self) -> Iterator[list[CronJob]]:
        """The job list under the lock; saved when the block exits without an error."""
        if getattr(self._in_txn, "active", False):
            raise RuntimeError("cron store transactions do not nest")
        with self._locked():
            self._in_txn.active = True
            try:
                jobs = self._load_locked()
                before = [job.to_record() for job in jobs]
                yield jobs
                if [job.to_record() for job in jobs] != before:
                    self._write(jobs)
            finally:
                self._in_txn.active = False

    # -- authored writes -------------------------------------------------------------------------

    def _parse(self, schedule: str | AnySchedule, now: datetime) -> AnySchedule:
        if isinstance(schedule, str):
            return parse_schedule(schedule, self.tz, now=now)
        return schedule

    def _check_script(self, script: str | None) -> None:
        if script and self._script_check is not None:
            self._script_check(script)

    def create(self, spec: JobSpec) -> CronJob:
        """Validate, apply the defaults and append one job (see the module notes)."""
        now = self._now()
        schedule = self._parse(spec.schedule, now)
        check_oneshot_time(schedule, now)
        if spec.paused_reason is not None and not spec.paused:
            raise InvalidJob("paused_reason needs paused=True")
        skills = _normalize_skills(spec.skills)
        prompt = (spec.prompt or "").strip() or None
        script = _clean_text(spec.script)
        self._check_script(script)
        times = spec.repeat if spec.repeat is not None and spec.repeat > 0 else None
        if isinstance(schedule, OnceSchedule) and times is None:
            times = 1
        name = _clean_text(spec.name) or derive_label(prompt, skills, script) or None
        data: dict[str, Any] = {
            "id": new_job_id(),
            "created_at": now,
            "name": name,
            "prompt": prompt,
            "skills": skills,
            "schedule": schedule,
            "schedule_display": schedule.display,
            "repeat": Repeat(times=times),
            "deliver": _clean_text(spec.deliver) or ("origin" if spec.origin else "local"),
            "failure_deliver": _clean_text(spec.failure_deliver),
            "origin": spec.origin,
            "enabled_toolsets": spec.enabled_toolsets,
            "workdir": _clean_text(spec.workdir),
            "model": _clean_text(spec.model),
            "provider": _clean_text(spec.provider),
            "base_url": _clean_text(spec.base_url),
            "pinned": spec.pinned,
            "reasoning_effort": _clean_text(spec.reasoning_effort),
            "script": script,
            "interpreter": _clean_text(spec.interpreter),
            "no_agent": spec.no_agent,
            "monitor_script": _clean_text(spec.monitor_script),
            "monitor_url": _clean_text(spec.monitor_url),
            "context_from": _normalize_skills(spec.context_from),
        }
        if spec.paused:
            data.update(
                enabled=False,
                state="paused",
                paused_at=now,
                paused_reason=_clean_text(spec.paused_reason) or CREATED_PAUSED_REASON,
                next_run_at=None,
            )
        job = CronJob.model_validate(data)
        _check_modes(job)
        if not spec.paused:
            plan = plan_next(job, now=now, tz=self.tz)
            job.next_run_at = plan.next_run_at
            job.state = plan.state if plan.state == "error" else "scheduled"
            job.schedule_error = plan.schedule_error
        with self.transaction() as jobs:
            while any(other.id == job.id for other in jobs):
                job.id = new_job_id()
            jobs.append(job)
        return job

    def update(self, ref: str, **changes: Any) -> CronJob:
        """Change authored fields (and ``repeat`` = times); run state is never touched here.

        ``schedule`` may be text; a changed schedule re-plans the next run unless the job is
        paused or finished. ``skills=[]`` clears the skills.
        """
        unknown = sorted(set(changes) - AUTHORED_FIELDS - {"repeat"})
        if unknown:
            raise InvalidJob(f"these fields cannot be edited: {', '.join(unknown)}")
        now = self._now()
        with self.transaction() as jobs:
            index = _index_of(jobs, ref)
            current = jobs[index]
            edits = dict(changes)
            if "schedule" in edits:
                schedule = self._parse(edits["schedule"], now)
                check_oneshot_time(schedule, now)
                edits["schedule"] = schedule
                edits["schedule_display"] = schedule.display
                if "repeat" not in edits:
                    edits["repeat"] = _repeat_for_kind(current, schedule)
            for key in ("skills", "context_from"):
                if key in edits:
                    edits[key] = _normalize_skills(edits[key] or [])
            if "prompt" in edits:
                edits["prompt"] = (edits["prompt"] or "").strip() or None
            if "script" in edits:
                edits["script"] = _clean_text(edits["script"])
                self._check_script(edits["script"])
            if "repeat" in edits and isinstance(edits["repeat"], int | None):
                times = edits["repeat"]
                edits["repeat"] = {"times": times if times and times > 0 else None}
            try:
                updated = merge_authored(current, edits)
            except ValidationError as exc:
                raise InvalidJob(f"invalid change: {_validation_fields(exc)}") from exc
            _check_modes(updated)
            if "schedule" in edits and updated.schedule != current.schedule:
                updated.quota_hold_until = None
                if not updated.paused and updated.state != "completed":
                    _replan_from_now(updated, now, self.tz)
            jobs[index] = updated
        return updated

    def pause(self, ref: str, reason: str | None = None) -> CronJob:
        now = self._now()
        with self.transaction() as jobs:
            index = _index_of(jobs, ref)
            job = jobs[index].model_copy(deep=True)
            job.enabled = False
            if job.state not in ("completed", "error"):
                job.state = "paused"
            job.paused_at = now
            job.paused_reason = _clean_text(reason)
            jobs[index] = job
        return job

    def resume(self, ref: str) -> CronJob:
        """Re-enable; a recurring slot that came due while paused is kept (one catch-up or a
        logged skip, decided by the due pass), everything else is re-planned from now."""
        now = self._now()
        with self.transaction() as jobs:
            index = _index_of(jobs, ref)
            job = jobs[index].model_copy(deep=True)
            if job.repeat.exhausted or (not job.recurring and job.last_run_at is not None):
                raise InvalidJob(f"job {job.label!r} has finished its runs; create it again")
            job.enabled = True
            job.state = "scheduled"
            job.paused_at = None
            job.paused_reason = None
            kept = job.recurring and job.next_run_at is not None and job.next_run_at <= now
            if not kept:
                if isinstance(job.schedule, OnceSchedule):
                    check_oneshot_time(job.schedule, now)
                _replan_from_now(job, now, self.tz)
            jobs[index] = job
        return job

    def remove(self, ref: str) -> CronJob:
        with self.transaction() as jobs:
            index = _index_of(jobs, ref)
            removed = jobs.pop(index)
        for hook in list(self._remove_hooks):
            try:
                hook(removed.id)
            except Exception:  # a cleanup hook must not undo or fail the removal
                log.warning("cleanup after removing cron job %s failed", removed.id, exc_info=True)
        return removed

    def import_definitions(
        self, authored_by_id: Mapping[str, Mapping[str, Any]], *, paused_reason: str
    ) -> list[CronJob]:
        """Merge authored definitions: known ids keep their run state, new ids arrive paused."""
        now = self._now()
        with self.transaction() as jobs:
            by_id = {job.id: i for i, job in enumerate(jobs)}
            touched: list[CronJob] = []
            for job_id, authored in authored_by_id.items():
                fields = dict(authored)
                if isinstance(fields.get("schedule"), str):
                    fields["schedule"] = parse_schedule(fields["schedule"], self.tz, now=now)
                schedule = fields.get("schedule")
                if isinstance(schedule, OnceSchedule | IntervalSchedule | CronSchedule):
                    fields.setdefault("schedule_display", schedule.display)
                if job_id in by_id:
                    local = jobs[by_id[job_id]]
                    merged = merge_authored(local, fields)
                    if merged.schedule != local.schedule:
                        merged.quota_hold_until = None
                        if merged.runnable:
                            _replan_from_now(merged, now, self.tz)
                        else:
                            merged.next_run_at = None
                    jobs[by_id[job_id]] = merged
                    touched.append(merged)
                    continue
                seed: dict[str, Any] = {
                    "id": job_id,
                    "created_at": now,
                    "enabled": False,
                    "state": "paused",
                    "paused_at": now,
                    "paused_reason": paused_reason,
                    "next_run_at": None,
                }
                seed.update({k: v for k, v in fields.items() if k in AUTHORED_FIELDS})
                if "repeat" in fields:
                    raw = fields["repeat"]
                    times = raw.get("times") if isinstance(raw, Mapping) else raw
                    seed["repeat"] = {"times": times}
                try:
                    created = CronJob.model_validate(seed)
                except ValidationError as exc:
                    raise InvalidJob(
                        f"cron job {job_id!r} cannot be imported: {_validation_fields(exc)}"
                    ) from exc
                jobs.append(created)
                touched.append(created)
        return touched

    # -- scheduler writes ------------------------------------------------------------------------

    def patch(self, job_id: str, **fields: Any) -> CronJob | None:
        """Set scheduler-owned fields (``repeat`` included); None when the job is gone."""
        refused = sorted(set(fields) & (AUTHORED_FIELDS | {"id", "created_at"}))
        if refused:
            raise InvalidJob(f"patch only sets run state; use update for: {', '.join(refused)}")
        with self.transaction() as jobs:
            for index, job in enumerate(jobs):
                if job.id != job_id:
                    continue
                data = job.model_dump(mode="python")
                data.update(fields)
                try:
                    updated = CronJob.model_validate(data)
                except ValidationError as exc:
                    raise InvalidJob(f"invalid run state: {_validation_fields(exc)}") from exc
                jobs[index] = updated
                return updated
        return None

    # -- skills named by jobs (the curator's reference source, wired in the service layer) --------

    def referenced_skills(self) -> set[str]:
        """Every skill any job names, paused and finished jobs included (R07.8)."""
        return {skill for job in self.load() for skill in job.skills}

    def rewrite_skills(
        self, renames: Mapping[str, str], dropped: Collection[str]
    ) -> tuple[str, ...]:
        """Apply curator renames to job skill lists; returns one line per change.

        A dropped skill (archived without a successor) is removed; a job left with no prompt,
        script or skill is paused with a reason.
        """
        changes: list[str] = []
        now = self._now()
        with self.transaction() as jobs:
            for index, job in enumerate(jobs):
                if not any(s in renames or s in dropped for s in job.skills):
                    continue
                new_skills: list[str] = []
                for skill in job.skills:
                    if skill in renames:
                        new_skills.append(renames[skill])
                        changes.append(f"job {job.label} ({job.id}): {skill} → {renames[skill]}")
                    elif skill in dropped:
                        changes.append(f"job {job.label} ({job.id}): {skill} dropped")
                    else:
                        new_skills.append(skill)
                updated = job.model_copy(deep=True)
                updated.skills = list(dict.fromkeys(new_skills))
                if not ((updated.prompt or "").strip() or updated.script or updated.skills):
                    updated.enabled = False
                    if updated.state not in ("completed", "error"):
                        updated.state = "paused"
                    updated.paused_at = now
                    updated.paused_reason = SKILLS_ARCHIVED_REASON
                    changes.append(f"job {job.label} ({job.id}): paused, no skills left")
                jobs[index] = updated
        return tuple(changes)

    def rename_skill(self, old: str, new: str) -> tuple[str, ...]:
        return self.rewrite_skills({old: new}, ())


# -- helpers ------------------------------------------------------------------------------------


def _find(jobs: list[CronJob], ref: str) -> CronJob:
    return jobs[_index_of(jobs, ref)]


def _index_of(jobs: list[CronJob], ref: str) -> int:
    wanted = ref.strip()
    if not wanted:
        raise JobNotFound("no job reference given")
    for index, job in enumerate(jobs):
        if job.id == wanted:
            return index
    folded = wanted.casefold()
    matches = [i for i, job in enumerate(jobs) if job.name and job.name.casefold() == folded]
    if not matches:
        raise JobNotFound(f"no cron job has the id or name {wanted!r}")
    if len(matches) > 1:
        raise AmbiguousJobRef(wanted, tuple(jobs[i].id for i in matches))
    return matches[0]


def _repeat_for_kind(job: CronJob, schedule: AnySchedule) -> dict[str, Any]:
    """Keep the create-time repeat default when an edit switches between once and recurring."""
    times = job.repeat.times
    was_once = isinstance(job.schedule, OnceSchedule)
    is_once = isinstance(schedule, OnceSchedule)
    if is_once and not was_once and times is None:
        times = 1
    elif was_once and not is_once and times == 1:
        times = None
    return {"times": times}


def _replan_from_now(job: CronJob, now: datetime, tz: tzinfo | None) -> None:
    """Next run from ``now``; a recurring job that cannot be planned goes to ``error``."""
    try:
        when = compute_next_run(job.schedule, now=now, tz=tz)
    except NextRunError as exc:
        job.next_run_at = None
        job.state = "error"
        job.schedule_error = str(exc)
        return
    job.next_run_at = when
    if job.state == "error":
        job.state = "scheduled"
    job.schedule_error = None


def _restore_owner(path: Path, before: os.stat_result | None) -> None:
    """Give a file rewritten by root back to its previous owner (POSIX only)."""
    if before is None or os.name != "posix":
        return
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None or geteuid() != 0:
        return
    try:
        now = path.stat()
        if (now.st_uid, now.st_gid) != (before.st_uid, before.st_gid):
            os.chown(path, before.st_uid, before.st_gid)
    except OSError as exc:
        log.warning("could not give %s back to uid %s: %s", path, before.st_uid, exc)
