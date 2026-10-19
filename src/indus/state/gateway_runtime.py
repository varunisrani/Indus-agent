"""The gateway's runtime records in the profile home, shared by everyone who needs them (P15-24).

The daemon writes these; the CLI (``indus gateway status/stop``), the cron tick guard and the
updater read them. They live here, below every surface, so none of those readers imports the
gateway (layer rule D-16.246).

Process identity (R06.18, R03.20): a process is the one a record names only when its pid exists,
its start time equals the recorded one and, where both sides can read it, its pid namespace is
ours. A pid number alone is never proof: pids are reused, and a pid written by a process in
another container means nothing here. When a value cannot be read on one side the check fails
closed (counts as alive), so a running gateway is never mistaken for a dead one.

Files (all under the profile home):
- ``gateway.lock``: the single-instance ``flock`` (held for the daemon's life; never deleted);
- ``gateway.pid``: the pid record (JSON: pid, start time, pid namespace, host, profile, ...);
- ``gateway_state.json``: the runtime status (``RuntimeStatus``), rewritten as a heartbeat; it is
  also the lifecycle marker: left in a live state by a dead owner means an unclean death;
- ``gateway_planned_stop.json``: written by ``indus gateway stop`` (or a unit) *before* it signals,
  so the daemon labels the exit as planned rather than an external kill;
- ``gateway_drain.json``: the external drain request (rules in ``gateway.daemon.drain``);
- ``gateway_boots.json``: the restart-loop guard's boot chain;
- ``logs/gateway-stdio.log``: what a detached ``indus gateway run`` prints.

The CLI side (P15-28) reads the status file through ``runtime_view``: the daemon's own state plus
our liveness verdict (running / stale / stopped / missing) and ``alive(pid, start_time)``.
"""

from __future__ import annotations

import contextlib
import errno
import json
import logging
import os
import re
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal, cast

from indus.foundation import fsio

__all__ = [
    "EXIT_CONFIG",
    "EXIT_OK",
    "EXIT_RESTART",
    "GATEWAY_GUARD_ENV",
    "LIVE_STATES",
    "PLANNED_STOP_FILE",
    "STATUS_FILE",
    "STATUS_STALE_AFTER",
    "STDIO_LOG",
    "SUPERVISED_ENV",
    "GatewayState",
    "PidRecord",
    "PlanStop",
    "PlatformState",
    "PlatformStatus",
    "ProcessIdentity",
    "RuntimeStatus",
    "RuntimeView",
    "alive",
    "boots_path",
    "clear_planned_stop",
    "drain_marker_path",
    "exits_log_path",
    "faulthandler_log_path",
    "is_stale",
    "live_gateway",
    "lock_path",
    "pid_alive",
    "pid_namespace",
    "pid_path",
    "planned_stop_path",
    "process_start_time",
    "read_pid_record",
    "read_planned_stop",
    "read_status",
    "record_planned_stop",
    "runtime_view",
    "status_path",
    "stdio_log_path",
    "supervised",
    "write_pid_record",
    "write_status",
]

log = logging.getLogger(__name__)

EXIT_OK: Final = 0
"""A clean stop."""
EXIT_RESTART: Final = 75
"""Please restart me (graceful restart, watchdog, another live gateway under a supervisor)."""
EXIT_CONFIG: Final = 78
"""Fatal configuration: supervisors must not restart (never used for transient conditions)."""

SUPERVISED_ENV: Final = "INDUS_GATEWAY_SUPERVISED"
"""Set to 1 by the service units: the process runs under a supervisor that restarts it."""
GATEWAY_GUARD_ENV: Final = "_INDUS_GATEWAY"
"""Set inside the daemon process: lifecycle commands run from it (the agent's own terminal)
must refuse to stop or restart the gateway that hosts them."""

STATUS_FILE: Final = "gateway_state.json"
PLANNED_STOP_FILE: Final = "gateway_planned_stop.json"
STDIO_LOG: Final = "gateway-stdio.log"

STATUS_STALE_AFTER: Final = 120.0
"""Seconds after ``updated_at`` when a reader treats the status file as stale."""

