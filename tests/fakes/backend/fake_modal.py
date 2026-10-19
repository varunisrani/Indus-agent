"""A fake of the Modal SDK's sync API, the parts the Modal backend calls (P21-15).

``FakeModal(cloud).module`` stands in for ``modal`` (``install(monkeypatch)`` also registers it in
``sys.modules`` as ``modal`` and ``modal.exception``). Names and argument names follow modal 1.6.1
(``tests/fakes/backend/sdk_surfaces.json``); see ``docs/notes/P21/fake-sdks.md`` for the surface.

Behaviour: ``Sandbox.create`` resets the cloud's file tree to the image (a registry image is
empty, an image from ``snapshot_filesystem`` restores its tree, an unknown ``im-`` id fails with
``NotFoundError``). ``exec`` starts a real process at once (``Cloud.start``); stdin bytes reach it
on ``drain()``, ``write_eof()`` closes it; ``stdout.read()`` waits for the end, while iterating
``stdout`` yields the output as the process writes it (as the SDK's reader does). Errors scripted by
status carry ``.status`` as a gRPC-style enum, like the real ones.
"""

from __future__ import annotations

import codecs
import enum
import os
import subprocess
import sys
import threading
import types
from collections.abc import Collection, Iterator, Mapping, Sequence
from typing import Any

import pytest

from tests.fakes.backend.sdk_kit import Cloud

DEFAULT_TIMEOUT_S = 300


class FakeGrpcStatus(enum.IntEnum):
    OK = 0
    INVALID_ARGUMENT = 3
    DEADLINE_EXCEEDED = 4
    NOT_FOUND = 5
    ALREADY_EXISTS = 6
    PERMISSION_DENIED = 7
    RESOURCE_EXHAUSTED = 8
    FAILED_PRECONDITION = 9
    INTERNAL = 13
    UNAVAILABLE = 14
    UNAUTHENTICATED = 16


_STATUS_FOR_HTTP: dict[int, FakeGrpcStatus] = {
    400: FakeGrpcStatus.INVALID_ARGUMENT,
    401: FakeGrpcStatus.UNAUTHENTICATED,
    403: FakeGrpcStatus.PERMISSION_DENIED,
    404: FakeGrpcStatus.NOT_FOUND,
    409: FakeGrpcStatus.ALREADY_EXISTS,
    429: FakeGrpcStatus.RESOURCE_EXHAUSTED,
    500: FakeGrpcStatus.INTERNAL,
    503: FakeGrpcStatus.UNAVAILABLE,
    504: FakeGrpcStatus.DEADLINE_EXCEEDED,
}


# -- modal.exception -------------------------------------------------------------------------------


class Error(Exception):
    def __init__(self, message: str = "", status: FakeGrpcStatus | None = None) -> None:
        super().__init__(message)
        self.status = status


class NotFoundError(Error): ...


class InvalidError(Error): ...


class AuthError(Error): ...


class ConnectionError(Error): ...  # noqa: A001 - the vendor's name


class ServiceError(Error): ...


class ResourceExhaustedError(Error): ...


class InternalError(Error): ...


class TimeoutError(Error): ...  # noqa: A001 - the vendor's name


class SandboxTimeoutError(TimeoutError): ...


class ExecTimeoutError(TimeoutError): ...


class SandboxTerminatedError(Error): ...


_CLASS_FOR_STATUS: dict[FakeGrpcStatus, type[Error]] = {
    FakeGrpcStatus.NOT_FOUND: NotFoundError,
    FakeGrpcStatus.INVALID_ARGUMENT: InvalidError,
    FakeGrpcStatus.UNAUTHENTICATED: AuthError,
    FakeGrpcStatus.PERMISSION_DENIED: AuthError,
    FakeGrpcStatus.RESOURCE_EXHAUSTED: ResourceExhaustedError,
    FakeGrpcStatus.INTERNAL: InternalError,
    FakeGrpcStatus.UNAVAILABLE: ServiceError,
    FakeGrpcStatus.DEADLINE_EXCEEDED: TimeoutError,
}


