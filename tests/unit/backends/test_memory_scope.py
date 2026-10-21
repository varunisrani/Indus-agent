"""Memory caps for background processes (P24-43): the scope argv, clamping, availability, the
probe cache and the OOM reason, through our fake ``systemd-run`` (tests/fakes/systemd_run.py)."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from indus.backends import memory_scope
from indus.backends.memory_scope import (
    DEFAULT_CAP,
    MAX_CAP,
    MIB,
    MIN_CAP,
    ScopeProbe,
    is_memory_kill,
    parse_cap,
    wrap,
)
from indus.backends.process_notify import completion_text
from indus.backends.processes import ProcessRecord, ProcessRegistry, SpawnRequest
from indus.foundation.clock import SystemClock
from indus.foundation.scope import Scope
from indus.safety.child_env import child_env
from tests.fakes import systemd_run
from tests.fakes.clock import FakeClock
from tests.fakes.terminal import terminal_scope

LINUX = sys.platform.startswith("linux") and shutil.which("bash") is not None
linux_only = pytest.mark.skipif(not LINUX, reason="systemd scopes are Linux only")


@pytest.fixture(autouse=True)
def _fresh_probe() -> Iterator[None]:
    memory_scope.reset_probe()
    yield
    memory_scope.reset_probe()


@pytest.fixture
def fake_bin(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "fakebin"
    systemd_run.install(bin_dir)
    return bin_dir


def _env(scope: Scope, fake_bin: Path, log: Path, **extra: str) -> dict[str, str]:
    env = child_env(scope)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
    env["FAKE_SYSTEMD_RUN_LOG"] = str(log)
    env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/fake/bus"
    env.update(extra)
    return env


# -- the cap value --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, DEFAULT_CAP),
        ("1G", 1024 * MIB),
        ("512M", 512 * MIB),
        ("768MiB", 768 * MIB),
        ("2gb", 2048 * MIB),
        (300, 300 * MIB),
        ("16M", MIN_CAP),  # clamped up
        ("1", MIN_CAP),
        ("64G", MAX_CAP),  # clamped down
        (10_000, MAX_CAP),
        ("off", None),
        ("0", None),
        (False, None),
        (0, None),
        ("not a size", DEFAULT_CAP),
    ],
)
def test_parse_cap_clamps_and_turns_off(raw: object, expected: int | None) -> None:
    assert parse_cap(raw) == expected


def test_out_of_range_value_warns_once(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="indus.backends.memory_scope")
    parse_cap("8G")
    parse_cap("8G")
    assert len([r for r in caplog.records if "outside" in r.getMessage()]) == 1


# -- wrapping -------------------------------------------------------------------------------------


@linux_only
def test_argv_carries_the_cap_and_a_unit(tmp_path: Path, fake_bin: Path) -> None:
    scope = terminal_scope(tmp_path, {"terminal": {"background_memory_max": "512M"}})
    log = tmp_path / "calls.jsonl"
    out = wrap(scope, ["bash", "-c", "true"], _env(scope, fake_bin, log), job_id="proc_abc")
    assert out.cap == 512 * MIB
    assert out.argv[0].endswith("systemd-run")
    assert out.argv[1:5] == ("--user", "--scope", "--quiet", "--collect")
    assert f"MemoryMax={512 * MIB}" in out.argv
    assert "--expand-environment=no" in out.argv
    tail = out.argv[out.argv.index("--") + 1 :]
    assert tail == ("bash", "-c", "true")
    unit = out.argv[out.argv.index("--unit") + 1]
    assert unit.startswith("indus-bg-proc-abc-")
    probe_calls = systemd_run.read_log(log)
    assert len(probe_calls) == 1  # the probe ran once
    assert f"MemoryMax={MIN_CAP}" in probe_calls[0]


@linux_only
def test_clamped_value_reaches_the_argv(tmp_path: Path, fake_bin: Path) -> None:
    scope = terminal_scope(tmp_path, {"terminal": {"background_memory_max": "32G"}})
    out = wrap(scope, ["true"], _env(scope, fake_bin, tmp_path / "l"), job_id="proc_x")
    assert f"MemoryMax={MAX_CAP}" in out.argv


@linux_only
def test_off_means_no_scope(tmp_path: Path, fake_bin: Path) -> None:
    scope = terminal_scope(tmp_path, {"terminal": {"background_memory_max": "off"}})
    log = tmp_path / "calls.jsonl"
    out = wrap(scope, ["true"], _env(scope, fake_bin, log), job_id="proc_x")
    assert out.argv == ("true",) and out.cap == 0
    assert systemd_run.read_log(log) == []


@linux_only
def test_no_user_bus_no_scope_and_one_log_line(
    tmp_path: Path, fake_bin: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="indus.backends.memory_scope")
    scope = terminal_scope(tmp_path, {})
    log = tmp_path / "calls.jsonl"
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    env = _env(scope, fake_bin, log, XDG_RUNTIME_DIR=str(runtime))
    del env["DBUS_SESSION_BUS_ADDRESS"]
    first = wrap(scope, ["true"], env, job_id="proc_1")
    second = wrap(scope, ["true"], env, job_id="proc_2")
    assert first.cap == 0 and second.cap == 0 and first.argv == ("true",)
    lines = [r for r in caplog.records if r.name == "indus.backends.memory_scope"]
    assert len(lines) == 1 and "user bus" in lines[0].getMessage()
    assert systemd_run.read_log(log) == []


@linux_only
def test_a_bus_socket_in_the_runtime_dir_is_used(tmp_path: Path, fake_bin: Path) -> None:
    import socket

    scope = terminal_scope(tmp_path, {})
    runtime = tmp_path / "rt"
    runtime.mkdir()
    if len(str(runtime / "bus")) > 100:
        pytest.skip("temp path too long for a unix socket")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(str(runtime / "bus"))
        env = _env(scope, fake_bin, tmp_path / "l", XDG_RUNTIME_DIR=str(runtime))
        del env["DBUS_SESSION_BUS_ADDRESS"]
        out = wrap(scope, ["true"], env, job_id="proc_1")
        assert out.cap == DEFAULT_CAP
        assert out.env["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path={runtime / 'bus'}"
    finally:
        sock.close()


def test_never_outside_linux(tmp_path: Path) -> None:
    scope = terminal_scope(tmp_path, {})
    for platform in ("win32", "darwin", "freebsd14"):
        out = wrap(scope, ["true"], {"PATH": "/usr/bin"}, job_id="proc_1", platform=platform)
        assert out.argv == ("true",) and out.cap == 0


@linux_only
def test_failed_probe_skips_the_scope(tmp_path: Path, fake_bin: Path) -> None:
    scope = terminal_scope(tmp_path, {})
    env = _env(scope, fake_bin, tmp_path / "l", FAKE_SYSTEMD_RUN_FAIL="1")
    out = wrap(scope, ["true"], env, job_id="proc_1")
    assert out.cap == 0 and out.argv == ("true",)


@linux_only
def test_old_systemd_without_expand_option(tmp_path: Path, fake_bin: Path) -> None:
    scope = terminal_scope(tmp_path, {})
    log = tmp_path / "calls.jsonl"
    env = _env(scope, fake_bin, log, FAKE_SYSTEMD_RUN_REJECT_EXPAND="1")
    out = wrap(scope, ["true"], env, job_id="proc_1")
    assert out.cap == DEFAULT_CAP
    assert not any(a.startswith("--expand-environment") for a in out.argv)
    assert len(systemd_run.read_log(log)) == 2  # tried with, then without


def test_probe_verdict_is_cached_then_expires() -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str], **_kw: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    clock = FakeClock(datetime(2026, 10, 18, tzinfo=UTC))
    probe = ScopeProbe(runner=runner, clock=clock)
    env = {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/x"}
    assert probe.check("/usr/bin/systemd-run", env).ok
    assert probe.check("/usr/bin/systemd-run", env).ok
    assert len(calls) == 1
    clock.advance(memory_scope.PROBE_TTL_S + 1)
    probe.check("/usr/bin/systemd-run", env)
    assert len(calls) == 2


def test_a_hanging_probe_counts_as_unavailable() -> None:
    def runner(argv: list[str], **_kw: Any) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(argv, 5)

    verdict = ScopeProbe(runner=runner, clock=FakeClock(datetime(2026, 10, 18, tzinfo=UTC))).check(
        "/x/systemd-run", {}
    )
    assert not verdict.ok and "timed out" in verdict.why


# -- the OOM reason -------------------------------------------------------------------------------


def test_memory_kill_rule() -> None:
    kill = int(signal.SIGKILL)
    assert is_memory_kill(-kill, scoped=True, killed_by_us=False)
    assert is_memory_kill(128 + kill, scoped=True, killed_by_us=False)
    assert not is_memory_kill(-kill, scoped=False, killed_by_us=False)
    assert not is_memory_kill(-kill, scoped=True, killed_by_us=True)
    assert not is_memory_kill(1, scoped=True, killed_by_us=False)
    assert not is_memory_kill(None, scoped=True, killed_by_us=False)
    text = completion_text(
        "proc_1", "make", "", exit_code=-kill, reason="killed", source="memory cap"
    )
    assert "was stopped (memory cap)" in text


def _request(scope: Scope, command: str, env: dict[str, str]) -> SpawnRequest:
    return SpawnRequest(
        command=command,
        scope=scope,
        session_id="s1",
        env_key="k1",
        cwd=str(scope.home),
        env=env,
    )


async def _run(registry: ProcessRegistry, request: SpawnRequest) -> ProcessRecord:
    record = await registry.spawn(request)
    await asyncio.wait_for(record.done.wait(), 15)
    return record


@linux_only
def test_registry_runs_the_job_in_a_scope(tmp_path: Path, fake_bin: Path) -> None:
    scope = terminal_scope(tmp_path, {"terminal": {"background_memory_max": "256M"}})
    log = tmp_path / "calls.jsonl"
    env = _env(scope, fake_bin, log)
    registry = ProcessRegistry(clock=SystemClock(), checkpoints=False, receipts=None)
    record = asyncio.run(_run(registry, _request(scope, "printf 'hi\\n'", env)))
    assert record.exit_code == 0 and record.buffer == "hi\n"
    assert record.memory_cap == 256 * MIB
    calls = systemd_run.read_log(log)
    assert len(calls) == 2  # the probe, then the job
    assert f"MemoryMax={256 * MIB}" in calls[1]
    assert "-c" in calls[1][calls[1].index("--") :]


@linux_only
def test_oom_kill_is_reported_as_the_memory_cap(tmp_path: Path, fake_bin: Path) -> None:
    scope = terminal_scope(tmp_path, {})
    env = _env(scope, fake_bin, tmp_path / "calls.jsonl")
    registry = ProcessRegistry(clock=SystemClock(), checkpoints=False, receipts=None)
    # The cgroup OOM killer sends SIGKILL; the job does the same to itself here.
    record = asyncio.run(_run(registry, _request(scope, "kill -9 $$", env)))
    assert record.reason == "killed" and record.source == "memory cap"


@linux_only
def test_unscoped_sigkill_is_not_a_memory_kill(tmp_path: Path) -> None:
    scope = terminal_scope(tmp_path, {"terminal": {"background_memory_max": "off"}})
    registry = ProcessRegistry(clock=SystemClock(), checkpoints=False, receipts=None)
    record = asyncio.run(_run(registry, _request(scope, "kill -9 $$", child_env(scope))))
    assert record.memory_cap == 0
    assert record.reason == "exited" and record.source == ""


@linux_only
def test_our_kill_keeps_its_own_source(tmp_path: Path, fake_bin: Path) -> None:
    scope = terminal_scope(tmp_path, {"terminal": {"daemon_term_grace_s": 0.2}})
    env = _env(scope, fake_bin, tmp_path / "calls.jsonl")

    async def go() -> ProcessRecord:
        registry = ProcessRegistry(clock=SystemClock(), checkpoints=False, receipts=None)
        record = await registry.spawn(_request(scope, "trap '' TERM; sleep 30", env))
        assert record.memory_cap > 0
        await SystemClock().sleep(0.3)
        await registry.kill(record)
        await asyncio.wait_for(record.done.wait(), 15)
        return record

    record = asyncio.run(go())
    assert record.reason == "killed" and record.source == "process_manage kill"
