"""Shared machinery of the fake vendor SDKs (P21-15): a fake vendor account (``Cloud``).

A ``Cloud`` is one vendor account in a test: a call log, scripted errors and latency per method, a
toy file system and real command execution.

- ``calls``: every SDK call as ``Call(method, args, kwargs)``, method named ``"Class.method"``.
- ``fail(method, status, times)`` / ``fail_with(method, exc, times)``: the next ``times`` calls of
  ``method`` raise; ``status`` errors are built by the fake SDK in its vendor's shape.
- ``delay(method, seconds)``: the call blocks until the injected ``FakeClock`` passed ``seconds``
  (``clock.sleep_sync``; call ``clock.allow_threaded_advance()`` first). ``hang(method)``: the call
  blocks until ``release()`` (or ``HANG_SAFETY_S`` of real time, so a broken test cannot hang the
  run). ``hang_command(fragment)``: the same for a command whose argv line contains ``fragment``
  (it never starts).
- ``fs`` (``ToyFs``): a sandbox path under ``root`` is used as is (tests hand their tmp dirs to the
  backend as workspace/temp dirs, as with the fake docker client); any other absolute path lives
  below ``root/_vfs``. Tar export/import and whole-tree capture/restore back snapshots.
- ``run(argv, …)``: runs real ``bash`` (or any argv) on this machine with a clean environment, or
  answers from ``script(pattern, …)`` rules first.
"""

from __future__ import annotations

import fnmatch
import io
import os
import shlex
import shutil
import subprocess
import tarfile
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from tests.fakes.clock import FakeClock

HANG_SAFETY_S = 30.0
VFS = "_vfs"
SANDBOX_HOME = "_home"
CLEAN_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


@dataclass(frozen=True)
class Call:
    method: str
    args: tuple[Any, ...]
    kwargs: Mapping[str, Any]


@dataclass
class _Failure:
    times: int
    status: int | None = None
    message: str = ""
    error: BaseException | None = None


@dataclass
class _Rule:
    pattern: str
    stdout: bytes
    stderr: bytes
    rc: int


@dataclass
class RunResult:
    stdout: bytes
    stderr: bytes
    returncode: int
    timed_out: bool = False


class ToyFs:
    """The sandbox file system of one ``Cloud`` (see the module notes)."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.vfs = self.root / VFS
        self.home = self.root / SANDBOX_HOME
        self._base_dirs()

    def _base_dirs(self) -> None:
        self.vfs.mkdir(parents=True, exist_ok=True)
        self.home.mkdir(parents=True, exist_ok=True)

    def host(self, path: str) -> Path:
        """Where sandbox ``path`` lives on this machine."""
        if not path.startswith("/"):
            raise ValueError(f"sandbox paths are absolute: {path!r}")
        pure = PurePosixPath(os.path.normpath(path))
        candidate = Path(str(pure))
        if candidate == self.root or self.root in candidate.parents:
            return candidate
        return self.vfs / str(pure).lstrip("/")

    def read(self, path: str) -> bytes:
        return self.host(path).read_bytes()

    def write(self, path: str, data: bytes, mode: int = 0o600) -> None:
        target = self.host(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(mode)

    def exists(self, path: str) -> bool:
        return self.host(path).exists()

    def is_dir(self, path: str) -> bool:
        return self.host(path).is_dir()

    def size(self, path: str) -> int:
        return self.host(path).stat().st_size

    def mkdir(self, path: str) -> None:
        self.host(path).mkdir(parents=True, exist_ok=True)

    def listdir(self, path: str) -> list[str]:
        return sorted(p.name for p in self.host(path).iterdir())

    def remove(self, path: str, *, recursive: bool = False) -> None:
        target = self.host(path)
        if target.is_dir() and not target.is_symlink():
            if not recursive:
                target.rmdir()
            else:
                shutil.rmtree(target)
        else:
            target.unlink()

    def rename(self, source: str, dest: str) -> None:
        target = self.host(dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        self.host(source).rename(target)

    def export_tar(self, path: str) -> bytes:
        """A tar of ``path``'s contents, members relative to ``path``."""
        buffer = io.BytesIO()
        base = self.host(path)
        with tarfile.open(fileobj=buffer, mode="w") as tar:
            for item in sorted(base.rglob("*")):
                tar.add(item, arcname=str(item.relative_to(base)), recursive=False)
        return buffer.getvalue()

    def import_tar(self, data: bytes, dest: str) -> None:
        target = self.host(dest)
        target.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r") as tar:
            tar.extractall(target, filter="data")

    def capture(self) -> bytes:
        """The whole sandbox tree (everything under ``root``), for a snapshot."""
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as tar:
            for item in sorted(self.root.rglob("*")):
                tar.add(item, arcname=str(item.relative_to(self.root)), recursive=False)
        return buffer.getvalue()

    def wipe(self) -> None:
        for item in self.root.iterdir():
            if item.is_dir() and not item.is_symlink():
                shutil.rmtree(item)
            else:
                item.unlink()
        self._base_dirs()

    def restore(self, data: bytes) -> None:
        self.wipe()
        with tarfile.open(fileobj=io.BytesIO(data), mode="r") as tar:
            tar.extractall(self.root, filter="data")
        self._base_dirs()


