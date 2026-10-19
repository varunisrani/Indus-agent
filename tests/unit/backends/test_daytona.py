"""The Daytona backend (P21-07) over the fake Daytona SDK."""

from __future__ import annotations

import logging
import os
import shutil
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from indus.backends.contract import BackendUnavailable, EnvironmentConnectionError
from indus.backends.daytona import (
    DaytonaEnvironment,
    open_daytona,
    requirement_reason,
    resources_for,
    sandbox_name,
)
from indus.foundation.scope import Scope
from tests.fakes.backend.fake_daytona import FakeDaytona, SandboxState
from tests.fakes.terminal import terminal_scope
from tests.unit.backends._aio import aio
from tests.unit.backends.sdk_rig import RecordingSupport, Rig, kwargs_of, new_cloud

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("timeout") is None, reason="needs bash, timeout"
)

IMAGE = "registry.test/indus-sandbox:test"
NAME = "indus-default"


def api_key() -> dict[str, str]:
    return {"DAYTONA_API_KEY": "dk-" + os.urandom(8).hex()}


def daytona_scope(tmp_path: Path, *, persistent: bool = True, **config: Any) -> Scope:
    terminal = {"container_persistent": persistent, "docker_image": IMAGE, **config}
    return terminal_scope(tmp_path, {"terminal": terminal}, secrets=api_key())


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    made = new_cloud(tmp_path)
    yield made
    made.cloud.release()


async def open_env(
    scope: Scope, rig: Rig, fake: FakeDaytona | None = None
) -> tuple[DaytonaEnvironment, FakeDaytona, RecordingSupport]:
    fake = fake or FakeDaytona(rig.cloud)
    support = RecordingSupport()
    env = await open_daytona(scope, support=support, sdk=fake.module, temp_dir=str(rig.tmp))
    return env, fake, support


def methods(fake: FakeDaytona, prefix: str = "") -> list[str]:
    """The SDK methods called, in order (only those starting with ``prefix``)."""
    return [m for m in fake.cloud.methods() if m.startswith(prefix)]


def staged_files(rig: Rig) -> list[str]:
    if not rig.tmp.is_dir():
        return []
    return [p.name for p in rig.tmp.iterdir() if p.name.startswith(".indus-stdin-")]


@aio
async def test_existing_stopped_sandbox_is_started_not_recreated(tmp_path: Path, rig: Rig) -> None:
    fake = FakeDaytona(rig.cloud)
    fake.add_stopped(NAME)
    env, fake, _ = await open_env(daytona_scope(tmp_path), rig, fake)
    try:
        assert "Sandbox.start" in methods(fake) and "Daytona.create" not in methods(fake)
        result = await env.execute("echo resumed")
        assert result.output.strip() == "resumed"
    finally:
        await env.cleanup()


@aio
async def test_missing_sandbox_is_created_with_auto_stop_off(tmp_path: Path, rig: Rig) -> None:
    env, fake, support = await open_env(daytona_scope(tmp_path), rig)
    try:
        sandbox = fake.find(NAME)
        assert sandbox is not None
        assert sandbox.auto_stop_interval == 0 and sandbox.image == IMAGE
        assert (sandbox.cpu, sandbox.memory, sandbox.disk) == (1, 5, 10)
        assert ("create", 300.0) in support.ops()
    finally:
        await env.cleanup()


def test_resource_math() -> None:
    assert resources_for(1.0, 512, 51200).memory_gib == 1
    assert resources_for(1.0, 5120, 51200).memory_gib == 5
    assert resources_for(1.0, 5121, 1024).memory_gib == 6
    clamped = resources_for(1.0, 5120, 51200)
    assert (clamped.disk_gib, clamped.disk_clamped) == (10, True)
    small = resources_for(2.7, 100, 100)
    assert (small.cpu, small.memory_gib, small.disk_gib, small.disk_clamped) == (2, 1, 1, False)
    assert resources_for(0.5, 1024, 10240) == resources_for(1.0, 1024, 10240)


@aio
async def test_disk_clamp_warns_once(
    tmp_path: Path, rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="indus.backends.daytona")
    env, _, _ = await open_env(daytona_scope(tmp_path), rig)
    await env.cleanup()
    warnings = [r for r in caplog.records if "GiB" in r.getMessage()]
    assert len(warnings) == 1


@aio
async def test_persistent_cleanup_stops_and_keeps_the_sandbox(tmp_path: Path, rig: Rig) -> None:
    env, fake, _ = await open_env(daytona_scope(tmp_path), rig)
    await env.cleanup()
    assert "Sandbox.stop" in methods(fake) and "Daytona.delete" not in methods(fake)
    kept = fake.find(NAME)
    assert kept is not None and kept.state == SandboxState.STOPPED


@aio
async def test_non_persistent_cleanup_deletes_and_leftovers_are_not_reused(
    tmp_path: Path, rig: Rig
) -> None:
    fake = FakeDaytona(rig.cloud)
    fake.add_stopped(NAME).state = SandboxState.STARTED
    env, fake, _ = await open_env(daytona_scope(tmp_path, persistent=False), rig, fake)
    client_calls = methods(fake, "Daytona.")
    assert client_calls[:4] == [
        "Daytona.__init__",
        "Daytona.get",
        "Daytona.delete",
        "Daytona.create",
    ]
    await env.cleanup()
    assert methods(fake, "Daytona.")[-1] == "Daytona.delete" and fake.find(NAME) is None


