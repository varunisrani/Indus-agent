"""Who owns the Photon sidecar process (P16-25); the sidecar itself is ``apps/photon-sidecar``.

One profile, one sidecar. Its state lives in ``<home>/platforms/photon/<profile id>/`` (0700):
``sidecar.pid`` (JSON: pid, the kernel start time of that pid, port, profile), ``sidecar.token``
(0600, the bearer token of the running sidecar, fresh for every start) and ``sidecar.log`` (0600,
the sidecar's own output: lifecycle lines only).

``PhotonSidecar.ensure`` needs a provider module and the project credentials (else a fatal
``photon_provider_missing`` / ``photon_credentials_missing`` before anything runs), heals the
provider package's dependencies (``DependencyHealer``), then adopts this profile's sidecar when the
pidfile names a live process with the recorded start time, on the port wanted, answering
``/health`` with our token and our profile. Anything else on the port is not ours: it is never
adopted and never signalled (``photon_sidecar_port_taken``, fatal). Otherwise a fresh sidecar is
spawned (``node <script>``, environment from ``safety.child_env`` minimal plus the
``INDUS_PHOTON_*`` values and the two vendor credential names, nothing secret in argv, its own
process group) and waited for until ``/health`` answers (15 s). ``attach`` is the send-only path:
the running sidecar of this profile or nothing. ``stop`` ends only a sidecar this owner started or
adopted (TERM, then KILL after 3 s) and removes the pidfile and token.

Dependencies: no Spectrum client ships (docs/notes/P16/photon.md); the operator's provider usually
needs packages of its own. When the provider is a path, its package folder (the nearest folder
above it with a ``package.json``, at most 6 levels) is healed before a start: a lock file with a
missing or older ``node_modules`` runs ``npm ci --ignore-scripts --no-audit --no-fund`` there,
bounded to 600 s (a timeout kills it and is retryable; a failure is fatal).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import signal
import subprocess
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx

from indus.foundation.clock import Clock
from indus.gateway.adapters.whatsapp.sidecar import (
    Child,
    PidRecord,
    port_free,
    spawn_process,
)
from indus.state.gateway_runtime import pid_alive, process_start_time

__all__ = [
    "DEFAULT_PORT",
    "NPM_TIMEOUT_S",
    "DependencyHealer",
    "Health",
    "PhotonSidecar",
    "SidecarEndpoint",
    "SidecarError",
    "http_probe",
    "needs_install",
    "provider_package",
    "run_npm",
    "state_dir",
]

log = logging.getLogger(__name__)

DEFAULT_PORT = 8789
SPAWN_WAIT_S = 15.0
PROBE_EVERY_S = 0.5
STOP_GRACE_S = 3.0
NPM_TIMEOUT_S = 600.0
PACKAGE_DEPTH = 6
_LOG_TAIL = 8192

PROJECT_ID_ENV = "PHOTON_PROJECT_ID"
PROJECT_SECRET_ENV = "PHOTON_PROJECT_SECRET"

# The sidecar's own "why I exited at start" codes (its stdout, in our log file).
_EXIT_READINGS: Mapping[str, tuple[str, str]] = {
    "no_provider": (
        "photon_provider_missing",
        "no Photon provider is configured: set platforms.photon.provider (or "
        "INDUS_PHOTON_PROVIDER) to a module that exports createSpectrumLink",
    ),
    "provider": (
        "photon_provider_failed",
        "the Photon provider module could not be loaded or does not export createSpectrumLink",
    ),
    "credentials": (
        "photon_credentials_missing",
        "PHOTON_PROJECT_ID and PHOTON_PROJECT_SECRET are both needed",
    ),
}


def state_dir(home: Path, profile_id: str) -> Path:
    return home / "platforms" / "photon" / profile_id


class SidecarError(Exception):
    """The sidecar could not be started or reached; ``retryable`` false is a configuration
    problem the operator must fix."""

    def __init__(self, message: str, *, code: str, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class SidecarEndpoint:
    url: str
    port: int
    token: str = field(repr=False)


@dataclass(frozen=True)
class Health:
    """What ``/health`` said: ``ok`` (with the body), ``unauthorized`` (our token refused: not our
    sidecar) or ``unreachable`` (nothing answered, or not a sidecar)."""

    state: Literal["ok", "unauthorized", "unreachable"]
    status: str = ""
    profile: str = ""
    epoch: str = ""


type ProbeFn = Callable[[str, str], Awaitable[Health]]
type Spawner = Callable[[Sequence[str], Mapping[str, str], Path], Awaitable[Child]]
# argv, folder, env, timeout → the exit code, or None when it was stopped at the timeout.
type NpmRunner = Callable[[Sequence[str], Path, Mapping[str, str], float], Awaitable[int | None]]


def _dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}  # pyright: ignore[reportUnknownArgumentType]


def http_probe(transport: httpx.AsyncBaseTransport | None = None) -> ProbeFn:
    """``/health`` of a sidecar at ``url`` with ``token`` (see `Health`)."""

    async def probe(url: str, token: str) -> Health:
        async with httpx.AsyncClient(
            transport=transport or httpx.AsyncHTTPTransport(),
            trust_env=False,
            timeout=httpx.Timeout(3.0),
        ) as client:
            try:
                response = await client.get(
                    f"{url}/health", headers={"Authorization": f"Bearer {token}"}
                )
            except httpx.HTTPError:
                return Health("unreachable")
            if response.status_code == 401:
                return Health("unauthorized")
            if response.status_code != 200:
                return Health("unreachable")
            try:
                body = _dict(response.json())
            except ValueError:
                return Health("unreachable")
            return Health(
                "ok",
                status=str(body.get("status") or ""),
                profile=str(body.get("profile") or ""),
                epoch=str(body.get("epoch") or ""),
            )

    return probe


# -- dependency self-heal ----------------------------------------------------------------------


def provider_package(spec: str) -> Path | None:
    """The package folder of a provider given as a path (nearest ``package.json`` above it)."""
    text = spec.strip()
    if not text or not os.path.isabs(text):
        return None
    folder = Path(text).parent
    for _ in range(PACKAGE_DEPTH):
        if (folder / "package.json").is_file():
            return folder
        if folder.parent == folder:
            break
        folder = folder.parent
    return None


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def needs_install(folder: Path) -> bool:
    """A lock file whose packages are missing, or older than it (an update without an install)."""
    lock = next(
        (p for p in (folder / "package-lock.json", folder / "npm-shrinkwrap.json") if p.is_file()),
        None,
    )
    if lock is None:
        return False
    modules = folder / "node_modules"
    if not modules.is_dir():
        return True
    installed = _mtime(modules / ".package-lock.json") or _mtime(modules)
    locked = _mtime(lock)
    return installed is None or (locked is not None and locked > installed)


def _stop_group(proc: asyncio.subprocess.Process, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, sig)


async def run_npm(
    argv: Sequence[str], folder: Path, env: Mapping[str, str], timeout: float
) -> int | None:
    """Run ``argv`` in ``folder``, output discarded; None when it was stopped at ``timeout``."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(folder),
        env=dict(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        return await asyncio.wait_for(proc.wait(), timeout)
    except TimeoutError:
        _stop_group(proc, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), 5.0)
        except TimeoutError:
            _stop_group(proc, signal.SIGKILL)
            await proc.wait()
        return None
    except asyncio.CancelledError:
        _stop_group(proc, signal.SIGKILL)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), 5.0)
        raise