def error_for(status: int, message: str) -> Error:
    grpc = _STATUS_FOR_HTTP.get(status, FakeGrpcStatus.INTERNAL)
    return _CLASS_FOR_STATUS.get(grpc, Error)(message, status=grpc)


# -- objects --------------------------------------------------------------------------------------


class FakeApp:
    def __init__(self, name: str, app_id: str) -> None:
        self.name = name
        self.app_id = app_id


class FakeClient:
    def __init__(self, token_id: str, token_secret: str) -> None:
        self.token_id = token_id
        self.token_secret = token_secret


class FakeImage:
    def __init__(self, object_id: str, *, tag: str | None = None) -> None:
        self.object_id = object_id
        self.tag = tag


class FakeStreamReader:
    def __init__(self, process: FakeContainerProcess, which: str) -> None:
        self._process = process
        self._which = which

    def read(self) -> Any:
        self._process._record("StreamReader.read")
        out, err = self._process._collect()
        data = out if self._which == "stdout" else err
        return data.decode("utf-8", "replace") if self._process.text else data

    def __iter__(self) -> Iterator[Any]:
        return self._process._stream(self._which)


class FakeStreamWriter:
    def __init__(self, process: FakeContainerProcess) -> None:
        self._process = process
        self._buffer = bytearray()
        self.chunks: list[bytes] = []
        self.eof = False

    def write(self, data: bytes | bytearray | memoryview | str) -> None:
        self._process._record("StreamWriter.write", len(data))
        if self.eof:
            raise ValueError("stdin is closed")
        raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        self._buffer += raw
        self.chunks.append(raw)

    def write_eof(self) -> None:
        self._process._record("StreamWriter.write_eof")
        self.eof = True

    def drain(self) -> None:
        self._process._record("StreamWriter.drain")
        self._process._feed(bytes(self._buffer), close=self.eof)
        self._buffer.clear()


