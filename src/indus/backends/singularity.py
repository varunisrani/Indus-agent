# pyright: strict
"""The Singularity / Apptainer terminal backend (P21-04).

The CLI comes from the profile's PATH: ``apptainer`` first, then ``singularity`` (absent →
"Apptainer or Singularity not found"). The image is ``terminal.singularity_image`` or
``docker://<terminal.docker_image>``; an image reference (``docker://``, ``oras://``, …) is built
once into ``<scratch>/sif-cache/<sha256(image)[:16]>.sif`` with ``build --force`` under a file lock
(one build per image across processes, 600 s), built to a temp name and renamed so a half-built
file is never taken for a cached one. A local ``.sif`` path is used as is. The scratch dir is
``terminal.scratch_dir`` or ``<home>/sandboxes/singularity``; its usage is checked when an
environment opens and one warning is logged per process when it passes
``terminal.disk_warning_gb``.

Each environment starts one instance, ``indus_<12 hex>``, with ``instance start --containall
--no-home`` (no host home, private /tmp, clean environment), ``--cpus`` / ``--memory`` when the
host's cgroups allow them (a start that fails on cgroups is retried once without), read-only binds
of the profile's credential files and skills, and either ``--writable-tmpfs`` or, when
``terminal.container_persistent`` is on, ``--overlay <scratch>/overlays/<task>`` (its path kept in
the P21-01 records store and reused next time). The instance mounts no host path read-write, so it
is isolated and dangerous-command prompts are relaxed (R02.6).

Commands go through the shared P05 shell flow (``SpawnedEnvironment``): a fresh ``<cli> exec
instance://<name> bash -c <wrapper>`` per command, stdin piped, the client in its own process group
so the ordered kill reaches the command. Forwarded variables travel as ``APPTAINERENV_<NAME>`` (or
``SINGULARITYENV_`` for the singularity CLI) in the client's environment, never in argv (R02.7), and
are kept out of the snapshot (R02.1). An instance stopped outside Indus surfaces as a connection
error, so the registry starts a fresh one. Cleanup removes the snapshot from a persistent
instance's /tmp, stops the instance (30 s) and records the overlay.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import secrets
import subprocess
import threading
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from indus.backends.capture import DEFAULT_MAX_CHARS
from indus.backends.contract import (
    ActivityCallback,
    BackendError,
    BackendFlags,
    BackendUnavailable,
    CancelSignal,
    Environment,
    EnvironmentConnectionError,
    ExecResult,
)
from indus.backends.docker.mounts import (
    ReadonlyMount,
    credential_mounts,
    readonly_from_sources,
    sandbox_dir_name,
)
from indus.backends.docker.probe import CliResult
from indus.backends.local import SURVIVOR_WINDOW_S, LocalProcess, force_kill_tree, stop_tree
from indus.backends.sandbox_records import SandboxRecords
from indus.backends.spawned import SNAPSHOT_TIMEOUT_S, SpawnedEnvironment
from indus.backends.vendor_env import passthrough_plan, unset_line
from indus.backends.wait import ProcessHandle
from indus.foundation.blocking import BlockingPool, run_blocking
from indus.foundation.clock import Clock
from indus.foundation.config.schema import default_for
from indus.foundation.fsio import FileLock, LockTimeout
from indus.foundation.scope import Scope
from indus.platform import locate
from indus.safety.child_env import child_env

if TYPE_CHECKING:
    from indus.backends.registry import BackendEntry, CreateRequest

__all__ = [
    "BUILD_TIMEOUT_S",
    "CLI_NAMES",
    "NOT_FOUND_REASON",
    "SINGULARITY_FLAGS",
    "START_TIMEOUT_S",
    "STOP_TIMEOUT_S",
    "SingularityBuildError",
    "SingularityCli",
    "SingularityEnvironment",
    "SingularitySettings",
    "ensure_sif",
    "find_cli",
    "open_singularity",
    "settings_for",
    "sif_path",
    "singularity_entry",
    "singularity_probe",
    "start_argv",
    "warn_disk_usage",
]

log = logging.getLogger(__name__)

CLI_NAMES = ("apptainer", "singularity")
NOT_FOUND_REASON = "Apptainer or Singularity not found"
VERSION_TIMEOUT_S = 10.0
BUILD_TIMEOUT_S = 600.0
BUILD_LOCK_GRACE_S = 60.0
START_TIMEOUT_S = 120.0
STOP_TIMEOUT_S = 30.0
INSIDE_TIMEOUT_S = 5.0
STDERR_TAIL = 500
GIB = 1024**3
RECORD_KIND = "singularity"
SHARED_TASK = "shared"
SANDBOX_TEMP = "/tmp"  # noqa: S108 - the instance's private /tmp (--containall)
SKILLS_TARGET = "/root/.indus/skills"
IMAGE_SCHEMES = (
    "docker://",
    "docker-archive:",
    "docker-daemon:",
    "oci:",
    "oci-archive:",
    "oras://",
    "library://",
    "shub://",
    "http://",
    "https://",
)
CLIENT_PREFIXES = ("APPTAINER_", "SINGULARITY_")
CLIENT_NAMES = (
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)
SINGULARITY_FLAGS = BackendFlags(
    isolated=True, container_paths=True, stdin_mode="pipe", host_local=False
)

# CLI calls (version, start, stop, the in-instance cleanup) and SIF builds; a stuck build keeps
# its worker until the build timeout kills it.
CLI_POOL = BlockingPool("singularity", 4, queue_limit=32)

_warned_lock = threading.Lock()
_warned_scratch: set[str] = set()


class SingularityBuildError(BackendError):
    """The SIF image could not be built (bad image, registry trouble, timeout)."""

    default_code = "backend.singularity.build"


# -- settings -------------------------------------------------------------------------------------


def _config(scope: Scope, key: str) -> Any:
    value = scope.config.get(key) if scope.config is not None else None
    return default_for(key) if value is None else value


def _flag(raw: object) -> bool:
    if isinstance(raw, str):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return bool(raw)


@dataclass(frozen=True, slots=True)
class SingularitySettings:
    image: str
    scratch_dir: Path
    disk_warning_gb: float = 500.0
    cpu: float = 1.0
    memory_mb: int = 5120
    persistent: bool = True


def settings_for(scope: Scope) -> SingularitySettings:
    """The profile's Singularity settings (schema defaults behind the config)."""
    image = str(_config(scope, "terminal.singularity_image") or "").strip()
    if not image:
        image = f"docker://{str(_config(scope, 'terminal.docker_image')).strip()}"
    scratch_raw = str(_config(scope, "terminal.scratch_dir") or "").strip()
    if scratch_raw:
        home = scope.env.get("HOME") or ""
        if home and (scratch_raw == "~" or scratch_raw.startswith("~/")):
            scratch_raw = home + scratch_raw[1:]
        scratch = Path(scratch_raw)
    else:
        scratch = scope.home / "sandboxes" / "singularity"
    try:
        warning = float(_config(scope, "terminal.disk_warning_gb"))
    except (TypeError, ValueError):
        warning = 500.0
    return SingularitySettings(
        image=image,
        scratch_dir=scratch,
        disk_warning_gb=warning,
        cpu=float(_config(scope, "terminal.container_cpu") or 0),
        memory_mb=int(_config(scope, "terminal.container_memory_mb") or 0),
        persistent=_flag(_config(scope, "terminal.container_persistent")),
    )


