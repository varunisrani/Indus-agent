"""A fake of the Vercel Sandbox SDK's sync API, the parts the Vercel backend calls (P21-15).

``FakeVercel(cloud).sync_module`` stands in for ``vercel.sandbox.sync`` and ``.module`` for
``vercel.sandbox`` (``install(monkeypatch)`` registers ``vercel``, ``vercel.sandbox`` and
``vercel.sandbox.sync``). Names and argument names follow vercel-sandbox 0.7.0
(``tests/fakes/backend/sdk_surfaces.json``); see ``docs/notes/P21/fake-sdks.md``.

Behaviour: sandboxes are named; ``create_sandbox`` makes a running one over the cloud's file tree
(reset to the image's files, or restored from a ``SnapshotSource``). ``snapshot()`` captures the
tree and leaves the sandbox stopped (as the vendor does); ``stop()`` keeps the tree; a stopped
sandbox resumes on the next process or file call. Processes take ``command`` + ``args`` (no shell
of their own); stdin is not supported, as in the SDK. ``run_process`` waits for the end;
``create_process`` returns at once with a running process (``wait()`` collects it, ``kill()``
stops its process group). ``workspace`` (default the vendor's ``/vercel/sandbox``) lets a test put
the sandbox's working dir under the cloud root; ``status_script`` makes ``get_sandbox`` report
those statuses in turn.
"""

from __future__ import annotations

import contextlib
import enum
import os
import subprocess
import sys
import types
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest

from tests.fakes.backend.sdk_kit import Cloud

WORKSPACE = "/vercel/sandbox"


# -- errors ----------------------------------------------------------------------------------------


class SandboxError(Exception): ...


class SandboxApiError(SandboxError):
    class _Response:
        def __init__(self, status_code: int) -> None:
            self.status_code = status_code

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response = self._Response(status_code)


class SandboxCredentialsError(SandboxError): ...


class SandboxTimeoutError(SandboxError): ...


class SandboxResponseError(SandboxError): ...


class SandboxFilesystemError(SandboxError): ...


class SandboxPathNotFoundError(SandboxFilesystemError): ...


class SandboxFilesystemWriteError(SandboxFilesystemError): ...


class SandboxFilesystemCommandError(SandboxFilesystemError): ...


class SandboxTerminalStateError(SandboxError): ...


def error_for(status: int, message: str) -> SandboxApiError:
    return SandboxApiError(message, status)


# -- models ----------------------------------------------------------------------------------------


class SandboxStatus(enum.StrEnum):
    """The vendor's lifecycle states (alphabetical here)."""

    ABORTED = "aborted"
    FAILED = "failed"
    PENDING = "pending"
    RUNNING = "running"
    SNAPSHOTTING = "snapshotting"
    STOPPED = "stopped"
    STOPPING = "stopping"


@dataclass(frozen=True)
class SandboxCredentials:
    token: str
    team_id: str
    project_id: str


class SyncSandboxServiceOptions:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        credentials_factory: Callable[[], SandboxCredentials] | None = None,
        file_transfer_timeout: timedelta | None = None,
        region: str | None = None,
    ) -> None:
        self.base_url = base_url or "https://vercel.com/api"
        self.credentials_factory = credentials_factory
        self.file_transfer_timeout = file_transfer_timeout
        self.region = region


@dataclass(frozen=True)
class SnapshotSource:
    snapshot_id: str
    type: str = "snapshot"


@dataclass(frozen=True)
class SandboxResources:
    vcpus: int | None = None
    memory: int | None = None


@dataclass
class CompletedProcess:
    id: str
    name: str
    args: tuple[str, ...]
    cwd: str
    session_id: str
    started_at: int
    returncode: int
    stdout: str | None
    stderr: str | None

    def check_returncode(self) -> None:
        if self.returncode != 0:
            raise SandboxError(f"process exited with {self.returncode}")


# -- the sandbox -----------------------------------------------------------------------------------


class SyncTextReader:
    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> str:
        text, self._text = self._text, ""
        return text


