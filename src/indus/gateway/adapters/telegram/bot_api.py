"""A small async client for the Telegram Bot API over httpx (P15-33).

We do not depend on a Telegram library (the common one is LGPL-3.0). This module speaks the HTTP
interface documented at core.telegram.org/bots/api (Bot API 9.x): one POST per method,
``{"ok": ..., "result": ...}`` answers, errors with an ``error_code`` and a ``description``.
Method names here are the documented camelCase names in snake_case (``BOT_API_METHODS``); results
are small frozen dataclasses with the fields the adapter reads.

The bot token sits in every request path (``/bot<token>/<method>``). It must never reach a log
line, an exception text, a repr or a command line, so:

- requests are built with a placeholder in the path, and ``_TokenTransport`` swaps the real token
  in on a copy of the request just before it goes on the wire: httpx's own request log and every
  ``Request`` object our code or httpx holds keep the placeholder;
- transport failures are re-raised as our own errors ``from None`` with their class name and a
  scrubbed message; Bot API errors carry only Telegram's ``description``;
- ``repr(BotApi)`` names the API host only.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast

import httpx

from indus.foundation.blocking import run_blocking

__all__ = [
    "API_ROOT",
    "BOT_API_METHODS",
    "MAX_DOWNLOAD_BYTES",
    "MAX_UPLOAD_BYTES",
    "BadRequest",
    "BotApi",
    "BotCommand",
    "CallbackQuery",
    "Chat",
    "Conflict",
    "Entity",
    "File",
    "FileRef",
    "Forbidden",
    "InlineKeyboardButton",
    "InlineKeyboardMarkup",
    "InputFile",
    "Message",
    "NetworkError",
    "RetryAfter",
    "TelegramError",
    "TimedOut",
    "Unauthorized",
    "Update",
    "User",
]

log = logging.getLogger(__name__)

API_ROOT: Final = "https://api.telegram.org"
# Shown in the request path instead of the token (``*`` needs no escaping in a path).
_PLACEHOLDER: Final = "****"
# The Bot API's own size limits: uploads 50 MB, downloads through getFile 20 MB.
MAX_UPLOAD_BYTES: Final = 50 * 1024 * 1024
MAX_DOWNLOAD_BYTES: Final = 20 * 1024 * 1024

# Our method → the documented Bot API method it calls.
BOT_API_METHODS: Final[Mapping[str, str]] = {
    "get_me": "getMe",
    "get_updates": "getUpdates",
    "set_webhook": "setWebhook",
    "delete_webhook": "deleteWebhook",
    "send_message": "sendMessage",
    "edit_message_text": "editMessageText",
    "delete_message": "deleteMessage",
    "send_chat_action": "sendChatAction",
    "set_message_reaction": "setMessageReaction",
    "send_photo": "sendPhoto",
    "send_document": "sendDocument",
    "send_voice": "sendVoice",
    "send_audio": "sendAudio",
    "send_video": "sendVideo",
    "set_my_commands": "setMyCommands",
    "get_file": "getFile",
    "answer_callback_query": "answerCallbackQuery",
}


# -- errors ----------------------------------------------------------------------------------------


class TelegramError(Exception):
    """A failed Bot API call. ``message`` is safe to log (no token, no URL)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NetworkError(TelegramError):
    """The request did not get an answer (connection refused, DNS, proxy, a 5xx gateway)."""


class TimedOut(NetworkError):
    """No answer in time after the request was sent: Telegram may have acted on it."""

    def __init__(self, message: str = "Timed out") -> None:
        super().__init__(message)


class BadRequest(TelegramError):
    """HTTP 400: Telegram refused the request as written."""


class Unauthorized(TelegramError):
    """HTTP 401/404 on a bot path: the token is wrong or was revoked."""


class Forbidden(TelegramError):
    """HTTP 403: the bot may not act in this chat (blocked, kicked, no rights)."""


class Conflict(TelegramError):
    """HTTP 409: another getUpdates consumer, or a webhook, holds this bot's updates."""


class RetryAfter(TelegramError):
    """HTTP 429 flood control; ``retry_after`` is the server's wait in seconds."""

    def __init__(self, retry_after: float, message: str | None = None) -> None:
        super().__init__(message or f"Flood control exceeded. Retry in {int(retry_after)} seconds")
        self._retry_after = float(retry_after)

    @property
    def retry_after(self) -> float:
        return self._retry_after


# -- results ---------------------------------------------------------------------------------------


def _dict(value: object) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _opt_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


