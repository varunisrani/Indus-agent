# pyright: strict
"""Background jobs: the process registry (P05-13).

``ProcessRegistry`` starts commands that keep running after the tool call returns, keeps their
output and state, and announces exits, watched lines and heartbeats through ``process_notify``.

Starting on the host: bash in a new session and process group, stdout and stderr in one pipe — or
a pseudo-terminal when asked (``ptyprocess``, extra ``pty``; without it the job runs on pipes and
says so). On Linux with a systemd user manager the job runs in its own capped transient scope
(``memory_scope``, P24-43); a SIGKILL there that Indus did not send ends it as ``killed: memory
cap``. A request that names a non-local environment (Docker, SSH, ...) starts the job inside
it through ``remote_processes`` (P11-12): output arrives from a poller instead of a pipe. The
record, with its owner (profile home, session id, environment key), is in the table before any
output is read. A start that fails, or yields no pid, leaves a record that is ``exited`` with
reason ``failed_start``; it is never reported as running.

No thread per job: the output descriptor is non-blocking and the event loop reads it when it is
readable; the exit is noticed through a Linux pidfd (elsewhere a 0.2 s poll on the clock). At the
exit whatever is already in the pipe is read without waiting, so a grandchild that keeps the pipe
open (``server &`` inside the job) cannot hold the completion back. Output goes into a rolling
buffer of ``MAX_BUFFER_CHARS`` characters.

Ids are ``proc_`` + 12 hex digits; any unique prefix of at least ``MIN_PREFIX_HEX`` hex digits
finds a job. Lookups only see the caller's profile (R02.8). At most ``MAX_TRACKED`` jobs are kept:
finished ones go after ``FINISHED_TTL_S`` or, under pressure, least recently used first; with
``MAX_TRACKED`` jobs still running a new one is refused.

Stopping uses the local backend's ordered kill (P05-03, R02.12). ``release`` (session end,
``/new``) stops a session's jobs except those started with ``persist_on_release``; ``shutdown``
(process exit) stops every job.

Every exit writes a retained receipt (``process_receipts``, P11-13) before any completion event
goes out. The running jobs of each profile are listed in its crash checkpoint
(``process_checkpoint``, P11-14), rewritten when a job starts or ends; the first time a registry
serves a profile (``recover``) it adopts the jobs a crashed predecessor left running as
``detached`` records and marks the rest ``lost``.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import logging
import os
import posixpath
import re
import secrets
import shlex
import signal
import subprocess
import threading
import warnings
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import IO, Any, Literal, Protocol, cast

from indus.backends import memory_scope, process_checkpoint, process_receipts, remote_processes
from indus.backends.contract import BackendError, Environment
from indus.backends.local import LocalProcess, force_kill_tree, stop_tree
from indus.backends.process_notify import (
    COMPLETION_TAIL_CHARS,
    HEARTBEAT_MIN_S,
    HEARTBEAT_TICK_S,
    WATCH_MAX_CHARS,
    WATCH_MAX_LINES,
    Deliveries,
    GlobalBreaker,
    HeartbeatState,
    ProcessEvent,
    WatchState,
    breaker_text,
    completion_text,
    heartbeat_text,
    watch_off_text,
    watch_text,
)
from indus.backends.wrapper import rewrite_compound_background
from indus.foundation.blocking import run_blocking
from indus.foundation.clock import Clock, SystemClock
from indus.foundation.config.schema import default_for
from indus.foundation.scope import Scope
from indus.platform import locate
from indus.safety.redaction import redact_output

__all__ = [
    "FINISHED_TTL_S",
    "MAX_BUFFER_CHARS",
    "MAX_HANDOFFS",
    "MAX_TRACKED",
    "MIN_PREFIX_HEX",
    "HandoffGrant",
    "JobHandle",
    "LaunchSpec",
    "ProcessLimit",
    "ProcessRecord",
    "ProcessRegistry",
    "Reason",
    "SpawnRequest",
    "launch_local",
    "strip_ansi",
    "uptime_s",
]

log = logging.getLogger(__name__)

MAX_BUFFER_CHARS = 200_000
MAX_TRACKED = 64
MAX_HANDOFFS = 3  # jobs one subagent may hand to its parent (P10-06)
FINISHED_TTL_S = 30 * 60
MIN_PREFIX_HEX = 4
ID_PREFIX = "proc_"
EXIT_POLL_S = 0.2
READ_CHUNK = 65536
READS_PER_WAKE = 16  # ~1 MB per loop wake-up, then give way
FINAL_DRAIN_MAX = 4 * 1024 * 1024
STDIN_DEADLINE_S = 5.0
WAIT_SLICE_S = 1.0
PTY_SIZE = (30, 120)

PTY_MISSING_NOTE = (
    "A pseudo-terminal needs the optional 'pty' extra (pip install 'indus-agent[pty]'); this "
    "process runs with plain pipes instead."
)

_ANSI = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"  # CSI sequences (colours, cursor moves)
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC sequences (titles, links)
    r"|\x1b[PX^_][^\x1b]*\x1b\\"  # DCS / SOS / PM / APC strings
    r"|\x1b[@-Z\\-_]"  # two-character escapes
)


def strip_ansi(text: str) -> str:
    """``text`` without terminal escape sequences."""
    return _ANSI.sub("", text) if "\x1b" in text else text


type Reason = Literal["exited", "killed", "lost", "failed_start"]
type WaitOutcome = Literal["exited", "timeout", "interrupted"]


class ProcessLimit(BackendError):
    default_code = "backend.process_limit"


class CancelLike(Protocol):
    def is_set(self) -> bool: ...


# -- handles --------------------------------------------------------------------------------------


class JobHandle(Protocol):
    """A started job as the registry drives it."""

    @property
    def pid(self) -> int | None: ...

    @property
    def fd(self) -> int | None:
        """Non-blocking descriptor to read output from; None when output arrives through
        ``ProcessRegistry.ingest`` instead."""
        ...

    @property
    def pty(self) -> bool: ...

    def poll(self) -> int | None: ...

    async def write(self, data: str) -> str | None:
        """Send ``data`` to the job's input; an error text, or None."""
        ...

    def close_input(self) -> str | None: ...

    async def stop(self, clock: Clock, grace: float) -> None:
        """Ordered kill of the job's process tree."""
        ...

    def force_stop(self) -> None: ...

    def release(self) -> None:
        """Close what the handle holds open (after the exit)."""
        ...