def persistent_per_profile(scope: Scope) -> bool:
    """A persistent instance serves the whole profile (one environment key)."""
    return _flag(_config(scope, "terminal.container_persistent"))


# -- the CLI --------------------------------------------------------------------------------------


def find_cli(scope: Scope) -> str | None:
    """``apptainer`` on the scope's PATH, else ``singularity``, else None."""
    search = scope.env.get("PATH") or None
    for name in CLI_NAMES:
        found = locate.which(name, path=search)
        if found:
            return found
    return None


def singularity_probe(scope: Scope) -> str | None:
    """Why this backend cannot run here, or None (no subprocess, no network)."""
    return None if find_cli(scope) else NOT_FOUND_REASON


def _tail(text: str | bytes | None) -> str:
    if text is None:
        return ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    return text.strip()[-STDERR_TAIL:]


def run_cli_sync(argv: Sequence[str], timeout: float, env: Mapping[str, str]) -> CliResult:
    """One CLI call with stdin closed; a timeout keeps the stderr printed so far."""
    try:
        done = subprocess.run(  # noqa: S603 - fixed CLI argv, no shell
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
    except subprocess.TimeoutExpired as exc:
        return CliResult(-1, _tail(exc.stdout), _tail(exc.stderr), timed_out=True)
    except OSError as exc:
        return CliResult(127, "", f"could not run {argv[0]}: {exc.strerror or exc}")
    return CliResult(done.returncode, done.stdout or "", done.stderr or "")


type Runner = Callable[[Sequence[str], float, Mapping[str, str]], Awaitable[CliResult]]
type SyncRunner = Callable[[Sequence[str], float, Mapping[str, str]], CliResult]


async def run_cli(argv: Sequence[str], timeout: float, env: Mapping[str, str]) -> CliResult:
    return await run_blocking(
        run_cli_sync, list(argv), timeout, dict(env), pool=CLI_POOL, label="singularity"
    )


class SingularityCli:
    """The CLI path, the environment it runs with, and its runners (injectable for tests)."""

    def __init__(
        self,
        path: str,
        env: Mapping[str, str],
        *,
        runner: Runner | None = None,
        sync_runner: SyncRunner | None = None,
    ) -> None:
        self.path = path
        self.env = dict(env)
        self._runner: Runner = runner if runner is not None else run_cli
        self._sync: SyncRunner = sync_runner if sync_runner is not None else run_cli_sync

    @property
    def prog(self) -> str:
        return (
            "singularity" if os.path.basename(self.path).startswith("singularity") else "apptainer"
        )

    @property
    def env_prefix(self) -> str:
        """The client variable prefix that sets a variable inside the instance."""
        return f"{self.prog.upper()}ENV_"

    def argv(self, *args: str) -> list[str]:
        return [self.path, *args]

    def client_env(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        return {**self.env, **(extra or {})}

    async def call(self, *args: str, timeout: float) -> CliResult:
        return await self._runner(self.argv(*args), timeout, self.client_env())

    def call_sync(self, *args: str, timeout: float) -> CliResult:
        return self._sync(self.argv(*args), timeout, self.client_env())


def client_env(scope: Scope, scratch: Path) -> dict[str, str]:
    """The CLI's own environment: the minimal child env, the variables the CLI reads (registry
    login, proxies, the user's systemd session for cgroups) and our cache/temp dirs. Never a
    ``*ENV_`` variable from the host: those would land inside the instance."""
    control = {
        name: value
        for name, value in scope.env.items()
        if name.startswith(CLIENT_PREFIXES) or name in CLIENT_NAMES
    }
    env = child_env(scope, variant="minimal", extra=control)
    for prefix in ("APPTAINER", "SINGULARITY"):
        env[f"{prefix}_CACHEDIR"] = str(scratch / "cache")
        env[f"{prefix}_TMPDIR"] = str(scratch / "tmp")
    return env


# -- the SIF cache --------------------------------------------------------------------------------


def is_reference(image: str) -> bool:
    return image.startswith(IMAGE_SCHEMES)


def sif_path(scratch: Path, image: str) -> Path:
    digest = hashlib.sha256(image.encode("utf-8")).hexdigest()[:16]
    return scratch / "sif-cache" / f"{digest}.sif"


def _private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)


def build_sif_sync(
    cli: SingularityCli, image: str, scratch: Path, *, timeout: float, lock_timeout: float
) -> str:
    """Build (once) and return the cached SIF for ``image``; blocking (run it on a worker)."""
    target = sif_path(scratch, image)
    if target.is_file():
        return str(target)
    _private_dir(target.parent)
    _private_dir(scratch / "cache")
    _private_dir(scratch / "tmp")
    lock = FileLock(target.with_name(f"{target.name}.lock"), timeout=lock_timeout)
    try:
        lock.acquire()
    except LockTimeout as exc:
        raise SingularityBuildError(
            f"another process kept building {image} for more than {lock_timeout:g}s"
        ) from exc
    try:
        if target.is_file():
            return str(target)
        partial = target.with_name(f"{target.stem}.{secrets.token_hex(4)}.partial.sif")
        log.info("building %s from %s (once; up to %gs)", target.name, image, timeout)
        result = cli.call_sync("build", "--force", str(partial), image, timeout=timeout)
        if not result.ok or not partial.is_file():
            with contextlib.suppress(OSError):
                partial.unlink()
            why = f"timed out after {timeout:g}s" if result.timed_out else "failed"
            detail = _tail(result.stderr or result.stdout)
            raise SingularityBuildError(
                f"building the image {image} {why}" + (f": {detail}" if detail else ""),
                hint="check terminal.singularity_image (or terminal.docker_image) and the "
                "registry login, then try again",
            )
        os.replace(partial, target)
        return str(target)
    finally:
        lock.release()


async def ensure_sif(
    cli: SingularityCli,
    image: str,
    scratch: Path,
    *,
    timeout: float = BUILD_TIMEOUT_S,
    lock_timeout: float | None = None,
) -> str:
    """The image to start: a local path as given, a reference as its cached SIF."""
    if not is_reference(image):
        return image
    wait = lock_timeout if lock_timeout is not None else timeout + BUILD_LOCK_GRACE_S
    return await run_blocking(
        lambda: build_sif_sync(cli, image, scratch, timeout=timeout, lock_timeout=wait),
        pool=CLI_POOL,
        label="singularity build",
    )


# -- scratch usage --------------------------------------------------------------------------------


def dir_usage(path: Path) -> int:
    """Bytes used by the files under ``path`` (symlinks not followed)."""
    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
    return total


def warn_disk_usage(
    scratch: Path, limit_gb: float, *, usage: Callable[[Path], int] = dir_usage
) -> bool:
    """Log one warning per process and scratch dir when its usage passes ``limit_gb``."""
    if limit_gb <= 0:
        return False
    key = str(scratch)
    with _warned_lock:
        if key in _warned_scratch:
            return False
    used = usage(scratch)
    if used <= limit_gb * GIB:
        return False
    with _warned_lock:
        if key in _warned_scratch:
            return False
        _warned_scratch.add(key)
    log.warning(
        "the Singularity scratch dir %s uses %.1f GB (warning above %g GB); remove old overlays "
        "or SIF files there, or raise terminal.disk_warning_gb",
        scratch,
        used / GIB,
        limit_gb,
    )
    return True


def reset_for_tests() -> None:
    with _warned_lock:
        _warned_scratch.clear()


# -- starting -------------------------------------------------------------------------------------


def cgroups_usable() -> bool:
    """Whether ``--cpus`` / ``--memory`` can work: root, or a cgroup v2 hierarchy."""
    geteuid = getattr(os, "geteuid", None)
    if geteuid is not None and geteuid() == 0:
        return True
    return Path("/sys/fs/cgroup/cgroup.controllers").exists()


def start_argv(
    cli: SingularityCli,
    *,
    sif: str,
    name: str,
    overlay: str | None,
    binds: Sequence[ReadonlyMount],
    cpu: float,
    memory_mb: int,
    limits: bool,
) -> list[str]:
    """``instance start`` for one environment (pure)."""
    args = ["instance", "start", "--containall", "--no-home"]
    args += ["--overlay", overlay] if overlay else ["--writable-tmpfs"]
    for mount in binds:
        args += ["--bind", f"{mount.host_path}:{mount.container_path}:ro"]
    if limits:
        if cpu > 0:
            args += ["--cpus", f"{cpu:g}"]
        if memory_mb > 0:
            args += ["--memory", f"{memory_mb}M"]
    return cli.argv(*args, sif, name)


def readonly_binds(scope: Scope, session_id: str | None) -> list[ReadonlyMount]:
    """Credential files and the profile's skills, read-only."""
    found: list[ReadonlyMount] = []
    skills = scope.home / "skills"
    if skills.is_dir():
        found.append(ReadonlyMount(str(skills), SKILLS_TARGET, "dir"))
    found += credential_mounts(scope, session_id)
    found += readonly_from_sources(scope)
    seen: set[str] = set()
    unique: list[ReadonlyMount] = []
    for mount in found:
        if mount.container_path not in seen:
            seen.add(mount.container_path)
            unique.append(mount)
    return unique


def instance_gone(output: str, returncode: int) -> bool:
    """The CLI's answer when the instance no longer exists."""
    if returncode != 255:
        return False
    text = output.lower()
    return "no instance found" in text or ("instance" in text and "not found" in text)


async def start_instance(
    cli: SingularityCli,
    *,
    sif: str,
    overlay: str | None,
    binds: Sequence[ReadonlyMount],
    settings: SingularitySettings,
    cgroups: bool,
) -> str:
    """Start an instance; returns its name. One retry without limits when cgroups refuse."""
    name = f"indus_{secrets.token_hex(6)}"
    limits = cgroups and (settings.cpu > 0 or settings.memory_mb > 0)
    for attempt in (1, 2):
        argv = start_argv(
            cli,
            sif=sif,
            name=name,
            overlay=overlay,
            binds=binds,
            cpu=settings.cpu,
            memory_mb=settings.memory_mb,
            limits=limits,
        )
        result = await cli.call(*argv[1:], timeout=START_TIMEOUT_S)
        if result.ok:
            log.info("started %s instance %s (%s)", cli.prog, name, os.path.basename(sif))
            return name
        text = _tail(result.stderr or result.stdout)
        if attempt == 1 and limits and "cgroup" in text.lower():
            log.warning(
                "%s could not apply CPU/memory limits (%s); starting without them",
                cli.prog,
                text.splitlines()[-1] if text else "cgroups unavailable",
            )
            limits = False
            continue
        why = f"did not start within {START_TIMEOUT_S:g}s" if result.timed_out else "failed"
        raise BackendError(
            f"the {cli.prog} instance {why}" + (f": {text}" if text else ""),
            hint="check the image and terminal.container_* limits, then try again",
        )
    raise AssertionError("unreachable")


# -- the environment ------------------------------------------------------------------------------


class SingularityEnvironment(SpawnedEnvironment):
    """Commands in one Apptainer/Singularity instance (see the module notes)."""

    backend_name = "singularity"

    def __init__(
        self,
        scope: Scope,
        *,
        cli: SingularityCli,
        settings: SingularitySettings,
        instance: str,
        sif: str,
        overlay: str | None,
        task_key: str,
        records: SandboxRecords | None,
        cwd: str,
        timeout: float,
        persist_env: bool = True,
        term_grace_s: float = 2.0,
        clock: Clock | None = None,
        max_chars: int = DEFAULT_MAX_CHARS,
        snapshot_timeout: float = SNAPSHOT_TIMEOUT_S,
    ) -> None:
        super().__init__(
            cwd=cwd,
            timeout=timeout,
            temp_dir=SANDBOX_TEMP,
            flags=SINGULARITY_FLAGS,
            persist_env=persist_env,
            clock=clock,
            capture_home=scope.home,
            max_chars=max_chars,
            snapshot_timeout=snapshot_timeout,
        )
        self.scope = scope
        self.cli = cli
        self.settings = settings
        self.instance: str | None = instance
        self.sif = sif
        self.overlay = overlay
        self.task_key = task_key
        self.records = records
        self.term_grace_s = term_grace_s

    def describe(self) -> str:
        where = f"overlay {self.overlay}" if self.overlay else "a throwaway tmpfs"
        return f"{self.cli.prog} instance {self.instance or 'none'} with {where}, cwd {self._cwd}"

    def _excluded_names(self) -> tuple[str, ...]:
        return passthrough_plan(self.scope).names

    async def _spawn(self, script: str, *, login: bool, stdin: str | None) -> ProcessHandle:
        name = self.instance
        if name is None:
            raise BackendError(f"the {self.cli.prog} instance is not running")
        plan = passthrough_plan(self.scope)
        values = {f"{self.cli.env_prefix}{k}": v for k, v in plan.values.items()}
        argv = self.cli.argv(
            "exec",
            f"instance://{name}",
            "bash",
            *(["-l"] if login else []),
            "-c",
            unset_line(plan.unset) + script,
        )
        try:
            popen = subprocess.Popen(  # noqa: S603 - argv carries the script, values stay in env
                argv,
                stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=self.cli.client_env(values),
                start_new_session=True,
                close_fds=True,
            )
        except OSError as exc:
            raise EnvironmentConnectionError(
                f"the {self.cli.prog} CLI could not be run ({exc.strerror or exc})"
            ) from exc
        proc = LocalProcess(popen)
        if stdin is not None:
            proc.start_stdin(stdin)
        return proc

    async def _kill(self, handle: ProcessHandle, within: float | None = None) -> None:
        grace = self.term_grace_s
        if within is not None:
            grace = min(grace, max(0.0, within - SURVIVOR_WINDOW_S))
        await stop_tree(cast(LocalProcess, handle), clock=self.clock, grace=grace)

    def _force_kill(self, handle: ProcessHandle) -> None:
        force_kill_tree(cast(LocalProcess, handle))

    async def _run_script(
        self,
        script: str,
        *,
        login: bool,
        timeout: float,
        stdin: str | None,
        bounded: bool,
        on_activity: ActivityCallback | None = None,
        cancel: CancelSignal | None = None,
    ) -> ExecResult:
        result = await super()._run_script(
            script,
            login=login,
            timeout=timeout,
            stdin=stdin,
            bounded=bounded,
            on_activity=on_activity,
            cancel=cancel,
        )
        if (
            not result.timed_out
            and not result.interrupted
            and instance_gone(result.output, result.returncode)
        ):
            self.instance = None
            raise EnvironmentConnectionError(
                f"the {self.cli.prog} instance is gone (stopped outside Indus)",
                retry_hint="Run the command again; a new instance is started for it.",
            )
        return result

    async def cleanup(self) -> None:
        name = self.instance
        self.instance = None
        await super().cleanup()
        if name is not None:
            if self.overlay:
                snapshot = self.snapshot_path
                await self.cli.call(
                    "exec",
                    f"instance://{name}",
                    "sh",
                    "-c",
                    f"rm -f {snapshot} {snapshot}.tmp.*",
                    timeout=INSIDE_TIMEOUT_S,
                )
            stopped = await self.cli.call("instance", "stop", name, timeout=STOP_TIMEOUT_S)
            if not stopped.ok:
                log.warning("could not stop %s instance %s: %s", self.cli.prog, name, stopped.text)
        if self.overlay and self.records is not None:
            records, task_key, overlay = self.records, self.task_key, self.overlay
            try:
                await run_blocking(lambda: records.put(task_key, overlay_path=overlay))
            except Exception:
                log.warning("could not record the overlay of %s", task_key, exc_info=True)


# -- opening --------------------------------------------------------------------------------------


def _start_cwd(scope: Scope, override: object) -> str:
    if override and str(override).startswith(("/", "~")):
        return str(override)
    configured = str(_config(scope, "terminal.cwd") or "")
    return configured if configured.startswith(("/", "~")) else "~"


def _overlay_for(records: SandboxRecords, scratch: Path, task_key: str) -> str:
    try:
        record = records.get(task_key)
    except BackendError:
        log.warning("sandbox records unavailable; using the default overlay path", exc_info=True)
        record = None
    if record is not None and record.overlay_path and Path(record.overlay_path).is_dir():
        return record.overlay_path
    path = scratch / "overlays" / sandbox_dir_name(task_key)
    _private_dir(path)
    return str(path)


async def open_singularity(
    request: CreateRequest,
    *,
    runner: Runner | None = None,
    sync_runner: SyncRunner | None = None,
    cgroups: bool | None = None,
    clock: Clock | None = None,
) -> SingularityEnvironment:
    """A started Singularity environment for ``request`` (the registry's factory)."""
    scope = request.scope
    path = find_cli(scope)
    if path is None:
        raise BackendUnavailable(NOT_FOUND_REASON)
    settings = settings_for(scope)
    scratch = settings.scratch_dir
    await run_blocking(_private_dir, scratch)
    cli = SingularityCli(path, client_env(scope, scratch), runner=runner, sync_runner=sync_runner)
    version = await cli.call("version", timeout=VERSION_TIMEOUT_S)
    if not version.ok:
        first = (version.text.splitlines() or ["no answer"])[0]
        raise BackendUnavailable(f"`{os.path.basename(path)} version` failed: {first}")
    await run_blocking(lambda: warn_disk_usage(scratch, settings.disk_warning_gb))

    sif = await ensure_sif(cli, settings.image, scratch)
    task_key = SHARED_TASK if request.key.kind == "profile" else request.key.ident
    records: SandboxRecords | None = None
    overlay: str | None = None
    if settings.persistent:
        records = SandboxRecords.for_scope(scope, RECORD_KIND)
        store = records
        overlay = await run_blocking(lambda: _overlay_for(store, scratch, task_key))
    binds = readonly_binds(scope, request.session_id)
    instance = await start_instance(
        cli,
        sif=sif,
        overlay=overlay,
        binds=binds,
        settings=settings,
        cgroups=cgroups_usable() if cgroups is None else cgroups,
    )
    env = SingularityEnvironment(
        scope,
        cli=cli,
        settings=settings,
        instance=instance,
        sif=sif,
        overlay=overlay,
        task_key=task_key,
        records=records,
        cwd=_start_cwd(scope, request.overrides.get("cwd")),
        timeout=float(_config(scope, "terminal.timeout")),
        persist_env=bool(_config(scope, "terminal.persistent_shell")),
        term_grace_s=float(_config(scope, "terminal.daemon_term_grace_s")),
        clock=clock,
        max_chars=int(_config(scope, "tool_output.max_chars")),
    )
    try:
        await env.start()
    except BaseException:
        with contextlib.suppress(Exception):
            await env.cleanup()
        raise
    return env


async def _create(request: CreateRequest) -> Environment:
    return await open_singularity(request)


def singularity_entry() -> BackendEntry:
    """The registry row (registered with the other built-ins by P21-12)."""
    from indus.backends.registry import BackendEntry

    return BackendEntry(
        "singularity",
        _create,
        SINGULARITY_FLAGS,
        probe=singularity_probe,
        profile_scoped=persistent_per_profile,
    )
