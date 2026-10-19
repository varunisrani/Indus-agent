"""Reading discord.py objects without importing discord.py (P15-34).

The adapter receives library objects (messages, channels, members, interactions) and reads them
only through attributes, so the same code runs over the real library and over the test wire fake.
These helpers answer the questions the adapter asks: what kind of chat a channel is, whether a
message mentions us, which kind of media an attachment is, what to call a new thread.
"""

from __future__ import annotations

import mimetypes
import re
from typing import Any, Literal

from indus.gateway.core.events import Attachment, MessageKind

__all__ = [
    "THREAD_NAME_MAX",
    "attachment_kind",
    "attachments_of",
    "channel_kind",
    "display_name",
    "is_conversation",
    "mentions",
    "parent_id",
    "strip_mentions",
    "thread_name",
]

ChannelKind = Literal["dm", "group", "thread"]

THREAD_NAME_MAX = 80
_THREAD_TYPES = frozenset({"public_thread", "private_thread", "news_thread"})
# Message types that carry something a person wrote (pins, joins and thread notices do not).
_CONVERSATION_TYPES = frozenset({"default", "reply"})
_MENTION_TOKEN = re.compile(r"<(?:@[!&]?|#)\d+>")


def _type_name(value: Any) -> str:
    return str(getattr(value, "name", value) or "").lower()


def channel_kind(channel: Any) -> ChannelKind:
    """``dm`` for a direct message, ``thread`` for any thread, else ``group``."""
    name = _type_name(getattr(channel, "type", ""))
    if name == "private":
        return "dm"
    if name in _THREAD_TYPES:
        return "thread"
    return "group"


def parent_id(channel: Any) -> str | None:
    value = getattr(channel, "parent_id", None)
    return str(value) if value is not None else None


def is_conversation(message: Any) -> bool:
    """True for messages a person (or bot) typed; system notices are not."""
    kind = getattr(message, "type", None)
    return kind is None or _type_name(kind) in _CONVERSATION_TYPES


def mentions(message: Any, me: str) -> bool:
    """Does ``message`` mention the user ``me`` (in its mention list or as a raw token)?"""
    if not me:
        return False
    for user in getattr(message, "mentions", None) or ():
        if str(getattr(user, "id", "")) == me:
            return True
    content = str(getattr(message, "content", "") or "")
    return f"<@{me}>" in content or f"<@!{me}>" in content


def strip_mentions(text: str, me: str) -> str:
    """``text`` without our own mention tokens (others stay: they name people)."""
    if not me:
        return text.strip()
    return text.replace(f"<@!{me}>", "").replace(f"<@{me}>", "").strip()


def display_name(user: Any) -> str:
    for attr in ("display_name", "global_name", "name"):
        value = getattr(user, attr, None)
        if value:
            return str(value)
    return str(getattr(user, "id", "") or "someone")


def thread_name(text: str, fallback: str) -> str:
    """A thread title from the message: mention tokens gone, one line, at most 80 characters."""
    flat = " ".join(_MENTION_TOKEN.sub(" ", text).split())
    if not flat:
        return fallback[:THREAD_NAME_MAX]
    if len(flat) <= THREAD_NAME_MAX:
        return flat
    return flat[: THREAD_NAME_MAX - 1].rstrip() + "…"


def attachment_kind(attachment: Any) -> MessageKind:
    voice = getattr(attachment, "is_voice_message", None)
    if callable(voice) and voice() is True:
        return MessageKind.VOICE
    mime = str(getattr(attachment, "content_type", "") or "").lower()
    if not mime:
        mime = mimetypes.guess_type(str(getattr(attachment, "filename", "") or ""))[0] or ""
    if mime.startswith("image/"):
        return MessageKind.PHOTO
    if mime.startswith("video/"):
        return MessageKind.VIDEO
    if mime.startswith("audio/"):
        return MessageKind.AUDIO
    return MessageKind.DOCUMENT


def attachments_of(message: Any) -> tuple[tuple[Attachment, ...], MessageKind | None, float | None]:
    """The message's attachments (CDN URLs), the kind of the first one, a voice note's length."""
    items: list[Attachment] = []
    first: MessageKind | None = None
    duration: float | None = None
    for att in getattr(message, "attachments", None) or ():
        url = str(getattr(att, "url", "") or "")
        if not url:
            continue
        kind = attachment_kind(att)
        if first is None:
            first = kind
            seconds = getattr(att, "duration", None)
            if kind is MessageKind.VOICE and isinstance(seconds, int | float):
                duration = float(seconds)
        items.append(Attachment(ref=url, mime=str(getattr(att, "content_type", "") or "")))
    return tuple(items), first, duration
