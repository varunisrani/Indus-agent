"""The adapter surface outbound code uses, and the only place it calls an adapter.

Adapters (``gateway.adapters.base``, P15-05), ``SendResult`` (``gateway.core.send``, P15-04) and
the sent-message index (``gateway.core.sent_index``, P15-03) are the real types; outbound code
reaches them only through this module. Every send and edit leaves through ``send_text`` /
``edit_text``: they mask secrets (when the caller passes a redactor) and mark every non-final send
as interim, at both egress doors (R06.12). The adapter base takes the ``interim`` / ``final``
marks off before its hooks run, so they never reach the wire; an edit marked final is also passed
as a final edit (never skipped by the chat's send slot).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from indus.gateway.adapters.base import FINAL_KEY, INTERIM_KEY, Adapter, MediaKind
from indus.gateway.core.send import ErrorKind, SendResult
from indus.gateway.outbound.chunker import LenFn
from indus.gateway.outbound.formatting import Formatter, formatter_for

__all__ = [
    "FINAL_KEY",
    "INTERIM_KEY",
    "can_delete",
    "can_edit",
    "chat_unreachable",
    "delete_message",
    "edit_text",
    "formatter",
    "is_bad_format",
    "is_flood",
    "length_fn",
    "max_length",
    "platform_of",
    "send_media_file",
    "send_text",
    "typing",
]

type Redact = Callable[[str], str]


# -- adapter facts ---------------------------------------------------------------------------------


def platform_of(adapter: Adapter) -> str:
    return adapter.platform or "unknown"


def length_fn(adapter: Adapter) -> LenFn:
    return adapter.capabilities.len_fn


def max_length(adapter: Adapter) -> int:
    value = adapter.capabilities.max_len
    return value if value > 0 else 4000


def can_edit(adapter: Adapter) -> bool:
    return adapter.capabilities.supports_edit


def can_delete(adapter: Adapter) -> bool:
    return adapter.capabilities.supports_delete


def formatter(adapter: Adapter) -> Formatter:
    """The adapter's markup bundle (``Capabilities.markup``; plain when it declares none)."""
    return formatter_for(adapter.capabilities.markup)


# -- result readings -------------------------------------------------------------------------------


def is_flood(result: SendResult) -> bool:
    return not result.ok and result.kind is ErrorKind.RATE_LIMITED


def is_bad_format(result: SendResult) -> bool:
    return not result.ok and result.kind is ErrorKind.BAD_FORMAT


def chat_unreachable(result: SendResult) -> bool:
    """True when the failure proves the whole chat gone (forbidden, chat-level not found).

    Thread, topic and message not-found readings keep the chat alive; when both readings appear
    the sub-chat one wins (P15-04's classifier, ``SendResult.chat_level``)."""
    return result.chat_unreachable


# -- egress doors ----------------------------------------------------------------------------------


def _meta(meta: Mapping[str, Any] | None, final: bool) -> dict[str, Any]:
    marked = dict(meta or {})
    marked.pop(INTERIM_KEY, None)
    marked.pop(FINAL_KEY, None)
    marked[FINAL_KEY if final else INTERIM_KEY] = True
    return marked


async def send_text(
    adapter: Adapter,
    chat_id: str,
    text: str,
    *,
    final: bool,
    reply_to: str | None = None,
    meta: Mapping[str, Any] | None = None,
    redact: Redact | None = None,
) -> SendResult:
    body = redact(text) if redact is not None else text
    return await adapter.send(chat_id, body, reply_to=reply_to, meta=_meta(meta, final))


async def edit_text(
    adapter: Adapter,
    chat_id: str,
    message_id: str,
    text: str,
    *,
    final: bool,
    meta: Mapping[str, Any] | None = None,
    redact: Redact | None = None,
) -> SendResult:
    body = redact(text) if redact is not None else text
    return await adapter.edit(chat_id, message_id, body, final=final, meta=_meta(meta, final))


async def delete_message(adapter: Adapter, chat_id: str, message_id: str) -> bool:
    """Delete when the adapter can; False otherwise or on failure (never raises)."""
    if not can_delete(adapter):
        return False
    try:
        return await adapter.delete(chat_id, message_id)
    except Exception:
        return False


async def typing(adapter: Adapter, chat_id: str, *, on: bool) -> None:
    """Start or stop the typing indicator; the adapter logs and swallows its failures."""
    try:
        if on:
            await adapter.typing_start(chat_id)
        else:
            await adapter.typing_stop(chat_id)
    except Exception:
        return


# Outbound media kinds → the adapter's native kind (audio goes out as a plain file).
_MEDIA_KINDS: dict[str, MediaKind] = {
    "image": "image",
    "video": "video",
    "voice": "voice",
    "audio": "file",
    "document": "document",
}


async def send_media_file(
    adapter: Adapter,
    chat_id: str,
    kind: str,
    target: str,
    *,
    reply_to: str | None = None,
    meta: Mapping[str, Any] | None = None,
) -> SendResult:
    """Send one validated file (or URL) natively; a platform without that kind refuses it
    (``SendResult.unsupported``) and an unknown kind goes out as a document."""
    marked = _meta(meta, True)
    match _MEDIA_KINDS.get(kind, "document"):
        case "image":
            send = adapter.send_image
        case "video":
            send = adapter.send_video
        case "voice":
            send = adapter.send_voice
        case "file":
            send = adapter.send_file
        case _:
            send = adapter.send_document
    result = await send(chat_id, target, reply_to=reply_to, meta=marked)
    if result.unsupported and send != adapter.send_document:
        result = await adapter.send_document(chat_id, target, reply_to=reply_to, meta=marked)
    return result
