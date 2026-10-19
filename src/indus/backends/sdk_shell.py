# pyright: strict
"""The shared flow of the SDK sandbox backends: Modal, Daytona, Vercel Sandbox (P21-05..09).

These backends have no local process: a command is one vendor "exec" call. ``SdkEnvironment``
keeps the P05 spawn-per-command flow (snapshot, wrapper, cwd marker, capture, wait loop, backstop)
and replaces the process by an ``SdkProcess``: the vendor call runs on the SDK call wrapper (P21-02)
and feeds the output it reads into the handle, which the wait loop polls and drains like a pipe.

Every script the sandbox runs:

- first writes its shell pid to ``<temp>/indus-exec-<hex>.pid`` (removed on exit) and folds stderr
  into stdout, so a stop can signal the command's process group through a second exec (none of the
  three SDKs can kill one exec at the versions we pin, or only the Vercel one, which we also call);
- runs under the shell guard ``timeout -k 2 <N> bash -c …`` (N = command timeout + 2 s), so a
  vendor that ignores its own timeout still stops the command (R02.30);
- gets its stdin either streamed (``pipe``: Modal) or staged as a file the script opens and deletes
  before any user code runs (``payload``: Daytona, Vercel; P21-03). Without stdin a payload script
  reads ``/dev/null``. An encode failure is reported as ``stdin_error`` and the command runs
  without stdin;
- gets the forwarded variables (``terminal.env_passthrough``, P21-11) through the SDK's own env
  parameter, never as text in the script (R02.7); a forwarded name without a value now is unset at
  the top of the script, and every forwarded name is kept out of the shell snapshot (R02.1).

Vendor errors while a command runs (link lost, deadline of the wrapper) raise
``EnvironmentConnectionError``: the registry answers ``degraded`` and starts a fresh environment.

Ports (served by ``sdk_ports``: ``VendorSupport`` and ``ProfileRecords``):

- ``SdkSupport``: ``call`` (deadline + retries, R02.30), ``guard`` (shell timeout), ``stage_stdin``
  and ``stdin_chunks`` (P21-03).
- ``SandboxRecords``: the per-kind snapshot records of one profile (P21-01); async, since the
  store locks and fsyncs a file (the implementation runs it off the event loop).

Opening (``connect``): home detection (``echo $HOME``, 10 s), the ``~/.indus`` dirs, the first file
push (P11-10) and the shell snapshot. Before each command: the backend's readiness hook (start a
stopped sandbox, replace a dead one) and the rate-limited push. Cleanup: sync-back (P21-10) of the
mirrored dirs, then the backend's teardown (snapshot / stop / delete).
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import contextvars
import logging
import math
import os
import posixpath
import queue
import secrets
import shlex
import threading
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from indus.backends.capture import DEFAULT_MAX_CHARS
from indus.backends.contract import (
    ActivityCallback,
    BackendError,
    BackendFlags,
    CancelSignal,
    EnvironmentConnectionError,
    ExecResult,
)
from indus.backends.credential_files import resolve as resolve_credentials
from indus.backends.remote_sync import (
    CACHE_SUBDIRS,
    FileSync,
    SyncItem,
    SyncTransport,
    collect_items,
)
from indus.backends.sdk_call import (
    BULK_FILE_DEADLINE_S,
    CREATE_DEADLINE_S,
    FILE_DEADLINE_S,
    SNAPSHOT_DEADLINE_S,
    STOP_DEADLINE_S,
)
from indus.backends.sdk_call import EXEC_GRACE_S as _EXEC_GRACE_S
from indus.backends.spawned import SNAPSHOT_TIMEOUT_S, SpawnedEnvironment
from indus.backends.vendor_env import passthrough_plan, unset_line
from indus.backends.wait import ProcessHandle
from indus.foundation.clock import Clock
from indus.foundation.config.schema import default_for
from indus.foundation.scope import Scope
from indus.safety.redaction import redact_output

__all__ = [
    "BULK_TIMEOUT_S",
    "CREATE_TIMEOUT_S",
    "DELETE_TIMEOUT_S",
    "EXEC_GRACE_S",
    "FILE_TIMEOUT_S",
    "GUARD_SLACK_S",
    "HOME_TIMEOUT_S",
    "KILL_TIMEOUT_S",
    "OP_BULK",
    "OP_CREATE",
    "OP_DELETE",
    "OP_EXEC",
    "OP_FILE",
    "OP_GET",
    "OP_SNAPSHOT",
    "OP_STOP",
    "REMOTE_APP_DIR",
    "SNAPSHOT_OP_TIMEOUT_S",
    "STOP_TIMEOUT_S",
    "SandboxRecords",
    "SdkEnvironment",
    "SdkProcess",
    "SdkSupport",
    "StagedStdin",
    "config_value",
    "credential",
    "FileApiTransport",
    "kill_script",
    "mirrored_roots",
]

log = logging.getLogger(__name__)

# Operation names handed to the call wrapper (it picks retries by them; the deadline is explicit).
OP_EXEC = "exec"
OP_CREATE = "create"
OP_GET = "get"
OP_SNAPSHOT = "snapshot"
OP_STOP = "stop"
OP_FILE = "file"
OP_BULK = "bulk"
OP_DELETE = "delete"

# Per-operation deadlines: the call wrapper's (P21-02).
EXEC_GRACE_S = _EXEC_GRACE_S  # an exec call may take the command timeout plus this
CREATE_TIMEOUT_S = CREATE_DEADLINE_S
SNAPSHOT_OP_TIMEOUT_S = SNAPSHOT_DEADLINE_S
STOP_TIMEOUT_S = STOP_DEADLINE_S
FILE_TIMEOUT_S = FILE_DEADLINE_S
BULK_TIMEOUT_S = BULK_FILE_DEADLINE_S
DELETE_TIMEOUT_S = STOP_DEADLINE_S
HOME_TIMEOUT_S = 10.0
DIRS_TIMEOUT_S = 10.0
KILL_TIMEOUT_S = 5.0
GUARD_SLACK_S = 2  # the shell guard fires after our own deadline, never before it
KILLED_RC = -15

REMOTE_APP_DIR = ".indus"
REMOTE_SUBDIRS = ("skills", "credentials", "cache")


# -- ports -----------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StagedStdin:
    """A staged stdin file: ``prefix`` goes in front of the script (opens and removes the file);
    ``path`` is where it was written, removed by us when the command never started."""

    prefix: str = ""
    error: str | None = None
    path: str = ""


type WriteFile = Callable[[str, bytes], Awaitable[None]]
type FileSize = Callable[[str], Awaitable[int]]
type RemoveFile = Callable[[str], Awaitable[object]]


class SdkSupport(Protocol):
    """What every SDK backend needs from the call wrapper and the stdin helpers (P21-02/03)."""

    async def call[T](
        self,
        op: str,
        fn: Callable[[], T],
        *,
        timeout: float,
        idempotent: bool = False,
        on_deadline: Callable[[], None] | None = None,
    ) -> T:
        """Run the blocking vendor call ``fn`` within ``timeout``; retry transient failures only
        when ``idempotent``; on the deadline fire ``on_deadline`` once and raise."""
        ...

    def guard(self, script: str, seconds: float) -> str:
        """``script`` wrapped in the in-sandbox shell timeout (``timeout -k 2 N bash -c …``)."""
        ...

    async def stage_stdin(
        self,
        data: str,
        *,
        temp_dir: str,
        write: WriteFile,
        size: FileSize,
        remove: RemoveFile | None = None,
    ) -> StagedStdin:
        """Upload ``data`` as a file the script reads stdin from; an incomplete upload is removed
        through ``remove`` and reported as ``error``."""
        ...

    def stdin_chunks(self, data: str) -> list[bytes]:
        """``data`` as the byte chunks to stream; raises ``ValueError`` when it cannot be
        encoded."""
        ...


class SandboxRecords(Protocol):
    """Snapshot/name records of one backend kind in one profile (keys are task keys; the store
    prefixes the profile id, R02.8)."""

    async def load(self, task_key: str) -> Mapping[str, Any] | None: ...

    async def save(self, task_key: str, fields: Mapping[str, Any]) -> None: ...

    async def drop(self, task_key: str, *, expect: Mapping[str, Any] | None = None) -> None:
        """Remove the record; with ``expect``, only when those fields still hold those values."""
        ...


# -- small helpers ---------------------------------------------------------------------------------


def config_value(scope: Scope, key: str) -> Any:
    value = scope.config.get(key) if scope.config is not None else None
    return default_for(key) if value is None else value


def credential(scope: Scope, name: str) -> str | None:
    """A vendor credential: the profile's secret store first, then the scope env (D-16.22)."""
    value: str | None = None
    if scope.secrets is not None:
        try:
            value = scope.secrets.get(name)
        except Exception:
            log.debug("secret store lookup of %s failed", name, exc_info=True)
    if not value:
        value = scope.env.get(name)
    value = (value or "").strip()
    return value or None


