"""Modal direct mode: create, restore, snapshot, terminate (P21-05) over the fake Modal SDK."""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

from indus.backends.contract import BackendUnavailable
from indus.backends.modal import load_sdk
from indus.backends.modal.exec import ModalEnvironment, open_modal
from indus.backends.modal.lifecycle import requirement_reason, settings_from
from indus.foundation.config.schema import validate
from indus.foundation.scope import Scope
from tests.fakes.backend.fake_modal import FakeModal
from tests.fakes.backend.sdk_kit import Cloud
from tests.fakes.terminal import terminal_scope
from tests.unit.backends._aio import aio
from tests.unit.backends.sdk_rig import (
    RecordingSupport,
    new_cloud,
    record_of,
    records_for,
    scripts,
    seed_record,
)

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("timeout") is None, reason="needs bash, timeout"
)

IMAGE = "registry.test/indus-sandbox:test"


def tokens() -> dict[str, str]:
    return {
        "MODAL_TOKEN_ID": "id-" + os.urandom(6).hex(),
        "MODAL_TOKEN_SECRET": os.urandom(8).hex(),
    }


def modal_scope(
    tmp_path: Path,
    *,
    persistent: bool = True,
    secrets: dict[str, str] | None = None,
    **config: Any,
) -> Scope:
    terminal = {"container_persistent": persistent, "docker_image": IMAGE, **config}
    return terminal_scope(
        tmp_path, {"terminal": terminal}, secrets=tokens() if secrets is None else secrets
    )


def creates(cloud: Cloud) -> list[str]:
    """The image each ``Sandbox.create`` asked for (its tag or object id)."""
    out: list[str] = []
    for call in cloud.calls_of("Sandbox.create"):
        image = call.kwargs["image"]
        out.append(image.tag or image.object_id)
    return out


async def opened(
    tmp_path: Path, scope: Scope, cloud: Cloud | None = None
) -> tuple[ModalEnvironment, FakeModal, RecordingSupport]:
    rig = new_cloud(tmp_path)
    fake = FakeModal(cloud or rig.cloud)
    support = RecordingSupport()
    env = await open_modal(
        scope,
        support=support,
        records=records_for(scope, "modal"),
        sdk=fake.module,
        temp_dir=str(fake.cloud.root / "tmp"),
    )
    return env, fake, support


@aio
async def test_restore_path_is_used_when_a_record_exists(tmp_path: Path) -> None:
    scope = modal_scope(tmp_path)
    seed_record(scope, "modal", snapshot_id="im-snap-7")
    rig = new_cloud(tmp_path)
    (rig.cloud.home / "restored.txt").write_text("from the snapshot\n")
    rig.cloud.snapshots["im-snap-7"] = rig.cloud.fs.capture()
    env, fake, support = await opened(tmp_path, scope, rig.cloud)
    try:
        assert creates(fake.cloud) == ["im-snap-7"]
        create = fake.cloud.calls_of("Sandbox.create")[0]
        assert create.args == ("sleep", "infinity") and create.kwargs["timeout"] == 3600
        result = await env.execute("cat ~/restored.txt")
        assert result.output.strip() == "from the snapshot" and not result.environment_recreated
        assert ("create", 300.0) in support.ops()
    finally:
        await env.cleanup()


@aio
async def test_bad_snapshot_falls_back_to_the_base_image_and_flags_recreated(
    tmp_path: Path,
) -> None:
    scope = modal_scope(tmp_path)
    seed_record(scope, "modal", snapshot_id="im-gone")
    env, fake, _ = await opened(tmp_path, scope)
    try:
        assert creates(fake.cloud) == ["im-gone", IMAGE]
        assert record_of(scope, "modal") is None  # the stale record is gone
        first = await env.execute("echo one")
        second = await env.execute("echo two")
        assert first.environment_recreated and not second.environment_recreated
    finally:
        await env.cleanup()


@aio
async def test_persistent_cleanup_snapshots_then_terminates(tmp_path: Path) -> None:
    scope = modal_scope(tmp_path)
    env, fake, support = await opened(tmp_path, scope)
    await env.execute("echo hi > kept.txt")
    await env.cleanup()
    cloud = fake.cloud
    tail = [m for m in cloud.methods() if m in ("Sandbox.snapshot_filesystem", "Sandbox.terminate")]
    assert tail == ["Sandbox.snapshot_filesystem", "Sandbox.terminate"]
    assert cloud.calls_of("Sandbox.snapshot_filesystem")[0].kwargs["ttl"] is None  # kept
    saved = (record_of(scope, "modal") or {}).get("snapshot_id", "")
    assert saved.startswith("im-snap") and saved in cloud.snapshots
    assert ("snapshot", 60.0) in support.ops() and ("stop", 15.0) in support.ops()
    await env.cleanup()  # twice is fine
    assert cloud.methods().count("Sandbox.terminate") == 1