@aio
async def test_home_detection_drives_the_cwd(tmp_path: Path, rig: Rig) -> None:
    home = str(rig.cloud.home)
    for cwd in ("~", "/home/daytona", "."):
        env, _, _ = await open_env(daytona_scope(tmp_path, cwd=cwd), rig)
        try:
            assert env.remote_home == home and env.cwd == home
        finally:
            await env.cleanup()


@aio
async def test_exec_keeps_the_shell_timeout_guard(tmp_path: Path, rig: Rig) -> None:
    env, fake, _ = await open_env(daytona_scope(tmp_path, timeout=40), rig)
    try:
        await env.execute("echo guarded")
        execs = fake.cloud.calls_of("Process.exec")
        guarded = [c for c in execs if "echo guarded" in str(c.args[0])]
        assert guarded and "timeout -k 2 42 bash -c" in str(guarded[-1].args[0])
        # the vendor-side bound sits just past the guard
        assert guarded[-1].kwargs["timeout"] == 44
    finally:
        await env.cleanup()


@aio
async def test_stdin_is_staged_and_removed_before_the_command(tmp_path: Path, rig: Rig) -> None:
    env, fake, _ = await open_env(daytona_scope(tmp_path), rig)
    try:
        q = str(rig.tmp)
        result = await env.execute(
            f"cat; echo; ls -A {q} | grep -c indus-stdin || true", stdin="hi\n"
        )
        lines = result.output.splitlines()
        assert lines[0] == "hi" and lines[-1].strip() == "0"
        uploads = [c for c in fake.cloud.calls_of("FileSystem.upload_file")]
        assert len([c for c in uploads if ".indus-stdin-" in str(c.args[0])]) == 1
        no_stdin = await env.execute("cat; echo done")
        assert no_stdin.output.strip() == "done"  # reads /dev/null, never hangs
    finally:
        await env.cleanup()


@aio
async def test_staged_stdin_is_scrubbed_when_the_command_never_ran(
    tmp_path: Path, rig: Rig
) -> None:
    env, fake, _ = await open_env(daytona_scope(tmp_path), rig)
    try:
        fake.cloud.fail("Process.exec", 503, message="exec endpoint unavailable")
        with pytest.raises(EnvironmentConnectionError, match="exec endpoint unavailable"):
            await env.execute("cat", stdin="sudo-password\n")
        assert len(fake.cloud.calls_of("FileSystem.upload_file")) >= 1
        waiter = threading.Event()
        for _ in range(250):
            if not staged_files(rig):
                break
            waiter.wait(0.02)
        assert not staged_files(rig)
    finally:
        await env.cleanup()


@aio
async def test_sandbox_stopped_elsewhere_is_started_before_the_next_command(
    tmp_path: Path, rig: Rig
) -> None:
    env, fake, _ = await open_env(daytona_scope(tmp_path), rig)
    try:
        sandbox = fake.find(NAME)
        assert sandbox is not None
        sandbox.stop()
        result = await env.execute("echo back")
        assert result.output.strip() == "back"
        assert methods(fake).count("Sandbox.start") == 1
    finally:
        await env.cleanup()


@aio
async def test_transport_round_trip_and_sync_back(tmp_path: Path, rig: Rig) -> None:
    scope = daytona_scope(tmp_path)
    skill = scope.home / "skills" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("v1\n")
    env, fake, _ = await open_env(scope, rig)
    remote = Path(env.remote_base, "skills", "demo", "SKILL.md")
    assert remote.read_text() == "v1\n"  # pushed at open
    await env.execute(f"echo v2 > {remote}; echo new > {remote.parent / 'NOTES.md'}")
    await env.cleanup()
    assert skill.read_text() == "v2\n"
    assert (skill.parent / "NOTES.md").read_text() == "new\n"
    names = methods(fake)
    downloads = [i for i, n in enumerate(names) if n == "FileSystem.download_file"]
    assert downloads and downloads[-1] < names.index("Sandbox.stop")  # sync back, then stop
    assert all(kw["local"] for kw in kwargs_of(fake.cloud, "FileSystem.download_file"))


@aio
async def test_missing_key_is_a_requirement_reason(tmp_path: Path, rig: Rig) -> None:
    scope = terminal_scope(tmp_path, {"terminal": {}}, secrets={})
    assert "DAYTONA_API_KEY" in (requirement_reason(scope) or "")
    fake = FakeDaytona(rig.cloud)
    with pytest.raises(BackendUnavailable):
        await open_daytona(scope, support=RecordingSupport(), sdk=fake.module)
    assert fake.cloud.calls == [] and fake.clients == []


@aio
async def test_forwarded_variables_reach_the_sandbox_through_the_sdk_env_only(
    tmp_path: Path, rig: Rig
) -> None:
    value = "pv-" + os.urandom(6).hex()
    terminal = {"docker_image": IMAGE, "env_passthrough": ["PROJ_T_PASS", "PROJ_T_ABSENT"]}
    scope = terminal_scope(
        tmp_path, {"terminal": terminal}, secrets={**api_key(), "PROJ_T_PASS": value}
    )
    env, fake, _ = await open_env(scope, rig)
    try:
        result = await env.execute('echo "[$PROJ_T_PASS][${PROJ_T_ABSENT-unset}]"')
        assert result.output.strip() == f"[{value}][unset]"
        execs = fake.cloud.calls_of("Process.exec")
        assert all(value not in str(c.args[0]) for c in execs)  # never in the command text
        assert execs[-1].kwargs["env"] == {"PROJ_T_PASS": value}
    finally:
        await env.cleanup()


def test_sandbox_names_are_safe() -> None:
    assert sandbox_name("default") == NAME
    assert sandbox_name("Task 7/x") == "indus-task-7-x"