def kill_script(pid_file: str, signal_name: str) -> str:
    """Signal the process group of the shell whose pid is in ``pid_file``, its children and the
    shell itself. Silent and exit 0 whatever is (no longer) there."""
    q = shlex.quote(pid_file)
    return (
        f'p=$(cat {q} 2>/dev/null) || exit 0; [ -n "$p" ] || exit 0; '
        f'g=$(cut -d" " -f5 /proc/"$p"/stat 2>/dev/null); '
        f'[ -n "$g" ] && [ "$g" != 0 ] && kill -{signal_name} -- -"$g" 2>/dev/null; '
        f'pkill -{signal_name} -P "$p" 2>/dev/null; kill -{signal_name} "$p" 2>/dev/null; exit 0'
    )


def mirrored_roots(scope: Scope, remote_base: str) -> list[tuple[str, str]]:
    """(remote dir, host dir) pairs a sync-back may write into: the dirs the push mirrors."""
    base = remote_base.rstrip("/")
    roots = [(f"{base}/skills", str(scope.home / "skills"))]
    roots += [(f"{base}/cache/{sub}", str(scope.home / "cache" / sub)) for sub in CACHE_SUBDIRS]
    return roots


def _describe_error(exc: BaseException) -> str:
    text = str(exc).strip().splitlines()
    return f"{type(exc).__name__}: {text[-1]}" if text else type(exc).__name__


