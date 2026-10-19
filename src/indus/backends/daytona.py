# pyright: strict
"""The Daytona backend: commands in a Daytona cloud sandbox through its SDK (P21-07).

Credentials: ``DAYTONA_API_KEY`` from the profile's secret store, else the scope env (D-16.22),
handed to the SDK's config object (never a process env or argv). Missing → requirement reason.

Sandbox: named ``indus-<task key>``. Opening looks the name up: a stopped (or archived) sandbox is
started and keeps its files, a running one is used, none is created from the image with auto-stop
off. A non-persistent profile never reuses one: a leftover of that name is deleted first. There is
no lookup by labels (D-16.3). Resources: CPU from ``terminal.container_cpu`` (whole CPUs, at least
1), memory GiB = ceil(MB / 1024) (at least 1), disk GiB = ceil(MB / 1024) between 1 and
``MAX_DISK_GIB``; a larger disk request is cut with one warning.

Home: ``echo $HOME`` (10 s) through the shared flow; a cwd of ``~`` follows it. Before each
command the sandbox state is refreshed and a stopped sandbox (auto-stop, a stop elsewhere) is
started again. Commands: the shared SDK shell flow over ``process.exec`` (one call returning the
combined output and exit code) with the shell timeout guard kept, since the SDK's own timeout is
not trusted; stdin is staged as a file (``payload``, P21-03). Stopping a command signals its
process group through a second exec. Files: ``FileApiTransport`` over ``fs.upload_file`` /
``fs.download_file`` / ``fs.get_file_info``.

Cleanup: sync-back (P21-10), then stop when persistent (the filesystem is kept) or delete.
"""

from __future__ import annotations

import contextlib
import importlib
import logging
import math
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from indus.backends.capture import DEFAULT_MAX_CHARS
from indus.backends.contract import BackendFlags, BackendUnavailable, EnvironmentConnectionError
from indus.backends.remote_sync import SyncTransport
from indus.backends.sdk_shell import (
    CREATE_TIMEOUT_S,
    FILE_TIMEOUT_S,
    OP_CREATE,
    OP_FILE,
    OP_GET,
    OP_STOP,
    STOP_TIMEOUT_S,
    FileApiTransport,
    SdkEnvironment,
    SdkProcess,
    SdkSupport,
    config_value,
    credential,
)
from indus.foundation.clock import Clock
from indus.foundation.scope import Scope

__all__ = [
    "API_KEY",
    "DAYTONA_FLAGS",
    "EXTRA",
    "MAX_DISK_GIB",
    "DaytonaEnvironment",
    "DaytonaSettings",
    "Resources",
    "load_sdk",
    "open_daytona",
    "requirement_reason",
    "resources_for",
    "sandbox_name",
    "settings_from",
]

log = logging.getLogger(__name__)

API_KEY = "DAYTONA_API_KEY"
EXTRA = "daytona"
MAX_DISK_GIB = 10
GET_TIMEOUT_S = 30.0
START_TIMEOUT_S = 120.0
REMOTE_TEMP = "/tmp"
VENDOR_HOME = "/home/daytona"

DAYTONA_FLAGS = BackendFlags(
    isolated=True, container_paths=True, stdin_mode="payload", host_local=False
)

_STOPPED_STATES = frozenset({"stopped", "archived"})


def load_sdk() -> Any:
    """The ``daytona`` module, or ``BackendUnavailable`` naming the extra (never ImportError)."""
    try:
        return importlib.import_module("daytona")
    except ImportError as exc:
        raise BackendUnavailable(
            f"the daytona backend needs the Daytona SDK, and indus was installed without the "
            f"`{EXTRA}` extra (install indus-agent[{EXTRA}])"
        ) from exc


@dataclass(frozen=True, slots=True)
class Resources:
    cpu: int
    memory_gib: int
    disk_gib: int
    disk_clamped: bool = False


def resources_for(cpu: float, memory_mb: int, disk_mb: int) -> Resources:
    """The sandbox resources for the container settings (see the module notes)."""
    memory_gib = max(1, math.ceil(memory_mb / 1024))
    disk_gib = max(1, math.ceil(disk_mb / 1024))
    clamped = disk_gib > MAX_DISK_GIB
    return Resources(
        cpu=max(1, int(cpu)),
        memory_gib=memory_gib,
        disk_gib=min(disk_gib, MAX_DISK_GIB),
        disk_clamped=clamped,
    )


_NAME_SAFE = re.compile(r"[^a-z0-9-]+")


def sandbox_name(task_key: str) -> str:
    slug = _NAME_SAFE.sub("-", task_key.lower()).strip("-") or "default"
    return f"indus-{slug[:48]}"


@dataclass(frozen=True, slots=True)
class DaytonaSettings:
    image: str
    resources: Resources
    persistent: bool
    timeout: float
    cwd: str
    task_key: str = "default"


