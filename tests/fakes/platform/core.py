"""A scriptable in-memory chat platform for adapter tests (P15-01).

Platform-neutral on purpose: the per-platform wire fakes (``telegram_wire``, ``discord_wire``,
``slack_wire``) sit on top and speak the vendor library's shapes. Tests:

- inject inbound traffic with the builders (``dm``, ``group``, ``thread``, ``bot_message``,
  ``edited``, ``voice``, ``photo``, ``document``, ``button_press``); adapters read it with
  ``await receive_inbound()`` (blocks) or ``next_inbound()`` (None when empty);
- read back every outbound operation in ``ops`` (with fake-clock times) and the message store;
- arm failures by class with ``fail(kind, op=..., chat_id=..., times=..., after=...)``; each
  failure raises a `FakePlatformError` subclass whose class name and text carry the reading the
  send classifier recognises (P15-04).

The platform enforces its own length cap with its own length function, so an adapter that does
not split long text gets a "too long" error like on the real service.
"""

from __future__ import annotations

import asyncio
import itertools
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from tests.fakes.clock import FakeClock

OpKind = Literal[
    "connect",
    "disconnect",
    "send",
    "edit",
    "delete",
    "typing",
    "typing_stop",
    "reaction",
    "media",
    "answer",
]
ChatKind = Literal["dm", "group", "channel", "thread", "forum"]


class FailureKind(StrEnum):
    CONNECT_TIMEOUT = "connect_timeout"
    READ_TIMEOUT = "read_timeout"
    RATE_LIMIT = "rate_limit"
    CHAT_NOT_FOUND = "chat_not_found"
    THREAD_NOT_FOUND = "thread_not_found"
    FORBIDDEN = "forbidden"
    PARSE_ERROR = "parse_error"
    TOO_LONG = "too_long"


class FakePlatformError(Exception):
    kind: FailureKind

    def __init__(self, text: str, *, retry_after: float | None = None) -> None:
        super().__init__(text)
        self.text = text
        self.retry_after = retry_after


class FakeConnectTimeout(FakePlatformError):
    kind = FailureKind.CONNECT_TIMEOUT


class FakeReadTimeout(FakePlatformError):
    kind = FailureKind.READ_TIMEOUT


class FakeRateLimited(FakePlatformError):
    kind = FailureKind.RATE_LIMIT


class FakeChatNotFound(FakePlatformError):
    kind = FailureKind.CHAT_NOT_FOUND


class FakeThreadNotFound(FakePlatformError):
    kind = FailureKind.THREAD_NOT_FOUND


class FakeForbidden(FakePlatformError):
    kind = FailureKind.FORBIDDEN


class FakeParseError(FakePlatformError):
    kind = FailureKind.PARSE_ERROR


class FakeTooLong(FakePlatformError):
    kind = FailureKind.TOO_LONG


class FakeMessageNotFound(FakePlatformError):
    """An edit or delete of a message that does not exist (a sub-chat reading)."""

    kind = FailureKind.THREAD_NOT_FOUND


_ERRORS: dict[FailureKind, tuple[type[FakePlatformError], str]] = {
    FailureKind.CONNECT_TIMEOUT: (FakeConnectTimeout, "could not connect to the platform"),
    FailureKind.READ_TIMEOUT: (FakeReadTimeout, "the platform did not answer in time"),
    FailureKind.RATE_LIMIT: (FakeRateLimited, "Too Many Requests: retry after {wait}"),
    FailureKind.CHAT_NOT_FOUND: (FakeChatNotFound, "Bad Request: chat not found"),
    FailureKind.THREAD_NOT_FOUND: (FakeThreadNotFound, "Bad Request: message thread not found"),
    FailureKind.FORBIDDEN: (FakeForbidden, "Forbidden: bot was blocked by the user"),
    FailureKind.PARSE_ERROR: (FakeParseError, "Bad Request: can't parse entities"),
    FailureKind.TOO_LONG: (FakeTooLong, "Bad Request: message is too long"),
}


