"""``/api/session-import/*``: preview and import sessions of two other coding-agent CLIs (P24-28).

The desktop's Command center (and any dashboard page) lists the Claude Code and Codex session
files found in the home of the user the server runs as, previews one as our messages, and imports
it into the named profile's store. The mapping is ``indus.service.session_import``.

- ``GET /api/session-import/candidates?source=claude-code|codex`` → ``{sources, candidates[]}``.
- ``POST /api/session-import/preview {source, path}`` → the mapped messages (nothing written).
- ``POST /api/session-import/import {source, path}`` → ``{session_id, title, message_count}``;
  409 ``already_imported`` (with the existing ``session_id``) for a file imported before.

Only a file under the source's own session root is accepted (400 ``not_a_candidate``), so the
route never reads an arbitrary path; an unknown format is 422 ``unknown_format``. Errors use the
``{detail: {code, message}}`` envelope the clients read.

The home folder is the scope's ``HOME`` (``USERPROFILE`` on Windows), never a process-env read.
Seam (``DashboardState.extras``): ``session_import.home`` replaces it (tests).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Final, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from indus.foundation.errors import StoreError
from indus.foundation.scope import Scope
from indus.service import session_import
from indus.state import registry
from indus.web.scope import read_scope, write_scope
from indus.web.state import dashboard_state

__all__ = ["ROUTE_LIST", "router"]

router = APIRouter()

ROUTE_LIST: Final[tuple[tuple[str, str, bool], ...]] = (
    ("GET", "/api/session-import/candidates", True),
    ("POST", "/api/session-import/preview", True),
    ("POST", "/api/session-import/import", True),
)
"""``(method, path, scoped)`` of every route here."""

ReadScope = Annotated[Scope, Depends(read_scope)]
WriteScope = Annotated[Scope, Depends(write_scope)]

_STATUS: Final = {
    "unknown_source": 400,
    "not_a_candidate": 400,
    "not_found": 404,
    "unreadable": 422,
    "unknown_format": 422,
    "empty": 422,
    "already_imported": 409,
}


def _fail(status: int, code: str, message: str, **extra: Any) -> NoReturn:
    raise HTTPException(status_code=status, detail={"code": code, "message": message, **extra})


def _refused(exc: session_import.ImportRefused) -> NoReturn:
    extra = {} if exc.session_id is None else {"session_id": exc.session_id}
    _fail(_STATUS.get(exc.code, 400), exc.code, str(exc), **extra)


def _home(request: Request, scope: Scope) -> Path | None:
    """The home folder of the user the server runs as, from the scope's env view (no process env
    read here); None when it names none."""
    found = dashboard_state(request.app).extras.get("session_import.home")
    if found is not None:
        return Path(found)
    raw = scope.env.get("HOME") or scope.env.get("USERPROFILE") or ""
    return Path(raw) if raw else None


def _need_home(request: Request, scope: Scope) -> Path:
    home = _home(request, scope)
    if home is None:
        _fail(404, "not_found", "The server's user has no home folder to look in.")
    return home


class FileBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(max_length=32)
    path: str = Field(min_length=1, max_length=4096)


@router.get("/api/session-import/candidates")
def list_candidates(
    request: Request, scope: ReadScope, source: str | None = None
) -> dict[str, Any]:
    try:
        kind = None if source is None else session_import.source_kind(source)
    except session_import.ImportRefused as exc:
        _refused(exc)
    home = _home(request, scope)
    rows = [] if home is None else session_import.candidates(home, kind=kind)
    return {"sources": list(session_import.KINDS), "candidates": [r.to_dict() for r in rows]}


@router.post("/api/session-import/preview")
def preview_file(request: Request, body: FileBody, scope: WriteScope) -> dict[str, Any]:
    try:
        path = session_import.resolve_candidate(_need_home(request, scope), body.source, body.path)
        kind = session_import.source_kind(body.source)
        return session_import.preview(path, kind).to_dict()
    except session_import.ImportRefused as exc:
        _refused(exc)


@router.post("/api/session-import/import")
def import_file(request: Request, body: FileBody, scope: WriteScope) -> dict[str, Any]:
    try:
        path = session_import.resolve_candidate(_need_home(request, scope), body.source, body.path)
        kind = session_import.source_kind(body.source)
    except session_import.ImportRefused as exc:
        _refused(exc)
    try:
        handle = registry.acquire(scope)
    except StoreError:
        _fail(503, "store_unavailable", "The session store cannot be opened right now.")
    try:
        return session_import.import_session(handle, path, kind).to_dict()
    except session_import.ImportRefused as exc:
        _refused(exc)
    finally:
        handle.close()
