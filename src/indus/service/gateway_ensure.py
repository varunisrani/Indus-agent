"""Find the profile's gateway daemon, or start one, before a client attaches (P17-15).

``ensure(home, env, mode=...)`` answers with an ``Ensured`` verdict and, when a daemon is ready,
its credential-free ``Endpoint`` (``ws_url``, ``profile_id``, ``instance_id``, ``code_version``).
A daemon is alive only when its control socket answers ``identify`` for this home (R06.18): a pid
record or a pid that exists proves nothing.

Modes:

- ``start``: whatever is needed, also a daemon a person stopped on purpose (``indus gateway
  ensure``, a client's first attach);
- ``discover``: never start or restart anything (``--no-start``, a plain reconnect);
- ``recover``: a client's automatic re-ensure after it lost the daemon. A daemon that died or
  idled out is started again, one that was stopped on purpose (the planned-stop label P15-28
  writes) is not, and at most ``RECOVER_LIMIT`` recoveries may happen per ``RECOVER_WINDOW_S``
  (the caller's ``RecoverBudget``); past it the verdict is ``refused/recover_budget``.

In ``start`` mode a daemon whose ``code_version`` differs from ``version`` (the caller's own,
``indus.__version__`` by default) is asked to restart gracefully (SIGUSR1: it refuses new turns and
lets running ones finish), once per call; a fresh one that still differs is accepted with ``skew``
set. The other modes only report the skew.

A start goes through the user's installed service when the caller passes one (it is never
installed here), else it is a detached ``indus gateway run``: double-forked through a tiny stdlib
launcher so the daemon is nobody's child, in its own session, stdin from ``/dev/null``, output
appended to ``<home>/logs/gateway-stdio.log``, launch-option variables removed (they belong to a
session, P17-09) and ``AUTOSTART_ENV`` set so the daemon knows ``ensure`` started it (P17-23's
idle exit). This module lives in ``service`` because the CLI and the attach channel both call it
and neither may import the gateway package.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal, Protocol

from indus.foundation.redact import redact_text
from indus.service import gateway_control
from indus.service.creation_policy import scrub_launch_env
from indus.service.gateway_units import default_program
from indus.service.ws_auth import stable_profile_id
from indus.state import gateway_runtime

__all__ = [
    "AUTOSTART_ENV",
    "DEFAULT_TIMEOUT_S",
    "RECOVER_LIMIT",
    "RECOVER_WINDOW_S",
    "Endpoint",
    "Ensured",
    "Mode",
    "RecoverBudget",
    "Starter",
    "daemon_argv",
    "ensure",
    "planned_stop_reason",
]

log = logging.getLogger(__name__)

type Mode = Literal["start", "discover", "recover"]
type State = Literal["ready", "absent", "stopped", "starting", "exited", "refused", "failed"]

DEFAULT_TIMEOUT_S: Final = 60.0
RECOVER_LIMIT: Final = 3
RECOVER_WINDOW_S: Final = 60.0
AUTOSTART_ENV: Final = "INDUS_GATEWAY_AUTOSTART"
"""Set to 1 on a daemon ``ensure`` started itself (not a service's, not ``gateway start``'s)."""
_POLL_S: Final = 0.1
_TAIL_LINES: Final = 15
_TAIL_BYTES: Final = 64 * 1024
_LAUNCH_WAIT_S: Final = 10.0
_NOT_DELIBERATE: Final = frozenset({"restart"})
"""Planned-stop reasons that are part of a restart, not a person stopping the daemon."""

# The intermediate of the double fork: starts the daemon in a new session with its output on
# our stderr (the stdio log), prints its pid and exits, so the daemon is re-parented away.
_LAUNCHER: Final = (
    "import subprocess, sys\n"
    "child = subprocess.Popen(sys.argv[1:], stdin=subprocess.DEVNULL, stdout=2, stderr=2,"
    " start_new_session=True, close_fds=True)\n"
    "sys.stdout.write(str(child.pid))\n"
)


class Starter(Protocol):
    """What ``ensure`` needs from an installed service (``gateway_manager.ServiceManager``)."""

    def start(self) -> None: ...


@dataclass(frozen=True)
class Endpoint:
    """Where a client dials, without any credential (``describe`` / ``identify``)."""

    ws_url: str | None
    profile_id: str
    instance_id: str
    code_version: str

    def to_json(self) -> dict[str, Any]:
        return {
            "ws_url": self.ws_url,
            "profile_id": self.profile_id,
            "instance_id": self.instance_id,
            "code_version": self.code_version,
        }


@dataclass(frozen=True)
class Ensured:
    """The verdict. ``state`` is ``ready`` (with ``endpoint``), ``absent`` (nothing runs and
    nothing was started), ``stopped`` (stopped on purpose, left alone), ``starting`` (it did not
    answer within the deadline), ``exited`` (the started daemon died first), ``refused``
    (``reason``: ``unsafe_home``, ``unsupported``, ``recover_budget``) or ``failed``
    (``reason``: ``spawn_failed``, ``service_failed``, ``restart_timeout``)."""

    state: State
    reason: str = ""
    detail: str = ""
    endpoint: Endpoint | None = None
    pid: int | None = None
    started: bool = False
    restarted: bool = False
    skew: bool = False

    @property
    def attachable(self) -> bool:
        return self.state == "ready" and self.endpoint is not None

    @property
    def terminal(self) -> bool:
        """Trying again soon will not help (a person must act first)."""
        return self.state in ("stopped", "refused") and self.reason != "recover_budget"

    def to_json(self) -> dict[str, Any]:
        if self.endpoint is not None and self.state == "ready":
            return self.endpoint.to_json()
        return {"state": self.state, "reason": self.reason, "detail": self.detail}


@dataclass
class RecoverBudget:
    """At most ``limit`` automatic recoveries per sliding ``window`` seconds (one per client)."""

    limit: int = RECOVER_LIMIT
    window: float = RECOVER_WINDOW_S
    monotonic: Callable[[], float] = time.monotonic
    _spent: deque[float] = field(default_factory=deque[float])

    def available(self) -> bool:
        self._expire()
        return len(self._spent) < self.limit

    def take(self) -> bool:
        """Spend one recovery; False (nothing spent) when the window is full."""
        if not self.available():
            return False
        self._spent.append(self.monotonic())
        return True

    def _expire(self) -> None:
        now = self.monotonic()
        while self._spent and now - self._spent[0] >= self.window:
            self._spent.popleft()


def daemon_argv() -> list[str]:
    """The foreground daemon command a detached start runs (the service unit runs the same)."""
    return [*default_program(), "gateway", "run"]


def planned_stop_reason(home: Path) -> str | None:
    """The reason a person stopped the daemon on purpose (``stop``), or None: the status file says
    ``stopped`` with a ``planned_stop:<reason>`` label that is not part of a restart."""
    view = gateway_runtime.runtime_view(home)
    if view.liveness != "stopped":
        return None
    label = (view.data or {}).get("exit_reason")
    if not isinstance(label, str) or not label.startswith("planned_stop:"):
        return None
    reason = label.removeprefix("planned_stop:") or "stop"
    return None if reason in _NOT_DELIBERATE else reason


def _wait(seconds: float) -> None:
    threading.Event().wait(max(0.0, seconds))


@dataclass
class _Run:
    """One ``ensure`` call's settings and deadline."""

    home: Path
    env: Mapping[str, str]
    deadline: float
    version: str
    monotonic: Callable[[], float]
    pause: Callable[[float], None]
    argv: Sequence[str]

    def left(self) -> float:
        return self.deadline - self.monotonic()


