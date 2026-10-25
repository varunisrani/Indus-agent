"""Records of running dashboard backends, and ``indus serve ensure`` / ``discover`` (P18-01).

Two kinds of record, both JSON written atomically with mode 0600, removed by their owner on a
clean exit:

- ``<root>/dashboard.json`` — the machine's dashboard backend (one per machine root, it serves every
  profile under it);
- ``<home>/serve.json`` — a profile's headless backend (``indus serve``; the desktop shell's REST
  side, D-16.89).

A record proves nothing alone: a backend is live only when its pid is alive with the recorded start
fingerprint **and** ``GET /api/health`` on its loopback URL answers with the same profile id.

``discover(home)`` returns the live headless backend of a profile or None. ``ensure(home, env)``
discovers one, or starts ``indus serve --port 0`` detached (double fork, own session, stdin from
``/dev/null``, output appended to ``<home>/logs/serve-stdio.log``) and waits for its record and its
health answer. A backend running another ``code_version`` than the caller is stopped (SIGTERM, a
bounded wait) and replaced, once per call. The session token is the caller's: it travels to the
child only through the environment (``INDUS_DASHBOARD_SESSION_TOKEN``) and is never printed —
``Served.to_json`` is ``{url, port, pid, profile_id, code_version}``.

Windows (docs/notes/P25/windows-gateway.md): no double fork; the server starts as a detached
process (``platform.winproc.spawn_detached``), and the SIGTERM that replaces an old one ends it at
once (``os.kill`` terminates there; a headless backend holds no turns).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final, Literal, cast

import httpx

from indus.foundation.fsio import FileLock, LockTimeout, atomic_write_json
from indus.service.creation_policy import scrub_launch_env
from indus.service.gateway_units import default_program
from indus.service.ws_auth import stable_profile_id
from indus.state import gateway_runtime

__all__ = [
    "DASHBOARD_RECORD",
    "SERVE_RECORD",
    "Served",
    "ServerRecord",
    "clear_record",
    "dashboard_record_path",
    "discover",
    "ensure",
    "health_probe",
    "live_health",
    "read_record",
    "serve_record_path",
    "url_for",
    "write_record",
]

log = logging.getLogger(__name__)

SERVE_RECORD: Final = "serve.json"
SERVE_LOCK: Final = "serve.lock"
DASHBOARD_RECORD: Final = "dashboard.json"
STDIO_LOG: Final = "serve-stdio.log"
DEFAULT_TIMEOUT_S: Final = 30.0
_POLL_S: Final = 0.1
_STOP_WAIT_S: Final = 10.0
_LAUNCHER: Final = (
    "import subprocess, sys\n"
    "child = subprocess.Popen(sys.argv[1:], stdin=subprocess.DEVNULL, stdout=2, stderr=2,"
    " start_new_session=True, close_fds=True)\n"
    "sys.stdout.write(str(child.pid))\n"
)

type Mode = Literal["dashboard", "serve"]


@dataclass(frozen=True)
class ServerRecord:
    pid: int
    start_time: int | None
    host: str
    port: int
    url: str
    profile_id: str
    profile: str
    code_version: str
    mode: str
    started_at: float

    @classmethod
    def from_json(cls, data: object) -> ServerRecord | None:
        if not isinstance(data, dict):
            return None
        raw = cast(dict[str, Any], data)
        try:
            pid = raw["pid"]
            port = raw["port"]
            if not isinstance(pid, int) or not isinstance(port, int) or pid <= 0:
                return None
            start = raw.get("start_time")
            return cls(
                pid=pid,
                start_time=start if isinstance(start, int) else None,
                host=str(raw["host"]),
                port=port,
                url=str(raw["url"]),
                profile_id=str(raw["profile_id"]),
                profile=str(raw.get("profile", "")),
                code_version=str(raw.get("code_version", "")),
                mode=str(raw.get("mode", "")),
                started_at=float(raw.get("started_at", 0.0)),
            )
        except (KeyError, TypeError, ValueError):
            return None


def serve_record_path(home: Path) -> Path:
    return home / SERVE_RECORD


def dashboard_record_path(root: Path) -> Path:
    return root / DASHBOARD_RECORD


def url_for(host: str, port: int) -> str:
    """A loopback-reachable URL for a bound address (all-interfaces binds answer on 127.0.0.1)."""
    bare = host.strip("[]")
    if bare in ("0.0.0.0", ""):
        bare = "127.0.0.1"
    elif bare == "::":
        bare = "::1"
    shown = f"[{bare}]" if ":" in bare else bare
    return f"http://{shown}:{port}"


def write_record(path: Path, record: ServerRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, asdict(record), sort_keys=True, mode=0o600, preserve_mode=False)


def read_record(path: Path) -> ServerRecord | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return ServerRecord.from_json(data)


def clear_record(path: Path, pid: int) -> None:
    """Remove the record when it still names ``pid`` (never another process's)."""
    found = read_record(path)
    if found is not None and found.pid == pid:
        with contextlib.suppress(OSError):
            path.unlink()


def new_record(
    *, home: Path, profile: str, host: str, port: int, mode: Mode, code_version: str
) -> ServerRecord:
    pid = os.getpid()
    return ServerRecord(
        pid=pid,
        start_time=gateway_runtime.process_start_time(pid),
        host=host,
        port=port,
        url=url_for(host, port),
        profile_id=stable_profile_id(home),
        profile=profile,
        code_version=code_version,
        mode=mode,
        started_at=time.time(),
    )


# -- liveness -------------------------------------------------------------------------------------


def health_probe(url: str, *, timeout: float = 2.0) -> dict[str, Any] | None:
    """``GET <url>/api/health`` on loopback (no proxy); the JSON answer or None."""
    try:
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            response = client.get(url.rstrip("/") + "/api/health")
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    try:
        data: Any = response.json()
    except ValueError:
        return None
    return cast(dict[str, Any], data) if isinstance(data, dict) else None


type Probe = Callable[[str], dict[str, Any] | None]
type Alive = Callable[[int, int | None], bool]


def live_health(
    record: ServerRecord, *, probe: Probe = health_probe, alive: Alive = gateway_runtime.alive
) -> dict[str, Any] | None:
    """The health answer of the backend ``record`` names, or None when it is not that backend."""
    if not alive(record.pid, record.start_time):
        return None
    answer = probe(record.url)
    if answer is None or answer.get("profile_id") != record.profile_id:
        return None
    return answer


@dataclass(frozen=True)
class Served:
    """A live headless backend (no credential in here)."""

    url: str
    port: int
    pid: int
    profile_id: str
    code_version: str
    started: bool = False
    restarted: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "port": self.port,
            "pid": self.pid,
            "profile_id": self.profile_id,
            "code_version": self.code_version,
        }


