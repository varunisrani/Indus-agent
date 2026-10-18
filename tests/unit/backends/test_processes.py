"""The process registry (P05-13): real short-lived processes for spawn/exit/kill, fake job handles
and a fake clock for limits, watch patterns and heartbeats."""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import os
import shutil
import signal
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from indus.backends.process_notify import GLOBAL_MAX_PER_WINDOW, WATCH_SPACING_S
from indus.backends.processes import (
    FINISHED_TTL_S,
    MAX_TRACKED,
    LaunchSpec,
    ProcessLimit,
    ProcessRecord,
    ProcessRegistry,
    SpawnRequest,
)
from indus.foundation.clock import SystemClock
from indus.foundation.scope import Scope
from indus.safety.child_env import child_env
from tests.fakes.clock import FakeClock
from tests.fakes.terminal import FakeJob, Inbox, forge_token, terminal_scope

HAVE_BASH = shutil.which("bash") is not None and Path("/proc/self/stat").exists()
real = pytest.mark.skipif(not HAVE_BASH, reason="needs bash and /proc")
HAVE_PTY = importlib.util.find_spec("ptyprocess") is not None


def _request(scope: Scope, command: str, **kw: Any) -> SpawnRequest:
    values: dict[str, Any] = {
        "command": command,
        "scope": scope,
        "session_id": "s1",
        "env_key": "k1",
        "cwd": str(scope.home),
        "env": child_env(scope),
    }
    values.update(kw)
    return SpawnRequest(**values)


async def _ended(record: ProcessRecord, seconds: float = 10.0) -> None:
    await asyncio.wait_for(record.done.wait(), seconds)


def _alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state not in ("Z", "X")


@pytest.fixture
def real_scope(tmp_path: Path) -> Scope:
    return terminal_scope(tmp_path, {"terminal": {"daemon_term_grace_s": 0.3}})


# -- real processes -----------------------------------------------------------------------------


@real
def test_spawn_runs_and_reports_exit(real_scope: Scope) -> None:
    async def go() -> ProcessRecord:
        registry = ProcessRegistry(clock=SystemClock())
        record = await registry.spawn(_request(real_scope, "printf 'hello\\n'; exit 3"))
        assert record.status == "running" or record.exited
        assert record.id.startswith("proc_") and len(record.id) == len("proc_") + 12
        await _ended(record)
        return record

    record = asyncio.run(go())
    assert record.exit_code == 3 and record.reason == "exited"
    assert record.buffer == "hello\n"


@real
def test_failed_start_is_not_running(real_scope: Scope) -> None:
    async def go() -> tuple[ProcessRecord, ProcessRegistry]:
        registry = ProcessRegistry(clock=SystemClock())
        record = await registry.spawn(_request(real_scope, "true", cwd="/no/such/dir/here"))
        return record, registry

    record, registry = asyncio.run(go())
    assert record.exited and record.reason == "failed_start" and record.exit_code == -1
    assert "could not start" in record.buffer
    assert record not in registry.running()


@real
def test_grandchild_holding_pipe_does_not_delay_exit(real_scope: Scope) -> None:
    """``sleep 30 &`` keeps the pipe open; the completion still comes at the leader's exit."""

    async def go() -> ProcessRecord:
        registry = ProcessRegistry(clock=SystemClock())
        record = await registry.spawn(_request(real_scope, "sleep 30 & echo $!; echo leader-done"))
        await _ended(record, 5.0)
        return record

    record = asyncio.run(go())
    try:
        assert record.exit_code == 0
        assert "leader-done" in record.buffer
    finally:
        first = record.buffer.split()[0]
        if first.isdigit():  # the orphaned sleep is not the registry's to stop
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(first), signal.SIGKILL)


@real
def test_kill_sigkills_a_sigterm_ignoring_child(real_scope: Scope) -> None:
    async def go() -> tuple[ProcessRecord, float]:
        registry = ProcessRegistry(clock=SystemClock())
        inbox = Inbox()
        registry.deliveries.register(str(real_scope.home), "s1", inbox)
        record = await registry.spawn(
            _request(
                real_scope,
                "trap '' TERM; echo ready; while :; do sleep 0.05; done",
                watch_patterns=["ready"],
            )
        )
        await asyncio.wait_for(inbox.arrived.wait(), 10)  # the trap is in place
        clock = SystemClock()
        started = clock.monotonic()
        await registry.kill(record, source="test kill")
        return record, clock.monotonic() - started

    record, took = asyncio.run(go())
    assert record.exited and record.reason == "killed" and record.source == "test kill"
    assert record.consumed
    assert record.pid is not None and not _alive(record.pid)
    assert took < 5.0


