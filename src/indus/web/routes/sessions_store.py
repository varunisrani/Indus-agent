"""Store access behind the dashboard's session routes (P18-11).

Reads go through ``reading(scope)``: the store opened by ``state.browse.open_readonly``
(``mode=ro``, nothing set up, migrated, checkpointed or created; R03.16), wrapped in a
``ReadView`` that the P02 read helpers accept as a handle. A missing store is ``None`` (the routes
answer an empty page); a store that cannot be read is a 503 with a code, never a 500 or an empty
page that looks real.

The rest is what the routes share: the JSON shape of a session and of a message, the mutation
snapshot (``revision`` + ``generation``) and the fence that guards single-session writes, and the
error envelope ``{detail: {code, message}}`` that the SPA reads.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import zlib
from collections.abc import Generator, Iterable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal, NoReturn, cast

from fastapi import HTTPException

from indus.foundation.errors import IndusError, StoreUnavailable
from indus.foundation.scope import Scope
from indus.state import browse, deletion, listing, messages, runtime_ledger
from indus.state.errors import Bucket, classify
from indus.state.registry import StoreHandle

__all__ = [
    "ACTIVE_WINDOW_SECONDS",
    "active_count_in",
    "AUTOMATION_SOURCES",
    "Execution",
    "Identity",
    "ReadOnlyView",
    "ReadView",
    "Snapshot",
    "check_identity",
    "check_idle",
    "execution_state",
    "fail",
    "ids_in",
    "info_from_row",
    "info_from_summary",
    "message_json",
    "now_of",
    "reading",
    "running_in",
    "snapshot_in",
    "store_failure",
    "text_of",
    "token_counts_in",
]

ACTIVE_WINDOW_SECONDS: Final = 300.0
"""A session touched this recently and not ended counts as active."""
AUTOMATION_SOURCES: Final = ("cron", "tool", "oneshot", "api_server", "acp")
"""The Sessions page's "Automation" tab (the "Chats" tab is everything else)."""

_REVISION_FIELDS: Final = (
    "title",
    "archived",
    "pinned",
    "hidden",
    "message_count",
    "tool_call_count",
    "rewind_count",
    "ended_at",
    "end_reason",
    "last_activity_at",
    "parent_session_id",
    "model",
)


def fail(status: int, code: str, message: str, **extra: Any) -> NoReturn:
    """Raise the error envelope the SPA reads (``detail.code`` / ``detail.message``)."""
    raise HTTPException(status_code=status, detail={"code": code, "message": message, **extra})


_UNAVAILABLE: Final = {
    "uninitialised": "The session history file exists but was never set up.",
    "incompatible": "The session history was written by a newer version of Indus Agent.",
}


def store_failure(exc: BaseException) -> HTTPException:
    """The 503 for a store that cannot be used right now (no paths, no SQL in the message)."""
    if isinstance(exc, StoreUnavailable):
        message = _UNAVAILABLE.get(exc.kind, "The session history cannot be read.")
        detail = {"code": "store_unavailable", "message": message, "kind": exc.kind}
        return HTTPException(status_code=503, detail=detail)
    bucket = classify(exc)
    if bucket is Bucket.CORRUPT:
        code, message = "store_corrupt", "The session history is damaged; run `indus doctor`."
    else:
        code, message = "store_busy", "The session history is busy right now; try again."
    return HTTPException(status_code=503, detail={"code": code, "message": message})


def _index_damaged() -> HTTPException:
    message = "The search index is damaged; it is rebuilt the next time Indus Agent starts."
    return HTTPException(
        status_code=503, detail={"code": "search_index_damaged", "message": message}
    )


class ReadOnlyView(Exception):
    """A P02 helper tried to write through the read-only view."""


@dataclass
class ReadView:
    """What the P02 read helpers need from a handle, over a read-only connection."""

    scope: Scope
    store: browse.ReadonlyStore

    @property
    def path(self) -> Path:
        return self.store.path

    def read(self) -> AbstractContextManager[sqlite3.Connection]:
        return self.store.read()

    def write(self, *_args: object, **_kwargs: object) -> NoReturn:
        raise ReadOnlyView("the dashboard browses the store read-only")

    def as_handle(self) -> StoreHandle:
        """For P02 functions typed on the writable handle that only read through it."""
        return cast(StoreHandle, self)


@contextlib.contextmanager
def reading(scope: Scope) -> Generator[ReadView | None]:
    """A read-only view of the store of ``scope``; None when no store file exists yet."""
    try:
        store = browse.open_readonly(scope)
    except StoreUnavailable as exc:
        if exc.kind == "missing":
            yield None
            return
        raise store_failure(exc) from None
    except (sqlite3.Error, OSError, IndusError) as exc:
        raise store_failure(exc) from None
    try:
        yield ReadView(scope, store)
    except ReadOnlyView:
        raise _index_damaged() from None
    except (sqlite3.Error, IndusError) as exc:
        raise store_failure(exc) from None
    finally:
        store.close()


