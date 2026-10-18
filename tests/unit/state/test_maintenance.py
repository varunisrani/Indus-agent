"""Prune, automatic maintenance, VACUUM gates and stats (P02-19)."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.errors import StoreCorrupt
from indus.foundation.scope import Scope
from indus.state import deletion, fts, guards, maintenance, registry, sessions
from indus.state.maintenance import Holder, VacuumRefused
from indus.state.registry import StoreHandle
from tests.unit.state._helpers_d import (
    DAY,
    Config,
    add_guard,
    add_message,
    add_session,
    advance,
    count,
    meta_value,
    now_of,
    session_row,
)


def _aged(store: StoreHandle, sid: str, days: float, **columns: Any) -> None:
    """An ended session whose start, end and only message are ``days`` old."""
    at = now_of(store) - days * DAY
    columns.setdefault("end_reason", "cli_close")
    add_session(store, sid, started_at=at, ended_at=at, **columns)
    add_message(store, sid, timestamp=at)


def _configured(scope: Scope, **values: object) -> Scope:
    return scope.with_config(Config({f"sessions.{k}": v for k, v in values.items()}))


# -- prune ----------------------------------------------------------------------------------------


def test_prune_table(store: StoreHandle) -> None:
    _aged(store, "old", 100)
    _aged(store, "recent", 10)
    add_session(store, "open-old", started_at=now_of(store) - 100 * DAY)
    _aged(store, "pinned", 100, pinned=1)

    preview = maintenance.prune(store, 90, dry_run=True)
    assert (preview.ids, preview.spared_pinned, preview.dry_run) == (["old"], ["pinned"], True)
    assert session_row(store, "old") is not None  # a dry run writes nothing

    report = maintenance.prune(store, 90)
    assert report.ids == ["old"] and report.count == 1
    assert session_row(store, "old") is None
    for kept in ("recent", "open-old", "pinned"):
        assert session_row(store, kept) is not None, kept
    stone = json.loads(meta_value(store, sessions.TOMBSTONE_PREFIX + "old") or "{}")
    assert stone["by"] == "sweep"


def test_recent_activity_keeps_an_old_row(store: StoreHandle) -> None:
    now = now_of(store)
    _aged(store, "heartbeat", 100, last_activity_at=now - 2 * DAY)
    _aged(store, "late-message", 100)
    add_message(store, "late-message", timestamp=now - 3 * DAY)
    _aged(store, "far-future", 100, last_activity_at=now + 900 * DAY)  # outside the window
    _aged(store, "reaped", 100, end_reason=sessions.END_STARTUP_ORPHAN_REAP)
    store.write(
        lambda c: c.execute("UPDATE sessions SET ended_at = ? WHERE id = 'reaped'", (now - DAY,))
    )
    assert maintenance.prune(store, 10).ids == ["far-future"]


def test_compression_root_kept_while_its_tip_is_recent(store: StoreHandle) -> None:
    now = now_of(store)
    for chat in ("live", "idle"):
        _aged(store, chat, 120, end_reason="compression")
        _aged(store, f"{chat}-2", 119, end_reason="compression", parent=chat)
    add_session(store, "live-3", parent="live-2", started_at=now - 118 * DAY)
    add_message(store, "live-3", timestamp=now - DAY)
    _aged(store, "idle-3", 118, parent="idle-2")
    # A branch off the idle root does not keep it.
    add_session(store, "idle-fork", parent="idle", marker=sessions.MARKER_BRANCHED)

    assert set(maintenance.prune(store, 90).ids) == {"idle", "idle-2", "idle-3"}
    for kept in ("live", "live-2", "live-3", "idle-fork"):
        assert session_row(store, kept) is not None, kept


def test_pinned_segment_spares_its_continuations(store: StoreHandle) -> None:
    _aged(store, "root", 120, end_reason="compression", pinned=1)
    _aged(store, "next", 110, parent="root")
    report = maintenance.prune(store, 90)
    assert report.ids == [] and set(report.spared_pinned) == {"root", "next"}


def test_guarded_rows_are_spared(store: StoreHandle) -> None:
    now = now_of(store)
    _aged(store, "leased", 100)
    _aged(store, "expired", 100)
    add_guard(store, "leased", table="turn_leases", holder="w", expires_at=now + 60)
    add_guard(store, "expired", table="compression_locks", holder="w", expires_at=now - 1)
    preview = maintenance.prune(store, 90, dry_run=True)
    assert preview.spared_guarded == ["leased"] and preview.ids == ["expired"]
    assert count(store, "SELECT COUNT(*) FROM compression_locks") == 1  # dry run reaps nothing
    report = maintenance.prune(store, 90)
    assert report.ids == ["expired"] and report.spared_guarded == ["leased"]
    assert count(store, "SELECT COUNT(*) FROM compression_locks") == 0


def test_guarded_continuation_keeps_its_ancestors(store: StoreHandle) -> None:
    _aged(store, "root", 120, end_reason="compression")
    _aged(store, "tip", 110, parent="root")
    add_guard(store, "tip", table="turn_leases", holder="w", expires_at=now_of(store) + 60)
    report = maintenance.prune(store, 90)
    assert report.ids == [] and report.spared_guarded == ["tip"]
    assert session_row(store, "root") is not None


def test_prune_source_filter_and_bad_window(store: StoreHandle) -> None:
    _aged(store, "a-cli", 100)
    _aged(store, "a-cron", 100, source="cron")
    assert maintenance.prune(store, 90, source="cron").ids == ["a-cron"]
    with pytest.raises(ValueError):
        maintenance.prune(store, -1)


def test_prune_reaps_aged_tombstones(store: StoreHandle) -> None:
    old = now_of(store) - 8 * DAY
    store.write(lambda c: deletion.write_tombstone_in(c, "swept", by="sweep", now=old))
    store.write(lambda c: deletion.write_tombstone_in(c, "kept", by="user", now=old))
    maintenance.prune(store, 90)
    assert meta_value(store, sessions.TOMBSTONE_PREFIX + "swept") is None
    assert meta_value(store, sessions.TOMBSTONE_PREFIX + "kept") is not None


def test_prune_does_not_cascade_delegates(store: StoreHandle) -> None:
    _aged(store, "boss", 100)
    add_session(store, "worker", parent="boss", marker=sessions.MARKER_DELEGATE)
    maintenance.prune(store, 90)
    kept = session_row(store, "worker")
    assert kept is not None and kept["parent_session_id"] is None


# -- automatic maintenance ------------------------------------------------------------------------


def test_auto_maintain_is_off_by_default(store: StoreHandle, scope: Scope) -> None:
    _aged(store, "old", 100)
    assert maintenance.auto_maintain(scope, store=store).skipped == "disabled"
    assert maintenance.auto_maintain(_configured(scope, auto_prune="yes"), store=store).skipped == (
        "disabled"
    )
    assert session_row(store, "old") is not None


def test_auto_maintain_runs_once_a_day(store: StoreHandle, scope: Scope) -> None:
    on = _configured(scope, auto_prune=True, retention_days=30)
    _aged(store, "old", 100)
    first = maintenance.auto_maintain(on, store=store)
    assert first.ran and first.pruned == 1 and first.error is None
    _aged(store, "old-2", 100)
    advance(store, 23 * 3600)
    second = maintenance.auto_maintain(on, store=store)
    assert (second.ran, second.skipped) == (False, "recent")
    assert session_row(store, "old-2") is not None
    advance(store, 3600)
    third = maintenance.auto_maintain(on, store=store)
    assert third.ran and third.pruned == 1


def test_auto_maintain_records_a_run_that_pruned_nothing(store: StoreHandle, scope: Scope) -> None:
    on = _configured(scope, auto_prune=True, retention_days=30)
    assert maintenance.auto_maintain(on, store=store).ran
    assert meta_value(store, maintenance.LAST_AUTO_PRUNE) == repr(now_of(store))


@pytest.mark.parametrize("retention", [-1, None, "90", 1.5, True])
def test_auto_maintain_bad_retention_skips(
    store: StoreHandle, scope: Scope, retention: object, caplog: pytest.LogCaptureFixture
) -> None:
    on = _configured(scope, auto_prune=True, retention_days=retention)
    with caplog.at_level(logging.WARNING, logger="indus.state.maintenance"):
        report = maintenance.auto_maintain(on, store=store)
    assert report.skipped == "bad_retention" and "retention_days" in caplog.text


def test_auto_maintain_lock_held_skips(store: StoreHandle, scope: Scope) -> None:
    on = _configured(scope, auto_prune=True, retention_days=30)
    lock_path = store.path.parent / maintenance.AUTO_LOCK_NAME
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert maintenance.auto_maintain(on, store=store).skipped == "busy"
    finally:
        os.close(fd)
    assert maintenance.auto_maintain(on, store=store).ran


def test_auto_maintain_without_a_store_file(scope: Scope) -> None:
    on = _configured(scope, auto_prune=True, retention_days=30)
    report = maintenance.auto_maintain(on)
    assert report.skipped == "no_store"
    assert not (scope.home / "state.db").exists()


def test_auto_maintain_opens_the_shared_handle(store: StoreHandle, scope: Scope) -> None:
    _aged(store, "old", 100)
    report = maintenance.auto_maintain(_configured(scope, auto_prune=True, retention_days=30))
    assert report.ran and report.pruned == 1
    assert registry.live_handle_count(store.path) == 1  # the shared one was released again


def test_auto_maintain_closes_stale_own_sources(store: StoreHandle, scope: Scope) -> None:
    now = now_of(store)
    add_session(store, "cli-stale", started_at=now - 40 * DAY)
    add_session(store, "chat-stale", source="telegram", started_at=now - 40 * DAY)
    add_session(store, "cli-busy", started_at=now - 40 * DAY)
    add_message(store, "cli-busy", timestamp=now - DAY)
    add_session(store, "cron-pinned", source="cron", started_at=now - 40 * DAY, pinned=1)
    report = maintenance.auto_maintain(
        _configured(scope, auto_prune=True, retention_days=30), store=store
    )
    assert report.closed == ["cli-stale"]
    closed = session_row(store, "cli-stale")
    assert closed is not None and closed["end_reason"] == sessions.END_STARTUP_ORPHAN_REAP
    for still_open in ("chat-stale", "cli-busy", "cron-pinned"):
        row = session_row(store, still_open)
        assert row is not None and row["ended_at"] is None, still_open


def test_auto_maintain_never_raises(
    store: StoreHandle, scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(maintenance, "prune", boom)
    report = maintenance.auto_maintain(
        _configured(scope, auto_prune=True, retention_days=30), store=store
    )
    assert report.error == "disk on fire" and not report.ran
    assert meta_value(store, maintenance.LAST_AUTO_PRUNE) is None  # the next start retries


@pytest.mark.parametrize(
    ("pruned", "last_days_ago", "ratio", "holders", "due"),
    [
        (1, None, 0.5, 0, True),
        (0, None, 0.5, 0, False),
        (1, 29.0, 0.5, 0, False),
        (1, 30.0, 0.5, 0, True),
        (1, None, 0.25, 0, False),
        (1, None, 0.26, 0, True),
        (1, None, None, 0, False),
        (1, None, 0.9, 1, False),
    ],
)
def test_auto_vacuum_gates(
    pruned: int, last_days_ago: float | None, ratio: float | None, holders: int, due: bool
) -> None:
    now = 2_000_000_000.0
    last = None if last_days_ago is None else now - last_days_ago * DAY
    assert (
        maintenance.auto_vacuum_due(
            pruned=pruned, last_vacuum=last, now=now, freelist_ratio=ratio, holders=holders
        )
        is due
    )


def _bulky(store: StoreHandle, sid: str, days: float) -> None:
    _aged(store, sid, days)
    at = now_of(store) - days * DAY
    for _ in range(400):
        add_message(store, sid, "z" * 4000, timestamp=at)


def test_auto_maintain_vacuums_when_due(store: StoreHandle, scope: Scope) -> None:
    on = _configured(scope, auto_prune=True, retention_days=30)
    _bulky(store, "old", 100)
    report = maintenance.auto_maintain(on, store=store)
    assert report.pruned == 1 and report.vacuumed
    assert report.freelist_ratio is not None and report.freelist_ratio > 0.25
    assert meta_value(store, maintenance.LAST_VACUUM) == repr(now_of(store))

    advance(store, 2 * DAY)
    _bulky(store, "old-2", 100)
    again = maintenance.auto_maintain(on, store=store)
    assert again.pruned == 1 and not again.vacuumed  # the last VACUUM is only two days old


def test_auto_maintain_skips_vacuum_with_a_holder(store: StoreHandle, scope: Scope) -> None:
    on = _configured(scope, auto_prune=True, retention_days=30)
    _bulky(store, "old", 100)
    shared = registry.acquire(scope)
    try:
        report = maintenance.auto_maintain(on, store=store)
    finally:
        shared.close()
    assert report.pruned == 1 and report.holders == 1 and not report.vacuumed


# -- vacuum ---------------------------------------------------------------------------------------


def test_vacuum_refuses_another_generation(store: StoreHandle, scope: Scope) -> None:
    shared = registry.acquire(scope)
    try:
        with pytest.raises(VacuumRefused) as caught:
            maintenance.vacuum(store)
        assert caught.value.code == "store.vacuum_refused"
        assert [h.pid for h in caught.value.holders] == [os.getpid()]
        assert str(os.getpid()) in str(caught.value)
        forced = maintenance.vacuum(store, force=True)
        assert forced.forced and forced.holders
    finally:
        shared.close()


def test_vacuum_refuses_a_quarantined_handle(store: StoreHandle) -> None:
    guards.mark_corrupt(store.path, "test damage")
    with pytest.raises(StoreCorrupt):
        maintenance.vacuum(store, force=True)


def test_vacuum_shrinks_merges_first_and_rerecords_identity(
    store: StoreHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(fts, "merge", lambda s: calls.append("merge"))
    _bulky(store, "big", 1)
    deletion.delete_session(store, "big")
    report = maintenance.vacuum(store)
    assert calls == ["merge"]
    assert report.size_before is not None and report.size_after is not None
    assert report.size_after < report.size_before
    assert meta_value(store, maintenance.LAST_VACUUM) == repr(now_of(store))
    add_session(store, "after")  # the guard did not take our own rewrite for a replaced file
    assert not store.replaced


def test_vacuum_survives_a_failing_fts_merge(
    store: StoreHandle, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(_store: StoreHandle) -> None:
        raise sqlite3.OperationalError("merge failed")

    monkeypatch.setattr(fts, "merge", broken)
    with caplog.at_level(logging.WARNING, logger="indus.state.maintenance"):
        maintenance.vacuum(store)
    assert "FTS" in caplog.text


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the holder scan is Linux-only")
def test_scan_failure_is_an_unknown_holder(
    store: StoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "no-proc"
    found = maintenance.scan_holders(store.path, proc_root=missing)
    assert len(found) == 1 and found[0].pid == -1
    original = maintenance.scan_holders
    monkeypatch.setattr(maintenance, "scan_holders", lambda path: original(path, proc_root=missing))
    with pytest.raises(VacuumRefused) as caught:
        maintenance.vacuum(store)
    assert caught.value.holders[0].pid == -1


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the holder scan is Linux-only")
def test_scan_ignores_this_process(store: StoreHandle, tmp_path: Path) -> None:
    assert maintenance.scan_holders(store.path) == []
    assert maintenance.scan_holders(tmp_path / "missing.db") == []


def test_holder_text_names_the_pid() -> None:
    err = VacuumRefused("refused", holders=[Holder(42, "/x/state.db")])
    assert err.holders[0].pid == 42


# -- stats ----------------------------------------------------------------------------------------


def test_stats_fields(store: StoreHandle) -> None:
    add_session(store, "a")
    add_message(store, "a")
    add_message(store, "a")
    snap = maintenance.stats(store)
    assert (
        snap.page_count and snap.page_size and snap.size_bytes == snap.page_count * snap.page_size
    )
    assert snap.freelist_ratio is not None and 0 <= snap.freelist_ratio <= 1
    assert snap.rows["sessions"] == 1 and snap.rows["messages"] == 2


def test_stats_on_a_busy_store_do_not_block(store: StoreHandle) -> None:
    add_session(store, "a")
    other = sqlite3.connect(store.path, timeout=0.1, isolation_level=None)
    try:
        other.execute("BEGIN IMMEDIATE")
        other.execute("INSERT INTO sessions (id, source, started_at) VALUES ('b', 'cli', 1)")
        started = time.monotonic()
        snap = maintenance.stats(store)
        assert time.monotonic() - started < 2.0
        assert snap.rows["sessions"] == 1  # the uncommitted row is not seen
    finally:
        other.execute("ROLLBACK")
        other.close()
