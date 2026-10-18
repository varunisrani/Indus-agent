# pyright: strict
"""Finding the container client and learning what the host allows (P11-01).

Client: ``terminal.docker_binary``, then ``INDUS_DOCKER_BINARY`` from the scope's env, then
``docker`` on the scope's PATH, then ``podman``. Every CLI call goes through ``DockerCli`` with
its own timeout; the runner is injectable so tests answer in process.

Probes never pull an image (no network): ``version`` (10 s) says whether the daemon answers;
one ``info`` call (JSON) says whether cgroup limits work and which storage driver is in use; a
per-container disk quota needs overlay2 on XFS and is confirmed with a throwaway
``create --pull=never --storage-opt size=1m`` (never on macOS); ``image inspect`` says whether the
image boots its own s6-style ``/init``. Only definite answers are cached for the process.

``Availability`` is the backend row's probe. It answers from a short cache and keeps serving
"available" when a probe fails within ``FLAKE_GRACE_S`` of a success, so one slow ``docker
version`` under load does not take the terminal away mid-session (R02.13).
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast

from indus.backends.contract import BackendUnavailable, EnvironmentConnectionError
from indus.backends.docker.settings import DockerConfigError, settings_for
from indus.foundation.blocking import BlockingPool, run_blocking
from indus.foundation.clock import Clock
from indus.foundation.scope import Scope
from indus.platform import locate

__all__ = [
    "AVAILABILITY_TTL_S",
    "DOCKER_POOL",
    "FLAKE_GRACE_S",
    "Availability",
    "ClientInfo",
    "CliResult",
    "DockerCli",
    "DockerDaemonDown",
    "DoctorRow",
    "HostFacts",
    "Runner",
    "SyncRunner",
    "clear_caches",
    "doctor_rows",
    "ensure_daemon",
    "find_client",
    "host_facts",
    "image_present",
    "image_uses_s6_init",
    "start_hint",
]

log = logging.getLogger(__name__)

BINARY_ENV = "INDUS_DOCKER_BINARY"
VERSION_TIMEOUT_S = 10.0
INFO_TIMEOUT_S = 10.0
INSPECT_TIMEOUT_S = 15.0
STORAGE_PROBE_TIMEOUT_S = 15.0
FLAKE_GRACE_S = 60.0
AVAILABILITY_TTL_S = 30.0
S6_ENTRYPOINTS = ("/init", "/package/admin/s6-overlay/command/init")

# Docker CLI calls hold a worker for at most their timeout; their own pool keeps them from
# crowding the general ``io`` pool.
DOCKER_POOL = BlockingPool("docker", 8, queue_limit=64)

type RuntimeName = Literal["docker", "podman"]


# -- the client -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClientInfo:
    path: str
    runtime: RuntimeName

    @property
    def label(self) -> str:
        return "Podman" if self.runtime == "podman" else "Docker"


def runtime_of(path: str) -> RuntimeName:
    return "podman" if "podman" in os.path.basename(path).lower() else "docker"


def _executable(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _resolve(name: str, search: str | None) -> str | None:
    if os.sep in name:
        return name if _executable(name) else None
    return locate.which(name, path=search)


def find_client(scope: Scope, *, configured: str | None = None) -> ClientInfo | None:
    """The container client for ``scope`` (see the module notes), or None."""
    search = scope.env.get("PATH") or None
    if configured is None:
        raw = scope.config.get("terminal.docker_binary") if scope.config is not None else None
        configured = str(raw or "").strip()
    for source, wanted in (
        ("terminal.docker_binary", configured),
        (BINARY_ENV, (scope.env.get(BINARY_ENV) or "").strip()),
    ):
        if not wanted:
            continue
        found = _resolve(wanted, search)
        if found:
            return ClientInfo(found, runtime_of(found))
        log.warning("%s names %s, which is not an executable; looking on PATH", source, wanted)
    for name in ("docker", "podman"):
        found = locate.which(name, path=search)
        if found:
            if name == "podman":
                log.info("no docker client on PATH; using podman at %s", found)
            return ClientInfo(found, name)
    return None


def start_hint(client: ClientInfo, os_name: str = sys.platform) -> str:
    """How to bring the runtime up on ``os_name`` (``sys.platform`` values)."""
    if client.runtime == "podman":
        if os_name.startswith("linux"):
            return "start it with `systemctl --user start podman.socket`, then try again"
        return "start the Podman machine with `podman machine start`, then try again"
    if os_name == "darwin" or os_name.startswith("win"):
        return "open Docker Desktop and wait until it says it is running, then try again"
    return (
        "start the daemon with `sudo systemctl start docker` (and check that your user may use "
        "it: the `docker` group), then try again"
    )


# -- running the CLI ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CliResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    @property
    def text(self) -> str:
        """The client's own message: stderr, else stdout."""
        return (self.stderr.strip() or self.stdout.strip())[:2000]


