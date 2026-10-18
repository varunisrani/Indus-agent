"""Error buckets and copy (P02-07)."""

from __future__ import annotations

import errno
import sqlite3
from pathlib import Path

import pytest

from indus.foundation import yamlio
from indus.foundation.errors import (
    IndusError,
    StoreCorrupt,
    StoreError,
    StoreLocked,
    StoreNotWritable,
    StoreReplaced,
    StoreUnopenable,
)
from indus.state.errors import (
    COPY,
    Bucket,
    classify,
    copy_for,
    describe,
    is_fts_scoped_corruption,
    is_lock_error,
)

ROOT = Path(__file__).resolve().parents[3]


def _coded(cls: type[sqlite3.Error], text: str, code: int) -> sqlite3.Error:
    exc = cls(text)
    exc.sqlite_errorcode = code  # type: ignore[attr-defined]
    return exc


CASES: list[tuple[object, Bucket]] = [
    # typed errors first
    (StoreLocked("busy for 20s"), Bucket.LOCKED),
    (StoreReplaced("x"), Bucket.REPLACED),
    (StoreCorrupt("x"), Bucket.CORRUPT),
    (StoreUnopenable("x"), Bucket.CORRUPT),
    (StoreNotWritable("x"), Bucket.DISK),
    (IndusError("store.compression_busy", "a compression is running"), Bucket.COMPRESSION),
    (IndusError("store.compression_closed", "adopt the child"), Bucket.COMPRESSION_CLOSED),
    (IndusError("store.turn_lease_lost", "lease gone"), Bucket.TURN_LEASE),
    # a typed error whose code is not mapped falls through to its text
    (StoreError("database is locked"), Bucket.LOCKED),
    # FTS provenance before prose
    (_coded(sqlite3.DatabaseError, "database disk image is malformed", 267), Bucket.FTS_INDEX),
    ("fts5: corruption found reading messages_fts_data", Bucket.FTS_INDEX),
    ("database disk image is malformed (in messages_trigram_idx)", Bucket.FTS_INDEX),
    # result codes
    (_coded(sqlite3.OperationalError, "database is locked", sqlite3.SQLITE_BUSY), Bucket.LOCKED),
    (_coded(sqlite3.OperationalError, "table is locked", 262), Bucket.LOCKED),  # LOCKED_SHAREDCACHE
    (_coded(sqlite3.DatabaseError, "file is not a database", 26), Bucket.CORRUPT),
    (_coded(sqlite3.DatabaseError, "database disk image is malformed", 11), Bucket.CORRUPT),
    (_coded(sqlite3.DatabaseError, "messages_fts malformed", 11), Bucket.CORRUPT),
    (_coded(sqlite3.OperationalError, "disk I/O error", 10), Bucket.DISK),
    (_coded(sqlite3.OperationalError, "database or disk is full", 13), Bucket.DISK),
    (_coded(sqlite3.OperationalError, "attempt to write a readonly database", 8), Bucket.DISK),
    (
        _coded(sqlite3.IntegrityError, "FOREIGN KEY constraint failed", 787),
        Bucket.SESSION_ROW_MISSING,
    ),
    # phrases (no code)
    ("FOREIGN KEY constraint failed", Bucket.SESSION_ROW_MISSING),
    ("database is locked", Bucket.LOCKED),
    ("SQLITE_BUSY: the database file is busy", Bucket.LOCKED),
    ("database disk image is malformed", Bucket.CORRUPT),
    ("file is not a database", Bucket.CORRUPT),
    ("session abc is closed by compression", Bucket.COMPRESSION_CLOSED),
    ("session abc is being compressed", Bucket.COMPRESSION),
    ("the turn lease for abc was lost", Bucket.TURN_LEASE),
    ("the store was replaced while open", Bucket.REPLACED),
    ("disk I/O error", Bucket.DISK),
    ("attempt to write a readonly database", Bucket.DISK),
    (OSError(errno.ENOSPC, "No space left on device"), Bucket.DISK),
    (PermissionError(errno.EACCES, "Permission denied"), Bucket.DISK),
    # nothing known
    ("something odd happened", Bucket.UNKNOWN),
    (ValueError("bad value"), Bucket.UNKNOWN),
    (None, Bucket.UNKNOWN),
]


@pytest.mark.parametrize(("value", "bucket"), CASES)
def test_classifier_table(value: object, bucket: Bucket) -> None:
    assert classify(value) is bucket


def test_table_is_big_enough() -> None:
    assert len(CASES) >= 25


def test_store_locked_text_never_reads_as_damage() -> None:
    from indus.state.writer import ROUTINE, Writer

    writer = Writer.__new__(Writer)
    writer.path = Path("/home/u/.indus/fix-corrupt-disk/state.db")
    err = writer._locked_error(ROUTINE)  # pyright: ignore[reportPrivateUsage]
    message = str(err).replace(str(writer.path), "")
    assert classify(message) is Bucket.LOCKED
    assert classify(err) is Bucket.LOCKED
    assert classify(RuntimeError(message)) not in (Bucket.DISK, Bucket.CORRUPT)


def test_lock_predicate_prefers_the_code() -> None:
    assert is_lock_error(_coded(sqlite3.OperationalError, "whatever", sqlite3.SQLITE_BUSY))
    assert not is_lock_error(_coded(sqlite3.OperationalError, "locked out", 10))
    assert is_lock_error("database table is locked")


def test_fts_predicate() -> None:
    assert is_fts_scoped_corruption(_coded(sqlite3.DatabaseError, "x", 267))
    assert not is_fts_scoped_corruption(_coded(sqlite3.DatabaseError, "messages_fts corrupt", 11))
    assert not is_fts_scoped_corruption("messages_fts: no such column")
    assert not is_fts_scoped_corruption(None)


def test_every_bucket_has_copy() -> None:
    assert set(COPY) == set(Bucket)
    for bucket, copy in COPY.items():
        assert copy.bucket is bucket and copy.what and copy.todo and copy.action
    assert copy_for(Bucket.LOCKED).transient and copy_for(Bucket.COMPRESSION).transient
    assert not copy_for(Bucket.CORRUPT).transient and copy_for(Bucket.CORRUPT).action == "doctor"
    assert describe("database is locked").bucket is Bucket.LOCKED


def test_copy_matches_the_english_catalogue() -> None:
    catalogue = yamlio.load_safe(ROOT / "locales" / "en.yaml")
    store = catalogue["errors"]["store"]
    for bucket, copy in COPY.items():
        what_key, todo_key = copy.keys
        assert what_key == f"errors.store.{bucket.value}.what"
        assert store[bucket.value]["what"] == copy.what
        assert store[bucket.value]["todo"] == copy.todo