def make_error(kind: FailureKind, retry_after: float | None = None) -> FakePlatformError:
    cls, text = _ERRORS[kind]
    if kind is FailureKind.RATE_LIMIT:
        wait = 5.0 if retry_after is None else retry_after
        return cls(text.format(wait=int(wait)), retry_after=wait)
    return cls(text)


@dataclass(frozen=True)
class Op:
    kind: OpKind
    chat_id: str = ""
    message_id: str | None = None
    text: str = ""
    thread_id: str | None = None
    reply_to: str | None = None
    at: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict[str, Any], compare=False)


@dataclass(frozen=True)
class Button:
    label: str
    data: str


@dataclass(frozen=True)
class FakeMedia:
    kind: Literal["voice", "audio", "photo", "video", "document", "sticker"]
    ref: str
    mime: str = ""
    caption: str = ""
    duration: float | None = None


@dataclass
class FakeMessage:
    message_id: str
    chat_id: str
    text: str
    thread_id: str | None = None
    reply_to: str | None = None
    buttons: tuple[Button, ...] = ()
    media: tuple[FakeMedia, ...] = ()
    deleted: bool = False
    edits: int = 0
    from_bot: bool = True


@dataclass(frozen=True)
class FakeChat:
    chat_id: str
    kind: ChatKind = "dm"
    name: str = ""
    scope_id: str | None = None
    guild_id: str | None = None


@dataclass(frozen=True)
class FakeInbound:
    """One inbound update, platform-neutral (wire fakes reshape it)."""

    update_id: int
    event: Literal["message", "edited", "button"]
    chat_id: str
    chat_kind: ChatKind
    user_id: str
    user_name: str = ""
    text: str = ""
    message_id: str = ""
    thread_id: str | None = None
    reply_to: str | None = None
    is_bot: bool = False
    mentions_bot: bool = False
    media: tuple[FakeMedia, ...] = ()
    button_data: str | None = None
    button_id: str | None = None
    chat_name: str = ""
    scope_id: str | None = None
    guild_id: str | None = None
    at: datetime = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass
class _Armed:
    kind: FailureKind
    op: str
    chat_id: str | None
    times: int
    after: int
    retry_after: float | None


