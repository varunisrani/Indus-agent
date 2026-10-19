# pyright: strict
"""Vercel Sandbox: settings, auth, create / restore / wait / snapshot / stop (P21-08).

Auth (vendor-standard names, D-16.22, from the secret store or the scope env): ``VERCEL_TOKEN`` +
``VERCEL_PROJECT_ID`` + ``VERCEL_TEAM_ID`` together, handed to the SDK through its credentials
factory; or a short-lived ``VERCEL_OIDC_TOKEN``, which the SDK reads itself from the process
environment (it carries the project and team). Anything else is a requirement reason that names
what is missing.

Refused before any API call: ``terminal.container_disk_mb`` other than the default 51200 or 0
(Vercel sizes the disk itself). ``terminal.vercel_runtime`` (one of the vendor's documented names,
checked by config validation) replaces the image; without it the vendor's default image is used.

Create: from the snapshot recorded for the task key when the profile is persistent, else from the
image; a failed restore drops that record (only while it still names that snapshot) and creates
from the image with ``recreated`` set (R02.19). Create is retried on transient errors by the call
wrapper. The sandbox time limit is the command timeout, at least ``MIN_LIFETIME_S``. Then wait for
``running``: at most ``RUNNING_WAIT_S``, polling every ``POLL_S``; a terminal state ends the wait
at once. Snapshot (persistent cleanup): its id is recorded (a snapshot holds files, never
processes). Stop: within ``STOP_TIMEOUT_S``, polling ``POLL_S`` until the sandbox reports stopped.
"""

from __future__ import annotations

import contextlib
import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from indus.backends.contract import BackendError, EnvironmentConnectionError
from indus.backends.sdk_shell import (
    CREATE_TIMEOUT_S,
    OP_CREATE,
    OP_GET,
    OP_SNAPSHOT,
    OP_STOP,
    SNAPSHOT_OP_TIMEOUT_S,
    STOP_TIMEOUT_S,
    SandboxRecords,
    SdkSupport,
    config_value,
    credential,
)
from indus.foundation.clock import Clock
from indus.foundation.scope import Scope

__all__ = [
    "DEFAULT_DISK_MB",
    "KIND",
    "MIN_LIFETIME_S",
    "OIDC_TOKEN",
    "POLL_S",
    "PROJECT_ID",
    "RUNNING_WAIT_S",
    "TEAM_ID",
    "TERMINAL_STATES",
    "TOKEN",
    "WORKSPACE_ROOT",
    "VercelAuth",
    "VercelSandbox",
    "VercelSettings",
    "auth_from",
    "disk_problem",
    "requirement_reason",
    "settings_from",
    "status_of",
]

log = logging.getLogger(__name__)

TOKEN = "VERCEL_TOKEN"
PROJECT_ID = "VERCEL_PROJECT_ID"
TEAM_ID = "VERCEL_TEAM_ID"
OIDC_TOKEN = "VERCEL_OIDC_TOKEN"
DEFAULT_DISK_MB = 51200
MIN_LIFETIME_S = 300
RUNNING_WAIT_S = 30.0
POLL_S = 0.5
GET_TIMEOUT_S = 30.0
WORKSPACE_ROOT = "/vercel/sandbox"
KIND = "vercel"  # the sandbox records kind (P21-01)

RUNNING = "running"
STOPPED = "stopped"
TERMINAL_STATES = frozenset({"stopped", "failed", "aborted"})


@dataclass(frozen=True, slots=True)
class VercelAuth:
    token: str | None = None
    project_id: str | None = None
    team_id: str | None = None

    @property
    def oidc(self) -> bool:
        return self.token is None


@dataclass(frozen=True, slots=True)
class VercelSettings:
    image: str | None
    vcpus: int | None
    memory_mb: int | None
    persistent: bool
    timeout: float
    cwd: str
    task_key: str = "default"

    @property
    def lifetime_s(self) -> int:
        return max(MIN_LIFETIME_S, math.ceil(self.timeout))


def disk_problem(scope: Scope) -> str | None:
    disk = int(config_value(scope, "terminal.container_disk_mb"))
    if disk in (0, DEFAULT_DISK_MB):
        return None
    return (
        f"Vercel Sandbox sizes its disk itself: terminal.container_disk_mb is {disk}, but the "
        f"vercel_sandbox backend accepts only the default ({DEFAULT_DISK_MB}) or 0"
    )


