"""Modal direct mode: exec, interrupt, deadline, file transport (P21-06) over the fake SDK."""

from __future__ import annotations

import hashlib
import os
import shutil
import tarfile
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from indus.backends.contract import BackendError, EnvironmentConnectionError
from indus.backends.modal.exec import ModalEnvironment, ModalTransport, open_modal
from indus.backends.sdk_call import BackendDeadline
from indus.foundation.clock import Clock
from tests.fakes.backend.fake_modal import FakeModal
from tests.fakes.backend.sdk_kit import Cloud
from tests.fakes.clock import FakeClock
from tests.unit.backends._aio import aio
from tests.unit.backends.conftest import drive
from tests.unit.backends.modal.test_lifecycle import modal_scope
from tests.unit.backends.sdk_rig import (
    RecordingSupport,
    Rig,
    kwargs_of,
    new_cloud,
    records_for,
    scripts,
)

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("timeout") is None, reason="needs bash, timeout"
)


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    made = new_cloud(tmp_path)
    yield made
    made.cloud.release()


async def open_env(
    tmp_path: Path, rig: Rig, clock: Clock | None = None, **config: Any
) -> tuple[ModalEnvironment, FakeModal, RecordingSupport]:
    fake = FakeModal(rig.cloud)
    support = RecordingSupport(clock)
    scope = modal_scope(tmp_path, persistent=False, **config)
    env = await open_modal(
        scope,
        support=support,
        records=records_for(scope, "modal"),
        sdk=fake.module,
        clock=clock,
        temp_dir=str(rig.tmp),
    )
    return env, fake, support


def stdin_writes(cloud: Cloud) -> list[int]:
    return [int(call.args[0]) for call in cloud.calls_of("StreamWriter.write")]


@aio
async def test_commands_keep_cwd_and_exports(tmp_path: Path, rig: Rig) -> None:
    env, fake, _ = await open_env(tmp_path, rig)
    home = rig.cloud.home
    try:
        assert env.cwd == str(home)  # "~" follows the detected home
        first = await env.execute("mkdir -p sub && cd sub && export GREETING=hello")
        assert first.returncode == 0 and first.cwd_observed == str(home / "sub")
        second = await env.execute('echo "$GREETING from $(pwd)"; echo oops >&2; exit 3')
        assert second.returncode == 3
        assert f"hello from {home / 'sub'}" in second.output and "oops" in second.output
        assert env.cwd == str(home / "sub")
        guarded = [s for s in scripts(fake.cloud) if s.startswith("timeout -k 2 182 ")]
        assert guarded, "every command runs under the shell timeout guard"
        timeouts = [kw["timeout"] for kw in kwargs_of(fake.cloud, "Sandbox.exec")]
        assert 184 in timeouts  # the vendor's own bound sits just past the guard
    finally:
        await env.cleanup()


@aio
async def test_interrupt_signals_the_command_and_returns_130(tmp_path: Path, rig: Rig) -> None:
    env, fake, _ = await open_env(tmp_path, rig)
    marker = rig.cloud.root / "still-running"
    try:
        cancel = threading.Event()
        threading.Timer(0.5, cancel.set).start()
        result = await env.execute(f"sleep 30; touch {marker}", cancel=cancel, timeout=60)
        assert result.returncode == 130 and result.interrupted
        assert any("kill -TERM" in s for s in scripts(fake.cloud))
        assert not marker.exists()
    finally:
        await env.cleanup()


@aio
async def test_never_returning_exec_gives_124_at_the_deadline(tmp_path: Path, rig: Rig) -> None:
    clock = FakeClock(datetime(2026, 10, 19, tzinfo=UTC))
    env, fake, support = await drive(clock, open_env(tmp_path, rig, clock), step=0.01)
    try:
        rig.cloud.hang_command("HANG-FOREVER")
        start = clock.monotonic()
        result = await drive(clock, env.execute("echo HANG-FOREVER", timeout=5))
        assert result.returncode == 124 and result.timed_out
        assert 5 <= clock.monotonic() - start < 8
        assert any("kill -KILL" in s for s in scripts(fake.cloud))
        exec_calls = [c for c in support.calls if c.op == "exec" and c.timeout == 15.0]
        assert exec_calls and not exec_calls[-1].idempotent  # timeout + 10 s, never retried
    finally:
        rig.cloud.release()
        await drive(clock, env.cleanup())


