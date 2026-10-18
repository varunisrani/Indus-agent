"""Session listings for pickers: one row per conversation, a short preview and a trustworthy
"last active" time.

A row is a root, or a child the user made on purpose (a branch or a reset; with
``include_subagents`` also delegate runs). Compression continuations are never rows of their own:
a root whose conversation continued is shown with its tip's fields (id, title, counters, model,
workspace, preview, activity) while keeping the root's start time, so the order does not jump when
a rotation happens. Machine sessions (board workers, tool integrations, one-shot runs, sub-agents)
stay out of human pickers unless asked for.

Preview: the first live user row, in id order (R03.14), that is not a compression handoff row; CR
and LF fold to spaces, the first 63 characters are kept, and anything longer than 60 shows as 60
plus "...". ``last_active``: the newest of the activity stamp, the newest message time and the
start time, ignoring any value outside [2020-01-01, now + 1 day] (a corrupt or far-future stamp
must not pin a row to the top).

``recent`` is the bounded browse used by the agent itself: candidates come from the activity and
start-time indexes, each is resolved to its conversation, and the walk stops when the time budget
is spent.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from indus.state import lineage, messages, sessions
from indus.state.handoff import not_handoff_sql
from indus.state.registry import StoreHandle

__all__ = [
    "ACTIVITY_FLOOR",
    "INTERNAL_SOURCES",
    "PREVIEW_HEAD",
    "PREVIEW_MAX",
    "ListFilters",
    "SessionSummary",
    "list_sessions",
    "recent",
    "shape_preview",
]

log = logging.getLogger(__name__)

INTERNAL_SOURCES = ("kanban", "tool", "oneshot", "subagent")
PREVIEW_HEAD = 63
PREVIEW_MAX = 60
ACTIVITY_FLOOR = datetime(2020, 1, 1, tzinfo=UTC).timestamp()
ACTIVITY_CEILING_SLACK = 86400.0
CHUNK = 900
RECENT_MIN_CANDIDATES = 128


@dataclass(frozen=True, slots=True)
class ListFilters:
    """``archived`` / ``hidden`` / ``pinned``: False = exclude, True = only, None = both."""

    sources: Sequence[str] | None = None
    exclude_sources: Sequence[str] | None = None
    cwd_prefix: str | None = None
    workspace: str | None = None
    min_messages: int = 0
    archived: bool | None = False
    hidden: bool | None = False
    pinned: bool | None = None
    include_subagents: bool = False
    include_internal: bool = False
    query: str | None = None


@dataclass(frozen=True, slots=True)
class SessionSummary:
    id: str
    source: str
    title: str | None
    preview: str
    started_at: float
    last_active: float | None
    ended_at: float | None
    end_reason: str | None
    message_count: int
    tool_call_count: int
    model: str | None
    cwd: str | None
    git_root: str | None
    git_branch: str | None
    profile_id: str | None
    parent_session_id: str | None
    kind: str
    archived: bool
    pinned: bool
    hidden: bool
    unread: bool
    root_id: str | None = None
    lineage_ids: tuple[str, ...] = ()


# -- SQL fragments ------------------------------------------------------------------------------


def _marker(alias: str, key: str) -> str:
    return (
        f"json_extract(CASE WHEN json_valid({alias}.model_config) "
        f"THEN {alias}.model_config END, '$.{key}')"
    )


def _names_parent(alias: str, key: str, parent: str) -> str:
    """The child's ``key`` marker names exactly ``parent`` (R03.13)."""
    return f"({parent} IS NOT NULL AND COALESCE({_marker(alias, key)}, '') = {parent})"


def _quoted(values: Iterable[str]) -> str:
    return ", ".join("'" + v.replace("'", "''") + "'" for v in sorted(values))


