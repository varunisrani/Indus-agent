"""A fake of the Telegram client surface our adapter touches (P15-01, P15-33).

We talk to Telegram through our own Bot API client (``indus.gateway.adapters.telegram.bot_api``,
no library: the common one is LGPL-3.0). ``FakeBot`` stands in for ``BotApi``: the same method
names and keyword arguments, the client's own result types (``Message`` with ``message_id`` /
``chat.id``, ``Update``, ``File``…) and its own error classes (``RetryAfter``, ``BadRequest``,
``Forbidden``, ``TimedOut``, ``NetworkError``), backed by one `FakeChatPlatform`. Only names
listed in ``SURFACE`` exist; the drift check proves each is on the real client and that the
client's methods are documented Bot API methods.
"""

from __future__ import annotations

import itertools
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from indus.gateway.adapters.base import utf16_units
from indus.gateway.adapters.telegram.bot_api import (
    BadRequest,
    CallbackQuery,
    Chat,
    File,
    FileRef,
    Forbidden,
    ForumTopic,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    NetworkError,
    RetryAfter,
    TelegramError,
    TimedOut,
    Update,
    User,
)
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

_CLIENT = "indus.gateway.adapters.telegram.bot_api"

SURFACE: dict[str, tuple[str, ...]] = {
    f"{_CLIENT}:BotApi": (
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
        "send_message_draft",
        "create_forum_topic",
        "download_file",
        "aclose",
    ),
    f"{_CLIENT}:InlineKeyboardButton": (),
    f"{_CLIENT}:InlineKeyboardMarkup": (),
    f"{_CLIENT}:RetryAfter": ("retry_after",),
    f"{_CLIENT}:BadRequest": (),
    f"{_CLIENT}:Forbidden": (),
    f"{_CLIENT}:TimedOut": (),
    f"{_CLIENT}:NetworkError": (),
}


def _translate(exc: FakePlatformError) -> TelegramError:
    kind = exc.kind
    if kind is FailureKind.RATE_LIMIT:
        return RetryAfter(exc.retry_after or 5.0)
    if kind is FailureKind.READ_TIMEOUT:
        return TimedOut()
    if kind is FailureKind.CONNECT_TIMEOUT:
        return NetworkError("ConnectError: connection refused")
    if kind is FailureKind.FORBIDDEN:
        return Forbidden(exc.text)
    return BadRequest(exc.text)


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


def _file(media: FakeMedia) -> FileRef:
    duration = int(media.duration) if media.duration is not None else None
    return FileRef(
        file_id=f"f-{media.ref}",
        file_unique_id=f"u-{media.ref}",
        mime_type=media.mime or None,
        file_name=media.ref if media.kind == "document" else None,
        duration=duration,
    )


