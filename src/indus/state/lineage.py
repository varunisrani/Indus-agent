"""Compression lineages: one conversation stored as root → … → tip segments.

A hop of a lineage is a *continuation*: a child row whose parent ended with a compression reason
and whose ``sessions.child_kind`` is ``continuation`` (branches, resets, delegates and tool runs
are their own conversations). The walkers here read through whatever connection they are given,
so a writer can resolve the tip inside its own transaction.

- ``root_in`` / ``ancestors_in`` walk parent links back over continuation hops.
- ``tip_in`` walks forward over the one live continuation of each segment. "Live" means still
  open or itself rotated; a closed sibling (a stale writer that created and closed a row) loses to
  a live one. Two live continuations of one parent cannot be told apart, so the walk fails closed
  with ``AmbiguousChain`` instead of guessing.
- ``lineage_ids_in`` is root … tip in order; ``members_in`` is every segment reachable from the
  root, used where a fork must not block the operation.

No step cap: a chain of thousands of segments resolves, and a cycle in the parent links stops at
the first repeated id.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any

from indus.foundation.errors import StoreError
from indus.state import sessions

__all__ = [
    "AmbiguousChain",
    "ancestors_in",
    "continuations_in",
    "forward_in",
    "is_continuation",
    "lineage_ids_in",
    "members_in",
    "root_in",
    "tip_in",
]

_COLUMNS = "id, parent_session_id, source, model_config, ended_at, end_reason, started_at"


class AmbiguousChain(StoreError):
    """A compressed segment has more than one live continuation; nobody can tell which is real."""

    default_code = "store.ambiguous_chain"

    def __init__(
        self, message: str, *, session_id: str = "", candidates: Sequence[str] = ()
    ) -> None:
        super().__init__(message)
        self.session_id: str = session_id
        self.candidates: list[str] = list(candidates)


def _row_in(conn: sqlite3.Connection, session_id: str) -> dict[str, Any] | None:
    row = conn.execute(f"SELECT {_COLUMNS} FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return dict(row) if row is not None else None


def is_continuation(child: Mapping[str, Any], parent: Mapping[str, Any] | None) -> bool:
    """Is ``child`` the compression continuation of ``parent``?"""
    if parent is None or child.get("parent_session_id") != parent.get("id"):
        return False
    if parent.get("end_reason") not in sessions.COMPRESSION_REASONS:
        return False
    return sessions.child_kind(child, parent) == "continuation"


def ancestors_in(conn: sqlite3.Connection, session_id: str) -> list[str]:
    """``[root, …, session_id]``: the segments before this one, oldest first."""
    chain = [session_id]
    seen = {session_id}
    row = _row_in(conn, session_id)
    while row is not None:
        parent_id = row.get("parent_session_id")
        if not parent_id or parent_id in seen:
            break
        parent = _row_in(conn, str(parent_id))
        if not is_continuation(row, parent):
            break
        assert parent is not None
        seen.add(str(parent_id))
        chain.append(str(parent_id))
        row = parent
    chain.reverse()
    return chain


def root_in(conn: sqlite3.Connection, session_id: str) -> str:
    """The first segment of the conversation ``session_id`` belongs to (itself when none)."""
    return ancestors_in(conn, session_id)[0]


def continuations_in(conn: sqlite3.Connection, session_id: str) -> list[dict[str, Any]]:
    """Every continuation child of a segment (empty unless it ended with a compression reason)."""
    parent = _row_in(conn, session_id)
    if parent is None or parent.get("end_reason") not in sessions.COMPRESSION_REASONS:
        return []
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM sessions WHERE parent_session_id = ? ORDER BY started_at, id",
        (session_id,),
    ).fetchall()
    return [child for child in map(dict, rows) if is_continuation(child, parent)]


def _is_live(row: Mapping[str, Any]) -> bool:
    return row.get("ended_at") is None or row.get("end_reason") == sessions.END_COMPRESSION


def _next_in(conn: sqlite3.Connection, session_id: str) -> str | None:
    found = continuations_in(conn, session_id)
    if not found:
        return None
    live = [row for row in found if _is_live(row)]
    pool = live if live else found
    if len(pool) > 1:
        ids = [str(row["id"]) for row in pool]
        raise AmbiguousChain(
            f"session {session_id} has {len(ids)} continuations: {', '.join(ids)}",
            session_id=session_id,
            candidates=ids,
        )
    return str(pool[0]["id"])


def forward_in(conn: sqlite3.Connection, session_id: str) -> list[str]:
    """``[session_id, …, tip]``: this segment and the ones after it."""
    chain = [session_id]
    seen = {session_id}
    current = session_id
    while True:
        following = _next_in(conn, current)
        if following is None or following in seen:
            return chain
        seen.add(following)
        chain.append(following)
        current = following


def tip_in(conn: sqlite3.Connection, session_id: str) -> str:
    """The newest segment of the conversation (itself when nothing continues it)."""
    return forward_in(conn, session_id)[-1]


def lineage_ids_in(conn: sqlite3.Connection, session_id: str) -> list[str]:
    """Root … tip, in order, through ``session_id``."""
    before = ancestors_in(conn, session_id)
    seen = set(before)
    return before + [sid for sid in forward_in(conn, session_id)[1:] if sid not in seen]


def members_in(conn: sqlite3.Connection, session_id: str) -> list[str]:
    """Every segment reachable from the root over continuation hops, root first; never raises."""
    root = root_in(conn, session_id)
    found = [root]
    seen = {root}
    index = 0
    while index < len(found):
        for child in continuations_in(conn, found[index]):
            child_id = str(child["id"])
            if child_id not in seen:
                seen.add(child_id)
                found.append(child_id)
        index += 1
    if session_id not in seen:
        found.append(session_id)
    return found