def now_of(scope: Scope) -> float:
    return scope.clock.now().timestamp()


# -- shapes -------------------------------------------------------------------------------------


def _is_active(ended_at: object, last_active: float | None, now: float) -> bool:
    return (
        ended_at is None and last_active is not None and now - last_active < ACTIVE_WINDOW_SECONDS
    )


_RUNNING_SQL: Final = (
    "SELECT conversation_id FROM turn_leases WHERE expires_at > ? "
    "UNION SELECT session_id FROM runtime_admissions WHERE state = 'started'"
)


def running_in(conn: sqlite3.Connection, now: float) -> set[str]:
    """Sessions holding a turn right now: an unexpired turn lease or a started admission."""
    return {str(r[0]) for r in conn.execute(_RUNNING_SQL, (now,))}


def active_count_in(conn: sqlite3.Connection, now: float) -> int:
    """Open sessions touched within the active window, plus sessions running a turn."""
    found = conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE (ended_at IS NULL "
        "AND COALESCE(last_activity_at, started_at) >= ?) "
        f"OR id IN ({_RUNNING_SQL})",
        (now - ACTIVE_WINDOW_SECONDS, now),
    ).fetchone()
    return int(found[0])


def token_counts_in(conn: sqlite3.Connection, ids: Sequence[str]) -> dict[str, tuple[int, int]]:
    """``id → (input_tokens, output_tokens)`` for the given rows."""
    out: dict[str, tuple[int, int]] = {}
    for batch in deletion.chunked(ids):
        marks = ",".join("?" * len(batch))
        query = f"SELECT id, input_tokens, output_tokens FROM sessions WHERE id IN ({marks})"
        for row in conn.execute(query, batch):
            out[str(row[0])] = (int(row[1] or 0), int(row[2] or 0))
    return out


def info_from_summary(
    row: listing.SessionSummary, tokens: tuple[int, int], now: float, *, running: bool = False
) -> dict[str, Any]:
    """A listing row as the SPA's ``SessionInfo``."""
    return {
        "id": row.id,
        "title": row.title,
        "preview": row.preview or None,
        "source": row.source,
        "model": row.model,
        "started_at": row.started_at,
        "last_active": row.last_active if row.last_active is not None else row.started_at,
        "ended_at": row.ended_at,
        "end_reason": row.end_reason,
        "is_active": running or _is_active(row.ended_at, row.last_active, now),
        "input_tokens": tokens[0],
        "output_tokens": tokens[1],
        "tool_call_count": row.tool_call_count,
        "message_count": row.message_count,
        "archived": row.archived,
        "pinned": row.pinned,
        "kind": row.kind,
        "cwd": row.cwd,
        "git_branch": row.git_branch,
        "parent_session_id": row.parent_session_id,
    }


def _first_ask(conn: sqlite3.Connection, session_id: str) -> str:
    found = conn.execute(
        "SELECT content FROM messages WHERE session_id = ? AND role = 'user' AND active = 1 "
        "AND content IS NOT NULL ORDER BY id LIMIT 1",
        (session_id,),
    ).fetchone()
    return listing.shape_preview(text_of(messages.decode_content(found[0]))) if found else ""


def _newest_message(conn: sqlite3.Connection, session_id: str) -> float | None:
    found = conn.execute(
        "SELECT MAX(timestamp) FROM messages WHERE session_id = ?", (session_id,)
    ).fetchone()
    return float(found[0]) if found and found[0] is not None else None


def info_from_row(conn: sqlite3.Connection, row: Mapping[str, Any], now: float) -> dict[str, Any]:
    """One stored session row as ``SessionInfo`` (the single-session read)."""
    sid = str(row["id"])
    stamps = [
        v
        for v in (row.get("last_activity_at"), _newest_message(conn, sid), row.get("started_at"))
        if isinstance(v, int | float) and not isinstance(v, bool) and v <= now + 86400.0
    ]
    last_active = float(max(stamps)) if stamps else float(row["started_at"])
    return {
        "id": sid,
        "title": row.get("title"),
        "preview": _first_ask(conn, sid) or None,
        "source": row.get("source"),
        "model": row.get("model"),
        "started_at": float(row["started_at"]),
        "last_active": last_active,
        "ended_at": row.get("ended_at"),
        "end_reason": row.get("end_reason"),
        "is_active": sid in running_in(conn, now)
        or _is_active(row.get("ended_at"), last_active, now),
        "input_tokens": int(row.get("input_tokens") or 0),
        "output_tokens": int(row.get("output_tokens") or 0),
        "tool_call_count": int(row.get("tool_call_count") or 0),
        "message_count": int(row.get("message_count") or 0),
        "archived": bool(row.get("archived")),
        "pinned": bool(row.get("pinned")),
        "kind": None,
        "cwd": row.get("cwd"),
        "git_branch": row.get("git_branch"),
        "parent_session_id": row.get("parent_session_id"),
    }