def settings_from(
    scope: Scope, *, task_key: str = "default", overrides: Mapping[str, Any] | None = None
) -> DaytonaSettings:
    over = overrides or {}
    image = str(
        over.get("image")
        or config_value(scope, "terminal.daytona_image")
        or config_value(scope, "terminal.docker_image")
    ).strip()
    raw_cwd = str(over.get("cwd") or config_value(scope, "terminal.cwd") or "").strip()
    return DaytonaSettings(
        image=image,
        resources=resources_for(
            float(config_value(scope, "terminal.container_cpu")),
            int(config_value(scope, "terminal.container_memory_mb")),
            int(config_value(scope, "terminal.container_disk_mb")),
        ),
        persistent=bool(config_value(scope, "terminal.container_persistent")),
        timeout=float(config_value(scope, "terminal.timeout")),
        cwd="~" if raw_cwd in ("", ".", VENDOR_HOME) else raw_cwd,
        task_key=task_key,
    )


def requirement_reason(scope: Scope) -> str | None:
    if credential(scope, API_KEY) is None:
        return (
            f"the daytona backend needs a Daytona API key: store {API_KEY} in this profile's "
            "secret store (or set it in the environment)"
        )
    return None


def _state(sandbox: Any) -> str:
    value: Any = getattr(sandbox, "state", None)
    return str(getattr(value, "value", value) or "").lower()


def _not_found(sdk: Any, exc: BaseException) -> bool:
    kind: Any = getattr(sdk, "DaytonaNotFoundError", None)
    if isinstance(kind, type) and isinstance(exc, kind):
        return True
    return getattr(exc, "status_code", None) == 404 or "NotFound" in type(exc).__name__


class DaytonaEnvironment(SdkEnvironment):
    """Commands in a Daytona sandbox (see the module notes)."""

    backend_name = "daytona"
    default_home = "/root"

    def __init__(
        self,
        scope: Scope,
        support: SdkSupport,
        *,
        sdk: Any,
        client: Any,
        sandbox: Any,
        settings: DaytonaSettings,
        session_id: str | None = None,
        clock: Clock | None = None,
        max_chars: int = DEFAULT_MAX_CHARS,
        temp_dir: str = REMOTE_TEMP,
        sync_files: bool = True,
    ) -> None:
        super().__init__(
            scope,
            support,
            cwd=settings.cwd,
            timeout=settings.timeout,
            temp_dir=temp_dir,
            flags=DAYTONA_FLAGS,
            session_id=session_id,
            clock=clock,
            max_chars=max_chars,
            persist_env=bool(config_value(scope, "terminal.persistent_shell")),
            sync_files=sync_files,
        )
        self.sdk = sdk
        self.client = client
        self.sandbox = sandbox
        self.settings = settings

    def describe(self) -> str:
        return f"daytona sandbox {sandbox_name(self.settings.task_key)}, cwd {self._cwd}"

    # -- vendor calls ----------------------------------------------------------------------------

    def _vendor_exec(
        self, argv: list[str], proc: SdkProcess, stdin_chunks: list[bytes] | None, timeout: float
    ) -> int:
        command = shlex.join(argv)
        extra: dict[str, Any] = {"env": dict(proc.env)} if proc.env else {}
        response = self.sandbox.process.exec(command, timeout=max(1, math.ceil(timeout)), **extra)
        proc.feed(str(getattr(response, "result", "") or ""))
        return int(getattr(response, "exit_code", 1))

    async def write_file(self, path: str, data: bytes) -> None:
        sandbox = self.sandbox
        await self.support.call(
            OP_FILE, lambda: sandbox.fs.upload_file(data, path), timeout=FILE_TIMEOUT_S
        )

    async def file_size(self, path: str) -> int:
        sandbox = self.sandbox
        info = await self.support.call(
            OP_FILE, lambda: sandbox.fs.get_file_info(path), timeout=FILE_TIMEOUT_S, idempotent=True
        )
        return int(getattr(info, "size", -1))

    async def put_file(self, local_path: str, remote_path: str, timeout: float) -> None:
        sandbox = self.sandbox
        await self.support.call(
            OP_FILE, lambda: sandbox.fs.upload_file(local_path, remote_path), timeout=timeout
        )

    async def get_file(self, remote_path: str, local_path: str, timeout: float) -> None:
        sandbox = self.sandbox
        await self.support.call(
            OP_FILE, lambda: sandbox.fs.download_file(remote_path, local_path), timeout=timeout
        )

    def transport(self) -> SyncTransport:
        return FileApiTransport(self)

    # -- lifecycle -------------------------------------------------------------------------------

    async def _ensure_ready(self) -> None:
        sandbox = self.sandbox
        try:
            await self.support.call(
                OP_GET, sandbox.refresh_data, timeout=GET_TIMEOUT_S, idempotent=True
            )
        except Exception as exc:
            raise self.connection_error(exc, "checking the sandbox") from exc
        if _state(sandbox) in _STOPPED_STATES:
            log.info("daytona: starting the stopped sandbox again")
            try:
                await self.support.call(
                    OP_CREATE,
                    lambda: sandbox.start(timeout=START_TIMEOUT_S),
                    timeout=CREATE_TIMEOUT_S,
                    idempotent=True,
                )
            except Exception as exc:
                raise self.connection_error(exc, "starting the sandbox") from exc

    async def _teardown(self) -> None:
        sandbox = self.sandbox
        if self.settings.persistent:
            await self.support.call(
                OP_STOP,
                lambda: sandbox.stop(timeout=STOP_TIMEOUT_S),
                timeout=STOP_TIMEOUT_S,
                idempotent=True,
            )
            log.info("daytona: stopped the sandbox (files kept)")
        else:
            client = self.client
            await self.support.call(
                OP_STOP, lambda: client.delete(sandbox), timeout=STOP_TIMEOUT_S, idempotent=True
            )
            log.info("daytona: deleted the sandbox")