# -- the process handle ----------------------------------------------------------------------------


class SdkProcess:
    """One vendor exec as the wait loop sees it (meets ``wait.ProcessHandle``).

    The vendor call (on a worker thread) calls ``feed`` with what it reads and the wrapper's task
    settles the exit status. ``binary`` sinks take raw bytes (downloads) instead of text.
    """

    def __init__(self, *, binary: Callable[[bytes], None] | None = None) -> None:
        self._chunks: queue.SimpleQueue[str] = queue.SimpleQueue()
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._done = threading.Event()
        self._lock = threading.Lock()
        self._rc: int | None = None
        self.binary = binary
        self.error: BaseException | None = None
        self.cancelled = False
        self.pid_file = ""
        self.stdin_error: str | None = None
        self._stdin_task: asyncio.Future[str | None] | None = None
        self.job: asyncio.Future[Any] | None = None
        self.text_parts: list[str] = []  # kept for short internal calls (``run_simple``)
        self.keep_text = False
        self.vendor: Any = None  # the vendor's own process handle, when it has one
        self.staged_path = ""  # a staged stdin file the script removes once it runs
        self.env: dict[str, str] = {}  # forwarded variables, for the SDK's env parameter

    @property
    def pid(self) -> int | None:
        return None

    @property
    def done(self) -> bool:
        return self._done.is_set()

    def feed(self, data: bytes | str) -> None:
        if self._done.is_set():
            return
        if isinstance(data, bytes):
            if self.binary is not None:
                self.binary(data)
                return
            text = self._decoder.decode(data)
        else:
            text = data
        if text:
            self._put(text)

    def _put(self, text: str) -> None:
        if self.keep_text:
            self.text_parts.append(text)
        self._chunks.put(text)

    def finish(self, returncode: int) -> None:
        with self._lock:
            if self._done.is_set():
                return
            tail = self._decoder.decode(b"", final=True)
            if tail:
                self._put(tail)
            self._rc = returncode
            self._done.set()

    def fail(self, error: BaseException) -> None:
        if self._done.is_set():
            return
        self.error = error
        self.finish(1)

    def poll(self) -> int | None:
        return self._rc if self._done.is_set() else None

    def drain(self, sink: Callable[[str], None], stop: threading.Event) -> None:
        while True:
            try:
                sink(self._chunks.get(timeout=0.02))
                continue
            except queue.Empty:
                pass
            if self._done.is_set() and self._chunks.empty():
                return
            if stop.is_set():
                return

    @property
    def stdin_task(self) -> asyncio.Future[str | None] | None:
        return self._stdin_task

    def arm_stdin(self) -> None:
        """There is stdin: the wait loop will read its outcome once the command ended."""
        self._stdin_task = asyncio.get_running_loop().create_future()

    def settle_stdin(self) -> None:
        if self._stdin_task is not None and not self._stdin_task.done():
            self._stdin_task.set_result(self.stdin_error)

    def text(self) -> str:
        return "".join(self.text_parts)