_COMPRESSED = _quoted(sessions.COMPRESSION_REASONS)
_BRANCH = _names_parent("s", sessions.MARKER_BRANCHED, "s.parent_session_id")
_RESET = _names_parent("s", sessions.MARKER_RESET, "s.parent_session_id")
_DELEGATE = _names_parent("s", sessions.MARKER_DELEGATE, "s.parent_session_id")
_CONTINUATION = (
    f"(s.parent_session_id IS NOT NULL AND COALESCE(s.source, '') <> 'tool' "
    f"AND NOT {_BRANCH} AND NOT {_RESET} AND NOT {_DELEGATE} "
    f"AND EXISTS (SELECT 1 FROM sessions p WHERE p.id = s.parent_session_id "
    f"AND p.end_reason IN ({_COMPRESSED})))"
)
_LISTABLE = f"(s.parent_session_id IS NULL OR {_BRANCH} OR {_RESET})"
_KIND = (
    "CASE WHEN COALESCE(s.source, '') = 'tool' THEN 'tool' "
    "WHEN s.parent_session_id IS NULL THEN 'root' "
    f"WHEN {_BRANCH} THEN 'branch' WHEN {_RESET} THEN 'reset' ELSE 'delegate' END"
)
# One forward step of a compression chain: ``c`` continues ``p``.
_CHAIN_STEP = (
    f"p.end_reason IN ({_COMPRESSED}) AND COALESCE(c.source, '') <> 'tool' "
    f"AND COALESCE({_marker('c', sessions.MARKER_BRANCHED)}, '') <> p.id "
    f"AND COALESCE({_marker('c', sessions.MARKER_RESET)}, '') <> p.id "
    f"AND COALESCE({_marker('c', sessions.MARKER_DELEGATE)}, '') <> p.id"
)


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _under(column: str, prefix: str) -> tuple[str, list[object]]:
    """``column`` equals ``prefix`` or names a path below it (either separator)."""
    trimmed = prefix.rstrip("/\\")
    if not trimmed:
        return f"{column} LIKE ? ESCAPE '\\'", [_escape_like(prefix) + "%"]
    esc = _escape_like(trimmed)
    return (
        f"({column} = ? OR {column} LIKE ? ESCAPE '\\' OR {column} LIKE ? ESCAPE '\\')",
        [trimmed, esc + "/%", esc + "\\\\%"],
    )


def _tri(column: str, wanted: bool | None) -> str | None:
    if wanted is None:
        return None
    return f"{column} = {1 if wanted else 0}"


def _where(filters: ListFilters) -> tuple[list[str], list[object]]:
    clauses: list[str] = []
    params: list[object] = []
    if filters.include_subagents:
        clauses.append(f"NOT {_CONTINUATION}")
    else:
        clauses.append(_LISTABLE)
    if filters.sources:
        clauses.append(f"s.source IN ({', '.join('?' for _ in filters.sources)})")
        params.extend(filters.sources)
    elif not filters.include_internal:
        hidden = [
            src for src in INTERNAL_SOURCES if not (filters.include_subagents and src == "subagent")
        ]
        clauses.append(f"s.source NOT IN ({_quoted(hidden)})")
    if filters.exclude_sources:
        clauses.append(f"s.source NOT IN ({', '.join('?' for _ in filters.exclude_sources)})")
        params.extend(filters.exclude_sources)
    if filters.cwd_prefix:
        clause, values = _under("s.cwd", filters.cwd_prefix)
        clauses.append(clause)
        params.extend(values)
    if filters.workspace:
        clause, values = _under("s.cwd", filters.workspace)
        clauses.append(f"(s.git_root = ? OR (COALESCE(s.git_root, '') = '' AND {clause}))")
        params.append(filters.workspace.rstrip("/\\") or filters.workspace)
        params.extend(values)
    if filters.min_messages > 0:
        clauses.append("s.message_count >= ?")
        params.append(filters.min_messages)
    # The archived-only view is where anything that fell out of the normal lists is recovered,
    # so it shows hidden rows too unless hidden ones are asked for explicitly.
    hidden = None if filters.archived is True and filters.hidden is False else filters.hidden
    for column, wanted in (
        ("s.archived", filters.archived),
        ("s.hidden", hidden),
        ("s.pinned", filters.pinned),
    ):
        clause = _tri(column, wanted)
        if clause is not None:
            clauses.append(clause)
    return clauses, params


