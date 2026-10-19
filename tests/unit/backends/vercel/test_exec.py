"""Vercel Sandbox: exec, write retries, transport, replaced sandboxes (P21-09)."""

from __future__ import annotations

import hashlib
import os
import shutil
import tarfile
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from indus.backends.remote_processes import RemoteJob, Started, generation, start
from indus.backends.vercel.exec import VercelEnvironment
from tests.fakes.backend.fake_vercel import FakeVercel
from tests.fakes.clock import FakeClock
from tests.unit.backends._aio import aio
from tests.unit.backends.conftest import drive
from tests.unit.backends.sdk_rig import Rig
from tests.unit.backends.vercel.test_lifecycle import open_env, rig, vercel_scope

__all__ = ["rig"]

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("timeout") is None, reason="needs bash, timeout"
)


class RecordingClock(FakeClock):
    def __init__(self) -> None:
        super().__init__(datetime(2026, 10, 19, tzinfo=UTC))
        self.slept: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.slept.append(round(seconds, 3))
        await super().sleep(seconds)


@aio
async def test_commands_start_in_the_workspace_and_parse_the_marker(
    tmp_path: Path, rig: Rig
) -> None:
    env, _, _ = await open_env(vercel_scope(tmp_path, persistent=False), rig)
    work = rig.work
    try:
        assert env.workspace_root == str(work) and env.cwd == str(work)
        result = await env.execute("printf abc")  # no trailing newline before the marker
        assert result.output == "abc" and result.cwd_observed == str(work)
        moved = await env.execute("mkdir -p d && cd d")
        assert moved.cwd_observed == str(work / "d") and env.cwd == moved.cwd_observed
    finally:
        await env.cleanup()


@aio
async def test_write_fails_twice_then_succeeds_with_100ms_steps(tmp_path: Path, rig: Rig) -> None:
    clock = RecordingClock()
    env, fake, _ = await drive(
        clock, open_env(vercel_scope(tmp_path, persistent=False), rig, clock=clock), step=0.01
    )
    try:
        fake.cloud.fail("Filesystem.write_bytes", 503, times=2)
        clock.slept.clear()
        target = str(rig.tmp / "written.txt")
        await drive(clock, env.write_file(target, b"payload"), step=0.01)
        assert Path(target).read_bytes() == b"payload"
        backoff = [s for s in clock.slept if s != 30.0]  # 30 s: the wrapper's deadline timers
        assert backoff == [0.1, 0.2]
        writes = [c for c in fake.cloud.calls_of("Filesystem.write_bytes") if c.args[0] == target]
        assert len(writes) == 3
    finally:
        await drive(clock, env.cleanup(), step=0.01)


@aio
async def test_stdin_is_staged_then_removed(tmp_path: Path, rig: Rig) -> None:
    env, fake, _ = await open_env(vercel_scope(tmp_path, persistent=False), rig)
    try:
        result = await env.execute(
            f"cat; echo; ls -A {rig.tmp} | grep -c indus-stdin || true", stdin="in"
        )
        lines = result.output.splitlines()
        assert lines[0] == "in" and lines[-1].strip() == "0"
        writes = fake.cloud.calls_of("Filesystem.write_bytes")
        assert len([c for c in writes if ".indus-stdin-" in str(c.args[0])]) == 1
    finally:
        await env.cleanup()


@aio
async def test_interrupt_kills_the_vendor_process(tmp_path: Path, rig: Rig) -> None:
    env, _, _ = await open_env(vercel_scope(tmp_path, persistent=False), rig)
    try:
        cancel = threading.Event()
        threading.Timer(0.5, cancel.set).start()
        result = await env.execute("sleep 30", cancel=cancel, timeout=60)
        assert result.returncode == 130 and result.interrupted
        sandbox = env.lifecycle.sandbox
        assert any(9 in p.signals for p in sandbox.processes)  # the vendor's own kill too
    finally:
        await env.cleanup()


@aio
async def test_download_is_unbounded(tmp_path: Path, rig: Rig) -> None:
    env, _, _ = await open_env(vercel_scope(tmp_path, persistent=False), rig)
    try:
        big = Path(env.remote_base, "cache", "images", "big.bin")
        big.parent.mkdir(parents=True, exist_ok=True)
        data = os.urandom(20 * 1024 * 1024)
        big.write_bytes(data)
        dest = tmp_path / "down.tar"
        await env.transport().download_bulk(env.remote_base, str(dest))
        with tarfile.open(dest) as archive:
            member = next(m for m in archive.getmembers() if m.name.endswith("big.bin"))
            got = archive.extractfile(member)
            assert (
                got is not None
                and hashlib.sha256(got.read()).digest() == hashlib.sha256(data).digest()
            )
        assert not any(p.name.startswith("indus-sync-") for p in rig.tmp.iterdir())
    finally:
        await env.cleanup()


@aio
async def test_upload_one_and_bulk(tmp_path: Path, rig: Rig) -> None:
    env, _, support = await open_env(vercel_scope(tmp_path, persistent=False), rig)
    try:
        local = tmp_path / "f.txt"
        local.write_text("hello")
        dest = f"{env.remote_base}/cache/documents/f.txt"
        await env.transport().upload_one(str(local), dest)
        assert Path(dest).read_text() == "hello"
        tar_path = tmp_path / "t.tar"
        with tarfile.open(tar_path, "w") as archive:
            archive.add(local, arcname="skills/s/f.txt")
        await env.transport().upload_bulk(str(tar_path))
        assert Path(env.remote_base, "skills", "s", "f.txt").read_text() == "hello"
        await env.transport().delete([dest])
        assert not Path(dest).exists()
        assert ("bulk", 120.0) in support.ops() and ("delete", 15.0) in support.ops()
    finally:
        await env.cleanup()


@aio
async def test_replaced_sandbox_loses_background_jobs(tmp_path: Path, rig: Rig) -> None:
    env, fake, _ = await open_env(vercel_scope(tmp_path, persistent=False), rig)
    try:
        before = generation(env)
        started = await start(env, "proc_bg1", "sleep 5", cwd=None, temp_dir=str(rig.tmp))
        assert isinstance(started, Started), started
        job = RemoteJob(env, started.pid, started.files, generation=before)
        assert started.pid > 0 and not job.replaced
        first = env.lifecycle.sandbox.name
        fake.status_script = ["stopped"]  # the sandbox was stopped behind our back
        result = await env.execute("echo again")
        assert result.output.strip() == "again" and result.environment_recreated
        assert env.lifecycle.sandbox.name != first
        assert generation(env) == before + 1 and job.replaced
        assert not (await env.execute("true")).environment_recreated
    finally:
        await env.cleanup()


def test_environment_class_flags() -> None:
    assert VercelEnvironment.backend_name == "vercel_sandbox"
    assert FakeVercel  # the fake surface is importable without the SDK