@real
def test_stdin_write_then_close(real_scope: Scope) -> None:
    async def go() -> tuple[ProcessRecord, str | None, str | None]:
        registry = ProcessRegistry(clock=SystemClock())
        record = await registry.spawn(_request(real_scope, "cat"))
        wrote = await registry.write(record, "hi there\n")
        closed = registry.close_input(record)
        await _ended(record)
        return record, wrote, closed

    record, wrote, closed = asyncio.run(go())
    assert wrote is None and closed is None
    assert record.exit_code == 0 and record.buffer == "hi there\n"


@real
def test_completion_reaches_the_session_redacted(real_scope: Scope) -> None:
    token = forge_token()

    async def go() -> Inbox:
        registry = ProcessRegistry(clock=SystemClock())
        inbox = Inbox()
        registry.deliveries.register(str(real_scope.home), "s1", inbox)
        record = await registry.spawn(
            _request(real_scope, f"echo start {token}; exit 1", notify_on_exit=True)
        )
        await _ended(record)
        return inbox

    inbox = asyncio.run(go())
    assert inbox.kinds() == ["completion"]
    event = inbox.events[0]
    assert event.exit_code == 1 and "start" in event.output
    assert token not in event.output and token not in event.command and token not in event.text
    assert "finished with exit code 1" in event.text


@real
@pytest.mark.skipif(not HAVE_PTY, reason="needs the pty extra")
def test_pty_job_sees_a_terminal(real_scope: Scope) -> None:
    async def go() -> ProcessRecord:
        registry = ProcessRegistry(clock=SystemClock())
        record = await registry.spawn(
            _request(
                real_scope,
                'tty -s && echo is-a-tty; read -r line; echo "got:$line"',
                use_pty=True,
            )
        )
        assert record.pty and not record.pty_note
        assert await registry.write(record, "abc\n") is None
        await _ended(record)
        return record

    record = asyncio.run(go())
    assert record.exit_code == 0
    assert "is-a-tty" in record.buffer and "got:abc" in record.buffer


def test_pty_without_the_extra_falls_back_to_pipes(tmp_path: Path) -> None:
    scope = terminal_scope(tmp_path)
    jobs: list[LaunchSpec] = []

    def launcher(spec: LaunchSpec) -> FakeJob:
        jobs.append(spec)
        if spec.use_pty:
            raise ImportError("no module named ptyprocess")
        return FakeJob()

    async def go() -> ProcessRecord:
        registry = ProcessRegistry(launcher=launcher, watch_exits=False)
        return await registry.spawn(_request(scope, "python3 -i", use_pty=True))

    record = asyncio.run(go())
    assert [s.use_pty for s in jobs] == [True, False]
    assert not record.pty and "plain pipes" in record.pty_note


@real
def test_release_spares_persist_on_release(real_scope: Scope) -> None:
    async def go() -> tuple[ProcessRecord, ProcessRecord, int]:
        registry = ProcessRegistry(clock=SystemClock())
        plain = await registry.spawn(_request(real_scope, "sleep 30"))
        kept = await registry.spawn(_request(real_scope, "sleep 30", persist_on_release=True))
        stopped = await registry.release(str(real_scope.home), "s1")
        assert plain.exited and not kept.exited
        await registry.shutdown()
        return plain, kept, stopped

    plain, kept, stopped = asyncio.run(go())
    assert stopped == 1
    assert plain.reason == "killed" and plain.source == "session ended"
    assert kept.exited and kept.source == "shutdown"


# -- fake handles, fake clock -------------------------------------------------------------------


def _clock() -> FakeClock:
    return FakeClock(datetime(2026, 10, 18, tzinfo=UTC))


