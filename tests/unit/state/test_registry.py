"""Handle registry (P02-05): sharing, generations, teardown barrier, profile sweeps."""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from pathlib import Path

import pytest

from indus.foundation.errors import StoreReplaced
from indus.foundation.scope import Scope, make_scope
from indus.state import connection, registry
from indus.state.registry import StoreHandle, acquire, close_all_under, other_generations


def _count_teardowns(monkeypatch: pytest.MonkeyPatch) -> list[StoreHandle]:
    torn: list[StoreHandle] = []
    original = StoreHandle._teardown  # pyright: ignore[reportPrivateUsage]

    def counting(self: StoreHandle) -> None:
        torn.append(self)
        original(self)

    monkeypatch.setattr(StoreHandle, "_teardown", counting)
    return torn


def test_acquires_share_one_handle_and_the_last_release_closes(
    scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    torn = _count_teardowns(monkeypatch)
    first = acquire(scope)
    second = acquire(scope)
    third = acquire(scope, scope.home / "state.db")
    assert first is second is third
    first.close()
    second.close()
    assert torn == [] and not first.closed
    third.close()
    assert torn == [first] and first.closed
    assert registry.live_handle_count(first.path) == 0


def test_shared_context_manager_releases(scope: Scope) -> None:
    with registry.shared(scope) as handle:
        handle.write(lambda c: c.execute("INSERT INTO meta(key, value) VALUES ('a', 'b')"))
    assert handle.closed


def _other_store(scope: Scope, name: str) -> Path:
    other = registry.open_unshared(scope, scope.home / name / "state.db")
    other.write(lambda c: c.execute("INSERT INTO meta(key, value) VALUES ('which', ?)", (name,)))
    other.close()
    return other.path


def test_inode_swap_opens_a_new_generation_and_fences_the_old_one(scope: Scope) -> None:
    old = acquire(scope)
    old.write(lambda c: c.execute("INSERT INTO meta(key, value) VALUES ('which', 'old')"))
    replacement = _other_store(scope, "restore")
    # A restore brings its own sidecars; move the live generation's aside first.
    for suffix in ("-wal", "-shm"):
        side = Path(f"{old.path}{suffix}")
        if side.exists():
            os.rename(side, Path(f"{old.path}{suffix}.retired"))
    os.replace(replacement, old.path)

    new = acquire(scope)
    try:
        assert new is not old
        with new.read() as conn:
            assert (
                conn.execute("SELECT value FROM meta WHERE key='which'").fetchone()[0] == "restore"
            )
        with old.read() as conn:  # the old holder keeps reading its own file
            assert conn.execute("SELECT value FROM meta WHERE key='which'").fetchone()[0] == "old"
        with pytest.raises(StoreReplaced):
            old.write(lambda c: c.execute("INSERT INTO meta(key, value) VALUES ('x', 'y')"))
        assert other_generations(old.path) == ["in-process store handle (refcount 1)"]
        assert other_generations(old.path, exclude=new) == []
    finally:
        old.close()
        new.close()
    assert old.closed and new.closed


def test_close_all_under_closes_only_that_profile(scope: Scope, isolated_home: Path) -> None:
    root_scope = make_scope(isolated_home, env=dict(scope.env), clock=scope.clock)
    work = acquire(scope)
    default = acquire(root_scope)
    try:
        assert close_all_under(scope.home) == 1
        assert work.closed and not default.closed
        assert acquire(scope) is not work  # a fresh generation after the sweep
    finally:
        registry.close_all()
    assert default.closed


def test_close_all_under_a_folder_with_no_handles_is_a_noop(scope: Scope) -> None:
    assert close_all_under(scope.home / "nothing") == 0


def test_acquire_waits_for_an_admitted_teardown(
    scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = StoreHandle._teardown  # pyright: ignore[reportPrivateUsage]
    in_teardown = threading.Event()
    finish_teardown = threading.Event()
    order: list[str] = []

    def slow_teardown(self: StoreHandle) -> None:
        in_teardown.set()
        finish_teardown.wait(10)
        original(self)
        order.append("closed")

    monkeypatch.setattr(StoreHandle, "_teardown", slow_teardown)
    handle = acquire(scope)
    closer = threading.Thread(target=handle.close)
    closer.start()
    assert in_teardown.wait(10)

    real_open = registry._open_handle  # pyright: ignore[reportPrivateUsage]

    def recording_open(s: Scope, p: Path) -> StoreHandle:
        order.append("opened")
        return real_open(s, p)

    monkeypatch.setattr(registry, "_open_handle", recording_open)
    got: list[StoreHandle] = []
    entered = threading.Event()

    def reacquire() -> None:
        entered.set()
        got.append(acquire(scope))

    waiter = threading.Thread(target=reacquire)
    waiter.start()
    assert entered.wait(10)
    waiter.join(0.1)
    assert waiter.is_alive() and order == []  # still waiting behind the barrier
    finish_teardown.set()
    closer.join(10)
    waiter.join(10)
    assert order == ["closed", "opened"]
    assert got and got[0] is not handle
    monkeypatch.setattr(StoreHandle, "_teardown", original)
    got[0].close()


def test_cold_concurrent_acquires_open_one_handle(
    scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_open = registry._open_handle  # pyright: ignore[reportPrivateUsage]
    opens: list[Path] = []
    second_waiting = threading.Event()

    def gated_open(s: Scope, p: Path) -> StoreHandle:
        opens.append(p)
        second_waiting.wait(10)
        return real_open(s, p)

    monkeypatch.setattr(registry, "_open_handle", gated_open)
    results: list[StoreHandle] = []
    first = threading.Thread(target=lambda: results.append(acquire(scope)))
    first.start()
    while not opens:  # the first thread is inside the open
        first.join(0.001)
    second = threading.Thread(target=lambda: results.append(acquire(scope)))
    second.start()
    second.join(0.05)
    second_waiting.set()
    first.join(10)
    second.join(10)
    assert len(opens) == 1 and len(results) == 2 and results[0] is results[1]
    results[0].close()
    results[1].close()


def test_failed_open_leaves_no_entry_and_the_next_acquire_retries(
    scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_open = registry._open_handle  # pyright: ignore[reportPrivateUsage]
    calls = [0]

    def flaky(s: Scope, p: Path) -> StoreHandle:
        calls[0] += 1
        if calls[0] == 1:
            raise sqlite3.OperationalError("unable to open database file")
        return real_open(s, p)

    monkeypatch.setattr(registry, "_open_handle", flaky)
    with pytest.raises(sqlite3.OperationalError):
        acquire(scope)
    handle = acquire(scope)
    assert calls[0] == 2
    handle.close()


def test_many_handles_on_one_path_warn_once(scope: Scope, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    handles = [registry.open_unshared(scope) for _ in range(registry.HANDLES_PER_PATH_WARN + 2)]
    try:
        warnings = [r for r in caplog.records if "store handles are open" in r.getMessage()]
        assert len(warnings) == 1
    finally:
        for handle in handles:
            handle.close()


def test_close_sequence_checkpoints_and_releases_the_header_descriptor(
    store: StoreHandle,
) -> None:
    store.write(lambda c: c.execute("INSERT INTO meta(key, value) VALUES ('a', 'b')"))
    connection.read_header(store.path)
    key = os.path.abspath(store.path)
    assert key in connection._probe_fds  # pyright: ignore[reportPrivateUsage]
    store.close()
    assert key not in connection._probe_fds  # pyright: ignore[reportPrivateUsage]
    assert store.writer.conn is None


def test_a_delete_mode_store_works_end_to_end(
    scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    from indus.state import journal

    monkeypatch.setattr(journal, "linked_sqlite_version", lambda: (3, 45, 1))  # WAL-reset range
    handle = acquire(scope)
    try:
        assert handle.journal_mode == "delete" and not handle.wal
        handle.write(lambda c: c.execute("INSERT INTO meta(key, value) VALUES ('m', 'd')"))
        with handle.read() as conn:
            assert conn is handle.writer.conn
            assert conn.execute("SELECT value FROM meta WHERE key='m'").fetchone()[0] == "d"
    finally:
        handle.close()
