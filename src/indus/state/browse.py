"""Read-only browsing: look at a store without ever changing it (R03.16).

``open_readonly(scope, path=None)`` opens the store file through a ``mode=ro`` URI (the path
percent-encoded, so ``?`` or ``#`` in a home path cannot name another file) with a 5 s busy
timeout and does nothing else: no schema init, no journal switch, no quarantine of a bad file,
no migration, no checkpoint, and a missing file is reported, never created. FTS presence is
probed with SELECTs only. A read-only reader can get a transient ``disk I/O error`` while a
writer resets its WAL; that is retried 3 times, 50 ms apart on the scope's clock. Lock errors are
not retried: the busy timeout already waited. A file that was never set up, or whose schema is
newer than ours, raises ``StoreUnavailable`` (kind ``uninitialised`` / ``incompatible``).

``integrity(scope)`` runs ``PRAGMA quick_check`` (``integrity_check`` with ``full=True``) and the
FTS check on that read-only connection and reports the process health latch. ``health(scope)``
sums everything up for doctor and listings as ``ok``, ``busy``, ``corrupt``, ``missing`` or
``unavailable``, with the error bucket and its plain-language copy line. Damage found by a read
latches the path corrupt for this process (``guards.mark_corrupt``), so its writers stop too.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from indus.foundation.errors import StoreUnavailable
from indus.foundation.scope import Scope
from indus.state import connection, fts, guards
from indus.state.errors import (
    Bucket,
    StoreCopy,
    classify,
    copy_for,
    is_disk_io_error,
    is_fts_scoped_corruption,
    is_lock_error,
)
from indus.state.readpool import read_only_uri
from indus.state.schema import SCHEMA_VERSION, read_version
from indus.state.writer import blocking_sleep

__all__ = [
    "IO_RETRIES",
    "IO_RETRY_PAUSE",
    "READ_BUSY_SECONDS",
    "UNAVAILABLE_COPY",
    "HealthReport",
    "HealthState",
    "IntegrityReport",
    "ReadonlyStore",
    "health",
    "integrity",
    "open_readonly",
]

log = logging.getLogger(__name__)

READ_BUSY_SECONDS = 5.0
IO_RETRIES = 3
IO_RETRY_PAUSE = 0.05

HealthState = Literal["ok", "busy", "corrupt", "missing", "unavailable"]

# What a person is told about a store that cannot be read as asked (mirrored in
# locales/en.yaml under errors.store.<kind>).
UNAVAILABLE_COPY: dict[str, tuple[str, str]] = {
    "uninitialised": (
        "The session history file exists but was never set up.",
        "Start Indus Agent once to set it up, or move the file away if it is not yours.",
    ),
    "incompatible": (
        "The session history was written by a newer version of Indus Agent.",
        "Update Indus Agent to read it.",
    ),
}


def _connect(path: Path) -> sqlite3.Connection:
    """A read-only connection (tests wrap this to inject errors)."""
    conn = sqlite3.connect(
        read_only_uri(path),
        uri=True,
        timeout=READ_BUSY_SECONDS,
        isolation_level=None,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    return conn


@dataclass
class ReadonlyStore:
    """A read-only view of one store file; close it (or use ``with``)."""

    path: Path
    conn: sqlite3.Connection
    schema_version: int
    journal_mode: str
    fts: bool
    trigram: bool
    fts_stale: bool
    fts_damage: str | None = None
    _closed: bool = field(default=False, repr=False)

    @contextlib.contextmanager
    def read(self) -> Generator[sqlite3.Connection]:
        if self._closed:
            raise sqlite3.ProgrammingError(f"the read-only view of {self.path} is closed")
        yield self.conn

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            with contextlib.suppress(sqlite3.Error):
                self.conn.close()

    def __enter__(self) -> ReadonlyStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _target(scope: Scope, path: Path | None) -> Path:
    raw = Path(path) if path is not None else connection.default_store_path(scope)
    return raw.expanduser().resolve(strict=False)


def _open_once(target: Path) -> ReadonlyStore:
    conn = _connect(target)
    try:
        version = read_version(conn)
        if version is None:
            raise StoreUnavailable(
                f"the session store {target} was never set up",
                kind="uninitialised",
                hint="start Indus Agent once with this profile",
            )
        if version > SCHEMA_VERSION:
            raise StoreUnavailable(
                f"the session store {target} has schema version {version}, newer than this "
                f"build reads ({SCHEMA_VERSION})",
                kind="incompatible",
                hint="update Indus Agent",
            )
        mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        usable, damage = fts.probe(conn)
        return ReadonlyStore(
            path=target,
            conn=conn,
            schema_version=version,
            journal_mode=mode,
            fts=fts.BASE in usable,
            trigram=fts.TRIGRAM in usable,
            fts_stale=fts.is_stale(conn),
            fts_damage=None if damage is None else str(damage),
        )
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            conn.close()
        raise


def open_readonly(
    scope: Scope, path: Path | None = None, *, sleep: Callable[[float], None] | None = None
) -> ReadonlyStore:
    """A read-only view of the store of ``scope`` (or ``path``); see the module docstring."""
    target = _target(scope, path)
    if not target.is_file():
        raise StoreUnavailable(
            f"there is no session store at {target} yet",
            kind="missing",
            hint="it is created the first time Indus Agent saves a conversation",
        )
    pause = sleep if sleep is not None else blocking_sleep(scope.clock)
    retries = 0
    while True:
        try:
            return _open_once(target)
        except sqlite3.OperationalError as exc:
            if retries < IO_RETRIES and is_disk_io_error(exc) and not is_lock_error(exc):
                retries += 1
                pause(IO_RETRY_PAUSE)
                continue
            raise


# -- integrity ----------------------------------------------------------------------------------


@dataclass
class IntegrityReport:
    path: Path
    ok: bool
    structure: list[str]
    fts: bool | None
    fts_stale: bool
    latched: bool
    latch_reason: str | None = None


def _canonical_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND sql NOT LIKE 'CREATE VIRTUAL%'"
    ).fetchall()
    families = (fts.BASE + "_", fts.TRIGRAM + "_")
    return [str(r[0]) for r in rows if not str(r[0]).startswith(families)]


def _structure(conn: sqlite3.Connection, *, full: bool) -> tuple[list[str], bool]:
    """Lines of the SQLite check (``["ok"]`` when clean), and whether an FTS table broke it.

    A damaged FTS table makes the whole-file check raise; the canonical tables are then
    checked one by one so the transcript's own state is still known."""
    pragma = "integrity_check" if full else "quick_check"
    try:
        return [str(r[0]) for r in conn.execute(f"PRAGMA {pragma}").fetchall()], False
    except sqlite3.DatabaseError as exc:
        if not is_fts_scoped_corruption(exc):
            raise
    problems: list[str] = []
    for table in _canonical_tables(conn):
        lines = [str(r[0]) for r in conn.execute(f'PRAGMA {pragma}("{table}")').fetchall()]
        problems.extend(line for line in lines if line != "ok")
    return problems or ["ok"], True


