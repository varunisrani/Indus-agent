"""Failure incidents: one alert per distinct failure, a reminder later, silence once acknowledged.

Failures of a job are grouped by a signature of their error text: lower-cased, whitespace runs
collapsed, measured durations (``31.2s``, ``45 seconds``, ``2 h``) replaced, then the first 200
characters hashed together with the job id. Each group is one row in ``cron_incidents`` inside
``<home>/cron/executions.db`` (the execution ledger's file); the stored error is secret-masked and
cut to 500 characters.

States: ``detected`` (seen, no alert out yet) → ``alerted`` (an alert went out at ``alerted_at``)
→ ``resolved`` (the job ran fine afterwards; a repeat of the error re-opens the row as
``detected``) or ``closed`` (acknowledged by the person; stays silent for that signature).

``decide_failure_alert`` is what the runner calls for a failed run: it records the failure and
says whether to alert — first time yes; while ``alerted`` no, until
``cron.failure_repeat_alert_hours`` (6; 0 = every run) have passed since the last alert; ``closed``
never. When the table cannot be
read or written the answer is "alert" (a broken ledger must not swallow failures).
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import re
import sqlite3
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from indus.cron import ports
from indus.foundation.clock import Clock
from indus.foundation.scope import Scope
from indus.safety.redaction import redact_output

__all__ = [
    "MAX_ERROR_CHARS",
    "SIGNATURE_CHARS",
    "STATES",
    "AlertDecision",
    "Incident",
    "IncidentLedger",
    "decide_failure_alert",
    "error_signature",
]

log = logging.getLogger(__name__)

STATES = ("detected", "alerted", "resolved", "closed")
MAX_ERROR_CHARS = 500
SIGNATURE_CHARS = 200
BUSY_TIMEOUT_S = 5.0

type IncidentState = Literal["detected", "alerted", "resolved", "closed"]

_DURATION = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:ms|msecs?|milliseconds?|s|secs?|seconds?|m|mins?|minutes?|h|hrs?|"
    r"hours?)\b"
)
_SPACES = re.compile(r"\s+")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cron_incidents (
    id            TEXT PRIMARY KEY,
    job_id        TEXT NOT NULL,
    signature     TEXT NOT NULL,
    state         TEXT NOT NULL,
    error         TEXT NOT NULL,
    output_path   TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    alerted_at    TEXT,
    resolved_at   TEXT,
    acked_at      TEXT,
    occurrences   INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS cron_incidents_job ON cron_incidents(job_id);
CREATE INDEX IF NOT EXISTS cron_incidents_state ON cron_incidents(state);
"""


def normalise_error(error: str) -> str:
    text = _SPACES.sub(" ", str(error or "")).strip().lower()
    return _DURATION.sub("<t>", text)[:SIGNATURE_CHARS]


def error_signature(job_id: str, error: str) -> str:
    digest = hashlib.sha256(f"{job_id}\n{normalise_error(error)}".encode()).hexdigest()
    return digest[:12]


def incident_id(job_id: str, signature: str) -> str:
    return f"{job_id[:6]}_{signature}"


@dataclass(frozen=True)
class Incident:
    id: str
    job_id: str
    signature: str
    state: IncidentState
    error: str
    output_path: str | None
    first_seen_at: str
    last_seen_at: str
    alerted_at: str | None
    resolved_at: str | None
    acked_at: str | None
    occurrences: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Incident:
        return cls(**{key: row[key] for key in row.keys()})  # noqa: SIM118


@dataclass(frozen=True)
class AlertDecision:
    alert: bool
    incident_id: str | None
    reason: str


