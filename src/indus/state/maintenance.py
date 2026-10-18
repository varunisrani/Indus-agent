"""Prune, the opt-in startup maintenance, VACUUM and a read-only size snapshot (P02-19).

``prune`` removes ended sessions that went quiet before a cutoff. "Quiet" is the freshest of the
activity stamp, the newest message and the start time — each only when it lies inside the sane
window [2020-01-01, now + 1 day] — plus the close time for rows the startup reaper closed (those
age from the reap, not from their old activity). Spared: pinned rows and the continuations of a
pinned segment, rows under a live turn lease or compression lock, and every compressed-away
segment while a later segment of its conversation stays. Pruned ids get sweep tombstones
(P02-18) and every pass also drops tombstones that aged out.

``auto_maintain(scope)`` is the opt-in startup job (``sessions.auto_prune``). One process at a
time (non-blocking ``.auto-maintenance.lock`` next to the store), once a day
(``meta.last_auto_prune``): prune by ``sessions.retention_days``, close open rows of our own
short-lived sources that were quiet for the whole window (``startup_orphan_reap``; still
resumable), and VACUUM when the gates allow. It never raises; problems become skips (R03.25).

``vacuum`` refuses on a damaged or replaced handle, and — unless forced — while another
in-process generation of the path is open or another process holds the store or a sidecar
(Linux: a scan of ``/proc/*/fd``; a failed scan is an unknown holder). It merges FTS segments
first, checkpoints PASSIVE, runs VACUUM, checkpoints TRUNCATE and re-records the file identity
so the guard does not mistake our own rewrite for a replaced file.
"""

from __future__ import annotations

import contextlib
import logging
import math
import os
import sqlite3
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from indus.foundation.errors import StoreError
from indus.foundation.fsio import FileLock, LockTimeout
from indus.foundation.scope import Scope
from indus.state import connection, deletion, fts, registry, schema, sessions
from indus.state.registry import StoreHandle
from indus.state.writer import ROUTINE

__all__ = [
    "AUTO_INTERVAL_SECONDS",
    "AUTO_LOCK_NAME",
    "OWN_SOURCES",
    "VACUUM_MIN_DAYS",
    "VACUUM_MIN_FREE_RATIO",
    "Holder",
    "MaintenanceReport",
    "PruneReport",
    "StoreStats",
    "VacuumRefused",
    "VacuumReport",
    "auto_maintain",
    "auto_vacuum_due",
    "close_stale_open_in",
    "holders_of",
    "prune",
    "scan_holders",
    "stats",
    "vacuum",
]

log = logging.getLogger(__name__)

DAY: Final = 86_400.0
WINDOW_START: Final = 1_577_836_800.0  # 2020-01-01T00:00:00Z
AUTO_LOCK_NAME: Final = ".auto-maintenance.lock"
AUTO_INTERVAL_SECONDS: Final = DAY
VACUUM_MIN_DAYS: Final = 30
VACUUM_MIN_FREE_RATIO: Final = 0.25
OWN_SOURCES: Final = ("cli", "cron", "acp", "subagent", "tool")
LAST_AUTO_PRUNE: Final = "last_auto_prune"
LAST_VACUUM: Final = "last_vacuum"


@dataclass(frozen=True)
class Holder:
    """Someone with the store open: a pid (``-1`` = unknown) and what it holds."""

    pid: int
    what: str


class VacuumRefused(StoreError):
    """Another generation or process has the store open; VACUUM was not run."""

    default_code = "store.vacuum_refused"

    def __init__(self, message: str, *, holders: list[Holder], hint: str | None = None) -> None:
        super().__init__(message, hint=hint)
        self.holders: list[Holder] = holders


@dataclass
class PruneReport:
    ids: list[str] = field(default_factory=lambda: [])
    spared_pinned: list[str] = field(default_factory=lambda: [])
    spared_guarded: list[str] = field(default_factory=lambda: [])
    dry_run: bool = False

    @property
    def count(self) -> int:
        return len(self.ids)


@dataclass
class VacuumReport:
    size_before: int | None
    size_after: int | None
    holders: list[Holder]
    forced: bool


