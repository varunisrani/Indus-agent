"""P02-20: read-only browsing, integrity and health — never creates, migrates or checkpoints."""

from __future__ import annotations

import shutil
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from indus.foundation import yamlio
from indus.foundation.errors import StoreUnavailable
from indus.foundation.scope import Scope
from indus.state import browse, fts, guards, journal, registry
from indus.state.errors import Bucket, copy_for
from tests.unit.state._helpers_c import add_message, add_session
from tests.unit.state.conftest import AutoClock

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _fresh_capabilities() -> Iterator[None]:
    fts.reset_capabilities()
    yield
    fts.reset_capabilities()


def _make_store(scope: Scope, rows: int = 3) -> Path:
    with registry.open_unshared(scope) as handle:
        add_session(handle, "s1")
        for n in range(rows):
            add_message(handle, "s1", "user", f"stored row number {n} " + "w" * 200)
        return handle.path


def _signature(path: Path) -> tuple[int, int]:
    st = path.stat()
    return st.st_mtime_ns, st.st_size


def _coded(text: str, code: int) -> sqlite3.OperationalError:
    exc = sqlite3.OperationalError(text)
    exc.sqlite_errorcode = code  # type: ignore[attr-defined]
    return exc


def _failing_connect(monkeypatch: pytest.MonkeyPatch, make: list[BaseException]) -> list[int]:
    real = browse._connect  # pyright: ignore[reportPrivateUsage]
    calls: list[int] = []

    def connect(path: Path) -> sqlite3.Connection:
        calls.append(1)
        if make:
            raise make.pop(0)
        return real(path)

    monkeypatch.setattr(browse, "_connect", connect)
    return calls


# -- opening ------------------------------------------------------------------------------------


def test_missing_store_is_reported_and_never_created(scope: Scope) -> None:
    path = scope.home / "state.db"
    with pytest.raises(StoreUnavailable) as caught:
        browse.open_readonly(scope)
    assert caught.value.kind == "missing"
    report = browse.health(scope)
    assert report.state == "missing" and report.bucket is None and not report.ok
    assert not path.exists()
    assert not any(p.name.startswith("state.db") for p in scope.home.iterdir())


def test_open_reads_without_setting_anything_up(scope: Scope) -> None:
    path = _make_store(scope)
    before = _signature(path)
    with browse.open_readonly(scope) as store:
        assert store.schema_version == 1 and store.journal_mode == "wal"
        assert store.fts and store.trigram and not store.fts_stale
        with store.read() as conn:
            assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 3
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("INSERT INTO meta(key, value) VALUES ('x', 'y')")
    with pytest.raises(sqlite3.ProgrammingError), store.read():
        pass
    assert browse.integrity(scope).ok
    assert browse.health(scope).ok
    assert _signature(path) == before


def test_uninitialised_and_zero_byte_files_are_unavailable(scope: Scope) -> None:
    path = scope.home / "state.db"
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE unrelated(x)")
    raw.commit()
    raw.close()
    before = _signature(path)
    report = browse.health(scope)
    assert (report.state, report.kind) == ("unavailable", "uninitialised")
    assert report.line == " ".join(browse.UNAVAILABLE_COPY["uninitialised"])
    assert _signature(path) == before
    path.write_bytes(b"")
    assert browse.health(scope).kind == "uninitialised"
    assert path.read_bytes() == b""


def test_newer_schema_is_incompatible(scope: Scope) -> None:
    path = _make_store(scope)
    raw = sqlite3.connect(path)
    raw.execute("UPDATE schema_version SET version = 7")
    raw.commit()
    raw.close()
    with pytest.raises(StoreUnavailable) as caught:
        browse.open_readonly(scope)
    assert caught.value.kind == "incompatible"
    report = browse.health(scope)
    assert (report.state, report.kind) == ("unavailable", "incompatible")
    assert "newer version" in report.line


def test_reads_while_a_writer_holds_a_long_transaction(scope: Scope) -> None:
    path = _make_store(scope)
    writer = sqlite3.connect(path, isolation_level=None)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("DELETE FROM messages")
        report = browse.health(scope)
        assert report.ok and report.journal_mode == "wal"
        with browse.open_readonly(scope) as store, store.read() as conn:
            assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 3
    finally:
        writer.execute("ROLLBACK")
        writer.close()