def _query_clause(needle: str) -> tuple[str, list[object]]:
    """Admit a row when its forward chain matches ``needle`` by id, title or first user ask."""
    like = "%" + _escape_like(needle.lower()) + "%"
    compact = re.sub(r"[\W_]+", "", needle.lower())
    title_compact = (
        "REPLACE(REPLACE(REPLACE(REPLACE(lower(COALESCE(cs.title, '')), "
        "'-', ''), '_', ''), '.', ''), ' ', '')"
    )
    first_ask = (
        "(SELECT m.content FROM messages m WHERE m.session_id = cs.id AND m.role = 'user' "
        f"AND m.active = 1 AND m.content IS NOT NULL AND {not_handoff_sql('m.')} "
        "ORDER BY m.id LIMIT 1)"
    )
    parts = [
        "lower(q.cur_id) LIKE ? ESCAPE '\\'",
        "lower(COALESCE(cs.title, '')) LIKE ? ESCAPE '\\'",
        f"lower(COALESCE({first_ask}, '')) LIKE ? ESCAPE '\\'",
    ]
    params: list[object] = [like, like, like]
    if compact:
        parts.append(f"{title_compact} LIKE ? ESCAPE '\\'")
        params.append("%" + _escape_like(compact) + "%")
    clause = (
        "EXISTS (SELECT 1 FROM chain q JOIN sessions cs ON cs.id = q.cur_id "
        f"WHERE q.root_id = s.id AND ({' OR '.join(parts)}))"
    )
    return clause, params


# -- hydration ----------------------------------------------------------------------------------


def shape_preview(text: str | None) -> str:
    """Fold line breaks, keep the head, and cut to 60 characters plus an ellipsis."""
    if not text:
        return ""
    folded = text.replace("\r", " ").replace("\n", " ").strip()[:PREVIEW_HEAD]
    if len(folded) > PREVIEW_MAX:
        return folded[:PREVIEW_MAX] + "..."
    return folded


def _content_preview(stored: str | None) -> str:
    try:
        value = messages.decode_content(stored)
    except ValueError:
        return ""
    if isinstance(value, str):
        return shape_preview(value)
    texts: list[str] = []
    if isinstance(value, list):
        for part in value:  # pyright: ignore[reportUnknownVariableType]
            if isinstance(part, dict):
                text = part.get("text")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
                if isinstance(text, str) and text.strip():
                    texts.append(text.strip())
    return shape_preview(" ".join(texts))


def _chunks(ids: Sequence[str]) -> Iterable[Sequence[str]]:
    for start in range(0, len(ids), CHUNK):
        yield ids[start : start + CHUNK]


def _previews(conn: sqlite3.Connection, ids: Sequence[str]) -> dict[str, str]:
    found: dict[str, str] = {}
    for part in _chunks(ids):
        marks = ", ".join("?" for _ in part)
        rows = conn.execute(
            "SELECT m.session_id, m.content FROM messages m WHERE m.id IN ("
            f"SELECT MIN(id) FROM messages WHERE session_id IN ({marks}) AND role = 'user' "
            f"AND active = 1 AND content IS NOT NULL AND content <> '' AND {not_handoff_sql()} "
            "GROUP BY session_id)",
            tuple(part),
        ).fetchall()
        for row in rows:
            found[str(row["session_id"])] = _content_preview(row["content"])
    return found


def _newest_messages(
    conn: sqlite3.Connection, ids: Sequence[str], window: tuple[float, float]
) -> dict[str, float]:
    found: dict[str, float] = {}
    for part in _chunks(ids):
        marks = ", ".join("?" for _ in part)
        rows = conn.execute(
            "SELECT session_id, MAX(timestamp) FROM messages "
            f"WHERE session_id IN ({marks}) AND timestamp BETWEEN ? AND ? GROUP BY session_id",
            (*part, *window),
        ).fetchall()
        for row in rows:
            if row[1] is not None:
                found[str(row[0])] = float(row[1])
    return found


