"""Compression handoff rows and live-transcript counters, shared by listing, rewind and compression.

``compression.publish_continuation`` writes the summary ("handoff") rows at the head of a
continuation. They are scaffolding, not something the user typed, so pickers must not show them as
a preview and ``/undo`` must not count them as user turns. Each one carries
``finish_reason = HANDOFF_FINISH``: a user or summary row never has a provider finish reason of its
own, the marker needs no extra column, and it travels with the row through export, import and
cloning. ``not_handoff_sql`` / ``is_handoff`` are the only two places that read it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any

__all__ = ["HANDOFF_FINISH", "is_handoff", "not_handoff_sql", "recount_in"]

HANDOFF_FINISH = "indus.handoff"


def not_handoff_sql(alias: str = "") -> str:
    """A WHERE fragment that keeps every row except handoff rows (``alias`` like ``"m."``)."""
    return f"COALESCE({alias}finish_reason, '') <> '{HANDOFF_FINISH}'"


def is_handoff(row: Mapping[str, Any]) -> bool:
    return row.get("finish_reason") == HANDOFF_FINISH


_TOOL_CALLS_IN_ROW = (
    "CASE WHEN json_valid(tool_calls) AND json_type(tool_calls) = 'array' "
    "THEN json_array_length(tool_calls) ELSE 0 END"
)


def recount_in(conn: sqlite3.Connection, session_id: str) -> tuple[int, int]:
    """Set ``message_count`` / ``tool_call_count`` from the live rows; returns the new pair.

    ``message_count`` counts live rows; ``tool_call_count`` sums the lengths of their
    ``tool_calls`` arrays.
    """
    row = conn.execute(
        f"SELECT COUNT(*), COALESCE(SUM({_TOOL_CALLS_IN_ROW}), 0) FROM messages "
        "WHERE session_id = ? AND active = 1",
        (session_id,),
    ).fetchone()
    messages, calls = int(row[0]), int(row[1])
    conn.execute(
        "UPDATE sessions SET message_count = ?, tool_call_count = ? WHERE id = ?",
        (messages, calls, session_id),
    )
    return messages, calls
