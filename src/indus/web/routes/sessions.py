"""``/api/sessions``: the dashboard's Sessions page (P18-11, R03.16, R03.22, R09.21).

Reads (list, search, stats, one session, its messages, export, the mutation snapshot) open the
store read-only (``sessions_store.reading``) and never create, migrate or checkpoint it; a store
that cannot be read is a 503 with a code.

Writes come in two kinds:

- **One session** (PATCH rename/archive/pin, DELETE): the request carries the identity it read
  from ``/mutation-snapshot`` (request id, expected revision, expected generation); missing,
  partial or stale identities are 409s, and a delete also refuses a busy session or one whose last
  turn was lost. The delete goes through the maintenance gate: inside the daemon when one owns the
  profile, else here under the ownership lock. A deleted id keeps its tombstone, so a retry finds it
  absent and answers 200.
- **Bulk maintenance** (bulk delete, delete-empty, prune without ``dry_run``) runs only while no
  daemon owns the profile: a daemon answering its control socket or a held ownership lock is 409
  ``daemon_running``, and nothing is written. A dry-run prune is a read.

Every route takes its profile from ``web.scope`` (``read_scope`` / ``write_scope``).
"""

from __future__ import annotations

import contextlib
import re
import sqlite3
from collections.abc import Generator
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from indus.foundation.errors import IndusError, ResumeTooLarge, StoreError
from indus.foundation.scope import Scope
from indus.service.maintenance_gate import MaintenanceGate, MaintenanceRefused
from indus.state import (
    connection,
    deletion,
    lineage,
    listing,
    maintenance,
    portability,
    registry,
    search,
    sessions,
    titles,
)
from indus.state.registry import StoreHandle
from indus.web.routes.sessions_store import (
    AUTOMATION_SOURCES,
    Execution,
    Identity,
    ReadView,
    Snapshot,
    active_count_in,
    check_identity,
    check_idle,
    execution_state,
    fail,
    ids_in,
    info_from_row,
    info_from_summary,
    message_json,
    now_of,
    reading,
    running_in,
    snapshot_in,
    store_failure,
    token_counts_in,
)
from indus.web.scope import read_scope, write_scope
from indus.web.state import dashboard_state

__all__ = ["GATE_KEY", "ROUTE_LIST", "router"]

router = APIRouter()

GATE_KEY: Final = "sessions.gate"
"""``DashboardState.extras`` key of a ``(scope) -> MaintenanceGate`` factory (tests)."""

MESSAGE_PAGE: Final = 500
LIST_MAX: Final = 100
SEARCH_MAX: Final = 50
BULK_MAX: Final = 500
DAY: Final = 86400.0

ROUTE_LIST: Final[tuple[tuple[str, str, bool], ...]] = (
    ("GET", "/api/sessions", True),
    ("GET", "/api/sessions/search", True),
    ("GET", "/api/sessions/stats", True),
    ("POST", "/api/sessions/prune", True),
    ("POST", "/api/sessions/bulk-delete", True),
    ("DELETE", "/api/sessions/empty", True),
    ("GET", "/api/sessions/{session_id}", True),
    ("GET", "/api/sessions/{session_id}/messages", True),
    ("GET", "/api/sessions/{session_id}/export", True),
    ("GET", "/api/sessions/{session_id}/mutation-snapshot", True),
    ("PATCH", "/api/sessions/{session_id}", True),
    ("DELETE", "/api/sessions/{session_id}", True),
)
"""``(method, path, scoped)`` of every route here (the SPA's generated route list)."""

ReadScope = Annotated[Scope, Depends(read_scope)]
WriteScope = Annotated[Scope, Depends(write_scope)]


# -- helpers ------------------------------------------------------------------------------------


