"""The cron ticker: one pass (``indus cron tick``), the 60 s loop (``indus cron daemon``), fire
claims, the per-profile pool, manual runs and the global pause (P14-07, R07.13).

A pass: take the process-wide tick lock ``<home>/cron/.tick.lock`` without waiting (held → the
pass ends with "another tick is running"; the lock file cannot even be created → the store is
unwritable), write the ``ticker_heartbeat`` marker, probe the store (unwritable → no job fires),
reap dead ledger attempts, stop here while the global pause is on, ask the due rules which jobs
fire, then per job:

1. skip it when this process already runs it (in-flight registry, per home and job id);
2. claim it on its record under the store lock: a ``fire_claim`` ``{owner, claimed_at,
   heartbeat_at}`` whose last heartbeat is younger than ``CLAIM_TTL_S`` belongs to someone else;
   a recurring job's ``next_run_at`` moves to its following slot in the same write, so a crash
   mid-run can never fire that slot again;
3. record the attempt in the ledger before dispatch (refused when the slot already completed);
4. start it in the pool (``cron.max_parallel_jobs`` / ``INDUS_CRON_MAX_PARALLEL``).

A pass that finishes without an internal error writes ``ticker_last_success``. While a run is in
flight its claim and ledger attempt are refreshed every ``HEARTBEAT_S``; when the claim was taken
over, or could not be refreshed for ``HEARTBEAT_GRACE_S``, the run is cancelled (scripts and
agents kill their children on the way out). After the run: ``runner.settle`` (ledger, delivery,
bookkeeping), then the claim is released.

Global pause (``pause_all`` / ``resume_all``): a marker file; no scheduled fire starts while it is
there, runs in flight continue, manual runs (``Ticker.run_now``) still execute.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import logging
import os
import secrets
import socket
import sys
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from indus.cron import settings
from indus.cron.bookkeeping import is_retry_fire
from indus.cron.classify import RunOutcome
from indus.cron.ports import AttemptKind, DueSlot, JobSpec, spec_of
from indus.cron.runner import RunEnv, run_job, settle
from indus.foundation.clock import Clock
from indus.foundation.fsio import atomic_write_json, atomic_write_text
from indus.foundation.home import secure_dir

__all__ = [
    "CLAIM_TTL_S",
    "DRAIN_S",
    "HEARTBEAT_GRACE_S",
    "HEARTBEAT_S",
    "TICK_INTERVAL_S",
    "AlreadyRunning",
    "JobNotFound",
    "PauseState",
    "TickLock",
    "TickReport",
    "Ticker",
    "claim_is_live",
    "global_pause",
    "in_flight",
    "pause_all",
    "resume_all",
]

log = logging.getLogger(__name__)

CLAIM_TTL_S = 300.0
HEARTBEAT_S = 60.0
HEARTBEAT_GRACE_S = 3 * HEARTBEAT_S
TICK_INTERVAL_S = 60.0
DRAIN_S = 30.0
TICK_LOCK = ".tick.lock"
HEARTBEAT_MARKER = "ticker_heartbeat"
SUCCESS_MARKER = "ticker_last_success"
PAUSE_FILE = "paused_all.json"
BUSY_MESSAGE = "another tick is running"
_CONTENDED = frozenset({errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK})
_UNWRITABLE = frozenset({errno.ENOSPC, errno.EROFS, errno.EACCES, errno.EPERM, errno.EDQUOT})

# Runs in flight in this process, by (home, job id).
_INFLIGHT: dict[tuple[str, str], asyncio.Task[RunOutcome]] = {}
_inflight_lock = threading.Lock()


def in_flight(home: Path, job_id: str) -> bool:
    """Is ``job_id`` of the profile at ``home`` running in this process now?"""
    with _inflight_lock:
        return (str(home), job_id) in _INFLIGHT


class JobNotFound(LookupError):
    """No job matches the reference given to a manual run."""


class AlreadyRunning(RuntimeError):
    """The job is running now (here, or claimed by another process)."""


# -- fire claims ----------------------------------------------------------------------------------


def _get(record: Any, name: str) -> Any:
    if isinstance(record, Mapping):
        return cast(Mapping[str, Any], record).get(name)
    return getattr(record, name, None)


def _put(record: Any, name: str, value: Any) -> None:
    if isinstance(record, dict):
        cast(dict[str, Any], record)[name] = value
    else:
        setattr(record, name, value)


def _instant(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None


def _iso(when: datetime) -> str:
    return when.astimezone(UTC).isoformat()


def new_owner() -> str:
    """A claim owner: host, pid and a fresh random part (a process's later claim is new)."""
    try:
        host = socket.gethostname() or "host"
    except OSError:
        host = "host"
    return f"{host}:{os.getpid()}:{secrets.token_hex(4)}"


def _plain(value: object) -> object:
    """A stored claim as a mapping (the job model keeps it as a model object)."""
    dump = getattr(value, "model_dump", None)
    return dump(mode="json") if callable(dump) else value


def claim_is_live(claim: object, now: datetime, ttl: float = CLAIM_TTL_S) -> bool:
    """True for a well-formed claim whose last heartbeat is within ``[0, ttl)`` of ``now``.
    Malformed or future-dated claims are stale, so they can never wedge a job."""
    claim = _plain(claim)
    if not isinstance(claim, Mapping):
        return False
    data = cast(Mapping[str, Any], claim)
    stamp = _instant(data.get("heartbeat_at")) or _instant(data.get("claimed_at"))
    if stamp is None or not data.get("owner"):
        return False
    age = (now - stamp).total_seconds()
    return 0 <= age < ttl


def claim_owner(claim: object) -> str | None:
    claim = _plain(claim)
    if not isinstance(claim, Mapping):
        return None
    owner = cast(Mapping[str, Any], claim).get("owner")
    return owner if isinstance(owner, str) else None


def _take_claim(
    record: Any,
    *,
    owner: str,
    now: datetime,
    next_run: Callable[[Any, datetime], datetime | None] | None,
    updates: Mapping[str, Any] | None = None,
) -> JobSpec | None:
    """Claim ``record`` (inside the store's ``edit``); the fresh spec, or None when another live
    claim holds it. ``updates`` (the due rules' next run and dispatch stamp) are written in the
    same change; without a next run in them, ``next_run`` given, a recurring job moves to its
    following slot."""
    if claim_is_live(_get(record, "fire_claim"), now):
        return None
    _put(record, "fire_claim", {"owner": owner, "claimed_at": _iso(now), "heartbeat_at": _iso(now)})
    for name, value in (updates or {}).items():
        _put(record, name, value)
    spec = spec_of(record)
    if next_run is not None and spec.recurring and "next_run_at" not in (updates or {}):
        following = next_run(record, now)
        if following is not None:
            _put(record, "next_run_at", following)
    return spec


def _refresh_claim(record: Any, *, owner: str, now: datetime) -> bool:
    claim = _get(record, "fire_claim")
    if claim_owner(claim) != owner:
        return False
    fresh = dict(cast(Mapping[str, Any], _plain(claim)))
    fresh["heartbeat_at"] = _iso(now)
    _put(record, "fire_claim", fresh)
    return True


def _release_claim(record: Any, *, owner: str) -> bool:
    if claim_owner(_get(record, "fire_claim")) != owner:
        return False
    _put(record, "fire_claim", None)
    return True


# -- global pause and markers ---------------------------------------------------------------------


@dataclass(frozen=True)
class PauseState:
    reason: str
    since: datetime | None


def _cron_dir(home: Path) -> Path:
    return secure_dir(settings.cron_dir(home))


def pause_all(home: Path, reason: str, now: datetime) -> PauseState:
    """Stop every scheduled fire of this profile until ``resume_all``."""
    state = PauseState(reason.strip() or "paused by the user", now)
    atomic_write_json(
        _cron_dir(home) / PAUSE_FILE, {"reason": state.reason, "since": _iso(now)}, mode=0o600
    )
    return state


def resume_all(home: Path) -> bool:
    """Lift the global pause; False when none was on."""
    try:
        (settings.cron_dir(home) / PAUSE_FILE).unlink()
    except FileNotFoundError:
        return False
    return True


def global_pause(home: Path) -> PauseState | None:
    path = settings.cron_dir(home) / PAUSE_FILE
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return PauseState("paused (the pause record is unreadable)", None)
    data = cast(dict[str, Any], raw) if isinstance(raw, dict) else {}
    reason = data.get("reason")
    return PauseState(reason if isinstance(reason, str) else "paused", _instant(data.get("since")))


def write_marker(home: Path, name: str, now: datetime) -> None:
    try:
        atomic_write_text(_cron_dir(home) / name, _iso(now) + "\n", mode=0o600)
    except OSError as exc:
        log.debug("could not write the %s marker: %s", name, exc)


def read_marker(home: Path, name: str) -> datetime | None:
    try:
        return _instant((settings.cron_dir(home) / name).read_text(encoding="utf-8"))
    except OSError:
        return None


# -- the tick lock --------------------------------------------------------------------------------


if sys.platform == "win32":  # pragma: no cover - Windows is out of scope (D-16.55)
    import msvcrt

    def _try_lock(fd: int) -> None:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


class TickLock:
    """The process-wide tick lock: one open file description per holder, never waited for (two
    holders in one process conflict too, unlike ``fsio.FileLock``'s per-thread re-entry)."""

    def __init__(self, home: Path) -> None:
        self.path = settings.cron_dir(home) / TICK_LOCK
        self._fd: int | None = None

    def acquire(self) -> Literal["held", "busy", "unwritable"]:
        try:
            secure_dir(self.path.parent)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            if exc.errno in _UNWRITABLE:
                return "unwritable"
            raise
        try:
            _try_lock(fd)
        except OSError as exc:
            os.close(fd)
            if exc.errno in _CONTENDED:
                return "busy"
            raise
        self._fd = fd
        return "held"

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        with contextlib.suppress(OSError):
            _unlock(fd)
        os.close(fd)


# -- the ticker -----------------------------------------------------------------------------------


@dataclass
class TickReport:
    status: Literal["ran", "busy", "unwritable", "paused", "failed"]
    message: str = ""
    dispatched: list[str] = field(default_factory=list[str])
    outcomes: dict[str, RunOutcome] = field(default_factory=dict[str, RunOutcome])

    @property
    def exit_code(self) -> int:
        return 1 if self.status in ("failed", "unwritable") else 0


class _Progress:
    """Ledger heartbeats from agent progress, at most once per ``HEARTBEAT_S``."""

    def __init__(self, ticker: Ticker, attempt_id: str) -> None:
        self.ticker = ticker
        self.attempt_id = attempt_id
        self.last = ticker.clock.monotonic()

    def __call__(self) -> None:
        now = self.ticker.clock.monotonic()
        if now - self.last < HEARTBEAT_S:
            return
        self.last = now
        try:
            self.ticker.env.ports.ledger.heartbeat(self.attempt_id, self.ticker.clock.now())
        except Exception:
            log.debug("ledger heartbeat failed", exc_info=True)


class Ticker:
    """One profile's ticker (see the module notes). Use it on one event loop."""

    def __init__(self, env: RunEnv, *, clock: Clock | None = None) -> None:
        self.env = env
        self.clock = clock or env.scope.clock
        self.home = env.scope.home
        self._limit = settings.max_parallel(env.scope)
        self._slots: asyncio.Semaphore | None = None
        self._mine: set[asyncio.Task[RunOutcome]] = set()

    # -- passes ---------------------------------------------------------------------------------

    async def tick(self, *, wait: bool = True) -> TickReport:
        """One pass; with ``wait`` it also waits for the runs it started."""
        lock = TickLock(self.home)
        state = lock.acquire()
        if state == "busy":
            log.info("cron tick skipped: %s", BUSY_MESSAGE)
            return TickReport("busy", BUSY_MESSAGE)
        if state == "unwritable":
            return TickReport("unwritable", "the cron folder cannot be written; no job fires")
        tasks: dict[str, asyncio.Task[RunOutcome]] = {}
        try:
            report = await self._pass(tasks)
        except Exception as exc:
            log.exception("cron tick failed")
            report = TickReport("failed", f"{type(exc).__name__}: {exc}")
        finally:
            lock.release()
        report.dispatched = list(tasks)
        if wait and tasks:
            await asyncio.wait(set(tasks.values()))
            for job_id, task in tasks.items():
                if not task.cancelled() and task.exception() is None:
                    report.outcomes[job_id] = task.result()
        return report

    async def _pass(self, tasks: dict[str, asyncio.Task[RunOutcome]]) -> TickReport:
        ports = self.env.ports
        now = self.clock.now()
        write_marker(self.home, HEARTBEAT_MARKER, now)
        if not ports.store_writable(now):
            return TickReport("unwritable", "the cron store cannot be written; no job fires")
        try:
            reaped = ports.ledger.reap(now)
            if reaped:
                log.warning("%d cron attempt(s) whose owner died are now unknown", reaped)
        except Exception:
            log.warning("ledger reaping failed", exc_info=True)
        paused = global_pause(self.home)
        if paused is not None:
            write_marker(self.home, SUCCESS_MARKER, now)
            return TickReport("paused", f"all scheduled jobs are paused: {paused.reason}")
        for slot in ports.due(now):
            task = self._dispatch(slot, now)
            if task is not None:
                tasks[spec_of(slot.job).id] = task
        write_marker(self.home, SUCCESS_MARKER, self.clock.now())
        return TickReport("ran")

    def _dispatch(self, slot: DueSlot, now: datetime) -> asyncio.Task[RunOutcome] | None:
        ports = self.env.ports
        job_id = spec_of(slot.job).id
        key = (str(self.home), job_id)
        with _inflight_lock:
            if key in _INFLIGHT:
                log.info("job %s is still running here; not started again", job_id)
                return None
        owner = new_owner()
        kind: AttemptKind = "retry" if is_retry_fire(slot.job, slot.scheduled_for) else "scheduled"
        try:
            spec = ports.store.edit(
                job_id,
                lambda rec: _take_claim(
                    rec, owner=owner, now=now, next_run=ports.next_run, updates=slot.updates
                ),
            )
        except Exception as exc:
            # No durable claim (and no advance): firing could repeat the slot after a crash.
            log.error("job %s not started: its claim could not be written (%s)", job_id, exc)
            return None
        if spec is None:
            log.info("job %s is claimed elsewhere (or gone); skipped", job_id)
            return None
        try:
            attempt = ports.ledger.begin(job_id, slot.scheduled_for, kind, now)
        except Exception as exc:
            log.error("job %s not started: the attempt could not be recorded (%s)", job_id, exc)
            self._release(job_id, owner)
            return None
        if attempt is None:
            log.info("job %s: slot %s already ran; not replayed", job_id, slot.scheduled_for)
            self._release(job_id, owner)
            return None
        if slot.lateness != "on_time":
            log.info("job %s dispatched %s (slot %s)", job_id, slot.lateness, slot.scheduled_for)
        return self._start(spec, owner, attempt, kind, slot.scheduled_for, None)

    def _start(
        self,
        spec: JobSpec,
        owner: str,
        attempt: str,
        kind: AttemptKind,
        scheduled_for: datetime | None,
        run_context: str | None,
    ) -> asyncio.Task[RunOutcome]:
        key = (str(self.home), spec.id)
        task = asyncio.ensure_future(
            self._execute(spec, owner, attempt, kind, scheduled_for, run_context)
        )
        with _inflight_lock:
            _INFLIGHT[key] = task
        self._mine.add(task)

        def done(finished: asyncio.Task[RunOutcome]) -> None:
            self._mine.discard(finished)
            with _inflight_lock:
                if _INFLIGHT.get(key) is finished:
                    del _INFLIGHT[key]

        task.add_done_callback(done)
        return task

    # -- one run --------------------------------------------------------------------------------

    def _pool(self) -> asyncio.Semaphore | None:
        if self._limit is None:
            return None
        if self._slots is None:
            self._slots = asyncio.Semaphore(self._limit)
        return self._slots

    async def _execute(
        self,
        spec: JobSpec,
        owner: str,
        attempt: str,
        kind: AttemptKind,
        scheduled_for: datetime | None,
        run_context: str | None,
    ) -> RunOutcome:
        pool = self._pool()
        if pool is None:
            return await self._run(spec, owner, attempt, kind, scheduled_for, run_context)
        async with pool:
            return await self._run(spec, owner, attempt, kind, scheduled_for, run_context)

    async def _run(
        self,
        spec: JobSpec,
        owner: str,
        attempt: str,
        kind: AttemptKind,
        scheduled_for: datetime | None,
        run_context: str | None,
    ) -> RunOutcome:
        ports = self.env.ports
        try:
            ports.ledger.running(attempt, self.clock.now())
        except Exception:
            log.warning("job %s: could not mark the attempt running", spec.id, exc_info=True)
        cancel = asyncio.Event()
        work = asyncio.ensure_future(
            run_job(
                self.env,
                spec,
                kind=kind,
                scheduled_for=scheduled_for,
                run_context=run_context,
                cancel=cancel,
                on_progress=_Progress(self, attempt),
            )
        )
        watch = asyncio.ensure_future(self._keep_claim(spec.id, owner, attempt, cancel, work))
        try:
            outcome = await work
        except asyncio.CancelledError:
            if _being_cancelled():  # this run itself is being stopped (daemon shutdown)
                stopped = RunOutcome(
                    status="failed",
                    message="the run was stopped before it finished (shutdown)",
                    error="cancelled",
                    kind=kind,
                    scheduled_for=scheduled_for,
                )
                await self._close(spec, owner, attempt, stopped, watch)
                raise
            # Only the claim heartbeat cancels the work on its own: the claim was lost.
            outcome = RunOutcome(
                status="failed",
                message="the run was stopped: its claim on the job was lost",
                error="claim lost",
                deliver=True,
                kind=kind,
                scheduled_for=scheduled_for,
            )
        await self._close(spec, owner, attempt, outcome, watch)
        return outcome

    async def _close(
        self,
        spec: JobSpec,
        owner: str,
        attempt: str,
        outcome: RunOutcome,
        watch: asyncio.Task[bool],
    ) -> None:
        watch.cancel()
        with contextlib.suppress(BaseException):
            await watch
        try:
            await settle(self.env, spec, outcome, attempt)
        finally:
            self._release(spec.id, owner)

    async def _keep_claim(
        self,
        job_id: str,
        owner: str,
        attempt: str,
        cancel: asyncio.Event,
        work: asyncio.Future[RunOutcome],
    ) -> bool:
        """Refresh the claim and the attempt every ``HEARTBEAT_S``; cancel ``work`` and return True
        when the claim is lost (taken over, or not refreshed for ``HEARTBEAT_GRACE_S``)."""
        ports = self.env.ports
        last_ok = self.clock.monotonic()
        while not work.done():
            await self.clock.sleep(HEARTBEAT_S)
            if work.done():
                break
            now = self.clock.now()
            try:
                kept = ports.store.edit(
                    job_id, lambda rec, at=now: _refresh_claim(rec, owner=owner, now=at)
                )
            except Exception:
                log.debug("job %s: claim heartbeat failed", job_id, exc_info=True)
                if self.clock.monotonic() - last_ok >= HEARTBEAT_GRACE_S:
                    log.warning(
                        "job %s: claim not refreshed for %.0f s; stopping the run",
                        job_id,
                        HEARTBEAT_GRACE_S,
                    )
                    cancel.set()
                    work.cancel()
                    return True
                continue
            if kept is None:
                return False  # the job removed itself: nothing left to keep fresh
            if not kept:
                log.warning("job %s: another owner took the claim; stopping this run", job_id)
                cancel.set()
                work.cancel()
                return True
            last_ok = self.clock.monotonic()
            with contextlib.suppress(Exception):
                ports.ledger.heartbeat(attempt, now)
        return False

    def _release(self, job_id: str, owner: str) -> None:
        try:
            self.env.ports.store.edit(job_id, lambda rec: _release_claim(rec, owner=owner))
        except Exception:
            log.warning("job %s: could not release its claim", job_id, exc_info=True)

    # -- manual runs ----------------------------------------------------------------------------

    async def run_now(self, ref: str, *, run_context: str | None = None) -> RunOutcome:
        """Run a job now (``indus cron run``), paused or not, global pause or not."""
        ports = self.env.ports
        now = self.clock.now()
        with contextlib.suppress(Exception):
            ports.ledger.reap(now)
        record = ports.store.find(ref)
        if record is None:
            raise JobNotFound(f"no scheduled job matches {ref!r}")
        job_id = spec_of(record).id
        with _inflight_lock:
            if (str(self.home), job_id) in _INFLIGHT:
                raise AlreadyRunning(f"job {job_id} is already running")
        owner = new_owner()
        spec = ports.store.edit(
            job_id, lambda rec: _take_claim(rec, owner=owner, now=now, next_run=None)
        )
        if spec is None:
            raise AlreadyRunning(f"job {job_id} is already running")
        attempt = ports.ledger.begin(job_id, None, "manual", now)
        if attempt is None:
            self._release(job_id, owner)
            raise AlreadyRunning(f"job {job_id} could not be recorded in the ledger")
        task = self._start(spec, owner, attempt, "manual", None, run_context)
        return await task

    # -- the daemon -----------------------------------------------------------------------------

    async def run_daemon(self, stop: asyncio.Event, *, interval: float = TICK_INTERVAL_S) -> None:
        """A pass every ``interval`` seconds until ``stop``; then drain (see ``drain``)."""
        while not stop.is_set():
            try:
                await self.tick(wait=False)
            except Exception:
                log.exception("cron tick failed")
            await self._sleep_or_stop(interval, stop)
        await self.drain()

    async def _sleep_or_stop(self, seconds: float, stop: asyncio.Event) -> None:
        sleeper = asyncio.ensure_future(self.clock.sleep(seconds))
        stopper = asyncio.ensure_future(stop.wait())
        try:
            await asyncio.wait({sleeper, stopper}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in (sleeper, stopper):
                waiter.cancel()
            await asyncio.wait({sleeper, stopper})

    async def drain(self, seconds: float = DRAIN_S) -> None:
        """Wait up to ``seconds`` for this ticker's runs, then cancel what is left."""
        pending = set(self._mine)
        if not pending:
            return
        timer = asyncio.ensure_future(self.clock.sleep(seconds))
        try:
            waiting = asyncio.ensure_future(asyncio.wait(pending))
            await asyncio.wait({timer, waiting}, return_when=asyncio.FIRST_COMPLETED)
            waiting.cancel()
        finally:
            timer.cancel()
        left = {t for t in pending if not t.done()}
        if left:
            log.warning("cancelling %d cron run(s) still in flight at shutdown", len(left))
            for task in left:
                task.cancel()
            await asyncio.wait(left)


def _being_cancelled() -> bool:
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0