def _in_window(value: object, window: tuple[float, float]) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if window[0] <= number <= window[1] else None


def _last_active(
    row: sqlite3.Row, newest: dict[str, float], window: tuple[float, float]
) -> float | None:
    values = [
        _in_window(row["last_activity_at"], window),
        newest.get(str(row["id"])),
        _in_window(row["started_at"], window),
    ]
    present = [v for v in values if v is not None]
    return max(present) if present else None


def _tips(conn: sqlite3.Connection, rows: Sequence[sqlite3.Row]) -> dict[str, list[str]]:
    """Chains (root … tip) of the rows whose conversation continued past them."""
    chains: dict[str, list[str]] = {}
    for row in rows:
        if row["end_reason"] not in sessions.COMPRESSION_REASONS:
            continue
        sid = str(row["id"])
        try:
            chain = lineage.lineage_ids_in(conn, sid)
        except lineage.AmbiguousChain:
            log.warning("session %s has competing continuations; listing it as it is", sid)
            continue
        if sid in chain and chain[-1] != sid:
            chains[sid] = chain[chain.index(sid) :]
    return chains


def _rows_by_id(conn: sqlite3.Connection, ids: Sequence[str]) -> dict[str, sqlite3.Row]:
    found: dict[str, sqlite3.Row] = {}
    for part in _chunks(ids):
        marks = ", ".join("?" for _ in part)
        for row in conn.execute(
            f"SELECT s.*, {_KIND} AS kind FROM sessions s WHERE s.id IN ({marks})", tuple(part)
        ).fetchall():
            found[str(row["id"])] = row
    return found


def _summaries(
    conn: sqlite3.Connection, rows: Sequence[sqlite3.Row], now: float
) -> list[SessionSummary]:
    window = (ACTIVITY_FLOOR, now + ACTIVITY_CEILING_SLACK)
    chains = _tips(conn, rows)
    tips = _rows_by_id(conn, sorted({chain[-1] for chain in chains.values()}))
    every = [str(r["id"]) for r in rows] + list(tips)
    previews = _previews(conn, every)
    newest = _newest_messages(conn, every, window)
    out: list[SessionSummary] = []
    for row in rows:
        sid = str(row["id"])
        chain = chains.get(sid)
        tip = tips.get(chain[-1]) if chain else None
        last_active = _last_active(row, newest, window)
        shown = row
        preview = previews.get(sid, "")
        if tip is not None:
            shown = tip
            tip_active = _last_active(tip, newest, window)
            if tip_active is not None:
                last_active = tip_active if last_active is None else max(last_active, tip_active)
            preview = previews.get(str(tip["id"])) or preview
        last_read = row["last_read_at"]
        out.append(
            SessionSummary(
                id=str(shown["id"]),
                source=str(shown["source"]),
                title=shown["title"] if shown["title"] is not None else row["title"],
                preview=preview,
                started_at=float(row["started_at"]),
                last_active=last_active,
                ended_at=shown["ended_at"],
                end_reason=shown["end_reason"],
                message_count=int(shown["message_count"]),
                tool_call_count=int(shown["tool_call_count"]),
                model=shown["model"],
                cwd=shown["cwd"],
                git_root=shown["git_root"],
                git_branch=shown["git_branch"],
                profile_id=shown["profile_id"],
                parent_session_id=row["parent_session_id"],
                kind=str(row["kind"]),
                archived=bool(row["archived"]),
                pinned=bool(row["pinned"]),
                hidden=bool(row["hidden"]),
                unread=last_read is not None
                and last_active is not None
                and last_active > float(last_read),
                root_id=sid if tip is not None else None,
                lineage_ids=tuple(chain) if chain else (),
            )
        )
    return out


# -- public -------------------------------------------------------------------------------------


def _now(store: StoreHandle) -> float:
    return store.scope.clock.now().timestamp()


