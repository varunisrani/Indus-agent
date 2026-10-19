"""The inbound event every adapter produces, and command detection on it (P15-03).

Adapters turn a platform message into one `InboundEvent` whose `Source` says where it came from.
Everything downstream (authorization, session keys, busy guards, slash dispatch) reads only these
two models, never a platform payload.

A message is a command when, after any leading attachment-reference lines (``@image:``,
``@file:``, ``@url:``), it starts with ``/`` and the event allows control. The name is the first
word without the slash, lower-cased, without an ``@botname`` suffix; a name containing ``/`` (a
path such as ``/etc/hosts``) or an empty name means the message is plain text.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from indus.gateway.core.sent_index import SentIndex

ChatType = Literal["dm", "group", "channel", "thread", "forum"]

# A quoted reply is cut to this many UTF-8 bytes before it is put in front of the user's text.
REPLY_QUOTE_MAX_BYTES = 4096


class MessageKind(StrEnum):
    # Plain text and commands first, then the attachment kinds alphabetically.
    TEXT = "text"
    COMMAND = "command"
    AUDIO = "audio"
    DOCUMENT = "document"
    LOCATION = "location"
    PHOTO = "photo"
    STICKER = "sticker"
    VIDEO = "video"
    VOICE = "voice"


class Source(BaseModel):
    """Where an inbound message came from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    platform: str
    chat_id: str
    chat_type: ChatType = "dm"
    chat_name: str | None = None
    user_id: str | None = None
    user_name: str | None = None
    thread_id: str | None = None
    # A second id for the same sender where the platform has two (a phone number and a UUID).
    user_id_alt: str | None = None
    is_bot: bool = False
    # Workspace or tenant the chat id is unique within (Slack team id).
    scope_id: str | None = None
    guild_id: str | None = None
    parent_chat_id: str | None = None
    # The thread a reply will open (Discord auto-threads), so the first message and the
    # follow-ups inside the new thread share one session key (P15-07).
    prospective_thread_id: str | None = None
    # The adapter verified a role grant for this sender (Discord role allowlist); the
    # authorization chain (P15-08) accepts it as a grant of its own.
    role_authorized: bool = False


class Attachment(BaseModel):
    """One media item of an event: a local path (downloaded) or a URL."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ref: str
    mime: str = ""
    # True when the file's text is already part of the event text; None when unknown.
    inlined: bool | None = None


class ParsedCommand(NamedTuple):
    name: str
    args: str


class InboundEvent(BaseModel):
    """One normalised inbound message. Frozen: build changed copies with ``model_copy``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: Source
    text: str = ""
    kind: MessageKind = MessageKind.TEXT
    message_id: str | None = None
    # The platform's update counter (Telegram ``update_id``), for dedupe and restart offsets.
    update_id: int | None = None
    media: tuple[Attachment, ...] = ()
    reply_to_id: str | None = None
    reply_to_text: str | None = None
    reply_to_author_id: str | None = None
    reply_to_author_name: str | None = None
    reply_to_is_own: bool = False
    # Made by the gateway itself (a background completion), not by a person.
    internal: bool = False
    # False for untrusted proactive payloads: their text never runs a command.
    allow_control: bool = True
    # Was the message addressed to us? None = the adapter cannot tell.
    reply_expected: bool | None = None
    metadata: dict[str, Any] = Field(default_factory=dict[str, Any])
    # The platform's own send time when it gives one.
    timestamp: datetime | None = None

    @property
    def user_id(self) -> str | None:
        return self.source.user_id

    @property
    def user_name(self) -> str | None:
        return self.source.user_name

    def parsed_command(self) -> ParsedCommand | None:
        if not self.allow_control:
            return None
        return parse_command(self.text)

    @property
    def is_command(self) -> bool:
        return self.parsed_command() is not None

    @property
    def command(self) -> str | None:
        parsed = self.parsed_command()
        return parsed.name if parsed is not None else None

    @property
    def command_args(self) -> str:
        parsed = self.parsed_command()
        return parsed.args if parsed is not None else ""


# -- command detection ------------------------------------------------------------------------

_REF_LINE = re.compile(r"@(?:image|file|url):\S.*", re.IGNORECASE)
_EM_DASHES = re.compile("—+")
_EN_DASH = "–"


def strip_attachment_refs(text: str) -> str:
    """``text`` without its leading attachment-reference lines (and surrounding whitespace)."""
    rest = text.lstrip()
    while rest:
        line, newline, after = rest.partition("\n")
        if not _REF_LINE.fullmatch(line.rstrip("\r")):
            break
        rest = after.lstrip() if newline else ""
    return rest


def normalise_dashes(text: str) -> str:
    """Undo phone-keyboard dash replacement: em dash runs → ``--``, en dash → ``-``."""
    return _EM_DASHES.sub("--", text).replace(_EN_DASH, "-")


def parse_command(text: str) -> ParsedCommand | None:
    """The command in ``text``, or None when it is plain text (``allow_control`` not checked)."""
    body = strip_attachment_refs(text)
    if not body.startswith("/"):
        return None
    parts = body[1:].split(None, 1)
    if not parts or body[1:2].isspace():
        return None
    name = parts[0].lower().split("@", 1)[0]
    if not name or "/" in name:
        return None
    args = parts[1].strip() if len(parts) > 1 else ""
    return ParsedCommand(name, normalise_dashes(args))


# -- reply context ----------------------------------------------------------------------------


def _cut_utf8(text: str, limit: int) -> str:
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[:limit].decode("utf-8", errors="ignore")


def with_reply_context(event: InboundEvent, index: SentIndex) -> InboundEvent:
    """Fill the replied-to text from ``index`` when the platform gave none; mark own messages."""
    if event.reply_to_id is None:
        return event
    platform, chat_id = event.source.platform, event.source.chat_id
    own = event.reply_to_is_own or index.is_own(platform, chat_id, event.reply_to_id)
    text = event.reply_to_text
    if not text:
        text = index.lookup(platform, chat_id, event.reply_to_id)
    if own == event.reply_to_is_own and text == event.reply_to_text:
        return event
    return event.model_copy(update={"reply_to_is_own": own, "reply_to_text": text})


def model_text(event: InboundEvent) -> str:
    """The text the agent sees: the replied-to quote (when known) in front of the message."""
    quote = (event.reply_to_text or "").strip()
    if not quote:
        return event.text
    if event.reply_to_is_own:
        who = "your earlier message"
    else:
        who = event.reply_to_author_name or "a message"
    quote = _cut_utf8(quote, REPLY_QUOTE_MAX_BYTES)
    return f'[Replying to {who}: "{quote}"]\n\n{event.text}'


__all__ = [
    "REPLY_QUOTE_MAX_BYTES",
    "Attachment",
    "ChatType",
    "InboundEvent",
    "MessageKind",
    "ParsedCommand",
    "Source",
    "model_text",
    "normalise_dashes",
    "parse_command",
    "strip_attachment_refs",
    "with_reply_context",
]
