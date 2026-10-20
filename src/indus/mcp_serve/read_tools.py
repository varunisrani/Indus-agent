"""The read tools of ``indus mcp serve`` (P19-14): conversations, messages, attachments, channels.

Everything here reads the store through the server's one read-only connection (``StoreReader``)
and works without a running gateway. A conversation is a row of the gateway routing index
(``gateway_routes``: session key → current session, origin, display name); its messages are the
live transcript rows of that session in row-id order (R03.14). Each public method returns the
JSON-ready answer of one tool; problems come back as ``{"error": ...}`` (the server wraps every
call so nothing raises past it).

``MEDIA:`` paths written in a message are reported only when the media path policy (R06.22) would
let the gateway send that file; the others are listed under ``refused`` with the reason.
"""

from __future__ import annotations

import contextlib
import json
import math
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast

from indus.foundation.scope import Scope
from indus.mcp_serve.store import StoreReader
from indus.safety.media_policy import MediaPolicy, extract_media
from indus.state import messages as message_rows
from indus.state.handoff import is_handoff

__all__ = [
    "CONTENT_CHARS",
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "Channel",
    "Conversation",
    "ReadTools",
    "attachments_of",
    "coerce_int",
    "directory_path",
    "iso_time",
    "load_directory",
    "message_text",
]

DEFAULT_LIMIT: Final = 50
MAX_LIMIT: Final = 200
CONTENT_CHARS: Final = 2000
TEXT_ROLES: Final = frozenset({"user", "assistant"})

_ROUTE_COLUMNS: Final = (
    "session_key, session_id, platform, chat_type, origin_json, display_name, "
    "created_at, updated_at"
)


def coerce_int(value: object, *, default: int, low: int, high: int) -> int:
    """``value`` as an integer clamped to ``[low, high]``; anything that is not a number (a
    boolean, a word, null) gives ``default``."""
    number: int = default
    if isinstance(value, bool) or value is None:
        pass
    elif isinstance(value, int):
        number = value
    elif isinstance(value, float):
        number = _whole(value, default)
    elif isinstance(value, str):
        try:
            number = int(value.strip())
        except ValueError:
            with contextlib.suppress(ValueError):
                number = _whole(float(value.strip()), default)
    return max(low, min(number, high))


def _whole(value: float, default: int) -> int:
    return int(value) if math.isfinite(value) else default