class IncidentLedger:
    """The ``cron_incidents`` table of one profile's ``executions.db``."""

    def __init__(self, path: Path, clock: Clock) -> None:
        self.path = path
        self.clock = clock
        self._lock = threading.Lock()

    @classmethod
    def for_scope(cls, scope: Scope) -> IncidentLedger:
        return cls(ports.cron_dir(scope) / "executions.db", scope.clock)

    # -- plumbing ------------------------------------------------------------------------------

    def _ensure_file(self) -> None:
        folder = self.path.parent
        if not folder.exists():
            folder.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                os.chmod(folder, 0o700)
        if not self.path.exists():
            fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)

    @contextlib.contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._ensure_file()
            conn = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_S, isolation_level=None)
            try:
                conn.row_factory = sqlite3.Row
                conn.executescript(_SCHEMA)
                conn.execute("BEGIN IMMEDIATE")
                try:
                    yield conn
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
                conn.execute("COMMIT")
            finally:
                conn.close()

    def _now(self) -> datetime:
        return self.clock.now()

    # -- writes ----------------------------------------------------------------------------------

    def record_failure(
        self, job_id: str, error: str, *, output_path: str | None = None, scope: Scope | None = None
    ) -> tuple[Incident, bool]:
        """Record one failure; ``(incident, reopened_or_new)``. A resolved row re-opens."""
        signature = error_signature(job_id, error)
        ident = incident_id(job_id, signature)
        stored = _stored_error(error, scope)
        now = ports.iso(self._now())
        with self._tx() as conn:
            row = conn.execute("SELECT state FROM cron_incidents WHERE id=?", (ident,)).fetchone()
            fresh = row is None or row["state"] == "resolved"
            if row is None:
                conn.execute(
                    "INSERT INTO cron_incidents (id, job_id, signature, state, error, output_path,"
                    " first_seen_at, last_seen_at) VALUES (?, ?, ?, 'detected', ?, ?, ?, ?)",
                    (ident, job_id, signature, stored, output_path, now, now),
                )
            elif row["state"] == "resolved":
                conn.execute(
                    "UPDATE cron_incidents SET state='detected', alerted_at=NULL, resolved_at=NULL,"
                    " error=?, output_path=?, last_seen_at=?, occurrences=occurrences+1 WHERE id=?",
                    (stored, output_path, now, ident),
                )
            else:
                conn.execute(
                    "UPDATE cron_incidents SET error=?, output_path=?, last_seen_at=?,"
                    " occurrences=occurrences+1 WHERE id=?",
                    (stored, output_path, now, ident),
                )
            current = conn.execute("SELECT * FROM cron_incidents WHERE id=?", (ident,)).fetchone()
        return Incident.from_row(current), fresh

    def mark_alerted(self, ident: str) -> bool:
        """An alert went out now (restarts the reminder window); never re-opens a closed row."""
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE cron_incidents SET state='alerted', alerted_at=? "
                "WHERE id=? AND state != 'closed'",
                (ports.iso(self._now()), ident),
            )
            return cur.rowcount > 0

    def resolve_job(self, job_id: str) -> int:
        """The job ran fine: its open incidents become ``resolved``; how many."""
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE cron_incidents SET state='resolved', resolved_at=? "
                "WHERE job_id=? AND state IN ('detected', 'alerted')",
                (ports.iso(self._now()), job_id),
            )
            return cur.rowcount

    def ack(self, ident: str) -> bool:
        """Acknowledge: the signature stays silent. False for unknown or already closed rows."""
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE cron_incidents SET state='closed', acked_at=? "
                "WHERE id=? AND state != 'closed'",
                (ports.iso(self._now()), ident),
            )
            return cur.rowcount > 0

    def forget_job(self, job_id: str) -> int:
        with self._tx() as conn:
            return conn.execute("DELETE FROM cron_incidents WHERE job_id=?", (job_id,)).rowcount

    # -- reads -----------------------------------------------------------------------------------

    def get(self, ident: str) -> Incident | None:
        if not self.path.exists():
            return None
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM cron_incidents WHERE id=?", (ident,)).fetchone()
        return Incident.from_row(row) if row is not None else None

    def list(self, state: str | None = None, *, job_id: str | None = None) -> list[Incident]:
        """Newest activity first, optionally one state and/or one job."""
        if not self.path.exists():
            return []  # reading never creates the ledger file
        clauses: list[str] = []
        params: list[Any] = []
        if state is not None:
            if state not in STATES:
                return []
            clauses.append("state=?")
            params.append(state)
        if job_id is not None:
            clauses.append("job_id=?")
            params.append(job_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._tx() as conn:
            rows = conn.execute(
                f"SELECT * FROM cron_incidents{where} ORDER BY last_seen_at DESC, id",  # noqa: S608
                params,
            ).fetchall()
        return [Incident.from_row(r) for r in rows]


def _stored_error(error: str, scope: Scope | None) -> str:
    text = str(error or "")
    try:
        text = redact_output(text, force=True, scope=scope)
    except Exception:
        text = "[error text withheld: masking failed]"
    return text[:MAX_ERROR_CHARS]


def _withheld(incident: Incident, now: datetime, repeat_hours: float) -> bool:
    if incident.state == "closed":
        return True
    if incident.state != "alerted" or repeat_hours <= 0:
        return False
    last = ports.parse_instant(incident.alerted_at)
    if last is None:
        return False
    return now - last < timedelta(hours=repeat_hours)


def decide_failure_alert(
    scope: Scope,
    job_id: str,
    error: str,
    *,
    output_path: str | None = None,
    ledger: IncidentLedger | None = None,
) -> AlertDecision:
    """Record a failure and say whether to alert; marks the incident alerted when it says yes.

    The caller that then fails to send calls nothing: the next failure alerts again only after
    the reminder window, so a lost alert is retried at the reminder at the latest.
    """
    book = ledger if ledger is not None else IncidentLedger.for_scope(scope)
    hours = ports.number_setting(scope, "failure_repeat_alert_hours", 6.0)
    try:
        incident, _fresh = book.record_failure(job_id, error, output_path=output_path, scope=scope)
        if _withheld(incident, book.clock.now(), hours):
            why = "acknowledged" if incident.state == "closed" else "already alerted"
            return AlertDecision(False, incident.id, why)
        book.mark_alerted(incident.id)
        reason = "reminder" if incident.state == "alerted" else "new"
        return AlertDecision(True, incident.id, reason)
    except (sqlite3.Error, OSError) as exc:
        log.warning("cron incident ledger unavailable (%s); alerting anyway", type(exc).__name__)
        return AlertDecision(True, None, "incident ledger unavailable")


def resolve_after_success(
    scope: Scope, job_id: str, *, ledger: IncidentLedger | None = None
) -> int:
    """Best effort: a successful run resolves the job's open incidents."""
    book = ledger if ledger is not None else IncidentLedger.for_scope(scope)
    try:
        return book.resolve_job(job_id)
    except (sqlite3.Error, OSError) as exc:
        log.debug("cron incident ledger unavailable: %s", exc)
        return 0
