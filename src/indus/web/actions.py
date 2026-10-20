"""Background actions the dashboard starts and the System page tails (P18-15, P18-16).

An action is a long job with text output: a child ``indus`` command (doctor, backup, import,
checkpoint prune, …) or a job run in a dashboard thread (daemon start/stop/restart, the git update,
a profile export). Each one belongs to a profile and has a name; at most one run per (profile, name)
is live, a second start is refused (``ActionBusy``) until it ends.

Output goes to ``<home>/logs/dashboard-actions/<name>.log`` (owner-only, truncated at each start,
created only inside a live profile home so a deleted profile is never recreated). ``status`` reads
the last lines back with every known secret masked (``foundation.redact``), plus whether the run
is still going and how it ended: ``running``, ``done`` (exit 0) or ``failed``.

Child commands run ``python -m indus`` with the profile's own inherited environment and
``INDUS_HOME`` pinned to its home; every ``INDUS_DASHBOARD_*`` variable (session token, drain and
signing secrets) is removed first. The command prefix is a seam (``DashboardState.extras
["actions.command"]``) so tests can run a stand-in script.
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal, cast

from indus.foundation.home import HOME_ENV
from indus.foundation.profiles import mkdir_in_profile
from indus.foundation.redact import redact_text
from indus.foundation.scope import Scope

__all__ = [
    "LOG_DIR",
    "MAX_TAIL_LINES",
    "ActionBusy",
    "ActionRun",
    "Actions",
    "Job",
    "Say",
    "actions_of",
    "child_env",
]

log = logging.getLogger(__name__)

LOG_DIR: Final = Path("logs") / "dashboard-actions"
MAX_TAIL_LINES: Final = 2000
_TAIL_BYTES: Final = 512 * 1024
_EXTRAS_KEY: Final = "actions"
_COMMAND_KEY: Final = "actions.command"
_DASHBOARD_PREFIX: Final = "INDUS_DASHBOARD_"

type Say = Callable[[str], None]
type Job = Callable[[Say], int]
"""An in-process job: writes lines with ``say`` and returns its exit code (0 = done)."""
type State = Literal["running", "done", "failed"]


class ActionBusy(RuntimeError):
    """The action already runs for this profile."""

    def __init__(self, name: str) -> None:
        super().__init__(f"{name} is already running")
        self.name = name


def default_command(args: Sequence[str]) -> list[str]:
    return [sys.executable, "-m", "indus", *args]


def child_env(scope: Scope) -> dict[str, str]:
    """The environment a child ``indus`` gets: the profile's inherited env, its home pinned, no
    dashboard credentials."""
    env = {k: v for k, v in scope.env.base.items() if not k.startswith(_DASHBOARD_PREFIX)}
    env[HOME_ENV] = str(scope.home)
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


@dataclass
class ActionRun:
    name: str
    home: Path
    log_path: Path
    started_at: float
    process: subprocess.Popen[bytes] | None = None
    thread: threading.Thread | None = None
    exit_code: int | None = None
    secrets: tuple[str, ...] = field(default=(), repr=False)

    def running(self) -> bool:
        if self.process is not None:
            code = self.process.poll()
            if code is None:
                return True
            self.exit_code = code
            return False
        return self.thread is not None and self.thread.is_alive()

    def state(self) -> State:
        if self.running():
            return "running"
        return "done" if self.exit_code == 0 else "failed"


def _stamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _tail(path: Path, lines: int) -> list[str]:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            start = max(0, size - _TAIL_BYTES)
            handle.seek(start)
            data = handle.read()
    except OSError:
        return []
    text = data.decode("utf-8", errors="replace").splitlines()
    if start > 0 and text:
        text = text[1:]  # the first line was cut by the byte window
    return text[-lines:] if lines > 0 else []


class Actions:
    """The action registry of one dashboard app."""

    def __init__(self, *, command: Callable[[Sequence[str]], list[str]] = default_command) -> None:
        self.command = command
        self._runs: dict[tuple[str, str], ActionRun] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(home: Path, name: str) -> tuple[str, str]:
        return (str(home), name)

    def _claim(self, scope: Scope, name: str) -> ActionRun:
        key = self._key(scope.home, name)
        with self._lock:
            current = self._runs.get(key)
            if current is not None and current.running():
                raise ActionBusy(name)
            folder = mkdir_in_profile(scope.home, LOG_DIR)
            path = folder / f"{name}.log"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as out:
                out.write(f"=== {name} started {_stamp()} ===\n".encode())
            run = ActionRun(name, scope.home, path, time.time(), secrets=_known_secrets(scope))
            self._runs[key] = run
            return run

    def start_command(self, scope: Scope, name: str, args: Sequence[str]) -> ActionRun:
        """Run ``indus <args>`` for ``scope`` in the background (``ActionBusy`` when running)."""
        run = self._claim(scope, name)
        argv = self.command(list(args))
        try:
            with run.log_path.open("ab") as out:
                run.process = subprocess.Popen(  # noqa: S603 - our own CLI, fixed argv
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=subprocess.STDOUT,
                    env=child_env(scope),
                    cwd=str(scope.home),
                    start_new_session=True,
                )
        except OSError as exc:
            self._append(run, f"could not start: {exc}")
            run.exit_code = 127
            run.process = None
        return run

    def start_job(self, scope: Scope, name: str, job: Job) -> ActionRun:
        """Run ``job`` in a dashboard thread (``ActionBusy`` when running)."""
        run = self._claim(scope, name)

        def say(line: str) -> None:
            self._append(run, line)

        def body() -> None:
            code = 1
            try:
                code = job(say)
            except Exception as exc:  # a job's failure is its own result, never the server's
                log.warning("dashboard action %s failed: %s", name, type(exc).__name__)
                say(f"failed: {exc}")
            finally:
                run.exit_code = code
                say(f"=== {name} {'done' if code == 0 else 'failed'} ({code}) ===")

        run.thread = threading.Thread(target=body, name=f"indus-action-{name}", daemon=True)
        run.thread.start()
        return run

    def _append(self, run: ActionRun, line: str) -> None:
        with contextlib.suppress(OSError), run.log_path.open("ab") as out:
            out.write((line.rstrip("\n") + "\n").encode("utf-8", errors="replace"))

    def find(self, scope: Scope, name: str) -> ActionRun | None:
        with self._lock:
            return self._runs.get(self._key(scope.home, name))

    def status(self, scope: Scope, name: str, lines: int = 200) -> dict[str, Any]:
        """The run's state and the last ``lines`` lines of its log (masked)."""
        wanted = min(max(int(lines), 1), MAX_TAIL_LINES)
        run = self.find(scope, name)
        path = scope.home / LOG_DIR / f"{name}.log"
        if run is None:
            tail = _tail(path, wanted) if path.is_file() else []
            return {
                "name": name,
                "state": "idle" if not tail else "unknown",
                "running": False,
                "exit_code": None,
                "lines": _mask(tail, _known_secrets(scope)),
            }
        state = run.state()
        return {
            "name": name,
            "state": state,
            "running": state == "running",
            "exit_code": run.exit_code,
            "started_at": run.started_at,
            "lines": _mask(_tail(run.log_path, wanted), run.secrets),
        }


def _known_secrets(scope: Scope) -> tuple[str, ...]:
    store = scope.secrets
    values = getattr(store, "known_values", None)
    if not callable(values):
        return ()
    try:
        found = cast(list[object], values())
    except Exception:
        return ()
    return tuple(v for v in found if isinstance(v, str) and len(v) >= 6)


def _mask(lines: list[str], secrets: tuple[str, ...]) -> list[str]:
    return [redact_text(line, secrets) for line in lines]


def actions_of(extras: Mapping[str, Any] | dict[str, Any]) -> Actions:
    """The app's registry, made on first use (``DashboardState.extras``)."""
    found = extras.get(_EXTRAS_KEY)
    if isinstance(found, Actions):
        return found
    command = extras.get(_COMMAND_KEY)
    made = (
        Actions(command=cast(Callable[[Sequence[str]], list[str]], command))
        if callable(command)
        else Actions()
    )
    if isinstance(extras, dict):
        found = extras.setdefault(_EXTRAS_KEY, made)
        if isinstance(found, Actions):
            return found
    return made
