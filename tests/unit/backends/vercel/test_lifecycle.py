"""Vercel Sandbox: auth, disk refusal, create retries, wait, snapshot, stop (P21-08)."""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from indus.backends.contract import BackendUnavailable, EnvironmentConnectionError
from indus.backends.vercel.exec import VercelEnvironment, open_vercel
from indus.backends.vercel.lifecycle import (
    VercelAuth,
    VercelSandbox,
    auth_from,
    requirement_reason,
    settings_from,
)
from indus.foundation.clock import Clock
from indus.foundation.config.schema import validate
from indus.foundation.scope import Scope
from tests.fakes.backend.fake_vercel import FakeVercel, SandboxStatus
from tests.fakes.backend.sdk_kit import Cloud
from tests.fakes.clock import FakeClock
from tests.fakes.terminal import terminal_scope
from tests.unit.backends._aio import aio
from tests.unit.backends.conftest import drive
from tests.unit.backends.sdk_rig import (
    RecordingSupport,
    Rig,
    kwargs_of,
    new_cloud,
    record_of,
    records_for,
    scripts,
    seed_record,
)

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("timeout") is None, reason="needs bash, timeout"
)


def trio() -> dict[str, str]:
    return {
        "VERCEL_TOKEN": "vt" + os.urandom(8).hex(),
        "VERCEL_PROJECT_ID": "prj_" + os.urandom(4).hex(),
        "VERCEL_TEAM_ID": "team_" + os.urandom(4).hex(),
    }


def vercel_scope(
    tmp_path: Path,
    *,
    persistent: bool = True,
    secrets: dict[str, str] | None = None,
    **config: Any,
) -> Scope:
    terminal = {"container_persistent": persistent, **config}
    return terminal_scope(
        tmp_path, {"terminal": terminal}, secrets=trio() if secrets is None else secrets
    )


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    made = new_cloud(tmp_path)
    yield made
    made.cloud.release()


def new_fake(rig: Rig) -> FakeVercel:
    """The fake SDK with its workspace inside the test's cloud dir."""
    return FakeVercel(rig.cloud, workspace=str(rig.work))


async def open_env(
    scope: Scope,
    rig: Rig,
    *,
    fake: FakeVercel | None = None,
    clock: Clock | None = None,
) -> tuple[VercelEnvironment, FakeVercel, RecordingSupport]:
    fake = fake or new_fake(rig)
    support = RecordingSupport(clock)
    env = await open_vercel(
        scope,
        support=support,
        records=records_for(scope, "vercel"),
        sdk=fake.sync_module,
        clock=clock,
        temp_dir=str(rig.tmp),
    )
    return env, fake, support


def creates(cloud: Cloud) -> list[str | None]:
    """What each ``create_sandbox`` started from: a snapshot id or the image (None: default)."""
    out: list[str | None] = []
    for kwargs in kwargs_of(cloud, "SandboxClient.create_sandbox"):
        source = kwargs.get("source")
        out.append(source.snapshot_id if source is not None else kwargs.get("image"))
    return out


def methods(cloud: Cloud) -> list[str]:
    return cloud.methods()


@aio
async def test_bad_disk_is_refused_with_zero_api_calls(tmp_path: Path, rig: Rig) -> None:
    scope = vercel_scope(tmp_path, container_disk_mb=10240)
    assert "51200" in (requirement_reason(scope) or "")
    fake = new_fake(rig)
    with pytest.raises(BackendUnavailable, match="container_disk_mb"):
        await open_vercel(
            scope,
            support=RecordingSupport(),
            records=records_for(scope, "vercel"),
            sdk=fake.sync_module,
        )
    assert rig.cloud.calls == []
    for ok in (0, 51200):
        assert requirement_reason(vercel_scope(tmp_path, container_disk_mb=ok)) is None


@aio
async def test_429_then_success_on_create(tmp_path: Path, rig: Rig) -> None:
    rig.cloud.fail("SandboxClient.create_sandbox", 429, message="slow down")
    env, fake, support = await open_env(vercel_scope(tmp_path), rig)
    try:
        made = kwargs_of(fake.cloud, "SandboxClient.create_sandbox")
        assert len(made) == 2 and support.retry_sleeps == [0.5]
        create = next(c for c in support.calls if c.op == "create")
        assert create.idempotent and create.timeout == 300.0
        assert made[-1]["execution_time_limit"] == timedelta(seconds=300)
        assert made[-1]["image"] is None  # the vendor's default image
    finally:
        await env.cleanup()


@aio
async def test_400_on_create_is_not_retried(tmp_path: Path, rig: Rig) -> None:
    rig.cloud.fail("SandboxClient.create_sandbox", 400, message="bad request")
    with pytest.raises(EnvironmentConnectionError):
        await open_env(vercel_scope(tmp_path), rig)
    assert len(rig.cloud.calls_of("SandboxClient.create_sandbox")) == 1


@aio
async def test_wait_for_running_gives_up_at_30s(tmp_path: Path, rig: Rig) -> None:
    clock = FakeClock(datetime(2026, 10, 19, tzinfo=UTC))
    fake = new_fake(rig)
    fake.start_status = SandboxStatus.PENDING
    scope = vercel_scope(tmp_path)
    settings = settings_from(scope)
    auth = auth_from(scope)
    assert isinstance(auth, VercelAuth)
    lifecycle = VercelSandbox(
        fake.sync_module,
        RecordingSupport(clock),
        records_for(scope, "vercel"),
        settings,
        auth,
        clock,
    )
    start = clock.monotonic()
    with pytest.raises(EnvironmentConnectionError, match="within 30s"):
        await drive(clock, lifecycle.open(), step=0.05)
    assert 30 <= clock.monotonic() - start < 32
    polls = len(rig.cloud.calls_of("SandboxClient.get_sandbox"))
    assert 40 <= polls <= 61  # every 0.5 s (a slow worker thread may let the clock run ahead)