def ensure(
    home: Path,
    env: Mapping[str, str],
    *,
    mode: Mode = "start",
    timeout: float = DEFAULT_TIMEOUT_S,
    version: str | None = None,
    service: Starter | None = None,
    budget: RecoverBudget | None = None,
    argv: Sequence[str] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    pause: Callable[[float], None] = _wait,
) -> Ensured:
    """Find or start the daemon serving ``home`` (see the module notes). Never raises for an
    expected condition: every outcome is a verdict."""
    if os.name != "posix":
        return Ensured("refused", "unsupported", "the gateway control socket needs POSIX")
    if not gateway_control.home_is_safe(home):
        return Ensured("refused", "unsafe_home", "the profile home is writable by other users")
    if version is None:
        import indus

        version = indus.__version__
    run = _Run(
        home,
        env,
        monotonic() + max(0.0, timeout),
        version,
        monotonic,
        pause,
        list(argv) if argv is not None else daemon_argv(),
    )
    found = gateway_control.identify(home)
    if found is not None:
        return _with_live(run, found, mode, service)
    if _starting(home):
        if mode == "discover":
            return Ensured("starting", "starting", "a gateway is starting for this profile")
        return _await_answer(run, started=False)
    if mode == "discover":
        return Ensured("absent", "not_running", "no gateway runs for this profile")
    if mode == "recover":
        reason = planned_stop_reason(home)
        if reason is not None:
            return Ensured("stopped", "planned_stop", f"the gateway was stopped ({reason})")
        if budget is not None and not budget.take():
            return Ensured(
                "refused",
                "recover_budget",
                f"the gateway was restarted {budget.limit} times in {budget.window:g} s",
            )
    return _start(run, service)


