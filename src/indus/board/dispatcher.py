"""The board dispatcher: ready tasks become one-shot worker processes (P24-12).

``Dispatcher.tick()`` does one pass over a board, in this order:

1. book how workers ended (exit codes of the children this process started): a clean exit that
   left the task ``running`` is a protocol violation (the worker never called complete, block or
   review; ``VIOLATION_LIMIT`` in a row block the task); ``EXIT_RATE_LIMITED`` (75: provider rate
   limit or overload) releases the task and cools the assignee's lane for
   ``RATE_LIMIT_COOLDOWN_S`` without counting a failure; ``EXIT_TERMINAL_PROVIDER`` (78: the
   provider refused the credential or model) blocks the task as ``capability`` at once; anything
   else counts a failure (``FAILURE_LIMIT`` in a row block the task);
2. workers that vanished without an exit code we could read (not our children, e.g. after a
   restart) are crashes once their run is older than the crash grace;
3. expired claims (``BoardStore.release_stale_claims``), runtime caps (``max_runtime_s`` plus
   ``TERMINAL_GRACE_S`` for the worker's last board call), scheduled wake-ups, dependency
   promotion, and workers that outlived their closed run by ``REAP_GRACE_S``;
4. spawn: ``ready`` tasks (and ``review`` tasks for their reviewer) by priority, skipping
   unassigned tasks, assignees that are not live profiles and cooled lanes, up to the running cap.
   The cap comes from ``kanban.max_in_progress`` or from memory: one worker per
   ``MEMORY_MB_PER_WORKER``, clamped to ``CONCURRENCY_FLOOR``..``CONCURRENCY_CEILING``.

A worker is ``indus chat --local --oneshot -Q --source kanban -t core,kanban-worker --query-file
<prompt>`` for the assignee's profile, started in the task's workspace with an environment from the
one child-env builder (R02.22): ``INDUS_HOME`` of the assignee, the board pins
(``INDUS_BOARD_TASK``, ``INDUS_BOARD_RUN``, ``INDUS_BOARD_CLAIM``, ``INDUS_BOARD_SLUG``,
``INDUS_BOARD_ROOT``) and no provider key of the dispatching process. It is a one-shot run, so no
background completion can be delivered to it (R02.17). Its output goes to one log per task,
rotated at ``LOG_ROTATE_BYTES`` with ``LOG_BACKUPS`` backup.
"""

from __future__ import annotations

import contextlib
import logging
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from indus.foundation import profiles
from indus.foundation.home import secure_dir
from indus.foundation.scope import Scope, make_scope
from indus.safety.child_env import FORCE_PREFIX, child_env
from indus.state.board import (
    BOARD_ENV,
    CRASH_GRACE_S,
    FAILURE_LIMIT,
    BoardError,
    BoardStore,
    Task,
)

__all__ = [
    "CONCURRENCY_CEILING",
    "CONCURRENCY_FLOOR",
    "EXIT_RATE_LIMITED",
    "EXIT_TERMINAL_PROVIDER",
    "LOG_BACKUPS",
    "LOG_ROTATE_BYTES",
    "MEMORY_MB_PER_WORKER",
    "RATE_LIMIT_COOLDOWN_S",
    "REAP_GRACE_S",
    "TERMINAL_GRACE_S",
    "VIOLATION_LIMIT",
    "WORKER_TOOLSETS",
    "DispatchSettings",
    "Dispatcher",
    "ProcessSpawner",
    "Spawner",
    "TickReport",
    "WorkerHandle",
    "WorkerSpec",
    "derive_concurrency",
    "memory_total_mib",
    "rotate_log",
    "worker_prompt",
]

log = logging.getLogger(__name__)

