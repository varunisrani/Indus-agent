"""Read pool (P02-04): per-path and per-process caps, fd headroom, fallbacks, backoff, URIs."""

from __future__ import annotations

import sqlite3
from contextlib import ExitStack
from pathlib import Path

import pytest

from indus.foundation.scope import Scope
from indus.state import readpool, registry
from indus.state.readpool import (
    OPEN_RETRY_SECONDS,
    POOL_MAX_PER_PATH,
    POOL_MAX_PER_PROCESS,
    ReadPool,
    process_permits_in_use,
)
from indus.state.registry import StoreHandle
from tests.unit.state.conftest import AutoClock


def _is_writer(store: StoreHandle, conn: sqlite3.Connection) -> bool:
    return conn is store.writer.conn


def test_store_fixture_is_wal(store: StoreHandle) -> None:
    assert store.wal and store.reads.wal


def test_reads_reuse_a_pooled_connection(store: StoreHandle) -> None:
    with store.read() as first:
        assert not _is_writer(store, first)
    with store.read() as second:
        assert second is first
    assert store.reads.live_count == 1 and store.reads.idle_count == 1


def test_read_sees_a_committed_write(store: StoreHandle) -> None:
    with store.read() as conn:
        conn.execute("SELECT 1").fetchone()
    store.write(lambda c: c.execute("INSERT INTO meta(key, value) VALUES ('k', 'v')"))
    with store.read() as conn:
        assert conn.execute("SELECT value FROM meta WHERE key='k'").fetchone()[0] == "v"


def test_nine_concurrent_readers_use_eight_pooled_and_the_writer(store: StoreHandle) -> None:
    with ExitStack() as stack:
        conns = [stack.enter_context(store.read()) for _ in range(POOL_MAX_PER_PATH + 1)]
        pooled = [c for c in conns if not _is_writer(store, c)]
        assert len({id(c) for c in pooled}) == POOL_MAX_PER_PATH
        assert sum(1 for c in conns if _is_writer(store, c)) == 1
        assert store.reads.live_count == POOL_MAX_PER_PATH
    assert store.reads.idle_count == POOL_MAX_PER_PATH


def _extra_stores(scope: Scope, count: int) -> list[StoreHandle]:
    return [registry.open_unshared(scope, scope.home / f"s{i}" / "state.db") for i in range(count)]


def test_process_cap_falls_back_to_the_writer(scope: Scope, store: StoreHandle) -> None:
    others = _extra_stores(scope, 3)
    try:
        with ExitStack() as stack:
            for handle in (store, *others[:2]):
                for _ in range(POOL_MAX_PER_PATH):
                    stack.enter_context(handle.read())
            assert process_permits_in_use() == POOL_MAX_PER_PROCESS
            with others[2].read() as conn:
                assert _is_writer(others[2], conn)
    finally:
        for handle in others:
            handle.close()
    assert process_permits_in_use() == POOL_MAX_PER_PATH  # the fixture store's idle ones


def test_idle_connections_elsewhere_are_reclaimed_at_the_process_cap(
    scope: Scope, store: StoreHandle
) -> None:
    others = _extra_stores(scope, 3)
    try:
        with ExitStack() as stack:
            for handle in others[:2]:
                for _ in range(POOL_MAX_PER_PATH):
                    stack.enter_context(handle.read())
            with ExitStack() as idle_soon:
                for _ in range(POOL_MAX_PER_PATH):
                    idle_soon.enter_context(store.read())
            assert store.reads.idle_count == POOL_MAX_PER_PATH
            with others[2].read() as conn:
                assert not _is_writer(others[2], conn)
            assert store.reads.idle_count == POOL_MAX_PER_PATH - 1
    finally:
        for handle in others:
            handle.close()


def test_low_descriptor_headroom_refuses_new_connections(
    store: StoreHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(readpool, "fd_soft_limit", lambda: 128)
    monkeypatch.setattr(readpool, "open_fd_count", lambda: 70)
    with store.read() as conn:
        assert _is_writer(store, conn)
    assert store.reads.live_count == 0


def test_unmeasurable_descriptors_allow_reads(
    store: StoreHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(readpool, "fd_soft_limit", lambda: 128)
    monkeypatch.setattr(readpool, "open_fd_count", lambda: None)
    with store.read() as conn:
        assert not _is_writer(store, conn)


def test_failed_open_backs_off_for_sixty_seconds(store: StoreHandle, fake_clock: AutoClock) -> None:
    calls: list[Path] = []

    def broken(path: Path) -> sqlite3.Connection:
        calls.append(path)
        raise sqlite3.OperationalError("unable to open database file")

    pool = ReadPool(store.path, store.writer, wal=True, clock=fake_clock, opener=broken)
    try:
        for _ in range(3):
            with pool.read() as conn:
                assert _is_writer(store, conn)
        assert len(calls) == 1
        fake_clock.advance(OPEN_RETRY_SECONDS - 1)
        with pool.read():
            pass
        assert len(calls) == 1
        fake_clock.advance(1)
        with pool.read():
            pass
        assert len(calls) == 2
        assert pool.live_count == 0
    finally:
        pool.close()


def test_close_during_a_read_closes_that_connection(store: StoreHandle) -> None:
    with store.read() as conn:
        assert not _is_writer(store, conn)
        store.reads.close()
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")
    assert store.reads.live_count == 0 and store.reads.idle_count == 0
    assert process_permits_in_use() == 0


@pytest.mark.parametrize("folder", ["with#hash", "with?query", "both#and?"])
def test_special_characters_in_the_path_open_the_right_file(scope: Scope, folder: str) -> None:
    handle = registry.open_unshared(scope, scope.home / folder / "state.db")
    try:
        handle.write(
            lambda c: c.execute("INSERT INTO meta(key, value) VALUES ('here', ?)", (folder,))
        )
        with handle.read() as conn:
            assert not _is_writer(handle, conn)
            assert conn.execute("SELECT value FROM meta WHERE key='here'").fetchone()[0] == folder
    finally:
        handle.close()
    assert "%23" in readpool.read_only_uri(Path("/a#b")) and "%3F" in readpool.read_only_uri(
        Path("/a?b")
    )


def test_delete_mode_reads_on_the_writer_with_a_longer_busy_timeout(
    store: StoreHandle, fake_clock: AutoClock
) -> None:
    pool = ReadPool(store.path, store.writer, wal=False, clock=fake_clock)
    try:
        with pool.read() as conn:
            assert _is_writer(store, conn)
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == readpool.DELETE_READ_BUSY_MS
        with store.writer.borrow() as conn:
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 1000
    finally:
        pool.close()


def test_read_connections_are_read_only(store: StoreHandle) -> None:
    with store.read() as conn:
        assert not _is_writer(store, conn)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("INSERT INTO meta(key, value) VALUES ('x', 'y')")
