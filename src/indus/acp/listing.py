"""``session/list``: the editor's history panel (P19-07).

At most 1,000 ``acp``-source conversations are read (read-only, both host modes), one row per
logical conversation, newest activity first. The editor's ``cwd`` filters them, both sides
compared by ``paths.comparable`` (``~``, Windows drive → WSL mount, ``realpath`` so macOS
``/var`` and ``/private/var`` agree; R09.16). Pages hold 50 rows; the ``cursor`` is the last
session id of the previous page; an unknown cursor gives an empty page — never the first page
again; ``nextCursor`` is set only when more rows remain. Listing needs no daemon: before any
session call has picked a host, the store is read directly (the same read both hosts do).
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final

from indus.acp import paths, store_view
from indus.acp.agent import AcpAgent, handler, wire
from indus.acp.backend import ListedSession
from indus.foundation.blocking import run_blocking

__all__ = ["PAGE_SIZE", "Page", "page", "title_of"]

PAGE_SIZE: Final = 50
FALLBACK_TITLE: Final = "New conversation"


def title_of(row: ListedSession) -> str:
    """Stored title, else the first ask, else the folder's name, else a fixed label."""
    for candidate in (row.title, row.preview):
        if candidate and candidate.strip():
            return candidate.strip()
    leaf = os.path.basename((row.cwd or "").rstrip("/\\"))
    return leaf or FALLBACK_TITLE


def _stamp(value: float | None) -> str | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


type Page = tuple[list[ListedSession], str | None]


def page(
    rows: Sequence[ListedSession],
    *,
    cwd: str | None = None,
    cursor: str | None = None,
    size: int = PAGE_SIZE,
) -> Page:
    """One page of ``rows`` (already newest first) and the next cursor (module notes)."""
    wanted = paths.comparable(cwd) if cwd else None
    chosen = [r for r in rows if wanted is None or paths.comparable(r.cwd) == wanted]
    if cursor:
        index = next((i for i, r in enumerate(chosen) if r.session_id == cursor), None)
        chosen = [] if index is None else chosen[index + 1 :]
    shown = chosen[:size]
    more = len(chosen) > size
    return shown, (shown[-1].session_id if more and shown else None)


@handler("session/list")
async def list_sessions(agent: AcpAgent, params: Mapping[str, Any]) -> dict[str, Any]:
    from acp.schema import ListSessionsRequest, ListSessionsResponse

    request = ListSessionsRequest.model_validate(params)
    cwd = paths.translate_cwd(request.cwd, wsl=agent.wsl) if request.cwd else None
    backend = agent.selector.built
    if backend is not None:
        rows = await backend.list_sessions()
    else:  # listing needs no daemon: the store is read the same way in both modes
        rows = await run_blocking(store_view.listed, agent.scope, label="acp session list")
    shown, following = page(rows, cwd=cwd, cursor=request.cursor)
    response = ListSessionsResponse.model_validate(
        {
            "sessions": [
                {
                    "sessionId": row.session_id,
                    "cwd": row.cwd or "",
                    "title": title_of(row),
                    "updatedAt": _stamp(row.updated_at),
                }
                for row in shown
            ],
            "nextCursor": following,
        }
    )
    return wire(response)