@aio
async def test_stdin_is_streamed_in_64k_chunks(tmp_path: Path, rig: Rig) -> None:
    env, fake, _ = await open_env(tmp_path, rig)
    try:
        payload = "x" * (200 * 1024)
        result = await env.execute("wc -c", stdin=payload)
        assert result.output.strip() == str(len(payload))
        assert stdin_writes(fake.cloud)[-4:] == [65536, 65536, 65536, 8192]
        broken = await env.execute("echo ran; wc -c", stdin="bad \ud800 text")
        assert broken.stdin_error and "ran" in broken.output
    finally:
        await env.cleanup()


@aio
async def test_vendor_failure_is_a_connection_error(tmp_path: Path, rig: Rig) -> None:
    env, fake, support = await open_env(tmp_path, rig)
    try:
        fake.cloud.fail("Sandbox.exec", 503, message="upstream gone")
        before = len(fake.cloud.calls_of("Sandbox.exec"))
        with pytest.raises(EnvironmentConnectionError, match="upstream gone"):
            await env.execute("echo hi")
        assert len(fake.cloud.calls_of("Sandbox.exec")) == before + 1  # an exec is never retried
        assert support.retry_sleeps == []
    finally:
        await env.cleanup()


@aio
async def test_transport_round_trip_and_limits(tmp_path: Path, rig: Rig) -> None:
    env, _, support = await open_env(tmp_path, rig)
    transport = env.transport()
    assert isinstance(transport, ModalTransport)
    try:
        local = tmp_path / "one.bin"
        local.write_bytes(os.urandom(100_000))
        dest = f"{env.remote_base}/cache/documents/one.bin"
        await transport.upload_one(str(local), dest)
        assert Path(dest).read_bytes() == local.read_bytes()

        tar_path = tmp_path / "many.tar"
        with tarfile.open(tar_path, "w") as archive:
            for name in ("skills/a.md", "skills/b.md"):
                src = tmp_path / name.replace("/", "_")
                src.write_text(name)
                archive.add(src, arcname=name)
        await transport.upload_bulk(str(tar_path))
        assert Path(env.remote_base, "skills", "b.md").read_text() == "skills/b.md"

        await transport.delete([dest])
        assert not Path(dest).exists()
        ops = support.ops()
        assert ("file", 30.0) in ops and ("bulk", 120.0) in ops and ("delete", 15.0) in ops
    finally:
        await env.cleanup()


@aio
async def test_twenty_mib_download_is_not_truncated(tmp_path: Path, rig: Rig) -> None:
    env, _, _ = await open_env(tmp_path, rig)
    try:
        big = Path(env.remote_base, "cache", "documents", "big.bin")
        big.parent.mkdir(parents=True, exist_ok=True)
        data = os.urandom(20 * 1024 * 1024)
        big.write_bytes(data)
        dest = tmp_path / "down.tar"
        await env.transport().download_bulk(env.remote_base, str(dest))
        with tarfile.open(dest) as archive:
            member = next(m for m in archive.getmembers() if m.name.endswith("big.bin"))
            got = archive.extractfile(member)
            assert got is not None
            assert hashlib.sha256(got.read()).digest() == hashlib.sha256(data).digest()
    finally:
        await env.cleanup()


@aio
async def test_upload_byte_count_mismatch_is_an_error(tmp_path: Path, rig: Rig) -> None:
    env, _, _ = await open_env(tmp_path, rig)
    try:
        local = tmp_path / "one.bin"
        local.write_bytes(b"0123456789")
        dest = Path(env.remote_base, "cache", "documents", "one.bin")
        rig.cloud.script("*base64 -d*", stdout="6\n")  # only 6 bytes arrived
        with pytest.raises(BackendError, match="copied 6 of 10 bytes"):
            await env.transport().upload_one(str(local), str(dest))
    finally:
        await env.cleanup()


@aio
async def test_hung_bulk_upload_gives_up_at_120s(tmp_path: Path, rig: Rig) -> None:
    clock = FakeClock(datetime(2026, 10, 19, tzinfo=UTC))
    env, _, support = await drive(clock, open_env(tmp_path, rig, clock), step=0.01)
    try:
        rig.cloud.hang_command("tar -xf -")
        tar_path = tmp_path / "x.tar"
        with tarfile.open(tar_path, "w"):
            pass
        start = clock.monotonic()
        with pytest.raises(EnvironmentConnectionError):
            await drive(clock, env.transport().upload_bulk(str(tar_path)), step=1.0)
        assert 120 <= clock.monotonic() - start < 125
        assert support.deadlines >= 1
    finally:
        rig.cloud.release()
        await drive(clock, env.cleanup())


def test_a_missed_deadline_is_a_backend_error_with_its_code() -> None:
    missed = BackendDeadline("exec", 15.0)
    assert isinstance(missed, BackendError) and missed.code == "backend.deadline"