def _csv(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def _gate(request: Request, scope: Scope) -> MaintenanceGate:
    factory: object = dashboard_state(request.app).extras.get(GATE_KEY)
    if callable(factory):
        made: object = factory(scope)
        if isinstance(made, MaintenanceGate):
            return made
    return MaintenanceGate(scope)


def _store_exists(scope: Scope) -> bool:
    return connection.default_store_path(scope).is_file()


@contextlib.contextmanager
def _writable(scope: Scope) -> Generator[StoreHandle]:
    """The shared writable handle of an existing store (404 when there is none)."""
    if not _store_exists(scope):
        fail(404, "not_found", "No such session.")
    try:
        handle = registry.acquire(scope)
    except StoreError as exc:
        raise store_failure(exc) from None
    try:
        yield handle
    except (sqlite3.Error, StoreError) as exc:
        if isinstance(exc, deletion.SessionGuarded | titles.TitleTaken):
            raise
        raise store_failure(exc) from None
    finally:
        handle.close()


@contextlib.contextmanager
def _offline(gate: MaintenanceGate, what: str) -> Generator[None]:
    """Bulk maintenance: refuse while a daemon owns the profile, else hold its ownership lock."""
    if gate.owner().daemon:
        fail(
            409,
            "daemon_running",
            f"The daemon owns this profile's sessions; stop it before {what}.",
        )
    try:
        with gate.locally(what):
            yield
    except MaintenanceRefused:
        fail(
            409,
            "daemon_running",
            f"The daemon is starting or another clean-up runs; stop it before {what}.",
        )


def _mark(text: str, needle: str) -> str:
    words = [w for w in needle.split() if w]
    if not words or not text:
        return text
    pattern = re.compile("|".join(re.escape(w) for w in words), re.IGNORECASE)
    return pattern.sub(lambda m: f"<mark>{m.group(0)}</mark>", text)


def _snippet(raw: str) -> str:
    return raw.replace(search.SNIPPET_OPEN, "<mark>").replace(search.SNIPPET_CLOSE, "</mark>")


def _row(conn: sqlite3.Connection, session_id: str) -> dict[str, Any] | None:
    found = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return dict(found) if found is not None else None


def _filters(
    *,
    source: str | None,
    sources: str | None,
    exclude_sources: str | None,
    tab: str,
    archived: str,
) -> listing.ListFilters:
    only = [source] if source else _csv(sources)
    without = _csv(exclude_sources)
    if not only and tab == "automation":
        only = list(AUTOMATION_SOURCES)
    elif not only and tab == "chats":
        without = list(dict.fromkeys([*without, *AUTOMATION_SOURCES]))
    flag = {"exclude": False, "only": True, "include": None}[archived]
    return listing.ListFilters(sources=only or None, exclude_sources=without or None, archived=flag)


# -- reads --------------------------------------------------------------------------------------


@router.get("/api/sessions")
def list_sessions(
    scope: ReadScope,
    source: str | None = None,
    sources: str | None = None,
    exclude_sources: str | None = None,
    tab: Literal["chats", "automation", "all"] = "all",
    order: Literal["created", "recent"] = "created",
    archived: Literal["exclude", "only", "include"] = "exclude",
    limit: Annotated[int, Query(ge=0, le=LIST_MAX)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    flt = _filters(
        source=source, sources=sources, exclude_sources=exclude_sources, tab=tab, archived=archived
    )
    with reading(scope) as view:
        if view is None:
            return {"sessions": [], "total": 0, "limit": limit, "offset": offset}
        handle = view.as_handle()
        rows = listing.list_sessions(
            handle, flt, limit, offset, order="activity" if order == "recent" else "started"
        )
        total = sum(listing.count_by_source(handle, flt).values())
        now = now_of(scope)
        with view.read() as conn:
            tokens = token_counts_in(conn, [r.id for r in rows])
            running = running_in(conn, now)
    shaped = [
        info_from_summary(r, tokens.get(r.id, (0, 0)), now, running=r.id in running) for r in rows
    ]
    return {"sessions": shaped, "total": total, "limit": limit, "offset": offset}


def _title_hits(
    view: ReadView, q: str, wanted: int, seen: set[str], flt: listing.ListFilters
) -> list[dict[str, Any]]:
    """Sessions whose id, title or first ask matches ``q`` (the index covers messages only)."""
    query = listing.ListFilters(
        sources=flt.sources,
        exclude_sources=flt.exclude_sources,
        archived=None,
        hidden=None,
        include_internal=True,
        query=q,
    )
    out: list[dict[str, Any]] = []
    for row in listing.list_sessions(view.as_handle(), query, limit=wanted + len(seen)):
        if len(out) >= wanted:
            break
        if row.id in seen:
            continue
        seen.add(row.id)
        text = row.title or row.preview or row.id
        out.append(
            {
                "session_id": row.id,
                "title": row.title,
                "source": row.source,
                "snippet": _mark(text, q),
                "role": None,
                "last_active": row.last_active if row.last_active is not None else row.started_at,
            }
        )
    return out


@router.get("/api/sessions/search")
def search_sessions(
    scope: ReadScope,
    q: str = "",
    limit: Annotated[int, Query(ge=1, le=SEARCH_MAX)] = 20,
    source: str | None = None,
    sources: str | None = None,
    exclude_sources: str | None = None,
) -> dict[str, Any]:
    needle = q.strip()[: search.MAX_QUERY_CHARS]
    if not needle:
        return {"results": []}
    flt = _filters(
        source=source,
        sources=sources,
        exclude_sources=exclude_sources,
        tab="all",
        archived="include",
    )
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    with reading(scope) as view:
        if view is None:
            return {"results": []}
        hits = search.search(
            view.as_handle(),
            needle,
            sources=flt.sources,
            exclude_sources=flt.exclude_sources,
            limit=limit * 5,
        )
        with view.read() as conn:
            for hit in hits:
                if len(results) >= limit:
                    break
                if hit.session_id in seen:
                    continue
                seen.add(hit.session_id)
                row = _row(conn, hit.session_id) or {}
                last = row.get("last_activity_at") or row.get("started_at") or hit.timestamp
                results.append(
                    {
                        "session_id": hit.session_id,
                        "title": row.get("title"),
                        "source": hit.source,
                        "snippet": _snippet(hit.snippet),
                        "role": hit.role,
                        "last_active": float(last),
                    }
                )
        if len(results) < limit:
            results.extend(_title_hits(view, needle, limit - len(results), seen, flt))
    return {"results": results}


@router.get("/api/sessions/stats")
def session_stats(scope: ReadScope) -> dict[str, Any]:
    with reading(scope) as view:
        if view is None:
            return {"total": 0, "active": 0, "archived": 0, "messages": 0, "by_source": {}}
        handle = view.as_handle()
        everything = listing.ListFilters(archived=None, hidden=None, include_internal=True)
        by_source = listing.count_by_source(handle, everything)
        archived_only = listing.ListFilters(archived=True, hidden=None, include_internal=True)
        archived = sum(listing.count_by_source(handle, archived_only).values())
        with view.read() as conn:
            messages = int(
                conn.execute("SELECT COUNT(*) FROM messages WHERE active = 1").fetchone()[0]
            )
            active = active_count_in(conn, now_of(scope))
    return {
        "total": sum(by_source.values()),
        "active": active,
        "archived": archived,
        "messages": messages,
        "by_source": by_source,
    }


@router.get("/api/sessions/{session_id}")
def one_session(session_id: str, scope: ReadScope) -> dict[str, Any]:
    with reading(scope) as view:
        if view is None:
            fail(404, "not_found", "No such session.")
        with view.read() as conn:
            row = _row(conn, session_id)
            if row is None:
                fail(404, "not_found", "No such session.")
            info = info_from_row(conn, row, now_of(scope))
    info["profile"] = scope.profile_id
    return info


def _conversation_ids(conn: sqlite3.Connection, session_id: str) -> list[str]:
    """The compression segments up to ``session_id``, oldest first (the whole visible history)."""
    try:
        return lineage.ancestors_in(conn, session_id)
    except IndusError:
        return [session_id]


@router.get("/api/sessions/{session_id}/messages")
def session_messages(
    session_id: str,
    scope: ReadScope,
    limit: Annotated[int | None, Query(ge=1, le=MESSAGE_PAGE)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    order: Literal["oldest", "latest"] | None = None,
) -> dict[str, Any]:
    """Without ``limit`` the latest page of 500; an explicit page counts from the start unless
    ``order=latest`` says otherwise."""
    if order is None:
        order = "latest" if limit is None else "oldest"
    if limit is None:
        limit = MESSAGE_PAGE
    with reading(scope) as view:
        if view is None:
            fail(404, "not_found", "No such session.")
        with view.read() as conn:
            if _row(conn, session_id) is None:
                fail(404, "not_found", "No such session.")
            ids = _conversation_ids(conn, session_id)
            marks = ",".join("?" * len(ids))
            where = f"session_id IN ({marks}) AND active = 1"
            total = int(
                conn.execute(f"SELECT COUNT(*) FROM messages WHERE {where}", ids).fetchone()[0]
            )
            direction = "DESC" if order == "latest" else "ASC"
            rows = conn.execute(
                "SELECT id, role, content, tool_calls, tool_call_id, tool_name, timestamp "
                f"FROM messages WHERE {where} ORDER BY id {direction} LIMIT ? OFFSET ?",
                [*ids, limit, offset],
            ).fetchall()
    if order == "latest":
        rows.reverse()
    page = [message_json(r) for r in rows]
    return {
        "session_id": session_id,
        "messages": page,
        "pagination": {
            "limit": limit,
            "offset": offset,
            "order": order,
            "returned": len(page),
            "total": total,
        },
    }


@router.get("/api/sessions/{session_id}/export")
def export_session(session_id: str, scope: ReadScope) -> dict[str, Any]:
    with reading(scope) as view:
        if view is None:
            fail(404, "not_found", "No such session.")
        try:
            objects = list(
                portability.export_session(
                    view.as_handle(), session_id, lineage=True, include_inactive=True, guard=True
                )
            )
        except ResumeTooLarge:
            fail(
                413,
                "export_too_large",
                "This session is too large to export here; use `indus sessions export`.",
            )
    if not objects:
        fail(404, "not_found", "No such session.")
    return {"format": portability.FORMAT, "session_id": session_id, "objects": objects}


@router.get("/api/sessions/{session_id}/mutation-snapshot")
def mutation_snapshot(session_id: str, scope: ReadScope) -> dict[str, Any]:
    with reading(scope) as view:
        if view is None:
            return {"session_id": session_id, "exists": False, "revision": 0, "generation": None}
        with view.read() as conn:
            return snapshot_in(conn, session_id).to_json()


# -- bulk maintenance -----------------------------------------------------------------------------


class PruneRequest(BaseModel):
    older_than_days: int = Field(ge=1, le=36500)
    dry_run: bool = True


class BulkDelete(BaseModel):
    ids: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        min_length=1, max_length=BULK_MAX
    )


def _open_quiet(conn: sqlite3.Connection, cutoff: float) -> int:
    found = conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE ended_at IS NULL "
        "AND COALESCE(last_activity_at, started_at) < ?",
        (cutoff,),
    ).fetchone()
    return int(found[0])


@router.post("/api/sessions/prune")
def prune_sessions(request: Request, body: PruneRequest, scope: WriteScope) -> dict[str, Any]:
    cutoff = now_of(scope) - body.older_than_days * DAY
    if body.dry_run:
        with reading(scope) as view:
            if view is None:
                return {"dry_run": True, "count": 0, "skipped_open": 0}
            plan = maintenance.prune(view.as_handle(), body.older_than_days, dry_run=True)
            with view.read() as conn:
                skipped = _open_quiet(conn, cutoff) + len(plan.spared_guarded)
        return {"dry_run": True, "count": plan.count, "skipped_open": skipped}
    with _offline(_gate(request, scope), "pruning sessions"):
        if not _store_exists(scope):
            return {"dry_run": False, "count": 0, "skipped_open": 0}
        with _private(scope) as handle:
            with handle.read() as conn:
                open_quiet = _open_quiet(conn, cutoff)
            report = maintenance.prune(handle, body.older_than_days)
    return {
        "dry_run": False,
        "count": report.count,
        "skipped_open": open_quiet + len(report.spared_guarded),
    }


@contextlib.contextmanager
def _private(scope: Scope) -> Generator[StoreHandle]:
    try:
        handle = registry.open_unshared(scope)
    except StoreError as exc:
        raise store_failure(exc) from None
    try:
        yield handle
    except (sqlite3.Error, StoreError) as exc:
        raise store_failure(exc) from None
    finally:
        handle.close()


@router.post("/api/sessions/bulk-delete")
def bulk_delete(request: Request, body: BulkDelete, scope: WriteScope) -> dict[str, Any]:
    wanted = ids_in(body.ids)
    with _offline(_gate(request, scope), "deleting sessions in bulk"):
        if not _store_exists(scope):
            return {"ok": True, "deleted": 0, "skipped_active": []}
        with _private(scope) as handle:
            report = deletion.delete_many(handle, wanted, whole_chain=True)
    return {"ok": True, "deleted": len(report.deleted), "skipped_active": report.guarded}


def _empty_ids(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT s.id FROM sessions s WHERE s.ended_at IS NOT NULL AND s.archived = 0 "
        "AND s.title IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.session_id = s.id)"
    ).fetchall()
    return [str(r[0]) for r in rows]


@router.delete("/api/sessions/empty")
def delete_empty(request: Request, scope: WriteScope) -> dict[str, Any]:
    with _offline(_gate(request, scope), "deleting empty sessions"):
        if not _store_exists(scope):
            return {"ok": True, "deleted": 0}
        with _private(scope) as handle:
            with handle.read() as conn:
                candidates = _empty_ids(conn)
            deleted = sum(1 for sid in candidates if deletion.delete_if_empty(handle, sid))
    return {"ok": True, "deleted": deleted}


# -- one-session writes ---------------------------------------------------------------------------


class SessionPatch(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    archived: bool | None = None
    pinned: bool | None = None
    request_id: str | None = Field(default=None, max_length=128)
    expected_revision: int | None = None
    expected_generation: int | None = None


def _read_fence(scope: Scope, session_id: str) -> tuple[Snapshot, Execution]:
    """The session's snapshot and turn state, read-only (absent when there is no store)."""
    with reading(scope) as view:
        if view is None:
            return Snapshot(session_id, False, 0, None), "idle"
        with view.read() as conn:
            return snapshot_in(conn, session_id), execution_state(conn, session_id, now_of(scope))


@router.patch("/api/sessions/{session_id}")
def patch_session(session_id: str, body: SessionPatch, scope: WriteScope) -> dict[str, Any]:
    identity = Identity(body.request_id, body.expected_revision, body.expected_generation)
    snap, _ = _read_fence(scope, session_id)
    check_identity(identity, snap)
    if not snap.exists:
        fail(404, "not_found", "No such session.")
    fields = body.model_fields_set

    def change(conn: sqlite3.Connection) -> None:
        if "title" in fields:
            titles.set_title_in(conn, session_id, body.title, source="user")
        if body.archived is not None:
            sessions.set_archived_in(conn, session_id, body.archived)
        if body.pinned is not None:
            sessions.set_pinned_in(conn, session_id, body.pinned)

    with _writable(scope) as handle:
        try:
            handle.write(change)
        except titles.TitleTaken:
            fail(409, "title_taken", "Another session already has that title.")
        with handle.read() as conn:
            row = _row(conn, session_id)
            if row is None:
                fail(404, "not_found", "No such session.")
            return info_from_row(conn, row, now_of(scope))


@router.delete("/api/sessions/{session_id}")
def delete_session(
    request: Request,
    session_id: str,
    scope: WriteScope,
    request_id: Annotated[str | None, Query(max_length=128)] = None,
    expected_revision: int | None = None,
    expected_generation: int | None = None,
) -> dict[str, Any]:
    identity = Identity(request_id, expected_revision, expected_generation)
    snap, state = _read_fence(scope, session_id)
    check_identity(identity, snap)
    if not snap.exists:
        return {"ok": True, "session_id": session_id, "deleted": False, "already_absent": True}
    check_idle(state)

    def here() -> bool:
        with _writable(scope) as handle:
            return deletion.delete_session(handle, session_id, whole_chain=True)

    try:
        done = _gate(request, scope).run(
            "delete", {"session_id": session_id}, here, what="deleting a session"
        )
    except deletion.SessionGuarded:
        fail(409, "session_busy", "This session has a turn queued or running.")
    except MaintenanceRefused as exc:
        fail(409, "daemon_refused", exc.message)
    deleted = bool(done.value.get("removed")) if done.where == "daemon" else bool(done.value)
    return {"ok": True, "session_id": session_id, "deleted": deleted, "already_absent": not deleted}