@dataclass
class FakeBot:
    """A stand-in for ``BotApi`` over a `FakeChatPlatform`."""

    platform: FakeChatPlatform
    calls: list[tuple[str, dict[str, Any]]] = field(
        default_factory=list[tuple[str, dict[str, Any]]]
    )
    # Bytes ``download_file`` serves per file path (default: a short text naming the path).
    files: dict[str, bytes] = field(default_factory=dict[str, bytes])
    bot_username: str = "indus_test_bot"
    # P16-34: the bot's Threaded Mode (``getMe.has_topics_enabled``); topics opened by the bot.
    has_topics_enabled: bool = False
    topics: list[tuple[str, int, str]] = field(default_factory=list[tuple[str, int, str]])
    # P16-33: every draft frame (chat, draft id, text, thread); a refusal text for drafts.
    drafts: list[tuple[str, int, str, int | None]] = field(
        default_factory=list[tuple[str, int, str, int | None]]
    )
    refuse_drafts: str | None = None
    _topic_ids: itertools.count[int] = field(default_factory=lambda: itertools.count(101))

    async def _do(self, name: str, kwargs: dict[str, Any], coro: Any) -> Any:
        self.calls.append((name, kwargs))
        try:
            return await coro
        except FakePlatformError as exc:
            raise _translate(exc) from None

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def _message(self, sent: FakeMessage) -> Message:
        chat = Chat(int(sent.chat_id), "private")
        return Message(int(sent.message_id), chat, text=sent.text)

    async def get_me(self) -> User:
        await self._do("get_me", {}, self.platform.connect())
        return User(
            int(self.platform.bot_user_id or 1),
            True,
            self.platform.bot_name,
            username=self.bot_username,
            has_topics_enabled=self.has_topics_enabled,
        )

    async def aclose(self) -> None:
        self.calls.append(("aclose", {}))
        await self.platform.disconnect()

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
            author = None
            if original is not None and original.from_bot:
                author = User(int(self.platform.bot_user_id or 1), True, self.platform.bot_name)
            reply = Message(int(item.reply_to), chat, text=text, from_user=author)
        media = item.media[0] if item.media else None
        topic = _int(item.thread_id)
        message = Message(
            message_id=int(item.message_id) if item.message_id else 0,
            chat=chat,
            text=item.text if media is None else None,
            caption=item.text if media is not None else None,
            from_user=user,
            message_thread_id=topic,
            is_topic_message=topic is not None and chat.is_forum,
            reply_to_message=reply,
            voice=_file(media) if media and media.kind == "voice" else None,
            photo=(_file(media),) if media and media.kind == "photo" else (),
            document=_file(media) if media and media.kind == "document" else None,
            audio=_file(media) if media and media.kind == "audio" else None,
            video=_file(media) if media and media.kind == "video" else None,
            sticker=_file(media) if media and media.kind == "sticker" else None,
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
        reply_to_message_id: int | str | None = None,
        message_thread_id: int | None = None,
        reply_markup: InlineKeyboardMarkup | None = None,
        **kw: Any,
    ) -> Message:
        buttons = tuple(
            Button(b.text, b.callback_data)
            for row in (reply_markup.inline_keyboard if reply_markup else ())
            for b in row
        )
        args = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "reply_to_message_id": reply_to_message_id,
            "message_thread_id": message_thread_id,
            **kw,
        }
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
        args = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode,
            **kw,
        }
        # Like the Bot API: an edit without ``reply_markup`` removes the inline keyboard.
        markup: InlineKeyboardMarkup | None = kw.get("reply_markup")
        buttons = tuple(
            Button(b.text, b.callback_data)
            for row in (markup.inline_keyboard if markup else ())
            for b in row
        )
        edited = await self._do(
            "edit_message_text",
            args,
            self.platform.edit(str(chat_id), str(message_id), text, buttons=buttons),
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
            {"chat_id": chat_id, "action": action, "message_thread_id": message_thread_id},
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
        reply = kw.get("reply_to_message_id")
        sent = await self._do(
            name,
            {"chat_id": chat_id, "file": ref, **kw},
            self.platform.upload(
                str(chat_id),
                media,
                reply_to=str(reply) if reply else None,
                thread_id=str(thread) if thread else None,
            ),
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
        path = file_id.removeprefix("f-")
        return File(file_id, path, file_size=len(self._content(path)))

    def _content(self, file_path: str) -> bytes:
        return self.files.get(file_path, f"fake telegram file {file_path}\n".encode())

    async def download_file(self, file_path: str) -> AsyncIterator[bytes]:
        self.calls.append(("download_file", {"file_path": file_path}))
        data = self._content(file_path)
        for start in range(0, len(data), 64 * 1024):
            yield data[start : start + 64 * 1024]

    async def send_message_draft(
        self,
        chat_id: int | str,
        draft_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
        message_thread_id: int | None = None,
    ) -> bool:
        """Drafts are previews: recorded, never stored as messages. Arm failures with
        ``platform.fail(kind, op="draft")``; ``refuse_drafts`` refuses every frame."""
        args = {
            "chat_id": chat_id,
            "draft_id": draft_id,
            "text": text,
            "parse_mode": parse_mode,
            "message_thread_id": message_thread_id,
        }

        async def frame() -> bool:
            self.platform._check("draft", str(chat_id))  # pyright: ignore[reportPrivateUsage]
            if self.refuse_drafts is not None:
                raise BadRequest(f"Bad Request: {self.refuse_drafts}")
            if utf16_units(text) > MAX_LEN:
                raise BadRequest("Bad Request: text is too long")
            self.drafts.append((str(chat_id), draft_id, text, message_thread_id))
            return True

        return await self._do("send_message_draft", args, frame())

    async def create_forum_topic(self, chat_id: int | str, name: str) -> ForumTopic:
        args = {"chat_id": chat_id, "name": name}

        async def opened() -> ForumTopic:
            self.platform._check("topic", str(chat_id))  # pyright: ignore[reportPrivateUsage]
            if not self.has_topics_enabled and str(chat_id).isdigit():
                raise BadRequest("Bad Request: the chat is not a forum")
            thread = next(self._topic_ids)
            self.topics.append((str(chat_id), thread, name))
            return ForumTopic(thread, name)

        return await self._do("create_forum_topic", args, opened())

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
        f"{_CLIENT}:BotApi": FakeBot,
        f"{_CLIENT}:InlineKeyboardButton": InlineKeyboardButton,
        f"{_CLIENT}:InlineKeyboardMarkup": InlineKeyboardMarkup,
        f"{_CLIENT}:RetryAfter": RetryAfter,
        f"{_CLIENT}:BadRequest": BadRequest,
        f"{_CLIENT}:Forbidden": Forbidden,
        f"{_CLIENT}:TimedOut": TimedOut,
        f"{_CLIENT}:NetworkError": NetworkError,
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
    "ForumTopic",
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
