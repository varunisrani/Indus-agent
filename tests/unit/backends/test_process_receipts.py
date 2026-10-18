"""Retained process receipts (P11-13)."""

from __future__ import annotations

import json
import shutil
import stat
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from indus.backends import process_receipts
from indus.backends.process_notify import Deliveries, ProcessEvent
from indus.backends.processes import FINISHED_TTL_S, ProcessRecord, ProcessRegistry, SpawnRequest
from indus.foundation.scope import Scope
from tests.fakes.clock import FakeClock
from tests.fakes.terminal import FakeJob, forge_token, terminal_scope
from tests.unit.backends._aio import aio
from tests.unit.tools.builtin._terminal_rig import Rig


@pytest.fixture(autouse=True)
def no_resolver() -> Iterator[None]:
    process_receipts.set_lineage_resolver(None)
    yield
    process_receipts.set_lineage_resolver(None)


def _registry(clock: FakeClock, deliveries: Deliveries | None = None) -> ProcessRegistry:
    return ProcessRegistry(
        deliveries, clock=clock, launcher=lambda spec: FakeJob(), watch_exits=False
    )


def _request(scope: Scope, command: str = "make", **kw: Any) -> SpawnRequest:
    values: dict[str, Any] = {
        "command": command,
        "scope": scope,
        "session_id": "s1",
        "env_key": "k",
        "cwd": str(scope.home),
        "env": {},
    }
    values.update(kw)
    return SpawnRequest(**values)


def _finish(registry: ProcessRegistry, record: ProcessRecord, code: int = 0) -> None:
    assert isinstance(record.handle, FakeJob)
    record.handle.code = code
    registry.notice_exit(record)


def _receipt(scope: Scope, record_id: str) -> Path:
    return process_receipts.receipt_dir(scope.home) / f"{record_id}.json"


@aio
async def test_receipt_exists_when_the_completion_handler_runs(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 10, 18, tzinfo=UTC))
    scope = terminal_scope(tmp_path)
    seen: list[bool] = []
    deliveries = Deliveries()

    def sink(event: ProcessEvent) -> None:
        seen.append(_receipt(scope, event.process_id).is_file())

    deliveries.register(str(scope.home), "s1", sink)
    registry = _registry(clock, deliveries)
    record = await registry.spawn(_request(scope, notify_on_exit=True))
    registry.ingest(record, "built 3 targets\n")
    clock.advance(42)
    _finish(registry, record, 2)
    assert seen == [True]
    path = _receipt(scope, record.id)
    body = json.loads(path.read_text())
    assert body["id"] == record.id and body["exit_code"] == 2
    assert body["completion_reason"] == "exited" and body["owner_session"] == "s1"
    assert body["tail"] == "built 3 targets\n" and body["exited_at"].startswith("2026-10-18")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    # Every exit keeps a receipt, also without notify-on-exit.
    quiet = await registry.spawn(_request(scope, "ls"))
    _finish(registry, quiet)
    assert _receipt(scope, quiet.id).is_file()
    await registry.shutdown()


@aio
async def test_newest_64_are_kept(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 10, 18, tzinfo=UTC))
    scope = terminal_scope(tmp_path)
    registry = _registry(clock)
    ids: list[str] = []
    for _ in range(process_receipts.MAX_KEPT + 1):
        record = await registry.spawn(_request(scope))
        clock.advance(1)
        _finish(registry, record)
        ids.append(record.id)
    kept = {p.stem for p in process_receipts.receipt_dir(scope.home).glob("proc_*.json")}
    assert len(kept) == process_receipts.MAX_KEPT
    assert ids[0] not in kept and ids[-1] in kept
    await registry.shutdown()


@aio
async def test_receipts_older_than_seven_days_are_pruned(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 10, 18, tzinfo=UTC))
    scope = terminal_scope(tmp_path)
    registry = _registry(clock)
    old = await registry.spawn(_request(scope))
    _finish(registry, old)
    clock.advance(timedelta(days=8).total_seconds())
    new = await registry.spawn(_request(scope))
    _finish(registry, new)
    assert not _receipt(scope, old.id).exists() and _receipt(scope, new.id).exists()
    await registry.shutdown()


