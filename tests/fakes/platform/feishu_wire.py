"""A fake Feishu / Lark Open Platform: REST behind ``httpx.MockTransport``, a fake long-connection
socket and signed, encrypted webhook callbacks (P16-14, P16-15).

One `FakeChatPlatform` underneath; ids are the platform's own: a DM chat id is the user's open id
(``ou_…``), any other chat id is used as written, a message id is the platform's message id.

- REST: every route in ``ROUTES`` (the adapter's ``client.ROUTES`` must be a subset, checked by the
  adapter tests). The tenant token is checked on every call (a wrong or retired token answers code
  99991663); the token route answers code 10014 for wrong app credentials and is where
  ``platform.connect()`` happens. Armed failures become Feishu's answers: 429 + code 99991400 +
  ``x-ogw-ratelimit-reset``; 400 + code 230001 naming ``receive_id`` (chat not found), 230011
  (message withdrawn), 230002 (bot not in the chat), 230001 about the content (bad format), 230025
  (too long); connect and read timeouts raise httpx's own exceptions.
- Long connection: ``open(url)`` stands in for the httpx-ws opener and gives a `FakeSocket` that
  turns each inbound update into a protobuf data frame (event JSON; button presses as ``card``
  frames) and records every frame the adapter sends (acks, pings). Its frame codec is written
  separately from the adapter's (``_frame`` / ``read_frame`` here). ``split_parts`` sends events
  in that many parts.
- Webhook: ``render(item)`` gives a signed (``X-Lark-Signature``) and encrypted callback for the
  adapter's listener (``deliver`` / ``pump`` from `HttpCallbackWire`).
- Documents and comments: ``documents[token] = text``; ``comments[token]`` holds comment dicts;
  ``reply_not_allowed`` makes replies to those comment ids answer code 1069302.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import itertools
import json
import os
from collections.abc import AsyncIterator
from email.parser import BytesParser
from email.policy import HTTP
from typing import TYPE_CHECKING, Any

import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from tests.fakes.clock import FakeClock
from tests.fakes.platform.core import (
    Button,
    FailureKind,
    FakeChatPlatform,
    FakeInbound,
    FakeMedia,
    FakePlatformError,
)
from tests.fakes.platform.http_wire import ApiCall, Callback, HttpCallbackWire, api, json_body

if TYPE_CHECKING:
    from indus.gateway.adapters.feishu.adapter import FeishuAdapter, FeishuOptions

FEISHU_URL = "https://open.feishu.cn"
LARK_URL = "https://open.larksuite.com"
BOT_OPEN_ID = "ou_bot500"
BOT_NAME = "IndusBot"
MAX_LEN = 8000
SERVICE_ID = 77
WEBHOOK_PATH = "/feishu/webhook"

ROUTES: dict[str, str] = {
    "tenant_token": "POST /open-apis/auth/v3/tenant_access_token/internal",
    "bot_info": "GET /open-apis/bot/v3/info",
    "ws_endpoint": "POST /callback/ws/endpoint",
    "create_message": "POST /open-apis/im/v1/messages",
    "reply_message": "POST /open-apis/im/v1/messages/{message_id}/reply",
    "update_message": "PUT /open-apis/im/v1/messages/{message_id}",
    "patch_message": "PATCH /open-apis/im/v1/messages/{message_id}",
    "delete_message": "DELETE /open-apis/im/v1/messages/{message_id}",
    "get_message": "GET /open-apis/im/v1/messages/{message_id}",
    "get_chat": "GET /open-apis/im/v1/chats/{chat_id}",
    "upload_image": "POST /open-apis/im/v1/images",
    "upload_file": "POST /open-apis/im/v1/files",
    "get_resource": "GET /open-apis/im/v1/messages/{message_id}/resources/{file_key}",
    "add_reaction": "POST /open-apis/im/v1/messages/{message_id}/reactions",
    "delete_reaction": "DELETE /open-apis/im/v1/messages/{message_id}/reactions/{reaction_id}",
    "get_user": "GET /open-apis/contact/v3/users/{user_id}",
    "doc_raw_content": "GET /open-apis/docx/v1/documents/{document_id}/raw_content",
    "list_comments": "GET /open-apis/drive/v1/files/{file_token}/comments",
    "get_comment": "GET /open-apis/drive/v1/files/{file_token}/comments/{comment_id}",
    "list_replies": "GET /open-apis/drive/v1/files/{file_token}/comments/{comment_id}/replies",
    "reply_comment": "POST /open-apis/drive/v1/files/{file_token}/comments/{comment_id}/replies",
    "add_comment": "POST /open-apis/drive/v1/files/{file_token}/comments",
}


def fake_app_id() -> str:
    return "".join(["cli", "_", "fake", "0" * 12])


def fake_secret(tag: str = "sec") -> str:
    """A secret-looking value built at runtime (never a credential-shaped literal)."""
    return "".join(["fs", "fake", tag, "7" * 24])


def _ok(data: Any = None, **extra: Any) -> httpx.Response:
    body: dict[str, Any] = {"code": 0, "msg": "success", **extra}
    if data is not None:
        body["data"] = data
    return httpx.Response(200, json=body)


def _code(code: int, msg: str, status: int = 400, **headers: str) -> httpx.Response:
    return httpx.Response(status, json={"code": code, "msg": msg}, headers=headers)


# -- an independent protobuf writer / reader for the frames ------------------------------------


def _uvarint(n: int) -> bytes:
    out = b""
    while n > 0x7F:
        out += bytes([(n & 0x7F) | 0x80])
        n >>= 7
    return out + bytes([n])


def _ld(field: int, payload: bytes) -> bytes:
    return bytes([(field << 3) | 2]) + _uvarint(len(payload)) + payload


def _frame(
    method: int, headers: dict[str, str], payload: bytes = b"", *, seq: int = 0, service: int = 0
) -> bytes:
    out = bytes([0x08]) + _uvarint(seq) + bytes([0x10]) + _uvarint(seq * 10)
    out += bytes([0x18]) + _uvarint(service) + bytes([0x20]) + _uvarint(method)
    for key, value in headers.items():
        out += _ld(5, _ld(1, key.encode()) + _ld(2, value.encode()))
    if payload:
        out += _ld(8, payload)
    return out


def read_frame(data: bytes) -> dict[str, Any]:
    """{"method", "service", "seq", "headers": {...}, "payload": bytes} of a frame."""
    found: dict[str, Any] = {"headers": {}, "payload": b"", "method": 0, "service": 0, "seq": 0}
    i = 0

    def varint() -> int:
        nonlocal i
        shift = value = 0
        while True:
            byte = data[i]
            i += 1
            value |= (byte & 0x7F) << shift
            shift += 7
            if byte < 0x80:
                return value

    while i < len(data):
        key = varint()
        field, kind = key >> 3, key & 7
        if kind == 0:
            value = varint()
            names = {1: "seq", 3: "service", 4: "method"}
            if field in names:
                found[names[field]] = value
        else:
            size = varint()
            chunk = data[i : i + size]
            i += size
            if field == 5:
                inner = read_frame_header(chunk)
                found["headers"][inner[0]] = inner[1]
            elif field == 8:
                found["payload"] = chunk
    return found


def read_frame_header(data: bytes) -> tuple[str, str]:
    key = value = ""
    i = 0
    while i < len(data):
        field = data[i] >> 3
        size = data[i + 1]
        text = data[i + 2 : i + 2 + size].decode()
        i += 2 + size
        if field == 1:
            key = text
        else:
            value = text
    return key, value


# -- webhook crypto (the vendor's scheme, done here with ``cryptography`` / hashlib) -----------


def encrypt(encrypt_key: str, plain: bytes, iv: bytes | None = None) -> str:
    key = hashlib.sha256(encrypt_key.encode()).digest()
    iv = iv or os.urandom(16)
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plain) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return base64.b64encode(iv + encryptor.update(padded) + encryptor.finalize()).decode()


def sign(timestamp: str, nonce: str, encrypt_key: str, body: bytes) -> str:
    return hashlib.sha256(
        timestamp.encode() + nonce.encode() + encrypt_key.encode() + body
    ).hexdigest()


class FakeSocket:
    """One open long connection."""

    def __init__(self, wire: FeishuWire) -> None:
        self.wire = wire
        self.sent: list[bytes] = []
        self.closed = False
        self._waiting: list[bytes] = []

    def frames_sent(self) -> list[dict[str, Any]]:
        return [read_frame(raw) for raw in self.sent]

    async def receive_bytes(self) -> bytes:
        while True:
            if self._waiting:
                return self._waiting.pop(0)
            if self.wire.raw_frames:
                frame = self.wire.raw_frames.pop(0)
                if frame is None:
                    raise ConnectionResetError("the server closed the link")
                return frame
            item = self.wire.platform.next_inbound()
            if item is None:
                item = await self.wire.next_item()
                if item is None:
                    continue
            frames = self.wire.frames_for(item)
            if frames:
                self._waiting.extend(frames[1:])
                return frames[0]

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(bytes(data))

    async def close(self) -> None:
        self.closed = True


class _Transport(httpx.MockTransport):
    def __init__(self, wire: FeishuWire) -> None:
        super().__init__(wire._handle)  # pyright: ignore[reportPrivateUsage]
        self._wire = wire

    async def aclose(self) -> None:
        await self._wire.platform.disconnect()


class FeishuWire(HttpCallbackWire):
    """The fake service (see the module notes)."""

    ROUTES = ROUTES

    def __init__(
        self,
        platform: FakeChatPlatform | None = None,
        *,
        clock: FakeClock | None = None,
        domain: str = "feishu",
        encrypt_key: str | None = None,
        verification_token: str | None = None,
    ) -> None:
        super().__init__(
            platform
            or FakeChatPlatform(
                "feishu", clock=clock, max_len=MAX_LEN, bot_user_id=BOT_OPEN_ID, bot_name=BOT_NAME
            )
        )
        self.clock: FakeClock = self.platform.clock
        self.base_url = LARK_URL if domain == "lark" else FEISHU_URL
        self.domain = domain
        self.app_id = fake_app_id()
        self.app_secret = fake_secret()
        self.encrypt_key = encrypt_key if encrypt_key is not None else fake_secret("enc")
        self.verification_token = (
            verification_token if verification_token is not None else fake_secret("vt")
        )
        self.tokens: list[str] = []
        self.retired: set[str] = set()
        self.token_ttl = 7200
        self.hosts: list[str] = []
        self.sockets: list[FakeSocket] = []
        self.socket_urls: list[str] = []
        self.socket_failures: list[BaseException] = []
        self.raw_frames: list[bytes | None] = []
        self.split_parts = 1
        self.files: dict[str, tuple[str, bytes, str]] = {}
        self.reactions: list[tuple[str, str, str]] = []
        self.user_names: dict[str, str] = {}
        self.documents: dict[str, str] = {}
        self.comments: dict[str, list[dict[str, Any]]] = {}
        self.comment_posts: list[tuple[str, str, str]] = []
        self.reply_not_allowed: set[str] = set()
        self.cards: dict[str, dict[str, Any]] = {}
        self.bot_open_id = BOT_OPEN_ID
        self._ids = itertools.count(1)
        self._events = itertools.count(1)
        self._ready = asyncio.Event()

    # -- wiring ------------------------------------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return _Transport(self)

    def adapter(self, *, options: FeishuOptions | None = None, **kw: Any) -> FeishuAdapter:
        from indus.gateway.adapters.feishu.adapter import FeishuAdapter, FeishuOptions

        kw.setdefault("clock", self.clock)
        kw.setdefault("rng", lambda: 0.0)
        kw.setdefault("opener", self.open)
        return FeishuAdapter(
            app_id=self.app_id,
            app_secret=self.app_secret,
            transport=self.transport(),
            options=options or FeishuOptions(domain=self.domain),
            **kw,
        )

    def webhook_adapter(self, *, options: FeishuOptions | None = None, **kw: Any) -> FeishuAdapter:
        from indus.gateway.adapters.feishu.adapter import FeishuOptions

        adapter = self.adapter(
            options=options or FeishuOptions(domain=self.domain, connection_mode="webhook"),
            encrypt_key=self.encrypt_key,
            verification_token=self.verification_token,
            serve=False,
            **kw,
        )
        assert adapter.listener is not None
        self.attach(adapter.listener.app)
        return adapter

    @contextlib.asynccontextmanager
    async def open(self, url: str) -> AsyncIterator[FakeSocket]:
        """Stands in for the httpx-ws opener."""
        self.socket_urls.append(url)
        if self.socket_failures:
            raise self.socket_failures.pop(0)
        socket = FakeSocket(self)
        self.sockets.append(socket)
        try:
            yield socket
        finally:
            socket.closed = True

    def drop_link(self) -> None:
        """The server closes the current link."""
        self.raw_frames.append(None)
        self._ready.set()

    async def next_item(self) -> FakeInbound | None:
        """The next inbound update, or None when a raw frame was queued meanwhile."""
        if self.raw_frames:
            return None
        self._ready.clear()
        getter = asyncio.ensure_future(self.platform.receive_inbound())
        ready = asyncio.ensure_future(self._ready.wait())
        try:
            done, _ = await asyncio.wait({getter, ready}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (getter, ready):
                if not task.done():
                    task.cancel()
        if getter in done and not getter.cancelled():
            return getter.result()
        return None

    # -- inbound shapes ----------------------------------------------------------------------------

    def _header(self, event_type: str) -> dict[str, Any]:
        return {
            "event_id": f"ev{next(self._events)}",
            "event_type": event_type,
            "create_time": str(int(self.clock.now().timestamp() * 1000)),
            "token": self.verification_token,
            "app_id": self.app_id,
            "tenant_key": "tenant1",
        }

    def _store(self, media: FakeMedia) -> str:
        key = f"{'img' if media.kind == 'photo' else 'file'}_{next(self._ids)}"
        self.files[key] = (media.ref, f"bytes of {media.ref}".encode(), media.mime)
        return key

    def event_for(self, item: FakeInbound) -> tuple[str, dict[str, Any]] | None:
        """(frame type, event body) for an inbound update; None when Feishu has no such event."""
        if item.event == "edited":
            return None
        if item.event == "button":
            body = {
                "schema": "2.0",
                "header": self._header("card.action.trigger"),
                "event": {
                    "operator": {"open_id": item.user_id, "union_id": "on_" + item.user_id},
                    "token": f"c-{item.button_id}",
                    "action": {"tag": "button", "value": {"indus": item.button_data}},
                    "context": {
                        "open_message_id": item.message_id,
                        "open_chat_id": item.chat_id,
                    },
                },
            }
            return "card", body
        mentions: list[dict[str, Any]] = []
        if item.media:
            media = item.media[0]
            key = self._store(media)
            if media.kind == "photo" and media.caption:
                message_type = "post"
                content: dict[str, Any] = {
                    "title": "",
                    "content": [
                        [{"tag": "text", "text": media.caption}],
                        [{"tag": "img", "image_key": key}],
                    ],
                }
            elif media.kind == "photo":
                message_type, content = "image", {"image_key": key}
            elif media.kind in ("voice", "audio"):
                message_type, content = "audio", {"file_key": key, "duration": 3000}
            elif media.kind == "video":
                message_type, content = "media", {"file_key": key, "file_name": media.ref}
            elif media.kind == "sticker":
                message_type, content = "sticker", {"file_key": key}
            else:
                message_type, content = "file", {"file_key": key, "file_name": media.ref}
        else:
            text = item.text
            if item.mentions_bot:
                text = "@_user_1 " + text
                mentions.append(
                    {
                        "key": "@_user_1",
                        "id": {"open_id": self.bot_open_id, "union_id": "on_bot"},
                        "name": BOT_NAME,
                        "tenant_key": "tenant1",
                    }
                )
            message_type, content = "text", {"text": text}
        message: dict[str, Any] = {
            "message_id": item.message_id,
            "root_id": item.reply_to or "",
            "parent_id": item.reply_to or "",
            "create_time": str(int(item.at.timestamp() * 1000)),
            "chat_id": item.chat_id,
            "chat_type": "p2p" if item.chat_kind == "dm" else "group",
            "message_type": message_type,
            "content": json.dumps(content),
            "mentions": mentions,
        }
        if item.thread_id:
            message["thread_id"] = item.thread_id
        body = {
            "schema": "2.0",
            "header": self._header("im.message.receive_v1"),
            "event": {
                "sender": {
                    "sender_id": {
                        "open_id": item.user_id,
                        "union_id": "on_" + item.user_id,
                        "user_id": "u_" + item.user_id,
                    },
                    "sender_type": "app" if item.is_bot else "user",
                    "tenant_key": "tenant1",
                },
                "message": message,
            },
        }
        return "event", body

    def frames_for(self, item: FakeInbound) -> list[bytes]:
        found = self.event_for(item)
        if found is None:
            return []
        kind, body = found
        return self.frames_of(kind, body)

    def frames_of(self, kind: str, body: dict[str, Any]) -> list[bytes]:
        payload = json.dumps(body).encode()
        seq = next(self._ids)
        parts = max(1, self.split_parts)
        size = -(-len(payload) // parts)
        pieces = [payload[i * size : (i + 1) * size] for i in range(parts)]
        frames: list[bytes] = []
        for index, piece in enumerate(pieces):
            headers = {
                "type": kind,
                "message_id": f"msg{seq}",
                "sum": str(parts),
                "seq": str(index),
                "trace_id": f"trace{seq}",
            }
            frames.append(_frame(1, headers, piece, seq=seq, service=SERVICE_ID))
        return frames

    def push_event(self, kind: str, body: dict[str, Any]) -> None:
        """Queue a hand-made event for the long connection."""
        self.raw_frames.extend(self.frames_of(kind, body))
        self._ready.set()

    def comment_notice(
        self,
        file_token: str,
        comment_id: str,
        reply_id: str,
        *,
        author: str = "ou_user200",
        mentioned: bool = True,
        to: str | None = None,
        notice_type: str = "add_reply",
        file_type: str = "docx",
    ) -> dict[str, Any]:
        return {
            "schema": "2.0",
            "header": self._header("drive.notice.comment_add_v1"),
            "event": {
                "comment_id": comment_id,
                "reply_id": reply_id,
                "is_mentioned": mentioned,
                "timestamp": str(int(self.clock.now().timestamp() * 1000)),
                "notice_meta": {
                    "file_token": file_token,
                    "file_type": file_type,
                    "notice_type": notice_type,
                    "from_user_id": {"open_id": author, "union_id": "on_" + author},
                    "to_user_id": {"open_id": to if to is not None else self.bot_open_id},
                },
            },
        }

    def render(self, item: FakeInbound) -> Callback | None:
        found = self.event_for(item)
        if found is None:
            return None
        return self.signed(found[1])

    def signed(
        self,
        body: dict[str, Any],
        *,
        encrypt_key: str | None = None,
        timestamp: str | None = None,
        signature: str | None = None,
        encrypted: bool = True,
    ) -> Callback:
        key = self.encrypt_key if encrypt_key is None else encrypt_key
        plain = json_body(body)
        raw = json_body({"encrypt": encrypt(key, plain)}) if encrypted else plain
        stamp = timestamp or str(int(self.clock.now().timestamp()))
        nonce = f"n{next(self._ids)}"
        headers = {
            "content-type": "application/json",
            "x-lark-request-timestamp": stamp,
            "x-lark-request-nonce": nonce,
            "x-lark-signature": signature or sign(stamp, nonce, key, raw),
        }
        return Callback(WEBHOOK_PATH, raw, headers)

    # -- REST --------------------------------------------------------------------------------------

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        self.hosts.append(request.url.host)
        path = request.url.path
        if path not in (
            "/open-apis/auth/v3/tenant_access_token/internal",
            "/callback/ws/endpoint",
        ):
            auth = request.headers.get("Authorization", "")
            token = auth.removeprefix("Bearer ")
            if not auth.startswith("Bearer ") or token not in self.tokens or token in self.retired:
                return _code(99991663, "Invalid access token for authorization.")
        return await super()._handle(request)

    def error_response(self, exc: FakePlatformError) -> httpx.Response:
        kind = exc.kind
        if kind is FailureKind.RATE_LIMIT:
            wait = exc.retry_after if exc.retry_after is not None else 5.0
            return _code(
                99991400,
                "request trigger frequency limit",
                429,
                **{"x-ogw-ratelimit-reset": str(int(wait))},
            )
        if kind is FailureKind.CHAT_NOT_FOUND:
            return _code(230001, "Your request contains an invalid request parameter: receive_id.")
        if kind is FailureKind.THREAD_NOT_FOUND:
            return _code(230011, "The message was withdrawn.")
        if kind is FailureKind.FORBIDDEN:
            return _code(230002, "Bot/User can NOT be out of the chat.")
        if kind is FailureKind.PARSE_ERROR:
            return _code(230001, "invalid message content")
        return _code(230025, "The message content reaches its length limit.")

    @api("POST", "/open-apis/auth/v3/tenant_access_token/internal")
    async def _token(self, call: ApiCall) -> httpx.Response:
        body = call.json() or {}
        await self.platform.connect()
        if body.get("app_id") != self.app_id or body.get("app_secret") != self.app_secret:
            return _code(10014, "app secret invalid", 400)
        token = "".join(["t-", "fake", str(next(self._ids)).zfill(8)])
        self.tokens.append(token)
        return httpx.Response(
            200,
            json={"code": 0, "msg": "ok", "tenant_access_token": token, "expire": self.token_ttl},
        )

    @api("GET", "/open-apis/bot/v3/info")
    async def _bot(self, call: ApiCall) -> httpx.Response:
        bot = {"open_id": self.bot_open_id, "app_name": BOT_NAME, "activate_status": 2}
        return httpx.Response(200, json={"code": 0, "msg": "ok", "bot": bot})

    @api("POST", "/callback/ws/endpoint")
    async def _endpoint(self, call: ApiCall) -> httpx.Response:
        body = call.json() or {}
        if body.get("AppID") != self.app_id or body.get("AppSecret") != self.app_secret:
            return _code(10014, "app secret invalid", 400)
        ticket = "".join(["tk", str(next(self._ids)), "x" * 12])
        url = (
            f"wss://msg-frontier.fake.test/ws/v2?device_id=d1&access_key=k1&service_id={SERVICE_ID}"
            f"&ticket={ticket}"
        )
        config = {
            "ReconnectCount": -1,
            "ReconnectInterval": 120,
            "ReconnectNonce": 30,
            "PingInterval": 120,
        }
        return _ok({"URL": url, "ClientConfig": config})

    def _decode(self, msg_type: str, content: str) -> tuple[str, tuple[Button, ...], str | None]:
        """(text, buttons, media key) of an outbound message's content."""
        data = json.loads(content)
        if msg_type == "text":
            return str(data["text"]), (), None
        if msg_type == "post":
            locale = next(iter(data.values()))
            texts: list[str] = []
            media_key = None
            for row in locale["content"]:
                for element in row:
                    if element["tag"] == "md":
                        texts.append(element["text"])
                    elif element["tag"] == "img":
                        media_key = element["image_key"]
            return "\n".join(texts), (), media_key
        if msg_type == "interactive":
            text = ""
            buttons: list[Button] = []
            for element in data.get("elements", []):
                if element.get("tag") == "markdown":
                    text = element["content"]
                elif element.get("tag") == "action":
                    for action in element["actions"]:
                        buttons.append(Button(action["text"]["content"], action["value"]["indus"]))
            return text, tuple(buttons), None
        key = data.get("image_key") or data.get("file_key")
        return "", (), key

    async def _deliver(
        self, chat_id: str, msg_type: str, content: str, *, reply_to: str | None, thread: str | None
    ) -> httpx.Response:
        text, buttons, key = self._decode(msg_type, content)
        if key is not None:
            name, _data, mime = self.files.get(key, (key, b"", ""))
            kind = (
                "photo"
                if msg_type in ("image", "post")
                else "voice"
                if msg_type == "audio"
                else "video"
                if msg_type == "media"
                else "document"
            )
            sent = await self.platform.upload(
                chat_id,
                FakeMedia(kind, name, mime, text),  # pyright: ignore[reportArgumentType]
                reply_to=reply_to,
                thread_id=thread,
            )
        else:
            sent = await self.platform.post(
                chat_id, text, reply_to=reply_to, thread_id=thread, buttons=buttons
            )
        if msg_type == "interactive":
            self.cards[sent.message_id] = json.loads(content)
        return _ok({"message_id": sent.message_id, "chat_id": chat_id, "msg_type": msg_type})

    @api("POST", "/open-apis/im/v1/messages")
    async def _create(self, call: ApiCall) -> httpx.Response:
        body = call.json()
        chat_id = str(body["receive_id"])
        return await self._deliver(
            chat_id, body["msg_type"], body["content"], reply_to=None, thread=None
        )

    @api("POST", "/open-apis/im/v1/messages/{message_id}/reply")
    async def _reply(self, call: ApiCall, message_id: str) -> httpx.Response:
        body = call.json()
        parent = self.platform.message(message_id)
        if parent is None or parent.deleted:
            return _code(230011, "The message was withdrawn.")
        thread = None
        if body.get("reply_in_thread"):
            thread = parent.thread_id or f"omt_{message_id}"
        return await self._deliver(
            parent.chat_id, body["msg_type"], body["content"], reply_to=message_id, thread=thread
        )

    @api("PUT", "/open-apis/im/v1/messages/{message_id}")
    async def _update(self, call: ApiCall, message_id: str) -> httpx.Response:
        body = call.json()
        found = self.platform.message(message_id)
        if found is None:
            return _code(230011, "The message was withdrawn.")
        text, _buttons, _key = self._decode(body["msg_type"], body["content"])
        await self.platform.edit(found.chat_id, message_id, text)
        return _ok({})

    @api("PATCH", "/open-apis/im/v1/messages/{message_id}")
    async def _patch(self, call: ApiCall, message_id: str) -> httpx.Response:
        body = call.json()
        found = self.platform.message(message_id)
        if found is None or message_id not in self.cards:
            return _code(230001, "invalid message_id: not a card")
        text, buttons, _key = self._decode("interactive", body["content"])
        self.cards[message_id] = json.loads(body["content"])
        await self.platform.edit(found.chat_id, message_id, text, buttons=buttons)
        return _ok({})

    @api("DELETE", "/open-apis/im/v1/messages/{message_id}")
    async def _delete(self, call: ApiCall, message_id: str) -> httpx.Response:
        found = self.platform.message(message_id)
        if found is None:
            return _code(230011, "The message was withdrawn.")
        await self.platform.delete(found.chat_id, message_id)
        return _ok({})

    @api("GET", "/open-apis/im/v1/messages/{message_id}")
    async def _get_message(self, call: ApiCall, message_id: str) -> httpx.Response:
        found = self.platform.message(message_id)
        if found is None:
            return _code(230011, "The message was withdrawn.")
        item = {
            "message_id": message_id,
            "chat_id": found.chat_id,
            "msg_type": "text",
            "body": {"content": json.dumps({"text": found.text})},
        }
        return _ok({"items": [item]})

    @api("GET", "/open-apis/im/v1/chats/{chat_id}")
    async def _chat(self, call: ApiCall, chat_id: str) -> httpx.Response:
        chat = await self.platform.chat(chat_id)
        mode = "p2p" if chat.kind == "dm" else "group"
        return _ok({"name": chat.name, "chat_mode": mode, "chat_type": "private"})

    def _multipart(self, call: ApiCall) -> dict[str, tuple[str, bytes, str]]:
        content_type = call.headers.get("content-type", "")
        message = BytesParser(policy=HTTP).parsebytes(
            b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + call.body
        )
        fields: dict[str, tuple[str, bytes, str]] = {}
        for part in message.iter_parts():  # pyright: ignore[reportAttributeAccessIssue]
            name = str(part.get_param("name", header="content-disposition"))
            raw = part.get_payload(decode=True)
            fields[name] = (
                str(part.get_filename() or ""),
                raw if isinstance(raw, bytes) else b"",
                part.get_content_type(),
            )
        return fields

    @api("POST", "/open-apis/im/v1/images")
    async def _image(self, call: ApiCall) -> httpx.Response:
        fields = self._multipart(call)
        name, data, _ = fields["image"]
        key = f"img_up{next(self._ids)}"
        self.files[key] = (name, data, "image/png")
        return _ok({"image_key": key})

    @api("POST", "/open-apis/im/v1/files")
    async def _file(self, call: ApiCall) -> httpx.Response:
        fields = self._multipart(call)
        name, data, _ = fields["file"]
        key = f"file_up{next(self._ids)}"
        file_type = fields["file_type"][1].decode()
        self.files[key] = (name, data, f"application/x-{file_type}")
        return _ok({"file_key": key})

    @api("GET", "/open-apis/im/v1/messages/{message_id}/resources/{file_key}")
    async def _resource(self, call: ApiCall, message_id: str, file_key: str) -> httpx.Response:
        found = self.files.get(file_key)
        if found is None:
            return _code(234003, "file not found", 404)
        _name, data, mime = found
        return httpx.Response(200, content=data, headers={"Content-Type": mime or "x/unknown"})

    @api("POST", "/open-apis/im/v1/messages/{message_id}/reactions")
    async def _react(self, call: ApiCall, message_id: str) -> httpx.Response:
        emoji = str((call.json() or {})["reaction_type"]["emoji_type"])
        reaction = f"r{next(self._ids)}"
        self.reactions.append(("add", message_id, emoji))
        return _ok({"reaction_id": reaction})

    @api("DELETE", "/open-apis/im/v1/messages/{message_id}/reactions/{reaction_id}")
    async def _unreact(self, call: ApiCall, message_id: str, reaction_id: str) -> httpx.Response:
        self.reactions.append(("remove", message_id, reaction_id))
        return _ok({})

    @api("GET", "/open-apis/contact/v3/users/{user_id}")
    async def _user(self, call: ApiCall, user_id: str) -> httpx.Response:
        name = self.user_names.get(user_id)
        if name is None:
            return _code(41050, "no user authority error", 400)
        return _ok({"user": {"open_id": user_id, "name": name}})

    # -- documents and comments --------------------------------------------------------------------

    @api("GET", "/open-apis/docx/v1/documents/{document_id}/raw_content")
    async def _raw(self, call: ApiCall, document_id: str) -> httpx.Response:
        text = self.documents.get(document_id)
        if text is None:
            return _code(1770002, "not found", 404)
        return _ok({"content": text})

    def _find_comment(self, token: str, comment_id: str) -> dict[str, Any] | None:
        return next(
            (c for c in self.comments.get(token, []) if c["comment_id"] == comment_id), None
        )

    @api("GET", "/open-apis/drive/v1/files/{file_token}/comments")
    async def _comments(self, call: ApiCall, file_token: str) -> httpx.Response:
        items = list(self.comments.get(file_token, []))
        if call.params.get("is_whole") == "true":
            items = [c for c in items if c.get("is_whole")]
        size = int(call.params.get("page_size", "50"))
        start = int(call.params.get("page_token") or 0)
        page = items[start : start + size]
        more = start + size < len(items)
        data: dict[str, Any] = {"items": page, "has_more": more}
        if more:
            data["page_token"] = str(start + size)
        return _ok(data)

    @api("GET", "/open-apis/drive/v1/files/{file_token}/comments/{comment_id}")
    async def _comment(self, call: ApiCall, file_token: str, comment_id: str) -> httpx.Response:
        found = self._find_comment(file_token, comment_id)
        if found is None:
            return _code(1069303, "comment not found", 404)
        return _ok(found)

    @api("GET", "/open-apis/drive/v1/files/{file_token}/comments/{comment_id}/replies")
    async def _replies(self, call: ApiCall, file_token: str, comment_id: str) -> httpx.Response:
        found = self._find_comment(file_token, comment_id)
        if found is None:
            return _code(1069303, "comment not found", 404)
        return _ok({"items": found["reply_list"]["replies"], "has_more": False})

    @api("POST", "/open-apis/drive/v1/files/{file_token}/comments/{comment_id}/replies")
    async def _comment_reply(
        self, call: ApiCall, file_token: str, comment_id: str
    ) -> httpx.Response:
        if comment_id in self.reply_not_allowed:
            return _code(1069302, "reply not allowed", 400)
        found = self._find_comment(file_token, comment_id)
        if found is None:
            return _code(1069303, "comment not found", 404)
        body = call.json()
        text = body["content"]["elements"][0]["text_run"]["text"]
        reply_id = f"rp{next(self._ids)}"
        found["reply_list"]["replies"].append(
            {
                "reply_id": reply_id,
                "user_id": self.bot_open_id,
                "content": body["content"],
            }
        )
        self.comment_posts.append(("reply", comment_id, text))
        return _ok({"reply_id": reply_id})

    @api("POST", "/open-apis/drive/v1/files/{file_token}/comments")
    async def _comment_add(self, call: ApiCall, file_token: str) -> httpx.Response:
        body = call.json()
        reply = body["reply_list"]["replies"][0]
        text = reply["content"]["elements"][0]["text_run"]["text"]
        comment_id = str(7000 + next(self._ids))
        self.comments.setdefault(file_token, []).append(
            {
                "comment_id": comment_id,
                "is_whole": True,
                "quote": "",
                "reply_list": {
                    "replies": [{"reply_id": f"rp{comment_id}", "content": reply["content"]}]
                },
            }
        )
        self.comment_posts.append(("add", comment_id, text))
        return _ok({"comment_id": comment_id})


def comment(
    comment_id: str,
    *replies: tuple[str, str, str],
    quote: str = "",
    whole: bool = False,
) -> dict[str, Any]:
    """A stored comment: ``replies`` are (reply id, author open id, text); a text containing
    ``@bot`` puts a person element for the bot there."""
    rows: list[dict[str, Any]] = []
    for reply_id, author, text in replies:
        elements: list[dict[str, Any]] = []
        for n, piece in enumerate(text.split("@bot")):
            if n:
                elements.append({"type": "person", "person": {"user_id": BOT_OPEN_ID}})
            if piece:
                elements.append({"type": "text_run", "text_run": {"text": piece}})
        rows.append({"reply_id": reply_id, "user_id": author, "content": {"elements": elements}})
    return {
        "comment_id": comment_id,
        "is_whole": whole,
        "quote": quote,
        "reply_list": {"replies": rows},
    }


__all__ = [
    "BOT_NAME",
    "BOT_OPEN_ID",
    "FEISHU_URL",
    "LARK_URL",
    "ROUTES",
    "SERVICE_ID",
    "FakeSocket",
    "FeishuWire",
    "comment",
    "encrypt",
    "fake_app_id",
    "fake_secret",
    "read_frame",
    "sign",
]
