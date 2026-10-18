"""Rewind (``/undo``, ``/retry``), clear, and branch: the store side of taking turns back and of
copying a conversation.

``rewind`` takes back user turns by soft-deleting (``active = 0``) every live row from the chosen
user message onward, target included, and hands back that message's text so the surface can offer
it again. The running process holds its own ("warm") copy of the conversation; before anything is
written, the warm copy must agree with the stored one — the same number of user turns and the same
text at the target — or nothing changes (``RewindMismatch``). A stale UI therefore never cuts the
wrong turn. Rewound rows stay in the table for audit reads (``include_inactive``).

``branch`` copies a conversation, up to a chosen live message, into an independent new session in
one transaction: its own id, a ``_branched_from`` marker naming the parent, the parent's route and
workspace, cloned rows (fresh ids, same uids) and its own title.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from indus.foundation.errors import StoreError
from indus.foundation.ids import new_session_id
from indus.state import messages, session_meta, sessions, titles
from indus.state.compression import CompressionBusy, is_locked_in
from indus.state.handoff import HANDOFF_FINISH, not_handoff_sql, recount_in
from indus.state.registry import StoreHandle

__all__ = [
    "PREVIEW_CHARS",
    "BranchRefused",
    "RewindMismatch",
    "RewindResult",
    "RewindUnavailable",
    "UserTurn",
    "branch",
    "branch_in",
    "clear",
    "content_text",
    "recent_user_messages",
    "rewind",
    "rewind_in",
]

PREVIEW_CHARS = 80

# Columns a branch takes over from its parent.
_BRANCH_CARRIED = (
    "created_source",
    "user_id",
    "model",
    "system_prompt_hash",
    "cwd",
    "git_root",
    "git_branch",
    "billing_provider",
    "billing_base_url",
    "profile_id",
)


class RewindUnavailable(StoreError):
    """No user turn at the requested position (empty transcript, or past the newest turn)."""

    default_code = "store.rewind_unavailable"


class RewindMismatch(StoreError):
    """The caller's in-memory conversation disagrees with the stored one; nothing was changed."""

    default_code = "store.rewind_mismatch"


class BranchRefused(StoreError):
    """The session to branch from is missing, or the boundary is not one of its live rows."""

    default_code = "store.branch_refused"


@dataclass(frozen=True, slots=True)
class RewindResult:
    removed: int
    target_text: str
    new_head_id: int | None
    target_id: int


@dataclass(frozen=True, slots=True)
class UserTurn:
    id: int
    timestamp: float
    preview: str


def _now(store: StoreHandle) -> float:
    return store.scope.clock.now().timestamp()


# -- content helpers ----------------------------------------------------------------------------


def content_text(value: object) -> str:
    """Text of a message content value: a string as-is; for a list of parts, the text parts
    joined with nothing between them (re-sending them gives the same bytes)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        part: Mapping[str, Any] = value  # pyright: ignore[reportUnknownVariableType]
        text = part.get("text")
        return text if isinstance(text, str) else ""
    if isinstance(value, Sequence):
        items: Sequence[Any] = value  # pyright: ignore[reportUnknownVariableType]
        return "".join(content_text(item) for item in items if not isinstance(item, str | bytes))
    return str(value)


def _comparable(value: object) -> str:
    """One canonical text per content value, so a warm and a stored copy compare equal."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _preview(value: object) -> str:
    folded = " ".join(content_text(value).split())
    if len(folded) > PREVIEW_CHARS:
        return folded[: PREVIEW_CHARS - 3] + "..."
    return folded


# -- rewind ------------------------------------------------------------------------------------


def _live_user_rows(conn: sqlite3.Connection, session_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, content FROM messages WHERE session_id = ? AND active = 1 AND role = 'user' "
        f"AND {not_handoff_sql()} ORDER BY id",
        (session_id,),
    ).fetchall()