type Runner = Callable[[Sequence[str], float, Mapping[str, str]], Awaitable[CliResult]]
type SyncRunner = Callable[[Sequence[str], float, Mapping[str, str]], CliResult]


def run_sync(argv: Sequence[str], timeout: float, env: Mapping[str, str]) -> CliResult:
    """One CLI call: text output, stdin closed (a prompt can never hang it), bounded."""
    try:
        done = subprocess.run(  # noqa: S603 - fixed client argv, no shell
            list(argv),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env=dict(env),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return CliResult(-1, "", f"no answer within {timeout:g}s", timed_out=True)
    except OSError as exc:
        return CliResult(127, "", f"could not run {argv[0]}: {exc.strerror or exc}")
    return CliResult(done.returncode, done.stdout or "", done.stderr or "")


async def run_async(argv: Sequence[str], timeout: float, env: Mapping[str, str]) -> CliResult:
    return await run_blocking(
        run_sync, list(argv), timeout, dict(env), pool=DOCKER_POOL, label="docker"
    )


class DockerCli:
    """The client plus the environment it runs with (values for name-only ``-e`` flags are added
    per call through ``extra_env``)."""

    def __init__(
        self,
        client: ClientInfo,
        env: Mapping[str, str],
        *,
        runner: Runner | None = None,
        sync_runner: SyncRunner | None = None,
    ) -> None:
        self.client = client
        self.env = dict(env)
        self._runner: Runner = runner if runner is not None else run_async
        self._sync: SyncRunner = sync_runner if sync_runner is not None else run_sync

    @property
    def binary(self) -> str:
        return self.client.path

    def argv(self, *args: str) -> list[str]:
        return [self.client.path, *args]

    def client_env(self, extra_env: Mapping[str, str] | None = None) -> dict[str, str]:
        return {**self.env, **(extra_env or {})}

    async def call(
        self, *args: str, timeout: float, extra_env: Mapping[str, str] | None = None
    ) -> CliResult:
        return await self._runner(self.argv(*args), timeout, self.client_env(extra_env))

    def call_sync(
        self, *args: str, timeout: float, extra_env: Mapping[str, str] | None = None
    ) -> CliResult:
        return self._sync(self.argv(*args), timeout, self.client_env(extra_env))


# -- the daemon -----------------------------------------------------------------------------------


class DockerDaemonDown(EnvironmentConnectionError):
    """The client is there but its daemon does not answer."""

    default_code = "backend.docker.daemon"


def _daemon_error(client: ClientInfo, result: CliResult, os_name: str) -> DockerDaemonDown:
    why = "did not answer in time" if result.timed_out else "is not reachable"
    detail = result.text.splitlines()[0] if result.text else ""
    reason = f"the {client.label} daemon {why}" + (f" ({detail})" if detail else "")
    return DockerDaemonDown(reason, retry_hint=start_hint(client, os_name).capitalize())


_VERSION_LINE = re.compile(r"^\s*Version:\s*(\S+)", re.MULTILINE)


def parse_version(text: str) -> str:
    found = _VERSION_LINE.search(text)
    return found.group(1) if found else ""


async def ensure_daemon(cli: DockerCli, *, os_name: str = sys.platform) -> str:
    """The client's version when its daemon answers; else ``DockerDaemonDown`` with a hint."""
    result = await cli.call("version", timeout=VERSION_TIMEOUT_S)
    if not result.ok:
        raise _daemon_error(cli.client, result, os_name)
    return parse_version(result.stdout)


# -- host facts -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HostFacts:
    """What the host lets ``docker run`` use."""

    cgroups: bool = False
    storage_quota: bool = False
    s6_init: bool = False


_cache_lock = threading.Lock()
_cache: dict[tuple[str, ...], bool] = {}


def clear_caches() -> None:
    """Forget every cached probe answer (tests, and after the user changes the runtime)."""
    with _cache_lock:
        _cache.clear()
    AVAILABILITY.clear()


def _cached(key: tuple[str, ...]) -> bool | None:
    with _cache_lock:
        return _cache.get(key)


def _remember(key: tuple[str, ...], value: bool) -> bool:
    with _cache_lock:
        _cache[key] = value
    return value


async def read_info(cli: DockerCli) -> dict[str, Any] | None:
    result = await cli.call("info", "--format", "{{json .}}", timeout=INFO_TIMEOUT_S)
    if not result.ok:
        log.debug("%s info failed: %s", cli.client.label, result.text)
        return None
    try:
        data: object = json.loads(result.stdout or "null")
    except ValueError:
        return None
    return cast(dict[str, Any], data) if isinstance(data, dict) else None


def cgroups_from_info(info: Mapping[str, Any]) -> bool | None:
    """True/False when ``info`` says whether cpu, memory and pids limits work; None when it does
    not say (an unknown shape)."""
    docker_keys = ("MemoryLimit", "PidsLimit", "CpuCfsQuota")
    if all(k in info for k in docker_keys):
        return all(bool(info[k]) for k in docker_keys)
    host = info.get("host")
    if isinstance(host, Mapping):
        controllers = cast(Mapping[str, Any], host).get("cgroupControllers")
        if isinstance(controllers, list):
            names = {str(c) for c in cast(list[object], controllers)}
            return {"cpu", "memory", "pids"} <= names
    return None


def storage_from_info(info: Mapping[str, Any]) -> tuple[str, str]:
    """(storage driver, backing file system), lower case; empty when unknown."""
    driver = str(info.get("Driver") or "")
    backing = ""
    status = info.get("DriverStatus")
    if isinstance(status, list):
        for pair in cast(list[object], status):
            if isinstance(pair, list | tuple):
                row = cast(Sequence[object], pair)
                if len(row) == 2 and str(row[0]).lower() == "backing filesystem":
                    backing = str(row[1])
    store = info.get("store")
    if not driver and isinstance(store, Mapping):
        store_map = cast(Mapping[str, Any], store)
        driver = str(store_map.get("graphDriverName") or "")
        graph = store_map.get("graphStatus")
        if isinstance(graph, Mapping):
            backing = str(cast(Mapping[str, Any], graph).get("Backing Filesystem") or "")
    return driver.lower(), backing.lower()


async def _storage_quota(
    cli: DockerCli, image: str, info: Mapping[str, Any] | None, os_name: str
) -> bool:
    if os_name == "darwin":
        return False
    key = ("storage", cli.binary)
    known = _cached(key)
    if known is not None:
        return known
    if info is None:
        return False  # not cached: the daemon may answer next time
    driver, backing = storage_from_info(info)
    if driver not in ("overlay2", "overlay") or backing != "xfs":
        return _remember(key, False)
    probe = await cli.call(
        "create",
        "--pull=never",
        "--storage-opt",
        "size=1m",
        image,
        "true",
        timeout=STORAGE_PROBE_TIMEOUT_S,
    )
    if probe.ok:
        made = probe.stdout.strip()
        if made:
            await cli.call("rm", "-f", made, timeout=STORAGE_PROBE_TIMEOUT_S)
        return _remember(key, True)
    if "storage" in probe.text.lower():
        return _remember(key, False)  # the daemon refused the option: a host property
    return False  # image missing or daemon trouble says nothing; try again next time


async def image_uses_s6_init(cli: DockerCli, image: str) -> bool | None:
    """Whether ``image`` boots an s6-style ``/init`` as PID 1; None when it cannot be inspected
    (not pulled yet), which callers treat as "no"."""
    key = ("s6", cli.binary, image)
    known = _cached(key)
    if known is not None:
        return known
    result = await cli.call(
        "image",
        "inspect",
        "--format",
        "{{json .Config.Entrypoint}}",
        image,
        timeout=INSPECT_TIMEOUT_S,
    )
    if not result.ok:
        return None
    raw = result.stdout.strip()
    try:
        entry: object = json.loads(raw) if raw else None
    except ValueError:
        return None
    if isinstance(entry, str):
        entry = [entry]
    first = ""
    if isinstance(entry, list) and entry:
        first = str(cast(list[object], entry)[0]).strip()
    return _remember(key, first in S6_ENTRYPOINTS)


async def image_present(cli: DockerCli, image: str) -> bool:
    result = await cli.call(
        "image", "inspect", "--format", "{{.Id}}", image, timeout=INSPECT_TIMEOUT_S
    )
    return result.ok


async def host_facts(cli: DockerCli, image: str, *, os_name: str = sys.platform) -> HostFacts:
    """The facts ``docker run`` needs (see the module notes)."""
    cg_key = ("cgroups", cli.binary)
    cgroups = _cached(cg_key)
    info: dict[str, Any] | None = None
    if cgroups is None or _cached(("storage", cli.binary)) is None:
        info = await read_info(cli)
    if cgroups is None:
        seen = cgroups_from_info(info) if info is not None else None
        if seen is None:
            log.warning(
                "could not tell whether %s can limit CPU, memory and processes here; the "
                "sandbox runs without those limits for now",
                cli.client.label,
            )
            cgroups = False
        else:
            cgroups = _remember(cg_key, seen)
            if not seen:
                log.warning(
                    "cgroup limits are not available to %s here; sandboxes run without CPU, "
                    "memory and process limits",
                    cli.client.label,
                )
    quota = await _storage_quota(cli, image, info, os_name)
    s6 = bool(await image_uses_s6_init(cli, image))
    return HostFacts(cgroups=cgroups, storage_quota=quota, s6_init=s6)


# -- availability (the backend row's probe) -------------------------------------------------------


def quick_check(scope: Scope, *, sync_runner: SyncRunner | None = None) -> str | None:
    """Why Docker cannot run for ``scope`` right now, or None. Blocking (``version``, 10 s)."""
    try:
        settings = settings_for(scope)
    except DockerConfigError as exc:
        return str(exc)
    client = find_client(scope, configured=settings.binary)
    if client is None:
        return (
            "no docker or podman client was found on PATH; install Docker or Podman, or set "
            "terminal.docker_binary"
        )
    cli = DockerCli(client, _probe_env(scope), sync_runner=sync_runner)
    result = cli.call_sync("version", timeout=VERSION_TIMEOUT_S)
    if not result.ok:
        error = _daemon_error(client, result, sys.platform)
        return f"{error.reason}; {error.retry_hint}"
    return None


def _probe_env(scope: Scope) -> dict[str, str]:
    from indus.backends.docker.env import client_env

    return client_env(scope)


class Availability:
    """A probe with a short cache and the flake grace (see the module notes)."""

    def __init__(
        self,
        check: Callable[[Scope], str | None],
        *,
        clock: Clock | None = None,
        ttl_s: float = AVAILABILITY_TTL_S,
        grace_s: float = FLAKE_GRACE_S,
    ) -> None:
        self.check = check
        self.clock = clock
        self.ttl_s = ttl_s
        self.grace_s = grace_s
        self._lock = threading.Lock()
        self._answers: dict[str, tuple[float, str | None]] = {}
        self._last_ok: dict[str, float] = {}

    def clear(self) -> None:
        with self._lock:
            self._answers.clear()
            self._last_ok.clear()

    def __call__(self, scope: Scope) -> str | None:
        clock = self.clock if self.clock is not None else scope.clock
        key = str(scope.home)
        now = clock.monotonic()
        with self._lock:
            hit = self._answers.get(key)
            if hit is not None and now - hit[0] < self.ttl_s:
                return hit[1]
        try:
            problem = self.check(scope)
        except Exception as exc:  # a probe must answer, never raise
            problem = f"the Docker check failed: {type(exc).__name__}: {exc}"
        with self._lock:
            if problem is None:
                self._last_ok[key] = now
                self._answers[key] = (now, None)
                return None
            last = self._last_ok.get(key)
            if last is not None and now - last < self.grace_s:
                log.warning(
                    "Docker check failed %.0f s after it passed (%s); treated as a flake",
                    now - last,
                    problem,
                )
                return None
            self._answers[key] = (now, problem)
            return problem


AVAILABILITY = Availability(quick_check)


def docker_probe(scope: Scope) -> str | None:
    """The registry row's probe."""
    return AVAILABILITY(scope)


def require_client(scope: Scope, configured: str) -> ClientInfo:
    client = find_client(scope, configured=configured)
    if client is None:
        raise BackendUnavailable(
            "no docker or podman client was found on PATH",
            hint="install Docker or Podman, or set terminal.docker_binary",
        )
    return client


# -- doctor ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DoctorRow:
    id: str
    severity: Literal["ok", "info", "warn", "error"]
    message: str
    fix: str | None = None


def doctor_rows(
    scope: Scope, *, sync_runner: SyncRunner | None = None, os_name: str = sys.platform
) -> list[DoctorRow]:
    """``indus doctor`` rows for the Docker backend: client, version, daemon, image."""
    try:
        settings = settings_for(scope)
    except DockerConfigError as exc:
        return [DoctorRow("backend.docker", "error", str(exc), exc.hint)]
    client = find_client(scope, configured=settings.binary)
    if client is None:
        return [
            DoctorRow(
                "backend.docker",
                "error",
                "terminal.backend is docker but no docker or podman client was found",
                "install Docker or Podman, or set terminal.docker_binary",
            )
        ]
    cli = DockerCli(client, _probe_env(scope), sync_runner=sync_runner)
    version = cli.call_sync("version", timeout=VERSION_TIMEOUT_S)
    if not version.ok:
        error = _daemon_error(client, version, os_name)
        return [
            DoctorRow("backend.docker", "ok", f"{client.label} client: {client.path}"),
            DoctorRow("backend.docker.daemon", "error", error.reason, error.retry_hint),
        ]
    number = parse_version(version.stdout) or "unknown version"
    rows = [
        DoctorRow(
            "backend.docker",
            "ok",
            f"{client.label} {number} at {client.path}; the daemon answers",
        )
    ]
    image = cli.call_sync(
        "image", "inspect", "--format", "{{.Id}}", settings.image, timeout=INSPECT_TIMEOUT_S
    )
    if image.ok:
        rows.append(DoctorRow("backend.docker.image", "ok", f"sandbox image {settings.image}"))
    else:
        rows.append(
            DoctorRow(
                "backend.docker.image",
                "info",
                f"sandbox image {settings.image} is not pulled yet (pulled on first use)",
                f"{client.runtime} pull {settings.image}",
            )
        )
    return rows