def _fts_check(store: ReadonlyStore) -> bool | None:
    if store.fts_damage is not None:
        return False
    return fts.integrity_ok(store.conn)


def integrity(scope: Scope, path: Path | None = None, *, full: bool = False) -> IntegrityReport:
    """Run the SQLite and FTS checks on a read-only connection (raises what opening raises)."""
    target = _target(scope, path)
    latched = guards.health_state(target) == "corrupt"
    with open_readonly(scope, target) as store:
        lines, fts_broke = _structure(store.conn, full=full)
        fts_ok = False if fts_broke else _fts_check(store)
        return IntegrityReport(
            path=target,
            ok=lines == ["ok"] and not latched,
            structure=lines,
            fts=fts_ok,
            fts_stale=store.fts_stale,
            latched=latched,
            latch_reason=guards.health_reason(target),
        )


# -- health -------------------------------------------------------------------------------------


@dataclass
class HealthReport:
    path: Path
    state: HealthState
    bucket: Bucket | None = None
    copy: StoreCopy | None = None
    line: str = ""
    detail: str = ""
    kind: str | None = None
    journal_mode: str | None = None
    schema_version: int | None = None
    fts: bool | None = None
    trigram: bool | None = None
    fts_stale: bool = False
    sqlite_version: str = sqlite3.sqlite_version

    @property
    def ok(self) -> bool:
        return self.state == "ok"


def _with_bucket(report: HealthReport, bucket: Bucket) -> HealthReport:
    copy = copy_for(bucket)
    report.bucket = bucket
    report.copy = copy
    report.line = f"{copy.what} {copy.todo}"
    return report


def _unavailable(report: HealthReport, exc: StoreUnavailable) -> HealthReport:
    report.kind = exc.kind
    report.detail = str(exc)
    if exc.kind == "missing":
        report.state = "missing"
        return report
    report.state = "unavailable"
    what, todo = UNAVAILABLE_COPY.get(exc.kind, ("", ""))
    report.line = f"{what} {todo}".strip()
    return report


def _from_error(report: HealthReport, exc: BaseException) -> HealthReport:
    bucket = classify(exc)
    report.detail = str(exc)
    if bucket is Bucket.LOCKED:
        report.state = "busy"
    elif bucket is Bucket.CORRUPT:
        report.state = "corrupt"
        guards.mark_corrupt(report.path, exc)
    else:
        report.state = "unavailable"
    return _with_bucket(report, bucket)


def health(scope: Scope, path: Path | None = None) -> HealthReport:
    """``ok / busy / corrupt / missing / unavailable`` for the store, with bucket and copy."""
    target = _target(scope, path)
    report = HealthReport(path=target, state="ok")
    if guards.health_state(target) == "corrupt":
        report.state = "corrupt"
        report.detail = guards.health_reason(target) or ""
        return _with_bucket(report, Bucket.CORRUPT)
    try:
        with open_readonly(scope, target) as store:
            report.journal_mode = store.journal_mode
            report.schema_version = store.schema_version
            report.fts = store.fts
            report.trigram = store.trigram
            report.fts_stale = store.fts_stale
            lines, fts_broke = _structure(store.conn, full=False)
            if lines != ["ok"]:
                report.state = "corrupt"
                report.detail = "; ".join(lines[:5])
                guards.mark_corrupt(target, report.detail)
                return _with_bucket(report, Bucket.CORRUPT)
            fts_ok = False if fts_broke else _fts_check(store)
    except StoreUnavailable as exc:
        return _unavailable(report, exc)
    except (sqlite3.Error, OSError) as exc:
        return _from_error(report, exc)
    if fts_ok is False or report.fts_stale:
        report.detail = "the search index is out of date or damaged"
        return _with_bucket(report, Bucket.FTS_INDEX)
    return report