@dataclass(frozen=True)
class User:
    id: int
    is_bot: bool = False
    first_name: str = ""
    username: str | None = None
    last_name: str | None = None

    @property
    def full_name(self) -> str:
        return " ".join(p for p in (self.first_name, self.last_name) if p) or (self.username or "")

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> User:
        return cls(
            id=int(data.get("id") or 0),
            is_bot=data.get("is_bot") is True,
            first_name=str(data.get("first_name") or ""),
            username=_opt_str(data.get("username")),
            last_name=_opt_str(data.get("last_name")),
        )


@dataclass(frozen=True)
class Chat:
    id: int
    type: str  # private, group, supergroup, channel
    title: str | None = None
    is_forum: bool = False
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None

    @property
    def display_name(self) -> str | None:
        if self.title:
            return self.title
        name = " ".join(p for p in (self.first_name, self.last_name) if p)
        return name or self.username

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Chat:
        return cls(
            id=int(data.get("id") or 0),
            type=str(data.get("type") or ""),
            title=_opt_str(data.get("title")),
            is_forum=data.get("is_forum") is True,
            username=_opt_str(data.get("username")),
            first_name=_opt_str(data.get("first_name")),
            last_name=_opt_str(data.get("last_name")),
        )


@dataclass(frozen=True)
class FileRef:
    """One attachment of a message (photo size, voice, audio, video, document, sticker)."""

    file_id: str
    file_unique_id: str = ""
    file_size: int | None = None
    mime_type: str | None = None
    file_name: str | None = None
    duration: int | None = None
    width: int | None = None
    height: int | None = None
    emoji: str | None = None

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> FileRef:
        return cls(
            file_id=str(data.get("file_id") or ""),
            file_unique_id=str(data.get("file_unique_id") or ""),
            file_size=_opt_int(data.get("file_size")),
            mime_type=_opt_str(data.get("mime_type")),
            file_name=_opt_str(data.get("file_name")),
            duration=_opt_int(data.get("duration")),
            width=_opt_int(data.get("width")),
            height=_opt_int(data.get("height")),
            emoji=_opt_str(data.get("emoji")),
        )


@dataclass(frozen=True)
class Entity:
    """A message entity; ``offset`` and ``length`` count UTF-16 code units."""

    type: str
    offset: int
    length: int
    user: User | None = None

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Entity:
        user = data.get("user")
        return cls(
            type=str(data.get("type") or ""),
            offset=int(data.get("offset") or 0),
            length=int(data.get("length") or 0),
            user=User.from_json(_dict(user)) if isinstance(user, dict) else None,
        )


def _file(data: Mapping[str, Any], key: str) -> FileRef | None:
    value = data.get(key)
    return FileRef.from_json(_dict(value)) if isinstance(value, dict) else None


def _entities(data: Mapping[str, Any], key: str) -> tuple[Entity, ...]:
    raw = data.get(key)
    if not isinstance(raw, list):
        return ()
    return tuple(Entity.from_json(_dict(e)) for e in cast(list[object], raw) if isinstance(e, dict))


@dataclass(frozen=True)
class Message:
    message_id: int
    chat: Chat
    text: str | None = None
    caption: str | None = None
    from_user: User | None = None
    message_thread_id: int | None = None
    reply_to_message: Message | None = None
    voice: FileRef | None = None
    photo: tuple[FileRef, ...] = ()
    document: FileRef | None = None
    date: datetime | None = None
    is_topic_message: bool = False
    sender_chat: Chat | None = None
    quote_text: str | None = None
    entities: tuple[Entity, ...] = ()
    caption_entities: tuple[Entity, ...] = ()
    audio: FileRef | None = None
    video: FileRef | None = None
    video_note: FileRef | None = None
    animation: FileRef | None = None
    sticker: FileRef | None = None
    location: tuple[float, float] | None = None
    media_group_id: str | None = None

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Message:
        sender = data.get("from")
        sender_chat = data.get("sender_chat")
        reply = data.get("reply_to_message")
        photo = data.get("photo")
        date = data.get("date")
        quote = _dict(data.get("quote"))
        where = _dict(data.get("location"))
        location = None
        if isinstance(where.get("latitude"), int | float) and isinstance(
            where.get("longitude"), int | float
        ):
            location = (float(where["latitude"]), float(where["longitude"]))
        return cls(
            message_id=int(data.get("message_id") or 0),
            chat=Chat.from_json(_dict(data.get("chat"))),
            text=_opt_str(data.get("text")),
            caption=_opt_str(data.get("caption")),
            from_user=User.from_json(_dict(sender)) if isinstance(sender, dict) else None,
            message_thread_id=_opt_int(data.get("message_thread_id")),
            reply_to_message=cls.from_json(_dict(reply)) if isinstance(reply, dict) else None,
            voice=_file(data, "voice"),
            photo=tuple(
                FileRef.from_json(_dict(p))
                for p in cast(list[object], photo)
                if isinstance(p, dict)
            )
            if isinstance(photo, list)
            else (),
            document=_file(data, "document"),
            date=datetime.fromtimestamp(date, UTC) if isinstance(date, int) else None,
            is_topic_message=data.get("is_topic_message") is True,
            sender_chat=Chat.from_json(_dict(sender_chat))
            if isinstance(sender_chat, dict)
            else None,
            quote_text=_opt_str(quote.get("text")),
            entities=_entities(data, "entities"),
            caption_entities=_entities(data, "caption_entities"),
            audio=_file(data, "audio"),
            video=_file(data, "video"),
            video_note=_file(data, "video_note"),
            animation=_file(data, "animation"),
            sticker=_file(data, "sticker"),
            location=location,
            media_group_id=_opt_str(data.get("media_group_id")),
        )