class SyncProcess:
    def __init__(
        self,
        sandbox: FakeVercelSandbox,
        process_id: str,
        argv: list[str],
        cwd: str,
        returncode: int | None,
        stdout: str,
        stderr: str,
        popen: subprocess.Popen[bytes] | None = None,
    ) -> None:
        self._sandbox = sandbox
        self.id = process_id
        self.name = argv[0]
        self.args = argv[1:]
        self.cwd = cwd
        self.signals: list[int] = []
        self._returncode = returncode
        self._popen = popen
        self.stdout = SyncTextReader(stdout)
        self.stderr = SyncTextReader(stderr)

    @property
    def returncode(self) -> int | None:
        return self._returncode

    def refresh(self) -> SyncProcess:
        self._sandbox._enter("Process.refresh", self.id)
        return self

    def wait(self) -> int:
        self._sandbox._enter("Process.wait", self.id)
        popen = self._popen
        if popen is not None and self._returncode is None:
            out, err = popen.communicate()
            self.stdout = SyncTextReader(out.decode("utf-8", "replace"))
            self.stderr = SyncTextReader(err.decode("utf-8", "replace"))
            self._returncode = popen.returncode if popen.returncode >= 0 else 128 - popen.returncode
        assert self._returncode is not None
        return self._returncode

    def send_signal(self, signal: int | str) -> None:
        self._sandbox._enter("Process.send_signal", self.id, signal)
        number = int(signal) if isinstance(signal, int) else 15
        self.signals.append(number)
        popen = self._popen
        if popen is not None and popen.poll() is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(popen.pid, number)

    def terminate(self) -> None:
        self.send_signal(15)

    def kill(self) -> None:
        self.send_signal(9)


class SyncSnapshot:
    def __init__(self, fake: FakeVercel, snapshot_id: str) -> None:
        self._fake = fake
        self.id = snapshot_id
        self.status = "created"

    def delete(self) -> SyncSnapshot:
        self._fake.cloud.enter("Snapshot.delete", (self.id,), {}, error_for)
        self._fake.cloud.snapshots.pop(self.id, None)
        self.status = "deleted"
        return self


class SyncSandboxFilesystem:
    def __init__(self, sandbox: FakeVercelSandbox) -> None:
        self._sandbox = sandbox

    def _path(self, method: str, path: str, cwd: str | None, **kwargs: Any) -> str:
        self._sandbox._enter(f"Filesystem.{method}", path, cwd=cwd, **kwargs)
        self._sandbox._ensure_running()
        if path.startswith("/"):
            return path
        return f"{(cwd or self._sandbox.cwd).rstrip('/')}/{path}"

    def read_bytes(self, path: str, *, cwd: str | None = None) -> bytes:
        full = self._path("read_bytes", path, cwd)
        fs = self._sandbox.fake.cloud.fs
        if not fs.exists(full) or fs.is_dir(full):
            raise SandboxPathNotFoundError(f"{full} not found")
        return fs.read(full)

    def read_text(self, path: str, *, cwd: str | None = None, encoding: str = "utf-8") -> str:
        return self.read_bytes(path, cwd=cwd).decode(encoding)

    def write_bytes(
        self, path: str, data: bytes, *, cwd: str | None = None, mode: int | None = None
    ) -> None:
        full = self._path("write_bytes", path, cwd, size=len(data), mode=mode)
        self._sandbox.fake.cloud.fs.write(full, data, mode=mode if mode is not None else 0o644)

    def write_text(
        self,
        path: str,
        text: str,
        *,
        cwd: str | None = None,
        mode: int | None = None,
        encoding: str = "utf-8",
    ) -> None:
        full = self._path("write_text", path, cwd, mode=mode)
        self._sandbox.fake.cloud.fs.write(full, text.encode(encoding), mode=mode or 0o644)

    def exists(self, path: str, *, cwd: str | None = None) -> bool:
        return self._sandbox.fake.cloud.fs.exists(self._path("exists", path, cwd))

    def is_file(self, path: str, *, cwd: str | None = None) -> bool:
        full = self._path("is_file", path, cwd)
        fs = self._sandbox.fake.cloud.fs
        return fs.exists(full) and not fs.is_dir(full)

    def is_dir(self, path: str, *, cwd: str | None = None) -> bool:
        return self._sandbox.fake.cloud.fs.is_dir(self._path("is_dir", path, cwd))

    def mkdir(self, path: str, *, cwd: str | None = None, recursive: bool = True) -> None:
        self._sandbox.fake.cloud.fs.mkdir(self._path("mkdir", path, cwd, recursive=recursive))

    def listdir(self, path: str, *, cwd: str | None = None) -> list[str]:
        return self._sandbox.fake.cloud.fs.listdir(self._path("listdir", path, cwd))

    def remove(
        self,
        path: str,
        *,
        cwd: str | None = None,
        recursive: bool = False,
        missing_ok: bool = False,
    ) -> None:
        full = self._path("remove", path, cwd, recursive=recursive, missing_ok=missing_ok)
        fs = self._sandbox.fake.cloud.fs
        if not fs.exists(full):
            if missing_ok:
                return
            raise SandboxFilesystemCommandError(f"{full}: no such file or directory")
        fs.remove(full, recursive=recursive)

    def rename(self, source: str, destination: str, *, cwd: str | None = None) -> None:
        full = self._path("rename", source, cwd, destination=destination)
        dest = (
            destination
            if destination.startswith("/")
            else f"{cwd or self._sandbox.cwd}/{destination}"
        )
        self._sandbox.fake.cloud.fs.rename(full, dest)