@aio
async def test_secret_redacted_even_with_live_redaction_off(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 10, 18, tzinfo=UTC))
    scope = terminal_scope(tmp_path, {"security": {"redact_secrets": False}})
    token = forge_token()
    registry = _registry(clock)
    record = await registry.spawn(_request(scope, f"deploy --token {token}"))
    registry.ingest(record, f"using {token}\n")
    _finish(registry, record)
    text = _receipt(scope, record.id).read_text()
    assert token not in text
    assert "deploy --token" in text
    await registry.shutdown()


def _rig_with_old_job(tmp_path: Path) -> tuple[Rig, str]:
    rig = Rig(tmp_path)
    body = rig.run(rig.call("s1", command="make all", background=True))
    record = rig.shared.processes.get(body["session_id"], str(rig.scope.home))
    assert record is not None
    rig.shared.processes.ingest(record, "all built\n")
    _finish(rig.shared.processes, record, 0)
    rig.clock.advance(FINISHED_TTL_S + 60)  # gone from memory
    assert rig.shared.processes.get(record.id, str(rig.scope.home)) is None
    return rig, record.id


def test_owner_reads_the_receipt_others_do_not(tmp_path: Path) -> None:
    rig, ident = _rig_with_old_job(tmp_path)
    mine = rig.run(rig.manage_call("s1", action="poll", session_id=ident))
    assert mine["status"] == "exited" and mine["exit_code"] == 0
    assert mine["output_preview"] == "all built\n" and "kept result" in mine["note"]
    log = rig.run(rig.manage_call("s1", action="log", session_id=ident[:9]))
    assert log["output"] == "all built" and log["total_lines"] == 1
    killed = rig.run(rig.manage_call("s1", action="kill", session_id=ident))
    assert killed["status"] == "already_exited"
    wrote = rig.run(rig.manage_call("s1", action="write", session_id=ident, data="x"))
    assert wrote["status"] == "already_exited"
    theirs = rig.run(rig.manage_call("s2", action="poll", session_id=ident))
    assert theirs["status"] == "not_found"
    listed = rig.run(rig.manage_call("s1", action="list"))["processes"]
    assert [(p["session_id"], p["retained"], p["exit_code"]) for p in listed] == [(ident, True, 0)]
    assert rig.run(rig.manage_call("s2", action="list"))["processes"] == []
    nobody = rig.run(rig.manage_call("", action="poll", session_id=ident))
    assert nobody["status"] == "not_found"


def test_compression_continuation_may_read(tmp_path: Path) -> None:
    rig, ident = _rig_with_old_job(tmp_path)
    process_receipts.set_lineage_resolver(lambda owner, caller: (owner, caller) == ("s1", "s1-b"))
    assert rig.run(rig.manage_call("s1-b", action="poll", session_id=ident))["exit_code"] == 0
    assert rig.run(rig.manage_call("s3", action="poll", session_id=ident))["status"] == "not_found"


def test_bad_ids_never_glob(tmp_path: Path) -> None:
    rig, ident = _rig_with_old_job(tmp_path)
    for query in ("*", "proc_*", "../" + ident, ident[:7]):
        assert process_receipts.find(rig.scope.home, query, "s1") is None
    assert process_receipts.find(rig.scope.home, ident, "s1") is not None


@aio
async def test_no_receipt_for_a_deleted_profile(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 10, 18, tzinfo=UTC))
    root = tmp_path / "root"
    home = root / "profiles" / "work"
    home.mkdir(parents=True)
    scope = terminal_scope(home.parent, name="work")
    registry = _registry(clock)
    record = await registry.spawn(_request(scope))
    shutil.rmtree(home)  # the profile was deleted while the job ran
    _finish(registry, record)
    assert not home.exists()
    await registry.shutdown()


@aio
async def test_receipts_are_per_profile(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 10, 18, tzinfo=UTC))
    first = terminal_scope(tmp_path, name="a")
    second = terminal_scope(tmp_path, name="b")
    registry = _registry(clock)
    record = await registry.spawn(_request(first))
    _finish(registry, record)
    assert process_receipts.find(first.home, record.id, "s1") is not None
    assert process_receipts.find(second.home, record.id, "s1") is None  # R02.8
    assert process_receipts.owned_by(second.home, "s1") == []
    await registry.shutdown()