@dataclass(frozen=True)
class CallbackQuery:
    id: str
    from_user: User
    data: str | None = None
    message: Message | None = None

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> CallbackQuery:
        message = data.get("message")
        return cls(
            id=str(data.get("id") or ""),
            from_user=User.from_json(_dict(data.get("from"))),
            data=_opt_str(data.get("data")),
            # An inaccessible message has ``date`` 0 and no content; it still names the chat.
            message=Message.from_json(_dict(message)) if isinstance(message, dict) else None,
        )


@dataclass(frozen=True)
class Update:
    update_id: int
    message: Message | None = None
    edited_message: Message | None = None
    callback_query: CallbackQuery | None = None
    channel_post: Message | None = None

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Update:
        def message(key: str) -> Message | None:
            value = data.get(key)
            return Message.from_json(_dict(value)) if isinstance(value, dict) else None

        query = data.get("callback_query")
        return cls(
            update_id=int(data.get("update_id") or 0),
            message=message("message"),
            edited_message=message("edited_message"),
            callback_query=CallbackQuery.from_json(_dict(query))
            if isinstance(query, dict)
            else None,
            channel_post=message("channel_post"),
        )


@dataclass(frozen=True)
class File:
    """What getFile answers: ``file_path`` is valid for an hour."""

    file_id: str
    file_path: str | None = None
    file_unique_id: str = ""
    file_size: int | None = None

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> File:
        return cls(
            file_id=str(data.get("file_id") or ""),
            file_path=_opt_str(data.get("file_path")),
            file_unique_id=str(data.get("file_unique_id") or ""),
            file_size=_opt_int(data.get("file_size")),
        )


@dataclass(frozen=True)
class BotCommand:
    command: str
    description: str


@dataclass(frozen=True)
class InlineKeyboardButton:
    text: str
    callback_data: str


@dataclass(frozen=True)
class InlineKeyboardMarkup:
    inline_keyboard: tuple[tuple[InlineKeyboardButton, ...], ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [{"text": b.text, "callback_data": b.callback_data} for b in row]
                for row in self.inline_keyboard
            ]
        }


@dataclass(frozen=True)
class InputFile:
    """A local file to upload (read off the event loop when the request is built)."""

    path: Path
    filename: str | None = None
    size: int | None = field(default=None, compare=False)

    def __str__(self) -> str:
        return str(self.path)


# -- the client ------------------------------------------------------------------------------------


class _TokenTransport(httpx.AsyncBaseTransport):
    """Puts the token into a copy of each request's path; the original keeps the placeholder."""

    def __init__(self, inner: httpx.AsyncBaseTransport, token: str) -> None:
        self._inner = inner
        self._marker = f"/bot{_PLACEHOLDER}/".encode()
        self._real = f"/bot{token}/".encode()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raw = request.url.raw_path
        if self._marker in raw:
            url = request.url.copy_with(raw_path=raw.replace(self._marker, self._real, 1))
            request = httpx.Request(
                request.method,
                url,
                headers=request.headers,
                stream=request.stream,
                extensions=request.extensions,
            )
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def _params(values: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in values.items() if v is not None}


