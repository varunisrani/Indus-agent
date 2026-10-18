"""Error buckets for the store: one classifier and one table of plain-language copy.

``classify(exc_or_text)`` puts any persistence failure — a live exception or the text of one that
crossed a process boundary — into a ``Bucket``. The order of the checks matters and is fixed:

1. our own typed errors, by their stable ``code`` (``TYPED_BUCKETS``);
2. FTS provenance: corruption that SQLite scopes to a full-text index is ``fts_index``, never
   ``corrupt`` (the transcript itself is fine);
3. SQLite result codes, when the exception carries one;
4. a foreign-key failure (the session row vanished under a live writer);
5. phrases in the text;
6. disk-full and other disk words.

``copy_for(bucket)`` gives the user-facing lines. The English lives here and is mirrored in
``locales/en.yaml`` under ``errors.store.<bucket>`` (a test keeps the two equal).

The small predicates (``is_lock_error`` and friends) are shared by the writer, the schema init
and the journal code so "what counts as busy" is decided in one place.
"""

from __future__ import annotations

import errno
import re
import sqlite3
from dataclasses import dataclass
from enum import StrEnum

from indus.foundation.errors import IndusError

__all__ = [
    "COPY",
    "TYPED_BUCKETS",
    "Bucket",
    "StoreCopy",
    "classify",
    "copy_for",
    "describe",
    "is_disk_io_error",
    "is_fts_scoped_corruption",
    "is_lock_error",
    "is_no_more_rows",
    "primary_code",
]


class Bucket(StrEnum):
    LOCKED = "locked"
    DISK = "disk"
    COMPRESSION = "compression"
    COMPRESSION_CLOSED = "compression_closed"
    TURN_LEASE = "turn_lease"
    CORRUPT = "corrupt"
    FTS_INDEX = "fts_index"
    REPLACED = "replaced"
    SESSION_ROW_MISSING = "session_row_missing"
    UNKNOWN = "unknown"


# Error codes (``IndusError.code``) that decide the bucket on their own. Later store errors
# (compression lock, turn lease, ...) join by adding a row here.
TYPED_BUCKETS: dict[str, Bucket] = {
    "store.locked": Bucket.LOCKED,
    "store.replaced": Bucket.REPLACED,
    "store.corrupt": Bucket.CORRUPT,
    "store.unopenable": Bucket.CORRUPT,
    "store.not_writable": Bucket.DISK,
    "store.compression_busy": Bucket.COMPRESSION,
    "store.compression_closed": Bucket.COMPRESSION_CLOSED,
    "store.turn_lease_lost": Bucket.TURN_LEASE,
}

# SQLite primary result codes (extended codes keep the primary one in the low byte).
_BUSY = sqlite3.SQLITE_BUSY
_LOCKED = sqlite3.SQLITE_LOCKED
_CORRUPT = sqlite3.SQLITE_CORRUPT
_NOTADB = sqlite3.SQLITE_NOTADB
_DISK_CODES = frozenset(
    {
        sqlite3.SQLITE_FULL,
        sqlite3.SQLITE_IOERR,
        sqlite3.SQLITE_READONLY,
        sqlite3.SQLITE_CANTOPEN,
        sqlite3.SQLITE_PERM,
    }
)
_CORRUPT_VTAB = 267  # SQLITE_CORRUPT | (1 << 8): corruption inside a virtual table (FTS)
_CONSTRAINT_FOREIGNKEY = 787  # SQLITE_CONSTRAINT | (3 << 8)

_CORRUPTION_WORDS = ("malformed", "not a database", "database corruption", "corrupt")
_FTS_OBJECT = re.compile(r"\bmessages_(?:fts|trigram)\w*")
_DISK_FULL_WORDS = (
    "no space left on device",
    "database or disk is full",
    "disk full",
    "not enough space",
    "enospc",
)
_DISK_WORDS = ("disk i/o error", "readonly", "read-only", "permission denied", "disk")

# Phrase table, checked in order after the typed and code-based checks.
_PHRASES: tuple[tuple[tuple[str, ...], Bucket], ...] = (
    (("turn lease",), Bucket.TURN_LEASE),
    (("closed by compression",), Bucket.COMPRESSION_CLOSED),
    (("being compressed", "compression lock"), Bucket.COMPRESSION),
    (("was replaced", "store file changed"), Bucket.REPLACED),
    (_CORRUPTION_WORDS, Bucket.CORRUPT),
    (("locked", "busy"), Bucket.LOCKED),
    (("foreign key constraint failed",), Bucket.SESSION_ROW_MISSING),
)


def primary_code(exc: object) -> int | None:
    """The SQLite primary result code an exception carries, or None."""
    code = getattr(exc, "sqlite_errorcode", None)
    return code & 0xFF if isinstance(code, int) else None


def is_lock_error(exc: object) -> bool:
    """Busy or locked: wait and retry, never damage. A known code decides; else the text."""
    code = primary_code(exc)
    if code is not None:
        return code in (_BUSY, _LOCKED)
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def is_disk_io_error(exc: object) -> bool:
    return "disk i/o error" in str(exc).lower()


def is_no_more_rows(exc: object) -> bool:
    """A transient engine error seen on contended appends; retried like busy."""
    return "no more rows available" in str(exc).lower()