def test_delete_mode_store_reports_its_mode(scope: Scope, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(journal, "linked_sqlite_version", lambda: (3, 45, 1))
    _make_store(scope)
    report = browse.health(scope)
    assert report.ok and report.journal_mode == "delete"


def test_path_with_url_characters_opens_the_right_file(scope: Scope, tmp_path: Path) -> None:
    source = _make_store(scope)
    odd = tmp_path / "we#ird?dir"
    odd.mkdir()
    shutil.copy(source, odd / "state.db")
    (tmp_path / "we").mkdir()
    report = browse.health(scope, odd / "state.db")
    assert report.ok and report.path == (odd / "state.db").resolve()


# -- transient errors ---------------------------------------------------------------------------


def test_io_error_twice_then_success_is_ok(
    scope: Scope, fake_clock: AutoClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_store(scope)
    calls = _failing_connect(
        monkeypatch, [_coded("disk I/O error", 10), _coded("disk I/O error", 10)]
    )
    fake_clock.sleeps.clear()
    assert browse.health(scope).ok
    assert len(calls) == 3 and fake_clock.sleeps == [0.05, 0.05]


def test_io_error_four_times_gives_up(
    scope: Scope, fake_clock: AutoClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_store(scope)
    calls = _failing_connect(monkeypatch, [_coded("disk I/O error", 10) for _ in range(4)])
    fake_clock.sleeps.clear()
    report = browse.health(scope)
    assert (report.state, report.bucket) == ("unavailable", Bucket.DISK)
    assert len(calls) == 4 and fake_clock.sleeps == [0.05] * 3


def test_lock_errors_are_busy_and_not_retried(
    scope: Scope, fake_clock: AutoClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_store(scope)
    calls = _failing_connect(monkeypatch, [_coded("database is locked", 5)])
    fake_clock.sleeps.clear()
    report = browse.health(scope)
    assert (report.state, report.bucket) == ("busy", Bucket.LOCKED)
    assert report.line == f"{copy_for(Bucket.LOCKED).what} {copy_for(Bucket.LOCKED).todo}"
    assert len(calls) == 1 and fake_clock.sleeps == []
    assert guards.health_state(scope.home / "state.db") == "ok"


# -- damage -------------------------------------------------------------------------------------


def _damaged_copy(source: Path, target: Path) -> Path:
    shutil.copy(source, target)
    raw = sqlite3.connect(target)
    try:
        root = int(
            raw.execute("SELECT rootpage FROM sqlite_master WHERE name = 'messages'").fetchone()[0]
        )
        page_size = int(raw.execute("PRAGMA page_size").fetchone()[0])
    finally:
        raw.close()
    with target.open("r+b") as handle:
        handle.seek((root - 1) * page_size)
        handle.write(bytes((n * 37 + 11) % 256 for n in range(page_size)))
    return target


def test_damaged_page_is_corrupt_with_the_copy_line(scope: Scope, tmp_path: Path) -> None:
    source = _make_store(scope, rows=60)
    copy = _damaged_copy(source, tmp_path / "copy.db")
    report = browse.health(scope, copy)
    corrupt = copy_for(Bucket.CORRUPT)
    assert (report.state, report.bucket) == ("corrupt", Bucket.CORRUPT)
    assert report.line == f"{corrupt.what} {corrupt.todo}"
    assert guards.health_state(copy) == "corrupt"
    assert copy.exists() and not list(tmp_path.glob("copy.db.*.bak"))
    assert browse.health(scope).ok  # the original is untouched
    latched = browse.health(scope, copy)  # answered from the latch
    assert latched.state == "corrupt" and latched.detail


def test_garbage_file_is_corrupt_and_left_in_place(scope: Scope) -> None:
    path = scope.home / "state.db"
    path.write_bytes(b"this is not a database at all" * 10)
    report = browse.health(scope)
    assert report.state == "corrupt"
    assert path.read_bytes().startswith(b"this is not")


def test_integrity_full_check_and_latch(scope: Scope, tmp_path: Path) -> None:
    source = _make_store(scope, rows=60)
    clean = browse.integrity(scope, full=True)
    assert clean.ok and clean.structure == ["ok"] and clean.fts is True and not clean.latched
    copy = _damaged_copy(source, tmp_path / "copy.db")
    try:
        report = browse.integrity(scope, copy)
    except sqlite3.DatabaseError:
        pass  # SQLite may refuse the damaged tree outright
    else:
        assert not report.ok and report.structure != ["ok"]
    guards.mark_corrupt(source, "seen by a writer")
    latched = browse.integrity(scope)
    assert latched.latched and not latched.ok and latched.latch_reason == "seen by a writer"


def test_search_index_damage_keeps_the_store_ok(scope: Scope) -> None:
    path = _make_store(scope, rows=40)
    raw = sqlite3.connect(path)
    raw.execute(f"UPDATE {fts.BASE}_data SET block = X'DEADBEEFDEADBEEFDEADBEEFDEADBEEF'")
    raw.commit()
    raw.close()
    report = browse.health(scope)
    assert (report.state, report.bucket) == ("ok", Bucket.FTS_INDEX)
    assert report.line.startswith(copy_for(Bucket.FTS_INDEX).what)
    assert report.fts is False
    assert browse.integrity(scope).fts is False
    assert guards.health_state(path) == "ok"


def test_stale_index_is_reported(scope: Scope) -> None:
    _make_store(scope)
    with registry.open_unshared(scope) as handle:
        handle.write(lambda conn: fts.detach_in(conn, "test", now=1.0))
    report = browse.health(scope)
    assert report.ok and report.fts_stale and report.bucket is Bucket.FTS_INDEX


def test_browse_never_checkpoints_a_live_wal(scope: Scope) -> None:
    handle = registry.open_unshared(scope)
    try:
        add_session(handle, "s1")
        add_message(handle, "s1", "user", "sits in the wal")
        wal = Path(f"{handle.path}-wal")
        size = wal.stat().st_size
        assert size > 0
        main = _signature(handle.path)
        done = threading.Event()

        def look() -> None:
            browse.health(scope)
            browse.integrity(scope, full=True)
            done.set()

        thread = threading.Thread(target=look)
        thread.start()
        thread.join(10)
        assert done.is_set()
        assert wal.stat().st_size == size and _signature(handle.path) == main
    finally:
        handle.close()


def test_unavailable_copy_matches_the_catalogue() -> None:
    catalogue = yamlio.load_safe(ROOT / "locales" / "en.yaml")
    store = catalogue["errors"]["store"]
    for kind, (what, todo) in browse.UNAVAILABLE_COPY.items():
        assert store[kind]["what"] == what and store[kind]["todo"] == todo
