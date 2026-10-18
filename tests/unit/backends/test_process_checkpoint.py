"""Crash recovery of background jobs (P11-14): real sleeping children and a checkpoint file."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import stat
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from indus.backends import process_checkpoint as pc
from indus.backends.local import process_start_time
from indus.backends.process_notify import Deliveries
from indus.backends.processes import ProcessRegistry, SpawnRequest
from indus.backends.registry import EnvironmentRegistry
from indus.foundation.clock import SystemClock
from indus.foundation.scope import Scope
from indus.safety.child_env import child_env
from indus.tools.builtin.process_manage import ProcessManageTool
from indus.tools.builtin.terminal import TerminalRuntime
from tests.fakes.terminal import Inbox, forge_token, terminal_scope
from tests.unit.backends._aio import aio

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or not Path("/proc/self/stat").exists(),
    reason="needs bash and /proc",
)


@pytest.fixture
def scope(tmp_path: Path) -> Scope:
    return terminal_scope(tmp_path, {"terminal": {"daemon_term_grace_s": 0.3}})


@pytest.fixture
def sleepers() -> Iterator[list[subprocess.Popen[bytes]]]:
    started: list[subprocess.Popen[bytes]] = []
    yield started
    for proc in started:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def sleeper(started: list[subprocess.Popen[bytes]]) -> subprocess.Popen[bytes]:
    proc = subprocess.Popen(["sleep", "60"], start_new_session=True)  # noqa: S607
    started.append(proc)
    return proc


def request(scope: Scope, command: str, **kw: Any) -> SpawnRequest:
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


def entries(scope: Scope) -> list[dict[str, Any]]:
    return json.loads(pc.checkpoint_path(scope.home).read_text())


def write_entries(scope: Scope, items: list[dict[str, Any]]) -> None:
    pc.checkpoint_path(scope.home).write_text(json.dumps(items))


def entry(ident: str, pid: int, start: int | None, **kw: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": ident,
        "pid": pid,
        "pid_start_time": start,
        "backend": "local",
        "env_key": "k1",
        "log_path": "",
        "owner_session": "s1",
        "notify_on_exit": False,
        "persist_on_release": False,
        "command": "sleep 60",
        "cwd": "/",
        "started_at": "2026-10-18T09:00:00+00:00",
        "holder": {"pid": None, "start": None},
    }
    body.update(kw)
    return body


async def _wait(event: asyncio.Event, seconds: float = 10.0) -> None:
    await asyncio.wait_for(event.wait(), seconds)


@aio
async def test_new_registry_adopts_a_live_job_as_detached(scope: Scope) -> None:
    token = forge_token()
    first = ProcessRegistry(clock=SystemClock())
    record = await first.spawn(request(scope, f"sleep 60 # {token}", notify_on_exit=True))
    try:
        saved = entries(scope)
        assert [e["id"] for e in saved] == [record.id]
        assert saved[0]["pid"] == record.pid and saved[0]["pid_start_time"] == process_start_time(
            record.pid or 0
        )
        assert saved[0]["holder"]["pid"] == os.getpid() and saved[0]["notify_on_exit"] is True
        assert token not in pc.checkpoint_path(scope.home).read_text()
        assert stat.S_IMODE(pc.checkpoint_path(scope.home).stat().st_mode) == 0o600

        # The process that started it is gone; a new registry serves the profile.
        deliveries = Deliveries()
        inbox = Inbox()
        deliveries.register(str(scope.home), "s1", inbox)
        second = ProcessRegistry(deliveries, clock=SystemClock())
        found = second.recover(scope)
        assert found is not None and [e.id for e in found.adopt] == [record.id]
        adopted = second.get(record.id, str(scope.home))
        assert adopted is not None and adopted.detached and not adopted.exited
        assert adopted.pid == record.pid and adopted.buffer == ""

        runtime = TerminalRuntime(EnvironmentRegistry(), second, deliveries)
        tool = ProcessManageTool(runtime)
        polled = json.loads(
            await tool({"action": "poll", "session_id": record.id}, scope=scope, session_id="s1")
        )
        assert polled["status"] == "running" and polled["detached"] is True
        assert "recovered after Indus restarted" in polled["note"]
        listed = json.loads(await tool({"action": "list"}, scope=scope, session_id="s1"))
        assert listed["processes"][0]["detached"] is True

        # It ends on its own: notify-on-exit still fires, with no exit code to report.
        os.kill(record.pid or 0, signal.SIGTERM)
        await _wait(inbox.arrived)
        done = [e for e in inbox.events if e.kind == "completion"]
        assert [e.process_id for e in done] == [record.id] and done[0].exit_code is None
        assert adopted.exited and adopted.exit_code is None
        assert entries(scope) == []
    finally:
        await first.shutdown()


@aio
async def test_adopted_job_can_be_killed(scope: Scope) -> None:
    first = ProcessRegistry(clock=SystemClock())
    record = await first.spawn(request(scope, "trap '' TERM; sleep 60"))
    try:
        second = ProcessRegistry(clock=SystemClock())
        second.recover(scope)
        adopted = second.get(record.id, str(scope.home))
        assert adopted is not None and adopted.detached
        await second.kill(adopted)
        assert adopted.exited and adopted.reason == "killed"
        await _wait(record.done)  # the original handle sees its child die
    finally:
        await first.shutdown()


@aio
async def test_recycled_pid_is_lost_and_never_signalled(
    scope: Scope, sleepers: list[subprocess.Popen[bytes]]
) -> None:
    other = sleeper(sleepers)
    real_start = process_start_time(other.pid)
    assert real_start is not None
    write_entries(
        scope,
        [
            entry("proc_00000000aaaa", other.pid, real_start + 7),  # same pid, other process
            entry("proc_00000000bbbb", 2**22 + 12345, 99),  # gone
            entry("proc_00000000cccc", other.pid, real_start, backend="ssh"),  # in-sandbox pid
        ],
    )
    registry = ProcessRegistry(clock=SystemClock())
    found = registry.recover(scope)
    assert found is not None and not found.adopt and len(found.lost) == 3
    for ident in ("proc_00000000aaaa", "proc_00000000bbbb", "proc_00000000cccc"):
        record = registry.get(ident, str(scope.home))
        assert record is not None and record.exited and record.reason == "lost"
    await registry.shutdown()
    assert other.poll() is None  # nobody signalled the stranger
    assert entries(scope) == []


@aio
async def test_corrupt_checkpoint_is_ignored_with_a_warning(
    scope: Scope, caplog: pytest.LogCaptureFixture
) -> None:
    pc.checkpoint_path(scope.home).write_text("{not json")
    registry = ProcessRegistry(clock=SystemClock())
    with caplog.at_level(logging.WARNING):
        found = registry.recover(scope)
    assert found is not None and not (found.adopt or found.lost)
    assert "not valid JSON" in caplog.text
    record = await registry.spawn(request(scope, "sleep 60"))
    assert [e["id"] for e in entries(scope)] == [record.id]
    await registry.shutdown()
    assert entries(scope) == []


@aio
async def test_entries_of_a_live_other_holder_are_kept(
    scope: Scope, sleepers: list[subprocess.Popen[bytes]]
) -> None:
    holder = sleeper(sleepers)  # stands in for a second Indus process on this profile
    job = sleeper(sleepers)
    theirs = entry(
        "proc_00000000dddd",
        job.pid,
        process_start_time(job.pid),
        holder={"pid": holder.pid, "start": process_start_time(holder.pid)},
    )
    write_entries(scope, [theirs])
    registry = ProcessRegistry(clock=SystemClock())
    found = registry.recover(scope)
    assert found is not None and [e.id for e in found.foreign] == ["proc_00000000dddd"]
    assert registry.get("proc_00000000dddd", str(scope.home)) is None
    mine = await registry.spawn(request(scope, "sleep 60"))
    assert {e["id"] for e in entries(scope)} == {"proc_00000000dddd", mine.id}
    await registry.shutdown()
    assert [e["id"] for e in entries(scope)] == ["proc_00000000dddd"]
    assert job.poll() is None


@aio
async def test_persist_on_release_survives_session_end_not_shutdown(scope: Scope) -> None:
    registry = ProcessRegistry(clock=SystemClock())
    kept = await registry.spawn(request(scope, "sleep 60", persist_on_release=True))
    await registry.spawn(request(scope, "sleep 60"))
    assert len(entries(scope)) == 2
    assert await registry.release(str(scope.home), "s1") == 1
    assert [e["id"] for e in entries(scope)] == [kept.id]
    assert entries(scope)[0]["persist_on_release"] is True
    await registry.shutdown()
    assert entries(scope) == []


@aio
async def test_each_profile_has_its_own_checkpoint(tmp_path: Path) -> None:
    first = terminal_scope(tmp_path, name="a")
    second = terminal_scope(tmp_path, name="b")
    registry = ProcessRegistry(clock=SystemClock())
    one = await registry.spawn(request(first, "sleep 60"))
    two = await registry.spawn(request(second, "sleep 60"))
    assert [e["id"] for e in entries(first)] == [one.id]  # R02.8
    assert [e["id"] for e in entries(second)] == [two.id]
    await registry.shutdown()