class FakeVercelSandbox:
    def __init__(
        self,
        fake: FakeVercel,
        name: str,
        *,
        image: str | None,
        persistent: bool | None,
        env: Mapping[str, str],
        resources: SandboxResources | None,
        execution_time_limit: Any,
        tags: Mapping[str, str],
    ) -> None:
        self.fake = fake
        self._name = name
        self._image = image
        self._persistent = persistent
        self.env = dict(env)
        self.resources = resources
        self.execution_time_limit = execution_time_limit
        self.tags = dict(tags)
        self._status = SandboxStatus.RUNNING
        self._snapshot_id: str | None = None
        self.destroyed = False
        self._session = fake.cloud.next_id("ses_")
        self.processes: list[SyncProcess] = []
        self.fs = SyncSandboxFilesystem(self)

    # -- state

    def _enter(self, method: str, *args: Any, **kwargs: Any) -> None:
        self.fake.cloud.enter(method, args, kwargs, error_for)
        if self.destroyed:
            raise SandboxTerminalStateError(f"sandbox {self._name} was destroyed")

    def _ensure_running(self) -> None:
        if self._status != SandboxStatus.RUNNING:
            self.fake.resumes += 1
            self._status = SandboxStatus.RUNNING
            self._session = self.fake.cloud.next_id("ses_")

    @property
    def name(self) -> str:
        return self._name

    @property
    def status(self) -> SandboxStatus:
        return self._status

    @property
    def persistent(self) -> bool | None:
        return self._persistent

    @property
    def current_snapshot_id(self) -> str | None:
        return self._snapshot_id

    @property
    def current_session_id(self) -> str:
        return self._session

    @property
    def image(self) -> str | None:
        return self._image

    @property
    def cwd(self) -> str:
        return self.fake.workspace

    # -- processes

    def _run(
        self,
        command: str,
        args: Sequence[str] | None,
        cwd: str | None,
        env: Mapping[str, str] | None,
    ) -> tuple[list[str], str, int, str, str]:
        self._ensure_running()
        argv = [command, *(args or [])]
        workdir = cwd or self.fake.workspace
        result = self.fake.cloud.run(argv, cwd=workdir, env={**self.env, **(env or {})})
        out = result.stdout.decode("utf-8", "replace")
        err = result.stderr.decode("utf-8", "replace")
        return argv, workdir, result.returncode, out, err

    def run_process(
        self,
        command: str,
        args: Sequence[str] | None = None,
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        sudo: bool = False,
        kill_after: float | timedelta | None = None,
        check: bool = False,
        stdout: Any = None,
        stderr: Any = None,
        capture_output: bool = False,
    ) -> CompletedProcess:
        self._enter(
            "Sandbox.run_process", command, args=list(args or []), cwd=cwd, env=env, sudo=sudo
        )
        argv, workdir, code, out, err = self._run(command, args, cwd, env)
        done = CompletedProcess(
            id=self.fake.cloud.next_id("cmd_"),
            name=command,
            args=tuple(argv),
            cwd=workdir,
            session_id=self._session,
            started_at=0,
            returncode=code,
            stdout=out,
            stderr=err,
        )
        if check:
            done.check_returncode()
        return done

    def create_process(
        self,
        command: str,
        args: Sequence[str] | None = None,
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        sudo: bool = False,
        kill_after: float | timedelta | None = None,
        stdout: int = -1,
        stderr: int = -1,
    ) -> SyncProcess:
        self._enter("Sandbox.create_process", command, args=list(args or []), cwd=cwd, env=env)
        self._ensure_running()
        cloud = self.fake.cloud
        argv = [command, *(args or [])]
        workdir = cwd or self.fake.workspace
        scripted = cloud.scripted(argv)
        process_id = cloud.next_id("cmd_")
        if scripted is not None:
            process = SyncProcess(
                self,
                process_id,
                argv,
                workdir,
                scripted.returncode,
                scripted.stdout.decode("utf-8", "replace"),
                scripted.stderr.decode("utf-8", "replace"),
            )
        else:
            popen = cloud.start(argv, cwd=workdir, env={**self.env, **(env or {})})
            if popen.stdin is not None:
                popen.stdin.close()  # the SDK has no stdin for a process
            process = SyncProcess(self, process_id, argv, workdir, None, "", "", popen=popen)
        self.processes.append(process)
        return process

    # -- lifecycle

    def snapshot(self, *, expiration: Any = None) -> SyncSnapshot:
        self._enter("Sandbox.snapshot", expiration=expiration)
        self._ensure_running()
        snapshot_id = self.fake.cloud.next_id("snap_")
        self.fake.cloud.snapshots[snapshot_id] = self.fake.cloud.fs.capture()
        self._snapshot_id = snapshot_id
        self._status = SandboxStatus.STOPPED
        return SyncSnapshot(self.fake, snapshot_id)

    def stop(self) -> FakeVercelSandbox:
        self._enter("Sandbox.stop")
        self._status = SandboxStatus.STOPPED
        return self

    def destroy(self, *, delete_orphan_snapshots: bool = False) -> FakeVercelSandbox:
        self._enter("Sandbox.destroy", delete_orphan_snapshots=delete_orphan_snapshots)
        self.destroyed = True
        self._status = SandboxStatus.STOPPED
        self.fake.sandboxes.pop(self._name, None)
        return self

    def extend_execution_time_limit(self, duration: Any) -> None:
        self._enter("Sandbox.extend_execution_time_limit", duration)

    def session(self) -> FakeVercelSandbox:
        self._enter("Sandbox.session")
        self._ensure_running()
        return self