# -- a live daemon --------------------------------------------------------------------------------


def _version_of(found: Mapping[str, Any]) -> str:
    value = found.get("code_version")
    return value if isinstance(value, str) else ""


def _with_live(run: _Run, found: dict[str, Any], mode: Mode, service: Starter | None) -> Ensured:
    if _version_of(found) == run.version or mode != "start":
        return _ready(run, found, skew=_version_of(found) != run.version)
    restarted = _restart(run, found, service)
    if isinstance(restarted, Ensured):
        return restarted
    return _ready(run, restarted, restarted=True, skew=_version_of(restarted) != run.version)


def _restart(run: _Run, found: dict[str, Any], service: Starter | None) -> dict[str, Any] | Ensured:
    """Ask the daemon to restart (SIGUSR1) and return the new one's identity."""
    pid = int(found["pid"])
    start_time = found.get("start_time")
    start = start_time if isinstance(start_time, int) and not isinstance(start_time, bool) else None
    supervised = found.get("supervised") is True or found.get("supervisor") not in (
        None,
        "manual",
    )
    log.info(
        "gateway pid %d runs version %s, this client %s: restarting it",
        pid,
        _version_of(found) or "unknown",
        run.version,
    )
    gateway_runtime.record_planned_stop(run.home, pid, "restart", start_time=start, by="ensure")
    try:
        os.kill(pid, signal.SIGUSR1)
    except ProcessLookupError:
        pass
    except PermissionError:
        return Ensured("failed", "restart_refused", f"not allowed to signal pid {pid}")
    if supervised:
        answer = _await_answer(run, started=False, not_pid=pid)
        if answer.state != "ready" or answer.pid is None:
            return answer
        again = gateway_control.identify(run.home)
        return again if again is not None else answer
    while gateway_runtime.alive(pid, start):
        if run.left() <= 0:
            return Ensured(
                "failed", "restart_timeout", f"the old gateway (pid {pid}) is still finishing"
            )
        run.pause(_POLL_S)
    started = _start(run, service)
    if started.state != "ready":
        return started
    again = gateway_control.identify(run.home)
    return again if again is not None else started


def _ready(
    run: _Run,
    found: Mapping[str, Any],
    *,
    started: bool = False,
    restarted: bool = False,
    skew: bool = False,
) -> Ensured:
    described = _described(run)
    ws_url = described.get("ws_url")
    profile = described.get("profile_id")
    instance = described.get("instance_id")
    described_version = described.get("code_version")
    endpoint = Endpoint(
        ws_url=ws_url if isinstance(ws_url, str) and ws_url else None,
        profile_id=profile if isinstance(profile, str) else stable_profile_id(run.home),
        instance_id=instance if isinstance(instance, str) else "",
        code_version=_version_of(found)
        or (described_version if isinstance(described_version, str) else ""),
    )
    pid = found.get("pid")
    return Ensured(
        "ready",
        endpoint=endpoint,
        pid=pid if isinstance(pid, int) else None,
        started=started,
        restarted=restarted,
        skew=skew,
    )


def _described(run: _Run) -> dict[str, Any]:
    """``describe``, waiting (within the deadline) while the daemon still starts without its
    attach endpoint: the control socket answers early in the start, the WebSocket is mounted
    last, just before the daemon reports ``running``. A running daemon without one is final."""
    while True:
        described = gateway_control.describe(run.home) or {}
        if described.get("ws_url") or not _still_starting(run.home) or run.left() <= 0:
            return described
        run.pause(_POLL_S)


def _still_starting(home: Path) -> bool:
    view = gateway_runtime.runtime_view(home)
    return view.liveness == "running" and view.state == "starting"


