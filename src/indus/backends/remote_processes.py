# pyright: strict
"""Background jobs inside non-local backends: Docker, SSH, cloud sandboxes (P11-12).

Starting is one short ``env.execute`` (``SPAWN_TIMEOUT_S``): make the backend's temp dir, start
``( nohup bash -lc <cmd> > log 2>&1; echo $? > exit )`` in the background with job control on
(``set -m``: the job leads its own process group, so a kill reaches its whole tree) and its own
stdio detached (the call returns at once), write its PID to the pid file and print it. The files
are ``indus_bg_<proc>.{log,pid,exit}`` in the temp dir. No PID in the answer → ``failed_start``.

Watching: one ``RemotePoller`` per environment wakes every ``POLL_EVERY_S`` while any of its jobs
runs, and per job runs one command that prints the log size, the log bytes added since the last
read (base64, so a multibyte character cut at the read boundary survives), whether the process
group is alive, and the exit code once the exit file exists. Every read uses unbounded capture
(R02.2). New text goes to the registry's ``ingest`` (watch patterns, heartbeats, the completion
tail) and the exit to ``notice_exit`` (notify-on-exit where deliverable, R02.17). A log that
shrank was rotated: reading restarts at 0.

Stopping: SIGTERM to the job's process group, the P05 grace (``terminal.daemon_term_grace_s``),
then SIGKILL to what is left (R02.12); the rest of the log is read before the exit is settled.

Lost (R02.19): the environment was evicted or cleaned up, the backend could not be reached, or the
sandbox the job lived in was replaced → the job ends with reason ``lost``. Replacement is read
from the environment's ``recreations`` counter (``generation``), compared with the value when the
job started, and from a poll result's ``environment_recreated``. That flag is one-shot: a poll
only peeks at it and puts it back (``mark_recreated``), so the next foreground command still
carries the warning. A job of a replaced sandbox is never signalled (its pid now names something
else). Other read failures are retried; ``MAX_POLL_FAILURES`` in a row also mean lost. There is no
pseudo-terminal and no stdin for these jobs.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import codecs
import contextlib
import logging
import posixpath
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from indus.backends.contract import (
    BackendError,
    Environment,
    EnvironmentConnectionError,
    ExecResult,
)
from indus.foundation.clock import Clock

if TYPE_CHECKING:
    from indus.backends.processes import ProcessRecord

__all__ = [
    "FILE_PREFIX",
    "KILL_POLL_S",
    "MAX_POLL_FAILURES",
    "POLL_EVERY_S",
    "SPAWN_TIMEOUT_S",
    "JobFiles",
    "PollReading",
    "RemoteJob",
    "RemotePoller",
    "generation",
    "job_files",
    "kill_script",
    "parse_pid",
    "parse_poll",
    "poll_script",
    "pty_note",
    "spawn_script",
    "start",
]

log = logging.getLogger(__name__)

SPAWN_TIMEOUT_S = 10.0
POLL_EVERY_S = 2.0
POLL_TIMEOUT_S = 60.0
KILL_TIMEOUT_S = 10.0
KILL_POLL_S = 0.5
MAX_POLL_FAILURES = 5
GONE_ROUNDS = 2  # dead without an exit file this many polls in a row → ended, code unknown
FILE_PREFIX = "indus_bg_"
DEFAULT_TEMP_DIR = "/tmp"

_PID_LINE = re.compile(r"^__INDUS_PID__ (\d+)\s*$", re.M)


def pty_note(backend: str) -> str:
    return (
        f"A pseudo-terminal is not available on the {backend} backend; this process runs "
        "without one and cannot take input."
    )


@dataclass(frozen=True, slots=True)
class JobFiles:
    log: str
    pid: str
    exit: str


def job_files(temp_dir: str, proc_id: str) -> JobFiles:
    base = posixpath.join(temp_dir or DEFAULT_TEMP_DIR, FILE_PREFIX + proc_id)
    return JobFiles(base + ".log", base + ".pid", base + ".exit")


def spawn_script(command: str, files: JobFiles, temp_dir: str) -> str:
    """The text ``env.execute`` runs to start ``command`` in the background (module notes)."""
    q = shlex.quote
    return "\n".join(
        [
            f"mkdir -p -- {q(temp_dir)} || exit 1",
            f"rm -f -- {q(files.log)} {q(files.pid)} {q(files.exit)}",
            "set -m",
            f"( nohup bash -lc {q(command)} > {q(files.log)} 2>&1; echo $? > {q(files.exit)} ) "
            "</dev/null >/dev/null 2>&1 &",
            f"echo $! > {q(files.pid)}",
            f"printf '__INDUS_PID__ %s\\n' \"$(cat {q(files.pid)})\"",
        ]
    )


def parse_pid(output: str) -> int | None:
    found = _PID_LINE.findall(output)
    return int(found[-1]) if found else None


def _token(files: JobFiles) -> str:
    return "__INDUS_POLL_" + posixpath.basename(files.log).removesuffix(".log") + "__"


def poll_script(files: JobFiles, pid: int, offset: int) -> str:
    """One read of a job: header ``<token> <offset> <size>``, the base64 delta on one line, the
    alive flag, then the exit code when the exit file exists."""
    q = shlex.quote
    tok = _token(files)
    return "\n".join(
        [
            f"__f={q(files.log)}; __o={int(offset)}",
            '__s=$(wc -c < "$__f" 2>/dev/null | tr -dc 0-9); __s=${__s:-0}',
            'if [ "$__s" -lt "$__o" ]; then __o=0; fi',
            f'printf \'{tok} %s %s\\n\' "$__o" "$__s"',
            'if [ "$__s" -gt "$__o" ]; then',
            '  tail -c +$((__o + 1)) "$__f" 2>/dev/null | head -c $((__s - __o)) | base64 | '
            "tr -d '\\n\\r '",
            "fi",
            "printf '\\n'",
            f"if kill -0 -- -{int(pid)} 2>/dev/null || kill -0 {int(pid)} 2>/dev/null; "
            f"then echo '{tok} alive 1'; else echo '{tok} alive 0'; fi",
            f"if [ -f {q(files.exit)} ]; then",
            f"  printf '{tok} exit %s\\n' \"$(cat {q(files.exit)})\"",
            "fi",
        ]
    )


@dataclass(frozen=True, slots=True)
class PollReading:
    offset: int  # where this delta starts (0 after a rotation)
    size: int  # log size the delta reaches
    data: bytes
    alive: bool
    exit_code: int | None
    exited: bool  # the exit file exists


def parse_poll(output: str, files: JobFiles) -> PollReading | None:
    tok = _token(files)
    lines = output.split("\n")
    for index, line in enumerate(lines):
        parts = line.strip().split()
        if len(parts) != 3 or parts[0] != tok or not parts[1].isdigit() or not parts[2].isdigit():
            continue
        payload = lines[index + 1].strip() if index + 1 < len(lines) else ""
        try:
            data = base64.b64decode(payload, validate=True) if payload else b""
        except (binascii.Error, ValueError):
            return None
        alive = False
        exited = False
        code: int | None = None
        for rest in lines[index + 2 :]:
            words = rest.strip().split()
            if len(words) >= 3 and words[0] == tok and words[1] == "alive":
                alive = words[2] == "1"
            elif len(words) >= 2 and words[0] == tok and words[1] == "exit":
                exited = True
                text = words[2] if len(words) >= 3 else ""
                code = int(text) if text.lstrip("-").isdigit() else None
        return PollReading(int(parts[1]), int(parts[2]), data, alive, code, exited)
    return None


def kill_script(pid: int, signal_name: str) -> str:
    """Signal the job's process group (and the leader, in case the group is already gone);
    prints whether anything of it is still alive."""
    p = int(pid)
    return (
        f"kill -{signal_name} -- -{p} 2>/dev/null; kill -{signal_name} {p} 2>/dev/null; "
        f"if kill -0 -- -{p} 2>/dev/null || kill -0 {p} 2>/dev/null; then echo alive; "
        "else echo gone; fi"
    )


def generation(env: Environment) -> int:
    """How many times the sandbox behind ``env`` was replaced (0 when it cannot tell)."""
    value: object = getattr(env, "recreations", 0)
    return value if isinstance(value, int) else 0


def _put_back_recreated(env: Environment) -> None:
    """A poll consumed the one-shot ``environment_recreated`` flag: arm it again."""
    mark: object = getattr(env, "mark_recreated", None)
    if callable(mark):
        mark()


@dataclass(frozen=True, slots=True)
class Started:
    pid: int
    files: JobFiles


async def start(
    env: Environment, proc_id: str, command: str, *, cwd: str | None, temp_dir: str
) -> Started | str:
    """Start ``command`` in ``env``; the started job, or why it did not start."""
    files = job_files(temp_dir, proc_id)
    try:
        result = await env.execute(
            spawn_script(command, files, temp_dir),
            timeout=SPAWN_TIMEOUT_S,
            cwd=cwd or None,
            bounded=False,
        )
    except BackendError as exc:
        return f"could not start the process on the {env.name} backend: {exc}"
    pid = parse_pid(result.output)
    if pid is None:
        shown = result.output.strip()
        return f"the {env.name} backend did not report a process id (exit {result.returncode})" + (
            f":\n{shown}" if shown else ""
        )
    return Started(pid, files)


# -- the job handle -------------------------------------------------------------------------------


class JobSink(Protocol):
    """What the poller needs from the process registry."""

    clock: Clock

    def ingest(self, record: ProcessRecord, text: str) -> None: ...

    def notice_exit(
        self,
        record: ProcessRecord,
        code: int | None = None,
        *,
        reason: str | None = None,
        source: str = "",
    ) -> None: ...


@dataclass(eq=False)
class RemoteJob:
    """A job inside a backend (meets ``processes.JobHandle``); output arrives through the
    poller, not a descriptor."""

    env: Environment
    remote_pid: int
    files: JobFiles
    poller: RemotePoller | None = None
    exit_code: int | None = None
    ended: bool = False
    offset: int = 0
    failures: int = 0
    gone_rounds: int = 0
    decoder: codecs.IncrementalDecoder = field(
        default_factory=lambda: codecs.getincrementaldecoder("utf-8")(errors="replace")
    )
    remote: bool = True
    pty: bool = False
    fd: int | None = None
    generation: int = 0  # the environment's ``recreations`` when the job started

    @property
    def replaced(self) -> bool:
        """The sandbox the job lived in was replaced: its pid names something else now."""
        return generation(self.env) != self.generation

    @property
    def pid(self) -> int | None:
        return self.remote_pid

    def poll(self) -> int | None:
        return self.exit_code

    async def write(self, data: str) -> str | None:
        return f"processes on the {self.env.name} backend do not take input"

    def close_input(self) -> str | None:
        return f"processes on the {self.env.name} backend have no input to close"

    async def _signal(self, signal_name: str) -> bool | None:
        """Send a signal; True while something of the job is alive, None when unreachable."""
        if self.replaced:
            return None
        try:
            result = await self.env.execute(
                kill_script(self.remote_pid, signal_name), timeout=KILL_TIMEOUT_S, bounded=False
            )
        except BackendError:
            return None
        return "alive" in result.output.split()

    async def stop(self, clock: Clock, grace: float) -> None:
        """TERM the group, wait out the grace while it lives, KILL the rest, read the tail."""
        alive = await self._signal("TERM")
        deadline = clock.monotonic() + max(0.0, grace)
        while alive and clock.monotonic() < deadline:
            await clock.sleep(min(KILL_POLL_S, max(0.0, deadline - clock.monotonic())))
            alive = await self._signal("0")
        if alive:
            await self._signal("KILL")
        if self.poller is not None:
            await self.poller.read_tail(self)

    def force_stop(self) -> None:
        """KILL the group without waiting (process exit); best effort."""
        if self.replaced:
            return
        with contextlib.suppress(RuntimeError):
            task = asyncio.get_running_loop().create_task(self._signal("KILL"))
            task.add_done_callback(_ignore_result)

    def release(self) -> None:
        if self.poller is not None:
            self.poller.drop(self)


def _ignore_result(task: asyncio.Task[bool | None]) -> None:
    if not task.cancelled():
        task.exception()


# -- the poller -----------------------------------------------------------------------------------


@dataclass
class _Watched:
    record: ProcessRecord
    job: RemoteJob


class RemotePoller:
    """Reads every running job of one environment every ``POLL_EVERY_S`` (module notes)."""

    def __init__(
        self,
        env: Environment,
        sink: JobSink,
        *,
        clock: Clock,
        on_idle: Callable[[RemotePoller], None] | None = None,
    ) -> None:
        self.env = env
        self.sink = sink
        self.clock = clock
        self.on_idle = on_idle
        self._jobs: dict[int, _Watched] = {}
        self._task: asyncio.Future[None] | None = None

    @property
    def jobs(self) -> int:
        return len(self._jobs)

    def add(self, record: ProcessRecord, job: RemoteJob) -> None:
        job.poller = self
        self._jobs[id(job)] = _Watched(record, job)
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._run())

    def drop(self, job: RemoteJob) -> None:
        self._jobs.pop(id(job), None)

    def cancel(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        try:
            while self._jobs:
                await self.clock.sleep(POLL_EVERY_S)
                await self.round()
        finally:
            if not self._jobs and self.on_idle is not None:
                self.on_idle(self)

    async def round(self) -> None:
        """Read every job once (tests call this directly)."""
        for watched in list(self._jobs.values()):
            if watched.record.exited:
                self.drop(watched.job)
                continue
            await self._read(watched)

    async def read_tail(self, job: RemoteJob) -> None:
        """Read what the log gained since the last poll (after a kill, before the exit)."""
        watched = self._jobs.get(id(job))
        if watched is None or watched.record.exited:
            return
        reading = await self._fetch(watched, quiet=True)
        if reading is not None:
            self._take(watched, reading)
        tail = job.decoder.decode(b"", final=True)
        if tail:
            self.sink.ingest(watched.record, tail)

    async def _fetch(self, watched: _Watched, *, quiet: bool = False) -> PollReading | None:
        job = watched.job
        try:
            result: ExecResult = await self.env.execute(
                poll_script(job.files, job.remote_pid, job.offset),
                timeout=POLL_TIMEOUT_S,
                bounded=False,
            )
        except EnvironmentConnectionError as exc:
            if not quiet:
                self._lose(watched, f"the {self.env.name} backend could not be reached ({exc})")
            return None
        except BackendError as exc:
            if "cleaned up" in str(exc):
                if not quiet:
                    self._lose(watched, f"the {self.env.name} environment was closed")
                return None
            if not quiet:
                self._failed(watched, str(exc))
            return None
        if result.environment_recreated:
            _put_back_recreated(self.env)  # peek only: the next command still gets the warning
        if result.environment_recreated or job.replaced:
            if not quiet:
                self._lose(
                    watched,
                    f"the {self.env.name} sandbox was replaced, so the process is gone",
                )
            return None
        reading = parse_poll(result.output, job.files)
        if reading is None and not quiet:
            self._failed(watched, f"unreadable poll answer (exit {result.returncode})")
        return reading

    def _take(self, watched: _Watched, reading: PollReading) -> None:
        job = watched.job
        if reading.offset < job.offset:
            log.info(
                "%s: the log was truncated; reading it again from the start", watched.record.id
            )
            job.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        job.offset = reading.size
        if reading.data:
            text = job.decoder.decode(reading.data)
            if text:
                self.sink.ingest(watched.record, text)

    async def _read(self, watched: _Watched) -> None:
        job = watched.job
        reading = await self._fetch(watched)
        if reading is None:
            return
        job.failures = 0
        self._take(watched, reading)
        if reading.exited or not reading.alive:
            job.gone_rounds = 0 if reading.exited else job.gone_rounds + 1
            if reading.exited or job.gone_rounds >= GONE_ROUNDS:
                self._finish(watched, reading.exit_code)
        else:
            job.gone_rounds = 0

    def _finish(self, watched: _Watched, code: int | None) -> None:
        job = watched.job
        tail = job.decoder.decode(b"", final=True)
        if tail:
            self.sink.ingest(watched.record, tail)
        job.exit_code = code
        job.ended = True
        self.drop(job)
        self.sink.notice_exit(watched.record, code)

    def _failed(self, watched: _Watched, why: str) -> None:
        job = watched.job
        job.failures += 1
        log.debug("%s: poll failed (%s), %d in a row", watched.record.id, why, job.failures)
        if job.failures >= MAX_POLL_FAILURES:
            self._lose(watched, f"{MAX_POLL_FAILURES} reads in a row failed ({why})")

    def _lose(self, watched: _Watched, why: str) -> None:
        job = watched.job
        job.ended = True
        self.drop(job)
        log.warning("%s: background process lost: %s", watched.record.id, why)
        self.sink.ingest(watched.record, f"\n[process lost: {why}]\n")
        self.sink.notice_exit(watched.record, None, reason="lost", source="backend_lost")
