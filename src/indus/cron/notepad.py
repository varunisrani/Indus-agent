"""Per-job notepad: durable key/value notes a job carries between runs (P14-17).

Stored in the ``job_notes`` table of ``executions.db``. Limits: key 1–128 characters, value at
most 16 KiB (UTF-8 bytes), and at most 64 KiB per job counting keys and values. A write that
would break a limit raises ``NotepadLimit`` and changes nothing.

Each run's prompt gets ``render_block(job_id)`` (P14-10); a job without notes gets "" so its
prompt stays byte-identical. The agent writes notes through ``indus cron notepad <job>
get|set|unset|list`` (P14-19) via the terminal; there is no model tool. Removing a job clears its
notes (``CronContext`` registers ``clear`` as a remove hook).
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from indus.cron.errors import NotepadLimit
from indus.cron.ledger import ExecutionsDB, iso
from indus.foundation.clock import Clock

__all__ = [
    "MAX_JOB_BYTES",
    "MAX_KEY_CHARS",
    "MAX_VALUE_BYTES",
    "NoteEntry",
    "Notepad",
]

log = logging.getLogger(__name__)

MAX_VALUE_BYTES = 16 * 1024
MAX_JOB_BYTES = 64 * 1024
MAX_KEY_CHARS = 128


@dataclass(frozen=True)
class NoteEntry:
    key: str
    value: str
    updated_at: datetime


def _size(text: str) -> int:
    return len(text.encode("utf-8"))


class Notepad:
    def __init__(self, db: ExecutionsDB, *, clock: Clock | None = None) -> None:
        self.db = db
        self.clock = clock if clock is not None else db.clock

    @staticmethod
    def _check(key: str, value: str) -> None:
        if not key or not key.strip():
            raise NotepadLimit("a notepad key cannot be empty")
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in key):
            raise NotepadLimit("a notepad key cannot hold control characters or line breaks")
        if len(key) > MAX_KEY_CHARS:
            raise NotepadLimit(f"a notepad key has at most {MAX_KEY_CHARS} characters")
        if _size(value) > MAX_VALUE_BYTES:
            raise NotepadLimit(
                f"a notepad value has at most {MAX_VALUE_BYTES} bytes; this one has {_size(value)}"
            )

    def set(self, job_id: str, key: str, value: str) -> NoteEntry:
        self._check(key, value)
        now = self.clock.now()

        def apply(conn: sqlite3.Connection) -> None:
            row = conn.execute(
                "SELECT COALESCE(SUM(LENGTH(CAST(key AS BLOB)) + LENGTH(CAST(value AS BLOB))), 0) "
                "FROM job_notes WHERE job_id = ? AND key <> ?",
                (job_id, key),
            ).fetchone()
            others = int(row[0])
            total = others + _size(key) + _size(value)
            if total > MAX_JOB_BYTES:
                raise NotepadLimit(
                    f"the notepad of job {job_id} would hold {total} bytes (at most "
                    f"{MAX_JOB_BYTES}); unset keys it no longer needs first"
                )
            conn.execute(
                """INSERT INTO job_notes (job_id, key, value, updated_at) VALUES (?, ?, ?, ?)
                   ON CONFLICT (job_id, key)
                   DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
                (job_id, key, value, iso(now)),
            )

        self.db.write(apply)
        return NoteEntry(key, value, now)

    def get(self, job_id: str, key: str) -> str | None:
        def query(conn: sqlite3.Connection) -> str | None:
            row = conn.execute(
                "SELECT value FROM job_notes WHERE job_id = ? AND key = ?", (job_id, key)
            ).fetchone()
            return None if row is None else str(row[0])

        return self.db.read(query)

    def unset(self, job_id: str, key: str) -> bool:
        def apply(conn: sqlite3.Connection) -> bool:
            cur = conn.execute("DELETE FROM job_notes WHERE job_id = ? AND key = ?", (job_id, key))
            return cur.rowcount > 0

        return self.db.write(apply)

    def list(self, job_id: str) -> list[NoteEntry]:
        def query(conn: sqlite3.Connection) -> list[NoteEntry]:
            rows = conn.execute(
                "SELECT key, value, updated_at FROM job_notes WHERE job_id = ? ORDER BY key",
                (job_id,),
            ).fetchall()
            return [
                NoteEntry(str(r[0]), str(r[1]), datetime.fromisoformat(str(r[2]))) for r in rows
            ]

        return self.db.read(query)

    def clear(self, job_id: str) -> int:
        def apply(conn: sqlite3.Connection) -> int:
            return conn.execute("DELETE FROM job_notes WHERE job_id = ?", (job_id,)).rowcount

        return self.db.write(apply)

    def render_block(self, job_id: str) -> str:
        """The prompt block for ``job_id``; "" when it has no notes or they cannot be read."""
        try:
            entries = self.list(job_id)
        except (sqlite3.Error, OSError):
            log.warning("could not read the notepad of cron job %s", job_id, exc_info=True)
            return ""
        if not entries:
            return ""
        lines = [
            "## Notes kept from earlier runs of this job",
            "These notes persist between runs. Change them with the terminal: "
            f"`indus cron notepad {job_id} set <key> <value>` (also get, unset, list).",
            "",
        ]
        for entry in entries:
            value = entry.value.replace("\r\n", "\n").replace("\n", "\n    ")
            lines.append(f"- {entry.key}: {value}")
        return "\n".join(lines) + "\n"