@dataclass(frozen=True, slots=True)
class LaunchSpec:
    argv: tuple[str, ...]
    cwd: str
    env: Mapping[str, str]
    use_pty: bool = False


type Launcher = Callable[[LaunchSpec], JobHandle]


def _write_all(pipe: IO[bytes], raw: bytes) -> str | None:
    try:
        pipe.write(raw)
        pipe.flush()
    except BrokenPipeError:
        return "the process has closed its input"
    except (OSError, ValueError) as exc:
        return f"could not write to the process: {exc}"
    return None


class PipeHandle:
    """A job on plain pipes (``subprocess.Popen``)."""

    pty = False

    def __init__(self, popen: subprocess.Popen[bytes]) -> None:
        self.popen = popen
        self.local = LocalProcess(popen)
        out = popen.stdout
        self._fd: int | None = out.fileno() if out is not None else None
        if self._fd is not None:
            os.set_blocking(self._fd, False)

    @property
    def pid(self) -> int | None:
        return self.popen.pid

    @property
    def fd(self) -> int | None:
        return self._fd

    def poll(self) -> int | None:
        return self.popen.poll()

    async def write(self, data: str) -> str | None:
        pipe = self.popen.stdin
        if pipe is None or pipe.closed:
            return "the process input is closed"
        raw = data.encode("utf-8", "surrogateescape")
        try:
            return await run_blocking(
                _write_all, pipe, raw, pool="io", deadline=STDIN_DEADLINE_S, label="job stdin"
            )
        except TimeoutError:
            return "the process is not reading its input"

    def close_input(self) -> str | None:
        pipe = self.popen.stdin
        if pipe is None or pipe.closed:
            return "the process input is already closed"
        with contextlib.suppress(OSError):
            pipe.close()
        return None

    async def stop(self, clock: Clock, grace: float) -> None:
        await stop_tree(self.local, clock=clock, grace=grace)

    def force_stop(self) -> None:
        force_kill_tree(self.local)

    def release(self) -> None:
        for stream in (self.popen.stdout, self.popen.stdin):
            if stream is not None:
                with contextlib.suppress(OSError, ValueError):
                    stream.close()
        self._fd = None


class _PtyAsPopen:
    """Just enough of ``Popen`` for the local backend's kill functions."""

    stdout = None
    stdin = None

    def __init__(self, handle: PtyHandle) -> None:
        self._handle = handle

    @property
    def pid(self) -> int:
        return self._handle.child_pid

    @property
    def returncode(self) -> int | None:
        return self._handle.poll()

    def poll(self) -> int | None:
        return self._handle.poll()

    def send_signal(self, sig: int) -> None:
        os.kill(self.pid, sig)

    def kill(self) -> None:
        os.kill(self.pid, signal.SIGKILL)


class PtyHandle:
    """A job on a pseudo-terminal (``ptyprocess``; untyped, so used through ``Any``)."""

    pty = True

    def __init__(self, proc: Any) -> None:
        self._proc: Any = proc
        self.child_pid = int(proc.pid)
        self._fd: int | None = int(proc.fd)
        os.set_blocking(self._fd, False)
        self._code: int | None = None
        self.local = LocalProcess(cast(Any, _PtyAsPopen(self)))

    @property
    def pid(self) -> int | None:
        return self.child_pid

    @property
    def fd(self) -> int | None:
        return self._fd

    def poll(self) -> int | None:
        if self._code is not None:
            return self._code
        try:
            alive = bool(self._proc.isalive())
        except Exception:
            alive = False
        if alive:
            return None
        status: Any = self._proc.exitstatus
        if status is None:
            sig: Any = self._proc.signalstatus
            status = -int(sig) if sig is not None else -1
        self._code = int(status)
        return self._code

    async def write(self, data: str) -> str | None:
        fd = self._fd
        if fd is None:
            return "the terminal is closed"
        raw = data.encode("utf-8", "surrogateescape")
        while raw:
            try:
                written = os.write(fd, raw)
            except BlockingIOError:
                return "the terminal input buffer is full; the process is not reading"
            except OSError as exc:
                return f"could not write to the terminal: {exc}"
            raw = raw[written:]
        return None

    def close_input(self) -> str | None:
        try:
            self._proc.sendeof()
        except Exception as exc:
            return f"could not send end-of-input: {exc}"
        return None

    async def stop(self, clock: Clock, grace: float) -> None:
        await stop_tree(self.local, clock=clock, grace=grace)

    def force_stop(self) -> None:
        force_kill_tree(self.local)

    def release(self) -> None:
        # Close the descriptor ourselves: the library's own close() sleeps and may signal.
        with contextlib.suppress(Exception):
            self._proc.fileobj.close()
        with contextlib.suppress(Exception):
            self._proc.closed = True
        self._fd = None