EXIT_RATE_LIMITED = 75
EXIT_TERMINAL_PROVIDER = 78
VIOLATION_LIMIT = 3
RATE_LIMIT_COOLDOWN_S = 300
TERMINAL_GRACE_S = 30
REAP_GRACE_S = 120
LOG_ROTATE_BYTES = 2 * 1024 * 1024
LOG_BACKUPS = 1
MEMORY_MB_PER_WORKER = 512
CONCURRENCY_FLOOR = 2
CONCURRENCY_CEILING = 8
WORKER_TOOLSETS = ("core", "kanban-worker")
TERMINATE_WAIT_S = 5.0


# -- settings -----------------------------------------------------------------------------------


def derive_concurrency(total_mib: int | None) -> int:
    """One worker per 512 MB of memory, never fewer than 2 nor more than 8 (unknown → 2)."""
    if total_mib is None or total_mib <= 0:
        return CONCURRENCY_FLOOR
    return max(CONCURRENCY_FLOOR, min(total_mib // MEMORY_MB_PER_WORKER, CONCURRENCY_CEILING))


def memory_total_mib(meminfo: Path = Path("/proc/meminfo")) -> int | None:
    """Total memory in MiB from ``/proc/meminfo`` (else ``sysconf``), or None."""
    try:
        for line in meminfo.read_text(encoding="ascii", errors="replace").splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        size = os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError, AttributeError):
        return None
    if pages <= 0 or size <= 0:
        return None
    return int(pages * size // (1024 * 1024))


def _int_setting(view: object, key: str, default: int | None) -> int | None:
    getter = getattr(view, "get", None)
    if getter is None:
        return default
    try:
        value = getter(key)
    except Exception:
        return default
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class DispatchSettings:
    max_in_progress: int | None = None  # None: derived from memory
    failure_limit: int = FAILURE_LIMIT
    violation_limit: int = VIOLATION_LIMIT
    rate_limit_cooldown_s: int = RATE_LIMIT_COOLDOWN_S
    terminal_grace_s: int = TERMINAL_GRACE_S
    reap_grace_s: int = REAP_GRACE_S
    crash_grace_s: int = CRASH_GRACE_S
    log_rotate_bytes: int = LOG_ROTATE_BYTES
    log_backups: int = LOG_BACKUPS

    @classmethod
    def from_config(cls, view: object) -> DispatchSettings:
        """Read ``kanban.*`` settings (unset or invalid values keep the defaults)."""
        cap = _int_setting(view, "kanban.max_in_progress", None)
        failures = _int_setting(view, "kanban.failure_limit", FAILURE_LIMIT) or FAILURE_LIMIT
        return cls(
            max_in_progress=cap if cap and cap > 0 else None,
            failure_limit=max(1, failures),
        )


# -- workers ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkerSpec:
    task_id: str
    run_id: int
    profile: str
    argv: tuple[str, ...]
    env: Mapping[str, str]
    cwd: Path
    log_path: Path


@dataclass(frozen=True)
class WorkerHandle:
    pid: int
    fingerprint: str | None


class Spawner(Protocol):
    def spawn(self, spec: WorkerSpec) -> WorkerHandle: ...

    def exited(self) -> dict[int, int]:
        """Exit codes of this spawner's children that ended since the last call (pid → code;
        a signal ``n`` reads as ``128 + n``)."""
        ...

    def alive(self, pid: int, fingerprint: str | None) -> bool: ...

    def terminate(self, pid: int, fingerprint: str | None) -> bool:
        """Stop the process; True when it is gone afterwards."""
        ...


def _fingerprint(pid: int) -> str | None:
    """Boot id + start ticks of ``pid`` on Linux; None elsewhere or when unreadable."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError:
        return None
    fields = raw.rpartition(")")[2].split()
    return f"{boot}:{fields[19]}" if len(fields) >= 20 else None


class ProcessSpawner:
    """Real worker processes. Children are kept so their exit codes can be read."""

    def __init__(self, *, wait_s: float = TERMINATE_WAIT_S) -> None:
        self._children: dict[int, subprocess.Popen[bytes]] = {}
        self._lock = threading.Lock()
        self._wait_s = wait_s

    def spawn(self, spec: WorkerSpec) -> WorkerHandle:
        secure_dir(spec.log_path.parent)
        with spec.log_path.open("ab") as out:
            proc = subprocess.Popen(  # noqa: S603 - argv is ours, never a shell string
                list(spec.argv),
                cwd=str(spec.cwd) if spec.cwd.is_dir() else None,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                env=dict(spec.env),
                start_new_session=True,
            )
        with self._lock:
            self._children[proc.pid] = proc
        return WorkerHandle(proc.pid, _fingerprint(proc.pid))

    def exited(self) -> dict[int, int]:
        done: dict[int, int] = {}
        with self._lock:
            for pid, proc in list(self._children.items()):
                code = proc.poll()
                if code is None:
                    continue
                done[pid] = 128 - code if code < 0 else code
                del self._children[pid]
        return done

    def alive(self, pid: int, fingerprint: str | None) -> bool:
        with self._lock:
            proc = self._children.get(pid)
        if proc is not None:
            return proc.poll() is None
        if not Path(f"/proc/{pid}").exists() and sys.platform.startswith("linux"):
            return False
        if fingerprint is not None and sys.platform.startswith("linux"):
            return _fingerprint(pid) == fingerprint
        if sys.platform == "win32":  # os.kill(pid, 0) would terminate the process there
            from indus.platform import winproc

            return winproc.pid_alive(pid)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return True
        return True

    def terminate(self, pid: int, fingerprint: str | None) -> bool:
        if not self.alive(pid, fingerprint):
            return True
        with self._lock:
            proc = self._children.get(pid)
        for sig in (signal.SIGTERM, signal.SIGKILL):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                if proc is not None:
                    proc.send_signal(sig)
                else:
                    os.kill(pid, sig)
            if proc is not None:
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=self._wait_s)
            if not self.alive(pid, fingerprint):
                return True
        return not self.alive(pid, fingerprint)

    def wait(self, pid: int, timeout: float) -> int | None:
        """Wait up to ``timeout`` s for a child of this spawner; its exit code, or None."""
        with self._lock:
            proc = self._children.get(pid)
        if proc is None:
            return None
        try:
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def stop_all(self) -> None:
        """Stop every child still running (shutdown)."""
        with self._lock:
            pids = list(self._children)
        for pid in pids:
            self.terminate(pid, None)


def rotate_log(
    path: Path, *, max_bytes: int = LOG_ROTATE_BYTES, backups: int = LOG_BACKUPS
) -> bool:
    """Move ``path`` aside when it is larger than ``max_bytes`` (keeping ``backups`` old files)."""
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size <= max_bytes:
        return False
    if backups <= 0:
        path.unlink(missing_ok=True)
        return True
    for n in range(backups, 0, -1):
        older = path.with_name(f"{path.name}.{n}")
        if n == backups:
            older.unlink(missing_ok=True)
        newer = path if n == 1 else path.with_name(f"{path.name}.{n - 1}")
        if newer.exists():
            newer.replace(older)
    return True


def worker_prompt(store: BoardStore, task: Task) -> str:
    """What a worker reads about its task: the card, its parents' handoffs and the comments."""
    lines = [
        f"You are working on task {task.id} of the board '{store.slug}': {task.title}",
        "",
    ]
    if task.body:
        lines += [task.body.strip(), ""]
    for parent in store.parents(task.id):
        run = store.latest_run(parent)
        if run is not None and run.summary:
            lines.append(f"Handoff from parent {parent}: {run.summary.strip()[:4000]}")
    attachments = store.attachments(task.id)
    if attachments:
        lines.append("Attached files: " + ", ".join(a.stored_path for a in attachments))
    previous = [r for r in store.runs(task.id) if r.ended_at is not None][-3:]
    for run in previous:
        note = run.summary or run.error or ""
        if note:
            lines.append(f"Earlier attempt ({run.outcome}): {note.strip()[:2000]}")
    for comment in store.comments(task.id)[-20:]:
        lines.append(f"Comment from {comment.author}: {comment.body.strip()[:2000]}")
    lines += [
        "",
        "When you are finished, call kanban_complete with a summary of what you did. If you need "
        "a person or something you cannot get, call kanban_block with the reason. If the work "
        "should be checked first, call kanban_request_review. Call kanban_heartbeat during long "
        "steps. Ending without one of these calls counts against the task.",
    ]
    return "\n".join(lines).strip() + "\n"


# -- the tick -----------------------------------------------------------------------------------


@dataclass
class TickReport:
    spawned: list[tuple[str, str]] = field(default_factory=lambda: [])
    rate_limited: list[str] = field(default_factory=lambda: [])
    crashed: list[str] = field(default_factory=lambda: [])
    violations: list[str] = field(default_factory=lambda: [])
    blocked: list[str] = field(default_factory=lambda: [])
    reclaimed: list[str] = field(default_factory=lambda: [])
    timed_out: list[str] = field(default_factory=lambda: [])
    reaped: list[str] = field(default_factory=lambda: [])
    woke: list[str] = field(default_factory=lambda: [])
    promoted: int = 0
    skipped_unassigned: list[str] = field(default_factory=lambda: [])
    skipped_unknown_profile: list[str] = field(default_factory=lambda: [])
    skipped_cooled: list[str] = field(default_factory=lambda: [])
    capacity: int = 0
    errors: list[str] = field(default_factory=lambda: [])

    def as_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    @property
    def idle(self) -> bool:
        return not any(
            getattr(self, name)
            for name in self.__dataclass_fields__
            if name not in ("capacity", "skipped_unassigned", "skipped_unknown_profile")
        )


def _claimer() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(4)}"


class Dispatcher:
    """One board's dispatcher. ``spawner`` defaults to real processes."""

    def __init__(
        self,
        store: BoardStore,
        *,
        scope: Scope,
        spawner: Spawner | None = None,
        settings: DispatchSettings | None = None,
        memory_mib: Callable[[], int | None] = memory_total_mib,
        python: str | None = None,
    ) -> None:
        self.store = store
        self.scope = scope
        self.spawner: Spawner = spawner if spawner is not None else ProcessSpawner()
        self.settings = settings if settings is not None else DispatchSettings()
        self._memory_mib = memory_mib
        self._python = python or sys.executable
        self._lock = threading.Lock()

    # -- capacity -----------------------------------------------------------------------------

    def concurrency(self) -> int:
        configured = self.settings.max_in_progress
        if configured is not None and configured > 0:
            return configured
        return derive_concurrency(self._memory_mib())

    # -- one pass -----------------------------------------------------------------------------

    def tick(self, *, dry_run: bool = False) -> TickReport:
        with self._lock:
            report = TickReport()
            if not dry_run:
                self._book_exits(report)
                self._book_vanished(report)
                report.reclaimed = self.store.release_stale_claims(
                    alive=self.spawner.alive,
                    terminate=self.spawner.terminate,
                    failure_limit=self.settings.failure_limit,
                )
                self._enforce_runtime(report)
                report.woke = self.store.wake_scheduled()
                report.promoted = self.store.recompute_ready()
                self._reap(report)
            self._spawn(report, dry_run=dry_run)
            return report

    def _book_exits(self, report: TickReport) -> None:
        exited = self.spawner.exited()
        if not exited:
            return
        for task in self.store.list(status="running"):
            if task.worker_pid is None or task.worker_pid not in exited:
                continue
            assert task.current_run_id is not None
            self._book(task, exited[task.worker_pid], report)

    def _book(self, task: Task, code: int, report: TickReport) -> None:
        run_id = task.current_run_id
        assert run_id is not None
        pid = task.worker_pid
        settings = self.settings
        if code == 0:
            error = (
                f"the worker (pid {pid}) exited cleanly without kanban_complete, kanban_block or "
                "kanban_request_review: protocol violation"
            )
            blocked = self.store.record_violation(
                task.id, run_id, limit=settings.violation_limit, error=error
            )
            report.violations.append(task.id)
            if blocked:
                report.blocked.append(task.id)
            return
        if code == EXIT_RATE_LIMITED:
            if self.store.release_run(
                task.id,
                run_id,
                outcome="rate_limited",
                error=f"the worker (pid {pid}) hit a provider rate limit; requeued",
                event_payload={"pid": pid, "exit_code": code},
            ):
                if task.assignee:
                    self.store.cool_lane(
                        task.assignee, settings.rate_limit_cooldown_s, "provider rate limit"
                    )
                report.rate_limited.append(task.id)
            return
        if code == EXIT_TERMINAL_PROVIDER:
            with contextlib.suppress(BoardError):
                self.store.block(
                    task.id,
                    reason=(
                        f"the provider refused this profile's credential or model (worker exit "
                        f"{code}); fix the configuration, then unblock"
                    ),
                    kind="capability",
                    expected_run_id=run_id,
                )
                report.blocked.append(task.id)
            return
        error = f"the worker (pid {pid}) exited with code {code}"
        self._crash(task, run_id, error, {"pid": pid, "exit_code": code}, report)

    def _crash(
        self,
        task: Task,
        run_id: int,
        error: str,
        payload: Mapping[str, Any],
        report: TickReport,
        *,
        outcome: str = "crashed",
    ) -> None:
        if not self.store.release_run(
            task.id, run_id, outcome=outcome, error=error, event_payload=payload
        ):
            return
        (report.timed_out if outcome == "timed_out" else report.crashed).append(task.id)
        if self.store.record_failure(task.id, error, limit=self.settings.failure_limit):
            report.blocked.append(task.id)

    def _book_vanished(self, report: TickReport) -> None:
        now = self.store.now()
        for task in self.store.list(status="running"):
            if task.worker_pid is None or task.current_run_id is None:
                continue
            run = self.store.run(task.current_run_id)
            if run is None or now - run.started_at < self.settings.crash_grace_s:
                continue
            if self.spawner.alive(task.worker_pid, task.worker_fingerprint):
                continue
            self._crash(
                task,
                task.current_run_id,
                f"the worker (pid {task.worker_pid}) is gone and left no exit code",
                {"pid": task.worker_pid},
                report,
            )

    def _enforce_runtime(self, report: TickReport) -> None:
        now = self.store.now()
        for task in self.store.list(status="running"):
            if not task.max_runtime_s or task.current_run_id is None:
                continue
            run = self.store.run(task.current_run_id)
            if (
                run is None
                or now - run.started_at <= task.max_runtime_s + self.settings.terminal_grace_s
            ):
                continue
            if task.worker_pid is not None:
                self.spawner.terminate(task.worker_pid, task.worker_fingerprint)
            self._crash(
                task,
                task.current_run_id,
                f"the worker ran past its {task.max_runtime_s} s limit",
                {"pid": task.worker_pid, "limit_s": task.max_runtime_s},
                report,
                outcome="timed_out",
            )

    def _reap(self, report: TickReport) -> None:
        cutoff = self.store.now() - self.settings.reap_grace_s
        for run in self.store.finished_runs_with_workers(cutoff):
            assert run.worker_pid is not None
            if self.spawner.alive(run.worker_pid, run.worker_fingerprint):
                self.spawner.terminate(run.worker_pid, run.worker_fingerprint)
                report.reaped.append(run.task_id)
            self.store.forget_worker(run.id)

    # -- spawning -----------------------------------------------------------------------------

    def _candidates(self) -> list[Task]:
        ready = self.store.list(status="ready")
        review = [t for t in self.store.list(status="review") if t.assignee]
        merged = ready + review
        merged.sort(key=lambda t: (-t.priority, t.created_at, t.id))
        return merged

    def _spawn(self, report: TickReport, *, dry_run: bool) -> None:
        cap = self.concurrency()
        free = cap - self.store.count_running()
        report.capacity = max(free, 0)
        cooled = self.store.cooled_lanes()
        for task in self._candidates():
            if not task.assignee:
                report.skipped_unassigned.append(task.id)
                continue
            if not profiles.is_live(self.store.root, task.assignee):
                report.skipped_unknown_profile.append(task.id)
                continue
            if task.assignee in cooled:
                report.skipped_cooled.append(task.id)
                continue
            if free <= 0:
                continue  # keep going: the report still names what cannot run at all
            if dry_run:
                report.spawned.append((task.id, task.assignee))
                free -= 1
                continue
            if self._start(task, report):
                free -= 1

    def _start(self, task: Task, report: TickReport) -> bool:
        claimer = _claimer()
        if task.status == "review":
            claimed = self.store.claim_review(task.id, claimer)
        else:
            claimed = self.store.claim(task.id, claimer)
        if claimed is None or claimed.current_run_id is None or claimed.assignee is None:
            return False
        run_id = claimed.current_run_id
        try:
            spec = self.build_spec(claimed, run_id, claimer)
            rotate_log(
                spec.log_path,
                max_bytes=self.settings.log_rotate_bytes,
                backups=self.settings.log_backups,
            )
            handle = self.spawner.spawn(spec)
        except Exception as exc:  # spawn failure: book it, keep the tick going
            error = f"the worker could not be started: {type(exc).__name__}: {exc}"
            log.warning("board %s task %s: %s", self.store.slug, task.id, error)
            report.errors.append(f"{task.id}: {error}")
            released = self.store.release_run(task.id, run_id, outcome="spawn_failed", error=error)
            if released and self.store.record_failure(
                task.id, error, limit=self.settings.failure_limit
            ):
                report.blocked.append(task.id)
            return False
        self.store.set_worker(task.id, run_id, handle.pid, handle.fingerprint)
        report.spawned.append((task.id, claimed.assignee))
        return True

    def workspace_for(self, task: Task) -> Path:
        """The folder a worker runs in (made for scratch tasks)."""
        if task.workspace_kind == "dir":
            folder = Path(task.workspace_path or "")
            if not task.workspace_path or not folder.is_dir():
                raise BoardError(f"the workspace folder {task.workspace_path!r} does not exist")
            return folder
        if task.workspace_kind == "worktree":
            from indus.backends import worktrees

            repo = task.workspace_path or ""
            if not repo:
                raise BoardError(
                    "a worktree task needs the repository folder as its workspace path"
                )
            info = worktrees.create(self.scope, repo, f"board-{task.id}")
            return Path(info.path)
        folder = self.store.workspaces_dir() / task.id
        secure_dir(folder, env=self.scope.env)
        return folder

    def worker_scope(self, profile: str) -> Scope:
        home = profiles.profile_home(self.store.root, profile)
        return make_scope(home, env=self.scope.env, clock=self.scope.clock)

    def build_spec(self, task: Task, run_id: int, claimer: str) -> WorkerSpec:
        assert task.assignee is not None
        cwd = self.workspace_for(task)
        logs = self.store.logs_dir()
        secure_dir(logs, env=self.scope.env)
        prompt_path = logs / f"{task.id}.prompt.md"
        prompt_path.write_text(worker_prompt(self.store, task), encoding="utf-8")
        pins = {
            BOARD_ENV["task"]: task.id,
            BOARD_ENV["run"]: str(run_id),
            BOARD_ENV["claim"]: claimer,
            BOARD_ENV["slug"]: self.store.slug,
            BOARD_ENV["root"]: str(self.store.root),
        }
        extra = {FORCE_PREFIX + name: value for name, value in pins.items()}
        env = child_env(self.worker_scope(task.assignee), extra=extra)
        argv = (
            self._python,
            "-m",
            "indus",
            "chat",
            "--local",
            "--oneshot",
            "-Q",
            "--source",
            "kanban",
            "-t",
            ",".join(WORKER_TOOLSETS),
            "--query-file",
            str(prompt_path),
        )
        return WorkerSpec(
            task_id=task.id,
            run_id=run_id,
            profile=task.assignee,
            argv=argv,
            env=env,
            cwd=cwd,
            log_path=logs / f"{task.id}.log",
        )