def list_sessions(
    store: StoreHandle,
    filters: ListFilters | None = None,
    limit: int = 20,
    offset: int = 0,
) -> list[SessionSummary]:
    """One page of conversations, newest start first."""
    flt = filters if filters is not None else ListFilters()
    clauses, params = _where(flt)
    where = " AND ".join(clauses)
    needle = (flt.query or "").strip()
    if needle:
        match, match_params = _query_clause(needle)
        sql = (
            "WITH RECURSIVE chain(root_id, cur_id) AS ("
            f"SELECT s.id, s.id FROM sessions s WHERE {where} "
            "UNION SELECT chain.root_id, c.id FROM chain "
            "JOIN sessions p ON p.id = chain.cur_id "
            f"JOIN sessions c ON c.parent_session_id = p.id WHERE {_CHAIN_STEP}) "
            f"SELECT s.*, {_KIND} AS kind FROM sessions s WHERE {where} AND {match} "
            "ORDER BY s.started_at DESC, s.id DESC LIMIT ? OFFSET ?"
        )
        args: list[object] = [*params, *params, *match_params, max(limit, 0), max(offset, 0)]
    else:
        sql = (
            f"SELECT s.*, {_KIND} AS kind FROM sessions s WHERE {where} "
            "ORDER BY s.started_at DESC, s.id DESC LIMIT ? OFFSET ?"
        )
        args = [*params, max(limit, 0), max(offset, 0)]
    with store.read() as conn:
        rows = conn.execute(sql, args).fetchall()
        return _summaries(conn, rows, _now(store))


def _candidates(conn: sqlite3.Connection, count: int) -> list[str]:
    """Recently active ids from the activity index and the start-time index, merged."""
    base = (
        "s.archived = 0 AND s.hidden = 0 "
        f"AND s.source NOT IN ({_quoted(INTERNAL_SOURCES)}) AND NOT {_DELEGATE}"
    )
    by_activity = conn.execute(
        f"SELECT s.id, s.last_activity_at AS t FROM sessions s WHERE {base} "
        "AND s.last_activity_at IS NOT NULL ORDER BY s.last_activity_at DESC LIMIT ?",
        (count,),
    ).fetchall()
    by_start = conn.execute(
        f"SELECT s.id, COALESCE(s.last_activity_at, s.started_at) AS t FROM sessions s "
        f"WHERE {base} ORDER BY s.started_at DESC LIMIT ?",
        (count,),
    ).fetchall()
    stamp: dict[str, float] = {}
    for row in (*by_activity, *by_start):
        stamp[str(row["id"])] = float(row["t"])
    return sorted(stamp, key=lambda sid: (-stamp[sid], sid))[:count]


def recent(store: StoreHandle, limit: int = 20, budget: float = 3.0) -> list[SessionSummary]:
    """The most recently active conversations, resolved within ``budget`` seconds.

    Candidates are taken from the indexes, mapped to their conversation root and projected to
    the tip. Once the budget is spent no further candidate is resolved; the conversations found
    so far are returned.
    """
    clock = store.scope.clock
    deadline = clock.monotonic() + max(budget, 0.0)
    want = max(limit, 1)
    with store.read() as conn:
        roots: list[str] = []
        for sid in _candidates(conn, max(RECENT_MIN_CANDIDATES, want * 8)):
            if clock.monotonic() >= deadline:
                break
            found = lineage.root_in(conn, sid)
            if found not in roots:
                roots.append(found)
        admitted: list[sqlite3.Row] = []
        for part in _chunks(roots):
            marks = ", ".join("?" for _ in part)
            admitted.extend(
                conn.execute(
                    f"SELECT s.*, {_KIND} AS kind FROM sessions s WHERE s.id IN ({marks}) "
                    f"AND {_LISTABLE} AND s.archived = 0 AND s.hidden = 0 "
                    f"AND s.source NOT IN ({_quoted(INTERNAL_SOURCES)})",
                    tuple(part),
                ).fetchall()
            )
        found_rows = _summaries(conn, admitted, _now(store))
    found_rows.sort(key=lambda r: (r.last_active or 0.0, r.started_at, r.id), reverse=True)
    return found_rows[:want]
