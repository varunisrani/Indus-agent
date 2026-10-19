# pyright: strict
"""``session_search`` (P09-25, F31): look up this profile's earlier conversations.

Actions:

- ``discover {query, sort?: rank|newest, limit?}`` — full-text search over stored messages (the
  P02-15 search); each hit names the session, the message, the role, the time, the session title and
  a snippet with the match between ``[[`` and ``]]``;
- ``scroll {query?, offset?}`` — the next page of this session's last ``discover`` (or of an
  explicit query from ``offset``);
- ``read {session_id, message_id?, window?, offset?}`` — at most ``READ_MAX`` messages around a
  hit (or from ``offset`` in the session); a tool output is clipped to ``TOOL_CLIP`` characters;
- ``browse {limit?}`` — recent conversations with titles and previews (P02-09 listing).

The current conversation — every segment of its compression lineage — and hidden sources (``cron``,
``subagent``) are left out unless the call sets ``include_current`` / ``include_hidden``. Every text
that leaves passes the redactor. One answer is at most ``MAX_CHARS`` characters: past that, items
are dropped from the end and the answer says where to continue. With ``session_search.summarize``
on and an auxiliary client present, a ``read`` window that does not fit is condensed by the
side-task model (task ``session_search``); if that fails the cut answer is returned.

The loop runs the tool itself (inline table, P04-09) because it needs the session's id and store;
the registry entry carries the schema.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from indus.core.persist import StateStore
from indus.core.turn_state import TurnState
from indus.foundation.blocking import run_blocking
from indus.foundation.scope import Scope
from indus.safety.redaction import redact_output
from indus.state import compression, listing, messages, search, sessions
from indus.state.listing import ListFilters
from indus.state.registry import StoreHandle
from indus.tools.calls import ToolCall, ToolResult
from indus.tools.registry import ToolRegistry, ToolTraits

__all__ = [
    "BROWSE_DEFAULT",
    "DISCOVER_DEFAULT",
    "HIDDEN_SOURCES",
    "MAX_CHARS",
    "READ_MAX",
    "SCHEMA",
    "TOOL_CLIP",
    "TOOL_NAME",
    "SessionSearch",
    "SessionSearchWiring",
    "register_tools",
    "session_search_wiring",
]

log = logging.getLogger(__name__)

TOOL_NAME = "session_search"
TOOLSET = "session_search"
DISCOVER_DEFAULT = 10
DISCOVER_MAX = 25
BROWSE_DEFAULT = 10
BROWSE_MAX = 25
READ_DEFAULT = 20
READ_MAX = 40
TOOL_CLIP = 1000
MAX_CHARS = 12_000
HIDDEN_SOURCES = ("cron", "subagent")
SUMMARIZE_KEY = "session_search.summarize"
AUX_TASK = "session_search"
_CLIP_MARK = " … [clipped]"

SCHEMA: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": (
        "Look up earlier conversations with this user. 'discover' searches past messages by "
        "words (quote a phrase to match it exactly); 'scroll' shows the next page of the last "
        "search; 'read' opens the messages around a hit (session_id + message_id from a "
        "discover result); 'browse' lists recent conversations. The current conversation and "
        "scheduled or sub-agent runs are left out unless you ask for them."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["discover", "scroll", "read", "browse"]},
            "query": {"type": "string", "description": "discover: the words to look for."},
            "sort": {
                "type": "string",
                "enum": ["rank", "newest"],
                "description": "discover: best match first (rank, default) or newest first.",
            },
            "limit": {
                "type": "integer",
                "description": f"discover/browse: how many results (default {DISCOVER_DEFAULT}, "
                f"at most {DISCOVER_MAX}).",
            },
            "offset": {
                "type": "integer",
                "description": "scroll: where to continue (next_offset of the last answer); "
                "read: the first message to show when no message_id is given.",
            },
            "session_id": {"type": "string", "description": "read: the conversation to open."},
            "message_id": {
                "type": "integer",
                "description": "read: the message to centre the window on.",
            },
            "window": {
                "type": "integer",
                "description": f"read: how many messages (default {READ_DEFAULT}, at most "
                f"{READ_MAX}).",
            },
            "include_current": {
                "type": "boolean",
                "description": "Also search this conversation (default false).",
            },
            "include_hidden": {
                "type": "boolean",
                "description": "Also search scheduled jobs and sub-agent runs (default false).",
            },
        },
        "required": ["action"],
    },
}


class AuxCaller(Protocol):
    """The part of the P08-17 auxiliary client this tool uses."""

    async def call(self, task: str, messages: Sequence[dict[str, Any]]) -> Any: ...


class _Refused(Exception):
    pass


def _int(value: object, default: int, low: int, high: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        number = int(value)
    elif isinstance(value, str) and value.strip().lstrip("-").isdigit():
        number = int(value.strip())
    else:
        return default
    return max(low, min(high, number))


def _flag(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _when(stamp: object) -> str:
    if not isinstance(stamp, (int, float)):
        return ""
    return datetime.fromtimestamp(float(stamp), UTC).strftime("%Y-%m-%d %H:%M UTC")


def _text_of(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in cast(list[object], content):
            if isinstance(part, Mapping):
                text = cast(Mapping[str, object], part).get("text")
                if isinstance(text, str):
                    parts.append(text)
                elif cast(Mapping[str, object], part).get("type") == "image_url":
                    parts.append("[image]")
        return "\n".join(parts)
    return "" if content is None else str(content)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(0, limit - len(_CLIP_MARK))] + _CLIP_MARK


def _size(body: Mapping[str, Any]) -> int:
    return len(json.dumps(body, ensure_ascii=False))


@dataclass
class SessionSearch:
    """The tool over one store, for one session (``current`` follows a rotating compaction)."""

    store: StoreHandle
    scope: Scope
    current: Callable[[], str]
    aux: AuxCaller | None = None
    _last: dict[str, Any] | None = field(default=None)

    # -- helpers ----------------------------------------------------------------------------------

    def _redact(self, text: str) -> str:
        return redact_output(text, force=True, scope=self.scope)

    def _lineage(self) -> set[str]:
        current = self.current()
        if not current:
            return set()
        try:
            return set(compression.lineage_ids(self.store, current)) | {current}
        except Exception:
            log.debug("lineage of %s unavailable; excluding only it", current, exc_info=True)
            return {current}

    def _summarize_on(self) -> bool:
        config = self.scope.config
        if config is None or self.aux is None:
            return False
        try:
            return config.get(SUMMARIZE_KEY) is True
        except Exception:
            return False

    # -- discover / scroll ------------------------------------------------------------------------

    def discover(
        self,
        query: str,
        *,
        sort: str = "rank",
        limit: int = DISCOVER_DEFAULT,
        offset: int = 0,
        include_current: bool = False,
        include_hidden: bool = False,
    ) -> dict[str, Any]:
        if not query.strip():
            raise _Refused("discover needs a query: the words to look for")
        if sort not in ("rank", "newest"):
            sort = "rank"
        skip: set[str] = set() if include_current else self._lineage()
        hidden = None if include_hidden else HIDDEN_SOURCES
        kept: list[search.Hit] = []
        after: list[int] = []  # the raw result position just past each kept hit
        position = offset
        exhausted = False
        page = max(limit * 2, 20)
        while len(kept) < limit and not exhausted:
            hits = search.search(
                self.store,
                query,
                exclude_sources=hidden,
                sort=cast(Any, sort),
                limit=page,
                offset=position,
            )
            exhausted = len(hits) < page
            for hit in hits:
                position += 1
                if hit.session_id in skip:
                    continue
                kept.append(hit)
                after.append(position)
                if len(kept) >= limit:
                    break
        titles: dict[str, str] = {}
        for hit in kept:
            if hit.session_id not in titles:
                row = sessions.get(self.store, hit.session_id) or {}
                titles[hit.session_id] = str(row.get("title") or "")
        items = [
            {
                "session_id": hit.session_id,
                "message_id": hit.message_id,
                "role": hit.role,
                "when": _when(hit.timestamp),
                "title": self._redact(titles[hit.session_id]),
                "source": hit.source,
                "snippet": self._redact(hit.snippet),
            }
            for hit in kept
        ]
        more = not exhausted or len(kept) >= limit
        self._last = {
            "query": query,
            "sort": sort,
            "limit": limit,
            "include_current": include_current,
            "include_hidden": include_hidden,
        }
        return self._fit_hits(query, items, after, position, more)

    def _fit_hits(
        self, query: str, items: list[dict[str, Any]], after: list[int], end: int, more: bool
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"success": True, "query": query, "hits": items}
        if not items:
            body["message"] = "No earlier messages matched."
        cut = False
        while _size(body) > MAX_CHARS and len(items) > 1:
            items.pop()
            cut = True
        if cut:
            # The dropped hits come back on the next scroll: continue right after the last shown.
            end = after[len(items) - 1]
            more = True
            body["cut"] = True
        if more:
            body["next_offset"] = end
            body["hint"] = "Call session_search with action 'scroll' for more."
            if self._last is not None:
                self._last["offset"] = end
        elif self._last is not None:
            self._last["offset"] = None
        return body

    def scroll(self, query: str | None, offset: int | None) -> dict[str, Any]:
        last = self._last
        if query and query.strip():
            base = dict(last or {})
            base.update({"query": query, "offset": offset or 0})
            last = base
        if last is None or not last.get("query"):
            raise _Refused("nothing to scroll: run a discover first (or pass query and offset)")
        start = offset if offset is not None else last.get("offset")
        if start is None:
            return {"success": True, "query": last["query"], "hits": [], "message": "No more hits."}
        return self.discover(
            str(last["query"]),
            sort=str(last.get("sort", "rank")),
            limit=int(last.get("limit", DISCOVER_DEFAULT)),
            offset=int(start),
            include_current=bool(last.get("include_current")),
            include_hidden=bool(last.get("include_hidden")),
        )

    # -- read -------------------------------------------------------------------------------------

    def _readable(self, session_id: str, include_current: bool, include_hidden: bool) -> None:
        row = sessions.get(self.store, session_id)
        if row is None:
            raise _Refused(f"no session {session_id!r}; use an id from discover or browse")
        if not include_current and session_id in self._lineage():
            raise _Refused("that is the current conversation; set include_current to read it")
        if not include_hidden and row.get("source") in HIDDEN_SOURCES:
            raise _Refused("that session is a scheduled or sub-agent run; set include_hidden")

    def read_window(
        self,
        session_id: str,
        *,
        message_id: int | None,
        window: int,
        offset: int,
        include_current: bool,
        include_hidden: bool,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        self._readable(session_id, include_current, include_hidden)
        rows = [
            r
            for r in messages.get(self.store, session_id, include_inactive=True)
            if r.get("active") == 1 or r.get("compacted") == 1
        ]
        if message_id is not None:
            ids = [int(r["id"]) for r in rows]
            if message_id not in ids:
                raise _Refused(f"message {message_id} is not in session {session_id}")
            at = ids.index(message_id)
            start = max(0, min(at - window // 2, len(rows) - window))
        else:
            start = max(0, min(offset, len(rows)))
        chosen = rows[start : start + window]
        shown: list[dict[str, Any]] = []
        for row in chosen:
            text = _text_of(row.get("content"))
            if row.get("role") == "tool":
                text = _clip(text, TOOL_CLIP)
            item: dict[str, Any] = {
                "message_id": int(row["id"]),
                "role": row.get("role"),
                "when": _when(row.get("timestamp")),
                "text": self._redact(text),
            }
            if row.get("tool_name"):
                item["tool_name"] = row["tool_name"]
            calls = row.get("tool_calls")
            if isinstance(calls, list) and calls:
                names = [
                    str(cast(Mapping[str, Any], c).get("function", {}).get("name", ""))
                    for c in cast(list[object], calls)
                    if isinstance(c, Mapping)
                ]
                item["tool_calls"] = [n for n in names if n]
            shown.append(item)
        row = sessions.get(self.store, session_id) or {}
        head: dict[str, Any] = {
            "success": True,
            "session_id": session_id,
            "title": self._redact(str(row.get("title") or "")),
            "started": _when(row.get("started_at")),
            "total_messages": len(rows),
            "first_index": start,
        }
        if start + len(chosen) < len(rows):
            head["next_offset"] = start + len(chosen)
        return head, shown

    def fit_read(self, head: dict[str, Any], shown: list[dict[str, Any]]) -> dict[str, Any]:
        body = {**head, "messages": shown}
        if _size(body) <= MAX_CHARS:
            return body
        while len(shown) > 1 and _size({**head, "messages": shown}) > MAX_CHARS:
            shown = shown[:-1]
        if _size({**head, "messages": shown}) > MAX_CHARS and shown:
            room = MAX_CHARS - _size({**head, "messages": [{**shown[0], "text": ""}]}) - 50
            shown = [{**shown[0], "text": _clip(str(shown[0]["text"]), max(200, room))}]
        body = {**head, "messages": shown}
        body["next_offset"] = int(head["first_index"]) + len(shown)
        body["hint"] = (
            "The window was cut to fit; call read again with offset next_offset (and no "
            "message_id) for the rest."
        )
        return body

    async def condense(self, head: dict[str, Any], shown: list[dict[str, Any]]) -> dict[str, Any]:
        """Ask the side-task model for a summary of a window too long to return whole."""
        assert self.aux is not None
        transcript = "\n\n".join(f"[{m['message_id']}] {m['role']}: {m['text']}" for m in shown)
        prompt = (
            "Condense this excerpt of an earlier conversation for an assistant that needs to "
            "recall it. Keep decisions, facts, names, numbers, file paths, commands and open "
            "questions; drop pleasantries. Mention message ids in brackets for the key points. "
            "Answer in plain text under 1,500 words.\n\n" + transcript
        )
        result = await self.aux.call(AUX_TASK, [{"role": "user", "content": prompt}])
        text = str(getattr(result, "text", "") or "").strip()
        if not text:
            raise ValueError("the side-task model returned nothing")
        summary = self._redact(text)
        room = MAX_CHARS - _size({**head, "summary": "", "condensed_messages": len(shown)})
        body = {**head, "summary": _clip(summary, max(200, room)), "condensed_messages": len(shown)}
        return body

    # -- browse -----------------------------------------------------------------------------------

    def browse(self, *, limit: int, include_current: bool, include_hidden: bool) -> dict[str, Any]:
        skip: set[str] = set() if include_current else self._lineage()
        filters = ListFilters(
            exclude_sources=None if include_hidden else HIDDEN_SOURCES,
            include_subagents=include_hidden,
            include_internal=include_hidden,
        )
        rows = listing.list_sessions(self.store, filters, limit=limit + len(skip) + 5)
        items: list[dict[str, Any]] = []
        for row in rows:
            family = {row.id, row.root_id or row.id, *row.lineage_ids}
            if family & skip:
                continue
            items.append(
                {
                    "session_id": row.id,
                    "title": self._redact(row.title or ""),
                    "preview": self._redact(row.preview),
                    "started": _when(row.started_at),
                    "last_active": _when(row.last_active),
                    "source": row.source,
                    "messages": row.message_count,
                }
            )
            if len(items) >= limit:
                break
        body: dict[str, Any] = {"success": True, "sessions": items}
        while _size(body) > MAX_CHARS and len(items) > 1:
            items.pop()
            body["hint"] = "The list was cut to fit; ask for a smaller limit or use discover."
        return body

    # -- entry point ------------------------------------------------------------------------------

    async def run(self, args: Mapping[str, Any]) -> dict[str, Any]:
        action = args.get("action")
        include_current = _flag(args.get("include_current"))
        include_hidden = _flag(args.get("include_hidden"))
        try:
            if action == "discover":
                query = args.get("query")
                return await run_blocking(
                    lambda: self.discover(
                        query if isinstance(query, str) else "",
                        sort=str(args.get("sort") or "rank"),
                        limit=_int(args.get("limit"), DISCOVER_DEFAULT, 1, DISCOVER_MAX),
                        include_current=include_current,
                        include_hidden=include_hidden,
                    ),
                    label="session search",
                )
            if action == "scroll":
                query = args.get("query")
                raw = args.get("offset")
                offset = _int(raw, 0, 0, 1_000_000) if raw is not None else None
                return await run_blocking(
                    lambda: self.scroll(query if isinstance(query, str) else None, offset),
                    label="session search",
                )
            if action == "read":
                return await self._read(args, include_current, include_hidden)
            if action == "browse":
                limit = _int(args.get("limit"), BROWSE_DEFAULT, 1, BROWSE_MAX)
                return await run_blocking(
                    lambda: self.browse(
                        limit=limit,
                        include_current=include_current,
                        include_hidden=include_hidden,
                    ),
                    label="session browse",
                )
            raise _Refused("action must be discover, scroll, read or browse")
        except _Refused as exc:
            return {"success": False, "error": str(exc)}
        except Exception as exc:
            log.warning("session_search failed: %s", exc, exc_info=True)
            return {"success": False, "error": f"session search failed: {type(exc).__name__}"}

    async def _read(
        self, args: Mapping[str, Any], include_current: bool, include_hidden: bool
    ) -> dict[str, Any]:
        session_id = args.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            raise _Refused("read needs a session_id (from discover or browse)")
        raw_message = args.get("message_id")
        message_id = _int(raw_message, -1, 0, 2**62) if raw_message is not None else None
        head, shown = await run_blocking(
            lambda: self.read_window(
                session_id.strip(),
                message_id=message_id,
                window=_int(args.get("window"), READ_DEFAULT, 1, READ_MAX),
                offset=_int(args.get("offset"), 0, 0, 1_000_000),
                include_current=include_current,
                include_hidden=include_hidden,
            ),
            label="session read",
        )
        whole = {**head, "messages": shown}
        if _size(whole) > MAX_CHARS and self._summarize_on():
            try:
                return await self.condense(head, shown)
            except Exception as exc:
                log.info("session_search summary failed (%s); returning the cut window", exc)
        return self.fit_read(head, shown)


# -- registry and loop wiring ---------------------------------------------------------------------


def _outside_the_loop(args: Mapping[str, Any]) -> str:
    return ToolResult.error(
        "session_search is run by the agent loop; it is not available in this context"
    ).text


def register_tools(registry: ToolRegistry) -> None:
    """The schema-only registry entry (the loop runs it through ``SessionSearchWiring``)."""
    registry.register(
        TOOL_NAME, TOOLSET, SCHEMA, _outside_the_loop, traits=ToolTraits(read_only=True)
    )


@dataclass
class SessionSearchWiring:
    """Builds the tool lazily from the turn's runtime (store and session id)."""

    scope: Scope
    aux: AuxCaller | None = None
    _tool: SessionSearch | None = None

    def tool_for(self, state: TurnState) -> SessionSearch | None:
        runtime = state.runtime
        store = runtime.store
        if not isinstance(store, StateStore):
            return None
        if self._tool is None or self._tool.store is not store.handle:
            self._tool = SessionSearch(
                store.handle, self.scope, current=lambda: runtime.session_id, aux=self.aux
            )
        return self._tool

    async def execute(self, call: ToolCall, state: TurnState) -> ToolResult:
        tool = self.tool_for(state)
        if tool is None:
            body: dict[str, Any] = {
                "success": False,
                "error": "earlier conversations are not stored for this session, so there is "
                "nothing to search",
            }
        else:
            body = await tool.run(call.args)
        return ToolResult(json.dumps(body, ensure_ascii=False), is_error=not body.get("success"))


def session_search_wiring(scope: Scope, aux: AuxCaller | None = None) -> SessionSearchWiring:
    return SessionSearchWiring(scope, aux)