class Lab:
    """A registry over ``FakeJob`` handles with a fake clock."""

    def __init__(self, tmp_path: Path, *, max_tracked: int = MAX_TRACKED) -> None:
        self.clock = _clock()
        self.scope = terminal_scope(tmp_path)
        self.jobs: list[FakeJob] = []
        self.inbox = Inbox()
        self.registry = ProcessRegistry(
            clock=self.clock, launcher=self._launch, watch_exits=False, max_tracked=max_tracked
        )
        self.registry.deliveries.register(str(self.scope.home), "s1", self.inbox)

    def _launch(self, spec: LaunchSpec) -> FakeJob:
        job = FakeJob(pid=5000 + len(self.jobs))
        self.jobs.append(job)
        return job

    async def spawn(self, command: str = "job", **kw: Any) -> ProcessRecord:
        values: dict[str, Any] = {"env": {"PATH": "/usr/bin:/bin"}, "cwd": "/"}
        values.update(kw)
        return await self.registry.spawn(_request(self.scope, command, **values))

    def finish(self, record: ProcessRecord, code: int = 0) -> None:
        assert record.handle is not None
        job = record.handle
        assert isinstance(job, FakeJob)
        job.code = code
        self.registry.notice_exit(record)


@pytest.fixture
def lab(tmp_path: Path) -> Iterator[Lab]:
    yield Lab(tmp_path)


def test_lru_drops_the_least_recently_used_finished_job(lab: Lab) -> None:
    async def go() -> None:
        records = [await lab.spawn(f"job {i}") for i in range(MAX_TRACKED)]
        for record in records:
            lab.finish(record)
        lab.clock.advance(1)
        for record in records[1:]:
            assert lab.registry.get(record.id, str(lab.scope.home)) is record  # touched
        newest = await lab.spawn("job 65")
        home = str(lab.scope.home)
        assert lab.registry.get(records[0].id, home) is None  # the LRU one went
        assert lab.registry.get(records[1].id, home) is records[1]
        assert lab.registry.get(newest.id, home) is newest

    asyncio.run(go())


def test_running_cap_refuses_a_new_job(tmp_path: Path) -> None:
    small = Lab(tmp_path, max_tracked=3)

    async def go() -> None:
        for i in range(3):
            await small.spawn(f"job {i}")
        with pytest.raises(ProcessLimit):
            await small.spawn("one too many")

    asyncio.run(go())


def test_finished_jobs_are_kept_thirty_minutes(lab: Lab) -> None:
    async def go() -> None:
        record = await lab.spawn()
        lab.finish(record)
        home = str(lab.scope.home)
        lab.clock.advance(FINISHED_TTL_S - 1)
        assert lab.registry.get(record.id, home) is record
        lab.clock.advance(2)
        assert lab.registry.get(record.id, home) is None

    asyncio.run(go())


def test_prefix_lookup(lab: Lab, monkeypatch: pytest.MonkeyPatch) -> None:
    ids = iter(["proc_abcd00000001", "proc_abcd00000002", "proc_ef0100000003"])
    monkeypatch.setattr(lab.registry, "_new_id", lambda: next(ids))

    async def go() -> None:
        one = await lab.spawn()
        await lab.spawn()
        three = await lab.spawn()
        home = str(lab.scope.home)
        get = lab.registry.get
        assert get("proc_abcd00000001", home) is one
        assert get("abcd00000001", home) is one
        assert get("abcd", home) is None  # two jobs start with it: never a guess
        assert get("ef01", home) is three
        assert get("proc_ef01", home) is three
        assert get("ef0", home) is None  # too short
        assert get("ef01", "/another/profile") is None  # R02.8
        assert get(1234, home) is None

    asyncio.run(go())


def test_completion_once_and_not_after_it_was_read(lab: Lab) -> None:
    async def go() -> None:
        told = await lab.spawn("make", notify_on_exit=True)
        lab.registry.ingest(told, "compiling\nall done\n")
        lab.finish(told, 0)
        lab.registry.notice_exit(told)  # a second notice changes nothing
        read = await lab.spawn("make test", notify_on_exit=True)
        read.consumed = True  # the model already read it (wait/log)
        lab.finish(read, 0)

    asyncio.run(go())
    assert lab.inbox.kinds() == ["completion"]
    assert "all done" in lab.inbox.events[0].output


def test_notify_needs_a_registered_session(lab: Lab) -> None:
    async def go() -> None:
        record = await lab.spawn("make", notify_on_exit=True, session_id="cron-run")
        lab.finish(record)

    asyncio.run(go())
    assert lab.inbox.events == []  # nobody can receive it there (R02.17)