@dataclass
class MaintenanceReport:
    ran: bool = False
    skipped: str | None = None
    pruned: int = 0
    closed: list[str] = field(default_factory=lambda: [])
    vacuumed: bool = False
    freelist_ratio: float | None = None
    holders: int = 0
    error: str | None = None


@dataclass
class StoreStats:
    page_count: int | None = None
    page_size: int | None = None
    freelist_count: int | None = None
    rows: dict[str, int | None] = field(default_factory=lambda: {})

    @property
    def size_bytes(self) -> int | None:
        if self.page_count is None or self.page_size is None:
            return None
        return self.page_count * self.page_size

    @property
    def freelist_ratio(self) -> float | None:
        if self.page_count is None or self.freelist_count is None:
            return None
        return self.freelist_count / self.page_count if self.page_count else 0.0


def _now(store: StoreHandle) -> float:
    return store.scope.clock.now().timestamp()


# -- prune ----------------------------------------------------------------------------------------


def _in_window(value: object, now: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if WINDOW_START <= number <= now + DAY else None


def _last_active(row: sqlite3.Row, now: float) -> float | None:
    stamps = [row["last_activity_at"], row["last_message_at"], row["started_at"]]
    if row["end_reason"] == sessions.END_STARTUP_ORPHAN_REAP:
        stamps.append(row["ended_at"])
    usable = [s for s in (_in_window(v, now) for v in stamps) if s is not None]
    return max(usable) if usable else None


class _Chains:
    """Full session rows fetched on demand for the continuation walks of one prune pass."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.rows: dict[str, dict[str, Any] | None] = {}

    def row(self, session_id: str) -> dict[str, Any] | None:
        if session_id not in self.rows:
            self.rows[session_id] = deletion.rows_by_id_in(self.conn, [session_id]).get(session_id)
        return self.rows[session_id]

    def under_a_pin(self, session_id: str) -> bool:
        """The row is pinned, or continues (through compression) a pinned segment."""
        current = self.row(session_id)
        seen: set[str] = set()
        while current is not None and str(current["id"]) not in seen:
            if current.get("pinned"):
                return True
            seen.add(str(current["id"]))
            parent_id = current.get("parent_session_id")
            parent = self.row(str(parent_id)) if parent_id else None
            if not deletion.is_continuation(current, parent):
                return False
            current = parent
        return False

    def continuations(self, parent_ids: list[str]) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {p: [] for p in parent_ids}
        for kid in deletion.children_in(self.conn, parent_ids):
            parent = self.row(str(kid["parent_session_id"]))
            if deletion.is_continuation(kid, parent):
                out[str(kid["parent_session_id"])].append(str(kid["id"]))
        return out


def _plan_prune(
    conn: sqlite3.Connection, *, cutoff: float, now: float, source: str | None, reap: bool
) -> PruneReport:
    query = (
        "SELECT s.id, s.end_reason, s.started_at, s.ended_at, s.last_activity_at, "
        "(SELECT MAX(m.timestamp) FROM messages m WHERE m.session_id = s.id "
        "AND m.timestamp BETWEEN ? AND ?) AS last_message_at "
        "FROM sessions s WHERE s.ended_at IS NOT NULL"
    )
    params: list[object] = [WINDOW_START, now + DAY]
    if source is not None:
        query += " AND s.source = ?"
        params.append(source)
    report = PruneReport()
    quiet: list[str] = []
    for row in conn.execute(query, params):
        seen = _last_active(row, now)
        if seen is not None and seen < cutoff:
            quiet.append(str(row["id"]))
    chains = _Chains(conn)
    report.spared_pinned = [sid for sid in quiet if chains.under_a_pin(sid)]
    pinned = set(report.spared_pinned)
    rest = [sid for sid in quiet if sid not in pinned]
    guarded = deletion.guarded_ids_in(conn, rest, now=now, reap=reap)
    report.spared_guarded = [sid for sid in rest if sid in guarded]
    chosen = {sid for sid in rest if sid not in guarded}
    # A compressed-away segment goes only together with every continuation after it.
    compressed = [
        sid
        for sid in chosen
        if (row := chains.row(sid)) is not None
        and row.get("end_reason") in sessions.COMPRESSION_REASONS
    ]
    kids = chains.continuations(compressed)
    changed = True
    while changed:
        changed = False
        for parent_id in compressed:
            if parent_id in chosen and any(k not in chosen for k in kids[parent_id]):
                chosen.discard(parent_id)
                changed = True
    report.ids = [sid for sid in quiet if sid in chosen]
    return report


def prune(
    store: StoreHandle,
    older_than_days: float = 90,
    *,
    source: str | None = None,
    dry_run: bool = False,
) -> PruneReport:
    """Delete (or, with ``dry_run``, only list) ended sessions quiet for ``older_than_days``."""
    if isinstance(older_than_days, bool) or not math.isfinite(float(older_than_days)):
        raise ValueError("older_than_days must be a finite number of days")
    if older_than_days < 0:
        raise ValueError("older_than_days must be >= 0; a negative window matches everything")
    now = _now(store)
    cutoff = now - float(older_than_days) * DAY
    if dry_run:
        with store.read() as conn:
            report = _plan_prune(conn, cutoff=cutoff, now=now, source=source, reap=False)
        report.dry_run = True
        return report

    def run(conn: sqlite3.Connection) -> tuple[PruneReport, list[str]]:
        plan = _plan_prune(conn, cutoff=cutoff, now=now, source=source, reap=True)
        deletion.reap_tombstones_in(conn, now=now)
        removed = deletion.delete_rows_in(
            conn, plan.ids, by="sweep", now=now, cascade_delegates=False
        )
        return plan, removed

    report, removed = store.write(run, budget=ROUTINE)
    deletion.after_delete(store, removed, "prune")
    if removed:
        log.info("pruned %d session(s) quiet for %s days", len(removed), older_than_days)
    return report


def close_stale_open_in(
    conn: sqlite3.Connection, *, older_than_days: float, now: float
) -> list[str]:
    """End open rows of our own sources that were quiet for the whole window."""
    cutoff = now - float(older_than_days) * DAY
    marks = ",".join("?" * len(OWN_SOURCES))
    rows = conn.execute(
        "SELECT s.id, s.end_reason, s.started_at, s.ended_at, s.last_activity_at, "
        "(SELECT MAX(m.timestamp) FROM messages m WHERE m.session_id = s.id "
        "AND m.timestamp BETWEEN ? AND ?) AS last_message_at "
        f"FROM sessions s WHERE s.ended_at IS NULL AND s.pinned = 0 AND s.source IN ({marks}) "
        "AND s.started_at < ?",
        (WINDOW_START, now + DAY, *OWN_SOURCES, cutoff),
    ).fetchall()
    quiet = [
        str(r["id"]) for r in rows if (seen := _last_active(r, now)) is not None and seen < cutoff
    ]
    guarded = deletion.guarded_ids_in(conn, quiet, now=now)
    closed: list[str] = []
    for sid in quiet:
        if sid not in guarded and sessions.end_in(
            conn, sid, sessions.END_STARTUP_ORPHAN_REAP, now=now
        ):
            closed.append(sid)
    return closed


# -- holders --------------------------------------------------------------------------------------


def _watched_files(path: Path) -> Iterator[Path]:
    yield path
    for suffix in ("-wal", "-shm", "-journal"):
        yield path.with_name(path.name + suffix)


def scan_holders(path: Path, *, proc_root: Path = Path("/proc")) -> list[Holder]:
    """Other processes with the store or a sidecar open (Linux; elsewhere none until P17).

    A scan that cannot run reports one holder with pid ``-1``: never assume nobody is there.
    """
    if not sys.platform.startswith("linux"):
        return []
    watched: set[tuple[int, int]] = set()
    for candidate in _watched_files(path):
        try:
            st = os.stat(candidate)
        except FileNotFoundError:
            continue
        except OSError as exc:
            return [Holder(-1, f"cannot stat {candidate}: {exc}")]
        watched.add((st.st_dev, st.st_ino))
    if not watched:
        return []
    try:
        entries = os.listdir(proc_root)
    except OSError as exc:
        return [Holder(-1, f"the holder scan of {proc_root} failed: {exc}")]
    me = os.getpid()
    found: dict[int, Holder] = {}
    for name in entries:
        if not name.isdigit() or int(name) == me:
            continue
        fd_dir = proc_root / name / "fd"
        try:
            descriptors = os.listdir(fd_dir)
        except OSError:
            continue  # exited, or another user's process (it cannot open our 0600 file)
        for fd in descriptors:
            link = fd_dir / fd
            try:
                st = os.stat(link)
            except OSError:
                continue
            if (st.st_dev, st.st_ino) not in watched:
                continue
            try:
                target = os.readlink(link)
            except OSError:
                target = str(path)
            found.setdefault(int(name), Holder(int(name), target))
    return list(found.values())


def holders_of(store: StoreHandle) -> list[Holder]:
    """In-process generations of the path besides ``store``, then foreign processes."""
    me = os.getpid()
    inside = [Holder(me, text) for text in registry.other_generations(store.path, exclude=store)]
    return inside + scan_holders(store.path)


# -- vacuum ---------------------------------------------------------------------------------------


def _logical_size(conn: sqlite3.Connection) -> int | None:
    try:
        pages = int(conn.execute("PRAGMA page_count").fetchone()[0])
        size = int(conn.execute("PRAGMA page_size").fetchone()[0])
    except sqlite3.Error:
        return None
    return pages * size


def _checkpoint(conn: sqlite3.Connection, mode: str) -> None:
    try:
        conn.execute(f"PRAGMA wal_checkpoint({mode})").fetchall()
    except sqlite3.Error as exc:
        log.warning("%s checkpoint around VACUUM failed: %s", mode, exc)


def _describe(found: list[Holder]) -> str:
    return ", ".join(f"pid {h.pid if h.pid >= 0 else '?'} ({h.what})" for h in found[:5])


def vacuum(store: StoreHandle, *, force: bool = False) -> VacuumReport:
    """Rewrite the store to give free pages back; see the module docstring for the gates."""
    store.guard.before_write()  # damaged or replaced handles never rewrite the file
    found = holders_of(store)
    if found and not force:
        raise VacuumRefused(
            f"VACUUM refused: the session store is open elsewhere: {_describe(found)}",
            holders=found,
            hint="close the other Indus Agent processes, or pass force to run anyway",
        )
    try:
        fts.merge(store)
    except Exception:
        log.warning("merging the FTS index before VACUUM failed; continuing", exc_info=True)
    with store.writer.borrow() as conn:
        store.guard.before_write()
        before = _logical_size(conn)
        _checkpoint(conn, "PASSIVE")
        conn.execute("VACUUM")
        _checkpoint(conn, "TRUNCATE")
        after = _logical_size(conn)
        store.guard.rerecord(
            connection.file_identity(store.path), connection.connection_application_id(conn)
        )
    stamp = repr(_now(store))
    store.write(lambda c: schema.meta_set(c, LAST_VACUUM, stamp), budget=ROUTINE)
    log.info("VACUUM of %s: %s -> %s bytes", store.path, before, after)
    return VacuumReport(before, after, found, force)


def auto_vacuum_due(
    *,
    pruned: int,
    last_vacuum: float | None,
    now: float,
    freelist_ratio: float | None,
    holders: int,
) -> bool:
    """The automatic VACUUM gates: rows pruned, 30 days since the last one, more than 25 % of
    pages free, nobody else holding the file."""
    if pruned <= 0 or holders > 0:
        return False
    if last_vacuum is not None and now - last_vacuum < VACUUM_MIN_DAYS * DAY:
        return False
    return freelist_ratio is not None and freelist_ratio > VACUUM_MIN_FREE_RATIO


# -- automatic maintenance ------------------------------------------------------------------------


def _config(scope: Scope, key: str) -> object:
    return None if scope.config is None else scope.config.get(key)


def _stamp(raw: str | None) -> float | None:
    try:
        value = float(raw) if raw is not None else None
    except ValueError:
        return None
    return value if value is not None and math.isfinite(value) else None


def _retention(scope: Scope) -> int | None:
    raw = _config(scope, "sessions.retention_days")
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        log.warning(
            "automatic session maintenance skipped: sessions.retention_days=%r is not a whole "
            "number of days >= 0 (set sessions.auto_prune: false to turn it off)",
            raw,
        )
        return None
    return raw


def _auto_pass(store: StoreHandle, retention: int, report: MaintenanceReport) -> None:
    now = _now(store)
    with store.read() as conn:
        last_prune = _stamp(schema.meta_get(conn, LAST_AUTO_PRUNE))
        last_vacuum = _stamp(schema.meta_get(conn, LAST_VACUUM))
    if last_prune is not None and 0 <= now - last_prune < AUTO_INTERVAL_SECONDS:
        report.skipped = "recent"
        return
    report.pruned = prune(store, retention).count
    report.closed = store.write(
        lambda c: close_stale_open_in(c, older_than_days=retention, now=now), budget=ROUTINE
    )
    if report.pruned > 0 and (last_vacuum is None or now - last_vacuum >= VACUUM_MIN_DAYS * DAY):
        report.freelist_ratio = stats(store).freelist_ratio
        report.holders = len(holders_of(store))
        due = auto_vacuum_due(
            pruned=report.pruned,
            last_vacuum=last_vacuum,
            now=now,
            freelist_ratio=report.freelist_ratio,
            holders=report.holders,
        )
        if due:
            try:
                vacuum(store)
                report.vacuumed = True
            except Exception as exc:  # a refused or failed VACUUM is only a skip here
                log.warning("automatic VACUUM skipped: %s", exc)
    stamp = repr(now)
    store.write(lambda c: schema.meta_set(c, LAST_AUTO_PRUNE, stamp), budget=ROUTINE)
    report.ran = True
    if report.pruned or report.closed:
        log.info(
            "automatic session maintenance: pruned %d, closed %d stale open session(s)%s",
            report.pruned,
            len(report.closed),
            " and ran VACUUM" if report.vacuumed else "",
        )


def auto_maintain(scope: Scope, *, store: StoreHandle | None = None) -> MaintenanceReport:
    """Opt-in startup maintenance; never raises (problems are logged and reported as skips)."""
    report = MaintenanceReport()
    try:
        if _config(scope, "sessions.auto_prune") is not True:
            report.skipped = "disabled"
            return report
        retention = _retention(scope)
        if retention is None:
            report.skipped = "bad_retention"
            return report
        path = store.path if store is not None else connection.default_store_path(scope)
        if not path.exists():
            report.skipped = "no_store"
            return report
        lock = FileLock(path.parent / AUTO_LOCK_NAME, timeout=0)
        try:
            lock.acquire()
        except LockTimeout:
            report.skipped = "busy"
            return report
        try:
            if store is not None:
                _auto_pass(store, retention, report)
            else:
                with registry.shared(scope, path) as handle:
                    _auto_pass(handle, retention, report)
        finally:
            lock.release()
    except Exception as exc:
        log.warning("automatic session maintenance failed: %s", exc, exc_info=True)
        report.error = str(exc) or type(exc).__name__
    return report


# -- stats ----------------------------------------------------------------------------------------


def _pragma_int(conn: sqlite3.Connection, name: str) -> int | None:
    try:
        row = conn.execute(f"PRAGMA {name}").fetchone()
    except sqlite3.Error:
        return None
    return int(row[0]) if row is not None and row[0] is not None else None


def stats(store: StoreHandle) -> StoreStats:
    """A read-only snapshot; every field is read on its own and is None when it failed."""
    out = StoreStats()
    with store.read() as conn:
        out.page_count = _pragma_int(conn, "page_count")
        out.page_size = _pragma_int(conn, "page_size")
        out.freelist_count = _pragma_int(conn, "freelist_count")
        for table in schema.TABLES:
            count: int | None = None
            with contextlib.suppress(sqlite3.Error):
                count = int(conn.execute(f"SELECT COUNT(*) FROM {table.name}").fetchone()[0])
            out.rows[table.name] = count
    return out