# -- starting -------------------------------------------------------------------------------------


def _starting(home: Path) -> bool:
    """A daemon process is up (fresh status file, its process alive) but not answering yet."""
    view = gateway_runtime.runtime_view(home)
    return view.liveness == "running" and view.state in ("starting", "running", "degraded")


def _start(run: _Run, service: Starter | None) -> Ensured:
    if service is not None:
        try:
            service.start()
        except Exception as exc:
            return Ensured("failed", "service_failed", f"the service did not start: {exc}")
        return _await_answer(run, started=True)
    offset = _log_size(run.home)
    try:
        pid = _spawn(run)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return Ensured("failed", "spawn_failed", f"could not start the gateway: {exc}")
    log.info("started a gateway for %s (pid %d)", run.home, pid)
    return _await_answer(run, started=True, child=pid, offset=offset)


def _log_size(home: Path) -> int:
    try:
        return gateway_runtime.stdio_log_path(home).stat().st_size
    except OSError:
        return 0


def _launch_tail(home: Path, offset: int) -> str:
    """The last lines this launch wrote to the stdio log, credential shapes masked."""
    path = gateway_runtime.stdio_log_path(home)
    try:
        with open(path, "rb") as stream:
            stream.seek(max(offset, path.stat().st_size - _TAIL_BYTES))
            text = stream.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    lines = [line.rstrip() for line in text.splitlines() if line.strip()][-_TAIL_LINES:]
    return redact_text("\n".join(lines))


def _child_env(run: _Run) -> dict[str, str]:
    env = scrub_launch_env(run.env)
    env["INDUS_HOME"] = str(run.home)
    env.pop(gateway_runtime.SUPERVISED_ENV, None)
    env.pop(gateway_runtime.GATEWAY_GUARD_ENV, None)
    env[AUTOSTART_ENV] = "1"
    return env


def _spawn(run: _Run) -> int:
    """Double-fork ``run.argv`` (see the module notes); the daemon's pid."""
    log_path = gateway_runtime.stdio_log_path(run.home)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab") as out:
        launcher = subprocess.Popen(
            [sys.executable, "-I", "-c", _LAUNCHER, *run.argv],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=out,
            cwd=str(run.home),
            env=_child_env(run),
            start_new_session=True,
            close_fds=True,
        )
    try:
        printed, _ = launcher.communicate(timeout=_LAUNCH_WAIT_S)
    except subprocess.TimeoutExpired:
        launcher.kill()
        launcher.communicate()
        raise
    text = printed.decode("ascii", errors="replace").strip()
    if launcher.returncode != 0 or not text.isdigit():
        raise ValueError(f"the launcher failed (exit {launcher.returncode})")
    return int(text)


def _await_answer(
    run: _Run,
    *,
    started: bool,
    child: int | None = None,
    not_pid: int | None = None,
    offset: int = 0,
) -> Ensured:
    """Wait for an ``identify`` answer (from a pid other than ``not_pid``) until the deadline."""
    while True:
        found = gateway_control.identify(run.home)
        if found is not None and found["pid"] != not_pid:
            return _ready(run, found, started=started)
        if child is not None and not gateway_runtime.pid_alive(child):
            # Lost a start race? The winner answers; give it a moment before calling it an exit.
            found = gateway_control.identify(run.home)
            if found is not None:
                return _ready(run, found)
            if not _starting(run.home):
                return _exited(run, child, offset)
        if run.left() <= 0:
            return Ensured(
                "starting",
                "timeout",
                "the gateway did not answer in time; it may still be starting",
                started=started,
            )
        run.pause(_POLL_S)


def _exited(run: _Run, pid: int, offset: int) -> Ensured:
    log_path = gateway_runtime.stdio_log_path(run.home)
    detail = f"the gateway exited before it answered; see {log_path}"
    status = gateway_runtime.read_status(run.home)
    if status is not None and status.pid == pid:
        why = status.exit_reason or status.state
        code = "" if status.exit_code is None else f", code {status.exit_code}"
        detail = f"the gateway exited before it answered ({why}{code}); see {log_path}"
    tail = _launch_tail(run.home, offset)
    if tail:
        detail += "\n" + "\n".join("  " + line for line in tail.splitlines())
    log.warning("gateway pid %d exited before answering", pid)
    return Ensured("exited", "exited", detail, pid=pid, started=True)
