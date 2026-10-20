"""Read-only looks at the profile's session store, shared by both ACP hosts (P19-03..07).

Everything here opens the store through ``state.browse.open_readonly`` (``mode=ro``: nothing is
created, migrated or checkpointed, R03.16) and closes it again; a missing or unreadable store reads
as "nothing there". In attached mode the daemon owns the store (P17-21); reading it this way is the
one access another process may have.

Blocking: call these off the event loop (``run_blocking``).
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
from collections.abc import Generator, Iterable
from dataclasses import dataclass
from typing import Any

from indus.acp.backend import ListedSession, LostTurn
from indus.core.persist import rows_from_store
from indus.core.transcript import TranscriptRow
from indus.foundation.errors import StoreUnavailable
from indus.foundation.scope import Scope
from indus.state import browse, lineage, listing, messages, sessions
from indus.state import runtime_ledger as ledger

__all__ = [
    "ACP_SOURCE",
    "LIST_LIMIT",
    "Lineage",
    "admission",
    "lineage_of",
    "listed",
    "lost_turns",
    "resolve",
    "session_row",
    "transcript",
]

log = logging.getLogger(__name__)

ACP_SOURCE = "acp"
LIST_LIMIT = 1000


class _View:
    """What the ``state`` readers take: ``read()`` and ``scope`` over a read-only connection."""

    def __init__(self, store: browse.ReadonlyStore, scope: Scope) -> None:
        self._store = store
        self.scope = scope

    def read(self) -> contextlib.AbstractContextManager[sqlite3.Connection]:
        return self._store.read()


@contextlib.contextmanager
def _opened(scope: Scope) -> Generator[_View | None]:
    try:
        store = browse.open_readonly(scope)
    except (StoreUnavailable, sqlite3.Error, OSError):
        yield None
        return
    try:
        yield _View(store, scope)
    finally:
        store.close()


def session_row(scope: Scope, session_id: str) -> dict[str, Any] | None:
    with _opened(scope) as view:
        if view is None:
            return None
        try:
            with view.read() as conn:
                return sessions.get_in(conn, session_id)
        except sqlite3.Error:
            log.warning("the session row could not be read", exc_info=True)
            return None


def resolve(scope: Scope, session_id: str) -> tuple[str, str]:
    """``(root, tip)`` of the compression lineage ``session_id`` belongs to (itself twice when the
    store does not know it or the chain cannot be followed)."""
    with _opened(scope) as view:
        if view is None:
            return session_id, session_id
        try:
            with view.read() as conn:
                root = lineage.root_in(conn, session_id)
                tip = lineage.tip_in(conn, session_id)
                return root, tip
        except (lineage.AmbiguousChain, sqlite3.Error):
            log.warning("the lineage of %s could not be followed", session_id, exc_info=True)
            return session_id, session_id


def transcript(scope: Scope, session_id: str) -> list[TranscriptRow]:
    """The live rows of ``session_id`` (oldest first)."""
    with _opened(scope) as view:
        if view is None:
            return []
        try:
            with view.read() as conn:
                return rows_from_store(messages.get_in(conn, session_id))
        except sqlite3.Error:
            log.warning("the transcript of %s could not be read", session_id, exc_info=True)
            return []


@dataclass(frozen=True)
class Lineage:
    """Where a session sits in its compression chain (provenance ``_meta``)."""

    current: str
    root: str
    parent: str | None
    continuation: bool
    depth: int


def lineage_of(scope: Scope, session_id: str) -> Lineage | None:
    with _opened(scope) as view:
        if view is None:
            return None
        try:
            with view.read() as conn:
                row = sessions.get_in(conn, session_id)
                if row is None:
                    return None
                chain = lineage.ancestors_in(conn, session_id)
                parent = row.get("parent_session_id")
                parent_row = sessions.get_in(conn, str(parent)) if parent else None
                continuation = parent_row is not None and lineage.is_continuation(row, parent_row)
                root = chain[0] if chain else session_id
                return Lineage(
                    current=session_id,
                    root=root,
                    parent=str(parent) if parent else None,
                    continuation=continuation,
                    depth=max(0, len(chain) - 1) if chain else 0,
                )
        except (lineage.AmbiguousChain, sqlite3.Error):
            log.debug("no provenance for %s", session_id, exc_info=True)
            return None


def listed(scope: Scope, *, limit: int = LIST_LIMIT) -> list[ListedSession]:
    """ACP conversations with at least one message, one row per logical conversation (its root
    id, its tip's title and activity), newest activity first."""
    with _opened(scope) as view:
        if view is None:
            return []
        try:
            found = listing.list_sessions(
                view,  # pyright: ignore[reportArgumentType]
                listing.ListFilters(sources=(ACP_SOURCE,), min_messages=1),
                limit,
            )
        except sqlite3.Error:
            log.warning("the session list could not be read", exc_info=True)
            return []
    out: list[ListedSession] = []
    for row in found:
        out.append(
            ListedSession(
                session_id=row.root_id or row.id,
                cwd=row.cwd,
                title=row.title,
                preview=row.preview,
                updated_at=row.last_active if row.last_active is not None else row.started_at,
            )
        )
    out.sort(key=_activity, reverse=True)
    return out


def _activity(row: ListedSession) -> float:
    return row.updated_at if row.updated_at is not None else float("-inf")


def lost_turns(scope: Scope, session_ids: Iterable[str]) -> list[LostTurn]:
    """Admissions of these sessions in state ``unknown`` (lost in a daemon restart), FIFO."""
    with _opened(scope) as view:
        if view is None:
            return []
        rows: list[ledger.AdmissionRow] = []
        for sid in sorted(set(session_ids)):
            try:
                rows.extend(ledger.pending(view, sid))  # pyright: ignore[reportArgumentType]
            except sqlite3.Error:
                log.warning("the runtime ledger could not be read", exc_info=True)
    rows.sort(key=lambda row: row.seq)
    return [
        LostTurn(row.admission_id, row.generation or 0) for row in rows if row.state == "unknown"
    ]


def admission(scope: Scope, admission_id: str) -> ledger.AdmissionRow | None:
    with _opened(scope) as view:
        if view is None:
            return None
        try:
            return ledger.get(view, admission_id)  # pyright: ignore[reportArgumentType]
        except sqlite3.Error:
            log.warning("the runtime ledger could not be read", exc_info=True)
            return None