def auth_from(scope: Scope) -> VercelAuth | str:
    """The credentials to use, or the reason there are none."""
    trio = {name: credential(scope, name) for name in (TOKEN, PROJECT_ID, TEAM_ID)}
    if all(trio.values()):
        return VercelAuth(trio[TOKEN], trio[PROJECT_ID], trio[TEAM_ID])
    if credential(scope, OIDC_TOKEN) is not None:
        return VercelAuth()
    missing = [name for name, value in trio.items() if not value]
    if len(missing) < len(trio):
        return (
            f"the vercel_sandbox backend uses {TOKEN}, {PROJECT_ID} and {TEAM_ID} together; "
            f"missing: {', '.join(missing)}"
        )
    return (
        f"the vercel_sandbox backend needs {TOKEN}, {PROJECT_ID} and {TEAM_ID} in this profile's "
        f"secret store (or {OIDC_TOKEN} for short local runs)"
    )


def requirement_reason(scope: Scope) -> str | None:
    """Why Vercel Sandbox cannot run for ``scope``, or None (no network, no import)."""
    problem = disk_problem(scope)
    if problem:
        return problem
    auth = auth_from(scope)
    return auth if isinstance(auth, str) else None


def settings_from(
    scope: Scope, *, task_key: str = "default", overrides: Mapping[str, Any] | None = None
) -> VercelSettings:
    over = overrides or {}
    runtime = str(config_value(scope, "terminal.vercel_runtime") or "").strip()
    image = str(over.get("image") or "").strip() or runtime or None
    cpu = float(config_value(scope, "terminal.container_cpu"))
    memory = int(config_value(scope, "terminal.container_memory_mb"))
    raw_cwd = str(over.get("cwd") or config_value(scope, "terminal.cwd") or "").strip()
    return VercelSettings(
        image=image,
        vcpus=math.floor(cpu) if cpu >= 1 else None,
        memory_mb=memory if memory > 0 else None,
        persistent=bool(config_value(scope, "terminal.container_persistent")),
        timeout=float(config_value(scope, "terminal.timeout")),
        cwd="" if raw_cwd in (".", WORKSPACE_ROOT) else raw_cwd,
        task_key=task_key,
    )


def status_of(sandbox: Any) -> str:
    value: Any = getattr(sandbox, "status", None)
    return str(getattr(value, "value", value) or "").lower()


