"""A fake of the Daytona SDK's sync API, the parts the Daytona backend calls (P21-15).

``FakeDaytona(cloud).module`` stands in for ``daytona`` (``install(monkeypatch)`` registers it in
``sys.modules``). Names and argument names follow daytona 0.220.0
(``tests/fakes/backend/sdk_surfaces.json``); see ``docs/notes/P21/fake-sdks.md``.

Behaviour: ``create`` makes a started sandbox over the cloud's file tree (reset to the image's
files; ``CreateSandboxFromSnapshotParams`` restores a captured tree). Sandboxes are found by id or
name (``get``); ``stop`` keeps the tree, ``delete`` forgets the sandbox. ``process.exec`` splits its
command with shell rules (it runs no shell of its own) and returns stdout and stderr together.
"""

from __future__ import annotations

import enum
import shlex
import sys
import types
from dataclasses import dataclass, field
from typing import Any

import pytest

from tests.fakes.backend.sdk_kit import Cloud

DEFAULT_LANGUAGE = "python"


# -- errors ----------------------------------------------------------------------------------------


class DaytonaError(Exception):
    def __init__(self, message: str, status_code: int | None = None, headers: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.headers = headers or {}


class DaytonaNotFoundError(DaytonaError): ...


class DaytonaAuthenticationError(DaytonaError): ...


class DaytonaForbiddenError(DaytonaError): ...


class DaytonaRateLimitError(DaytonaError): ...


class DaytonaConflictError(DaytonaError): ...


class DaytonaBadRequestError(DaytonaError): ...


class DaytonaTimeoutError(DaytonaError): ...


class DaytonaConnectionError(DaytonaError): ...


class DaytonaInternalServerError(DaytonaError): ...


class DaytonaServiceUnavailableError(DaytonaError): ...


_ERROR_FOR_STATUS: dict[int, type[DaytonaError]] = {
    400: DaytonaBadRequestError,
    401: DaytonaAuthenticationError,
    403: DaytonaForbiddenError,
    404: DaytonaNotFoundError,
    408: DaytonaTimeoutError,
    409: DaytonaConflictError,
    429: DaytonaRateLimitError,
    500: DaytonaInternalServerError,
    503: DaytonaServiceUnavailableError,
}


def error_for(status: int, message: str) -> DaytonaError:
    return _ERROR_FOR_STATUS.get(status, DaytonaError)(message, status_code=status)


# -- models ----------------------------------------------------------------------------------------


class SandboxState(enum.StrEnum):
    CREATING = "creating"
    STARTING = "starting"
    STARTED = "started"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"
    ARCHIVED = "archived"
    DESTROYED = "destroyed"


@dataclass
class DaytonaConfig:
    api_key: str | None = None
    jwt_token: str | None = None
    organization_id: str | None = None
    api_url: str | None = None
    target: str | None = None


@dataclass
class Resources:
    cpu: int | None = None
    memory: int | None = None
    disk: int | None = None
    gpu: int | None = None


@dataclass(kw_only=True)
class _CreateParams:
    name: str | None = None
    language: str | None = None
    os_user: str | None = None
    env_vars: dict[str, str] | None = None
    labels: dict[str, str] | None = None
    public: bool | None = None
    auto_stop_interval: int | None = None
    auto_archive_interval: int | None = None
    auto_delete_interval: int | None = None
    ephemeral: bool | None = None


@dataclass(kw_only=True)
class CreateSandboxFromImageParams(_CreateParams):
    image: str
    resources: Resources | None = None


@dataclass(kw_only=True)
class CreateSandboxFromSnapshotParams(_CreateParams):
    snapshot: str | None = None


@dataclass
class ExecuteResponse:
    exit_code: int
    result: str
    artifacts: Any = None
    additional_properties: dict[str, object] = field(default_factory=lambda: {})


@dataclass
class FileInfo:
    name: str
    size: int
    is_dir: bool
    mode: str


@dataclass
class FileUpload:
    source: bytes | str
    destination: str


# -- the sandbox -----------------------------------------------------------------------------------


class FakeProcess:
    def __init__(self, sandbox: FakeDaytonaSandbox) -> None:
        self._sandbox = sandbox

    def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        sandbox = self._sandbox
        sandbox._record("Process.exec", command, cwd=cwd, env=env, timeout=timeout)
        sandbox._require_started()
        cloud = sandbox.fake.cloud
        result = cloud.run(
            shlex.split(command),
            cwd=cwd or sandbox.work_dir,
            env={**sandbox.env, **(env or {})},
            timeout=timeout,
        )
        text = (result.stdout + result.stderr).decode("utf-8", "replace")
        code = -1 if result.timed_out else result.returncode
        return ExecuteResponse(exit_code=code, result=text)


class FakeFileSystem:
    def __init__(self, sandbox: FakeDaytonaSandbox) -> None:
        self._sandbox = sandbox

    def _enter(self, method: str, *args: Any, **kwargs: Any) -> Cloud:
        self._sandbox._record(f"FileSystem.{method}", *args, **kwargs)
        self._sandbox._require_started()
        return self._sandbox.fake.cloud

    def upload_file(self, file: bytes | str, remote_path: str, timeout: int = 1800) -> None:
        cloud = self._enter("upload_file", remote_path, size=len(file), timeout=timeout)
        data = file if isinstance(file, bytes) else open(file, "rb").read()  # noqa: SIM115
        cloud.fs.write(remote_path, data, mode=0o644)

    def upload_files(self, files: list[FileUpload], timeout: int = 1800) -> None:
        cloud = self._enter("upload_files", [f.destination for f in files], timeout=timeout)
        for item in files:
            data = item.source if isinstance(item.source, bytes) else open(item.source, "rb").read()  # noqa: SIM115
            cloud.fs.write(item.destination, data, mode=0o644)

    def download_file(
        self, remote_path: str, local_path: str | int | None = None, timeout: int = 1800
    ) -> bytes | None:
        """The SDK's two forms: ``(remote[, timeout])`` returns the bytes, ``(remote, local[,
        timeout])`` writes them to the local path."""
        if isinstance(local_path, int):
            local_path, timeout = None, local_path
        cloud = self._enter("download_file", remote_path, local=local_path, timeout=timeout)
        if not cloud.fs.exists(remote_path):
            raise DaytonaNotFoundError(f"file {remote_path} not found", status_code=404)
        data = cloud.fs.read(remote_path)
        if local_path is None:
            return data
        with open(local_path, "wb") as out:
            out.write(data)
        return None

    def delete_file(
        self, path: str, recursive: bool = False, request_timeout: float | None = None
    ) -> None:
        cloud = self._enter("delete_file", path, recursive=recursive)
        if not cloud.fs.exists(path):
            raise DaytonaNotFoundError(f"file {path} not found", status_code=404)
        cloud.fs.remove(path, recursive=recursive)

    def create_folder(self, path: str, mode: str, request_timeout: float | None = None) -> None:
        self._enter("create_folder", path, mode=mode).fs.mkdir(path)

    def get_file_info(self, path: str, request_timeout: float | None = None) -> FileInfo:
        cloud = self._enter("get_file_info", path)
        if not cloud.fs.exists(path):
            raise DaytonaNotFoundError(f"file {path} not found", status_code=404)
        host = cloud.fs.host(path)
        return FileInfo(
            name=host.name,
            size=0 if host.is_dir() else host.stat().st_size,
            is_dir=host.is_dir(),
            mode=oct(host.stat().st_mode & 0o777)[2:],
        )

    def set_file_permissions(
        self,
        path: str,
        mode: str | None = None,
        owner: str | None = None,
        group: str | None = None,
        request_timeout: float | None = None,
    ) -> None:
        cloud = self._enter("set_file_permissions", path, mode=mode)
        if mode is not None:
            cloud.fs.host(path).chmod(int(mode, 8))

    def list_files(
        self, path: str, depth: int | None = None, request_timeout: float | None = None
    ) -> list[FileInfo]:
        cloud = self._enter("list_files", path)
        return [self.get_file_info(f"{path.rstrip('/')}/{name}") for name in cloud.fs.listdir(path)]


class FakeDaytonaSandbox:
    def __init__(
        self,
        fake: FakeDaytona,
        sandbox_id: str,
        *,
        name: str,
        image: str | None,
        env: dict[str, str],
        labels: dict[str, str],
        resources: Resources | None,
        auto_stop_interval: int | None,
    ) -> None:
        self.fake = fake
        self.id = sandbox_id
        self.name = name
        self.image = image
        self.env = dict(env)
        self.labels = dict(labels)
        self.cpu = resources.cpu if resources else None
        self.memory = resources.memory if resources else None
        self.disk = resources.disk if resources else None
        self.auto_stop_interval = 15 if auto_stop_interval is None else auto_stop_interval
        self.state = SandboxState.STARTED
        self.deleted = False
        self.home_dir = fake.home_dir
        self.work_dir = fake.home_dir
        self._fs = FakeFileSystem(self)
        self._process = FakeProcess(self)

    def _record(self, method: str, *args: Any, **kwargs: Any) -> None:
        self.fake.cloud.enter(method, args, kwargs, error_for)

    def _require_started(self) -> None:
        if self.deleted:
            raise DaytonaNotFoundError(f"sandbox {self.id} was deleted", status_code=404)
        if self.state != SandboxState.STARTED:
            raise DaytonaConflictError(f"sandbox {self.id} is {self.state}", status_code=409)

    @property
    def fs(self) -> FakeFileSystem:
        return self._fs

    @property
    def process(self) -> FakeProcess:
        return self._process

    def start(self, timeout: float | None = 60) -> None:
        self._record("Sandbox.start", timeout=timeout)
        if self.deleted:
            raise DaytonaNotFoundError(f"sandbox {self.id} was deleted", status_code=404)
        self.state = SandboxState.STARTED

    def stop(self, timeout: float | None = 60, force: bool = False) -> None:
        self._record("Sandbox.stop", timeout=timeout, force=force)
        self.state = SandboxState.STOPPED

    def delete(self, timeout: float | None = 60) -> None:
        self._record("Sandbox.delete", timeout=timeout)
        self.fake.forget(self)

    def set_autostop_interval(self, interval: int, request_timeout: float | None = None) -> None:
        self._record("Sandbox.set_autostop_interval", interval)
        self.auto_stop_interval = interval

    def get_user_home_dir(self) -> str:
        self._record("Sandbox.get_user_home_dir")
        return self.home_dir

    def get_work_dir(self) -> str:
        self._record("Sandbox.get_work_dir")
        return self.work_dir

    def refresh_data(self, request_timeout: float | None = None) -> None:
        self._record("Sandbox.refresh_data")


class FakeDaytonaClient:
    """``daytona.Daytona`` (one per backend environment)."""

    def __init__(self, fake: FakeDaytona, config: DaytonaConfig | None) -> None:
        self._fake = fake
        self.config = config

    def _enter(self, method: str, *args: Any, **kwargs: Any) -> None:
        self._fake.cloud.enter(f"Daytona.{method}", args, kwargs, error_for)

    def create(
        self,
        params: CreateSandboxFromImageParams | CreateSandboxFromSnapshotParams | None = None,
        *,
        timeout: float = 60,
        on_snapshot_create_logs: Any = None,
    ) -> FakeDaytonaSandbox:
        self._enter("create", params, timeout=timeout)
        fake = self._fake
        cloud = fake.cloud
        params = params if params is not None else CreateSandboxFromSnapshotParams()
        name = params.name or cloud.next_id("sbx-")
        if fake.find(name) is not None:
            raise DaytonaConflictError(f"sandbox name {name} is taken", status_code=409)
        image: str | None = None
        resources: Resources | None = None
        if isinstance(params, CreateSandboxFromSnapshotParams) and params.snapshot:
            data = cloud.snapshots.get(params.snapshot)
            if data is None:
                raise DaytonaNotFoundError(f"snapshot {params.snapshot} not found", status_code=404)
            cloud.fs.restore(data)
        else:
            cloud.fs.wipe()
            if isinstance(params, CreateSandboxFromImageParams):
                image, resources = params.image, params.resources
                for path, content in cloud.images.get(params.image, {}).items():
                    cloud.fs.write(path, content)
        cloud.fs.mkdir(fake.home_dir)
        sandbox = FakeDaytonaSandbox(
            fake,
            cloud.next_id("dtn-"),
            name=name,
            image=image,
            env=params.env_vars or {},
            labels=params.labels or {},
            resources=resources,
            auto_stop_interval=params.auto_stop_interval,
        )
        fake.sandboxes[sandbox.id] = sandbox
        return sandbox

    def get(
        self, sandbox_id_or_name: str, request_timeout: float | None = None
    ) -> FakeDaytonaSandbox:
        self._enter("get", sandbox_id_or_name)
        found = self._fake.find(sandbox_id_or_name)
        if found is None:
            raise DaytonaNotFoundError(f"sandbox {sandbox_id_or_name} not found", status_code=404)
        return found

    def list(self, *args: Any, **kwargs: Any) -> list[FakeDaytonaSandbox]:
        self._enter("list")
        return list(self._fake.sandboxes.values())

    def start(self, sandbox: FakeDaytonaSandbox, timeout: float = 60) -> None:
        self._enter("start", sandbox.id, timeout=timeout)
        sandbox.start(timeout=timeout)

    def stop(self, sandbox: FakeDaytonaSandbox, timeout: float = 60) -> None:
        self._enter("stop", sandbox.id, timeout=timeout)
        sandbox.stop(timeout=timeout)

    def delete(self, sandbox: FakeDaytonaSandbox, timeout: float = 60, wait: bool = False) -> None:
        self._enter("delete", sandbox.id, timeout=timeout)
        self._fake.forget(sandbox)


class FakeDaytona:
    """The fake ``daytona`` package bound to one ``Cloud`` (see the module notes)."""

    def __init__(self, cloud: Cloud, *, api_key: str | None = None) -> None:
        self.cloud = cloud
        self.api_key = api_key
        self.home_dir = str(cloud.root / "home" / "daytona")
        self.sandboxes: dict[str, FakeDaytonaSandbox] = {}
        self.clients: list[FakeDaytonaClient] = []
        module = types.ModuleType("daytona")

        def make_client(config: DaytonaConfig | None = None) -> FakeDaytonaClient:
            self.cloud.enter("Daytona.__init__", (), {}, error_for)
            key = config.api_key if config is not None else None
            if self.api_key is not None and key != self.api_key:
                raise DaytonaAuthenticationError("invalid API key", status_code=401)
            client = FakeDaytonaClient(self, config)
            self.clients.append(client)
            return client

        setattr(module, "Daytona", make_client)  # noqa: B010 - a module object
        for item in (
            DaytonaConfig,
            Resources,
            CreateSandboxFromImageParams,
            CreateSandboxFromSnapshotParams,
            ExecuteResponse,
            FileUpload,
            SandboxState,
            DaytonaError,
            DaytonaNotFoundError,
            DaytonaAuthenticationError,
            DaytonaForbiddenError,
            DaytonaRateLimitError,
            DaytonaConflictError,
            DaytonaBadRequestError,
            DaytonaTimeoutError,
            DaytonaConnectionError,
            DaytonaInternalServerError,
            DaytonaServiceUnavailableError,
        ):
            setattr(module, item.__name__, item)
        self.module = module

    def find(self, ref: str) -> FakeDaytonaSandbox | None:
        for sandbox in self.sandboxes.values():
            if sandbox.id == ref or sandbox.name == ref:
                return sandbox
        return None

    def forget(self, sandbox: FakeDaytonaSandbox) -> None:
        sandbox.deleted = True
        sandbox.state = SandboxState.DESTROYED
        self.sandboxes.pop(sandbox.id, None)

    def add_stopped(
        self, name: str, *, files: dict[str, bytes] | None = None
    ) -> FakeDaytonaSandbox:
        """A sandbox left stopped by an earlier process (its files kept)."""
        sandbox = FakeDaytonaSandbox(
            self,
            self.cloud.next_id("dtn-"),
            name=name,
            image=None,
            env={},
            labels={},
            resources=None,
            auto_stop_interval=0,
        )
        sandbox.state = SandboxState.STOPPED
        self.cloud.fs.mkdir(self.home_dir)
        for path, content in (files or {}).items():
            self.cloud.fs.write(path, content)
        self.sandboxes[sandbox.id] = sandbox
        return sandbox

    def snapshot_tree(self, snapshot: str) -> None:
        """Store the cloud's current tree under ``snapshot`` (for create-from-snapshot tests)."""
        self.cloud.snapshots[snapshot] = self.cloud.fs.capture()

    def install(self, monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
        monkeypatch.setitem(sys.modules, "daytona", self.module)
        return self.module


SURFACE: dict[str, type] = {
    "daytona.Daytona": FakeDaytonaClient,
    "daytona.Sandbox": FakeDaytonaSandbox,
    "daytona._sync.process.Process": FakeProcess,
    "daytona._sync.filesystem.FileSystem": FakeFileSystem,
    "daytona.DaytonaConfig": DaytonaConfig,
    "daytona.Resources": Resources,
    "daytona.CreateSandboxFromImageParams": CreateSandboxFromImageParams,
    "daytona.CreateSandboxFromSnapshotParams": CreateSandboxFromSnapshotParams,
    "daytona.ExecuteResponse": ExecuteResponse,
    "daytona.FileUpload": FileUpload,
}
