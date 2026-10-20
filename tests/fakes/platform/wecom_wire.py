"""Fake WeCom for both WeCom platforms (P16-17, P16-18).

``WeComWire`` — the smart-robot WebSocket API. ``open`` stands in for ``httpx_ws.aconnect_ws``
(the platform connects when a socket opens and disconnects when it closes; an armed connect
failure raises ``httpx.ConnectTimeout``). The socket answers every request frame
``{cmd, headers.req_id, body}`` with ``{headers.req_id, errcode, errmsg}`` (no message id, as
WeCom does): ``aibot_subscribe`` (wrong bot id or secret → 40001), ``ping`` (unless muted),
``aibot_respond_msg`` (markdown, media, or ``stream`` frames: the first frame of a stream posts the
message, later ones edit it; every frame is kept in ``streams``), ``aibot_send_msg``,
``aibot_upload_media_init|chunk|finish``. Because WeCom names no message, the fake files platform
messages under the names the adapter uses: ``<req_id>`` for a proactive send, ``<req_id>:<n>`` for
the n-th markdown/media reply to one callback, the ``stream.id`` for a stream. Inbound updates
become ``aibot_msg_callback`` frames (``single`` / ``group``; voice as WeCom's transcript; a photo
with a caption as ``mixed``; media URLs on ``wecom-media.fake.test`` whose bytes the
``transport()`` serves AES-encrypted with the frame's ``aeskey``). WeCom has no threads, edits,
bot flags or buttons: those updates are dropped. Platform failures become WeCom codes: 846607 rate
limit (``errmsg`` states the wait, a test convention the adapter reads), 93006 chat, 846604 reply
anchor, 60011 forbidden, 40058 parameter, 45002 length; a connect timeout fails ``send_text``
(the frame never left) and a read timeout leaves the frame unanswered.

``WeComCallbackWire`` — the callback app over the P16-01 HTTP-callback wire base: signed,
encrypted XML callbacks into the adapter's listener (DM only; text, image, voice, video; no
documents and no captions), and the corp API (``gettoken``, ``message/send`` answering ``msgid``,
``media/get``) with WeCom's ``errcode`` bodies.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import itertools
import json
import os
from collections.abc import AsyncIterator, Mapping
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

import httpx

from tests.fakes.clock import FakeClock
from tests.fakes.platform.core import (
    FailureKind,
    FakeChatPlatform,
    FakeInbound,
    FakeMedia,
    FakeMessage,
    FakePlatformError,
)
from tests.fakes.platform.http_wire import ApiCall, Callback, HttpCallbackWire, api

if TYPE_CHECKING:
    from indus.gateway.adapters.wecom.adapter import WeComAdapter, WeComOptions

MAX_LEN = 4000
MEDIA_HOST = "wecom-media.fake.test"
BOT_NAME = "IndusBot"

# WeCom codes for each armed failure (rate limit's errmsg carries the wait).
CODES: dict[FailureKind, tuple[int, str]] = {
    FailureKind.RATE_LIMIT: (846607, "api freq out of limit, retry after {wait} s"),
    FailureKind.CHAT_NOT_FOUND: (93006, "invalid chatid"),
    FailureKind.THREAD_NOT_FOUND: (846604, "invalid req_id"),
    FailureKind.FORBIDDEN: (60011, "no privilege to access/modify contact/party/agent"),
    FailureKind.PARSE_ERROR: (40058, "invalid parameter"),
    FailureKind.TOO_LONG: (45002, "content size out of limit"),
}


def fake_bot_id() -> str:
    return "".join(["aib", "fake", "0" * 14])


def fake_secret() -> str:
    """Built at runtime: never a credential-shaped literal."""
    return "".join(["wc", "fake", "sec", "5" * 24])


def _rename(platform: FakeChatPlatform, message: FakeMessage, new_id: str) -> None:
    """File ``message`` under ``new_id`` (WeCom answers no id; the adapter names its messages)."""
    store: dict[str, FakeMessage] = platform._messages  # pyright: ignore[reportPrivateUsage]
    store.pop(message.message_id, None)
    message.message_id = new_id
    store[new_id] = message


def _errcode(exc: FakePlatformError) -> tuple[int, str]:
    code, text = CODES.get(exc.kind, (-1, exc.text))
    wait = int(exc.retry_after if exc.retry_after is not None else 5)
    return code, text.format(wait=wait)


class FakeWeComSocket:
    def __init__(self, wire: WeComWire) -> None:
        self.wire = wire
        self.closed = False

    async def receive_text(self) -> str:
        while True:
            frame = await self.wire.next_frame()
            if frame is not None:
                return frame

    async def send_text(self, data: str) -> None:
        if self.closed:
            raise ConnectionResetError("the socket is closed")
        await self.wire.on_frame(json.loads(data))

    async def close(self) -> None:
        self.closed = True


class _MediaTransport(httpx.MockTransport):
    def __init__(self, wire: WeComWire) -> None:
        super().__init__(wire.media_get)


_DROP = object()


class WeComWire:
    """The fake smart-robot service (see the module notes)."""

    def __init__(
        self,
        platform: FakeChatPlatform | None = None,
        *,
        clock: FakeClock | None = None,
        bot_id: str | None = None,
        secret: str | None = None,
    ) -> None:
        self.platform = platform or FakeChatPlatform(
            "wecom", clock=clock, max_len=MAX_LEN, bot_user_id="bot", bot_name=BOT_NAME
        )
        self.clock: FakeClock = self.platform.clock
        self.bot_id = bot_id or fake_bot_id()
        self.secret = secret or fake_secret()
        self.frames: list[dict[str, Any]] = []
        self.sockets: list[FakeWeComSocket] = []
        self.socket_attempts: list[float] = []
        self.socket_failures: list[BaseException] = []
        self.muted: set[str] = set()
        self.req_chat: dict[str, str] = {}
        self.streams: dict[str, list[tuple[str, bool]]] = {}
        self.uploads: dict[str, dict[str, Any]] = {}
        self.media: dict[str, tuple[bytes, str]] = {}
        self._replies: dict[str, int] = {}
        self._out: list[object] = []
        self._ready = asyncio.Event()
        self._ids = itertools.count(1)

    # -- wiring ------------------------------------------------------------------------------------

    def transport(self) -> httpx.AsyncBaseTransport:
        return _MediaTransport(self)

    def adapter(self, *, options: WeComOptions | None = None, **kw: Any) -> WeComAdapter:
        from indus.gateway.adapters.wecom.adapter import WeComAdapter

        kw.setdefault("clock", self.clock)
        return WeComAdapter(
            bot_id=self.bot_id,
            secret=self.secret,
            opener=self.open,
            transport=self.transport(),
            options=options,
            **kw,
        )

    @contextlib.asynccontextmanager
    async def open(self, client: httpx.AsyncClient, url: str) -> AsyncIterator[FakeWeComSocket]:
        """Stands in for ``httpx_ws.aconnect_ws(url, client)``."""
        self.socket_attempts.append(self.clock.monotonic())
        if self.socket_failures:
            raise self.socket_failures.pop(0)
        try:
            await self.platform.connect()
        except FakePlatformError as exc:
            raise httpx.ConnectTimeout(exc.text) from None
        socket = FakeWeComSocket(self)
        self.sockets.append(socket)
        self._out = [f for f in self._out if f is not _DROP]
        try:
            yield socket
        finally:
            socket.closed = True
            await self.platform.disconnect()

    def push(self, frame: Mapping[str, Any] | str) -> None:
        self._out.append(frame if isinstance(frame, str) else json.dumps(frame))
        self._ready.set()

    def drop_socket(self) -> None:
        self._out.append(_DROP)
        self._ready.set()

    def take_over(self) -> None:
        """Another connection subscribed the bot."""
        self.push(
            {
                "cmd": "aibot_event_callback",
                "headers": {"req_id": f"ev{next(self._ids)}"},
                "body": {"msgtype": "event", "event": {"eventtype": "disconnected_event"}},
            }
        )

    async def next_frame(self) -> str | None:
        while True:
            if self._out:
                frame = self._out.pop(0)
                if frame is _DROP:
                    raise ConnectionResetError("the server closed the socket")
                return str(frame)
            item = self.platform.next_inbound()
            if item is not None:
                return self._frame_of(item)
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
                return self._frame_of(getter.result())

    # -- inbound -----------------------------------------------------------------------------------

    def _store_media(self, ref: str) -> dict[str, str]:
        media_id = f"m{next(self._ids)}"
        key = os.urandom(32)
        self.media[media_id] = (f"bytes of {ref}".encode(), base64.b64encode(key).decode())
        return {"url": f"https://{MEDIA_HOST}/{media_id}", "aeskey": self.media[media_id][1]}

    def callback_body(self, item: FakeInbound) -> dict[str, Any] | None:
        if item.thread_id or item.is_bot or item.event != "message":
            return None
        group = item.chat_kind != "dm"
        body: dict[str, Any] = {
            "msgid": item.message_id,
            "aibotid": self.bot_id,
            "chattype": "group" if group else "single",
            "from": {"userid": item.user_id},
        }
        if group:
            body["chatid"] = item.chat_id
        if not item.media:
            prefix = f"@{BOT_NAME} " if group and item.mentions_bot else ""
            body.update(msgtype="text", text={"content": prefix + item.text})
            return body
        media = item.media[0]
        if media.kind in ("voice", "audio"):
            body.update(msgtype="voice", voice={"content": media.caption or "voice transcript"})
        elif media.kind == "photo" and media.caption:
            items = [
                {"msgtype": "text", "text": {"content": media.caption}},
                {"msgtype": "image", "image": self._store_media(media.ref)},
            ]
            body.update(msgtype="mixed", mixed={"msg_item": items})
        elif media.kind == "photo":
            body.update(msgtype="image", image=self._store_media(media.ref))
        elif media.kind == "video":
            body.update(msgtype="video", video=self._store_media(media.ref))
        else:
            body.update(msgtype="file", file=self._store_media(media.ref))
        return body

    def callback(self, body: Mapping[str, Any], *, chat_id: str, req_id: str | None = None) -> str:
        """Queue a raw callback; returns its ``req_id``."""
        rid = req_id or f"cb{next(self._ids)}"
        self.req_chat[rid] = chat_id
        self.push({"cmd": "aibot_msg_callback", "headers": {"req_id": rid}, "body": dict(body)})
        return rid

    def _frame_of(self, item: FakeInbound) -> str | None:
        body = self.callback_body(item)
        if body is None:
            return None
        rid = f"cb{item.update_id}x{next(self._ids)}"
        self.req_chat[rid] = item.chat_id
        return json.dumps({"cmd": "aibot_msg_callback", "headers": {"req_id": rid}, "body": body})

    # -- requests ----------------------------------------------------------------------------------

    def _answer(self, req_id: str, code: int = 0, message: str = "ok", **body: Any) -> None:
        frame: dict[str, Any] = {"headers": {"req_id": req_id}, "errcode": code, "errmsg": message}
        if body:
            frame["body"] = body
        self.push(frame)

    async def on_frame(self, frame: dict[str, Any]) -> None:
        self.frames.append(frame)
        cmd = str(frame.get("cmd") or "")
        req_id = str((frame.get("headers") or {}).get("req_id") or "")
        body: dict[str, Any] = dict(frame.get("body") or {})
        if cmd in self.muted:
            return
        if cmd == "aibot_subscribe":
            if body.get("bot_id") != self.bot_id or body.get("secret") != self.secret:
                self._answer(req_id, 40001, "invalid credential")
            else:
                self._answer(req_id)
            return
        if cmd == "ping":
            self._answer(req_id)
            return
        if cmd.startswith("aibot_upload_media"):
            self._upload(cmd, req_id, body)
            return
        try:
            if cmd == "aibot_respond_msg":
                await self._respond(req_id, body)
            elif cmd == "aibot_send_msg":
                sent = await self._post(str(body.get("chatid") or ""), body)
                _rename(self.platform, sent, req_id)
                self._answer(req_id)
            else:
                self._answer(req_id, 40058, "unknown command")
        except FakePlatformError as exc:
            if exc.kind is FailureKind.CONNECT_TIMEOUT:
                raise ConnectionResetError(exc.text) from None
            if exc.kind is FailureKind.READ_TIMEOUT:
                return
            code, message = _errcode(exc)
            self._answer(req_id, code, message)

    async def _post(self, chat_id: str, body: Mapping[str, Any]) -> FakeMessage:
        kind = str(body.get("msgtype") or "")
        if kind == "markdown":
            return await self.platform.post(chat_id, str(body["markdown"]["content"]))
        media_id = str((body.get(kind) or {}).get("media_id") or "")
        upload = self.uploads.get(media_id, {})
        kinds = {"image": "photo", "voice": "voice", "video": "video"}
        media = FakeMedia(kinds.get(kind, "document"), str(upload.get("filename") or media_id))  # pyright: ignore[reportArgumentType]
        return await self.platform.upload(chat_id, media)

    async def _respond(self, req_id: str, body: dict[str, Any]) -> None:
        chat_id = self.req_chat.get(req_id)
        if chat_id is None:
            self._answer(req_id, 846604, "invalid req_id")
            return
        if body.get("msgtype") == "stream":
            stream = dict(body.get("stream") or {})
            sid = str(stream.get("id") or "")
            content = str(stream.get("content") or "")
            frames = self.streams.setdefault(sid, [])
            if self.platform.message(sid) is None:
                sent = await self.platform.post(chat_id, content)
                _rename(self.platform, sent, sid)
            else:
                await self.platform.edit(chat_id, sid, content)
            frames.append((content, bool(stream.get("finish"))))
            self._answer(req_id)
            return
        sent = await self._post(chat_id, body)
        count = self._replies.get(req_id, 0) + 1
        self._replies[req_id] = count
        _rename(self.platform, sent, f"{req_id}:{count}")
        self._answer(req_id)

    def _upload(self, cmd: str, req_id: str, body: dict[str, Any]) -> None:
        if cmd.endswith("_init"):
            upload_id = f"up{next(self._ids)}"
            self.uploads[upload_id] = {**body, "chunks": {}}
            self._answer(req_id, upload_id=upload_id)
        elif cmd.endswith("_chunk"):
            upload = self.uploads.get(str(body.get("upload_id")))
            if upload is None:
                self._answer(req_id, 40058, "invalid upload_id")
                return
            upload["chunks"][int(body["chunk_index"])] = base64.b64decode(body["base64_data"])
            self._answer(req_id)
        else:
            upload = self.uploads.get(str(body.get("upload_id")))
            if upload is None or len(upload["chunks"]) != upload["total_chunks"]:
                self._answer(req_id, 40058, "incomplete upload")
                return
            media_id = f"media{next(self._ids)}"
            self.uploads[media_id] = upload
            self._answer(req_id, media_id=media_id)

    # -- media downloads ---------------------------------------------------------------------------

    async def media_get(self, request: httpx.Request) -> httpx.Response:
        from indus.gateway.adapters.wecom.crypto import aes_cbc_encrypt, pad

        if request.url.host != MEDIA_HOST:
            return httpx.Response(404)
        found = self.media.get(request.url.path.strip("/"))
        if found is None:
            return httpx.Response(404)
        data, aeskey = found
        key = base64.b64decode(aeskey)
        return httpx.Response(200, content=aes_cbc_encrypt(key, pad(data)))


# -- the callback app ---------------------------------------------------------------------------


def fake_corp_secret() -> str:
    return "".join(["corp", "fake", "sec", "3" * 24])


def fake_callback_token() -> str:
    return "".join(["cb", "tok", "9" * 10])


FAKE_AES_KEY = "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFE"
CORP_ID = "wwfakecorp000001"
AGENT_ID = "1000002"


class _CallbackTransport(httpx.MockTransport):
    def __init__(self, wire: WeComCallbackWire) -> None:
        super().__init__(wire._handle)  # pyright: ignore[reportPrivateUsage]
        self._wire = wire

    async def aclose(self) -> None:
        await self._wire.platform.disconnect()


class WeComCallbackWire(HttpCallbackWire):
    """The callback app's side of WeCom (see the module notes)."""

    base_url = "https://qyapi.weixin.qq.com"

    def __init__(
        self,
        platform: FakeChatPlatform,
        *,
        corp_id: str = CORP_ID,
        token: str | None = None,
        aes_key: str = FAKE_AES_KEY,
        corp_secret: str | None = None,
    ) -> None:
        super().__init__(platform)
        self.corp_id = corp_id
        self.token = token or fake_callback_token()
        self.aes_key = aes_key
        self.corp_secret = corp_secret or fake_corp_secret()
        self.access_token = "".join(["acc", "tok", "0"])
        self.token_requests = 0
        self.revoked: set[str] = set()
        self.media: dict[str, bytes] = {}
        self._ids = itertools.count(1)

    def transport(self) -> httpx.MockTransport:
        return _CallbackTransport(self)

    def crypto(self) -> Any:
        from indus.gateway.adapters.wecom.crypto import CallbackCrypto

        return CallbackCrypto(self.token, self.aes_key, self.corp_id)

    # -- REST --------------------------------------------------------------------------------------

    @api("GET", "/cgi-bin/gettoken")
    async def gettoken(self, call: ApiCall) -> httpx.Response:
        self.token_requests += 1
        if call.params.get("corpid") != self.corp_id or (
            call.params.get("corpsecret") != self.corp_secret
        ):
            return httpx.Response(200, json={"errcode": 40001, "errmsg": "invalid credential"})
        await self.platform.connect()
        self.access_token = "".join(["acc", "tok", str(next(self._ids))])
        return httpx.Response(
            200,
            json={
                "errcode": 0,
                "errmsg": "ok",
                "access_token": self.access_token,
                "expires_in": 7200,
            },
        )

    def _token_ok(self, call: ApiCall) -> bool:
        given = call.params.get("access_token", "")
        return given == self.access_token and given not in self.revoked

    @api("POST", "/cgi-bin/message/send")
    async def message_send(self, call: ApiCall) -> httpx.Response:
        if not self._token_ok(call):
            return httpx.Response(200, json={"errcode": 42001, "errmsg": "access_token expired"})
        body = call.json()
        sent = await self.platform.post(str(body["touser"]), str(body["text"]["content"]))
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok", "msgid": sent.message_id})

    @api("GET", "/cgi-bin/media/get")
    async def media_get(self, call: ApiCall) -> httpx.Response:
        if not self._token_ok(call):
            return httpx.Response(200, json={"errcode": 42001, "errmsg": "access_token expired"})
        data = self.media.get(call.params.get("media_id", ""))
        if data is None:
            return httpx.Response(200, json={"errcode": 40007, "errmsg": "invalid media_id"})
        return httpx.Response(200, content=data, headers={"Content-Type": "image/jpeg"})

    def error_response(self, exc: FakePlatformError) -> httpx.Response:
        code, message = _errcode(exc)
        # The corp API's own codes where they differ from the smart robot's.
        code = {846607: 45009, 846604: 40007, 93006: 81013}.get(code, code)
        return httpx.Response(200, json={"errcode": code, "errmsg": message})

    # -- callbacks ---------------------------------------------------------------------------------

    def inner_xml(self, item: FakeInbound) -> str | None:
        if item.thread_id or item.is_bot or item.event != "message" or item.chat_kind != "dm":
            return None
        fields: list[tuple[str, str]] = [
            ("ToUserName", self.corp_id),
            ("FromUserName", item.user_id),
            ("CreateTime", str(int(item.at.timestamp()))),
        ]
        if not item.media:
            fields += [("MsgType", "text"), ("Content", item.text)]
        else:
            media = item.media[0]
            media_id = f"mid{next(self._ids)}"
            self.media[media_id] = f"bytes of {media.ref}".encode()
            if media.kind == "photo":
                fields += [("MsgType", "image"), ("PicUrl", f"https://{MEDIA_HOST}/{media_id}")]
                fields += [("MediaId", media_id)]
            elif media.kind in ("voice", "audio"):
                fields += [("MsgType", "voice"), ("MediaId", media_id), ("Format", "amr")]
            elif media.kind == "video":
                fields += [("MsgType", "video"), ("MediaId", media_id), ("ThumbMediaId", "t")]
            else:
                return None  # callback apps receive no documents
        fields += [("MsgId", item.message_id), ("AgentID", AGENT_ID)]
        return "<xml>" + "".join(f"<{k}><![CDATA[{v}]]></{k}>" for k, v in fields) + "</xml>"

    def signed(
        self, inner: str, *, timestamp: str | None = None, nonce: str | None = None
    ) -> Callback:
        crypto = self.crypto()
        encrypted = crypto.encrypt(inner.encode("utf-8"))
        stamp = timestamp or str(int(self.platform.clock.now().timestamp()))
        once = nonce or f"n{next(self._ids)}"
        outer = (
            f"<xml><ToUserName><![CDATA[{self.corp_id}]]></ToUserName>"
            f"<Encrypt><![CDATA[{encrypted}]]></Encrypt>"
            f"<AgentID><![CDATA[{AGENT_ID}]]></AgentID></xml>"
        )
        query = urlencode(
            {
                "msg_signature": crypto.signature(stamp, once, encrypted),
                "timestamp": stamp,
                "nonce": once,
            }
        )
        return Callback(
            "/wecom/callback", outer.encode(), {"Content-Type": "text/xml"}, "POST", query
        )

    def render(self, item: FakeInbound) -> Callback | None:
        inner = self.inner_xml(item)
        return None if inner is None else self.signed(inner)


__all__ = [
    "AGENT_ID",
    "CORP_ID",
    "FAKE_AES_KEY",
    "MAX_LEN",
    "MEDIA_HOST",
    "FakeWeComSocket",
    "WeComCallbackWire",
    "WeComWire",
    "fake_bot_id",
    "fake_callback_token",
    "fake_corp_secret",
    "fake_secret",
]