# The processes spawned by the current ``_run_script`` call (read after it returns).
_SPAWNED: contextvars.ContextVar[list[SdkProcess] | None] = contextvars.ContextVar(
    "indus_sdk_spawned", default=None
)


@dataclass
class SimpleRun:
    """What a short internal exec returned."""

    returncode: int
    output: str = ""
    error: BaseException | None = field(default=None, repr=False)


# -- the environment -------------------------------------------------------------------------------


class SdkEnvironment(SpawnedEnvironment, ABC):
    """Commands in a vendor sandbox through its SDK (see the module notes)."""

    backend_name = "sdk"
    default_home = "/root"

    def __init__(
        self,
        scope: Scope,
        support: SdkSupport,
        *,
        cwd: str,
        timeout: float,
        temp_dir: str,
        flags: BackendFlags,
        session_id: str | None = None,
        clock: Clock | None = None,
        max_chars: int = DEFAULT_MAX_CHARS,
        snapshot_timeout: float = SNAPSHOT_TIMEOUT_S,
        persist_env: bool = True,
        sync_files: bool = True,
    ) -> None:
        super().__init__(
            cwd=cwd,
            timeout=timeout,
            temp_dir=temp_dir,
            flags=flags,
            persist_env=persist_env,
            clock=clock,
            capture_home=scope.home,
            max_chars=max_chars,
            snapshot_timeout=snapshot_timeout,
        )
        self.scope = scope
        self.support = support
        self.session_id = session_id
        self.temp_dir = temp_dir.rstrip("/") or "/"
        self.remote_home = self.default_home
        self.home_detected = False
        self.recreations = 0
        self.sync_files = sync_files
        self.sync: FileSync | None = None
        self._spawn_timeout = self.timeout

    # -- backend hooks -----------------------------------------------------------------------------

    @abstractmethod
    def _vendor_exec(
        self, argv: list[str], proc: SdkProcess, stdin_chunks: list[bytes] | None, timeout: float
    ) -> int:
        """Blocking: run ``argv`` in the sandbox through the SDK, ``proc.feed`` what it prints,
        return the exit status. ``timeout`` is a vendor-side bound (the wrapper's deadline is
        ours). Runs on a worker thread of the call wrapper."""

    def _vendor_cancel(self, proc: SdkProcess) -> None:  # noqa: B027 - optional hook
        """Blocking, best effort: stop ``proc`` through the vendor's own API, when it has one."""

    @abstractmethod
    def transport(self) -> SyncTransport:
        """The file transport of this sandbox (P11 ``SyncTransport``)."""

    async def write_file(self, path: str, data: bytes) -> None:
        """Write a file in the sandbox (stdin staging of ``payload`` backends)."""
        raise BackendError(f"the {self.backend_name} backend cannot write files")

    async def put_file(self, local_path: str, remote_path: str, timeout: float) -> None:
        """Copy a local file into the sandbox (``FileApiTransport``)."""
        raise BackendError(f"the {self.backend_name} backend cannot upload files")

    async def get_file(self, remote_path: str, local_path: str, timeout: float) -> None:
        """Copy a sandbox file to a local path, in full (``FileApiTransport``)."""
        raise BackendError(f"the {self.backend_name} backend cannot download files")

    async def file_size(self, path: str) -> int:
        """Size in bytes of a sandbox file (verifies a staged payload, R02.16)."""
        run = await self.run_simple(f"wc -c < {shlex.quote(path)}", HOME_TIMEOUT_S)
        try:
            return int(run.output.strip().split()[-1])
        except (ValueError, IndexError):
            raise BackendError(f"could not read the size of {path} in the sandbox") from None

    async def _ensure_ready(self) -> None:  # noqa: B027 - optional hook
        """Before each command: start a stopped sandbox or replace a dead one."""

    @abstractmethod
    async def _teardown(self) -> None:
        """Give the sandbox back (snapshot, stop, delete) after the sync-back."""

    def _adopt_home(self, home: str) -> None:
        """Move a home-relative cwd (``~``) to the detected home."""
        if self._cwd in ("", "~", "."):
            self._cwd = home

    # -- client pieces -----------------------------------------------------------------------------

    def describe(self) -> str:
        return f"{self.backend_name} sandbox, cwd {self._cwd}"

    @property
    def remote_base(self) -> str:
        return posixpath.join(self.remote_home, REMOTE_APP_DIR)

    def connection_error(self, exc: BaseException, what: str) -> EnvironmentConnectionError:
        detail = redact_output(_describe_error(exc), scope=self.scope)
        return EnvironmentConnectionError(
            f"{what} on the {self.backend_name} sandbox failed: {detail}",
            retry_hint=(
                f"Check the {self.backend_name} service status and your credentials, then run the "
                "command again; a new sandbox connection is made automatically."
            ),
        )

    async def run_simple(
        self,
        script: str,
        timeout: float,
        *,
        op: str = OP_EXEC,
        stdin_chunks: list[bytes] | None = None,
        binary: Callable[[bytes], None] | None = None,
    ) -> SimpleRun:
        """One short internal exec (no wrapper, no snapshot): ``bash -c script``. Vendor errors
        raise ``EnvironmentConnectionError``."""
        proc = SdkProcess(binary=binary)
        proc.keep_text = True
        argv = ["bash", "-c", script]
        try:
            code = await self.support.call(
                op,
                lambda: self._vendor_exec(argv, proc, stdin_chunks, timeout),
                timeout=timeout,
            )
        except EnvironmentConnectionError:
            raise
        except Exception as exc:
            raise self.connection_error(exc, "a sandbox call") from exc
        proc.finish(code)
        return SimpleRun(code, proc.text())

    # -- opening -----------------------------------------------------------------------------------

    async def connect(self) -> None:
        """Home, dirs, first push, snapshot (see the module notes)."""
        await self.detect_home()
        await self._make_dirs()
        if self.sync_files:
            self.rebuild_sync()
            if self.sync is not None:
                await self.sync.sync(force=True)
        await self.start()

    async def detect_home(self) -> bool:
        try:
            run = await self.run_simple('printf "%s\\n" "$HOME"', HOME_TIMEOUT_S)
        except EnvironmentConnectionError:
            raise
        except Exception:
            run = SimpleRun(-1)
        lines = [line.strip() for line in run.output.splitlines() if line.strip()]
        home = lines[-1] if lines else ""
        if run.returncode == 0 and home.startswith("/"):
            self.remote_home = home
            self.home_detected = True
            self._adopt_home(home)
            return True
        log.warning(
            "could not detect the home dir in the %s sandbox; using %s",
            self.backend_name,
            self.remote_home,
        )
        self._adopt_home(self.remote_home)
        return False

    async def _make_dirs(self) -> None:
        dirs = " ".join(shlex.quote(posixpath.join(self.remote_base, d)) for d in REMOTE_SUBDIRS)
        temp = shlex.quote(self.temp_dir)
        try:
            run = await self.run_simple(f"mkdir -p -- {dirs} {temp}", DIRS_TIMEOUT_S)
        except EnvironmentConnectionError as exc:
            log.warning("creating %s in the sandbox failed: %s", self.remote_base, exc.reason)
            return
        if run.returncode != 0:
            log.warning("creating %s in the sandbox failed: %s", self.remote_base, run.output)

    def sync_items(self) -> list[SyncItem]:
        credentials = resolve_credentials(self.scope, self.session_id)
        return collect_items(self.scope, self.remote_base, credentials)

    def rebuild_sync(self) -> None:
        """A fresh push state for the current sandbox (a replaced sandbox holds none of it).
        Upload archives are built in a private dir of the profile, not the system temp dir."""
        staging = self.scope.home / "cache" / "sync-push"
        staging.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.sync = FileSync(
            self.sync_items,
            self.transport(),
            base=self.remote_base,
            clock=self.clock,
            env=self.scope.env,
            temp_dir=str(staging),
        )

    async def reopen(self) -> None:
        """The sandbox was replaced: background jobs of the old one are lost (``recreations``),
        the next result says so once, and home, dirs, push and snapshot start over."""
        self.recreations += 1
        self.mark_recreated()
        self.snapshot_ready = False
        self.prefer_nonlogin = False
        await self.connect()

    # -- running -----------------------------------------------------------------------------------

    async def execute(
        self,
        command: str,
        *,
        timeout: float | None = None,
        cwd: str | None = None,
        stdin: str | None = None,
        bounded: bool = True,
        on_activity: ActivityCallback | None = None,
        cancel: CancelSignal | None = None,
    ) -> ExecResult:
        if not self._closed:
            await self._ensure_ready()
            if self.sync is not None:
                await self.sync.sync()
        return await super().execute(
            command,
            timeout=timeout,
            cwd=cwd,
            stdin=stdin,
            bounded=bounded,
            on_activity=on_activity,
            cancel=cancel,
        )

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
        spawned: list[SdkProcess] = []
        token = _SPAWNED.set(spawned)
        self._spawn_timeout = timeout
        try:
            result = await super()._run_script(
                script,
                login=login,
                timeout=timeout,
                stdin=stdin,
                bounded=bounded,
                on_activity=on_activity,
                cancel=cancel,
            )
        finally:
            _SPAWNED.reset(token)
        for proc in spawned:
            if proc.error is not None and not proc.cancelled:
                if result.timed_out or result.interrupted:
                    continue
                raise self.connection_error(proc.error, "running a command")
        return result

    def _excluded_names(self) -> tuple[str, ...]:
        return passthrough_plan(self.scope).names

    def _pid_file(self) -> str:
        return f"{self.temp_dir}/indus-exec-{secrets.token_hex(6)}.pid"

    async def _spawn(self, script: str, *, login: bool, stdin: str | None) -> ProcessHandle:
        proc = SdkProcess()
        proc.pid_file = self._pid_file()
        q = shlex.quote(proc.pid_file)
        head = f"printf '%s' \"$$\" > {q} 2>/dev/null; trap 'rm -f -- {q}' EXIT\nexec 2>&1\n"
        plan = passthrough_plan(self.scope)
        proc.env = dict(plan.values)
        body = unset_line(plan.unset) + script
        chunks: list[bytes] | None = None
        if stdin is not None:
            proc.arm_stdin()
        if self.flags.stdin_mode == "payload":
            staged = await self._stage(stdin, proc) if stdin else None
            body = (staged or "exec 0</dev/null\n") + body
        elif stdin is not None:
            try:
                chunks = self.support.stdin_chunks(stdin)
            except (ValueError, UnicodeError) as exc:
                proc.stdin_error = str(exc) or "stdin could not be encoded"
        inner = head + body
        if login:
            inner = f"exec bash -l -c {shlex.quote(inner)}"
        seconds = math.ceil(max(self._spawn_timeout, 1.0)) + GUARD_SLACK_S
        argv = ["bash", "-c", self.support.guard(inner, seconds)]
        spawned = _SPAWNED.get()
        if spawned is not None:
            spawned.append(proc)

        def run() -> int:
            return self._vendor_exec(argv, proc, chunks, seconds + GUARD_SLACK_S)

        def on_deadline() -> None:
            self._kill_in_background(proc)

        job = asyncio.ensure_future(
            self.support.call(
                OP_EXEC,
                run,
                timeout=self._spawn_timeout + EXEC_GRACE_S,
                idempotent=False,
                on_deadline=on_deadline,
            )
        )
        proc.job = job
        job.add_done_callback(lambda done: self._settle(proc, done))
        return proc

    async def _stage(self, stdin: str, proc: SdkProcess) -> str | None:
        """Stage a payload stdin; the script prefix, or None after recording the error."""
        try:
            staged = await self.support.stage_stdin(
                stdin,
                temp_dir=self.temp_dir,
                write=self.write_file,
                size=self.file_size,
                remove=self._remove_file,
            )
        except EnvironmentConnectionError:
            raise
        except Exception as exc:
            proc.stdin_error = _describe_error(exc)
            return None
        if staged.error:
            proc.stdin_error = staged.error
            return None
        proc.staged_path = staged.path
        return staged.prefix

    async def _remove_file(self, path: str) -> None:
        await self.run_simple(f"rm -f -- {shlex.quote(path)}", DELETE_TIMEOUT_S, op=OP_DELETE)

    def _settle(self, proc: SdkProcess, job: asyncio.Future[Any]) -> None:
        if job.cancelled():
            proc.finish(KILLED_RC)
        elif job.exception() is not None:
            proc.fail(cast(BaseException, job.exception()))
            if proc.staged_path:
                self._scrub_staged(proc.staged_path)
        else:
            proc.finish(int(job.result()))
        proc.settle_stdin()

    def _scrub_staged(self, path: str) -> None:
        """The vendor call failed, so the script may never have removed its staged stdin (it can
        hold a sudo password): remove it, best effort, off the event loop."""

        def work() -> None:
            with contextlib.suppress(Exception):
                argv = ["bash", "-c", f"rm -f -- {shlex.quote(path)}"]
                self._vendor_exec(argv, SdkProcess(), None, KILL_TIMEOUT_S)

        threading.Thread(target=work, name=f"{self.backend_name}-scrub", daemon=True).start()

    # -- stopping ----------------------------------------------------------------------------------

    def _kill_argv(self, proc: SdkProcess, signal_name: str) -> list[str]:
        return ["bash", "-c", kill_script(proc.pid_file, signal_name)]

    async def _signal(self, proc: SdkProcess, signal_name: str, bound: float) -> None:
        if bound <= 0 or not proc.pid_file:
            return
        sink = SdkProcess()
        try:
            await self.support.call(
                OP_EXEC,
                lambda: self._vendor_exec(self._kill_argv(proc, signal_name), sink, None, bound),
                timeout=bound,
            )
        except Exception as exc:
            log.debug("signalling a sandbox command failed: %s", type(exc).__name__)

    async def _kill(self, handle: ProcessHandle, within: float | None = None) -> None:
        proc = cast(SdkProcess, handle)
        proc.cancelled = True
        bound = KILL_TIMEOUT_S if within is None else max(0.0, min(KILL_TIMEOUT_S, within / 2))
        await self._signal(proc, "TERM", bound)
        await self._signal(proc, "KILL", bound)
        try:
            await self.support.call(OP_EXEC, lambda: self._vendor_cancel(proc), timeout=bound or 1)
        except Exception as exc:
            log.debug("cancelling a sandbox command failed: %s", type(exc).__name__)
        proc.finish(KILLED_RC)
        proc.settle_stdin()

    def _kill_in_background(self, proc: SdkProcess) -> None:
        def work() -> None:
            sink = SdkProcess()
            with contextlib.suppress(Exception):
                self._vendor_exec(self._kill_argv(proc, "KILL"), sink, None, KILL_TIMEOUT_S)
            with contextlib.suppress(Exception):
                self._vendor_cancel(proc)

        threading.Thread(target=work, name=f"{self.backend_name}-kill", daemon=True).start()

    def _force_kill(self, handle: ProcessHandle) -> None:
        proc = cast(SdkProcess, handle)
        proc.cancelled = True
        self._kill_in_background(proc)
        proc.finish(KILLED_RC)

    # -- ending ------------------------------------------------------------------------------------

    async def cleanup(self) -> None:
        if self._closed:
            return
        await super().cleanup()
        try:
            await self._sync_back()
        except Exception:
            log.warning("sync-back from the %s sandbox failed", self.backend_name, exc_info=True)
        try:
            await self._teardown()
        except Exception:
            log.warning("cleaning up the %s sandbox failed", self.backend_name, exc_info=True)

    async def _sync_back(self) -> None:
        if self.sync is None:
            return
        from indus.backends.sync_back import credential_hosts, sync_back

        files = resolve_credentials(self.scope, self.session_id)
        await sync_back(
            self.sync,
            home=self.scope.home,
            remote_dir=self.remote_base,
            roots=mirrored_roots(self.scope, self.remote_base),
            upload_only_hosts=credential_hosts(files),
            max_bytes=int(config_value(self.scope, "terminal.sync_back_max_bytes")),
            clock=self.clock,
        )


