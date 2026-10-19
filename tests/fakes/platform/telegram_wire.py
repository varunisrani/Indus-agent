"""A fake of the Telegram library surface our adapter touches (P15-01).

Shapes follow python-telegram-bot (method names snake_case, keyword arguments as the library
takes them, results with ``message_id`` / ``chat.id``), backed by one `FakeChatPlatform`.
Errors are raised as stand-ins named like the library's (``RetryAfter``, ``BadRequest``,
``Forbidden``, ``TimedOut``, ``NetworkError``) so the adapter's error mapping can be exercised.
Only names listed in ``SURFACE`` exist; the drift check proves each is real in the pinned
library (P15-33 extends both together).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from indus.gateway.adapters.base import utf16_units
from tests.fakes.platform.core import (
    Button,
    FailureKind,
    FakeChatPlatform,
    FakeInbound,
    FakeMedia,
    FakeMessage,
    FakePlatformError,
)

MAX_LEN = 4096

SURFACE: dict[str, tuple[str, ...]] = {
    "telegram:Bot": (
        "get_me",
        "get_updates",
        "set_webhook",
        "delete_webhook",
        "send_message",
        "edit_message_text",
        "delete_message",
        "send_chat_action",
        "set_message_reaction",
        "send_photo",
        "send_document",
        "send_voice",
        "send_audio",
        "send_video",
        "set_my_commands",
        "get_file",
        "answer_callback_query",
    ),
    "telegram:InlineKeyboardButton": (),
    "telegram:InlineKeyboardMarkup": (),
    "telegram.error:RetryAfter": ("retry_after",),
    "telegram.error:BadRequest": (),
    "telegram.error:Forbidden": (),
    "telegram.error:TimedOut": (),
    "telegram.error:NetworkError": (),
}


# -- library-shaped errors ----------------------------------------------------------------------


class TelegramError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NetworkError(TelegramError):
    pass


class TimedOut(NetworkError):
    def __init__(self, message: str = "Timed out") -> None:
        super().__init__(message)


class BadRequest(NetworkError):
    pass


class Forbidden(TelegramError):
    pass


class RetryAfter(TelegramError):
    def __init__(self, retry_after: float) -> None:
        super().__init__(f"Flood control exceeded. Retry in {int(retry_after)} seconds")
        self._retry_after = retry_after

    @property
    def retry_after(self) -> float:
        return self._retry_after


def _translate(exc: FakePlatformError) -> TelegramError:
    kind = exc.kind
    if kind is FailureKind.RATE_LIMIT:
        return RetryAfter(exc.retry_after or 5.0)
    if kind is FailureKind.READ_TIMEOUT:
        return TimedOut()
    if kind is FailureKind.CONNECT_TIMEOUT:
        return NetworkError("httpx.ConnectError: connection refused")
    if kind is FailureKind.FORBIDDEN:
        return Forbidden(exc.text)
    return BadRequest(exc.text.removeprefix("Bad Request: "))


# -- library-shaped objects ---------------------------------------------------------------------


@dataclass(frozen=True)
class InlineKeyboardButton:
    text: str
    callback_data: str


@dataclass(frozen=True)
class InlineKeyboardMarkup:
    inline_keyboard: tuple[tuple[InlineKeyboardButton, ...], ...]


@dataclass(frozen=True)
class User:
    id: int
    is_bot: bool
    first_name: str
    username: str | None = None


@dataclass(frozen=True)
class Chat:
    id: int
    type: str  # private, group, supergroup, channel
    title: str | None = None
    is_forum: bool = False


@dataclass(frozen=True)
class File:
    file_id: str
    file_path: str


@dataclass(frozen=True)
class Message:
    message_id: int
    chat: Chat
    text: str | None = None
    caption: str | None = None
    from_user: User | None = None
    message_thread_id: int | None = None
    reply_to_message: Message | None = None
    voice: File | None = None
    photo: tuple[File, ...] = ()
    document: File | None = None
    date: Any = None


@dataclass(frozen=True)
class CallbackQuery:
    id: str
    from_user: User
    data: str
    message: Message | None = None


@dataclass(frozen=True)
class Update:
    update_id: int
    message: Message | None = None
    edited_message: Message | None = None
    callback_query: CallbackQuery | None = None


_CHAT_TYPES = {
    "dm": "private",
    "group": "supergroup",
    "thread": "supergroup",
    "forum": "supergroup",
}


def _int(value: str | None) -> int | None:
    return int(value) if value not in (None, "") else None  # type: ignore[arg-type]


def _chat_of(item: FakeInbound) -> Chat:
    kind = _CHAT_TYPES.get(item.chat_kind, "channel")
    title = None if item.chat_kind == "dm" else item.chat_name
    return Chat(int(item.chat_id), kind, title, is_forum=item.chat_kind in ("thread", "forum"))


def _file(media: FakeMedia) -> File:
    return File(file_id=f"f-{media.ref}", file_path=media.ref)


@dataclass
class FakeBot:
    """A stand-in for ``telegram.Bot`` over a `FakeChatPlatform`."""

    platform: FakeChatPlatform
    calls: list[tuple[str, dict[str, Any]]] = field(
        default_factory=list[tuple[str, dict[str, Any]]]
    )
    _offset: int = 0

    async def _do(self, name: str, kwargs: dict[str, Any], coro: Any) -> Any:
        self.calls.append((name, kwargs))
        try:
            return await coro
        except FakePlatformError as exc:
            raise _translate(exc) from None

    def _message(self, sent: FakeMessage) -> Message:
        chat = Chat(int(sent.chat_id), "private")
        return Message(int(sent.message_id), chat, text=sent.text)

    async def get_me(self) -> User:
        await self._do("get_me", {}, self.platform.connect())
        return User(int(self.platform.bot_user_id or 1), True, self.platform.bot_name)

    async def get_updates(
        self, offset: int | None = None, timeout: float = 0, **kwargs: Any
    ) -> list[Update]:
        self.calls.append(("get_updates", {"offset": offset, "timeout": timeout, **kwargs}))
        updates = [self._update(item) for item in self.platform.drain_inbound()]
        return [u for u in updates if offset is None or u.update_id >= offset]

    def _update(self, item: FakeInbound) -> Update:
        chat = _chat_of(item)
        user = User(int(item.user_id), item.is_bot, item.user_name or item.user_id)
        reply = None
        if item.reply_to:
            original = self.platform.message(item.reply_to)
            # Telegram does not echo rich content: the quoted text is dropped for our own messages.
            text = None if original is None or original.from_bot else original.text
            reply = Message(int(item.reply_to), chat, text=text)
        media = item.media[0] if item.media else None
        message = Message(
            message_id=int(item.message_id) if item.message_id else 0,
            chat=chat,
            text=item.text if media is None else None,
            caption=item.text if media is not None else None,
            from_user=user,
            message_thread_id=_int(item.thread_id),
            reply_to_message=reply,
            voice=_file(media) if media and media.kind == "voice" else None,
            photo=(_file(media),) if media and media.kind == "photo" else (),
            document=_file(media) if media and media.kind == "document" else None,
            date=item.at,
        )
        if item.event == "button":
            query = CallbackQuery(str(item.button_id), user, str(item.button_data), message)
            return Update(item.update_id, callback_query=query)
        if item.event == "edited":
            return Update(item.update_id, edited_message=message)
        return Update(item.update_id, message=message)

    async def set_webhook(self, url: str, secret_token: str | None = None, **kw: Any) -> bool:
        self.calls.append(("set_webhook", {"url": url, "secret_token": secret_token, **kw}))
        return True

    async def delete_webhook(self, **kw: Any) -> bool:
        self.calls.append(("delete_webhook", kw))
        return True

    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        parse_mode: str | None = None,
        reply_to_message_id: int | None = None,
        message_thread_id: int | None = None,
        reply_markup: InlineKeyboardMarkup | None = None,
        **kw: Any,
    ) -> Message:
        buttons = tuple(
            Button(b.text, b.callback_data)
            for row in (reply_markup.inline_keyboard if reply_markup else ())
            for b in row
        )
        args = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode, **kw}
        sent = await self._do(
            "send_message",
            args,
            self.platform.post(
                str(chat_id),
                text,
                reply_to=str(reply_to_message_id) if reply_to_message_id else None,
                thread_id=str(message_thread_id) if message_thread_id else None,
                buttons=buttons,
                extra={"parse_mode": parse_mode},
            ),
        )
        return self._message(sent)

    async def edit_message_text(
        self,
        text: str,
        chat_id: int | str,
        message_id: int,
        parse_mode: str | None = None,
        **kw: Any,
    ) -> Message:
        args = {"chat_id": chat_id, "message_id": message_id, "text": text, **kw}
        edited = await self._do(
            "edit_message_text", args, self.platform.edit(str(chat_id), str(message_id), text)
        )
        return self._message(edited)

    async def delete_message(self, chat_id: int | str, message_id: int) -> bool:
        args = {"chat_id": chat_id, "message_id": message_id}
        return await self._do(
            "delete_message", args, self.platform.delete(str(chat_id), str(message_id))
        )

    async def send_chat_action(
        self, chat_id: int | str, action: str, message_thread_id: int | None = None
    ) -> bool:
        thread = str(message_thread_id) if message_thread_id else None
        await self._do(
            "send_chat_action",
            {"chat_id": chat_id, "action": action},
            self.platform.typing(str(chat_id), thread_id=thread),
        )
        return True

    async def set_message_reaction(
        self, chat_id: int | str, message_id: int, reaction: str | None = None
    ) -> bool:
        await self._do(
            "set_message_reaction",
            {"chat_id": chat_id, "message_id": message_id, "reaction": reaction},
            self.platform.react(str(chat_id), str(message_id), reaction or ""),
        )
        return True

    async def _upload(
        self, name: str, kind: Any, chat_id: int | str, ref: Any, **kw: Any
    ) -> Message:
        caption = kw.get("caption") or ""
        media = FakeMedia(kind, str(ref), caption=caption)
        thread = kw.get("message_thread_id")
        sent = await self._do(
            name,
            {"chat_id": chat_id, **kw},
            self.platform.upload(str(chat_id), media, thread_id=str(thread) if thread else None),
        )
        return self._message(sent)

    async def send_photo(self, chat_id: int | str, photo: Any, **kw: Any) -> Message:
        return await self._upload("send_photo", "photo", chat_id, photo, **kw)

    async def send_document(self, chat_id: int | str, document: Any, **kw: Any) -> Message:
        return await self._upload("send_document", "document", chat_id, document, **kw)

    async def send_voice(self, chat_id: int | str, voice: Any, **kw: Any) -> Message:
        return await self._upload("send_voice", "voice", chat_id, voice, **kw)

    async def send_audio(self, chat_id: int | str, audio: Any, **kw: Any) -> Message:
        return await self._upload("send_audio", "audio", chat_id, audio, **kw)

    async def send_video(self, chat_id: int | str, video: Any, **kw: Any) -> Message:
        return await self._upload("send_video", "video", chat_id, video, **kw)

    async def set_my_commands(self, commands: Any, **kw: Any) -> bool:
        self.calls.append(("set_my_commands", {"commands": list(commands), **kw}))
        return True

    async def get_file(self, file_id: str) -> File:
        self.calls.append(("get_file", {"file_id": file_id}))
        return File(file_id, file_id.removeprefix("f-"))

    async def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> bool:
        await self._do(
            "answer_callback_query",
            {"callback_query_id": callback_query_id, "text": text},
            self.platform.answer(callback_query_id, text),
        )
        return True


class TelegramWire:
    """One fake Telegram service: the platform (UTF-16 cap 4096) and its bot."""

    SURFACE = SURFACE
    FAKES: dict[str, type] = {
        "telegram:Bot": FakeBot,
        "telegram:InlineKeyboardButton": InlineKeyboardButton,
        "telegram:InlineKeyboardMarkup": InlineKeyboardMarkup,
        "telegram.error:RetryAfter": RetryAfter,
        "telegram.error:BadRequest": BadRequest,
        "telegram.error:Forbidden": Forbidden,
        "telegram.error:TimedOut": TimedOut,
        "telegram.error:NetworkError": NetworkError,
    }

    def __init__(self, platform: FakeChatPlatform | None = None) -> None:
        self.platform = platform or FakeChatPlatform(
            "telegram", max_len=MAX_LEN, len_fn=utf16_units, bot_user_id="1000"
        )
        self.bot = FakeBot(self.platform)


__all__ = [
    "SURFACE",
    "BadRequest",
    "CallbackQuery",
    "Chat",
    "FakeBot",
    "Forbidden",
    "InlineKeyboardButton",
    "InlineKeyboardMarkup",
    "Message",
    "NetworkError",
    "RetryAfter",
    "TelegramWire",
    "TimedOut",
    "Update",
    "User",
]
