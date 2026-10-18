"""Background jobs on non-local backends (P11-12): a simulated sandbox for the protocol, and the
SSH backend over the fake ssh CLI end to end."""

from __future__ import annotations

import asyncio
import base64
import re
import shutil
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from indus.backends.contract import (
    ActivityCallback,
    BackendError,
    BackendFlags,
    CancelSignal,
    EnvironmentConnectionError,
    ExecResult,
)
from indus.backends.process_notify import Deliveries
from indus.backends.processes import ProcessRecord, ProcessRegistry, SpawnRequest
from indus.backends.remote_processes import (
    MAX_POLL_FAILURES,
    RemoteJob,
    job_files,
    parse_poll,
    poll_script,
)
from indus.backends.ssh.environment import open_ssh
from indus.foundation.clock import SystemClock
from indus.foundation.scope import Scope
from tests.fakes.backend.ssh_cli import install, redirect_remote_tmp
from tests.fakes.clock import FakeClock
from tests.fakes.terminal import Inbox, terminal_scope
from tests.unit.backends._aio import aio
from tests.unit.backends.conftest import drive

_TOKEN = re.compile(r"(__INDUS_POLL_\S+__)")
_OFFSET = re.compile(r"__o=(\d+)")


class Sandbox:
    """A non-local ``Environment`` that answers the spawn, poll and kill scripts from state."""

    def __init__(self, name: str = "docker") -> None:
        self._name = name
        self.log = b""
        self.alive = True
        self.exit: int | None = None
        self.pid: int | None = 4242
        self.term_stops = True
        self.fail: BaseException | None = None
        self.recreated = False
        self.signals: list[str] = []
        self.calls: list[tuple[str, bool, float | None]] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def flags(self) -> BackendFlags:
        return BackendFlags(isolated=True, container_paths=True, host_local=False)

    @property
    def cwd(self) -> str:
        return "/workspace"

    def describe(self) -> str:
        return "simulated sandbox"

    async def cleanup(self) -> None:
        return None

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
        self.calls.append((command, bounded, timeout))
        if self.fail is not None:
            raise self.fail
        if "nohup bash -lc" in command:
            out = f"__INDUS_PID__ {self.pid}\n" if self.pid is not None else "sh: syntax error\n"
            return ExecResult(output=out, returncode=0 if self.pid else 2)
        token = _TOKEN.search(command)
        if token is not None:
            found = _OFFSET.search(command)
            offset = int(found.group(1)) if found else 0
            size = len(self.log)
            offset = 0 if size < offset else offset
            data = base64.b64encode(self.log[offset:size]).decode()
            tok = token.group(1)
            out = f"{tok} {offset} {size}\n{data}\n{tok} alive {int(self.alive)}\n"
            if self.exit is not None:
                out += f"{tok} exit {self.exit}\n"
            return ExecResult(output=out, returncode=0, environment_recreated=self.recreated)
        if command.startswith("kill -"):
            sig = command.split()[1].removeprefix("-")
            self.signals.append(sig)
            if sig == "KILL" or (sig == "TERM" and self.term_stops):
                self.alive = False
            return ExecResult(output="alive\n" if self.alive else "gone\n", returncode=0)
        raise AssertionError(f"unexpected command: {command}")


@pytest.fixture
def scope(tmp_path: Path) -> Scope:
    return terminal_scope(tmp_path, {"terminal": {"daemon_term_grace_s": 1.0}})


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 10, 18, tzinfo=UTC))


def request(scope: Scope, env: Any, command: str = "make test", **kw: Any) -> SpawnRequest:
    values: dict[str, Any] = {
        "command": command,
        "scope": scope,
        "session_id": "s1",
        "env_key": "k-remote",
        "cwd": "/workspace",
        "env": {},
        "environment": env,
    }
    values.update(kw)
    return SpawnRequest(**values)


def job(record: ProcessRecord) -> RemoteJob:
    assert isinstance(record.handle, RemoteJob)
    return record.handle


async def one_round(record: ProcessRecord) -> None:
    poller = job(record).poller
    assert poller is not None
    await poller.round()


@aio
async def test_spawn_reports_the_pid_and_runs_the_wrapper(scope: Scope, clock: FakeClock) -> None:
    box = Sandbox()
    registry = ProcessRegistry(clock=clock)
    record = await registry.spawn(request(scope, box, "npm run dev -- --port 3000"))
    try:
        assert not record.exited and record.pid == 4242 and record.backend == "docker"
        command, bounded, timeout = box.calls[0]
        assert "set -m" in command and "nohup bash -lc" in command
        assert "'npm run dev -- --port 3000'" in command
        assert f"indus_bg_{record.id}.log" in command and timeout == 10.0 and not bounded
        assert record.log_path.endswith(f"indus_bg_{record.id}.log")
        assert registry.alive_for_env_key("k-remote")
    finally:
        await registry.shutdown()