# -- transport over a file API ---------------------------------------------------------------------


def _parent(path: str) -> str:
    return posixpath.dirname(path.rstrip("/")) or "/"


class FileApiTransport:
    """``SyncTransport`` for sandboxes with a file API (Daytona, Vercel): single files through
    the API with a byte-count check (R02.16); many files as one tar uploaded to the sandbox temp
    dir and unpacked there; downloads as a tar written there and fetched in full (R02.2)."""

    def __init__(self, env: SdkEnvironment) -> None:
        self.env = env

    def _temp_tar(self) -> str:
        return f"{self.env.temp_dir}/indus-sync-{secrets.token_hex(6)}.tar"

    async def _checked(self, script: str, timeout: float, op: str, what: str) -> None:
        run = await self.env.run_simple(script, timeout, op=op)
        if run.returncode != 0:
            detail = run.output.strip().splitlines()
            raise BackendError(
                f"{what} in the {self.env.backend_name} sandbox failed (exit {run.returncode})"
                + (f": {detail[-1]}" if detail else "")
            )

    async def upload_one(self, path: str, dest: str) -> None:
        await self._checked(
            f"mkdir -p -- {shlex.quote(_parent(dest))}", DIRS_TIMEOUT_S, OP_FILE, "creating a dir"
        )
        await self.env.put_file(path, dest, FILE_TIMEOUT_S)
        expected = os.path.getsize(path)
        got = await self.env.file_size(dest)
        if got != expected:
            raise BackendError(f"copied {got} of {expected} bytes to {dest} in the sandbox")

    async def upload_bulk(self, tar_path: str) -> None:
        remote = self._temp_tar()
        base = shlex.quote(self.env.remote_base)
        q = shlex.quote(remote)
        await self.env.put_file(tar_path, remote, BULK_TIMEOUT_S)
        await self._checked(
            f"mkdir -p -- {base} && tar -xf {q} -C {base}; rc=$?; rm -f -- {q}; exit $rc",
            BULK_TIMEOUT_S,
            OP_BULK,
            "unpacking synced files",
        )

    async def download_bulk(self, src: str, dest: str) -> None:
        remote = self._temp_tar()
        q = shlex.quote(remote)
        rel = shlex.quote(src.strip("/") or ".")
        try:
            await self._checked(
                f"tar cf {q} --exclude='*.sock' -C / {rel}",
                BULK_TIMEOUT_S,
                OP_BULK,
                "packing remote files",
            )
            await self.env.get_file(remote, dest, BULK_TIMEOUT_S)
        finally:
            with contextlib.suppress(Exception):
                await self.env.run_simple(f"rm -f -- {q}", DELETE_TIMEOUT_S, op=OP_DELETE)

    async def delete(self, paths: Sequence[str]) -> None:
        if not paths:
            return
        quoted = " ".join(shlex.quote(p) for p in paths)
        await self._checked(f"rm -f -- {quoted}", DELETE_TIMEOUT_S, OP_DELETE, "removing files")