type GatewayState = Literal[
    "starting", "running", "draining", "stopping", "stopped", "startup_failed", "degraded"
]
type PlatformState = Literal["connected", "retrying", "fatal", "paused"]

LIVE_STATES: Final = frozenset({"starting", "running", "draining", "stopping"})
"""States a process leaves the file in only while it runs: found with a dead owner, the previous
process died without its exit path (an unclean death)."""


# -- paths -----------------------------------------------------------------------------------------


def lock_path(home: Path) -> Path:
    return home / "gateway.lock"


def pid_path(home: Path) -> Path:
    return home / "gateway.pid"


def status_path(home: Path) -> Path:
    return home / STATUS_FILE


def planned_stop_path(home: Path) -> Path:
    return home / PLANNED_STOP_FILE


def drain_marker_path(home: Path) -> Path:
    return home / "gateway_drain.json"


def boots_path(home: Path) -> Path:
    return home / "gateway_boots.json"


def exits_log_path(home: Path) -> Path:
    return home / "logs" / "gateway_exits.log"


def faulthandler_log_path(home: Path) -> Path:
    return home / "logs" / "gateway_faulthandler.log"


def stdio_log_path(home: Path) -> Path:
    """Where a detached ``indus gateway run`` writes what it prints."""
    return home / "logs" / STDIO_LOG


def supervised(env: Mapping[str, str]) -> bool:
    """True when a service manager that restarts the gateway started this process."""
    return str(env.get(SUPERVISED_ENV, "")).strip().lower() in {"1", "true", "yes", "on"}


# -- process identity ------------------------------------------------------------------------------

_NS_INODE = re.compile(r"\[(\d+)\]")


def _zombie(pid: int) -> bool:
    """True for a process that has exited and waits to be reaped (Linux; False elsewhere)."""
    if not sys.platform.startswith("linux"):
        return False
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
    except OSError:
        return False
    fields = raw.rsplit(")", 1)[-1].split()
    return bool(fields) and fields[0] in {"Z", "X"}


def pid_alive(pid: int) -> bool:
    """True when a running process with ``pid`` exists (owned by anyone); a zombie is gone."""
    if pid < 1:
        return False
    try:
        os.kill(pid, 0)  # signal 0 delivers nothing; it only asks whether the pid exists
    except OverflowError:
        return False
    except OSError as exc:
        return exc.errno == errno.EPERM and not _zombie(pid)  # another user's process
    return not _zombie(pid)


