"""Integrity guards (P02-07): replaced file, corruption quarantine, health latch, divert."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from indus.foundation.errors import StoreCorrupt, StoreError, StoreReplaced
from indus.foundation.scope import Scope
from indus.state import guards, registry
from indus.state.guards import divert_messages, divert_path, health_state
from indus.state.registry import StoreHandle

SESSION = "20260101_090000_abcdef"


def _put(key: str, value: str = "v"):  # type: ignore[no-untyped-def]
    def fn(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    return fn


def _swap_in_another_store(scope: Scope, store: StoreHandle) -> None:
    other = registry.open_unshared(scope, scope.home / "restored" / "state.db")
    other.write(_put("which", "restored"))
    other.close()
    for suffix in ("-wal", "-shm"):
        side = Path(f"{store.path}{suffix}")
        if side.exists():
            os.rename(side, Path(f"{store.path}{suffix}.live"))
    os.replace(other.path, store.path)


def _trace(store: StoreHandle) -> list[str]:
    statements: list[str] = []
    conn = store.writer.conn
    assert conn is not None
    conn.set_trace_callback(statements.append)
    return statements


# -- replaced file ------------------------------------------------------------------------------


def test_replaced_file_fences_every_later_write(scope: Scope, store: StoreHandle) -> None:
    store.write(_put("before"))
    _swap_in_another_store(scope, store)
    calls: list[int] = []

    def fn(conn: sqlite3.Connection) -> None:
        calls.append(1)

    with pytest.raises(StoreReplaced):
        store.write(fn)
    with pytest.raises(StoreReplaced):
        store.write(fn)
    assert calls == [] and store.replaced and health_state(store.path) == "ok"

    target = divert_messages(
        scope.home, SESSION, [{"role": "user", "content": "kept \ud800"}, {"role": "assistant"}]
    )
    assert target == scope.home / "sessions" / f"{SESSION}.jsonl"
    lines = target.read_text(encoding="ascii").splitlines()
    assert [json.loads(line)["role"] for line in lines] == ["user", "assistant"]
    assert os.stat(target).st_mode & 0o777 == 0o600

    statements = _trace(store)
    store.close()
    assert not any("wal_checkpoint" in s for s in statements)


def test_application_id_change_alone_is_detected(
    store: StoreHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.write(_put("a"))
    assert store.guard.application_id != 0
    monkeypatch.setattr(guards, "header_application_id", lambda _p: store.guard.application_id + 1)
    with pytest.raises(StoreReplaced):
        store.write(_put("b"))


def test_header_zero_is_not_a_replacement(
    store: StoreHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(guards, "header_application_id", lambda _p: 0)
    store.write(_put("a"))
    assert not store.replaced


def test_corruption_on_a_replaced_file_is_not_quarantined(
    scope: Scope, store: StoreHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    def malformed(conn: sqlite3.Connection) -> None:
        # the swap happens while the write is in flight
        _swap_in_another_store(scope, store)
        raise sqlite3.DatabaseError("database disk image is malformed")

    with pytest.raises(StoreReplaced):
        store.write(malformed)
    assert store.replaced and not store.quarantined
    assert health_state(store.path) == "ok"


# -- structural corruption ----------------------------------------------------------------------


def _malformed(conn: sqlite3.Connection) -> None:
    raise sqlite3.DatabaseError("database disk image is malformed")


def test_corruption_quarantines_the_handle(scope: Scope, store: StoreHandle) -> None:
    with pytest.raises(StoreCorrupt) as caught:
        store.write(_malformed)
    assert isinstance(caught.value.__cause__, sqlite3.DatabaseError)
    assert store.quarantined and health_state(store.path) == "corrupt"

    statements = _trace(store)
    calls: list[int] = []
    with pytest.raises(StoreCorrupt):
        store.write(lambda c: calls.append(1))
    assert calls == [] and statements == []  # failed before touching SQLite

    conn = store.writer.conn
    assert conn is not None
    assert conn.getconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE) is True

    other = registry.open_unshared(scope)
    try:
        with pytest.raises(StoreCorrupt):
            other.write(_put("x"))
        assert other.quarantined
    finally:
        other.close()

    store.close()
    assert not any("wal_checkpoint" in s for s in statements)


def test_quarantined_handle_never_reopens(store: StoreHandle) -> None:
    with pytest.raises(StoreCorrupt):
        store.write(_malformed)
    store.writer.close(checkpoint=False)
    with pytest.raises(StoreCorrupt):
        store.write(_put("x"))
    assert store.writer.conn is None


def test_fts_scoped_corruption_is_not_quarantined(store: StoreHandle) -> None:
    def fts_broken(conn: sqlite3.Connection) -> None:
        raise sqlite3.DatabaseError("database disk image is malformed (messages_fts_data)")

    with pytest.raises(sqlite3.DatabaseError):
        store.write(fts_broken)
    assert not store.quarantined and health_state(store.path) == "ok"


def test_fts_fail_open_hook_retries_the_write(store: StoreHandle) -> None:
    attempts: list[int] = []
    detached: list[BaseException] = []

    def flaky(conn: sqlite3.Connection) -> None:
        attempts.append(1)
        if len(attempts) == 1:
            raise sqlite3.DatabaseError("fts5: corruption in messages_fts")
        conn.execute("INSERT INTO meta(key, value) VALUES ('ok', '1')")

    def detach(exc: BaseException) -> bool:
        detached.append(exc)
        return True

    store.writer.set_fts_fail_open(detach)
    store.write(flaky)
    assert len(attempts) == 2 and len(detached) == 1
    with store.read() as conn:
        assert conn.execute("SELECT value FROM meta WHERE key='ok'").fetchone()[0] == "1"


def test_ordinary_errors_pass_through(store: StoreHandle) -> None:
    def constraint(conn: sqlite3.Connection) -> None:
        conn.execute("INSERT INTO sessions(id, started_at) VALUES ('x', 1)")  # source NOT NULL

    with pytest.raises(sqlite3.IntegrityError):
        store.write(constraint)
    assert not store.quarantined and not store.replaced


# -- divert -------------------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["../escape", "a/b", ".hidden", "", "x" * 200])
def test_divert_refuses_unsafe_ids(tmp_path: Path, bad: str) -> None:
    with pytest.raises(StoreError):
        divert_path(tmp_path, bad)


def test_divert_appends(tmp_path: Path) -> None:
    divert_messages(tmp_path, SESSION, [{"n": 1}])
    divert_messages(tmp_path, SESSION, [{"n": 2}, {"n": 3}])
    lines = divert_path(tmp_path, SESSION).read_text(encoding="ascii").splitlines()
    assert [json.loads(line)["n"] for line in lines] == [1, 2, 3]
