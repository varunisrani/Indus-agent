"""Google Chat interaction events → `InboundEvent` (P16-13).

Two envelope shapes reach a Chat app, over HTTP or Pub/Sub alike:

- the classic Chat event: ``{"type": "MESSAGE" | "CARD_CLICKED" | "ADDED_TO_SPACE" |
  "REMOVED_FROM_SPACE", "space", "message", "user", "action", "common"}``;
- the Workspace add-on event: ``{"chat": {"user", "messagePayload" | "buttonClickedPayload" |
  "addedToSpacePayload" | "removedFromSpacePayload"}, "commonEventObject": {"parameters"}}``.

`read_event` sorts an envelope into a `ChatEvent`; `to_inbound` builds the `InboundEvent`:

- space type ``DIRECT_MESSAGE`` (legacy ``DM``, or a single-user bot DM) → ``dm``, else ``group``;
- ``threadReply: true`` → ``thread`` with the thread's resource name as ``thread_id``;
- ``users/<id>`` is the ``user_id``, the lower-cased email the ``user_id_alt``; ``BOT`` senders are
  ``is_bot``;
- the text is ``argumentText`` (the message without the app's @mention) when Chat sends it; a
  slash command keeps its ``/name``; a mention of an app (``USER_MENTION`` of a ``BOT``) marks
  ``mentioned``;
- attachments: ``attachmentDataRef.resourceName`` → ``gchat-media:<name>`` (downloaded through the
  Chat media endpoint), else a ``downloadUri`` (fetched only from Google hosts); Drive-only files
  are skipped;
- message ids are the last segment of ``spaces/<s>/messages/<id>`` (unique inside the space).

A card button we sent carries ``function`` ``indus.button`` and one parameter ``data`` (our callback
data); its click becomes an event with ``metadata["button"]``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, cast

from indus.gateway.core.events import Attachment, ChatType, InboundEvent, MessageKind, Source

__all__ = [
    "BUTTON_FUNCTION",
    "BUTTON_PARAM",
    "MEDIA_SCHEME",
    "ChatEvent",
    "message_kind",
    "read_event",
    "short_id",
    "to_inbound",
]

MEDIA_SCHEME = "gchat-media:"
BUTTON_FUNCTION = "indus.button"
BUTTON_PARAM = "data"

type EventKind = Literal["message", "button", "added", "removed", "ignored"]


def _map(value: object) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _list(value: object) -> list[Any]:
    return cast(list[Any], value) if isinstance(value, list) else []


@dataclass(frozen=True)
class ChatEvent:
    kind: EventKind
    message: Mapping[str, Any] = field(default_factory=dict[str, Any])
    space: Mapping[str, Any] = field(default_factory=dict[str, Any])
    user: Mapping[str, Any] = field(default_factory=dict[str, Any])
    button: str | None = None

    @property
    def name(self) -> str:
        """The message's resource name (dedupe key; empty when absent)."""
        return str(self.message.get("name") or "")


def _parameter(pairs: object, key: str) -> str | None:
    for raw in _list(pairs):
        item = _map(raw)
        if item.get("key") == key and isinstance(item.get("value"), str):
            return str(item["value"])
    return None


def _addon(chat: Mapping[str, Any], envelope: Mapping[str, Any]) -> ChatEvent:
    user = _map(chat.get("user"))
    for key, kind in (("messagePayload", "message"), ("buttonClickedPayload", "button")):
        payload = _map(chat.get(key))
        if payload:
            message = _map(payload.get("message"))
            space = _map(payload.get("space")) or _map(message.get("space"))
            button = None
            if kind == "button":
                params = _map(_map(envelope.get("commonEventObject")).get("parameters"))
                found = params.get(BUTTON_PARAM)
                button = found if isinstance(found, str) else None
            return ChatEvent(cast(EventKind, kind), message, space, user, button)
    if chat.get("addedToSpacePayload"):
        return ChatEvent("added", space=_map(_map(chat["addedToSpacePayload"]).get("space")))
    if chat.get("removedFromSpacePayload"):
        return ChatEvent("removed", space=_map(_map(chat["removedFromSpacePayload"]).get("space")))
    return ChatEvent("ignored")


def read_event(envelope: Mapping[str, Any]) -> ChatEvent:
    """Sort one event envelope (see the module notes)."""
    chat = envelope.get("chat")
    if isinstance(chat, dict):
        return _addon(cast(dict[str, Any], chat), envelope)
    kind = str(envelope.get("type") or "")
    message = _map(envelope.get("message"))
    space = _map(envelope.get("space")) or _map(message.get("space"))
    user = _map(envelope.get("user"))
    if kind == "MESSAGE" and message:
        return ChatEvent("message", message, space, user)
    if kind == "CARD_CLICKED":
        action = _map(envelope.get("action"))
        common = _map(envelope.get("common"))
        function = str(action.get("actionMethodName") or common.get("invokedFunction") or "")
        data = _parameter(action.get("parameters"), BUTTON_PARAM)
        if data is None:
            found = _map(common.get("parameters")).get(BUTTON_PARAM)
            data = found if isinstance(found, str) else None
        if function == BUTTON_FUNCTION and data:
            return ChatEvent("button", message, space, user, data)
        return ChatEvent("ignored")
    if kind == "ADDED_TO_SPACE":
        return ChatEvent("added", space=space, user=user)
    if kind == "REMOVED_FROM_SPACE":
        return ChatEvent("removed", space=space, user=user)
    return ChatEvent("ignored")