def _reply(message_id: int | str | None) -> dict[str, Any] | None:
    if message_id is None or message_id == "":
        return None
    return {"message_id": int(message_id), "allow_sending_without_reply": True}


def _markup(markup: InlineKeyboardMarkup | None) -> dict[str, Any] | None:
    return markup.to_json() if markup is not None else None


def _chat(chat_id: int | str) -> int | str:
    text = str(chat_id).strip()
    return int(text) if text.lstrip("-").isdigit() else text


def _read_upload(path: Path) -> bytes:
    with path.open("rb") as handle:
        return handle.read(MAX_UPLOAD_BYTES + 1)


class BotApi:
    """One bot's Bot API client. Safe to reuse after ``aclose`` (a new connection pool opens)."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str | None = None,
        proxy: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        connect_timeout: float = 10.0,
        request_timeout: float = 30.0,
    ) -> None:
        if not token or "/" in token or any(c.isspace() for c in token):
            raise ValueError("the Telegram bot token is empty or malformed")
        self.__token = token
        self._root = (base_url or API_ROOT).rstrip("/")
        self._proxy = proxy
        self._transport = transport
        self._connect_timeout = connect_timeout
        self._request_timeout = request_timeout
        self._client: httpx.AsyncClient | None = None

    def __repr__(self) -> str:
        host = httpx.URL(self._root).host or "?"
        return f"<BotApi {host}>"

    def scrub(self, text: str) -> str:
        """``text`` with the token masked."""
        return text.replace(self.__token, _PLACEHOLDER) if text else text

    # -- plumbing ----------------------------------------------------------------------------------

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            inner = self._transport or httpx.AsyncHTTPTransport(proxy=self._proxy, retries=0)
            self._client = httpx.AsyncClient(
                transport=_TokenTransport(inner, self.__token),
                timeout=httpx.Timeout(self._request_timeout, connect=self._connect_timeout),
                trust_env=False,  # the gateway already chose the proxy (``registry.proxy_for``)
                follow_redirects=False,
            )
        return self._client

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    def _url(self, method: str) -> str:
        return f"{self._root}/bot{_PLACEHOLDER}/{method}"

    def _transport_error(self, exc: httpx.HTTPError) -> TelegramError:
        name = type(exc).__name__
        detail = self.scrub(str(exc)).strip()
        text = f"{name}: {detail}" if detail and detail != name else name
        if isinstance(exc, httpx.ConnectTimeout | httpx.PoolTimeout | httpx.ConnectError):
            return NetworkError(text)  # the request never left
        if isinstance(exc, httpx.ProxyError | httpx.UnsupportedProtocol):
            return NetworkError(text)
        if isinstance(
            exc,
            httpx.TimeoutException | httpx.ReadError | httpx.WriteError | httpx.RemoteProtocolError,
        ):
            return TimedOut(f"Timed out or the answer was lost ({name})")
        return NetworkError(text)

    def _api_error(self, status: int, body: Mapping[str, Any]) -> TelegramError:
        description = self.scrub(str(body.get("description") or f"HTTP {status}"))
        code = body.get("error_code")
        code = code if isinstance(code, int) else status
        wait = _dict(body.get("parameters")).get("retry_after")
        if code == 429 or isinstance(wait, int | float):
            return RetryAfter(float(wait) if isinstance(wait, int | float) else 1.0, description)
        if code == 400:
            return BadRequest(description)
        if code in (401, 404):
            return Unauthorized(description)
        if code == 403:
            return Forbidden(description)
        if code == 409:
            return Conflict(description)
        if code >= 500:
            return NetworkError(description)
        return TelegramError(description)

    async def _call(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        files: Mapping[str, InputFile] | None = None,
        timeout: float | None = None,
    ) -> Any:
        """POST one method; the ``result`` on success, else a `TelegramError`."""
        values = _params(params or {})
        request_timeout = httpx.Timeout(
            timeout if timeout is not None else self._request_timeout,
            connect=self._connect_timeout,
        )
        try:
            if files:
                upload: dict[str, tuple[str, bytes]] = {}
                for name, item in files.items():
                    data = await run_blocking(_read_upload, item.path, label="telegram upload")
                    if len(data) > MAX_UPLOAD_BYTES:
                        raise BadRequest("Request Entity Too Large: file is over 50 MB")
                    upload[name] = (item.filename or item.path.name, data)
                form = {k: v if isinstance(v, str) else json.dumps(v) for k, v in values.items()}
                response = await self._http().post(
                    self._url(method), data=form, files=upload, timeout=request_timeout
                )
            else:
                response = await self._http().post(
                    self._url(method), json=values, timeout=request_timeout
                )
        except httpx.HTTPError as exc:
            raise self._transport_error(exc) from None
        except OSError as exc:  # reading the upload
            raise BadRequest(f"could not read the file to upload ({type(exc).__name__})") from None
        try:
            body = _dict(response.json())
        except ValueError:
            body = {}
        if response.status_code == 200 and body.get("ok") is True:
            return body.get("result")
        raise self._api_error(response.status_code, body)

    # -- methods (snake_case of ``BOT_API_METHODS``) ---------------------------------------------

    async def get_me(self) -> User:
        return User.from_json(_dict(await self._call("getMe")))

    async def get_updates(
        self,
        offset: int | None = None,
        timeout: float = 0,
        *,
        limit: int | None = None,
        allowed_updates: Sequence[str] | None = None,
    ) -> list[Update]:
        result = await self._call(
            "getUpdates",
            {
                "offset": offset,
                "timeout": int(timeout),
                "limit": limit,
                "allowed_updates": list(allowed_updates) if allowed_updates is not None else None,
            },
            timeout=float(timeout) + self._request_timeout,
        )
        rows = cast(list[object], result) if isinstance(result, list) else []
        return [Update.from_json(_dict(row)) for row in rows if isinstance(row, dict)]

    async def set_webhook(
        self,
        url: str,
        secret_token: str | None = None,
        *,
        allowed_updates: Sequence[str] | None = None,
        drop_pending_updates: bool | None = None,
    ) -> bool:
        result = await self._call(
            "setWebhook",
            {
                "url": url,
                "secret_token": secret_token,
                "allowed_updates": list(allowed_updates) if allowed_updates is not None else None,
                "drop_pending_updates": drop_pending_updates,
            },
        )
        return result is True

    async def delete_webhook(self, *, drop_pending_updates: bool | None = None) -> bool:
        result = await self._call("deleteWebhook", {"drop_pending_updates": drop_pending_updates})
        return result is True

    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        parse_mode: str | None = None,
        reply_to_message_id: int | str | None = None,
        message_thread_id: int | None = None,
        reply_markup: InlineKeyboardMarkup | None = None,
        *,
        disable_notification: bool | None = None,
    ) -> Message:
        result = await self._call(
            "sendMessage",
            {
                "chat_id": _chat(chat_id),
                "text": text,
                "parse_mode": parse_mode,
                "reply_parameters": _reply(reply_to_message_id),
                "message_thread_id": message_thread_id,
                "reply_markup": _markup(reply_markup),
                "disable_notification": disable_notification,
                "link_preview_options": {"is_disabled": True},
            },
        )
        return Message.from_json(_dict(result))

    async def edit_message_text(
        self,
        text: str,
        chat_id: int | str,
        message_id: int,
        parse_mode: str | None = None,
        *,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> Message | bool:
        result = await self._call(
            "editMessageText",
            {
                "chat_id": _chat(chat_id),
                "message_id": int(message_id),
                "text": text,
                "parse_mode": parse_mode,
                "reply_markup": _markup(reply_markup),
                "link_preview_options": {"is_disabled": True},
            },
        )
        return Message.from_json(_dict(result)) if isinstance(result, dict) else result is True

    async def delete_message(self, chat_id: int | str, message_id: int) -> bool:
        result = await self._call(
            "deleteMessage", {"chat_id": _chat(chat_id), "message_id": int(message_id)}
        )
        return result is True

    async def send_chat_action(
        self, chat_id: int | str, action: str, message_thread_id: int | None = None
    ) -> bool:
        result = await self._call(
            "sendChatAction",
            {"chat_id": _chat(chat_id), "action": action, "message_thread_id": message_thread_id},
        )
        return result is True

    async def set_message_reaction(
        self, chat_id: int | str, message_id: int, reaction: str | None = None
    ) -> bool:
        """Set one emoji reaction (``None`` clears the bot's reactions)."""
        chosen = [{"type": "emoji", "emoji": reaction}] if reaction else []
        result = await self._call(
            "setMessageReaction",
            {"chat_id": _chat(chat_id), "message_id": int(message_id), "reaction": chosen},
        )
        return result is True

    async def _send_file(
        self,
        method: str,
        field_name: str,
        chat_id: int | str,
        source: InputFile | str,
        *,
        caption: str | None,
        parse_mode: str | None,
        reply_to_message_id: int | str | None,
        message_thread_id: int | None,
    ) -> Message:
        params: dict[str, Any] = {
            "chat_id": _chat(chat_id),
            "caption": caption,
            "parse_mode": parse_mode,
            "reply_parameters": _reply(reply_to_message_id),
            "message_thread_id": message_thread_id,
        }
        files: dict[str, InputFile] | None = None
        if isinstance(source, InputFile):
            files = {field_name: source}
        else:
            params[field_name] = source  # a URL or a file id Telegram already has
        result = await self._call(method, params, files=files, timeout=120.0)
        return Message.from_json(_dict(result))

    async def send_photo(
        self,
        chat_id: int | str,
        photo: InputFile | str,
        *,
        caption: str | None = None,
        parse_mode: str | None = None,
        reply_to_message_id: int | str | None = None,
        message_thread_id: int | None = None,
    ) -> Message:
        return await self._send_file(
            "sendPhoto",
            "photo",
            chat_id,
            photo,
            caption=caption,
            parse_mode=parse_mode,
            reply_to_message_id=reply_to_message_id,
            message_thread_id=message_thread_id,
        )

    async def send_document(
        self,
        chat_id: int | str,
        document: InputFile | str,
        *,
        caption: str | None = None,
        parse_mode: str | None = None,
        reply_to_message_id: int | str | None = None,
        message_thread_id: int | None = None,
    ) -> Message:
        return await self._send_file(
            "sendDocument",
            "document",
            chat_id,
            document,
            caption=caption,
            parse_mode=parse_mode,
            reply_to_message_id=reply_to_message_id,
            message_thread_id=message_thread_id,
        )

    async def send_voice(
        self,
        chat_id: int | str,
        voice: InputFile | str,
        *,
        caption: str | None = None,
        parse_mode: str | None = None,
        reply_to_message_id: int | str | None = None,
        message_thread_id: int | None = None,
    ) -> Message:
        return await self._send_file(
            "sendVoice",
            "voice",
            chat_id,
            voice,
            caption=caption,
            parse_mode=parse_mode,
            reply_to_message_id=reply_to_message_id,
            message_thread_id=message_thread_id,
        )

    async def send_audio(
        self,
        chat_id: int | str,
        audio: InputFile | str,
        *,
        caption: str | None = None,
        parse_mode: str | None = None,
        reply_to_message_id: int | str | None = None,
        message_thread_id: int | None = None,
    ) -> Message:
        return await self._send_file(
            "sendAudio",
            "audio",
            chat_id,
            audio,
            caption=caption,
            parse_mode=parse_mode,
            reply_to_message_id=reply_to_message_id,
            message_thread_id=message_thread_id,
        )

    async def send_video(
        self,
        chat_id: int | str,
        video: InputFile | str,
        *,
        caption: str | None = None,
        parse_mode: str | None = None,
        reply_to_message_id: int | str | None = None,
        message_thread_id: int | None = None,
    ) -> Message:
        return await self._send_file(
            "sendVideo",
            "video",
            chat_id,
            video,
            caption=caption,
            parse_mode=parse_mode,
            reply_to_message_id=reply_to_message_id,
            message_thread_id=message_thread_id,
        )

    async def set_my_commands(
        self, commands: Sequence[BotCommand], *, scope: Mapping[str, Any] | None = None
    ) -> bool:
        result = await self._call(
            "setMyCommands",
            {
                "commands": [
                    {"command": c.command, "description": c.description} for c in commands
                ],
                "scope": dict(scope) if scope is not None else None,
            },
        )
        return result is True

    async def get_file(self, file_id: str) -> File:
        return File.from_json(_dict(await self._call("getFile", {"file_id": file_id})))

    async def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> bool:
        result = await self._call(
            "answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text}
        )
        return result is True

    async def download_file(self, file_path: str) -> AsyncIterator[bytes]:
        """Stream a file getFile pointed at; stops with `BadRequest` past 20 MB."""
        if not file_path or file_path.startswith("/") or ".." in file_path.split("/"):
            raise BadRequest("Bad Request: invalid file path")
        url = f"{self._root}/file/bot{_PLACEHOLDER}/{file_path}"
        total = 0
        try:
            async with self._http().stream("GET", url, timeout=120.0) as response:
                if response.status_code != 200:
                    raise self._api_error(response.status_code, {})
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_BYTES:
                        raise BadRequest("Bad Request: file is too big")
                    yield chunk
        except httpx.HTTPError as exc:
            raise self._transport_error(exc) from None