def discover(
    home: Path, *, probe: Probe = health_probe, alive: Alive = gateway_runtime.alive
) -> Served | None:
    record = read_record(serve_record_path(home))
    if record is None or record.profile_id != stable_profile_id(home):
        return None
    answer = live_health(record, probe=probe, alive=alive)
    if answer is None:
        return None
    version = answer.get("code_version")
    return Served(
        url=record.url,
        port=record.port,
        pid=record.pid,
        profile_id=record.profile_id,
        code_version=version if isinstance(version, str) else record.code_version,
    )


# -- ensure ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class EnsureFailed:
    reason: str
    detail: str

    def to_json(self) -> dict[str, Any]:
        return {"state": "failed", "reason": self.reason, "detail": self.detail}


def _wait(seconds: float) -> None:
    threading.Event().wait(max(0.0, seconds))


def serve_argv() -> list[str]:
    return [*default_program(), "serve", "--port", "0"]


def spawn_detached(home: Path, env: Mapping[str, str], argv: Sequence[str]) -> int:
    """Start ``argv`` detached for ``home``; the server's pid."""
    child_env = scrub_launch_env(env)
    child_env["INDUS_HOME"] = str(home)
    logs = home / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        from indus.platform import winproc

        with open(logs / STDIO_LOG, "ab") as out:
            return winproc.spawn_detached(argv, cwd=str(home), env=child_env, out=out)
    with open(logs / STDIO_LOG, "ab") as out:
        launcher = subprocess.Popen(
            [sys.executable, "-I", "-c", _LAUNCHER, *argv],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=out,
            cwd=str(home),
            env=child_env,
            start_new_session=True,
            close_fds=True,
        )
    try:
        printed, _ = launcher.communicate(timeout=10.0)
    except subprocess.TimeoutExpired:
        launcher.kill()
        launcher.communicate()
        raise
    text = printed.decode("ascii", errors="replace").strip()
    if launcher.returncode != 0 or not text.isdigit():
        raise OSError(f"the launcher failed (exit {launcher.returncode})")
    return int(text)