def short_id(name: str) -> str:
    """``spaces/S/messages/M`` → ``M`` (other strings unchanged)."""
    head, sep, tail = name.rpartition("/messages/")
    return tail if sep and head else name


def message_kind(mime: str) -> MessageKind:
    if mime.startswith("image/"):
        return MessageKind.PHOTO
    if mime.startswith("audio/"):
        return MessageKind.VOICE
    if mime.startswith("video/"):
        return MessageKind.VIDEO
    return MessageKind.DOCUMENT


def _chat_type(space: Mapping[str, Any]) -> ChatType:
    kind = str(space.get("spaceType") or space.get("type") or "").upper()
    if kind in ("DIRECT_MESSAGE", "DM") or space.get("singleUserBotDm") is True:
        return "dm"
    return "group"


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    head, dot, rest = text.partition(".")
    if dot:
        digits = "".join(c for c in rest if c.isdigit())
        zone = rest[len(digits) :]
        text = f"{head}.{digits[:6]}{zone}"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _attachments(message: Mapping[str, Any]) -> tuple[Attachment, ...]:
    found: list[Attachment] = []
    for raw in _list(message.get("attachment")):
        item = _map(raw)
        mime = str(item.get("contentType") or "")
        resource = str(_map(item.get("attachmentDataRef")).get("resourceName") or "")
        uri = str(item.get("downloadUri") or "")
        if resource:
            found.append(Attachment(ref=MEDIA_SCHEME + resource, mime=mime))
        elif uri:
            found.append(Attachment(ref=uri, mime=mime))
        # A Drive-only file (driveDataRef) needs the user's own Drive access: skipped.
    return tuple(found)


def _mentions_app(message: Mapping[str, Any]) -> bool:
    for raw in _list(message.get("annotations")):
        item = _map(raw)
        if item.get("type") != "USER_MENTION":
            continue
        user = _map(_map(item.get("userMention")).get("user"))
        if user.get("type") == "BOT":
            return True
    return False


def _slash_name(message: Mapping[str, Any]) -> str:
    for raw in _list(message.get("annotations")):
        item = _map(raw)
        if item.get("type") == "SLASH_COMMAND":
            name = str(_map(item.get("slashCommand")).get("commandName") or "")
            if name:
                return name if name.startswith("/") else "/" + name
    return ""


def _source(platform: str, ev: ChatEvent, message: Mapping[str, Any]) -> Source | None:
    space = ev.space
    space_name = str(space.get("name") or "")
    if not space_name:
        return None
    sender = _map(message.get("sender")) or dict(ev.user)
    user_id = str(sender.get("name") or "")
    if not user_id:
        return None
    email = str(sender.get("email") or "").strip().lower()
    chat_type = _chat_type(space)
    thread = str(_map(message.get("thread")).get("name") or "")
    thread_id = thread if thread and message.get("threadReply") is True else None
    return Source(
        platform=platform,
        chat_id=space_name,
        chat_type="thread" if thread_id else chat_type,
        chat_name=str(space.get("displayName") or "") or None,
        user_id=user_id,
        user_name=str(sender.get("displayName") or "") or email or None,
        thread_id=thread_id,
        user_id_alt=email or None,
        is_bot=str(sender.get("type") or "").upper() == "BOT",
        parent_chat_id=space_name if thread_id else None,
    )


def to_inbound(platform: str, ev: ChatEvent) -> InboundEvent | None:
    """The `InboundEvent` for a message or button event; None for anything else."""
    if ev.kind == "button":
        source = _source(platform, ev, {**dict(ev.message), "sender": dict(ev.user)})
        if source is None or not ev.button:
            return None
        name = ev.name
        return InboundEvent(
            source=source,
            text="",
            message_id=short_id(name) if name else None,
            metadata={"button": ev.button},
        )
    if ev.kind != "message":
        return None
    message = ev.message
    source = _source(platform, ev, message)
    if source is None:
        return None
    argument = message.get("argumentText")
    text = str(argument if isinstance(argument, str) else message.get("text") or "").strip()
    slash = _slash_name(message)
    if slash and not text.startswith("/"):
        text = f"{slash} {text}".strip()
    media = _attachments(message)
    kind = message_kind(media[0].mime) if media else MessageKind.TEXT
    metadata: dict[str, Any] = {}
    if _mentions_app(message):
        metadata["mentioned"] = True
    name = ev.name
    return InboundEvent(
        source=source,
        text=text,
        kind=kind,
        message_id=short_id(name) if name else None,
        media=media,
        metadata=metadata,
        timestamp=_timestamp(message.get("createTime")),
    )