def test_watch_patterns_rate_limited_then_switched_off(lab: Lab) -> None:
    async def go() -> ProcessRecord:
        record = await lab.spawn("server", watch_patterns=["ERR"])
        for _ in range(3):
            lab.registry.ingest(record, "ok\nERR first in window\n")
            lab.registry.ingest(record, "ERR dropped\nERR dropped too\n")
            lab.clock.advance(WATCH_SPACING_S + 1)
        assert record.watch is not None and record.watch.off
        assert record.notify_on_exit  # falls back to one message at the exit
        lab.registry.ingest(record, "ERR after off\n")
        lab.finish(record, 1)
        return record

    asyncio.run(go())
    assert lab.inbox.kinds() == ["watch_match"] * 3 + ["watch_off", "completion"]
    assert lab.inbox.events[0].pattern == "ERR"
    assert lab.inbox.events[1].suppressed == 2


def test_watch_lines_split_across_reads(lab: Lab) -> None:
    async def go() -> None:
        record = await lab.spawn("server", watch_patterns=["READY"])
        lab.registry.ingest(record, "booting...\nRE")
        assert lab.inbox.events == []
        lab.registry.ingest(record, "ADY on port 8000\n")

    asyncio.run(go())
    assert lab.inbox.kinds() == ["watch_match"]
    assert "READY on port 8000" in lab.inbox.events[0].output


def test_global_breaker_across_processes(lab: Lab) -> None:
    async def go() -> None:
        records = [
            await lab.spawn(f"svc {i}", watch_patterns=["up"])
            for i in range(GLOBAL_MAX_PER_WINDOW + 1)
        ]
        for record in records:
            lab.registry.ingest(record, "up\n")
            lab.clock.advance(0.1)

    asyncio.run(go())
    kinds = lab.inbox.kinds()
    assert kinds.count("watch_match") == GLOBAL_MAX_PER_WINDOW
    assert kinds[-1] == "watch_paused"


def test_heartbeat_ticks(lab: Lab) -> None:
    async def go() -> ProcessRecord:
        record = await lab.spawn("long build", heartbeat_s=30, notify_on_exit=True)
        assert record.heartbeat is not None and record.heartbeat.every_s == 60  # raised to 60
        lab.registry.ingest(record, "step 1\n")
        lab.clock.advance(59)
        assert lab.registry.heartbeat_round() == 0  # not due yet
        lab.clock.advance(1)
        assert lab.registry.heartbeat_round() == 1
        lab.clock.advance(60)
        assert lab.registry.heartbeat_round() == 0  # quiet: skipped
        lab.registry.ingest(record, "step 2\n")
        lab.clock.advance(60)
        assert lab.registry.heartbeat_round() == 1
        lab.finish(record)
        lab.clock.advance(60)
        assert lab.registry.heartbeat_round() == 0  # ended: no more ticks
        return record

    asyncio.run(go())
    beats = [e for e in lab.inbox.events if e.kind == "heartbeat"]
    assert [b.output for b in beats] == ["step 1\n", "step 2\n"]
    assert [b.seq for b in beats] == [1, 2]
    assert lab.inbox.kinds()[-1] == "completion"


def test_kill_uses_the_ordered_stop_and_counts_as_read(lab: Lab) -> None:
    async def go() -> ProcessRecord:
        record = await lab.spawn("server", notify_on_exit=True)
        await lab.registry.kill(record)
        return record

    record = asyncio.run(go())
    job = lab.jobs[0]
    assert job.stopped == 1 and job.released == 1
    assert record.reason == "killed" and record.exit_code == -15
    assert lab.inbox.events == []  # the caller has the result; no extra turn


def test_alive_for_env_key_keeps_the_environment(lab: Lab) -> None:
    async def go() -> None:
        record = await lab.spawn(env_key="home::session:s1@local")
        assert lab.registry.alive_for_env_key("home::session:s1@local")
        assert not lab.registry.alive_for_env_key("home::session:s2@local")
        lab.finish(record)
        assert not lab.registry.alive_for_env_key("home::session:s1@local")

    asyncio.run(go())


def test_list_is_per_profile_and_session(tmp_path: Path) -> None:
    lab = Lab(tmp_path)
    other = terminal_scope(tmp_path, name="other")

    async def go() -> None:
        mine = await lab.spawn("a")
        await lab.spawn("b", session_id="s2")
        await lab.registry.spawn(
            SpawnRequest("c", other, "s1", "k", "/", {"PATH": "/usr/bin:/bin"})
        )
        assert lab.registry.list(str(lab.scope.home), "s1") == [mine]

    asyncio.run(go())
