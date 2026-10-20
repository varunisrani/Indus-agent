"""A real gateway daemon in a temp profile, for tests that attach to it (P17-15, P17-26, P17-27).

``ProfileHome`` lays out a profile under a test's temp dir: ``home`` (``INDUS_HOME``, mode 0700),
``user-home`` (``HOME``), ``config`` (``XDG_CONFIG_HOME``) and ``work`` (the working folder), with a
``config.yaml`` that points a ``custom`` provider at the loopback fake model
(``tests/fakes/model/http_serve.py``). No messaging platform is enabled: the daemon keeps running
for its background parts and its session host. ``gateway.idle_exit_seconds`` is 0 unless a test
asks for a window (``idle_exit_seconds=``): a daemon a client's ``ensure`` starts in these
profiles never idles out mid-test (P17-23).

- ``env()`` is the environment every child gets: no inherited ``INDUS_*`` variable, temp ``HOME``
  and config folder, loopback without a proxy.
- ``indus(*args)`` runs the real CLI once (``python -m indus``) and returns the finished process.
- ``start_daemon()`` runs ``indus gateway run`` as a child; ``RunningDaemon.attachable()`` waits
  until it answers ``describe`` with a mounted WebSocket endpoint.
- ``grant()`` is the short-lived ticket helper: a fresh single-use ticket for one dial, the way
  ``indus gateway grant`` gets one (never printed, never in a URL).
- ``stop_all()`` stops whatever serves the home (a child or a daemon a CLI started detached) and
  any process still naming the home; ``stray_processes()`` lists those.

Only pids that carry this home in their environment are ever signalled, so other tests' and other
worktrees' daemons are never touched.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from indus.service import gateway_control
from indus.service.remote.channel import Grant, grant_for

ROOT = Path(__file__).resolve().parents[2]
TIMEOUT = 90.0

_TICK = threading.Event()  # never set: a wall-clock pause between polls


def pause(seconds: float) -> None:
    """Wait ``seconds`` of wall-clock time (the child processes run on real time)."""
    _TICK.wait(seconds)


def until[T](check: Callable[[], T], what: str, limit: float = TIMEOUT) -> T:
    """Poll ``check`` until it returns something truthy; fail naming ``what`` after ``limit``."""
    end = time.monotonic() + limit
    while True:
        found = check()
        if found:
            return found
        if time.monotonic() > end:
            raise AssertionError(f"timed out waiting for {what}")
        pause(0.05)


def model_config(port: int, extra: str = "", idle_exit_seconds: float | None = 0.0) -> str:
    idle = (
        "" if idle_exit_seconds is None else f"gateway:\n  idle_exit_seconds: {idle_exit_seconds}\n"
    )
    return (
        "model:\n"
        "  default: fake-model\n"
        "  provider: custom\n"
        f"  base_url: http://127.0.0.1:{port}/v1\n" + idle + extra
    )


@dataclass
class ProfileHome:
    """A temp profile home whose model is the loopback fake on ``model_port``."""

    root: Path
    model_port: int
    config_extra: str = ""
    env_extra: dict[str, str] = field(default_factory=dict[str, str])
    idle_exit_seconds: float | None = 0.0
    """``gateway.idle_exit_seconds`` (0: an auto-started daemon never idles out; None: unset)."""

    def __post_init__(self) -> None:
        self.home = self.root / "home"
        self.user_home = self.root / "user-home"
        self.work = self.root / "work"
        for folder in (self.home, self.user_home, self.work):
            folder.mkdir(parents=True, exist_ok=True)
        self.home.chmod(0o700)  # the control socket client refuses a home others can write
        (self.home / "config.yaml").write_text(
            model_config(self.model_port, self.config_extra, self.idle_exit_seconds),
            encoding="utf-8",
        )
        self.daemons: list[RunningDaemon] = []

    def env(self, **extra: str) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("INDUS_")}
        for name in ("NOTIFY_SOCKET", "WATCHDOG_USEC", "TELEGRAM_BOT_TOKEN", "NODE_OPTIONS"):
            env.pop(name, None)
        env.update(
            {
                "INDUS_HOME": str(self.home),
                "HOME": str(self.user_home),
                "XDG_CONFIG_HOME": str(self.root / "config"),
                "INDUS_GATEWAY_LOCK_DIR": str(self.root / "token-locks"),
                "NO_PROXY": "127.0.0.1,localhost",
                "no_proxy": "127.0.0.1,localhost",
                "PYTHONPATH": str(ROOT),
                "PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin",
                **self.env_extra,
                **extra,
            }
        )
        return env

    def argv(self, *args: str) -> list[str]:
        return [sys.executable, "-m", "indus", *args]

    def indus(
        self,
        *args: str,
        stdin: str = "",
        timeout: float = TIMEOUT,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """One real CLI run in the work folder."""
        return subprocess.run(
            self.argv(*args),
            capture_output=True,
            text=True,
            input=stdin,
            timeout=timeout,
            cwd=self.work,
            env=self.env(**(env or {})),
            check=False,
        )

    def spawn(self, *args: str, env: Mapping[str, str] | None = None) -> subprocess.Popen[str]:
        """A CLI run left running (pipes for stdin/stdout/stderr); the caller ends it."""
        return subprocess.Popen(
            self.argv(*args),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=self.work,
            env=self.env(**(env or {})),
        )

    def start_daemon(self, *args: str, **extra: str) -> RunningDaemon:
        log = (self.root / f"daemon-{time.monotonic_ns()}.err").open("w", encoding="utf-8")
        try:
            proc = subprocess.Popen(
                self.argv("gateway", "run", *args),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                cwd=self.work,
                env=self.env(**extra),
            )
        finally:
            log.close()
        daemon = RunningDaemon(self, proc, Path(log.name))
        self.daemons.append(daemon)
        return daemon

    def grant(self, timeout: float = 10.0) -> Grant:
        """A fresh single-use ticket for one dial (through the owner-only control socket)."""
        return grant_for(self.home, timeout=timeout)

    def described(self, timeout: float = 2.0) -> dict[str, Any] | None:
        return gateway_control.describe(self.home, timeout=timeout)

    def logs(self) -> str:
        folder = self.home / "logs"
        if not folder.exists():
            return ""
        return "\n".join(p.read_text(errors="replace") for p in sorted(folder.glob("*.log")))

    def stray_processes(self) -> list[int]:
        """Pids (other than ours) whose environment names this home."""
        marker = f"INDUS_HOME={self.home}".encode()
        found: list[int] = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit() or int(entry.name) == os.getpid():
                continue
            try:
                environ = (entry / "environ").read_bytes()
            except OSError:
                continue
            if marker in environ.split(b"\0"):
                found.append(int(entry.name))
        return found

    def stop_all(self, grace: float = 30.0) -> None:
        """Stop every daemon of this home — our children and one a CLI started detached — and
        any other process naming the home: SIGTERM, then SIGKILL after ``grace`` seconds."""

        def targets() -> set[int]:
            children = {d.proc.pid for d in self.daemons if d.proc.poll() is None}
            return children | set(self.stray_processes())

        pids = targets()
        for pid in pids:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGTERM)
        end = time.monotonic() + grace
        while pids and time.monotonic() < end:
            pause(0.1)
            pids = targets()
        for pid in targets():
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
        for daemon in self.daemons:
            with contextlib.suppress(subprocess.TimeoutExpired):
                daemon.proc.wait(10)


@dataclass
class RunningDaemon:
    """One ``indus gateway run`` child of the test."""

    profile: ProfileHome
    proc: subprocess.Popen[bytes]
    log_path: Path

    def log(self) -> str:
        text = self.log_path.read_text(encoding="utf-8", errors="replace")
        gateway_log = self.profile.home / "logs" / "gateway.log"
        if gateway_log.exists():
            text += "\n--- gateway.log ---\n" + gateway_log.read_text(errors="replace")[-8000:]
        return text

    def attachable(self, limit: float = TIMEOUT) -> dict[str, Any]:
        """Wait until the daemon answers ``describe`` with its WebSocket endpoint mounted."""
        end = time.monotonic() + limit
        while True:
            found = self.profile.described()
            if found is not None and found.get("ws_url"):
                answering = gateway_control.identify(self.profile.home, timeout=2.0)
                if answering is not None and int(answering["pid"]) == self.proc.pid:
                    return found
            if self.proc.poll() is not None:
                raise AssertionError(f"the daemon exited ({self.proc.returncode}):\n{self.log()}")
            if time.monotonic() > end:
                raise AssertionError(f"the daemon never mounted its endpoint:\n{self.log()}")
            pause(0.1)

    def stop(self, limit: float = 60.0) -> int:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
        try:
            return self.proc.wait(limit)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(10)
            raise AssertionError(
                f"the daemon did not stop within {limit} s:\n{self.log()}"
            ) from None

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(10)