@aio
async def test_poll_feeds_output_and_completion_fires_once(scope: Scope, clock: FakeClock) -> None:
    box = Sandbox()
    deliveries = Deliveries()
    inbox = Inbox()
    deliveries.register(str(scope.home), "s1", inbox)
    registry = ProcessRegistry(deliveries, clock=clock)
    record = await registry.spawn(request(scope, box, notify_on_exit=True))
    try:
        box.log = b"compiling\n"
        await one_round(record)
        assert record.buffer == "compiling\n" and not record.exited
        box.log += b"done\n"
        box.alive, box.exit = False, 3
        await one_round(record)
        assert record.exited and record.exit_code == 3 and record.reason == "exited"
        assert record.buffer == "compiling\ndone\n"
        completions = [e for e in inbox.events if e.kind == "completion"]
        assert len(completions) == 1 and completions[0].exit_code == 3
        assert "done" in completions[0].output
        await one_round(record)
        assert len([e for e in inbox.events if e.kind == "completion"]) == 1
        polls = [c for c in box.calls if "__INDUS_POLL_" in c[0]]
        assert polls and all(not bounded for _, bounded, _ in polls)  # R02.2
    finally:
        await registry.shutdown()


@aio
async def test_large_log_read_intact_across_a_split_character(
    scope: Scope, clock: FakeClock
) -> None:
    box = Sandbox()
    registry = ProcessRegistry(clock=clock)
    record = await registry.spawn(request(scope, box))
    try:
        text = ("line ✓ ünïcode " * 20_000)[:300_000]
        raw = text.encode()
        cut = raw.index("✓".encode()) + 1  # in the middle of a three-byte character
        box.log = raw[:cut]
        await one_round(record)
        box.log = raw
        box.alive, box.exit = False, 0
        await one_round(record)
        assert record.total_chars == len(text)
        assert record.buffer == text[-len(record.buffer) :]
        assert "�" not in record.buffer
    finally:
        await registry.shutdown()


@aio
async def test_missing_pid_is_a_failed_start(scope: Scope, clock: FakeClock) -> None:
    box = Sandbox()
    box.pid = None
    registry = ProcessRegistry(clock=clock)
    record = await registry.spawn(request(scope, box))
    assert record.exited and record.reason == "failed_start"
    assert "did not report a process id" in record.buffer and "syntax error" in record.buffer
    box.pid, box.fail = 1, BackendError("too many commands are running at once")
    second = await registry.spawn(request(scope, box))
    assert second.exited and second.reason == "failed_start"
    await registry.shutdown()


@aio
async def test_backend_gone_marks_the_job_lost(scope: Scope, clock: FakeClock) -> None:
    deliveries = Deliveries()
    inbox = Inbox()
    deliveries.register(str(scope.home), "s1", inbox)
    registry = ProcessRegistry(deliveries, clock=clock)
    box = Sandbox()
    record = await registry.spawn(request(scope, box, notify_on_exit=True))
    box.fail = EnvironmentConnectionError("daemon gone")
    await one_round(record)
    assert record.exited and record.reason == "lost" and record.source == "backend_lost"
    assert "process lost" in record.buffer
    assert [e.reason for e in inbox.events if e.kind == "completion"] == ["lost"]
    # A replaced container loses its jobs too (R02.19).
    other = Sandbox()
    second = await registry.spawn(request(scope, other))
    other.recreated = True
    await one_round(second)
    assert second.reason == "lost" and "replaced" in second.buffer
    # A closed environment as well.
    third_box = Sandbox()
    third = await registry.spawn(request(scope, third_box))
    third_box.fail = BackendError("the docker environment was cleaned up")
    await one_round(third)
    assert third.reason == "lost"
    await registry.shutdown()


@aio
async def test_repeated_read_failures_end_as_lost(scope: Scope, clock: FakeClock) -> None:
    registry = ProcessRegistry(clock=clock)
    box = Sandbox()
    record = await registry.spawn(request(scope, box))
    box.fail = BackendError("too many commands are running at once")
    for _ in range(MAX_POLL_FAILURES - 1):
        await one_round(record)
    assert not record.exited
    box.fail = None
    await one_round(record)  # a good read resets the count
    box.fail = BackendError("too many commands are running at once")
    for _ in range(MAX_POLL_FAILURES):
        await one_round(record)
    assert record.exited and record.reason == "lost"
    await registry.shutdown()


@aio
async def test_kill_terms_the_group_then_kills_after_the_grace(
    scope: Scope, clock: FakeClock
) -> None:
    registry = ProcessRegistry(clock=clock)
    box = Sandbox()
    box.term_stops = False  # ignores SIGTERM
    record = await registry.spawn(request(scope, box))
    box.log = b"last words\n"
    await drive(clock, registry.kill(record))
    assert box.signals[0] == "TERM" and box.signals[-1] == "KILL"
    assert set(box.signals[1:-1]) <= {"0"} and len(box.signals) > 2
    kills = [c[0] for c in box.calls if c[0].startswith("kill -TERM")]
    assert "-- -4242" in kills[0]  # the whole process group (R02.12)
    assert record.exited and record.reason == "killed" and "last words" in record.buffer
    # A job that stops on SIGTERM gets no SIGKILL.
    polite = Sandbox()
    second = await registry.spawn(request(scope, polite))
    await drive(clock, registry.kill(second))
    assert polite.signals == ["TERM"]
    await registry.shutdown()