@aio
async def test_terminal_state_while_starting_fails_at_once(tmp_path: Path, rig: Rig) -> None:
    fake = new_fake(rig)
    fake.start_status = SandboxStatus.PENDING
    fake.status_script = ["failed"]
    with pytest.raises(EnvironmentConnectionError):
        await open_env(vercel_scope(tmp_path), rig, fake=fake)
    assert len(rig.cloud.calls_of("SandboxClient.get_sandbox")) == 1


@aio
async def test_persistent_cleanup_order_is_sync_back_snapshot_stop(
    tmp_path: Path, rig: Rig
) -> None:
    scope = vercel_scope(tmp_path)
    skill = scope.home / "skills" / "x" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("v1\n")
    env, fake, _ = await open_env(scope, rig)
    remote = Path(env.remote_base, "skills", "x", "SKILL.md")
    await env.execute(f"echo v2 > {remote}")
    await env.cleanup()
    assert skill.read_text() == "v2\n"
    names = methods(fake.cloud)
    last_read = max(i for i, n in enumerate(names) if n == "Filesystem.read_bytes")
    snapshot = names.index("Sandbox.snapshot")
    assert last_read < snapshot < names.index("Sandbox.stop") < names.index("SandboxClient.close")
    saved = (record_of(scope, "vercel") or {}).get("snapshot_id", "")
    assert saved.startswith("snap_") and saved in fake.cloud.snapshots


@aio
async def test_restore_from_record_and_fallback(tmp_path: Path, rig: Rig) -> None:
    persistent = vercel_scope(tmp_path)
    seed_record(persistent, "vercel", snapshot_id="snap-77")
    (rig.work / "kept.txt").parent.mkdir(parents=True, exist_ok=True)
    (rig.work / "kept.txt").write_text("restored\n")
    rig.cloud.snapshots["snap-77"] = rig.cloud.fs.capture()
    env, fake, _ = await open_env(persistent, rig)
    try:
        assert creates(fake.cloud) == ["snap-77"]
        result = await env.execute(f"cat {rig.work / 'kept.txt'}")
        assert result.output.strip() == "restored" and not result.environment_recreated
    finally:
        await env.cleanup()

    other = tmp_path / "other"
    other.mkdir()
    fleeting = vercel_scope(other, persistent=False)
    seed_record(fleeting, "vercel", snapshot_id="snap-gone")
    fake = new_fake(rig)
    env, fake, _ = await open_env(fleeting, rig, fake=fake)
    try:
        assert creates(fake.cloud)[-1] is None  # not persistent: no restore
        assert creates(fake.cloud).count("snap-gone") == 0
    finally:
        await env.cleanup()

    seed_record(persistent, "vercel", snapshot_id="snap-gone")
    before = len(creates(rig.cloud))
    env, fake, _ = await open_env(persistent, rig, fake=new_fake(rig))
    try:
        assert creates(fake.cloud)[before:] == ["snap-gone", None]
        assert (await env.execute("true")).environment_recreated
    finally:
        await env.cleanup()
    assert (record_of(persistent, "vercel") or {}).get("snapshot_id") not in (None, "snap-gone")


def test_unknown_runtime_is_rejected_by_config_validation() -> None:
    assert validate({"terminal": {"vercel_runtime": "ruby3"}})
    assert validate({"terminal": {"vercel_runtime": "node24"}}) == []


def test_runtime_replaces_the_image(tmp_path: Path) -> None:
    assert settings_from(vercel_scope(tmp_path, vercel_runtime="python3.13")).image == "python3.13"
    assert settings_from(vercel_scope(tmp_path)).image is None


def test_auth_needs_the_whole_trio_or_an_oidc_token(tmp_path: Path) -> None:
    full = auth_from(vercel_scope(tmp_path))
    assert isinstance(full, VercelAuth) and not full.oidc
    partial = auth_from(vercel_scope(tmp_path, secrets={"VERCEL_TOKEN": "vt1"}))
    assert (
        isinstance(partial, str) and "VERCEL_PROJECT_ID" in partial and "VERCEL_TEAM_ID" in partial
    )
    oidc = auth_from(vercel_scope(tmp_path, secrets={"VERCEL_OIDC_TOKEN": "x.y.z"}))
    assert isinstance(oidc, VercelAuth) and oidc.oidc
    none = auth_from(vercel_scope(tmp_path, secrets={}))
    assert isinstance(none, str) and "VERCEL_OIDC_TOKEN" in none


@aio
async def test_credentials_go_through_the_sdk_factory_only(tmp_path: Path, rig: Rig) -> None:
    secrets = trio()
    env, fake, _ = await open_env(vercel_scope(tmp_path, secrets=secrets), rig)
    try:
        options = kwargs_of(fake.cloud, "SandboxClient.create")[0]["options"]
        made = options.credentials_factory()
        assert made.token == secrets["VERCEL_TOKEN"]
        assert made.project_id == secrets["VERCEL_PROJECT_ID"]
        await env.execute("env")
        assert all(v not in "\n".join(scripts(fake.cloud)) for v in secrets.values())
    finally:
        await env.cleanup()