class DependencyHealer:
    """``npm ci`` for a provider package whose installed packages are missing or stale."""

    def __init__(
        self,
        *,
        npm: str | None,
        env_for: Callable[[dict[str, str]], dict[str, str]],
        runner: NpmRunner = run_npm,
        timeout: float = NPM_TIMEOUT_S,
    ) -> None:
        self.npm = npm
        self.env_for = env_for
        self.runner = runner
        self.timeout = timeout
        self.runs = 0

    async def heal(self, folder: Path) -> None:
        if not needs_install(folder):
            return
        if not self.npm:
            raise SidecarError(
                f"the Photon provider's packages in {folder} are not installed and npm was not "
                "found; run `npm ci` there",
                code="photon_npm_missing",
                retryable=False,
            )
        log.info("photon: installing the provider's packages (npm ci) in %s", folder)
        self.runs += 1
        argv = [self.npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund"]
        code = await self.runner(argv, folder, self.env_for({}), self.timeout)
        if code is None:
            raise SidecarError(
                f"npm ci in {folder} did not finish within {self.timeout:.0f} s; stopped",
                code="photon_deps_timeout",
                retryable=True,
            )
        if code != 0 or needs_install(folder):
            raise SidecarError(
                f"npm ci in {folder} failed (exit {code}); run it there by hand to see why",
                code="photon_deps_failed",
                retryable=False,
            )


# -- the process owner -------------------------------------------------------------------------


def _signal_pid(pid: int, sig: int) -> None:
    """Signal the sidecar's process group when it leads one (we start it in its own session),
    else the pid alone."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        if os.getpgid(pid) == pid:
            os.killpg(pid, sig)
        else:
            os.kill(pid, sig)


def _write_private(path: Path, text: str) -> None:
    """Replace ``path`` with ``text``, owner-only, never through a symlink."""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, path)


def exit_reason(log_file: Path) -> str:
    """The last ``{"event": "error"}`` code the sidecar printed, else ""."""
    try:
        with log_file.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(size - _LOG_TAIL, 0))
            tail = handle.read().decode("utf-8", "replace")
    except OSError:
        return ""
    found = ""
    for line in tail.splitlines():
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("event") == "error":  # pyright: ignore[reportUnknownMemberType]
            found = str(data.get("error") or "")  # pyright: ignore[reportUnknownMemberType]
    return found


class PhotonSidecar:
    """Starts, adopts, watches and stops this profile's sidecar (see the module notes)."""

    def __init__(
        self,
        *,
        home: Path,
        profile_id: str,
        clock: Clock,
        probe: ProbeFn,
        node: str | None,
        script: Path | None,
        env_for: Callable[[dict[str, str]], dict[str, str]],
        port: int = DEFAULT_PORT,
        provider: str = "",
        project_id: str = "",
        project_secret: str = "",
        healer: DependencyHealer | None = None,
        spawner: Spawner = spawn_process,
        start_time: Callable[[int], int | None] = process_start_time,
        alive_check: Callable[[int], bool] | None = None,
        kill: Callable[[int, int], None] | None = None,
        is_free: Callable[[int], bool] = port_free,
        token_factory: Callable[[], str] = lambda: secrets.token_urlsafe(32),
        parent_pid: Callable[[], int] = os.getpid,
    ) -> None:
        if not 1 <= port <= 65535:
            raise SidecarError(
                f"platforms.photon.sidecar_port {port} is not a port",
                code="config",
                retryable=False,
            )
        self.home = home
        self.profile_id = profile_id
        self.clock = clock
        self.probe = probe
        self.node = node
        self.script = script
        self.env_for = env_for
        self.port = port
        self.provider = provider.strip()
        self.project_id = project_id
        self._project_secret = project_secret
        self.healer = healer
        self._spawner = spawner
        self._start_time = start_time
        self._pid_alive = alive_check or pid_alive
        self._kill = kill or _signal_pid
        self._is_free = is_free
        self._token_factory = token_factory
        self._parent_pid = parent_pid
        self.state = state_dir(home, profile_id)
        self.pidfile = self.state / "sidecar.pid"
        self.token_file = self.state / "sidecar.token"
        self.log_file = self.state / "sidecar.log"
        self._child: Child | None = None
        self._adopted: PidRecord | None = None
        self.spawns = 0

    def __repr__(self) -> str:
        return f"<PhotonSidecar {self.profile_id} pid={self.pid}>"

    @property
    def pid(self) -> int | None:
        if self._child is not None:
            return self._child.pid
        return self._adopted.pid if self._adopted is not None else None

    @staticmethod
    def url(port: int) -> str:
        return f"http://127.0.0.1:{port}"

    # -- identity ----------------------------------------------------------------------------------

    def _ours(self, record: PidRecord) -> bool:
        """The recorded pid is alive and still the process we started (same kernel start time);
        without a recorded start time nothing proves it, so it is not ours."""
        if record.profile != self.profile_id or record.start is None:
            return False
        return self._pid_alive(record.pid) and self._start_time(record.pid) == record.start

    def _token(self) -> str | None:
        try:
            text = self.token_file.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return text or None

    def _forget(self) -> None:
        for path in (self.pidfile, self.token_file):
            with contextlib.suppress(OSError):
                path.unlink()

    async def _healthy(self, port: int, token: str) -> Health:
        try:
            return await self.probe(self.url(port), token)
        except Exception as exc:
            log.debug("photon: sidecar probe failed: %s", type(exc).__name__)
            return Health("unreachable")

    # -- lifecycle ---------------------------------------------------------------------------------

    def _check_settings(self) -> tuple[str, Path]:
        if not self.provider:
            code, message = _EXIT_READINGS["no_provider"]
            raise SidecarError(message, code=code, retryable=False)
        if not self.project_id or not self._project_secret:
            code, message = _EXIT_READINGS["credentials"]
            raise SidecarError(message, code=code, retryable=False)
        if not self.node:
            raise SidecarError(
                "Node.js was not found on PATH; Photon needs it to run its sidecar",
                code="photon_node_missing",
                retryable=False,
            )
        if self.script is None or not self.script.is_file():
            raise SidecarError(
                "the Photon sidecar script was not found; set platforms.photon.sidecar_script",
                code="photon_sidecar_missing",
                retryable=False,
            )
        return self.node, self.script

    def _taken(self) -> SidecarError:
        return SidecarError(
            f"port {self.port} is held by a process this profile does not own; nothing there was "
            "stopped. Give this profile its own platforms.photon.sidecar_port or stop that process",
            code="photon_sidecar_port_taken",
            retryable=False,
        )

    async def ensure(self) -> SidecarEndpoint:
        """This profile's sidecar, adopted or freshly started (see the module notes)."""
        node, script = self._check_settings()
        package = provider_package(self.provider)
        if package is not None and self.healer is not None:
            await self.healer.heal(package)
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self.state, 0o700)
        record = PidRecord.read(self.pidfile)
        if record is not None and self._ours(record):
            token = self._token()
            health = await self._healthy(record.port, token) if token else Health("unreachable")
            if (
                record.port == self.port
                and health.state == "ok"
                and health.profile == self.profile_id
            ):
                assert token is not None
                self._adopted = record
                log.info("photon: adopted the running sidecar (pid %d)", record.pid)
                return SidecarEndpoint(self.url(self.port), self.port, token)
            log.info("photon: replacing this profile's stale sidecar (pid %d)", record.pid)
            self._adopted = record
            await self.stop()
        elif record is not None:
            self._forget()
        if not self._is_free(self.port):
            raise self._taken()
        return await self._spawn(node, script)

    def _environment(self, token: str) -> dict[str, str]:
        return self.env_for(
            {
                "INDUS_PHOTON_TOKEN": token,
                "INDUS_PHOTON_HOST": "127.0.0.1",
                "INDUS_PHOTON_PORT": str(self.port),
                "INDUS_PHOTON_PROFILE": self.profile_id,
                "INDUS_PHOTON_PARENT_PID": str(self._parent_pid()),
                "INDUS_PHOTON_PROVIDER": self.provider,
                PROJECT_ID_ENV: self.project_id,
                PROJECT_SECRET_ENV: self._project_secret,
            }
        )

    async def _spawn(self, node: str, script: Path) -> SidecarEndpoint:
        token = self._token_factory()
        _write_private(self.token_file, token + "\n")
        fd = os.open(self.log_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.close(fd)
        child = await self._spawner([node, str(script)], self._environment(token), self.log_file)
        self._child = child
        self._adopted = None
        self.spawns += 1
        record = PidRecord(child.pid, self._start_time(child.pid), self.port, self.profile_id)
        _write_private(self.pidfile, record.to_json())
        waited = 0.0
        while True:
            if child.returncode is not None:
                code = child.returncode
                self._child = None
                self._forget()
                reason = exit_reason(self.log_file)
                if reason in _EXIT_READINGS:
                    error, message = _EXIT_READINGS[reason]
                    raise SidecarError(message, code=error, retryable=False)
                if code == 2:
                    raise SidecarError(
                        f"the Photon sidecar refused its settings ({reason or 'exit 2'}); see "
                        f"{self.log_file}",
                        code="photon_sidecar_config",
                        retryable=False,
                    )
                raise SidecarError(
                    f"the Photon sidecar exited at start (code {code}); see {self.log_file}",
                    code="photon_sidecar_exited",
                    retryable=True,
                )
            health = await self._healthy(self.port, token)
            if health.state == "ok" and health.profile == self.profile_id:
                log.info("photon: sidecar started on port %d (pid %d)", self.port, child.pid)
                return SidecarEndpoint(self.url(self.port), self.port, token)
            if health.state == "unauthorized" or (health.state == "ok" and health.profile):
                await self.stop()
                raise self._taken()
            if waited >= SPAWN_WAIT_S:
                await self.stop()
                raise SidecarError(
                    f"the Photon sidecar did not answer within {SPAWN_WAIT_S:.0f} s",
                    code="photon_sidecar_timeout",
                    retryable=True,
                )
            await self.clock.sleep(PROBE_EVERY_S)
            waited += PROBE_EVERY_S

    async def attach(self) -> SidecarEndpoint:
        """The running sidecar of this profile, for a send-only adapter; never starts one (a
        temporary sidecar would take this profile's inbound messages)."""
        record = PidRecord.read(self.pidfile)
        token = self._token()
        if record is not None and token and self._ours(record):
            health = await self._healthy(record.port, token)
            if health.state == "ok" and health.profile == self.profile_id:
                return SidecarEndpoint(self.url(record.port), record.port, token)
        raise SidecarError(
            "the Photon sidecar of this profile is not running; start the gateway first",
            code="photon_sidecar_not_running",
            retryable=False,
        )

    def exited(self) -> bool:
        """The sidecar this owner started or adopted is gone."""
        if self._child is not None:
            return self._child.returncode is not None
        if self._adopted is not None:
            return not self._ours(self._adopted)
        return False

    async def restart(self) -> SidecarEndpoint:
        await self.stop()
        return await self.ensure()

    async def stop(self) -> None:
        """End the sidecar this owner started or adopted; forget its pidfile and token."""
        child, adopted = self._child, self._adopted
        self._child = self._adopted = None
        if child is None and adopted is None:
            return
        if child is not None:
            started = child

            def gone() -> bool:
                return started.returncode is not None

            def send(sig: int) -> None:
                if isinstance(started, asyncio.subprocess.Process):
                    self._kill(started.pid, sig)
                else:
                    started.send_signal(sig)
        else:
            assert adopted is not None
            record = adopted

            def gone() -> bool:
                return not self._ours(record)

            def send(sig: int) -> None:
                self._kill(record.pid, sig)

        if not gone():
            send(signal.SIGTERM)
            waited = 0.0
            while not gone() and waited < STOP_GRACE_S:
                await self.clock.sleep(0.1)
                waited += 0.1
            if not gone():
                log.warning("photon: the sidecar ignored SIGTERM; killing it")
                send(signal.SIGKILL)
        if child is not None and isinstance(child, asyncio.subprocess.Process):
            with contextlib.suppress(Exception):
                await asyncio.wait_for(child.wait(), 2.0)
        self._forget()