def _ours_on_windows(home: Path, spawned_at: float) -> bool:
    """Windows: a venv's ``python.exe`` is a redirector that runs the real interpreter as its
    child, so the server records another pid than the one we started. A record written after our
    spawn is ours (``serve.lock`` keeps other callers from starting one meanwhile)."""
    if sys.platform != "win32":
        return False
    record = read_record(serve_record_path(home))
    return record is not None and record.started_at >= spawned_at - 1.0


def _terminate(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGTERM)


def ensure(
    home: Path,
    env: Mapping[str, str],
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
    **kwargs: Any,
) -> Served | EnsureFailed:
    """Discover the profile's headless backend or start one (see the module notes). Concurrent
    calls for one home are serialised by ``<home>/serve.lock``, so two callers start one backend."""
    try:
        with FileLock(home / SERVE_LOCK, timeout=timeout + _STOP_WAIT_S):
            return _ensure(home, env, timeout=timeout, **kwargs)
    except LockTimeout:
        return EnsureFailed("busy", "another caller is starting the backend")


def _ensure(
    home: Path,
    env: Mapping[str, str],
    *,
    version: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    argv: Sequence[str] | None = None,
    spawn: Callable[[Path, Mapping[str, str], Sequence[str]], int] = spawn_detached,
    terminate: Callable[[int], None] = _terminate,
    probe: Probe = health_probe,
    alive: Alive = gateway_runtime.alive,
    monotonic: Callable[[], float] = time.monotonic,
    pause: Callable[[float], None] = _wait,
) -> Served | EnsureFailed:
    if version is None:
        import indus

        version = indus.__version__
    deadline = monotonic() + max(0.0, timeout)
    found = discover(home, probe=probe, alive=alive)
    restarted = False
    if found is not None and found.code_version == version:
        return found
    if found is not None:
        log.info(
            "the headless backend runs version %s, not %s: restarting it",
            found.code_version,
            version,
        )
        record = read_record(serve_record_path(home))
        terminate(found.pid)
        stop_by = monotonic() + _STOP_WAIT_S
        start_time = record.start_time if record is not None else None
        while alive(found.pid, start_time):
            if monotonic() >= stop_by:
                return EnsureFailed("restart_timeout", f"pid {found.pid} did not stop in time")
            pause(_POLL_S)
        restarted = True
    spawned_at = time.time()
    try:
        pid = spawn(home, env, list(argv) if argv is not None else serve_argv())
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return EnsureFailed("spawn_failed", f"could not start the backend: {exc}")
    while True:
        found = discover(home, probe=probe, alive=alive)
        if found is not None and (found.pid == pid or _ours_on_windows(home, spawned_at)):
            return Served(
                url=found.url,
                port=found.port,
                pid=found.pid,
                profile_id=found.profile_id,
                code_version=found.code_version,
                started=True,
                restarted=restarted,
            )
        if not gateway_runtime.pid_alive(pid) and spawn is spawn_detached:
            return EnsureFailed(
                "exited", f"the backend exited before it answered; see {home / 'logs' / STDIO_LOG}"
            )
        if monotonic() >= deadline:
            return EnsureFailed("timeout", "the backend did not answer in time")
        pause(_POLL_S)