class SyncSandboxClient:
    def __init__(self, fake: FakeVercel, options: SyncSandboxServiceOptions | None) -> None:
        self._fake = fake
        self._options = options
        self._credentials: SandboxCredentials | None = None
        self.closed = False

    @classmethod
    def create(
        cls, *, options: SyncSandboxServiceOptions | None = None, httpx_client_factory: Any = None
    ) -> SyncSandboxClient:
        raise RuntimeError("bound by FakeVercel; use fake.sync_module.SyncSandboxClient.create")

    def _enter(self, method: str, *args: Any, **kwargs: Any) -> None:
        fake = self._fake
        fake.cloud.enter(f"SandboxClient.{method}", args, kwargs, error_for)
        if self._credentials is None:
            factory = self._options.credentials_factory if self._options else None
            if factory is None:
                raise SandboxCredentialsError("no credentials")
            try:
                self._credentials = factory()
            except Exception as exc:
                raise SandboxCredentialsError(str(exc)) from exc
            if fake.token is not None and self._credentials.token != fake.token:
                raise SandboxApiError("token rejected", 401)

    def create_sandbox(
        self,
        *,
        name: str | None = None,
        image: str | None = None,
        source: SnapshotSource | None = None,
        ports: list[int] | None = None,
        execution_time_limit: Any = None,
        resources: SandboxResources | None = None,
        persistent: bool | None = None,
        network_policy: Any = None,
        network_id: str | None = None,
        env: Mapping[str, str] | None = None,
        tags: Mapping[str, str] | None = None,
        mounts: Any = None,
        snapshot_expiration: Any = None,
        snapshot_retention: Any = None,
        region: str | None = None,
        failover_regions: Any = None,
        destroy: bool = True,
        **private: Any,
    ) -> FakeVercelSandbox:
        self._enter(
            "create_sandbox",
            name=name,
            image=image,
            source=source,
            resources=resources,
            persistent=persistent,
            env=env,
            execution_time_limit=execution_time_limit,
        )
        fake = self._fake
        cloud = fake.cloud
        name = name or cloud.next_id("sbx_")
        if name in fake.sandboxes:
            raise SandboxApiError(f"sandbox {name} already exists", 409)
        if source is not None:
            data = cloud.snapshots.get(source.snapshot_id)
            if data is None:
                raise SandboxApiError(f"snapshot {source.snapshot_id} not found", 404)
            cloud.fs.restore(data)
        else:
            cloud.fs.wipe()
            for path, content in cloud.images.get(image or "", {}).items():
                cloud.fs.write(path, content)
        cloud.fs.mkdir(fake.workspace)
        sandbox = FakeVercelSandbox(
            fake,
            name,
            image=image,
            persistent=persistent,
            env=env or {},
            resources=resources,
            execution_time_limit=execution_time_limit,
            tags=tags or {},
        )
        if fake.start_status is not None:
            sandbox._status = fake.start_status  # noqa: SLF001
        fake.sandboxes[name] = sandbox
        return sandbox

    def get_sandbox(
        self, *, name: str, include_system_routes: bool | None = None
    ) -> FakeVercelSandbox:
        self._enter("get_sandbox", name=name)
        found = self._fake.sandboxes.get(name)
        if found is None:
            raise SandboxApiError(f"sandbox {name} not found", 404)
        if self._fake.status_script:
            found._status = SandboxStatus(self._fake.status_script.pop(0))  # noqa: SLF001
        return found

    def get_or_create_sandbox(
        self, *, name: str, resume: bool = True, **kwargs: Any
    ) -> tuple[FakeVercelSandbox, bool]:
        self._enter("get_or_create_sandbox", name=name, resume=resume)
        found = self._fake.sandboxes.get(name)
        if found is not None:
            if resume:
                found._ensure_running()  # noqa: SLF001
            return found, False
        return self.create_sandbox(name=name, **kwargs), True

    def resume_sandbox(
        self, *, name: str, include_system_routes: bool | None = None
    ) -> FakeVercelSandbox:
        self._enter("resume_sandbox", name=name)
        found = self._fake.sandboxes.get(name)
        if found is None:
            raise SandboxApiError(f"sandbox {name} not found", 404)
        found._ensure_running()  # noqa: SLF001
        return found

    def get_snapshot(self, *, snapshot_id: str) -> SyncSnapshot:
        self._enter("get_snapshot", snapshot_id=snapshot_id)
        if snapshot_id not in self._fake.cloud.snapshots:
            raise SandboxApiError(f"snapshot {snapshot_id} not found", 404)
        return SyncSnapshot(self._fake, snapshot_id)

    def close(self) -> None:
        self._fake.cloud.enter("SandboxClient.close", (), {}, error_for)
        self.closed = True