def _handoff_texts(conn: sqlite3.Connection, session_id: str) -> set[str]:
    rows = conn.execute(
        "SELECT content FROM messages WHERE session_id = ? AND active = 1 AND role = 'user' "
        "AND finish_reason = ?",
        (session_id, HANDOFF_FINISH),
    ).fetchall()
    return {_comparable(messages.decode_content(r["content"])) for r in rows}


def _warm_user_turns(
    warm_history: Sequence[Mapping[str, Any]], handoffs: set[str]
) -> list[Mapping[str, Any]]:
    turns: list[Mapping[str, Any]] = []
    for msg in warm_history:
        if msg.get("role") != "user" or msg.get("finish_reason") == HANDOFF_FINISH:
            continue
        if handoffs and _comparable(msg.get("content")) in handoffs:
            continue
        turns.append(msg)
    return turns


def rewind_in(
    conn: sqlite3.Connection,
    session_id: str,
    user_ordinal: int,
    warm_history: Sequence[Mapping[str, Any]] | None,
    *,
    now: float,
) -> RewindResult:
    """The body of ``rewind`` on the caller's write transaction (every check precedes every
    write)."""
    if is_locked_in(conn, session_id, now=now):
        raise CompressionBusy(
            f"session {session_id} is being compressed; try the undo again in a moment"
        )
    users = _live_user_rows(conn, session_id)
    if not users:
        raise RewindUnavailable(f"session {session_id} has no user turn to take back")
    ordinal = user_ordinal if user_ordinal >= 0 else max(len(users) + user_ordinal, 0)
    if ordinal >= len(users):
        raise RewindUnavailable(
            f"session {session_id} has {len(users)} user turns; there is no turn {user_ordinal}"
        )
    target = users[ordinal]
    target_content = messages.decode_content(target["content"])
    if warm_history is not None:
        warm = _warm_user_turns(warm_history, _handoff_texts(conn, session_id))
        if len(warm) != len(users) or _comparable(warm[ordinal].get("content")) != _comparable(
            target_content
        ):
            raise RewindMismatch(
                f"the conversation in session {session_id} changed before the undo could be "
                "saved; nothing was taken back",
                hint="reload the session and try again",
            )
    target_id = int(target["id"])
    cur = conn.execute(
        "UPDATE messages SET active = 0 WHERE session_id = ? AND active = 1 AND id >= ?",
        (session_id, target_id),
    )
    conn.execute("UPDATE sessions SET rewind_count = rewind_count + 1 WHERE id = ?", (session_id,))
    recount_in(conn, session_id)
    head = conn.execute(
        "SELECT MAX(id) FROM messages WHERE session_id = ? AND active = 1", (session_id,)
    ).fetchone()[0]
    return RewindResult(
        removed=cur.rowcount,
        target_text=content_text(target_content),
        new_head_id=None if head is None else int(head),
        target_id=target_id,
    )


def rewind(
    store: StoreHandle,
    session_id: str,
    user_ordinal: int,
    warm_history: Sequence[Mapping[str, Any]] | None,
) -> RewindResult:
    """Take back user turns from ``user_ordinal`` on (0 = oldest; negative counts back from the
    newest and stops at the oldest). ``warm_history`` is the caller's in-memory conversation, or
    None when it holds none."""
    warm = None if warm_history is None else [dict(m) for m in warm_history]
    return store.write(
        lambda conn: rewind_in(conn, session_id, user_ordinal, warm, now=_now(store))
    )


def recent_user_messages(store: StoreHandle, session_id: str, n: int = 20) -> list[UserTurn]:
    """The ``n`` newest live user turns, newest first, with an 80-character preview each."""
    if n <= 0:
        return []
    with store.read() as conn:
        rows = conn.execute(
            "SELECT id, timestamp, content FROM messages WHERE session_id = ? AND active = 1 "
            f"AND role = 'user' AND {not_handoff_sql()} ORDER BY id DESC LIMIT ?",
            (session_id, n),
        ).fetchall()
    return [
        UserTurn(
            id=int(r["id"]),
            timestamp=float(r["timestamp"]),
            preview=_preview(messages.decode_content(r["content"])),
        )
        for r in rows
    ]