@aio
async def test_pty_is_not_available_remotely(scope: Scope, clock: FakeClock) -> None:
    registry = ProcessRegistry(clock=clock)
    record = await registry.spawn(request(scope, Sandbox(), use_pty=True))
    assert "not available on the docker backend" in record.pty_note
    assert await registry.write(record, "y\n") is not None
    await registry.shutdown()


@aio
async def test_no_completion_where_nothing_can_receive_it(scope: Scope, clock: FakeClock) -> None:
    deliveries = Deliveries()
    inbox = Inbox()
    deliveries.register(str(scope.home), "other-session", inbox)
    registry = ProcessRegistry(deliveries, clock=clock)
    box = Sandbox()
    record = await registry.spawn(request(scope, box, notify_on_exit=True))
    box.alive, box.exit = False, 0
    await one_round(record)
    assert record.exited and inbox.events == []  # R02.17
    await registry.shutdown()


def test_poll_parsing_ignores_noise() -> None:
    files = job_files("/tmp", "proc_0123456789ab")
    script = poll_script(files, 77, 5)
    assert "__o=5" in script and "-- -77" in script
    tok = "__INDUS_POLL_indus_bg_proc_0123456789ab__"
    out = (
        f"motd line\n{tok} 5 9\n{base64.b64encode(b'abcd').decode()}\n{tok} alive 0\n{tok} exit 7\n"
    )
    reading = parse_poll(out, files)
    assert reading is not None
    assert (reading.offset, reading.size, reading.data, reading.alive) == (5, 9, b"abcd", False)
    assert reading.exited and reading.exit_code == 7
    assert parse_poll("garbage", files) is None


# -- end to end over the fake ssh CLI --------------------------------------------------------


def _wait_for(path: Path, seconds: float = 10.0) -> bool:
    gate = threading.Event()
    for _ in range(int(seconds / 0.01)):
        if path.exists() and path.read_text().strip():
            return True
        gate.wait(0.01)
    return False


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@aio
async def test_ssh_background_job_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote_tmp = redirect_remote_tmp(monkeypatch, tmp_path / "remote-tmp")
    fake = install(tmp_path / "bin", tmp_path / "remote-home")
    runtime = tempfile.mkdtemp(prefix="isr", dir="/tmp")
    scope = terminal_scope(
        tmp_path, {"terminal": {"backend": "ssh", "ssh_host": "box.test", "ssh_user": "dev"}}
    )
    scope = scope.with_env(
        scope.env.with_overlay(
            {"PATH": fake.path_env(scope.env.get("PATH", "")), "XDG_RUNTIME_DIR": runtime}
        )
    )
    env = await open_ssh(scope, clock=SystemClock())
    deliveries = Deliveries()
    inbox = Inbox()
    deliveries.register(str(scope.home), "s1", inbox)
    registry = ProcessRegistry(deliveries, clock=FakeClock(datetime(2026, 10, 18, tzinfo=UTC)))
    try:
        record = await registry.spawn(
            request(
                scope,
                env,
                "printf 'from the remote side\\n'; exit 4",
                cwd="~",
                notify_on_exit=True,
            )
        )
        assert not record.exited and record.pid
        exit_file = Path(job(record).files.exit)
        assert exit_file.parent == remote_tmp  # never the shared /tmp
        ready = await asyncio.to_thread(_wait_for, exit_file)
        assert ready
        await one_round(record)
        assert record.exited and record.exit_code == 4
        assert record.buffer == "from the remote side\n"
        assert [e.exit_code for e in inbox.events if e.kind == "completion"] == [4]
    finally:
        await registry.shutdown()
        await env.cleanup()
        shutil.rmtree(runtime, ignore_errors=True)


class RecoveringSandbox(Sandbox):
    """A sandbox that counts replacements and keeps a one-shot recreated flag, like Docker."""

    def __init__(self) -> None:
        super().__init__()
        self.recreations = 0
        self.rearmed = 0

    def mark_recreated(self) -> None:
        self.rearmed += 1


@aio
async def test_poll_peeks_at_the_recreated_flag(scope: Scope, clock: FakeClock) -> None:
    registry = ProcessRegistry(clock=clock)
    box = RecoveringSandbox()
    record = await registry.spawn(request(scope, box))
    box.recreated = True
    await one_round(record)
    assert record.reason == "lost"
    assert box.rearmed == 1  # put back for the next foreground command's warning
    await registry.shutdown()


@aio
async def test_replaced_sandbox_loses_the_job_without_signalling(
    scope: Scope, clock: FakeClock
) -> None:
    registry = ProcessRegistry(clock=clock)
    box = RecoveringSandbox()
    record = await registry.spawn(request(scope, box))
    box.recreations += 1  # replaced; the flag went to another caller
    await one_round(record)
    assert record.exited and record.reason == "lost" and "replaced" in record.buffer
    box.signals.clear()
    other = await registry.spawn(request(scope, box))
    box.recreations += 1
    await registry.kill(other)
    assert box.signals == []  # its pid now names something else in the new sandbox
    await registry.shutdown()
