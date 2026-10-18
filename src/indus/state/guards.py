"""Integrity guards: a replaced store file and structural corruption both stop writes at once.

Replaced file (R03.4). Before every write, inside the writer's mutex, ``FileGuard.before_write``
compares the path's ``(st_dev, st_ino)`` and the header ``application_id`` with what was recorded
at open. A backup restore or ``cp`` over a live store fails that check; from then on the handle is
``replaced`` for good: every write raises ``StoreReplaced``, the close skips its checkpoint, and no
repair is ever tried on the new file. Transcript writers divert what they could not save to
``<home>/sessions/<session id>.jsonl`` (``divert_messages``).

Structural corruption (R03.5). A corrupt / not-a-database / "malformed" error that SQLite does not
scope to an FTS index quarantines the handle for the life of the process: the failing write raises
``StoreCorrupt``, later writes fail before touching SQLite, the writer never reopens, the close
skips its checkpoint (and SQLite's own last-connection checkpoint is switched off), and a
process-wide health latch for the path says ``corrupt`` so doctor and listings report it.
FTS-scoped corruption is answered with ``"fts"`` and handled by P02-14's fail-open hook.

The identity probe never opens and closes the store file: ``os.stat`` plus the cached-descriptor
header read from ``connection``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import sqlite3
import threading
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Literal

from indus.foundation.errors import StoreCorrupt, StoreError, StoreReplaced
from indus.state.connection import file_identity, header_application_id
from indus.state.errors import Bucket, classify, is_fts_scoped_corruption

__all__ = [
    "FileGuard",
    "divert_messages",
    "divert_path",
    "health_reason",
    "health_state",
    "is_structural_corruption",
    "mark_corrupt",
    "reset_health",
]

log = logging.getLogger(__name__)

HealthState = Literal["ok", "corrupt"]

# -- process-wide health latch ------------------------------------------------------------------

_health_lock = threading.Lock()
_corrupt_paths: dict[str, str] = {}


def _health_key(path: Path) -> str:
    return str(Path(path).expanduser().resolve(strict=False))


def mark_corrupt(path: Path, reason: object) -> None:
    """Latch ``path`` as corrupt for the rest of this process (idempotent)."""
    key = _health_key(path)
    with _health_lock:
        if key in _corrupt_paths:
            return
        _corrupt_paths[key] = str(reason)
    log.error(
        "the session store %s is damaged (%s); writes to it are stopped until Indus Agent "
        "restarts on a repaired or restored file. Run `indus doctor` for recovery steps.",
        path,
        reason,
    )


def health_state(path: Path) -> HealthState:
    with _health_lock:
        return "corrupt" if _health_key(path) in _corrupt_paths else "ok"


def health_reason(path: Path) -> str | None:
    with _health_lock:
        return _corrupt_paths.get(_health_key(path))


def reset_health(path: Path | None = None) -> None:
    """Forget the latch (tests; a verified in-process recovery)."""
    with _health_lock:
        if path is None:
            _corrupt_paths.clear()
        else:
            _corrupt_paths.pop(_health_key(path), None)


def is_structural_corruption(exc: BaseException) -> bool:
    """Corrupt / not-a-database damage that SQLite does not attribute to an FTS index."""
    return (
        isinstance(exc, sqlite3.DatabaseError)
        and not is_fts_scoped_corruption(exc)
        and classify(exc) is Bucket.CORRUPT
    )


# -- the per-handle guard -----------------------------------------------------------------------


class FileGuard:
    """The file identity recorded at open and the sticky replaced / quarantined states."""

    def __init__(
        self,
        path: Path,
        identity: tuple[int, int] | None,
        application_id: int,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        self.path = path
        self.identity = identity
        self.application_id = application_id
        self._conn = conn
        self.replaced = False
        self.quarantined = False
        self.reason = ""

    @property
    def blocks_reopen(self) -> bool:
        return self.replaced or self.quarantined

    @property
    def skip_close_checkpoint(self) -> bool:
        return self.replaced or self.quarantined

    def attach(self, conn: sqlite3.Connection | None) -> None:
        """The connection whose close-time checkpoint must be switched off on quarantine."""
        self._conn = conn

    def rerecord(self, identity: tuple[int, int] | None, application_id: int) -> None:
        """After a deliberate in-process change of the file (VACUUM, P02-19)."""
        self.identity = identity
        self.application_id = application_id

    def file_was_replaced(self) -> bool:
        current = file_identity(self.path)
        if self.identity is not None and current != self.identity:
            return True
        if not self.application_id:
            return False
        on_disk = header_application_id(self.path)
        # 0 on disk means the stamp still sits in the WAL; only a different non-zero value counts.
        return bool(on_disk) and on_disk != self.application_id

    def _replaced_error(self) -> StoreReplaced:
        return StoreReplaced(
            f"the session store file {self.path} was replaced while this process had it open; "
            "writes to it are stopped",
            hint="restart Indus Agent; unsaved messages were kept under sessions/ in your home",
        )

    def _corrupt_error(self) -> StoreCorrupt:
        return StoreCorrupt(
            f"the session store {self.path} is damaged; writes to it are stopped",
            detail=self.reason,
            hint="quit Indus Agent and run `indus doctor` for recovery steps",
        )

    def before_write(self) -> None:
        """Raise the sticky error, or detect a replacement / a latch set by another handle."""
        if self.replaced:
            raise self._replaced_error()
        if self.quarantined:
            raise self._corrupt_error()
        if self.file_was_replaced():
            self._mark_replaced()
            raise self._replaced_error()
        if health_state(self.path) == "corrupt":
            self._quarantine(health_reason(self.path) or "reported by another handle")
            raise self._corrupt_error()

    def on_write_error(self, exc: BaseException) -> Literal["fts", "raise"]:
        """Decide what a database error from a write means; raises for replaced / corrupt."""
        if not isinstance(exc, sqlite3.DatabaseError):
            return "raise"
        looks_damaged = is_fts_scoped_corruption(exc) or classify(exc) is Bucket.CORRUPT
        if looks_damaged and (self.replaced or self.file_was_replaced()):
            # A new file at the path looks like corruption from here; never repair or latch it.
            self._mark_replaced()
            raise self._replaced_error() from exc
        if is_fts_scoped_corruption(exc):
            return "fts"
        if is_structural_corruption(exc):
            self._quarantine(str(exc))
            raise self._corrupt_error() from exc
        return "raise"

    def _mark_replaced(self) -> None:
        if not self.replaced:
            self.replaced = True
            log.error(
                "the session store file %s was replaced underneath this process; "
                "no further writes go to it",
                self.path,
            )

    def _quarantine(self, reason: str) -> None:
        if self.quarantined:
            return
        self.quarantined = True
        self.reason = reason
        mark_corrupt(self.path, reason)
        self._disable_close_checkpoint()

    def _disable_close_checkpoint(self) -> None:
        """Switch off SQLite's own checkpoint on close so the WAL survives for forensics."""
        conn = self._conn
        flag = getattr(sqlite3, "SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE", None)
        if conn is None or flag is None:
            return
        try:
            conn.setconfig(flag, True)
        except sqlite3.Error:
            log.error(
                "could not switch off the close-time checkpoint for %s", self.path, exc_info=True
            )


# -- transcript diversion -----------------------------------------------------------------------

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def divert_path(home: Path, session_id: str) -> Path:
    """``<home>/sessions/<session id>.jsonl``; refuses ids that are not plain file names."""
    if not _SAFE_ID.fullmatch(session_id) or ".." in session_id:
        raise StoreError(f"cannot divert messages for an unsafe session id: {session_id!r}")
    return home / "sessions" / f"{session_id}.jsonl"


def divert_messages(home: Path, session_id: str, rows: Iterable[Mapping[str, Any]]) -> Path:
    """Append ``rows`` as JSON lines to the divert file (owner-only, synced); returns its path."""
    target = divert_path(home, session_id)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # ASCII JSON: lone surrogates survive as escapes instead of breaking the encoding.
    payload = "".join(json.dumps(dict(row), default=str) + "\n" for row in rows).encode("ascii")
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(target, flags, 0o600)
    try:
        view = memoryview(payload)
        while view:
            view = view[os.write(fd, view) :]
        with contextlib.suppress(OSError):
            os.fsync(fd)
    finally:
        os.close(fd)
    log.warning(
        "diverted %d pending message(s) for %s to %s", payload.count(b"\n"), session_id, target
    )
    return target
