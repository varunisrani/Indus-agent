"""The per-kind sandbox records store (P21-01, R02.8)."""

from __future__ import annotations

import json
import logging
import multiprocessing
import os
import stat
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from indus.backends.sandbox_records import (
    LOCK_TIMEOUT_S,
    SandboxRecordBusy,
    SandboxRecords,
    mutate_records,
)
from indus.foundation.fsio import FileLock
from tests.fakes.clock import FakeClock

START = datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)
PUTS_PER_PROCESS = 200


def _store(home: Path, clock: FakeClock, profile: str = "work") -> SandboxRecords:
    return SandboxRecords(home, "modal", profile_id=profile, clock=clock)


def test_round_trip_and_partial_update(tmp_path: Path) -> None:
    clock = FakeClock(START)
    store = _store(tmp_path, clock)
    assert store.get("task-1") is None
    first = store.put("task-1", snapshot_id="im-123", sandbox_name="indus-task-1")
    assert first.snapshot_id == "im-123" and first.sandbox_name == "indus-task-1"
    assert first.created_at == first.updated_at == "2026-10-19T12:00:00+00:00"
    clock.advance(60)
    second = store.put("task-1", overlay_path="/scratch/overlay")
    assert second.snapshot_id == "im-123" and second.overlay_path == "/scratch/overlay"
    assert second.created_at == "2026-10-19T12:00:00+00:00"
    assert second.updated_at == "2026-10-19T12:01:00+00:00"
    cleared = store.put("task-1", snapshot_id=None)
    assert cleared.snapshot_id is None and cleared.sandbox_name == "indus-task-1"
    assert store.get("task-1") == cleared
    assert store.keys() == ["task-1"]
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    assert list(raw) == ["work:task-1"]
    assert store.path == tmp_path / "sandboxes" / "records" / "modal.json"


def test_delete_with_snapshot_guard(tmp_path: Path) -> None:
    store = _store(tmp_path, FakeClock(START))
    store.put("t", snapshot_id="im-new")
    assert store.delete("t", only_if_snapshot="im-old") is False
    assert store.get("t") is not None
    assert store.delete("t", only_if_snapshot="im-new") is True
    assert store.get("t") is None
    assert store.delete("t") is False


def test_get_does_not_write(tmp_path: Path) -> None:
    store = _store(tmp_path, FakeClock(START))
    assert store.get("missing") is None
    assert not store.path.exists()


def test_profile_isolation(tmp_path: Path) -> None:
    clock = FakeClock(START)
    alpha = _store(tmp_path, clock, profile="alpha")
    beta = _store(tmp_path, clock, profile="beta")
    alpha.put("shared-task", snapshot_id="im-alpha")
    assert beta.get("shared-task") is None
    assert beta.keys() == []
    beta.put("shared-task", snapshot_id="im-beta")
    alpha_record = alpha.get("shared-task")
    beta_record = beta.get("shared-task")
    assert alpha_record is not None and alpha_record.snapshot_id == "im-alpha"
    assert beta_record is not None and beta_record.snapshot_id == "im-beta"


def test_corrupt_file_is_quarantined(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    clock = FakeClock(START)
    store = _store(tmp_path, clock)
    store.path.parent.mkdir(parents=True)
    store.path.write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="indus.backends.sandbox_records"):
        assert store.get("t") is None
    aside = store.path.with_name("modal.json.corrupt-20261019T120000Z")
    assert aside.read_text(encoding="utf-8") == "{not json"
    assert not store.path.exists()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "moved to" in warnings[0].getMessage()
    store.put("t", snapshot_id="im-1")  # usable again
    assert json.loads(store.path.read_text(encoding="utf-8"))["work:t"]["snapshot_id"] == "im-1"


def test_non_object_file_is_quarantined(tmp_path: Path) -> None:
    store = _store(tmp_path, FakeClock(START))
    store.path.parent.mkdir(parents=True)
    store.path.write_text("[1, 2]", encoding="utf-8")
    assert store.keys() == []
    assert store.path.with_name("modal.json.corrupt-20261019T120000Z").exists()


def test_malformed_entry_reads_as_absent(tmp_path: Path) -> None:
    store = _store(tmp_path, FakeClock(START))
    store.path.parent.mkdir(parents=True)
    store.path.write_text(json.dumps({"work:t": "im-bare"}), encoding="utf-8")
    assert store.get("t") is None


def test_lock_timeout_raises_busy(tmp_path: Path) -> None:
    clock = FakeClock(START)
    store = SandboxRecords(
        tmp_path, "daytona", profile_id="work", clock=clock, wait=lambda s: clock.advance(s)
    )
    store.path.parent.mkdir(parents=True)
    held = threading.Event()
    done = threading.Event()

    def hold() -> None:
        with FileLock(store.path.with_name("daytona.json.lock")):
            held.set()
            done.wait(10)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert held.wait(5)
    try:
        started = clock.monotonic()
        with pytest.raises(SandboxRecordBusy) as caught:
            store.put("t", snapshot_id="im-1")
        assert clock.monotonic() - started >= LOCK_TIMEOUT_S
        assert caught.value.code == "backend.sandbox_records_busy"
    finally:
        done.set()
        holder.join(5)
    assert store.put("t", snapshot_id="im-1").snapshot_id == "im-1"


def test_bad_kind_and_profile_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        SandboxRecords(tmp_path, "../x", profile_id="work")
    with pytest.raises(ValueError):
        SandboxRecords(tmp_path, "modal", profile_id="")


def test_for_scope_uses_profile_and_home(scope: object) -> None:
    from indus.foundation.scope import Scope

    assert isinstance(scope, Scope)
    store = SandboxRecords.for_scope(scope, "vercel_sandbox")
    assert store.path == scope.home / "sandboxes" / "records" / "vercel_sandbox.json"
    store.put("task", sandbox_name="indus-task")
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    assert list(raw) == [f"{scope.profile_id}:task"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_file_and_dir_modes(tmp_path: Path) -> None:
    store = _store(tmp_path, FakeClock(START))
    store.put("t", snapshot_id="im-1")
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700


def test_mutation_result_returned_and_unchanged_not_written(tmp_path: Path) -> None:
    path = tmp_path / "records" / "x.json"
    assert mutate_records(path, lambda data: len(data)) == 0
    assert not path.exists()
    mutate_records(path, lambda data: data.update({"a": 1}))
    first = path.stat().st_mtime_ns
    os.utime(path, ns=(first - 10_000_000, first - 10_000_000))
    mutate_records(path, lambda data: data.get("a"))
    assert path.stat().st_mtime_ns == first - 10_000_000


def _put_many(home: str, worker: int) -> None:
    store = SandboxRecords(Path(home), "singularity", profile_id="work")
    for n in range(PUTS_PER_PROCESS):
        store.put(f"w{worker}-{n}", overlay_path=f"/o/{worker}/{n}")


def test_two_processes_lose_no_update(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    workers = [context.Process(target=_put_many, args=(str(tmp_path), w)) for w in (1, 2)]
    for proc in workers:
        proc.start()
    for proc in workers:
        proc.join(120)
        assert proc.exitcode == 0
    store = SandboxRecords(tmp_path, "singularity", profile_id="work")
    keys = store.keys()
    assert len(keys) == 2 * PUTS_PER_PROCESS
    record = store.get("w2-199")
    assert record is not None and record.overlay_path == "/o/2/199"