class FakeContainerProcess:
    def __init__(
        self,
        sandbox: FakeModalSandbox,
        argv: Sequence[str],
        *,
        workdir: str | None,
        env: Mapping[str, str | None] | None,
        timeout: int | None,
        text: bool,
    ) -> None:
        self.sandbox = sandbox
        self.argv = list(argv)
        self.text = text
        self.timeout = timeout
        cloud = sandbox.fake.cloud
        clean = {k: v for k, v in (env or {}).items() if v is not None}
        self._popen: subprocess.Popen[bytes] | None = None
        self._scripted = cloud.scripted(self.argv)
        if self._scripted is None:
            self._popen = cloud.start(self.argv, cwd=workdir, env=clean)
        self._out: bytes | None = None
        self._err: bytes | None = None
        self._lock = threading.Lock()
        self.stdout = FakeStreamReader(self, "stdout")
        self.stderr = FakeStreamReader(self, "stderr")
        self.stdin = FakeStreamWriter(self)
        self._returncode: int | None = None

    def _record(self, method: str, *args: Any) -> None:
        self.sandbox.fake.cloud.enter(method, args, {}, error_for)

    def _feed(self, data: bytes, *, close: bool) -> None:
        popen = self._popen
        if popen is None or popen.stdin is None or popen.stdin.closed:
            return
        try:
            if data:
                popen.stdin.write(data)
                popen.stdin.flush()
            if close:
                popen.stdin.close()
        except BrokenPipeError:
            pass

    def _stream(self, which: str) -> Iterator[Any]:
        """Output chunks as they come; what was streamed is not returned again by ``read``."""
        popen = self._popen
        if popen is None or self._out is not None:
            out, err = self._collect()
            data = out if which == "stdout" else err
            if data:
                yield data.decode("utf-8", "replace") if self.text else data
            return
        pipe = popen.stdout if which == "stdout" else popen.stderr
        assert pipe is not None
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        fd = pipe.fileno()
        while chunk := os.read(fd, 64 * 1024):
            if self.text:
                text = decoder.decode(chunk)
                if text:
                    yield text
            else:
                yield chunk
        tail = decoder.decode(b"", final=True)
        if self.text and tail:
            yield tail

    def _collect(self) -> tuple[bytes, bytes]:
        with self._lock:
            if self._out is not None and self._err is not None:
                return self._out, self._err
            if self._scripted is not None:
                self._out, self._err = self._scripted.stdout, self._scripted.stderr
                self._returncode = self._scripted.returncode
                return self._out, self._err
            popen = self._popen
            assert popen is not None
            if popen.stdin is not None and not popen.stdin.closed and self.stdin.eof:
                popen.stdin.close()
            try:
                out, err = popen.communicate(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                popen.kill()
                out, err = popen.communicate()
            self._out, self._err = out or b"", err or b""
            self._returncode = popen.returncode if popen.returncode >= 0 else 137
            return self._out, self._err

    @property
    def returncode(self) -> int:
        if self._returncode is None:
            raise InvalidError("the process has not finished; call wait() first")
        return self._returncode

    def poll(self) -> int | None:
        self._record("ContainerProcess.poll")
        if self._returncode is not None:
            return self._returncode
        if self._popen is not None and self._popen.poll() is None:
            return None
        self._collect()
        return self._returncode

    def wait(self) -> int:
        self._record("ContainerProcess.wait")
        self._collect()
        assert self._returncode is not None
        return self._returncode


class FakeModalSandbox:
    def __init__(
        self,
        fake: FakeModal,
        object_id: str,
        *,
        app: FakeApp | None,
        name: str | None,
        image: FakeImage | None,
        env: Mapping[str, str | None],
        timeout: int,
        workdir: str | None,
        cpu: Any,
        memory: Any,
        tags: Mapping[str, str],
    ) -> None:
        self.fake = fake
        self.object_id = object_id
        self.app = app
        self.name = name
        self.image = image
        self.env = dict(env)
        self.timeout = timeout
        self.workdir = workdir
        self.cpu = cpu
        self.memory = memory
        self.tags = dict(tags)
        self.terminated = False
        self._returncode: int | None = None
        self.processes: list[FakeContainerProcess] = []

    def _enter(self, method: str, *args: Any, **kwargs: Any) -> None:
        self.fake.cloud.enter(f"Sandbox.{method}", args, kwargs, error_for)
        if self.terminated and method not in ("terminate", "poll", "wait", "get_tags", "detach"):
            raise SandboxTerminatedError(f"sandbox {self.object_id} was terminated")

    @property
    def returncode(self) -> int | None:
        return self._returncode

    def exec(
        self,
        *args: str,
        stdout: Any = None,
        stderr: Any = None,
        timeout: int | None = None,
        workdir: str | None = None,
        env: Mapping[str, str | None] | None = None,
        secrets: Collection[Any] | None = None,
        text: bool = True,
        bufsize: int = -1,
        pty: bool = False,
    ) -> FakeContainerProcess:
        self._enter("exec", *args, timeout=timeout, workdir=workdir, env=env, text=text)
        process = FakeContainerProcess(
            self,
            args,
            workdir=workdir or self.workdir,
            env={**self.env, **(env or {})},
            timeout=timeout,
            text=text,
        )
        self.processes.append(process)
        return process

    def snapshot_filesystem(
        self, timeout: int = 55, *, ttl: int | None = 30 * 24 * 3600
    ) -> FakeImage:
        self._enter("snapshot_filesystem", timeout=timeout, ttl=ttl)
        image_id = self.fake.cloud.next_id("im-snap")
        self.fake.cloud.snapshots[image_id] = self.fake.cloud.fs.capture()
        return FakeImage(image_id)

    def terminate(self, *, wait: bool = False) -> int | None:
        self._enter("terminate", wait=wait)
        self.terminated = True
        self._returncode = self._returncode if self._returncode is not None else 0
        return self._returncode if wait else None

    def poll(self) -> int | None:
        self._enter("poll")
        return self._returncode

    def wait(self, raise_on_termination: bool = True) -> None:
        self._enter("wait", raise_on_termination=raise_on_termination)
        if self.terminated and raise_on_termination:
            raise SandboxTerminatedError(f"sandbox {self.object_id} was terminated")

    def get_tags(self) -> dict[str, str]:
        self._enter("get_tags")
        return dict(self.tags)

    def set_tags(self, tags: dict[str, str], *, client: FakeClient | None = None) -> None:
        self._enter("set_tags", tags)
        self.tags = dict(tags)

    def detach(self) -> None:
        self._enter("detach")


# -- the class-level API (what the backend reaches as modal.App / modal.Sandbox / …) --------------


class _AppApi:
    def __init__(self, fake: FakeModal) -> None:
        self._fake = fake

    def lookup(
        self,
        name: str,
        *,
        client: FakeClient | None = None,
        environment_name: str | None = None,
        create_if_missing: bool = False,
    ) -> FakeApp:
        self._fake.record("App.lookup", name, create_if_missing=create_if_missing, client=client)
        app = self._fake.apps.get(name)
        if app is None:
            if not create_if_missing:
                raise NotFoundError(f"app {name!r} not found", status=FakeGrpcStatus.NOT_FOUND)
            app = FakeApp(name, self._fake.cloud.next_id("ap-"))
            self._fake.apps[name] = app
        return app


class _ClientApi:
    def __init__(self, fake: FakeModal) -> None:
        self._fake = fake

    def from_credentials(self, token_id: str, token_secret: str) -> FakeClient:
        self._fake.record("Client.from_credentials", "<id>", "<secret>")
        expected = self._fake.tokens
        if expected is not None and (token_id, token_secret) != expected:
            raise AuthError("token rejected", status=FakeGrpcStatus.UNAUTHENTICATED)
        return FakeClient(token_id, token_secret)


class _ImageApi:
    def __init__(self, fake: FakeModal) -> None:
        self._fake = fake

    def from_registry(
        self,
        tag: str,
        secret: Any = None,
        *,
        setup_dockerfile_commands: Sequence[str] = (),
        force_build: bool = False,
        add_python: str | None = None,
        **kwargs: Any,
    ) -> FakeImage:
        self._fake.record("Image.from_registry", tag, setup=list(setup_dockerfile_commands))
        return FakeImage(f"im-registry-{tag}", tag=tag)

    def from_id(self, image_id: str, client: FakeClient | None = None) -> FakeImage:
        self._fake.record("Image.from_id", image_id)
        return FakeImage(image_id)


class _SandboxApi:
    def __init__(self, fake: FakeModal) -> None:
        self._fake = fake

    def create(
        self,
        *args: str,
        app: FakeApp | None = None,
        name: str | None = None,
        tags: dict[str, str] | None = None,
        image: FakeImage | None = None,
        env: dict[str, str | None] | None = None,
        secrets: Collection[Any] | None = None,
        timeout: int = DEFAULT_TIMEOUT_S,
        idle_timeout: int | None = None,
        workdir: str | None = None,
        cpu: Any = None,
        memory: Any = None,
        block_network: bool = False,
        verbose: bool = False,
        client: FakeClient | None = None,
        **kwargs: Any,
    ) -> FakeModalSandbox:
        fake = self._fake
        fake.record(
            "Sandbox.create",
            *args,
            app=app,
            name=name,
            image=image,
            env=env,
            timeout=timeout,
            workdir=workdir,
            cpu=cpu,
            memory=memory,
            client=client,
        )
        if app is None:
            raise InvalidError("app is required", status=FakeGrpcStatus.INVALID_ARGUMENT)
        cloud = fake.cloud
        if image is not None and image.object_id.startswith("im-") and image.tag is None:
            data = cloud.snapshots.get(image.object_id)
            if data is None:
                raise NotFoundError(
                    f"image {image.object_id} not found", status=FakeGrpcStatus.NOT_FOUND
                )
            cloud.fs.restore(data)
        else:
            cloud.fs.wipe()
            tag = image.tag if image is not None and image.tag is not None else ""
            for path, content in cloud.images.get(tag, {}).items():
                cloud.fs.write(path, content)
        sandbox = FakeModalSandbox(
            fake,
            cloud.next_id("sb-"),
            app=app,
            name=name,
            image=image,
            env=env or {},
            timeout=timeout,
            workdir=workdir,
            cpu=cpu,
            memory=memory,
            tags=tags or {},
        )
        fake.sandboxes[sandbox.object_id] = sandbox
        return sandbox

    def from_id(self, sandbox_id: str, client: FakeClient | None = None) -> FakeModalSandbox:
        self._fake.record("Sandbox.from_id", sandbox_id)
        found = self._fake.sandboxes.get(sandbox_id)
        if found is None:
            raise NotFoundError(f"sandbox {sandbox_id} not found", status=FakeGrpcStatus.NOT_FOUND)
        return found

    def from_name(
        self,
        app_name: str,
        name: str,
        *,
        environment_name: str | None = None,
        client: FakeClient | None = None,
    ) -> FakeModalSandbox:
        self._fake.record("Sandbox.from_name", app_name, name)
        for sandbox in self._fake.sandboxes.values():
            app = sandbox.app
            same_app = app is not None and app.name == app_name
            if sandbox.name == name and same_app and not sandbox.terminated:
                return sandbox
        raise NotFoundError(f"sandbox {name} not found", status=FakeGrpcStatus.NOT_FOUND)


class FakeModal:
    """The fake ``modal`` package bound to one ``Cloud`` (see the module notes)."""

    def __init__(self, cloud: Cloud, *, tokens: tuple[str, str] | None = None) -> None:
        self.cloud = cloud
        self.tokens = tokens
        self.apps: dict[str, FakeApp] = {}
        self.sandboxes: dict[str, FakeModalSandbox] = {}
        exception = types.ModuleType("modal.exception")
        for cls in (
            Error,
            NotFoundError,
            InvalidError,
            AuthError,
            ConnectionError,
            ServiceError,
            ResourceExhaustedError,
            InternalError,
            TimeoutError,
            SandboxTimeoutError,
            ExecTimeoutError,
            SandboxTerminatedError,
        ):
            setattr(exception, cls.__name__, cls)
        module = types.ModuleType("modal")
        members: dict[str, object] = {
            "App": _AppApi(self),
            "Client": _ClientApi(self),
            "Image": _ImageApi(self),
            "Sandbox": _SandboxApi(self),
            "exception": exception,
            "__version__": "1.6.1",
        }
        for key, value in members.items():
            setattr(module, key, value)
        self.module = module
        self.exception = exception

    def record(self, method: str, *args: Any, **kwargs: Any) -> None:
        self.cloud.enter(method, args, kwargs, error_for)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
        monkeypatch.setitem(sys.modules, "modal", self.module)
        monkeypatch.setitem(sys.modules, "modal.exception", self.exception)
        return self.module


# Fake classes and the vendor classes their public names are checked against
# (tests/unit/fakes/test_fake_sdks.py, sdk_surfaces.json).
SURFACE: dict[str, type] = {
    "modal.App": _AppApi,
    "modal.Client": _ClientApi,
    "modal.Image": _ImageApi,
    "modal.Sandbox": _SandboxApi,
    "modal.Sandbox#instance": FakeModalSandbox,
    "modal.container_process.ContainerProcess": FakeContainerProcess,
    "modal.io_streams.StreamReader": FakeStreamReader,
    "modal.io_streams.StreamWriter": FakeStreamWriter,
}
