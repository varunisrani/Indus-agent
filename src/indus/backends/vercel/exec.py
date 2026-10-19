# pyright: strict
"""Vercel Sandbox: running commands and moving files (P21-09).

The workspace root is ``/vercel/sandbox`` (or the sandbox's own cwd when it reports an absolute
one); commands start there unless ``terminal.cwd`` says otherwise (``~`` = the detected home).
Commands use the shared SDK shell flow over ``create_process("bash", ["-c", <guarded script>])``:
the process is waited for and its output read; stdin is staged as a file (``payload``, P21-03).
Stopping a command kills the vendor process and signals its process group.

File writes (staged stdin, uploads) are retried ``WRITE_ATTEMPTS`` times with a ``WRITE_STEP_S``
step (0.1 s, then 0.2 s). The transport is ``FileApiTransport``: single files by a write with a
byte-count check, many as one tar unpacked in the sandbox, downloads as a tar fetched in full
(R02.2), deletes by ``rm -f``.

Before each command the sandbox status is read; a sandbox found stopped, failed or aborted (or
that cannot be read) is replaced by a fresh one (restored from the recorded snapshot when there is
one). ``recreations`` counts replacements, so background jobs of the old sandbox are reported lost
by P11's remote job flow: a detached process never survives a stop or a restart. The next result
says ``environment_recreated`` once.

Cleanup: sync-back (P21-10) → snapshot when persistent → stop → close the client.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Mapping
from typing import Any

from indus.backends.capture import DEFAULT_MAX_CHARS
from indus.backends.contract import (
    BackendError,
    BackendFlags,
    BackendUnavailable,
    EnvironmentConnectionError,
)
from indus.backends.remote_sync import SyncTransport
from indus.backends.sdk_shell import (
    FILE_TIMEOUT_S,
    OP_FILE,
    FileApiTransport,
    SandboxRecords,
    SdkEnvironment,
    SdkProcess,
    SdkSupport,
    config_value,
)
from indus.backends.vercel import load_sdk
from indus.backends.vercel.lifecycle import (
    TERMINAL_STATES,
    WORKSPACE_ROOT,
    VercelAuth,
    VercelSandbox,
    VercelSettings,
    auth_from,
    disk_problem,
    settings_from,
)
from indus.foundation.clock import Clock, SystemClock
from indus.foundation.scope import Scope

__all__ = [
    "VERCEL_FLAGS",
    "WRITE_ATTEMPTS",
    "WRITE_STEP_S",
    "VercelEnvironment",
    "open_vercel",
]

log = logging.getLogger(__name__)

VERCEL_FLAGS = BackendFlags(
    isolated=True, container_paths=True, stdin_mode="payload", host_local=False
)
WRITE_ATTEMPTS = 3
WRITE_STEP_S = 0.1
REMOTE_TEMP = "/tmp"


class VercelEnvironment(SdkEnvironment):
    """Commands in a Vercel sandbox (see the module notes)."""

    backend_name = "vercel_sandbox"

    def __init__(
        self,
        scope: Scope,
        support: SdkSupport,
        lifecycle: VercelSandbox,
        settings: VercelSettings,
        *,
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
            flags=VERCEL_FLAGS,
            session_id=session_id,
            clock=clock,
            max_chars=max_chars,
            persist_env=bool(config_value(scope, "terminal.persistent_shell")),
            sync_files=sync_files,
        )
        self.lifecycle = lifecycle
        self.settings = settings
        self.workspace_root = WORKSPACE_ROOT
        self._take_workspace()
        if lifecycle.recreated:
            self.mark_recreated()

    def _take_workspace(self) -> None:
        reported = str(getattr(self.lifecycle.sandbox, "cwd", "") or "")
        self.workspace_root = reported if reported.startswith("/") else WORKSPACE_ROOT
        self.remote_home = self.workspace_root

    def _adopt_home(self, home: str) -> None:
        if self._cwd == "~":
            self._cwd = home
        elif self._cwd in ("", "."):
            self._cwd = self.workspace_root

    def describe(self) -> str:
        return f"vercel sandbox, cwd {self._cwd}"

    # -- vendor calls ----------------------------------------------------------------------------

    def _vendor_exec(
        self, argv: list[str], proc: SdkProcess, stdin_chunks: list[bytes] | None, timeout: float
    ) -> int:
        sandbox = self.lifecycle.sandbox
        if sandbox is None:
            raise BackendError("the vercel sandbox is gone")
        extra: dict[str, Any] = {"env": dict(proc.env)} if proc.env else {}
        process = sandbox.create_process(argv[0], argv[1:], cwd=self.workspace_root, **extra)
        proc.vendor = process
        code = process.wait()
        reader = getattr(process, "stdout", None)
        if reader is not None:
            proc.feed(reader.read() or "")
        return int(code if code is not None else 1)

    def _vendor_cancel(self, proc: SdkProcess) -> None:
        process = proc.vendor
        if process is not None:
            process.kill()

    async def write_file(self, path: str, data: bytes, mode: int | None = 0o600) -> None:
        sandbox = self.lifecycle.sandbox
        for attempt in range(1, WRITE_ATTEMPTS + 1):
            try:
                await self.support.call(
                    OP_FILE,
                    lambda: sandbox.fs.write_bytes(path, data, mode=mode),
                    timeout=FILE_TIMEOUT_S,
                )
                return
            except EnvironmentConnectionError:
                raise
            except Exception as exc:
                if attempt >= WRITE_ATTEMPTS:
                    raise
                log.debug("vercel: write %d/%d failed (%s)", attempt, WRITE_ATTEMPTS, exc)
                await self.clock.sleep(WRITE_STEP_S * attempt)

    async def put_file(self, local_path: str, remote_path: str, timeout: float) -> None:
        with open(local_path, "rb") as handle:
            data = handle.read()
        await self.write_file(remote_path, data, mode=None)

    async def get_file(self, remote_path: str, local_path: str, timeout: float) -> None:
        sandbox = self.lifecycle.sandbox
        data = await self.support.call(
            OP_FILE, lambda: sandbox.fs.read_bytes(remote_path), timeout=timeout, idempotent=True
        )
        with open(local_path, "wb") as out:
            out.write(data)

    def transport(self) -> SyncTransport:
        return FileApiTransport(self)

    # -- lifecycle -------------------------------------------------------------------------------

    async def _ensure_ready(self) -> None:
        try:
            status = await self.lifecycle.refresh()
        except Exception as exc:
            log.warning("vercel: reading the sandbox failed (%s); replacing it", type(exc).__name__)
            status = "unreadable"
        if status not in TERMINAL_STATES and status != "unreadable":
            if status not in ("", "running"):
                await self.lifecycle.wait_running()
            return
        log.warning("vercel: the sandbox is %s; starting a fresh one", status)
        with contextlib.suppress(Exception):
            await self.lifecycle.close_client()
        try:
            await self.lifecycle.open()
        except (BackendUnavailable, EnvironmentConnectionError):
            raise
        except Exception as exc:
            raise self.connection_error(exc, "replacing the sandbox") from exc
        self._take_workspace()
        await self.reopen()

    async def _teardown(self) -> None:
        try:
            await self.lifecycle.snapshot()
            await self.lifecycle.stop()
        finally:
            await self.lifecycle.close_client()


async def open_vercel(
    scope: Scope,
    *,
    support: SdkSupport,
    records: SandboxRecords,
    sdk: Any = None,
    session_id: str | None = None,
    task_key: str = "default",
    overrides: Mapping[str, Any] | None = None,
    clock: Clock | None = None,
    temp_dir: str = REMOTE_TEMP,
    sync_files: bool = True,
) -> VercelEnvironment:
    """A ready Vercel Sandbox environment for ``scope`` (see the module notes). A bad disk
    setting or missing auth is refused before any API call."""
    problem = disk_problem(scope)
    if problem:
        raise BackendUnavailable(problem)
    auth = auth_from(scope)
    if not isinstance(auth, VercelAuth):
        raise BackendUnavailable(auth)
    settings = settings_from(scope, task_key=task_key, overrides=overrides)
    module = sdk if sdk is not None else load_sdk()
    the_clock: Clock = clock if clock is not None else SystemClock()
    lifecycle = VercelSandbox(module, support, records, settings, auth, the_clock)
    try:
        await lifecycle.open()
    except (BackendUnavailable, EnvironmentConnectionError):
        await lifecycle.close_client()
        raise
    except Exception as exc:
        with contextlib.suppress(Exception):
            await lifecycle.stop()
        await lifecycle.close_client()
        raise EnvironmentConnectionError(
            f"creating the vercel sandbox failed: {type(exc).__name__}",
            retry_hint="Check your Vercel token, project and team and Vercel's status, then retry.",
        ) from exc
    env = VercelEnvironment(
        scope,
        support,
        lifecycle,
        settings,
        session_id=session_id,
        clock=the_clock,
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