class VercelSandbox:
    """One Vercel sandbox's lifecycle (see the module notes). ``sdk`` is the synchronous sandbox
    SDK module (or a fake with the same surface)."""

    def __init__(
        self,
        sdk: Any,
        support: SdkSupport,
        records: SandboxRecords,
        settings: VercelSettings,
        auth: VercelAuth,
        clock: Clock,
    ) -> None:
        self.sdk = sdk
        self.support = support
        self.records = records
        self.settings = settings
        self.auth = auth
        self.clock = clock
        self.client: Any = None
        self.sandbox: Any = None
        self.recreated = False

    def _make_client(self) -> Any:
        sdk = self.sdk
        auth = self.auth
        if auth.oidc:
            return sdk.SyncSandboxClient.create()
        token, team, project = auth.token, auth.team_id, auth.project_id

        def credentials() -> Any:
            return sdk.SandboxCredentials(token=token, team_id=team, project_id=project)

        options = sdk.SandboxServiceOptions(credentials_factory=credentials)
        return sdk.SyncSandboxClient.create(options=options)

    async def open(self) -> None:
        self.client = await self.support.call(
            OP_GET, self._make_client, timeout=GET_TIMEOUT_S, idempotent=True
        )
        await self.attach()

    async def attach(self) -> None:
        """A fresh sandbox (restored when there is a record), running."""
        self.sandbox = None
        snapshot_id = await self._restore_candidate()
        if snapshot_id is not None:
            try:
                source = self.sdk.SnapshotSource(snapshot_id=snapshot_id)
                self.sandbox = await self._create(source=source)
                log.info("vercel: restored the sandbox from snapshot %s", snapshot_id)
            except Exception as exc:
                log.warning(
                    "vercel: restoring snapshot %s failed (%s); starting from the image",
                    snapshot_id,
                    type(exc).__name__,
                )
                await self._forget(snapshot_id)
                self.recreated = True
        if self.sandbox is None:
            self.sandbox = await self._create(image=self.settings.image)
        await self.wait_running()

    async def _forget(self, snapshot_id: str) -> None:
        """Drop the record of a snapshot that could not be restored (only while it still names
        that snapshot)."""
        try:
            await self.records.drop(self.settings.task_key, expect={"snapshot_id": snapshot_id})
        except Exception as exc:
            log.warning("%s: could not drop the stale snapshot record (%s)", KIND, exc)

    async def _restore_candidate(self) -> str | None:
        if not self.settings.persistent:
            return None
        record = await self.records.load(self.settings.task_key) or {}
        value = record.get("snapshot_id")
        return value if isinstance(value, str) and value else None

    async def _create(self, **source: Any) -> Any:
        sdk = self.sdk
        settings = self.settings
        client = self.client
        resources = None
        if settings.vcpus is not None or settings.memory_mb is not None:
            resources = sdk.SandboxResources(vcpus=settings.vcpus, memory=settings.memory_mb)

        def make() -> Any:
            return client.create_sandbox(
                execution_time_limit=timedelta(seconds=settings.lifetime_s),
                resources=resources,
                **source,
            )

        return await self.support.call(OP_CREATE, make, timeout=CREATE_TIMEOUT_S, idempotent=True)

    async def refresh(self) -> str:
        """Re-read the sandbox (a new handle) and return its status."""
        client = self.client
        name = getattr(self.sandbox, "name", None)
        self.sandbox = await self.support.call(
            OP_GET, lambda: client.get_sandbox(name=name), timeout=GET_TIMEOUT_S, idempotent=True
        )
        return status_of(self.sandbox)

    async def wait_running(self, limit_s: float = RUNNING_WAIT_S) -> None:
        status = status_of(self.sandbox)
        deadline = self.clock.monotonic() + limit_s
        while status not in ("", RUNNING):
            if status in TERMINAL_STATES:
                raise BackendError(f"the vercel sandbox ended up {status} while starting")
            if self.clock.monotonic() >= deadline:
                raise EnvironmentConnectionError(
                    f"the vercel sandbox did not start within {limit_s:g}s (last status {status})",
                    retry_hint="Vercel may be busy; run the command again in a moment.",
                )
            await self.clock.sleep(POLL_S)
            status = await self.refresh()

    async def snapshot(self) -> None:
        """Persistent profiles: snapshot the filesystem and record the id."""
        if not self.settings.persistent or self.sandbox is None:
            return
        sandbox = self.sandbox
        try:
            snap = await self.support.call(
                OP_SNAPSHOT, sandbox.snapshot, timeout=SNAPSHOT_OP_TIMEOUT_S
            )
        except Exception as exc:
            log.warning("vercel: filesystem snapshot failed (%s)", type(exc).__name__)
            return
        snapshot_id = str(getattr(snap, "id", "") or "")
        if not snapshot_id:
            log.warning("vercel: the snapshot came back without an id; nothing recorded")
            return
        try:
            await self.records.save(self.settings.task_key, {"snapshot_id": snapshot_id})
        except Exception as exc:
            log.warning("vercel: could not record snapshot %s (%s)", snapshot_id, exc)
            return
        log.info("vercel: saved snapshot %s", snapshot_id)

    async def stop(self) -> None:
        """Stop the sandbox and wait (bounded) until it says so."""
        sandbox = self.sandbox
        if sandbox is None:
            return
        deadline = self.clock.monotonic() + STOP_TIMEOUT_S
        try:
            await self.support.call(OP_STOP, sandbox.stop, timeout=STOP_TIMEOUT_S, idempotent=True)
            while status_of(self.sandbox) not in TERMINAL_STATES:
                if self.clock.monotonic() >= deadline:
                    log.warning("vercel: the sandbox did not report stopped in time")
                    break
                await self.clock.sleep(POLL_S)
                await self.refresh()
        except Exception as exc:
            log.warning("vercel: stopping the sandbox failed (%s)", type(exc).__name__)

    async def close_client(self) -> None:
        client, self.client = self.client, None
        if client is not None:
            with contextlib.suppress(Exception):
                await self.support.call(OP_STOP, client.close, timeout=STOP_TIMEOUT_S)
