"""A fake Tencent iLink bot service and media CDN behind ``httpx.MockTransport`` (P16-19).

The Weixin adapter speaks plain HTTP (our own iLink client), so this fake is the vendor's HTTP
surface (``ROUTES``) over one `FakeChatPlatform`; nothing opens a socket.

- Every ``/ilink/bot/*`` call must carry ``Authorization: Bearer <token>`` and
  ``AuthorizationType: ilink_bot_token`` (else HTTP 401) and ``base_info.channel_version``.
- ``getupdates`` answers at once with whatever inbound is queued, else waits on the fake clock (up
  to 35 s) for the next; ``get_updates_buf`` counts up. Inbound items become iLink messages:
  DMs from the user (``context_token`` ``ctx-<chat>``), group messages with a ``room_id`` (a
  mention as ``@IndusBot`` in the text), bot messages with ``message_type`` 2; voice, photo and
  document as media items whose bytes sit encrypted (AES-128-ECB, this fake's own key) on the fake
  CDN. Threads and edits do not exist on Weixin: such items produce nothing.
- ``sendmessage`` needs the chat's issued context token (else ``ret`` -14, "session timeout");
  text items post on the platform, media items upload there (the decrypted bytes are kept in
  ``delivered_media``); platform failures answer as HTTP statuses (429 with ``Retry-After``, 404,
  403, 400, 413) or raise httpx's timeouts. ``ret_queue`` answers the next sends with a given
  ``ret`` / ``errmsg`` instead.
- ``getconfig`` connects the platform and returns a typing ticket; ``sendtyping`` toggles typing.
- ``getuploadurl`` hands out an upload parameter; the CDN ``/c2c/upload`` stores the ciphertext and
  answers ``x-encrypted-param``; ``/c2c/download`` serves it back.
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import json
from collections.abc import Mapping
from typing import Any

import httpx

from tests.fakes.clock import FakeClock
from tests.fakes.platform.core import (
    FailureKind,
    FakeChatPlatform,
    FakeInbound,
    FakeMedia,
    FakePlatformError,
)

BASE = "https://ilinkai.weixin.qq.com"
CDN = "https://novac2c.cdn.weixin.qq.com/c2c"
BOT_ACCOUNT = "indusbot@im.bot"
BOT_NAME = "IndusBot"
MAX_CHARS = 2_000

ROUTES: dict[str, str] = {
    "get_updates": "POST /ilink/bot/getupdates",
    "send_message": "POST /ilink/bot/sendmessage",
    "send_typing": "POST /ilink/bot/sendtyping",
    "get_config": "POST /ilink/bot/getconfig",
    "get_upload_url": "POST /ilink/bot/getuploadurl",
    "cdn_upload": "POST <cdn>/upload",
    "cdn_download": "GET <cdn>/download",
}

_STATUS: dict[FailureKind, tuple[int, str]] = {
    FailureKind.RATE_LIMIT: (429, "too many requests"),
    FailureKind.CHAT_NOT_FOUND: (404, "user not found"),
    FailureKind.THREAD_NOT_FOUND: (404, "message not found"),
    FailureKind.FORBIDDEN: (403, "not allowed"),
    FailureKind.PARSE_ERROR: (400, "bad item_list"),
    FailureKind.TOO_LONG: (413, "text too long"),
}


def fake_token() -> str:
    """A bot-token-looking value built at run time (never a credential-shaped literal)."""
    return "".join(["ilink", "-bot-", "7f3a" * 6])


def _ecb(key: bytes) -> Any:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    return Cipher(algorithms.AES(key), modes.ECB())


def cdn_encrypt(data: bytes, key: bytes) -> bytes:
    pad = 16 - len(data) % 16
    encryptor = _ecb(key).encryptor()
    return encryptor.update(data + bytes([pad]) * pad) + encryptor.finalize()


def cdn_decrypt(data: bytes, key: bytes) -> bytes:
    decryptor = _ecb(key).decryptor()
    clear = decryptor.update(data) + decryptor.finalize()
    return clear[: -clear[-1]]


class _Transport(httpx.MockTransport):
    """Closing the adapter's HTTP client is the platform's disconnect."""

    def __init__(self, wire: WeixinWire) -> None:
        super().__init__(wire.handle)
        self._wire = wire

    async def aclose(self) -> None:
        await self._wire.platform.disconnect()


class WeixinWire:
    ROUTES = ROUTES

    def __init__(
        self,
        platform: FakeChatPlatform | None = None,
        *,
        clock: FakeClock | None = None,
        token: str | None = None,
    ) -> None:
        self.platform = platform or FakeChatPlatform(
            "weixin", clock=clock, max_len=MAX_CHARS, bot_user_id=BOT_ACCOUNT, bot_name=BOT_NAME
        )
        self.clock: FakeClock = self.platform.clock
        self.token = token or fake_token()
        self.requests: list[tuple[str, str, dict[str, Any]]] = []
        self.headers: list[httpx.Headers] = []
        self.sent: list[dict[str, Any]] = []
        self.typing: list[tuple[str, int]] = []
        self.cdn: dict[str, bytes] = {}
        self.uploads: dict[str, dict[str, Any]] = {}
        self.delivered_media: list[tuple[str, bytes, dict[str, Any]]] = []
        self.ret_queue: list[dict[str, Any]] = []
        self.poll_errors: list[httpx.Response | Exception] = []
        self.issued: dict[str, str] = {}
        self.raw: list[dict[str, Any]] = []
        self._raw_ready = asyncio.Event()
        self._cursor = itertools.count(1)
        self._ids = itertools.count(1)
        self._media_key = bytes(range(16))

    # -- wiring ------------------------------------------------------------------------------------

    def transport(self) -> httpx.AsyncBaseTransport:
        return _Transport(self)

    def token_for(self, chat_id: str) -> str:
        """The context token this service issues for a chat (and expects back)."""
        token = self.issued.setdefault(chat_id, f"ctx-{chat_id}-{len(self.issued) + 1}")
        return token

    def push_message(self, message: Mapping[str, Any]) -> None:
        """Queue a raw iLink message for the next poll."""
        self.raw.append(dict(message))
        self._raw_ready.set()

    def store_media(self, data: bytes, key: bytes | None = None) -> str:
        """Put ``data`` on the fake CDN encrypted with ``key`` (default: the fake's); its param."""
        param = f"dl{next(self._ids)}"
        self.cdn[param] = cdn_encrypt(data, key or self._media_key)
        return param

    @property
    def media_key_b64(self) -> str:
        return base64.b64encode(self._media_key).decode("ascii")

    # -- dispatch ----------------------------------------------------------------------------------

    async def handle(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        url = request.url
        if url.host == "novac2c.cdn.weixin.qq.com":
            return await self._cdn(request)
        path = url.path
        body: dict[str, Any] = json.loads(request.content or b"{}")
        self.requests.append((request.method, path, body))
        self.headers.append(request.headers)
        if (
            request.headers.get("Authorization") != f"Bearer {self.token}"
            or request.headers.get("AuthorizationType") != "ilink_bot_token"
        ):
            return httpx.Response(401, json={"errmsg": "invalid token"})
        if (body.get("base_info") or {}).get("channel_version") is None:
            return httpx.Response(400, json={"errmsg": "base_info missing"})
        name = path.rsplit("/", 1)[-1]
        handler = getattr(self, f"_route_{name}", None)
        if request.method != "POST" or handler is None:
            return httpx.Response(404, json={"errmsg": "no such endpoint"})
        return await handler(request, body)

    def _failure(self, exc: FakePlatformError, request: httpx.Request) -> httpx.Response:
        if exc.kind is FailureKind.CONNECT_TIMEOUT:
            raise httpx.ConnectTimeout(exc.text, request=request)
        if exc.kind is FailureKind.READ_TIMEOUT:
            raise httpx.ReadTimeout(exc.text, request=request)
        status, text = _STATUS[exc.kind]
        response = httpx.Response(status, json={"ret": -1, "errmsg": text})
        if exc.kind is FailureKind.RATE_LIMIT:
            response.headers["Retry-After"] = str(int(exc.retry_after or 5))
        return response

    # -- the bot API -------------------------------------------------------------------------------

    async def _route_getconfig(
        self, request: httpx.Request, body: dict[str, Any]
    ) -> httpx.Response:
        try:
            await self.platform.connect()
        except FakePlatformError as exc:
            return self._failure(exc, request)
        user = str(body.get("ilink_user_id") or "")
        return httpx.Response(200, json={"ret": 0, "typing_ticket": f"ticket-{user}"})

    async def _route_sendtyping(
        self, request: httpx.Request, body: dict[str, Any]
    ) -> httpx.Response:
        user = str(body.get("ilink_user_id") or "")
        status = int(body.get("status") or 0)
        self.typing.append((user, status))
        if body.get("typing_ticket") != f"ticket-{user}":
            return httpx.Response(200, json={"ret": -1, "errmsg": "bad ticket"})
        try:
            if status == 1:
                await self.platform.typing(user)
            else:
                await self.platform.typing_stop(user)
        except FakePlatformError as exc:
            return self._failure(exc, request)
        return httpx.Response(200, json={"ret": 0})

    async def _route_getupdates(
        self, request: httpx.Request, body: dict[str, Any]
    ) -> httpx.Response:
        if self.poll_errors:
            failure = self.poll_errors.pop(0)
            if isinstance(failure, Exception):
                raise failure
            return failure
        cursor = str(body.get("get_updates_buf") or "")
        items = await self._wait(35.0) if cursor else self.platform.drain_inbound()
        messages = [m for m in (self._message_of(i) for i in items) if m is not None]
        raw, self.raw = self.raw, []
        messages.extend(raw)
        following = f"c{next(self._cursor)}" if messages or not cursor else cursor
        return httpx.Response(
            200,
            json={
                "ret": 0,
                "msgs": messages,
                "get_updates_buf": following,
                "longpolling_timeout_ms": 35_000,
            },
        )

    async def _wait(self, timeout: float) -> list[FakeInbound]:
        first = self.platform.next_inbound()
        if first is None and not self.raw:
            self._raw_ready.clear()
            getter = asyncio.ensure_future(self.platform.receive_inbound())
            raw = asyncio.ensure_future(self._raw_ready.wait())
            sleeper = asyncio.ensure_future(self.clock.sleep(timeout))
            waiting = {getter, raw, sleeper}
            try:
                done, _ = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in waiting:
                    if not task.done():
                        task.cancel()
            if getter in done and not getter.cancelled():
                first = getter.result()
        items = [first] if first is not None else []
        items.extend(self.platform.drain_inbound())
        return items

    def _media_item(self, media: FakeMedia) -> dict[str, Any]:
        param = self.store_media(f"bytes of {media.ref}".encode())
        block = {"encrypt_query_param": param, "aes_key": self.media_key_b64, "encrypt_type": 1}
        if media.kind == "voice":
            return {"type": 3, "voice_item": {"media": block, "playtime": 3}}
        if media.kind in ("photo", "sticker"):
            return {"type": 2, "image_item": {"media": block}}
        if media.kind == "video":
            return {"type": 5, "video_item": {"media": block}}
        return {"type": 4, "file_item": {"media": block, "file_name": media.ref, "len": "10"}}

    def _message_of(self, item: FakeInbound) -> dict[str, Any] | None:
        if item.event != "message" or item.thread_id:
            return None
        items: list[dict[str, Any]] = []
        text = item.text
        if item.mentions_bot:
            text = f"@{BOT_NAME} {text}"
        for media in item.media:
            if media.caption:
                items.append({"type": 1, "text_item": {"text": media.caption}})
            items.append(self._media_item(media))
        if not item.media:
            items.append({"type": 1, "text_item": {"text": text}})
        message: dict[str, Any] = {
            "message_id": item.message_id,
            "from_user_id": item.user_id,
            "to_user_id": BOT_ACCOUNT,
            "message_type": 2 if item.is_bot else 1,
            "create_time_ms": int(item.at.timestamp() * 1000),
            "item_list": items,
        }
        chat = item.chat_id
        if item.chat_kind != "dm":
            message["room_id"] = chat
        message["context_token"] = self.token_for(chat)
        return message

    async def _route_sendmessage(
        self, request: httpx.Request, body: dict[str, Any]
    ) -> httpx.Response:
        msg = dict(body.get("msg") or {})
        self.sent.append(msg)
        to = str(msg.get("to_user_id") or "")
        if self.ret_queue:
            return httpx.Response(200, json=self.ret_queue.pop(0))
        if msg.get("context_token") != self.issued.get(to):
            return httpx.Response(200, json={"ret": -14, "errmsg": "session timeout"})
        message_id = ""
        try:
            for entry in msg.get("item_list") or []:
                kind = entry.get("type")
                if kind == 1:
                    sent = await self.platform.post(to, str(entry["text_item"]["text"]))
                    message_id = sent.message_id
                else:
                    message_id = await self._deliver_media(to, entry)
        except FakePlatformError as exc:
            return self._failure(exc, request)
        return httpx.Response(200, json={"ret": 0, "message_id": message_id})

    async def _deliver_media(self, to: str, entry: Mapping[str, Any]) -> str:
        key_name = {2: "image_item", 3: "voice_item", 4: "file_item", 5: "video_item"}[
            int(entry["type"])
        ]
        payload = dict(entry[key_name])
        block = payload["media"]
        stored = self.cdn[str(block["encrypt_query_param"])]
        key = bytes.fromhex(base64.b64decode(block["aes_key"]).decode("ascii"))
        clear = cdn_decrypt(stored, key)
        self.delivered_media.append((to, clear, payload))
        kind = {2: "photo", 3: "voice", 4: "document", 5: "video"}[int(entry["type"])]
        sent = await self.platform.upload(
            to,
            FakeMedia(kind, str(payload.get("file_name") or key_name)),  # pyright: ignore[reportArgumentType]
        )
        return sent.message_id

    async def _route_getuploadurl(
        self, request: httpx.Request, body: dict[str, Any]
    ) -> httpx.Response:
        param = f"up{next(self._ids)}"
        self.uploads[param] = body
        return httpx.Response(200, json={"ret": 0, "upload_param": param})

    # -- the CDN -----------------------------------------------------------------------------------

    async def _cdn(self, request: httpx.Request) -> httpx.Response:
        self.headers.append(request.headers)
        params = request.url.params
        if request.url.path.endswith("/upload") and request.method == "POST":
            grant = self.uploads.get(params.get("encrypted_query_param", ""))
            if grant is None or grant.get("filekey") != params.get("filekey"):
                return httpx.Response(404, text="unknown upload")
            if len(request.content) != grant.get("filesize"):
                return httpx.Response(400, text="size mismatch")
            param = f"dl{next(self._ids)}"
            self.cdn[param] = bytes(request.content)
            return httpx.Response(200, headers={"x-encrypted-param": param})
        if request.url.path.endswith("/download") and request.method == "GET":
            data = self.cdn.get(params.get("encrypted_query_param", ""))
            if data is None:
                return httpx.Response(404, text="not found")
            return httpx.Response(200, content=data)
        return httpx.Response(404, text="no such endpoint")


__all__ = [
    "BASE",
    "BOT_ACCOUNT",
    "BOT_NAME",
    "CDN",
    "ROUTES",
    "WeixinWire",
    "cdn_decrypt",
    "cdn_encrypt",
    "fake_token",
]