class FakeVercel:
    """The fake ``vercel.sandbox`` package bound to one ``Cloud`` (see the module notes)."""

    def __init__(
        self, cloud: Cloud, *, token: str | None = None, workspace: str = WORKSPACE
    ) -> None:
        self.cloud = cloud
        self.token = token
        self.workspace = workspace
        # Statuses ``get_sandbox`` reports in turn before the sandbox's own (then kept).
        self.status_script: list[str] = []
        self.sandboxes: dict[str, FakeVercelSandbox] = {}
        self.clients: list[SyncSandboxClient] = []
        self.resumes = 0
        # A status new sandboxes report first (``PENDING`` for wait-until-running tests).
        self.start_status: SandboxStatus | None = None
        fake = self

        class _Client(SyncSandboxClient):
            @classmethod
            def create(
                cls,
                *,
                options: SyncSandboxServiceOptions | None = None,
                httpx_client_factory: Any = None,
            ) -> SyncSandboxClient:
                fake.cloud.enter("SandboxClient.create", (), {"options": options}, error_for)
                client = SyncSandboxClient(fake, options)
                fake.clients.append(client)
                return client

        names: dict[str, Any] = {
            "SyncSandboxClient": _Client,
            "SandboxServiceOptions": SyncSandboxServiceOptions,
            "SyncSandboxServiceOptions": SyncSandboxServiceOptions,
            "SandboxCredentials": SandboxCredentials,
            "SyncSandbox": FakeVercelSandbox,
            "SyncProcess": SyncProcess,
            "SyncSnapshot": SyncSnapshot,
            "SyncSandboxFilesystem": SyncSandboxFilesystem,
            "SyncTextReader": SyncTextReader,
            "CompletedProcess": CompletedProcess,
            "SnapshotSource": SnapshotSource,
            "SandboxResources": SandboxResources,
            "SandboxStatus": SandboxStatus,
            "SandboxError": SandboxError,
            "SandboxApiError": SandboxApiError,
            "SandboxCredentialsError": SandboxCredentialsError,
            "SandboxTimeoutError": SandboxTimeoutError,
            "SandboxResponseError": SandboxResponseError,
            "SandboxFilesystemError": SandboxFilesystemError,
            "SandboxPathNotFoundError": SandboxPathNotFoundError,
            "SandboxFilesystemWriteError": SandboxFilesystemWriteError,
            "SandboxFilesystemCommandError": SandboxFilesystemCommandError,
            "SandboxTerminalStateError": SandboxTerminalStateError,
        }
        self.sync_module = types.ModuleType("vercel.sandbox.sync")
        self.module = types.ModuleType("vercel.sandbox")
        for key, value in names.items():
            setattr(self.sync_module, key, value)
            if not key.startswith("Sync") or key == "SyncSandboxServiceOptions":
                setattr(self.module, key, value)
        setattr(self.module, "sync", self.sync_module)  # noqa: B010 - a module object
        self.package = types.ModuleType("vercel")
        setattr(self.package, "sandbox", self.module)  # noqa: B010 - a module object

    def credentials(
        self, token: str = "", team_id: str = "team_x", project_id: str = "prj_x"
    ) -> Callable[[], SandboxCredentials]:
        """A credentials factory for ``SandboxServiceOptions`` (tests build tokens at run time)."""
        return lambda: SandboxCredentials(token=token, team_id=team_id, project_id=project_id)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
        monkeypatch.setitem(sys.modules, "vercel", self.package)
        monkeypatch.setitem(sys.modules, "vercel.sandbox", self.module)
        monkeypatch.setitem(sys.modules, "vercel.sandbox.sync", self.sync_module)
        return self.sync_module


SURFACE: dict[str, type] = {
    "vercel.sandbox.sync.SyncSandboxClient": SyncSandboxClient,
    "vercel.sandbox.sync.SyncSandbox": FakeVercelSandbox,
    "vercel.sandbox.sync.SyncProcess": SyncProcess,
    "vercel.sandbox.sync.SyncSnapshot": SyncSnapshot,
    "vercel.sandbox.sync.SyncSandboxFilesystem": SyncSandboxFilesystem,
    "vercel.sandbox.sync.SyncTextReader": SyncTextReader,
    "vercel.sandbox.sync.SandboxServiceOptions": SyncSandboxServiceOptions,
    "vercel.sandbox.sync.SandboxCredentials": SandboxCredentials,
    "vercel.sandbox.sync.CompletedProcess": CompletedProcess,
    "vercel.sandbox.sync.SnapshotSource": SnapshotSource,
    "vercel.sandbox.sync.SandboxResources": SandboxResources,
}
