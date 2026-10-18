"""Journal mode policy and pragmas (P02-02)."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from indus.state import connection, journal
from indus.state.journal import apply, apply_pragmas, wal_reset_vulnerable
from tests.unit.state.conftest import PINNED_SQLITE, FakeConnection, locked_error

SAFE = PINNED_SQLITE


def _connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path, isolation_level=None, check_same_thread=False)


def _make_wal_store(path: Path) -> None:
    conn = _connect(path)
    assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    conn.execute("CREATE TABLE t(x)")
    conn.close()


def _mode(conn: sqlite3.Connection) -> str:
    return str(conn.execute("PRAGMA journal_mode").fetchone()[0])


def _errors(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno >= logging.ERROR and r.name == journal.__name__]


def _mountinfo(tmp_path: Path, mount: Path, fstype: str) -> str:
    info = tmp_path / "mountinfo"
    info.write_text(
        "22 1 8:1 / / rw,relatime shared:1 - ext4 /dev/sda1 rw\n"
        f"40 22 0:50 / {mount} rw,relatime - {fstype} share rw\n",
        encoding="utf-8",
    )
    return str(info)


# -- never downgrade ----------------------------------------------------------------------------


def test_existing_wal_with_delete_config_stays_wal_and_logs_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    db = tmp_path / "state.db"
    _make_wal_store(db)
    caplog.set_level(logging.ERROR)
    for _ in range(2):
        conn = _connect(db)
        assert apply(conn, {"journal_mode": "delete"}, db, version=SAFE) == "wal"
        assert _mode(conn) == "wal"
        conn.close()
    assert len(_errors(caplog)) == 1
    assert "keeping WAL" in _errors(caplog)[0].getMessage()


def test_unreadable_header_keeps_wal_and_switches_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "state.db"
    _make_wal_store(db)
    monkeypatch.setattr(journal, "read_header", lambda _path: None)
    fake = FakeConnection(_connect(db))
    assert apply(fake, {"journal_mode": "delete"}, db, version=SAFE) == "wal"
    assert not any("journal_mode=" in s.replace(" ", "").lower() for s in fake.statements)


def test_fresh_store_gets_wal_with_companions(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    conn = _connect(db)
    assert apply(conn, {}, db, version=SAFE, platform="linux") == "wal"
    assert _mode(conn) == "wal"
    assert conn.execute("PRAGMA journal_size_limit").fetchone()[0] == journal.WAL_SIZE_LIMIT
    conn.close()
    assert connection.header_says_wal(connection.read_header(db)) is True
    connection.release_header_fd(db)


def test_configured_delete_on_a_fresh_store(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    conn = _connect(db)
    assert apply(conn, {"journal_mode": "delete"}, db, version=SAFE) == "delete"
    assert _mode(conn) == "delete"
    conn.close()


def test_invalid_configured_mode_means_wal(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    conn = _connect(db)
    assert apply(conn, {"journal_mode": "turbo"}, db, version=SAFE, platform="linux") == "wal"
    conn.close()
    assert journal.configured_mode(None) == "wal"
    assert journal.configured_mode({"journal_mode": " DELETE "}) == "delete"


# -- refusing to enable WAL ---------------------------------------------------------------------


@pytest.mark.parametrize("fstype", ["virtiofs", "fuse.virtiofs", "9p"])
def test_cross_vm_mount_gets_delete_for_a_fresh_store(tmp_path: Path, fstype: str) -> None:
    db = tmp_path / "state.db"
    info = _mountinfo(tmp_path, tmp_path, fstype)
    conn = _connect(db)
    assert apply(conn, {}, db, version=SAFE, mountinfo=info) == "delete"
    assert _mode(conn) == "delete"
    conn.close()


def test_cross_vm_mount_keeps_an_existing_wal_store(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    _make_wal_store(db)
    info = _mountinfo(tmp_path, tmp_path, "virtiofs")
    conn = _connect(db)
    assert apply(conn, {}, db, version=SAFE, mountinfo=info) == "wal"
    conn.close()


def test_fs_type_picks_the_longest_mount(tmp_path: Path) -> None:
    info = _mountinfo(tmp_path, tmp_path, "9p")
    assert journal.fs_type(tmp_path / "state.db", info) == "9p"
    assert journal.fs_type(Path("/usr/lib"), info) == "ext4"
    assert journal.fs_type(tmp_path, str(tmp_path / "missing")) == ""


@pytest.mark.parametrize(
    ("version", "vulnerable"),
    [
        ((3, 51, 2), True),
        ((3, 51, 3), False),
        ((3, 52, 0), False),
        ((3, 50, 7), False),
        ((3, 50, 6), True),
        ((3, 44, 6), False),
        ((3, 44, 5), True),
        ((3, 45, 1), True),
        ((3, 7, 0), True),
        ((3, 6, 23), False),
        ((4, 0, 0), False),
    ],
)
def test_wal_reset_bug_gate(version: tuple[int, int, int], vulnerable: bool) -> None:
    assert wal_reset_vulnerable(version) is vulnerable


def test_vulnerable_sqlite_uses_delete_for_new_and_keeps_existing_wal(tmp_path: Path) -> None:
    fresh = tmp_path / "fresh.db"
    conn = _connect(fresh)
    assert apply(conn, {}, fresh, version=(3, 51, 2)) == "delete"
    conn.close()
    old = tmp_path / "old.db"
    _make_wal_store(old)
    conn = _connect(old)
    assert apply(conn, {}, old, version=(3, 51, 2)) == "wal"
    assert _mode(conn) == "wal"
    conn.close()


# -- fallbacks ----------------------------------------------------------------------------------


def test_locking_protocol_error_falls_back_to_delete_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.ERROR)
    for name in ("a.db", "a.db"):
        db = tmp_path / name
        fake = FakeConnection(_connect(db))
        fake.fail(r"journal_mode\s*=\s*WAL", lambda: sqlite3.OperationalError("locking protocol"))
        assert apply(fake, {}, db, version=SAFE, platform="linux") == "delete"
        assert _mode(fake.real) == "delete"
        fake.real.close()
    assert len(_errors(caplog)) == 1


def test_unrelated_error_from_the_wal_switch_propagates(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    fake = FakeConnection(_connect(db))
    fake.fail(r"journal_mode\s*=\s*WAL", lambda: sqlite3.OperationalError("out of memory"))
    with pytest.raises(sqlite3.OperationalError, match="out of memory"):
        apply(fake, {}, db, version=SAFE, platform="linux")
    fake.real.close()


class _SilentRefusal(FakeConnection):
    def execute(self, sql: str, parameters: Any = (), /) -> Any:
        if sql.replace(" ", "").upper() == "PRAGMAJOURNAL_MODE=WAL":
            return self.real.execute("SELECT 'delete'")
        return super().execute(sql, parameters)


def test_silent_refusal_falls_back_to_delete(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    fake = _SilentRefusal(_connect(db))
    assert apply(fake, {}, db, version=SAFE, platform="linux") == "delete"
    fake.real.close()


def test_non_wal_switch_uses_zero_busy_timeout(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    conn = _connect(db)
    conn.execute("PRAGMA busy_timeout=1234")
    fake = FakeConnection(conn)
    journal.set_mode_no_wait(fake, "DELETE")
    switch = next(i for i, s in enumerate(fake.statements) if "journal_mode=DELETE" in s)
    assert fake.statements[switch - 1] == "PRAGMA busy_timeout=0"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 1234
    conn.close()


def test_delete_switch_blocked_by_another_opener_leaves_mode(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    fake = FakeConnection(_connect(db))
    fake.fail(r"journal_mode=DELETE", locked_error)
    assert apply(fake, {}, db, version=(3, 45, 1)) == "delete"
    fake.real.close()


# -- pragmas ------------------------------------------------------------------------------------


def test_bad_pragma_value_is_skipped_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    conn = _connect(tmp_path / "p.db")
    before = conn.execute("PRAGMA cache_size").fetchone()[0]
    caplog.set_level(logging.WARNING)
    apply_pragmas(conn, {"cache_size": "lots", "mmap_size": 0}, platform="linux")
    assert conn.execute("PRAGMA cache_size").fetchone()[0] == before
    assert any("cache_size" in r.getMessage() for r in caplog.records)
    conn.close()


def test_int_pragmas_from_both_spellings(tmp_path: Path) -> None:
    conn = _connect(tmp_path / "p.db")
    apply_pragmas(
        conn,
        {"pragmas": {"cache_size": -4000, "temp_store": 1}, "cache_size": -8000},
        platform="linux",
    )
    assert conn.execute("PRAGMA cache_size").fetchone()[0] == -8000
    assert conn.execute("PRAGMA temp_store").fetchone()[0] == 1
    conn.close()


@pytest.mark.parametrize(
    ("raw", "level"),
    [("off", 0), ("NORMAL", 1), ("Full", 2), ("extra", 3), (1, 1), ("3", 3), (False, 0)],
)
def test_synchronous_values(raw: object, level: int) -> None:
    assert journal.synchronous_level(raw) == level


@pytest.mark.parametrize("raw", ["fast", 7, True, "-1", None])
def test_synchronous_rejects(raw: object) -> None:
    assert journal.synchronous_level(raw) is None


def test_synchronous_applied_on_linux(tmp_path: Path) -> None:
    conn = _connect(tmp_path / "p.db")
    apply_pragmas(conn, {"synchronous": "normal"}, platform="linux")
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
    conn.close()


def test_macos_forces_full_and_fullfsync(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    conn = _connect(tmp_path / "p.db")
    caplog.set_level(logging.WARNING)
    apply_pragmas(conn, {"synchronous": "normal"}, platform="darwin")
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2
    assert conn.execute("PRAGMA checkpoint_fullfsync").fetchone()[0] == 1
    assert any("macOS" in r.getMessage() for r in caplog.records)
    conn.close()


def test_macos_barriers_on_every_wal_activation(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    conn = _connect(db)
    conn.execute("PRAGMA synchronous=1")
    assert apply(conn, {}, db, version=SAFE, platform="darwin") == "wal"
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2
    assert conn.execute("PRAGMA checkpoint_fullfsync").fetchone()[0] == 1
    conn.close()