def _ps_start_time(pid: int) -> int | None:
    try:
        done = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
            env={"LC_ALL": "C", "PATH": "/bin:/usr/bin:/usr/sbin:/sbin"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = " ".join(done.stdout.split())
    if not text:
        return None
    try:
        return int(time.mktime(time.strptime(text, "%a %b %d %H:%M:%S %Y")))
    except ValueError:
        return None


def process_start_time(pid: int) -> int | None:
    """A per-process start fingerprint (None when unknown).

    Linux: field 22 of ``/proc/<pid>/stat`` (clock ticks since boot). Elsewhere: ``ps``'s start
    time in epoch seconds. Only ever compared with a value read the same way on the same host.
    """
    if pid <= 0:
        return None
    if sys.platform.startswith("linux"):
        try:
            raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
        except OSError:
            return None
        fields = raw.rsplit(")", 1)[-1].split()
        try:
            return int(fields[19])
        except (IndexError, ValueError):
            return None
    if sys.platform == "win32":
        return None
    return _ps_start_time(pid)


def pid_namespace(pid: int | None = None) -> str | None:
    """The inode of a process's pid namespace (``None`` where it cannot be read)."""
    if not sys.platform.startswith("linux"):
        return None
    target = "self" if pid is None else str(pid)
    try:
        link = os.readlink(f"/proc/{target}/ns/pid")
    except OSError:
        return None
    found = _NS_INODE.search(link)
    return found.group(1) if found else None


def _host() -> str:
    try:
        return socket.gethostname() or "localhost"
    except OSError:
        return "localhost"


@dataclass(frozen=True)
class ProcessIdentity:
    """Who a record names: pid, start fingerprint, pid namespace and host."""

    pid: int
    start_time: int | None = None
    pidns: str | None = None
    host: str = ""

    @classmethod
    def current(cls) -> ProcessIdentity:
        pid = os.getpid()
        return cls(pid, process_start_time(pid), pid_namespace(), _host())

    def is_alive(
        self,
        *,
        alive: Callable[[int], bool] = pid_alive,
        start_time: Callable[[int], int | None] = process_start_time,
        namespace: Callable[[], str | None] = pid_namespace,
        host: Callable[[], str] = _host,
    ) -> bool:
        """Does this exact process still run? Pid + start time + namespace; unknowns fail closed.

        A record from another host or another pid namespace cannot be probed from here: it counts
        as alive (the single-instance lock, or the control socket, settles it)."""
        if self.pid < 1:
            return False
        if self.host and self.host != host():
            return True
        ours = namespace()
        if self.pidns and ours and self.pidns != ours:
            return True
        if not alive(self.pid):
            return False
        current = start_time(self.pid)
        if self.start_time is not None and current is not None:
            return self.start_time == current
        return True

    def to_json(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "start_time": self.start_time,
            "pidns": self.pidns,
            "host": self.host,
        }


def alive(pid: int, start_time: int | None) -> bool:
    """``pid`` still runs the process whose start fingerprint is ``start_time``: it exists, has
    not exited (a zombie is gone) and, when both start times are known, they match."""
    return ProcessIdentity(pid, start_time).is_alive()


def _as_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _as_str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _read_object(path: Path) -> tuple[bool, dict[str, Any] | None]:
    """``(exists, object)``: an existing file that is not a JSON object gives ``(True, None)``."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False, None
    except OSError:
        return True, None
    try:
        data = json.loads(text)
    except ValueError:
        return True, None
    if not isinstance(data, dict):
        return True, None
    return True, {str(k): v for k, v in cast(dict[object, Any], data).items()}


def _now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="seconds")


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


# -- the pid record --------------------------------------------------------------------------------


@dataclass(frozen=True)
class PidRecord:
    """``gateway.pid``: the live daemon of a profile home."""

    process: ProcessIdentity
    home: str = ""
    profile: str = ""
    started_at: str = ""
    code_version: str = ""
    supervised: bool = False

    @property
    def pid(self) -> int:
        return self.process.pid

    def to_json(self) -> dict[str, Any]:
        return {
            **self.process.to_json(),
            "home": self.home,
            "profile": self.profile,
            "started_at": self.started_at,
            "code_version": self.code_version,
            "supervised": self.supervised,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> PidRecord | None:
        pid = _as_int(data.get("pid"))
        if pid is None or pid < 1:
            return None
        process = ProcessIdentity(
            pid,
            _as_int(data.get("start_time")),
            _as_str(data.get("pidns")) or None,
            _as_str(data.get("host")),
        )
        return cls(
            process,
            home=_as_str(data.get("home")),
            profile=_as_str(data.get("profile")),
            started_at=_as_str(data.get("started_at")),
            code_version=_as_str(data.get("code_version")),
            supervised=data.get("supervised") is True,
        )


def write_pid_record(home: Path, record: PidRecord) -> None:
    fsio.atomic_write_json(pid_path(home), record.to_json(), mode=0o600)


def read_pid_record(home: Path) -> PidRecord | None:
    _exists, data = _read_object(pid_path(home))
    return None if data is None else PidRecord.from_json(data)


def live_gateway(home: Path, **probes: Any) -> PidRecord | None:
    """The record of the gateway that serves ``home`` now, or None (never a bare-pid answer).

    ``probes`` are passed to ``ProcessIdentity.is_alive`` (tests use a fake process table)."""
    record = read_pid_record(home)
    if record is None:
        return None
    return record if record.process.is_alive(**probes) else None


# -- runtime status --------------------------------------------------------------------------------


@dataclass(frozen=True)
class PlatformStatus:
    state: PlatformState
    needs_attention: bool = False
    error_code: str | None = None
    error: str | None = None
    retrying_since: str | None = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"state": self.state}
        if self.needs_attention:
            out["needs_attention"] = True
        if self.error_code:
            out["error_code"] = self.error_code
        if self.error:
            out["error"] = self.error
        if self.retrying_since:
            out["retrying_since"] = self.retrying_since
        return out

    @classmethod
    def from_json(cls, data: object) -> PlatformStatus | None:
        if not isinstance(data, dict):
            return None
        raw = {str(k): v for k, v in cast(dict[object, Any], data).items()}
        state = raw.get("state")
        if state not in ("connected", "retrying", "fatal", "paused"):
            return None
        return cls(
            state,
            needs_attention=raw.get("needs_attention") is True,
            error_code=_as_str(raw.get("error_code")) or None,
            error=_as_str(raw.get("error")) or None,
            retrying_since=_as_str(raw.get("retrying_since")) or None,
        )


_STATES: Final = frozenset(
    {"starting", "running", "draining", "stopping", "stopped", "startup_failed", "degraded"}
)


@dataclass(frozen=True)
class RuntimeStatus:
    """``gateway_state.json``."""

    state: GatewayState
    updated_at: str
    process: ProcessIdentity
    profile: str = ""
    code_version: str = ""
    epoch: str = ""
    exit_reason: str | None = None
    exit_code: int | None = None
    previous_exit: str | None = None
    platforms: Mapping[str, PlatformStatus] = field(default_factory=dict[str, PlatformStatus])
    surfaces: Mapping[str, Mapping[str, Any]] = field(default_factory=dict[str, Mapping[str, Any]])
    """The HTTP surfaces (P15-30..32): ``{name: {listening, active_turns, ...} | {problem}}``."""

    @property
    def pid(self) -> int:
        return self.process.pid

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "state": self.state,
            "updated_at": self.updated_at,
            **self.process.to_json(),
            "profile": self.profile,
            "code_version": self.code_version,
            "epoch": self.epoch,
            "platforms": {name: row.to_json() for name, row in sorted(self.platforms.items())},
        }
        if self.exit_reason is not None:
            out["exit_reason"] = self.exit_reason
        if self.exit_code is not None:
            out["exit_code"] = self.exit_code
        if self.previous_exit is not None:
            out["previous_exit"] = self.previous_exit
        if self.surfaces:
            out["surfaces"] = {name: dict(row) for name, row in sorted(self.surfaces.items())}
        return out

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> RuntimeStatus | None:
        state = data.get("state")
        pid = _as_int(data.get("pid"))
        if state not in _STATES or pid is None:
            return None
        platforms: dict[str, PlatformStatus] = {}
        raw_platforms = data.get("platforms")
        if isinstance(raw_platforms, dict):
            for name, row in cast(dict[object, object], raw_platforms).items():
                parsed = PlatformStatus.from_json(row)
                if parsed is not None:
                    platforms[str(name)] = parsed
        surfaces: dict[str, Mapping[str, Any]] = {}
        raw_surfaces = data.get("surfaces")
        if isinstance(raw_surfaces, dict):
            for name, row in cast(dict[object, object], raw_surfaces).items():
                if isinstance(row, dict):
                    surfaces[str(name)] = {
                        str(k): v for k, v in cast(dict[object, Any], row).items()
                    }
        return cls(
            cast(GatewayState, state),
            _as_str(data.get("updated_at")),
            ProcessIdentity(
                pid,
                _as_int(data.get("start_time")),
                _as_str(data.get("pidns")) or None,
                _as_str(data.get("host")),
            ),
            profile=_as_str(data.get("profile")),
            code_version=_as_str(data.get("code_version")),
            epoch=_as_str(data.get("epoch")),
            exit_reason=_as_str(data.get("exit_reason")) or None,
            exit_code=_as_int(data.get("exit_code")),
            previous_exit=_as_str(data.get("previous_exit")) or None,
            platforms=platforms,
            surfaces=surfaces,
        )

    def stamped(self, now: datetime) -> RuntimeStatus:
        return replace(self, updated_at=_now_iso(now))


def write_status(home: Path, status: RuntimeStatus) -> None:
    """Atomic replace, mode 0600."""
    fsio.atomic_write_json(status_path(home), status.to_json(), mode=0o600)


def read_status(home: Path) -> RuntimeStatus | None:
    _exists, data = _read_object(status_path(home))
    return None if data is None else RuntimeStatus.from_json(data)


def is_stale(
    status: RuntimeStatus | None, now: datetime, *, after: float = STATUS_STALE_AFTER
) -> bool:
    """True when the status was not refreshed for ``after`` seconds (or cannot be dated)."""
    if status is None:
        return True
    updated = _parse_iso(status.updated_at)
    if updated is None:
        return True
    return (now - updated).total_seconds() > after


# -- planned stop ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanStop:
    """``gateway_planned_stop.json``: a stop asked for by a person or a unit, not a kill.

    It names the process it was meant for (pid + start time); a record for another process
    means nothing."""

    pid: int
    reason: str
    at: str
    start_time: int | None = None
    by: str = ""

    def matches(self, pid: int, start_time: int | None) -> bool:
        if pid != self.pid:
            return False
        return self.start_time is None or start_time is None or self.start_time == start_time


def record_planned_stop(
    home: Path,
    pid: int,
    reason: str = "stop",
    *,
    start_time: int | None = None,
    by: str = "",
    now: datetime | None = None,
) -> None:
    """Write the planned-stop record (0600); call it *before* signalling the daemon."""
    fsio.atomic_write_json(
        planned_stop_path(home),
        {"pid": pid, "start_time": start_time, "reason": reason, "by": by, "at": _now_iso(now)},
        mode=0o600,
    )


def read_planned_stop(home: Path) -> PlanStop | None:
    _exists, data = _read_object(planned_stop_path(home))
    if data is None:
        return None
    pid = _as_int(data.get("pid"))
    if pid is None:
        return None
    return PlanStop(
        pid,
        _as_str(data.get("reason")) or "stop",
        _as_str(data.get("at")),
        start_time=_as_int(data.get("start_time")),
        by=_as_str(data.get("by")),
    )


def clear_planned_stop(home: Path) -> None:
    with contextlib.suppress(OSError):
        planned_stop_path(home).unlink(missing_ok=True)


# -- the CLI's view of the status file (P15-28) ----------------------------------------------------

type Liveness = Literal["running", "stale", "stopped", "missing"]

_DOWN_STATES: Final = frozenset({"stopped", "startup_failed"})


@dataclass(frozen=True)
class RuntimeView:
    """The status file as a reader outside the daemon sees it. ``state`` is the gateway's own word
    (``running``, ``draining``, ``degraded``, ...); ``liveness`` is the reader's verdict."""

    liveness: Liveness
    state: str = ""
    pid: int | None = None
    start_time: int | None = None
    age_s: float | None = None
    reason: str = ""
    data: dict[str, Any] | None = None


def _epoch(value: object) -> float | None:
    """``updated_at`` as epoch seconds: ISO-8601 text (what the daemon writes) or a number."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    parsed = _parse_iso(value)
    return parsed.timestamp() if parsed is not None else None


def runtime_view(home: Path, *, now: float | None = None) -> RuntimeView:
    """Read ``gateway_state.json`` for a reader: stopped (a down state), stale (its process is gone
    or its heartbeat is older than ``STATUS_STALE_AFTER``), running, or missing. Never raises."""
    exists, data = _read_object(status_path(home))
    if not exists:
        return RuntimeView("missing")
    if data is None:
        return RuntimeView("stale", reason="the status file cannot be read")
    state = _as_str(data.get("state"))
    pid = _as_int(data.get("pid"))
    start = _as_int(data.get("start_time"))
    updated = _epoch(data.get("updated_at"))
    current = time.time() if now is None else now
    age = None if updated is None else max(0.0, current - updated)
    if state in _DOWN_STATES:
        reason = _as_str(data.get("exit_reason"))
        return RuntimeView("stopped", state, pid, start, age, reason, data)
    identity = ProcessIdentity(
        pid or 0, start, _as_str(data.get("pidns")) or None, _as_str(data.get("host"))
    )
    if pid is None or not identity.is_alive():
        why = "its process is gone" if pid is not None else "it names no process"
        return RuntimeView("stale", state, pid, start, age, why, data)
    if age is None or age > STATUS_STALE_AFTER:
        return RuntimeView("stale", state, pid, start, age, "no heartbeat for over 120 s", data)
    return RuntimeView("running", state, pid, start, age, "", data)