def launch_local(spec: LaunchSpec) -> JobHandle:
    """Start ``spec`` on this machine. Raises ``ImportError`` for a PTY without the extra."""
    if spec.use_pty:
        from ptyprocess import PtyProcess  # pyright: ignore[reportMissingTypeStubs]

        env = dict(spec.env)
        env.setdefault("PAGER", "cat")  # a real terminal makes pagers wait for a key
        env.setdefault("GIT_PAGER", "cat")
        with warnings.catch_warnings():
            # The library forks a multi-threaded process; its child only sets up the terminal
            # and execs, so the interpreter's generic warning about forkpty() is silenced here.
            warnings.filterwarnings("ignore", message=r".*forkpty\(\) may lead to deadlocks")
            proc: Any = cast(Any, PtyProcess).spawn(
                list(spec.argv), cwd=spec.cwd, env=env, dimensions=PTY_SIZE
            )
        return PtyHandle(proc)
    popen = subprocess.Popen(  # noqa: S603 - the command is the user's, by design
        list(spec.argv),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=spec.cwd,
        env=dict(spec.env),
        start_new_session=True,
        close_fds=True,
    )
    return PipeHandle(popen)


# -- records --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SpawnRequest:
    command: str
    scope: Scope
    session_id: str | None
    env_key: str
    cwd: str
    env: Mapping[str, str]
    snapshot: str | None = None
    use_pty: bool = False
    notify_on_exit: bool = False
    watch_patterns: Sequence[str] = ()
    heartbeat_s: int = 0
    persist_on_release: bool = False
    environment: Environment | None = None  # a non-local backend runs the job itself (P11-12)


@dataclass(eq=False)
class ProcessRecord:
    """One background job (see the module notes)."""

    id: str
    command: str
    home: str
    session_id: str | None
    env_key: str
    cwd: str
    scope: Scope = field(repr=False)
    started_at: datetime
    started_mono: float
    pid: int | None = None
    pty: bool = False
    pty_note: str = ""
    persist_on_release: bool = False
    notify_on_exit: bool = False
    watch: WatchState | None = None
    heartbeat: HeartbeatState | None = None
    exited: bool = False
    exit_code: int | None = None
    reason: Reason = "exited"
    source: str = ""
    ended_mono: float = 0.0
    buffer: str = ""
    total_chars: int = 0
    consumed: bool = False
    last_used: float = 0.0
    handle: JobHandle | None = field(default=None, repr=False)
    backend: str = "local"
    detached: bool = False  # adopted after a restart: no output history (P11-14)
    log_path: str = ""
    pid_start: int | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    kill_source: str = field(default="", repr=False)
    decoder: codecs.IncrementalDecoder = field(
        default_factory=lambda: codecs.getincrementaldecoder("utf-8")(errors="replace"), repr=False
    )
    line_rest: str = field(default="", repr=False)
    pidfd: int | None = field(default=None, repr=False)
    poller: asyncio.Future[None] | None = field(default=None, repr=False)
    handoff_note: str = ""  # why a subagent handed this job to its parent (P10-06)
    memory_cap: int = 0  # bytes of the job's systemd scope cap; 0 = not scoped (P24-43)

    @property
    def status(self) -> Literal["running", "exited"]:
        return "exited" if self.exited else "running"

    def tail(self, chars: int) -> str:
        """The last ``chars`` characters of output, escape codes removed."""
        return strip_ansi(self.buffer[-chars:]) if chars > 0 else ""

    def append(self, text: str) -> None:
        self.buffer += text
        self.total_chars += len(text)
        if len(self.buffer) > MAX_BUFFER_CHARS:
            self.buffer = self.buffer[-MAX_BUFFER_CHARS:]


def _config(scope: Scope, key: str) -> Any:
    value = scope.config.get(key) if scope.config is not None else None
    return default_for(key) if value is None else value


def _bash(env: Mapping[str, str]) -> str:
    found = locate.which("bash", path=env.get("PATH") or None)
    if found:
        return found
    if os.access("/bin/bash", os.X_OK):
        return "/bin/bash"
    raise BackendError("bash was not found; background processes need it")


def job_argv(command: str, env: Mapping[str, str], snapshot: str | None) -> tuple[str, ...]:
    """bash running ``command``: after the session's shell snapshot when there is one (the
    variables and functions earlier commands set), else as a login shell."""
    script = rewrite_compound_background(command)
    bash = _bash(env)
    if snapshot and os.path.isfile(snapshot):
        prelude = f"source {shlex.quote(snapshot)} >/dev/null 2>&1 || true\n"
        return (bash, "-c", prelude + script)
    return (bash, "-l", "-c", script)