@aio
async def test_non_persistent_only_terminates_and_never_restores(tmp_path: Path) -> None:
    scope = modal_scope(tmp_path, persistent=False)
    seed_record(scope, "modal", snapshot_id="im-snap-1")
    env, fake, _ = await opened(tmp_path, scope)
    await env.cleanup()
    methods = fake.cloud.methods()
    assert "Sandbox.snapshot_filesystem" not in methods
    assert methods.count("Sandbox.terminate") == 1
    assert creates(fake.cloud) == [IMAGE]
    assert record_of(scope, "modal") == {"snapshot_id": "im-snap-1"}


@aio
async def test_snapshot_failure_still_terminates_and_keeps_the_record(tmp_path: Path) -> None:
    scope = modal_scope(tmp_path)
    env, fake, _ = await opened(tmp_path, scope)
    seed_record(scope, "modal", snapshot_id="im-old")
    fake.cloud.fail("Sandbox.snapshot_filesystem", 500)
    await env.cleanup()
    assert fake.cloud.methods()[-1] == "Sandbox.terminate"
    assert record_of(scope, "modal") == {"snapshot_id": "im-old"}


def test_managed_mode_is_refused_by_config_validation() -> None:
    warnings = validate({"terminal": {"modal_mode": "managed"}})
    assert len(warnings) == 1 and "D-16.16" in warnings[0]
    assert validate({"terminal": {"modal_mode": "direct"}}) == []
    assert validate({"terminal": {"modal_mode": "auto"}}) == []


@aio
async def test_managed_mode_is_refused_again_before_any_sdk_call(tmp_path: Path) -> None:
    fake = FakeModal(new_cloud(tmp_path).cloud)
    scope = modal_scope(tmp_path, modal_mode="managed")
    assert "D-16.16" in (requirement_reason(scope) or "")
    with pytest.raises(BackendUnavailable, match="D-16.16"):
        await open_modal(
            scope, support=RecordingSupport(), records=records_for(scope, "modal"), sdk=fake.module
        )
    assert fake.cloud.calls == []


@aio
async def test_missing_token_is_a_requirement_reason_not_a_crash(tmp_path: Path) -> None:
    scope = modal_scope(tmp_path, secrets={"MODAL_TOKEN_ID": "id-only"})
    reason = requirement_reason(scope) or ""
    assert "MODAL_TOKEN_ID" in reason and "MODAL_TOKEN_SECRET" in reason
    fake = FakeModal(new_cloud(tmp_path).cloud)
    with pytest.raises(BackendUnavailable):
        await open_modal(
            scope, support=RecordingSupport(), records=records_for(scope, "modal"), sdk=fake.module
        )
    assert fake.cloud.calls == []


@aio
async def test_token_values_never_reach_a_sandbox_command(tmp_path: Path) -> None:
    secrets = tokens()
    env, fake, _ = await opened(tmp_path, modal_scope(tmp_path, secrets=secrets))
    try:
        await env.execute("env")
        ran = "\n".join(scripts(fake.cloud))
        assert all(value not in ran for value in secrets.values())
        assert fake.cloud.calls_of("Client.from_credentials")
    finally:
        await env.cleanup()


def test_image_and_resources_come_from_the_container_settings(tmp_path: Path) -> None:
    scope = modal_scope(
        tmp_path, modal_image="im-base", container_cpu=2.0, container_memory_mb=8192
    )
    settings = settings_from(scope)
    assert (settings.image, settings.cpu, settings.memory_mb) == ("im-base", 2.0, 8192)
    assert settings_from(modal_scope(tmp_path)).image == IMAGE


def test_missing_extra_names_the_extra() -> None:
    if importlib.util.find_spec("modal") is not None:
        pytest.skip("the modal SDK is installed here")
    with pytest.raises(BackendUnavailable, match="`modal` extra"):
        load_sdk()
    assert "modal" not in sys.modules