def iso_time(value: object) -> str:
    """A stored epoch time as ISO-8601 UTC, or ``""``."""
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return ""
    try:
        return datetime.fromtimestamp(float(value), UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def _text_of(value: object) -> str:
    return value if isinstance(value, str) else ""


def message_text(content: object) -> str:
    """The text of a decoded message content: the string, or the ``text`` parts joined."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in cast(list[object], content):
            if isinstance(part, Mapping):
                item = cast(Mapping[str, Any], part)
                if item.get("type") == "text":
                    parts.append(_text_of(item.get("text")))
        return "\n".join(p for p in parts if p)
    return ""


# -- the routing index --------------------------------------------------------------------------


@dataclass(frozen=True)
class Conversation:
    session_key: str
    session_id: str
    platform: str
    chat_type: str
    origin: dict[str, Any]
    display_name: str
    created_at: float
    updated_at: float

    @property
    def chat_name(self) -> str:
        return str(self.origin.get("chat_name") or "")

    @property
    def user_name(self) -> str:
        return str(self.origin.get("user_name") or "")

    @property
    def chat_id(self) -> str:
        return str(self.origin.get("chat_id") or "")

    @property
    def thread_id(self) -> str | None:
        thread = self.origin.get("thread_id")
        return str(thread) if thread not in (None, "") else None

    def summary(self) -> dict[str, Any]:
        return {
            "session_key": self.session_key,
            "session_id": self.session_id,
            "platform": self.platform,
            "chat_type": self.chat_type,
            "display_name": self.display_name,
            "chat_name": self.chat_name,
            "user_name": self.user_name,
            "updated_at": iso_time(self.updated_at),
        }


def _origin(raw: object) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(value, dict):
        return {}
    return {str(k): v for k, v in cast(dict[object, Any], value).items()}


def _conversation(row: Sequence[Any]) -> Conversation:
    origin = _origin(row[4])
    platform = str(row[2] or origin.get("platform") or "")
    chat_type = str(row[3] or origin.get("chat_type") or "")
    display = str(row[5] or origin.get("chat_name") or origin.get("user_name") or "")
    return Conversation(
        session_key=str(row[0]),
        session_id=str(row[1] or ""),
        platform=platform,
        chat_type=chat_type,
        origin=origin,
        display_name=display,
        created_at=float(row[6] or 0.0),
        updated_at=float(row[7] or 0.0),
    )


def conversations_in(conn: sqlite3.Connection) -> list[Conversation]:
    rows = conn.execute(f"SELECT {_ROUTE_COLUMNS} FROM gateway_routes ORDER BY session_key")
    return [_conversation(tuple(row)) for row in rows.fetchall()]


def conversation_in(conn: sqlite3.Connection, session_key: str) -> Conversation | None:
    row = conn.execute(
        f"SELECT {_ROUTE_COLUMNS} FROM gateway_routes WHERE session_key = ?", (session_key,)
    ).fetchone()
    return None if row is None else _conversation(tuple(row))


# -- the channel directory ----------------------------------------------------------------------


@dataclass(frozen=True)
class Channel:
    platform: str
    chat_id: str
    name: str
    chat_type: str
    guild: str = ""
    thread_id: str | None = None

    @property
    def target(self) -> str:
        base = f"{self.platform}:{self.chat_id}"
        return base if self.thread_id is None else f"{base}:{self.thread_id}"

    def as_json(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "platform": self.platform,
            "name": self.name,
            "chat_type": self.chat_type,
        }


def directory_path(scope: Scope) -> Path:
    """Where the gateway saves its channel directory (P15-20)."""
    return scope.home / "gateway" / "channel_directory.json"


def load_directory(scope: Scope) -> dict[str, list[Channel]]:
    """The saved channel directory by platform; missing or unreadable → empty."""
    try:
        raw: object = json.loads(directory_path(scope).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, Mapping):
        return {}
    listed = cast(Mapping[str, Any], raw).get("platforms")
    if not isinstance(listed, Mapping):
        return {}
    out: dict[str, list[Channel]] = {}
    for name, items in cast(Mapping[str, Any], listed).items():
        if not isinstance(items, list):
            continue
        platform = str(name).strip().lower()
        found: list[Channel] = []
        for item in cast(list[object], items):
            if not isinstance(item, Mapping):
                continue
            entry = cast(Mapping[str, Any], item)
            chat_id = str(entry.get("id") or "").strip()
            if not chat_id:
                continue
            thread = entry.get("thread_id")
            found.append(
                Channel(
                    platform=platform,
                    chat_id=chat_id,
                    name=str(entry.get("name") or chat_id),
                    chat_type=str(entry.get("type") or "channel"),
                    guild=str(entry.get("guild") or ""),
                    thread_id=str(thread) if thread else None,
                )
            )
        if found:
            out[platform] = found
    return out


def channels_from(conversations: Sequence[Conversation]) -> list[Channel]:
    """Send targets the routing index has seen: one per chat (and thread)."""
    seen: set[tuple[str, str, str | None]] = set()
    out: list[Channel] = []
    for conv in conversations:
        chat_id = conv.chat_id
        if not conv.platform or not chat_id:
            continue
        key = (conv.platform, chat_id, conv.thread_id)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            Channel(
                platform=conv.platform,
                chat_id=chat_id,
                name=conv.display_name or conv.chat_name or conv.user_name or chat_id,
                chat_type=conv.chat_type or "dm",
                thread_id=conv.thread_id,
            )
        )
    return out


# -- attachments --------------------------------------------------------------------------------


def _image_part(item: Mapping[str, Any]) -> str:
    if item.get("type") == "image_url":
        inner = item.get("image_url")
        if isinstance(inner, Mapping):
            return _text_of(cast(Mapping[str, Any], inner).get("url"))
        return _text_of(inner)
    url = _text_of(item.get("url"))
    if url:
        return url
    source = item.get("source")
    if isinstance(source, Mapping):
        return _text_of(cast(Mapping[str, Any], source).get("url"))
    return ""


def _image(url: str) -> dict[str, Any]:
    if url.startswith("data:"):
        head = url[5:].split(",", 1)[0]
        return {
            "type": "image",
            "inline": True,
            "media_type": head.split(";", 1)[0] or "application/octet-stream",
            "bytes": len(url),
        }
    return {"type": "image", "url": url}


def attachments_of(
    content: object, policy: MediaPolicy
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """``(attachments, refused)`` of one decoded message content (see the module notes)."""
    found: list[dict[str, Any]] = []
    refused: list[dict[str, str]] = []
    if isinstance(content, list):
        for part in cast(list[object], content):
            if not isinstance(part, Mapping):
                continue
            item = cast(Mapping[str, Any], part)
            kind = str(item.get("type") or "")
            if kind in ("image_url", "image"):
                url = _image_part(item)
                if url:
                    found.append(_image(url))
            elif kind and kind != "text":
                found.append({"type": kind, "data": dict(item)})
    _rest, refs = extract_media(message_text(content))
    for ref in refs:
        if ref.is_url:
            found.append({"type": "image", "url": ref.raw})
            continue
        verdict = policy.check(ref.raw)
        if verdict.path is not None:
            found.append({"type": "media", "path": verdict.path, "kind": ref.kind})
        else:
            refused.append({"path": ref.raw, "reason": verdict.reason})
    return found, refused


# -- the tools ----------------------------------------------------------------------------------


def _not_found(session_key: str) -> dict[str, Any]:
    return {"error": f"conversation not found: {session_key}"}


class ReadTools:
    """The read tools over one ``StoreReader`` (see the module notes)."""

    def __init__(self, scope: Scope, reader: StoreReader) -> None:
        self.scope = scope
        self.reader = reader

    def conversations(self) -> list[Conversation]:
        return self.reader.read(conversations_in)

    def conversation(self, session_key: str) -> Conversation | None:
        return self.reader.read(lambda conn: conversation_in(conn, session_key))

    def conversations_list(
        self, platform: str | None = None, limit: object = DEFAULT_LIMIT, search: str | None = None
    ) -> dict[str, Any]:
        count = coerce_int(limit, default=DEFAULT_LIMIT, low=1, high=MAX_LIMIT)
        wanted = (platform or "").strip().lower()
        needle = (search or "").strip().lower()
        rows: list[Conversation] = []
        for conv in self.conversations():
            if wanted and conv.platform.lower() != wanted:
                continue
            if needle and not any(
                needle in text.lower()
                for text in (conv.display_name, conv.chat_name, conv.session_key)
            ):
                continue
            rows.append(conv)
        rows.sort(key=lambda c: (c.updated_at, c.session_key), reverse=True)
        picked = [conv.summary() for conv in rows[:count]]
        return {"count": len(picked), "conversations": picked}

    def conversation_get(self, session_key: str) -> dict[str, Any]:
        def read(conn: sqlite3.Connection) -> dict[str, Any]:
            conv = conversation_in(conn, session_key)
            if conv is None:
                return _not_found(session_key)
            row = conn.execute(
                "SELECT input_tokens, output_tokens FROM sessions WHERE id = ?",
                (conv.session_id,),
            ).fetchone()
            tokens_in = int(row[0] or 0) if row is not None else 0
            tokens_out = int(row[1] or 0) if row is not None else 0
            return {
                **conv.summary(),
                "chat_id": conv.chat_id,
                "thread_id": conv.thread_id,
                "created_at": iso_time(conv.created_at),
                "input_tokens": tokens_in,
                "output_tokens": tokens_out,
                "total_tokens": tokens_in + tokens_out,
            }

        return self.reader.read(read)

    def _transcript(
        self, session_key: str
    ) -> tuple[Conversation, list[dict[str, Any]]] | dict[str, Any]:
        def read(
            conn: sqlite3.Connection,
        ) -> tuple[Conversation, list[dict[str, Any]]] | dict[str, Any]:
            conv = conversation_in(conn, session_key)
            if conv is None:
                return _not_found(session_key)
            if not conv.session_id:
                return {"error": f"conversation {session_key} has no session yet"}
            return conv, message_rows.get_in(conn, conv.session_id)

        return self.reader.read(read)

    def messages_read(self, session_key: str, limit: object = DEFAULT_LIMIT) -> dict[str, Any]:
        count = coerce_int(limit, default=DEFAULT_LIMIT, low=1, high=MAX_LIMIT)
        found = self._transcript(session_key)
        if isinstance(found, dict):
            return found
        _conv, rows = found
        shown: list[dict[str, Any]] = []
        for row in rows:
            role = str(row.get("role") or "")
            if role not in TEXT_ROLES or is_handoff(row):
                continue
            text = message_text(row.get("content"))
            if not text:
                continue
            shown.append(
                {
                    "id": str(row.get("id")),
                    "role": role,
                    "content": text[:CONTENT_CHARS],
                    "timestamp": iso_time(row.get("timestamp")),
                }
            )
        last = shown[-count:]
        return {
            "session_key": session_key,
            "count": len(last),
            "total_in_session": len(shown),
            "messages": last,
        }

    def attachments_fetch(self, session_key: str, message_id: str) -> dict[str, Any]:
        found = self._transcript(session_key)
        if isinstance(found, dict):
            return found
        _conv, rows = found
        wanted = str(message_id).strip()
        row = next((r for r in rows if str(r.get("id")) == wanted), None)
        if row is None:
            return {"error": f"message not found: {message_id}"}
        attachments, refused = attachments_of(row.get("content"), MediaPolicy.for_scope(self.scope))
        answer: dict[str, Any] = {
            "message_id": wanted,
            "count": len(attachments),
            "attachments": attachments,
        }
        if refused:
            answer["refused"] = refused
        return answer

    def channels(self, platform: str | None = None) -> list[Channel]:
        wanted = (platform or "").strip().lower()
        directory = load_directory(self.scope)
        if directory:
            listed = [c for name, items in sorted(directory.items()) for c in items]
        else:
            listed = channels_from(self.conversations())
        return [c for c in listed if not wanted or c.platform == wanted]

    def channels_list(self, platform: str | None = None) -> dict[str, Any]:
        rows = [c.as_json() for c in self.channels(platform)]
        return {"count": len(rows), "channels": rows}