def text_of(content: object) -> str:
    """Stored content (text or a list of parts) as plain text for the page."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces: list[str] = []
        for part in cast(list[object], content):
            if isinstance(part, str):
                pieces.append(part)
            elif isinstance(part, dict):
                item = cast(dict[str, object], part)
                text = item.get("text")
                if isinstance(text, str):
                    pieces.append(text)
                elif str(item.get("type", "")).startswith("image"):
                    pieces.append("[image]")
        return "\n".join(pieces)
    return json.dumps(content, ensure_ascii=False, default=str)


def _calls(raw: object) -> list[dict[str, str]]:
    if not raw:
        return []
    try:
        parsed: object = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return []
    if not isinstance(parsed, list):
        return []
    out: list[dict[str, str]] = []
    for item in cast(list[object], parsed):
        if not isinstance(item, dict):
            continue
        call = cast(dict[str, Any], item)
        fn: object = call.get("function")
        fn_map = cast(dict[str, Any], fn) if isinstance(fn, dict) else {}
        name = fn_map.get("name", call.get("name", ""))
        args: object = fn_map.get("arguments", call.get("arguments", ""))
        if not isinstance(args, str):
            args = json.dumps(args, ensure_ascii=False, default=str)
        out.append({"id": str(call.get("id") or ""), "name": str(name or ""), "arguments": args})
    return out


def message_json(row: Mapping[str, Any]) -> dict[str, Any]:
    """One stored message as the SPA's ``SessionMessage``."""
    out: dict[str, Any] = {
        "id": int(row["id"]),
        "role": str(row["role"]),
        "content": text_of(messages.decode_content(row["content"])),
        "timestamp": float(row["timestamp"]),
    }
    calls = _calls(row["tool_calls"])
    if calls:
        out["tool_calls"] = calls
    if row["tool_call_id"]:
        out["tool_call_id"] = str(row["tool_call_id"])
    if row["tool_name"]:
        out["tool_name"] = str(row["tool_name"])
    return out


# -- the mutation fence -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Snapshot:
    """What a client must echo back to change a session: 0 / None when it does not exist."""

    session_id: str
    exists: bool
    revision: int
    generation: int | None

    def to_json(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "exists": self.exists,
            "revision": self.revision,
            "generation": self.generation,
        }


def _revision(row: Mapping[str, Any]) -> int:
    fields = [row.get(name) for name in _REVISION_FIELDS]
    text = json.dumps(fields, separators=(",", ":"), default=str)
    return (zlib.crc32(text.encode("utf-8")) & 0x7FFFFFFF) or 1


def snapshot_in(conn: sqlite3.Connection, session_id: str) -> Snapshot:
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None:
        return Snapshot(session_id, False, 0, None)
    generation = runtime_ledger.generation_in(conn, session_id)
    return Snapshot(session_id, True, _revision(dict(row)), generation)


type Execution = Literal["idle", "busy", "unknown"]


def execution_state(conn: sqlite3.Connection, session_id: str, now: float) -> Execution:
    """``unknown``: a turn was lost in a restart; ``busy``: a turn is queued, running or holds a
    live lease or compression lock; else ``idle``. Read-only (stale guards are only ignored)."""
    states = {
        str(r[0])
        for r in conn.execute(
            "SELECT DISTINCT state FROM runtime_admissions WHERE session_id = ? "
            "AND state IN ('queued', 'started', 'unknown')",
            (session_id,),
        )
    }
    if "unknown" in states:
        return "unknown"
    if states or deletion.guarded_ids_in(conn, [session_id], now=now, reap=False):
        return "busy"
    return "idle"


@dataclass(frozen=True, slots=True)
class Identity:
    request_id: str | None
    expected_revision: int | None
    expected_generation: int | None

    @property
    def empty(self) -> bool:
        return (
            self.request_id is None
            and self.expected_revision is None
            and self.expected_generation is None
        )


def check_identity(identity: Identity, snap: Snapshot) -> None:
    """Refuse a write whose identity is missing, partial or out of date (409s)."""
    if identity.empty:
        fail(
            409,
            "client_upgrade_required",
            "This client sent no mutation identity; reload the page and try again.",
        )
    if not identity.request_id or identity.expected_revision is None:
        fail(409, "mutation_identity_required", "The request id and revision are both required.")
    if not snap.exists:
        return
    if identity.expected_generation is None:
        fail(409, "mutation_identity_required", "The expected generation is required.")
    if (
        identity.expected_revision != snap.revision
        or identity.expected_generation != snap.generation
    ):
        fail(
            409,
            "stale_revision",
            "The session changed since it was read; reload it and try again.",
            snapshot=snap.to_json(),
        )


def check_idle(state: Execution) -> None:
    if state == "unknown":
        fail(
            409,
            "unknown_execution",
            "The last turn of this session was lost in a restart; resolve it first.",
        )
    if state == "busy":
        fail(409, "session_busy", "This session has a turn queued or running.")


def ids_in(values: Iterable[str]) -> list[str]:
    return [v for v in dict.fromkeys(values) if v]