async def _find(sdk: Any, client: Any, support: SdkSupport, name: str) -> Any:
    try:
        return await support.call(
            OP_GET, lambda: client.get(name), timeout=GET_TIMEOUT_S, idempotent=True
        )
    except Exception as exc:
        if _not_found(sdk, exc):
            return None
        raise


async def _open_sandbox(
    sdk: Any, client: Any, support: SdkSupport, settings: DaytonaSettings
) -> Any:
    name = sandbox_name(settings.task_key)
    found = await _find(sdk, client, support, name)
    if found is not None and not settings.persistent:
        log.info("daytona: removing the leftover sandbox %s (not persistent)", name)
        await support.call(
            OP_STOP, lambda: client.delete(found), timeout=STOP_TIMEOUT_S, idempotent=True
        )
        found = None
    if found is not None:
        if _state(found) in _STOPPED_STATES:
            await support.call(
                OP_CREATE,
                lambda: found.start(timeout=START_TIMEOUT_S),
                timeout=CREATE_TIMEOUT_S,
                idempotent=True,
            )
            log.info("daytona: started the existing sandbox %s", name)
        return found
    res = settings.resources
    if res.disk_clamped:
        log.warning(
            "daytona: the disk request is larger than the %d GiB Daytona allows; using %d GiB",
            MAX_DISK_GIB,
            MAX_DISK_GIB,
        )

    def make() -> Any:
        params = sdk.CreateSandboxFromImageParams(
            image=settings.image,
            name=name,
            auto_stop_interval=0,
            resources=sdk.Resources(cpu=res.cpu, memory=res.memory_gib, disk=res.disk_gib),
        )
        return client.create(params, timeout=CREATE_TIMEOUT_S)

    created = await support.call(OP_CREATE, make, timeout=CREATE_TIMEOUT_S, idempotent=True)
    log.info("daytona: created the sandbox %s", name)
    return created


async def open_daytona(
    scope: Scope,
    *,
    support: SdkSupport,
    sdk: Any = None,
    session_id: str | None = None,
    task_key: str = "default",
    overrides: Mapping[str, Any] | None = None,
    clock: Clock | None = None,
    temp_dir: str = REMOTE_TEMP,
    sync_files: bool = True,
) -> DaytonaEnvironment:
    """A ready Daytona environment for ``scope`` (see the module notes)."""
    problem = requirement_reason(scope)
    if problem:
        raise BackendUnavailable(problem)
    api_key = credential(scope, API_KEY)
    settings = settings_from(scope, task_key=task_key, overrides=overrides)
    module = sdk if sdk is not None else load_sdk()
    try:
        client = await support.call(
            OP_GET,
            lambda: module.Daytona(module.DaytonaConfig(api_key=api_key)),
            timeout=GET_TIMEOUT_S,
            idempotent=True,
        )
        sandbox = await _open_sandbox(module, client, support, settings)
    except (BackendUnavailable, EnvironmentConnectionError):
        raise
    except Exception as exc:
        raise EnvironmentConnectionError(
            f"opening the daytona sandbox failed: {type(exc).__name__}",
            retry_hint="Check your Daytona API key and Daytona's status, then run it again.",
        ) from exc
    env = DaytonaEnvironment(
        scope,
        support,
        sdk=module,
        client=client,
        sandbox=sandbox,
        settings=settings,
        session_id=session_id,
        clock=clock,
        max_chars=int(config_value(scope, "tool_output.max_chars")),
        temp_dir=temp_dir,
        sync_files=sync_files,
    )
    try:
        await env.connect()
    except BaseException:
        with contextlib.suppress(Exception):
            await env.cleanup()
        raise
    return env