class FakeChatPlatform:
    def __init__(
        self,
        name: str = "fake",
        *,
        clock: FakeClock | None = None,
        max_len: int = 4096,
        len_fn: Callable[[str], int] = len,
        bot_user_id: str = "bot",
        bot_name: str = "IndusBot",
    ) -> None:
        self.name = name
        self.clock = clock or FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
        self.max_len = max_len
        self.len_fn = len_fn
        self.bot_user_id = bot_user_id
        self.bot_name = bot_name
        self.connected = False
        self.ops: list[Op] = []
        self.chats: dict[str, FakeChat] = {}
        self._messages: dict[str, FakeMessage] = {}
        self._ids = itertools.count(1)
        self._updates = itertools.count(1)
        self._armed: list[_Armed] = []
        self._pending: deque[FakeInbound] = deque()
        self._queue: asyncio.Queue[FakeInbound] | None = None
        self.answered: list[tuple[str, str | None]] = []

    # -- setup ---------------------------------------------------------------------------------

    def add_chat(
        self,
        chat_id: str,
        kind: ChatKind = "dm",
        name: str = "",
        *,
        scope_id: str | None = None,
        guild_id: str | None = None,
    ) -> FakeChat:
        chat = FakeChat(str(chat_id), kind, name or str(chat_id), scope_id, guild_id)
        self.chats[chat.chat_id] = chat
        return chat

    def remove_chat(self, chat_id: str) -> None:
        """Later operations on this chat fail with "chat not found"."""
        self.chats.pop(str(chat_id), None)
        self.fail(FailureKind.CHAT_NOT_FOUND, chat_id=str(chat_id), times=10**9)

    def fail(
        self,
        kind: FailureKind,
        *,
        op: str = "*",
        chat_id: str | None = None,
        times: int = 1,
        after: int = 0,
        retry_after: float | None = None,
    ) -> None:
        """Arm a failure: the next ``times`` matching ops (after ``after`` matching successes)."""
        self._armed.append(_Armed(kind, op, chat_id, times, after, retry_after))

    def fail_connect(self, kind: FailureKind = FailureKind.CONNECT_TIMEOUT, times: int = 1) -> None:
        self.fail(kind, op="connect", times=times)

    def _check(self, op: str, chat_id: str = "") -> None:
        for armed in self._armed:
            if armed.times <= 0:
                continue
            if armed.op not in ("*", op) or (armed.op == "*" and op in ("connect", "disconnect")):
                continue
            if armed.chat_id is not None and armed.chat_id != str(chat_id):
                continue
            if armed.after > 0:
                armed.after -= 1
                continue
            armed.times -= 1
            raise make_error(armed.kind, armed.retry_after)

    def _now(self) -> float:
        return self.clock.monotonic()

    def _log(self, kind: OpKind, chat_id: str = "", **kw: Any) -> Op:
        op = Op(kind, str(chat_id), at=self._now(), **kw)
        self.ops.append(op)
        return op

    def _fits(self, text: str) -> None:
        if self.len_fn(text) > self.max_len:
            raise make_error(FailureKind.TOO_LONG)

    # -- outbound API (what wire fakes and simple adapters call) ---------------------------------

    async def connect(self) -> None:
        self._check("connect")
        if not self.connected:
            self.connected = True
            self._log("connect")

    async def disconnect(self) -> None:
        if self.connected:
            self.connected = False
            self._log("disconnect")

    async def post(
        self,
        chat_id: str,
        text: str,
        *,
        reply_to: str | None = None,
        thread_id: str | None = None,
        buttons: tuple[Button, ...] = (),
        extra: dict[str, Any] | None = None,
    ) -> FakeMessage:
        self._check("send", chat_id)
        self._fits(text)
        message = FakeMessage(
            str(next(self._ids)), str(chat_id), text, thread_id, reply_to, tuple(buttons)
        )
        self._messages[message.message_id] = message
        self._log(
            "send",
            chat_id,
            message_id=message.message_id,
            text=text,
            thread_id=thread_id,
            reply_to=reply_to,
            extra={"buttons": tuple(buttons), **(extra or {})},
        )
        return message

    def _existing(self, chat_id: str, message_id: str, what: str) -> FakeMessage:
        message = self._messages.get(str(message_id))
        if message is None or message.deleted or message.chat_id != str(chat_id):
            raise FakeMessageNotFound(f"Bad Request: message to {what} not found")
        return message

    async def edit(
        self,
        chat_id: str,
        message_id: str,
        text: str,
        *,
        buttons: tuple[Button, ...] | None = None,
    ) -> FakeMessage:
        self._check("edit", chat_id)
        message = self._existing(chat_id, message_id, "edit")
        self._fits(text)
        message.text = text
        message.edits += 1
        if buttons is not None:
            message.buttons = tuple(buttons)
        self._log("edit", chat_id, message_id=message.message_id, text=text)
        return message

    async def delete(self, chat_id: str, message_id: str) -> bool:
        self._check("delete", chat_id)
        message = self._existing(chat_id, message_id, "delete")
        message.deleted = True
        self._log("delete", chat_id, message_id=message.message_id)
        return True

    async def typing(self, chat_id: str, *, thread_id: str | None = None) -> None:
        self._check("typing", chat_id)
        self._log("typing", chat_id, thread_id=thread_id)

    async def typing_stop(self, chat_id: str) -> None:
        self._log("typing_stop", chat_id)

    async def react(self, chat_id: str, message_id: str, emoji: str) -> None:
        self._check("reaction", chat_id)
        self._log("reaction", chat_id, message_id=str(message_id), text=emoji)

    async def upload(
        self,
        chat_id: str,
        media: FakeMedia,
        *,
        reply_to: str | None = None,
        thread_id: str | None = None,
    ) -> FakeMessage:
        self._check("media", chat_id)
        if media.caption:
            self._fits(media.caption)
        message = FakeMessage(
            str(next(self._ids)), str(chat_id), media.caption, thread_id, reply_to, media=(media,)
        )
        self._messages[message.message_id] = message
        self._log(
            "media",
            chat_id,
            message_id=message.message_id,
            text=media.caption,
            thread_id=thread_id,
            reply_to=reply_to,
            extra={"kind": media.kind, "ref": media.ref},
        )
        return message

    async def answer(self, button_id: str, text: str | None = None) -> None:
        self._check("answer")
        self.answered.append((button_id, text))
        self._log("answer", text=text or "", extra={"button_id": button_id})

    async def chat(self, chat_id: str) -> FakeChat:
        self._check("chat_info", chat_id)
        found = self.chats.get(str(chat_id))
        if found is None:
            raise make_error(FailureKind.CHAT_NOT_FOUND)
        return found

    # -- inbound -------------------------------------------------------------------------------

    def _queue_for_loop(self) -> asyncio.Queue[FakeInbound]:
        if self._queue is None:
            self._queue = asyncio.Queue()
            while self._pending:
                self._queue.put_nowait(self._pending.popleft())
        return self._queue

    def _push(self, item: FakeInbound) -> FakeInbound:
        if self._queue is not None:
            self._queue.put_nowait(item)
        else:
            self._pending.append(item)
        return item

    async def receive_inbound(self) -> FakeInbound:
        """The next inbound update; waits until one is injected."""
        return await self._queue_for_loop().get()

    def next_inbound(self) -> FakeInbound | None:
        """The next inbound update, or None when nothing is waiting."""
        if self._queue is not None:
            return None if self._queue.empty() else self._queue.get_nowait()
        return self._pending.popleft() if self._pending else None

    def drain_inbound(self) -> list[FakeInbound]:
        items: list[FakeInbound] = []
        while (item := self.next_inbound()) is not None:
            items.append(item)
        return items

    def _inbound(
        self,
        chat_id: str,
        chat_kind: ChatKind,
        user_id: str,
        text: str,
        *,
        event: Literal["message", "edited", "button"] = "message",
        message_id: str | None = None,
        **kw: Any,
    ) -> FakeInbound:
        chat = self.chats.get(str(chat_id))
        if chat is None:
            chat = self.add_chat(str(chat_id), "group" if chat_kind == "thread" else chat_kind)
        mid = message_id if message_id is not None else str(next(self._ids))
        if event == "message" and mid not in self._messages:
            self._messages[mid] = FakeMessage(
                mid, str(chat_id), text, kw.get("thread_id"), kw.get("reply_to"), from_bot=False
            )
        item = FakeInbound(
            update_id=next(self._updates),
            event=event,
            chat_id=str(chat_id),
            chat_kind=chat_kind,
            user_id=str(user_id),
            text=text,
            message_id=mid,
            chat_name=chat.name,
            scope_id=chat.scope_id,
            guild_id=chat.guild_id,
            at=self.clock.now(),
            **kw,
        )
        return self._push(item)

    def dm(
        self, user_id: str, text: str, *, user_name: str = "", reply_to: str | None = None
    ) -> FakeInbound:
        """A direct message; the DM chat id is the user id."""
        return self._inbound(user_id, "dm", user_id, text, user_name=user_name, reply_to=reply_to)

    def group(
        self,
        chat_id: str,
        user_id: str,
        text: str,
        *,
        user_name: str = "",
        mention: bool = False,
        reply_to: str | None = None,
    ) -> FakeInbound:
        return self._inbound(
            chat_id,
            "group",
            user_id,
            text,
            user_name=user_name,
            mentions_bot=mention,
            reply_to=reply_to,
        )

    def thread(
        self,
        chat_id: str,
        thread_id: str,
        user_id: str,
        text: str,
        *,
        user_name: str = "",
        mention: bool = False,
    ) -> FakeInbound:
        return self._inbound(
            chat_id,
            "thread",
            user_id,
            text,
            thread_id=str(thread_id),
            user_name=user_name,
            mentions_bot=mention,
        )

    def bot_message(
        self, chat_id: str, bot_id: str, text: str, *, chat_kind: ChatKind = "group"
    ) -> FakeInbound:
        return self._inbound(chat_id, chat_kind, bot_id, text, is_bot=True, user_name=bot_id)

    def edited(self, original: FakeInbound, text: str) -> FakeInbound:
        """An edit of an earlier inbound message (same message id)."""
        message = self._messages.get(original.message_id)
        if message is not None:
            message.text = text
        return self._push(
            replace(original, update_id=next(self._updates), event="edited", text=text)
        )

    def _media(
        self, chat_id: str, user_id: str, media: FakeMedia, chat_kind: ChatKind
    ) -> FakeInbound:
        return self._inbound(chat_id, chat_kind, user_id, media.caption, media=(media,))

    def voice(
        self,
        chat_id: str,
        user_id: str,
        ref: str = "voice.ogg",
        *,
        duration: float = 3.0,
        chat_kind: ChatKind = "dm",
    ) -> FakeInbound:
        return self._media(
            chat_id, user_id, FakeMedia("voice", ref, "audio/ogg", duration=duration), chat_kind
        )

    def photo(
        self,
        chat_id: str,
        user_id: str,
        ref: str = "photo.jpg",
        *,
        caption: str = "",
        chat_kind: ChatKind = "dm",
    ) -> FakeInbound:
        return self._media(
            chat_id, user_id, FakeMedia("photo", ref, "image/jpeg", caption), chat_kind
        )

    def document(
        self,
        chat_id: str,
        user_id: str,
        ref: str = "notes.txt",
        *,
        mime: str = "text/plain",
        caption: str = "",
        chat_kind: ChatKind = "dm",
    ) -> FakeInbound:
        return self._media(chat_id, user_id, FakeMedia("document", ref, mime, caption), chat_kind)

    def button_press(
        self,
        chat_id: str,
        user_id: str,
        data: str,
        *,
        message_id: str | None = None,
        chat_kind: ChatKind = "dm",
    ) -> FakeInbound:
        """A press of an inline button we sent (``message_id``: the message carrying it)."""
        return self._inbound(
            chat_id,
            chat_kind,
            user_id,
            "",
            event="button",
            message_id=message_id or "",
            button_data=data,
            button_id=f"cb{next(self._updates)}",
        )

    # -- reading -------------------------------------------------------------------------------

    def ops_of(self, *kinds: OpKind) -> list[Op]:
        return [op for op in self.ops if op.kind in kinds]

    def message(self, message_id: str) -> FakeMessage | None:
        return self._messages.get(str(message_id))

    def messages(self, chat_id: str | None = None) -> list[FakeMessage]:
        """Messages we (the bot) sent and did not delete, in send order."""
        return [
            m
            for m in self._messages.values()
            if m.from_bot and not m.deleted and (chat_id is None or m.chat_id == str(chat_id))
        ]

    def texts(self, chat_id: str | None = None) -> list[str]:
        return [m.text for m in self.messages(chat_id)]


__all__ = [
    "Button",
    "ChatKind",
    "FailureKind",
    "FakeChat",
    "FakeChatNotFound",
    "FakeChatPlatform",
    "FakeConnectTimeout",
    "FakeForbidden",
    "FakeInbound",
    "FakeMedia",
    "FakeMessage",
    "FakeMessageNotFound",
    "FakeParseError",
    "FakePlatformError",
    "FakeRateLimited",
    "FakeReadTimeout",
    "FakeThreadNotFound",
    "FakeTooLong",
    "Op",
    "OpKind",
    "make_error",
]