def is_fts_scoped_corruption(exc_or_text: object) -> bool:
    """Corruption SQLite attributes to a full-text index.

    A known result code outranks prose: ``SQLITE_CORRUPT_VTAB`` is FTS-scoped, any other code is
    not. Without a code (wrapped text) the message must name one of our FTS objects and carry a
    corruption word.
    """
    if exc_or_text is None:
        return False
    code = getattr(exc_or_text, "sqlite_errorcode", None)
    if isinstance(code, int):
        return code == _CORRUPT_VTAB
    text = str(exc_or_text).lower()
    if not _FTS_OBJECT.search(text):
        return False
    return any(word in text for word in _CORRUPTION_WORDS)


def _is_disk_full(exc_or_text: object, text: str) -> bool:
    if isinstance(exc_or_text, OSError) and exc_or_text.errno == errno.ENOSPC:
        return True
    return any(word in text for word in _DISK_FULL_WORDS)


def classify(exc_or_text: object) -> Bucket:
    """The bucket for a persistence error or its text (``None`` → unknown)."""
    if exc_or_text is None:
        return Bucket.UNKNOWN
    if isinstance(exc_or_text, IndusError):
        typed = TYPED_BUCKETS.get(exc_or_text.code)
        if typed is not None:
            return typed
    if is_fts_scoped_corruption(exc_or_text):
        return Bucket.FTS_INDEX
    code = primary_code(exc_or_text)
    if code in (_BUSY, _LOCKED):
        return Bucket.LOCKED
    if code in (_CORRUPT, _NOTADB):
        return Bucket.CORRUPT
    if getattr(exc_or_text, "sqlite_errorcode", None) == _CONSTRAINT_FOREIGNKEY:
        return Bucket.SESSION_ROW_MISSING
    if code in _DISK_CODES:
        return Bucket.DISK
    text = str(exc_or_text).lower()
    if _is_disk_full(exc_or_text, text):
        return Bucket.DISK
    for words, bucket in _PHRASES:
        if any(word in text for word in words):
            return bucket
    if isinstance(exc_or_text, PermissionError) or any(word in text for word in _DISK_WORDS):
        return Bucket.DISK
    return Bucket.UNKNOWN


# -- copy ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class StoreCopy:
    """What a person is told about a bucket.

    ``action`` is a machine code a GUI can turn into a button: ``retry``, ``doctor``,
    ``restart``, ``free_space``, ``continue`` or ``none``. ``transient`` failures map to HTTP 503
    later, the rest to 500.
    """

    bucket: Bucket
    what: str
    todo: str
    action: str
    transient: bool

    @property
    def keys(self) -> tuple[str, str]:
        base = f"errors.store.{self.bucket.value}"
        return f"{base}.what", f"{base}.todo"


COPY: dict[Bucket, StoreCopy] = {
    copy.bucket: copy
    for copy in (
        StoreCopy(
            Bucket.LOCKED,
            "Another Indus process was busy writing to your session history.",
            "Nothing is damaged. Wait a moment and try again.",
            "retry",
            True,
        ),
        StoreCopy(
            Bucket.DISK,
            "Your session history could not be saved because of a disk problem.",
            "Free some disk space or fix the folder permissions, then try again.",
            "free_space",
            False,
        ),
        StoreCopy(
            Bucket.COMPRESSION,
            "This conversation was being condensed when the message arrived.",
            "Send it again in a few seconds.",
            "retry",
            True,
        ),
        StoreCopy(
            Bucket.COMPRESSION_CLOSED,
            "This conversation continued in a newer segment.",
            "Your client will switch to the newer segment; nothing was lost.",
            "continue",
            False,
        ),
        StoreCopy(
            Bucket.TURN_LEASE,
            "Another process took over this conversation.",
            "Reload the conversation to see its latest state.",
            "restart",
            False,
        ),
        StoreCopy(
            Bucket.CORRUPT,
            "The session history file is damaged, so saving was stopped to protect it.",
            "Quit Indus Agent and run `indus doctor` for recovery steps.",
            "doctor",
            False,
        ),
        StoreCopy(
            Bucket.FTS_INDEX,
            "The search index is damaged; your conversations are safe.",
            "Search uses a slower method until the index is rebuilt. Run `indus doctor`.",
            "doctor",
            False,
        ),
        StoreCopy(
            Bucket.REPLACED,
            "The session history file was swapped while Indus Agent was running.",
            "Unsaved messages are in the sessions folder of your home. Restart Indus Agent.",
            "restart",
            False,
        ),
        StoreCopy(
            Bucket.SESSION_ROW_MISSING,
            "This conversation was deleted while it was still open.",
            "Start a new conversation.",
            "none",
            False,
        ),
        StoreCopy(
            Bucket.UNKNOWN,
            "Your session history could not be saved.",
            "Try again; if it keeps happening, run `indus doctor`.",
            "doctor",
            False,
        ),
    )
}


def copy_for(bucket: Bucket) -> StoreCopy:
    return COPY[bucket]


def describe(exc_or_text: object) -> StoreCopy:
    """``copy_for(classify(exc_or_text))``; never raises."""
    return COPY[classify(exc_or_text)]