class Cloud:
    """One fake vendor account (see the module notes)."""

    def __init__(self, root: Path, *, clock: FakeClock | None = None) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.root = root.resolve()
        self.clock = clock
        self.fs = ToyFs(self.root)
        self.home = self.fs.home
        self.calls: list[Call] = []
        self.snapshots: dict[str, bytes] = {}
        self.images: dict[str, dict[str, bytes]] = {}
        self._failures: dict[str, list[_Failure]] = {}
        self._delays: dict[str, float] = {}
        self._hangs: set[str] = set()
        self._hang_commands: list[str] = []
        self._released = threading.Event()
        self._rules: list[_Rule] = []
        self._seq = 0
        self._lock = threading.Lock()
        self.ran: list[list[str]] = []

    # -- ids

    def next_id(self, prefix: str) -> str:
        with self._lock:
            self._seq += 1
            return f"{prefix}{self._seq:06d}"

    # -- scripting

    def fail(self, method: str, status: int, times: int = 1, message: str = "") -> None:
        self._failures.setdefault(method, []).append(_Failure(times, status, message))

    def fail_with(self, method: str, error: BaseException, times: int = 1) -> None:
        self._failures.setdefault(method, []).append(_Failure(times, error=error))

    def delay(self, method: str, seconds: float) -> None:
        self._delays[method] = seconds

    def hang(self, method: str) -> None:
        self._hangs.add(method)

    def hang_command(self, fragment: str) -> None:
        self._hang_commands.append(fragment)

    def _hold(self, argv: Sequence[str]) -> None:
        """Block a command named by ``hang_command`` until ``release()``; it then never ran."""
        line = shlex.join(argv)
        if not any(fragment in line for fragment in self._hang_commands):
            return
        self._released.wait(HANG_SAFETY_S)
        raise TimeoutError(f"command {line[:60]!r} hung until released")

    def release(self) -> None:
        """Let every hanging call return (it then raises ``TimeoutError``)."""
        self._released.set()

    def script(
        self, pattern: str, stdout: bytes | str = b"", rc: int = 0, stderr: bytes = b""
    ) -> None:
        out = stdout.encode() if isinstance(stdout, str) else stdout
        self._rules.append(_Rule(pattern, out, stderr, rc))

    def methods(self) -> list[str]:
        return [call.method for call in self.calls]

    def calls_of(self, method: str) -> list[Call]:
        return [call for call in self.calls if call.method == method]

    # -- the hook every fake method calls first

    def enter(
        self,
        method: str,
        args: tuple[Any, ...],
        kwargs: Mapping[str, Any],
        make_error: Callable[[int, str], BaseException],
    ) -> None:
        with self._lock:
            self.calls.append(Call(method, args, dict(kwargs)))
            queued = self._failures.get(method) or []
            failure = queued[0] if queued else None
            if failure is not None:
                failure.times -= 1
                if failure.times <= 0:
                    queued.pop(0)
        seconds = self._delays.get(method)
        if seconds:
            if self.clock is None:
                raise RuntimeError("Cloud.delay needs the Cloud to have a FakeClock")
            self.clock.sleep_sync(seconds)
        if method in self._hangs and not self._released.wait(HANG_SAFETY_S):
            raise TimeoutError(f"{method} hung past the fake's safety net")
        if method in self._hangs:
            raise TimeoutError(f"{method} was released after hanging")
        if failure is None:
            return
        if failure.error is not None:
            raise failure.error
        assert failure.status is not None
        raise make_error(failure.status, failure.message or f"scripted {failure.status}")

    # -- running commands

    def base_env(self, env: Mapping[str, str] | None) -> dict[str, str]:
        out = {"PATH": CLEAN_PATH, "HOME": str(self.home), "LANG": "C.UTF-8"}
        out.update({k: v for k, v in (env or {}).items() if v is not None})
        return out

    def host_cwd(self, cwd: str | None) -> str:
        if not cwd:
            return str(self.home)
        target = self.fs.host(cwd)
        target.mkdir(parents=True, exist_ok=True)
        return str(target)

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        stdin: bytes | None = None,
        timeout: float | None = None,
    ) -> RunResult:
        line = shlex.join(argv)
        for rule in self._rules:
            if fnmatch.fnmatchcase(line, rule.pattern):
                return RunResult(rule.stdout, rule.stderr, rule.rc)
        self._hold(argv)
        self.ran.append(list(argv))
        try:
            done = subprocess.run(  # noqa: S603 - the fake sandbox runs the test's command
                list(argv),
                cwd=self.host_cwd(cwd),
                env=self.base_env(env),
                input=stdin if stdin is not None else b"",
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return RunResult(exc.stdout or b"", exc.stderr or b"", 137, timed_out=True)
        return RunResult(done.stdout, done.stderr, done.returncode)

    def start(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.Popen[bytes]:
        """A running process with pipes (for SDKs with a stdin stream)."""
        self._hold(argv)
        self.ran.append(list(argv))
        return subprocess.Popen(  # noqa: S603 - the fake sandbox runs the test's command
            list(argv),
            cwd=self.host_cwd(cwd),
            env=self.base_env(env),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

    def scripted(self, argv: Sequence[str]) -> RunResult | None:
        line = shlex.join(argv)
        for rule in self._rules:
            if fnmatch.fnmatchcase(line, rule.pattern):
                return RunResult(rule.stdout, rule.stderr, rule.rc)
        return None


@dataclass
class Recorder:
    """A tiny helper to record method calls on a fake object: ``record(name, *a, **kw)``."""

    cloud: Cloud
    owner: str
    make_error: Callable[[int, str], BaseException]
    extra: dict[str, Any] = field(default_factory=dict)

    def __call__(self, method: str, *args: Any, **kwargs: Any) -> None:
        self.cloud.enter(f"{self.owner}.{method}", args, kwargs, self.make_error)


def public_names(cls: type) -> list[str]:
    """Public attribute names a fake class defines (methods and properties), for the surface
    self-test."""
    names: set[str] = set()
    for klass in cls.__mro__:
        if klass is object:
            continue
        for name in vars(klass):
            if not name.startswith("_"):
                names.add(name)
    return sorted(names)