def clear(store: StoreHandle, session_id: str) -> int:
    """Delete every message of ``session_id`` and zero its message counters; rows removed."""

    def wipe(conn: sqlite3.Connection) -> int:
        cur = conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        conn.execute(
            "UPDATE sessions SET message_count = 0, tool_call_count = 0 WHERE id = ?",
            (session_id,),
        )
        return cur.rowcount

    return store.write(wipe)


# -- branch ------------------------------------------------------------------------------------


def _boundary(conn: sqlite3.Connection, session_id: str, upto: int | None) -> int:
    """Highest row id the branch copies. An assistant boundary keeps the tool rows that answer
    it."""
    if upto is None:
        row = conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM messages WHERE session_id = ? AND active = 1",
            (session_id,),
        ).fetchone()
        return int(row[0])
    found = conn.execute(
        "SELECT role FROM messages WHERE id = ? AND session_id = ? AND active = 1",
        (upto, session_id),
    ).fetchone()
    if found is None:
        raise BranchRefused(
            f"message {upto} is not a live message of session {session_id}; pick a visible one"
        )
    last = int(upto)
    if found["role"] == "assistant":
        later = conn.execute(
            "SELECT id, role FROM messages WHERE session_id = ? AND active = 1 AND id > ? "
            "ORDER BY id",
            (session_id, upto),
        )
        for row in later:
            if row["role"] != "tool":
                break
            last = int(row["id"])
    return last


def branch_in(
    conn: sqlite3.Connection,
    session_id: str,
    child_id: str,
    *,
    upto_message_id: int | None = None,
    title: str | None = None,
    now: float,
) -> int:
    """The body of ``branch`` on the caller's write transaction; returns the rows copied."""
    parent = sessions.get_in(conn, session_id)
    if parent is None:
        raise BranchRefused(f"session {session_id} does not exist")
    last = _boundary(conn, session_id, upto_message_id)
    fields: dict[str, Any] = {name: parent.get(name) for name in _BRANCH_CARRIED}
    fields["parent_session_id"] = session_id
    # Only the marker: per-session settings in the parent's config (approval modes, usage
    # anchors) belong to that session, not to an independent copy.
    fields["model_config"] = json.dumps({sessions.MARKER_BRANCHED: session_id})
    if not sessions.create_in(conn, child_id, str(parent["source"]), now=now, **fields):
        raise BranchRefused(f"session {child_id} already exists")
    ids = [
        int(r[0])
        for r in conn.execute(
            "SELECT id FROM messages WHERE session_id = ? AND active = 1 AND id <= ? ORDER BY id",
            (session_id, last),
        ).fetchall()
    ]
    if ids:
        messages.clone_rows_in(conn, child_id, ids)
    recount_in(conn, child_id)
    if title is not None:
        titles.set_title_in(conn, child_id, title, source="user")
    else:
        base = str(parent.get("title") or "branch")
        titles.set_title_in(
            conn, child_id, titles.next_lineage_title_in(conn, base), source="derived"
        )
    session_meta.copy_branch_safe_in(conn, session_id, child_id, now=now)
    return len(ids)


def branch(
    store: StoreHandle,
    session_id: str,
    *,
    upto_message_id: int | None = None,
    title: str | None = None,
) -> str:
    """Copy ``session_id`` up to ``upto_message_id`` (default: its newest live row) into a new,
    independent session; returns the new id. ``title`` is kept with user provenance; without one
    the child gets the parent's next lineage title (``name #2``)."""
    child_id = new_session_id(store.scope.clock)
    store.write(
        lambda conn: branch_in(
            conn,
            session_id,
            child_id,
            upto_message_id=upto_message_id,
            title=title,
            now=_now(store),
        )
    )
    return child_id