# -- the registry ---------------------------------------------------------------------------------


@dataclass
class HandoffGrant:
    """A subagent session may hand up to ``limit`` of its jobs to ``parent_session`` (P10-06)."""

    home: str
    child_session: str
    parent_session: str
    limit: int = MAX_HANDOFFS
    used: int = 0


class ProcessRegistry:
    """Every background job of this process (see the module notes).

    Ownership is the record's ``session_id``. ``transfer_ownership`` moves a running job to
    another session under the registry's owner lock: a subagent hands a job to its parent
    (P10-06) only while its ``HandoffGrant`` is registered; the completion then goes to the new
    owner (notify on when that session can receive messages).
    """

    def __init__(
        self,
        deliveries: Deliveries | None = None,
        *,
        clock: Clock | None = None,
        launcher: Launcher = launch_local,
        receipts: Callable[[ProcessRecord], None] | None | Literal["default"] = "default",
        max_tracked: int = MAX_TRACKED,
        watch_exits: bool = True,
        checkpoints: bool = True,
    ) -> None:
        self.deliveries = deliveries if deliveries is not None else Deliveries()
        self.clock: Clock = clock if clock is not None else SystemClock()
        self.launcher = launcher
        self.receipts: Callable[[ProcessRecord], None] | None = (
            self._write_receipt if receipts == "default" else receipts
        )
        self.max_tracked = max_tracked
        # False for handles that report their own exit through ``notice_exit`` (tests, adapters).
        self.watch_exits = watch_exits
        self.checkpoints = checkpoints
        self.breaker = GlobalBreaker()
        self._records: dict[str, ProcessRecord] = {}
        self._ticker: asyncio.Future[None] | None = None
        self._recovered: set[str] = set()
        self._pollers: dict[int, remote_processes.RemotePoller] = {}
        self._owner_lock = threading.Lock()
        self._grants: dict[tuple[str, str], HandoffGrant] = {}

    def _write_receipt(self, record: ProcessRecord) -> None:
        process_receipts.write_receipt(record, now=self.clock.now())

    # -- starting --------------------------------------------------------------------------------

    def _new_id(self) -> str:
        while True:
            ident = ID_PREFIX + secrets.token_hex(6)
            if ident not in self._records:
                return ident

    async def spawn(self, request: SpawnRequest) -> ProcessRecord:
        """Start a job. Raises ``ProcessLimit`` when ``max_tracked`` jobs are running; a start
        that fails gives a ``failed_start`` record (check ``record.exited``)."""
        self.recover(request.scope)
        self._prune(make_room=True)
        running = sum(1 for r in self._records.values() if not r.exited)
        if running >= self.max_tracked:
            raise ProcessLimit(
                f"{running} background processes are already running (the limit is "
                f"{self.max_tracked})",
                hint="stop ones you no longer need with process_manage kill",
            )
        now = self.clock.monotonic()
        record = ProcessRecord(
            id=self._new_id(),
            command=request.command,
            home=str(request.scope.home),
            session_id=request.session_id,
            env_key=request.env_key,
            cwd=request.cwd,
            scope=request.scope,
            started_at=self.clock.now(),
            started_mono=now,
            persist_on_release=request.persist_on_release,
            notify_on_exit=request.notify_on_exit,
            last_used=now,
        )
        self._records[record.id] = record  # owner known before any output is read
        environment = request.environment
        if environment is not None and not environment.flags.host_local:
            await self._spawn_remote(record, request, environment)
            return record
        env = dict(request.env)
        env["PYTHONUNBUFFERED"] = "1"  # so poll shows progress, not one burst at the end
        try:
            argv = job_argv(request.command, env, request.snapshot)
            scoped = memory_scope.wrap(request.scope, argv, env, job_id=record.id)
            record.memory_cap = scoped.cap
            spec = LaunchSpec(scoped.argv, request.cwd, scoped.env, request.use_pty)
            handle = self._launch(record, spec)
        except Exception as exc:
            log.warning("background process failed to start: %s", type(exc).__name__)
            self._fail_start(record, f"could not start the process: {exc}")
            return record
        if handle.pid is None:
            self._fail_start(record, "the process did not report a process id")
            with contextlib.suppress(Exception):
                handle.release()
            return record
        record.handle, record.pid, record.pty = handle, handle.pid, handle.pty
        local = getattr(handle, "local", None)
        if isinstance(local, LocalProcess):
            record.pid_start = local.leader_start
        self._arm(record, request, now)
        self._attach(record, handle)
        self._save_checkpoint(record.scope)
        return record

    def _arm(self, record: ProcessRecord, request: SpawnRequest, now: float) -> None:
        if request.watch_patterns:
            record.watch = WatchState(tuple(p for p in request.watch_patterns if p))
        if request.heartbeat_s > 0:
            every = max(int(request.heartbeat_s), HEARTBEAT_MIN_S)
            record.heartbeat = HeartbeatState(every, now)
            self._ensure_ticker()

    async def _spawn_remote(
        self, record: ProcessRecord, request: SpawnRequest, environment: Environment
    ) -> None:
        """Start the job inside ``environment`` and hand it to that environment's poller."""
        record.backend = environment.name
        snapshot = str(getattr(environment, "snapshot_path", "") or "")
        temp_dir = posixpath.dirname(snapshot) if snapshot else remote_processes.DEFAULT_TEMP_DIR
        started = await remote_processes.start(
            environment, record.id, request.command, cwd=request.cwd, temp_dir=temp_dir
        )
        if isinstance(started, str):
            log.warning("background process failed to start on %s", environment.name)
            self._fail_start(record, started)
            return
        job = remote_processes.RemoteJob(
            environment,
            started.pid,
            started.files,
            generation=remote_processes.generation(environment),
        )
        record.handle, record.pid, record.log_path = job, started.pid, started.files.log
        if request.use_pty:
            record.pty_note = remote_processes.pty_note(environment.name)
        self._arm(record, request, self.clock.monotonic())
        self._poller_for(environment).add(record, job)
        self._save_checkpoint(record.scope)

    def _poller_for(self, environment: Environment) -> remote_processes.RemotePoller:
        key = id(environment)
        poller = self._pollers.get(key)
        if poller is None:
            poller = remote_processes.RemotePoller(
                environment, self, clock=self.clock, on_idle=self._poller_idle
            )
            self._pollers[key] = poller
        return poller

    def _poller_idle(self, poller: remote_processes.RemotePoller) -> None:
        if poller.jobs == 0 and self._pollers.get(id(poller.env)) is poller:
            del self._pollers[id(poller.env)]

    def _launch(self, record: ProcessRecord, spec: LaunchSpec) -> JobHandle:
        if spec.use_pty:
            try:
                return self.launcher(spec)
            except ImportError:
                record.pty_note = PTY_MISSING_NOTE
                spec = LaunchSpec(spec.argv, spec.cwd, spec.env, use_pty=False)
        return self.launcher(spec)

    def _fail_start(self, record: ProcessRecord, why: str) -> None:
        record.append(why)
        record.exited, record.exit_code, record.reason = True, -1, "failed_start"
        record.source = "failed_start"
        record.ended_mono = self.clock.monotonic()
        record.done.set()

    def _attach(self, record: ProcessRecord, handle: JobHandle) -> None:
        loop = asyncio.get_running_loop()
        fd = handle.fd
        if fd is not None:
            loop.add_reader(fd, self._on_readable, record)
        pid = handle.pid
        if pid is None or not self.watch_exits:
            return
        try:
            pidfd = os.pidfd_open(pid)
        except (AttributeError, OSError):
            record.poller = asyncio.ensure_future(self._poll_exit(record))
            return
        record.pidfd = pidfd
        loop.add_reader(pidfd, self._on_exit_signal, record)

    # -- output ----------------------------------------------------------------------------------

    def _on_readable(self, record: ProcessRecord) -> None:
        if self._read_available(record, READS_PER_WAKE * READ_CHUNK):
            self._stop_reading(record)

    def _read_available(self, record: ProcessRecord, budget: int) -> bool:
        """Read what is waiting (up to ``budget`` bytes); True at end of output."""
        handle = record.handle
        fd = handle.fd if handle is not None else None
        if fd is None:
            return True
        taken = 0
        while taken < budget:
            try:
                chunk = os.read(fd, READ_CHUNK)
            except BlockingIOError:
                return False
            except OSError:
                return True  # EIO: the terminal's other side is gone
            if not chunk:
                return True
            taken += len(chunk)
            self.ingest(record, record.decoder.decode(chunk))
        return False

    def _stop_reading(self, record: ProcessRecord) -> None:
        handle = record.handle
        fd = handle.fd if handle is not None else None
        if fd is not None:
            with contextlib.suppress(Exception):
                asyncio.get_running_loop().remove_reader(fd)

    def ingest(self, record: ProcessRecord, text: str) -> None:
        """Take output (readers call this; so do handles without a descriptor)."""
        if not text or record.exited:
            return
        record.append(text)
        watch = record.watch
        if watch is None or watch.off:
            return
        pending = record.line_rest + text
        *lines, record.line_rest = pending.split("\n")
        if len(record.line_rest) > MAX_BUFFER_CHARS:
            record.line_rest = record.line_rest[-MAX_BUFFER_CHARS:]
        if lines:
            self._check_watch(record, watch, [strip_ansi(line) for line in lines])

    def _check_watch(self, record: ProcessRecord, watch: WatchState, lines: list[str]) -> None:
        now = self.clock.monotonic()
        verdict = watch.offer(lines, now)
        if verdict.deliver:
            gate = self.breaker.admit(now)
            if gate.resumed > 0:
                self._send(
                    record, "watch_resumed", breaker_text(tripped=False, dropped=gate.resumed)
                )
            if gate.admit:
                shown = "\n".join(verdict.lines[:WATCH_MAX_LINES])
                if len(shown) > WATCH_MAX_CHARS:
                    shown = shown[:WATCH_MAX_CHARS] + "\n[...]"
                shown = self._redact(record, shown)
                self._send(
                    record,
                    "watch_match",
                    watch_text(
                        record.id, self._command(record), verdict.pattern, shown, verdict.suppressed
                    ),
                    output=shown,
                    pattern=verdict.pattern,
                    suppressed=verdict.suppressed,
                )
            elif gate.tripped:
                self._send(record, "watch_paused", breaker_text(tripped=True))
        if verdict.switched_off:
            record.notify_on_exit = True  # one message at the exit instead
            self._send(
                record,
                "watch_off",
                watch_off_text(record.id, verdict.switched_off, verdict.suppressed),
                suppressed=verdict.suppressed,
            )

    # -- exit -----------------------------------------------------------------------------------

    def _on_exit_signal(self, record: ProcessRecord) -> None:
        pidfd = record.pidfd
        if pidfd is not None:
            with contextlib.suppress(Exception):
                asyncio.get_running_loop().remove_reader(pidfd)
            with contextlib.suppress(OSError):
                os.close(pidfd)
            record.pidfd = None
        handle = record.handle
        if handle is not None and handle.poll() is None:
            record.poller = asyncio.ensure_future(self._poll_exit(record))
            return
        self.notice_exit(record)

    async def _poll_exit(self, record: ProcessRecord) -> None:
        handle = record.handle
        while handle is not None and not record.exited and handle.poll() is None:
            await self.clock.sleep(EXIT_POLL_S)
        self.notice_exit(record)

    def notice_exit(
        self,
        record: ProcessRecord,
        code: int | None = None,
        *,
        reason: str | None = None,
        source: str = "",
    ) -> None:
        """The job's leader has exited: collect the rest of its output, settle its state, keep
        its receipt, announce it. ``reason`` overrides the outcome (``lost``). Safe to call more
        than once."""
        if record.exited:
            return
        handle = record.handle
        if handle is not None:
            if code is None and reason is None:
                code = handle.poll()
            if getattr(handle, "unknown_exit", False):
                code = None  # an adopted job is not our child: its status is not ours to read
            if not self._read_available(record, FINAL_DRAIN_MAX):
                log.debug("%s: output still open after exit (a child kept it)", record.id)
            self._stop_reading(record)
            self._close_exit_watch(record)
        tail = record.decoder.decode(b"", final=True)
        if tail:
            record.append(tail)
        if handle is not None:
            with contextlib.suppress(Exception):
                handle.release()
        record.exited = True
        record.exit_code = code
        record.ended_mono = self.clock.monotonic()
        if record.kill_source:
            record.reason, record.source = "killed", record.kill_source
        elif memory_scope.is_memory_kill(code, scoped=record.memory_cap > 0, killed_by_us=False):
            record.reason, record.source = "killed", memory_scope.MEMORY_CAP_SOURCE
        if reason == "lost":
            record.reason, record.source = "lost", source or "backend_lost"
        record.heartbeat = None
        record.done.set()
        if self.receipts is not None:  # the retained receipt exists before any event (P11-13)
            try:
                self.receipts(record)
            except Exception:
                log.warning("writing the receipt of %s failed", record.id, exc_info=True)
        self._save_checkpoint(record.scope)
        if record.notify_on_exit and not record.consumed:
            self._announce_exit(record)

    def _close_exit_watch(self, record: ProcessRecord) -> None:
        pidfd = record.pidfd
        if pidfd is not None:
            with contextlib.suppress(Exception):
                asyncio.get_running_loop().remove_reader(pidfd)
            with contextlib.suppress(OSError):
                os.close(pidfd)
            record.pidfd = None
        poller = record.poller
        if poller is not None and not poller.done() and poller is not _current_task():
            poller.cancel()

    def _announce_exit(self, record: ProcessRecord) -> None:
        output = self._redact(record, record.tail(COMPLETION_TAIL_CHARS))
        cut = max(0, record.total_chars - COMPLETION_TAIL_CHARS)
        self._send(
            record,
            "completion",
            completion_text(
                record.id,
                self._command(record),
                output,
                exit_code=record.exit_code,
                reason=record.reason,
                source=record.source,
                cut=cut,
            ),
            output=output,
            exit_code=record.exit_code,
            reason=record.reason,
        )

    # -- events ---------------------------------------------------------------------------------

    def _redact(self, record: ProcessRecord, text: str) -> str:
        return redact_output(text, command=record.command, scope=record.scope)

    def _command(self, record: ProcessRecord) -> str:
        return redact_output(record.command, scope=record.scope)

    def _send(
        self,
        record: ProcessRecord,
        kind: Any,
        text: str,
        *,
        output: str = "",
        exit_code: int | None = None,
        reason: str = "",
        pattern: str = "",
        suppressed: int = 0,
        seq: int = 0,
    ) -> None:
        event = ProcessEvent(
            kind=kind,
            process_id=record.id,
            home=record.home,
            session_id=record.session_id,
            command=self._command(record),
            text=text,
            output=output,
            exit_code=exit_code,
            reason=reason,
            pattern=pattern,
            suppressed=suppressed,
            seq=seq,
        )
        self.deliveries.deliver(event)

    # -- heartbeats -----------------------------------------------------------------------------

    def _ensure_ticker(self) -> None:
        if self._ticker is None or self._ticker.done():
            self._ticker = asyncio.ensure_future(self._tick_forever())

    async def _tick_forever(self) -> None:
        while any(r.heartbeat is not None and not r.exited for r in self._records.values()):
            await self.clock.sleep(HEARTBEAT_TICK_S)
            self.heartbeat_round()

    def heartbeat_round(self) -> int:
        """One ticker round: send the heartbeats that are due; returns how many were sent."""
        now = self.clock.monotonic()
        sent = 0
        for record in list(self._records.values()):
            beat = record.heartbeat
            if beat is None or record.exited or not beat.due(now):
                continue
            taken = beat.take(now, record.buffer, record.total_chars)
            if taken is None:
                continue  # nothing new: no message (each one costs a turn)
            output, omitted = taken
            shown = self._redact(record, strip_ansi(output))
            self._send(
                record,
                "heartbeat",
                heartbeat_text(
                    record.id,
                    self._command(record),
                    shown,
                    seq=beat.seq,
                    elapsed_s=now - record.started_mono,
                    every_s=beat.every_s,
                    omitted=omitted,
                ),
                output=shown,
                seq=beat.seq,
            )
            sent += 1
        return sent

    # -- lookups --------------------------------------------------------------------------------

    def _prune(self, *, make_room: bool = False) -> None:
        """Forget finished jobs past their keep time; with ``make_room``, also the least recently
        used finished ones until a new job fits under ``max_tracked``."""
        now = self.clock.monotonic()
        for ident, record in list(self._records.items()):
            if record.exited and now - record.ended_mono > FINISHED_TTL_S:
                del self._records[ident]
        while make_room and len(self._records) >= self.max_tracked:
            finished = [r for r in self._records.values() if r.exited]
            if not finished:
                break
            oldest = min(finished, key=lambda r: r.last_used)
            del self._records[oldest.id]

    def get(self, ident: object, home: str) -> ProcessRecord | None:
        """The job ``ident`` names (full id or unique prefix) in profile ``home``."""
        if not isinstance(ident, str) or not ident.strip():
            return None
        self._prune()
        query = ident.strip()
        found = self._records.get(query)
        if found is None:
            if not query.startswith(ID_PREFIX):
                query = ID_PREFIX + query
            if len(query) - len(ID_PREFIX) < MIN_PREFIX_HEX:
                return None
            matches = [
                r for k, r in self._records.items() if k.startswith(query) and r.home == home
            ]
            found = matches[0] if len(matches) == 1 else None
        if found is None or found.home != str(home):
            return None
        found.last_used = self.clock.monotonic()
        return found

    def list(self, home: str, session_id: str | None) -> list[ProcessRecord]:
        self._prune()
        return sorted(
            (
                r
                for r in self._records.values()
                if r.home == str(home) and r.session_id == session_id
            ),
            key=lambda r: r.started_mono,
        )

    def consumed(self, process_id: str) -> bool:
        record = self._records.get(process_id)
        return record is not None and record.consumed

    def alive_for_env_key(self, key: object) -> bool:
        """Does an environment key still have a running job? (The idle reaper asks.)"""
        text = str(key)
        return any(not r.exited and r.env_key == text for r in self._records.values())

    def running(self) -> list[ProcessRecord]:
        return [r for r in self._records.values() if not r.exited]

    # -- acting ---------------------------------------------------------------------------------

    async def kill(self, record: ProcessRecord, *, source: str = "process_manage kill") -> None:
        """Stop the job with the ordered kill; its completion counts as read by the caller."""
        record.consumed = True
        if record.exited:
            return
        handle = record.handle
        record.kill_source = source
        if handle is None:
            self.notice_exit(record)
            return
        grace = float(_config(record.scope, "terminal.daemon_term_grace_s"))
        await handle.stop(self.clock, grace)
        self.notice_exit(record)

    async def wait(
        self, record: ProcessRecord, timeout: float, cancel: CancelLike | None = None
    ) -> WaitOutcome:
        """Wait up to ``timeout`` seconds for the job to end (checked in 1 s slices)."""
        deadline = self.clock.monotonic() + max(0.0, timeout)
        while True:
            if record.exited:
                return "exited"
            if cancel is not None and cancel.is_set():
                return "interrupted"
            left = deadline - self.clock.monotonic()
            if left <= 0:
                return "timeout"
            done = asyncio.ensure_future(record.done.wait())
            timer = asyncio.ensure_future(self.clock.sleep(min(WAIT_SLICE_S, left)))
            try:
                await asyncio.wait({done, timer}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                done.cancel()
                timer.cancel()

    async def write(self, record: ProcessRecord, data: str) -> str | None:
        if record.exited or record.handle is None:
            return "the process has already ended"
        return await record.handle.write(data)

    def close_input(self, record: ProcessRecord) -> str | None:
        if record.exited or record.handle is None:
            return "the process has already ended"
        return record.handle.close_input()

    # -- ownership (P10-06) -----------------------------------------------------------------------

    def grant_handoff(
        self, home: str, child_session: str, parent_session: str, *, limit: int = MAX_HANDOFFS
    ) -> HandoffGrant:
        """Let ``child_session`` hand jobs to ``parent_session`` (a subagent while it runs)."""
        grant = HandoffGrant(str(home), child_session, parent_session, limit)
        with self._owner_lock:
            self._grants[(grant.home, child_session)] = grant
        return grant

    def revoke_handoff(self, home: str, child_session: str) -> None:
        with self._owner_lock:
            self._grants.pop((str(home), child_session), None)

    def handoff_grant(self, home: str, session_id: str | None) -> HandoffGrant | None:
        if not session_id:
            return None
        with self._owner_lock:
            return self._grants.get((str(home), session_id))

    def transfer_ownership(
        self, record: ProcessRecord, *, from_session: str, to_session: str, note: str = ""
    ) -> bool:
        """Move a running job from ``from_session`` to ``to_session``; False (nothing changed)
        when it has ended or is not ``from_session``'s."""
        with self._owner_lock:
            if record.exited or record.session_id != from_session:
                return False
            record.session_id = to_session
            record.handoff_note = note
            record.notify_on_exit = self.deliveries.deliverable(record.home, to_session)
        self._save_checkpoint(record.scope)
        return True

    async def release(self, home: str, session_id: str | None) -> int:
        """Session end or ``/new``: stop the session's jobs except ``persist_on_release`` ones."""
        targets = [
            r
            for r in self.running()
            if r.home == str(home) and r.session_id == session_id and not r.persist_on_release
        ]
        for record in targets:
            await self.kill(record, source="session ended")
        return len(targets)

    async def shutdown(self) -> None:
        """Process exit: stop every job at once (no grace, no announcements)."""
        if self._ticker is not None:
            self._ticker.cancel()
            self._ticker = None
        for poller in list(self._pollers.values()):
            poller.cancel()
        self._pollers.clear()
        for record in self.running():
            record.consumed = True
            record.kill_source = "shutdown"
            handle = record.handle
            if handle is not None:
                with contextlib.suppress(Exception):
                    handle.force_stop()
            self.notice_exit(record)

    # -- crash checkpoint (P11-14) ----------------------------------------------------------------

    def _entry(self, record: ProcessRecord) -> process_checkpoint.Entry | None:
        handle = record.handle
        if not isinstance(
            handle,
            PipeHandle | PtyHandle | remote_processes.RemoteJob | process_checkpoint.DetachedJob,
        ):
            return None  # test and adapter handles never reach the file
        return process_checkpoint.Entry(
            id=record.id,
            pid=record.pid,
            pid_start_time=record.pid_start,
            backend=record.backend,
            env_key=record.env_key,
            log_path=record.log_path,
            owner_session=record.session_id,
            notify_on_exit=record.notify_on_exit,
            persist_on_release=record.persist_on_release,
            command=redact_output(record.command, force=True, scope=record.scope),
            cwd=record.cwd,
            started_at=record.started_at.isoformat(timespec="seconds"),
        )

    def _save_checkpoint(self, scope: Scope) -> None:
        if not self.checkpoints:
            return
        home = str(scope.home)
        if home not in self._recovered:
            return  # never overwrite a profile's checkpoint before it was read
        entries = [
            entry
            for record in self._records.values()
            if record.home == home and not record.exited
            if (entry := self._entry(record)) is not None
        ]
        process_checkpoint.save(scope.home, entries)

    def recover(self, scope: Scope) -> process_checkpoint.Recovery | None:
        """Read ``scope``'s crash checkpoint once per registry (module notes): adopt the jobs a
        crashed predecessor left running as ``detached`` records, mark the rest ``lost``."""
        home = str(scope.home)
        if not self.checkpoints or home in self._recovered:
            return None
        self._recovered.add(home)
        found = process_checkpoint.classify(process_checkpoint.read_entries(scope.home))
        for entry in found.adopt:
            if entry.id in self._records or entry.pid is None or entry.pid_start_time is None:
                continue
            record = self._recovered_record(scope, entry)
            handle = process_checkpoint.DetachedJob(entry.pid, entry.pid_start_time)
            record.handle, record.pid, record.pid_start = handle, entry.pid, entry.pid_start_time
            record.detached = True
            self._records[record.id] = record
            with contextlib.suppress(RuntimeError):  # no loop: noticed on the next poll
                self._attach(record, handle)
            log.info("adopted background process %s (pid %s) after a restart", entry.id, entry.pid)
        for entry in found.lost:
            if entry.id in self._records:
                continue
            record = self._recovered_record(scope, entry)
            record.append(
                "[the process was not running when Indus restarted, or its pid was reused]"
            )
            record.exited, record.exit_code = True, None
            record.reason, record.source = "lost", "restart"
            record.consumed = True  # nothing to announce: its end was never observed
            record.ended_mono = self.clock.monotonic()
            record.done.set()
            self._records[record.id] = record
        if found.adopt or found.lost:
            self._save_checkpoint(scope)
        return found

    def _recovered_record(self, scope: Scope, entry: process_checkpoint.Entry) -> ProcessRecord:
        now = self.clock.monotonic()
        try:
            started = datetime.fromisoformat(entry.started_at)
        except ValueError:
            started = self.clock.now()
        return ProcessRecord(
            id=entry.id,
            command=entry.command,
            home=str(scope.home),
            session_id=entry.owner_session,
            env_key=entry.env_key,
            cwd=entry.cwd,
            scope=scope,
            started_at=started,
            started_mono=now - max(0.0, (self.clock.now() - started).total_seconds()),
            persist_on_release=entry.persist_on_release,
            notify_on_exit=entry.notify_on_exit,
            last_used=now,
            backend=entry.backend,
            log_path=entry.log_path,
        )


def _current_task() -> asyncio.Task[Any] | None:
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


def uptime_s(record: ProcessRecord, clock: Clock) -> int:
    end = record.ended_mono if record.exited else clock.monotonic()
    return max(0, int(end - record.started_mono))
