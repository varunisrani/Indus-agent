"""Schema 1 (P02-06): declared DDL, reconcile, stamps, lock patience, migration runner."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TypeVar, cast

import pytest

from indus.foundation.errors import StoreLocked, StoreUnavailable
from indus.state import schema
from indus.state.schema import (
    INDEX_NAMES,
    SCHEMA_VERSION,
    TABLES,
    TRIGGER_NAMES,
    Column,
    MigrationStep,
    init,
    meta_get,
    run_migrations,
)
from tests.unit.state.conftest import AutoClock, FakeConnection

T = TypeVar("T")


def _connect(path: Path, timeout: float = 0.0) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=timeout, isolation_level=None, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "s.db"


@pytest.fixture
def conn(db: Path) -> Iterator[sqlite3.Connection]:
    c = _connect(db)
    yield c
    c.close()


def _names(conn: sqlite3.Connection, kind: str) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type=?", (kind,))}


def test_fresh_init_creates_and_stamps_everything(
    conn: sqlite3.Connection, fake_clock: AutoClock
) -> None:
    report = init(conn, clock=fake_clock)
    assert report.fresh and report.changed and report.version == SCHEMA_VERSION == 1
    assert {t.name for t in TABLES} <= _names(conn, "table")
    assert set(INDEX_NAMES) <= _names(conn, "index")
    assert set(TRIGGER_NAMES) <= _names(conn, "trigger")
    assert conn.execute("SELECT version FROM schema_version").fetchall() == [(1,)]
    app_id = conn.execute("PRAGMA application_id").fetchone()[0]
    token = meta_get(conn, "db_file_generation") or ""
    assert re.fullmatch(r"[0-9a-f]{32}", token)
    assert app_id == report.application_id == schema.derive_application_id(token) != 0
    assert app_id < 2**31
    assert re.fullmatch(r"[0-9a-f]{32}", meta_get(conn, "store_id") or "")
    assert (meta_get(conn, "created_at") or "").startswith("2026-01-01T09:00:00")
    for table in TABLES:
        live = {r[1] for r in conn.execute(f"PRAGMA table_info({table.name})")}
        assert set(table.column_names) == live


def test_second_init_writes_nothing(conn: sqlite3.Connection, fake_clock: AutoClock) -> None:
    first = init(conn, clock=fake_clock)
    before = conn.total_changes
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    second = init(conn, clock=fake_clock)
    conn.set_trace_callback(None)
    assert not second.changed and not second.fresh
    assert conn.total_changes == before
    assert not any(s.startswith("BEGIN") for s in statements)  # no write lock taken
    assert second.generation == first.generation
    assert second.application_id == first.application_id


def _sessions_without(*dropped: str) -> str:
    table = next(t for t in TABLES if t.name == "sessions")
    kept = schema.Table(
        "sessions", tuple(c for c in table.columns if c.name not in dropped), table.constraints
    )
    return kept.create_sql()


def test_reconcile_adds_missing_columns_and_keeps_rows(
    conn: sqlite3.Connection, fake_clock: AutoClock
) -> None:
    conn.execute("PRAGMA foreign_keys=OFF")  # a hand-made legacy table without its neighbours
    conn.execute(_sessions_without("pinned", "last_read_at"))
    conn.execute("INSERT INTO sessions(id, source, started_at, title) VALUES ('s1', 'cli', 5, 'T')")
    conn.execute("PRAGMA foreign_keys=ON")
    report = init(conn, clock=fake_clock)
    assert sorted(report.added_columns) == ["sessions.last_read_at", "sessions.pinned"]
    row = conn.execute("SELECT id, source, title, pinned, last_read_at FROM sessions").fetchone()
    assert tuple(row) == ("s1", "cli", "T", 0, None)
    assert report.fresh  # no schema_version row yet: the hand-made store gets stamped


def test_add_decl_drops_what_alter_table_cannot_take() -> None:
    assert Column("id", "TEXT PRIMARY KEY").add_decl == "TEXT"
    assert Column("n", "INTEGER NOT NULL DEFAULT 0").add_decl == "INTEGER NOT NULL DEFAULT 0"
    assert Column("t", "REAL NOT NULL").add_decl == "REAL"
    assert Column("p", "TEXT REFERENCES sessions(id)").add_decl == "TEXT REFERENCES sessions(id)"


def test_uncreatable_column_is_skipped_not_fatal(
    conn: sqlite3.Connection, fake_clock: AutoClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    init(conn, clock=fake_clock)
    extra = schema.Table("meta", (*TABLES[1].columns, Column("odd", "TEXT UNIQUE")))
    monkeypatch.setattr(schema, "TABLES", (*TABLES[:1], extra, *TABLES[2:]))
    monkeypatch.setattr(Column, "add_decl", property(lambda self: self.decl))
    report = init(conn, clock=fake_clock)
    assert "meta.odd" not in report.added_columns


def _session(conn: sqlite3.Connection, sid: str, title: str | None = None) -> None:
    conn.execute(
        "INSERT INTO sessions(id, source, started_at, title) VALUES (?, 'cli', 1, ?)", (sid, title)
    )


def test_message_without_uid_gets_one(conn: sqlite3.Connection, fake_clock: AutoClock) -> None:
    init(conn, clock=fake_clock)
    _session(conn, "s")
    conn.execute(
        "INSERT INTO messages(session_id, role, content, timestamp) VALUES ('s','user','hi',1)"
    )
    conn.execute(
        "INSERT INTO messages(session_id, role, content, timestamp, message_uid) "
        "VALUES ('s','user','hi',1,'kept')"
    )
    uids = [r[0] for r in conn.execute("SELECT message_uid FROM messages ORDER BY id")]
    assert re.fullmatch(r"[0-9a-f]{32}", uids[0]) and uids[1] == "kept"


def test_titles_unique_when_not_null(conn: sqlite3.Connection, fake_clock: AutoClock) -> None:
    init(conn, clock=fake_clock)
    _session(conn, "a", "Plan")
    _session(conn, "b")
    _session(conn, "c")
    with pytest.raises(sqlite3.IntegrityError):
        _session(conn, "d", "Plan")


def test_foreign_keys_cascade_and_orphan(conn: sqlite3.Connection, fake_clock: AutoClock) -> None:
    init(conn, clock=fake_clock)
    _session(conn, "p")
    conn.execute(
        "INSERT INTO sessions(id, source, started_at, parent_session_id) VALUES ('c','cli',1,'p')"
    )
    conn.execute("INSERT INTO messages(session_id, role, timestamp) VALUES ('p','user',1)")
    conn.execute("DELETE FROM sessions WHERE id='p'")
    assert conn.execute("SELECT parent_session_id FROM sessions WHERE id='c'").fetchone()[0] is None
    assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


def test_init_waits_out_a_three_second_lock(db: Path, fake_clock: AutoClock) -> None:
    holder = _connect(db)
    holder.execute("CREATE TABLE warmup(x)")
    holder.execute("BEGIN IMMEDIATE")
    start = fake_clock.monotonic()

    def sleep(seconds: float) -> None:
        fake_clock.sleep_sync(seconds)
        if fake_clock.monotonic() - start >= 3.0 and holder.in_transaction:
            holder.execute("COMMIT")

    conn = _connect(db)
    try:
        report = init(conn, clock=fake_clock, sleep=sleep)
        assert report.fresh
        assert fake_clock.monotonic() - start >= 3.0
        assert all(0.25 <= s <= 1.0 for s in fake_clock.sleeps)
    finally:
        conn.close()
        holder.close()


def test_init_gives_up_after_the_patience(db: Path, fake_clock: AutoClock) -> None:
    holder = _connect(db)
    holder.execute("CREATE TABLE warmup(x)")
    holder.execute("BEGIN IMMEDIATE")
    conn = _connect(db)
    try:
        with pytest.raises(StoreLocked):
            init(conn, clock=fake_clock)
        assert sum(fake_clock.sleeps) == pytest.approx(schema.INIT_LOCK_SECONDS)
    finally:
        conn.close()
        holder.execute("ROLLBACK")
        holder.close()


def test_non_lock_init_error_is_raised_at_once(db: Path, fake_clock: AutoClock) -> None:
    fake = FakeConnection(_connect(db))
    fake.fail(
        "CREATE TABLE IF NOT EXISTS sessions", lambda: sqlite3.OperationalError("disk I/O error")
    )
    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        init(cast(sqlite3.Connection, fake), clock=fake_clock)
    assert fake_clock.sleeps == []
    assert "schema_version" not in _names(fake.real, "table")  # rolled back
    fake.real.close()


def test_newer_store_is_refused_without_writing(
    conn: sqlite3.Connection, fake_clock: AutoClock
) -> None:
    init(conn, clock=fake_clock)
    conn.execute("UPDATE schema_version SET version = 2")
    before = conn.total_changes
    with pytest.raises(StoreUnavailable) as caught:
        init(conn, clock=fake_clock)
    assert caught.value.kind == "incompatible"
    assert conn.total_changes == before


class _Ext:
    name = "demo"

    def __init__(self) -> None:
        self.applied = 0

    def needs_work(self, conn: sqlite3.Connection) -> bool:
        return "demo_ext" not in _names(conn, "table")

    def apply(self, conn: sqlite3.Connection) -> None:
        assert conn.in_transaction
        self.applied += 1
        conn.execute("CREATE TABLE demo_ext(x)")


def test_extensions_run_inside_the_init_transaction(
    conn: sqlite3.Connection, fake_clock: AutoClock
) -> None:
    ext = _Ext()
    init(conn, clock=fake_clock, extensions=(ext,))
    init(conn, clock=fake_clock, extensions=(ext,))
    assert ext.applied == 1 and "demo_ext" in _names(conn, "table")


# -- migration runner ---------------------------------------------------------------------------


def test_chunked_migration_is_time_boxed_and_resumes(
    conn: sqlite3.Connection, fake_clock: AutoClock
) -> None:
    init(conn, clock=fake_clock)
    conn.execute("CREATE TABLE dummy(id INTEGER PRIMARY KEY, done INTEGER NOT NULL DEFAULT 0)")
    conn.execute("BEGIN")
    conn.executemany("INSERT INTO dummy(id) VALUES (?)", [(i,) for i in range(1, 10_001)])
    conn.execute("COMMIT")
    chunks: list[tuple[int, int]] = []
    held: list[float] = []

    def run_chunk(c: sqlite3.Connection, after: int, upto: int) -> None:
        chunks.append((after, upto))
        c.execute("UPDATE dummy SET done = 1 WHERE id > ? AND id <= ?", (after, upto))
        fake_clock.advance(0.6)

    step = MigrationStep(
        "demo",
        high_water=lambda c: int(c.execute("SELECT max(id) FROM dummy").fetchone()[0]),
        run_chunk=run_chunk,
    )

    def write(fn: Callable[[sqlite3.Connection], T]) -> T:
        began = fake_clock.monotonic()
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = fn(conn)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        held.append(fake_clock.monotonic() - began)
        return result

    assert run_migrations(write, [step], clock=fake_clock) == {"demo": False}
    assert chunks == [(0, 2000), (2000, 4000), (4000, 6000)]
    assert meta_get(conn, step.cursor_key) == "6000"
    assert run_migrations(write, [step], clock=fake_clock) == {"demo": True}
    assert len(chunks) == 5 and chunks[-1] == (8000, 10000)
    assert meta_get(conn, step.done_key) == "1" and meta_get(conn, step.cursor_key) is None
    assert conn.execute("SELECT count(*) FROM dummy WHERE done = 0").fetchone()[0] == 0
    assert max(held) <= schema.MIGRATION_TIME_BOX
    assert run_migrations(write, [step], clock=fake_clock) == {"demo": True}
    assert len(chunks) == 5
