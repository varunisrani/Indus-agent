"""A fake Yuanbao bot service for the Yuanbao adapter (P16-21): HTTPS, the socket, COS, media.

HTTP runs over one ``httpx.MockTransport`` (any host) on a `FakeChatPlatform`:

- ``POST /api/v5/robotLogic/sign-token``: checks the HMAC signature itself (stdlib ``hmac``, the
  fake's own copy of the rule) and answers a runtime-built token, the bot id and a lifetime (the
  platform "connects" here); ``retry_codes`` answers 10099 that many times first;
  ``refuse_sign`` answers a refusal code;
- ``POST /api/resource/genUploadInfo``: temporary COS credentials (needs ``X-Token``);
- ``PUT https://<bucket>.cos.accelerate.myqcloud.com/<key>``: keeps the object when the
  ``Authorization`` header carries the COS signature over the request (checked with the
  product's `cos_authorization`, which has its own known-answer test) and the security token;
- ``GET /api/resource/v1/download?resourceId=``: the direct URL of an inbound media resource;
- ``GET https://media.yuanbao.tencent.com/<name>``: the media bytes.

The socket: ``open(client, url)`` (an async context manager standing in for httpx-ws) yields a
`FakeYuanbaoSocket` speaking binary ``ConnMsg`` frames. It answers ``auth-bind`` (code 0 and a
connect id for the right token; ``refuse_auth`` refuses), ``ping`` (unless ``answer_pings`` is
off), the business requests (sends become platform messages; heartbeats become typing ops; group
info and member lists come from ``groups``), and serves the platform's queued inbound traffic as
pushes that ask for an acknowledgement (``json_pushes`` sends them as JSON; ``push_answers``
answers sends with a push carrying the request's msg id). Armed failures:
connect timeout → the frame never leaves (``send_bytes`` raises), read timeout → no answer,
anything else → a response with a non-zero status and the platform's error text.
``drop_socket(code)`` closes the open socket with ``code``.

Chat ids: groups are ``group:<code>``, DMs the bare account (the platform's DM convention).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import itertools
import json
from collections.abc import AsyncIterator
from typing import Any, Literal
from urllib.parse import parse_qs, urlsplit

import httpx

from indus.gateway.adapters.yuanbao import frames
from indus.gateway.adapters.yuanbao.client import SocketClosed, cos_authorization
from indus.gateway.adapters.yuanbao.frames import Element, Push
from tests.fakes.platform.core import (
    FailureKind,
    FakeChatPlatform,
    FakeInbound,
    FakeMedia,
    FakeMessage,
    FakePlatformError,
)
from tests.fakes.platform.http_wire import ApiCall, HttpCallbackWire, api

APP_ID = "ybapp-10001"
BOT_ID = "ybbot-9000"
MAX_LEN = 4000
API_HOST = "bot.yuanbao.tencent.com"
MEDIA_HOST = "media.yuanbao.tencent.com"
BUCKET = "ybbucket-1250000000"
COS_HOST = f"{BUCKET}.cos.accelerate.myqcloud.com"
WS_URL = "wss://bot-wss.yuanbao.tencent.com/wss/connection"


def fake_secret() -> str:
    """Built at runtime: never a credential-shaped literal in the source."""
    return "".join(["yb", "-secret-", "7a8b" * 4])


def fake_token() -> str:
    return "".join(["yb", "tok", "-", "c3d4" * 3])


def _rename(platform: FakeChatPlatform, message: FakeMessage, new_id: str) -> None:
    """File ``message`` under ``new_id``: Yuanbao answers no id, the adapter names its messages
    by the request's msg id."""
    store: dict[str, FakeMessage] = platform._messages  # pyright: ignore[reportPrivateUsage]
    store.pop(message.message_id, None)
    message.message_id = new_id
    store[new_id] = message


def _sign(app_id: str, secret: str, nonce: str, timestamp: str) -> str:
    data = (nonce + timestamp + app_id + secret).encode()
    return hmac.new(secret.encode(), data, hashlib.sha256).hexdigest()


class FakeYuanbaoSocket:
    """One open bot socket."""

    def __init__(self, wire: YuanbaoWire, url: str) -> None:
        self.wire = wire
        self.url = url
        self.closed = False

    async def receive_bytes(self) -> bytes:
        frame = await self.wire.next_frame()
        if isinstance(frame, SocketClosed):
            self.closed = True
            raise frame
        return frame

    async def send_bytes(self, data: bytes) -> None:
        if self.closed:
            raise SocketClosed(1006, "closed")
        frame = frames.decode_frame(data)
        self.wire.client_frames.append(frame)
        await self.wire.on_client_frame(frame)


class YuanbaoWire(HttpCallbackWire):
    """The fake Yuanbao (see the module notes)."""

    base_url = f"https://{API_HOST}"

    def __init__(self, platform: FakeChatPlatform) -> None:
        super().__init__(platform)
        self.app_id = APP_ID
        self.secret = fake_secret()
        self.token = fake_token()
        self.bot_id = BOT_ID
        self.lifetime = 7200
        self.signs: list[dict[str, Any]] = []
        self.sign_headers: list[dict[str, str]] = []
        self.retry_codes = 0
        self.refuse_sign = False
        self.refuse_auth = False
        self.answer_pings = True
        self.json_pushes = False
        self.push_answers = False
        self.client_frames: list[frames.Frame] = []
        self.auth_requests: list[frames.Fields] = []
        self.pings = 0
        self.acks: list[str] = []
        self.sockets: list[FakeYuanbaoSocket] = []
        self.socket_failures: list[BaseException] = []
        self.sends: list[dict[str, Any]] = []
        self.heartbeats: list[tuple[str, int]] = []
        self.groups: dict[str, dict[str, Any]] = {}
        self.objects: dict[str, bytes] = {}
        self.object_headers: dict[str, dict[str, str]] = {}
        self.media_files: dict[str, bytes] = {}
        self.resources: dict[str, str] = {}
        self.uploads = 0
        self._ids = itertools.count(1)
        self._frames: list[bytes | SocketClosed] = []
        self._ready = asyncio.Event()

    # -- wiring ---------------------------------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return _Transport(self)

    @contextlib.asynccontextmanager
    async def open(self, client: httpx.AsyncClient, url: str) -> AsyncIterator[FakeYuanbaoSocket]:
        """The adapter's socket opener (stands in for ``httpx_ws_opener``)."""
        if self.socket_failures:
            raise self.socket_failures.pop(0)
        socket = FakeYuanbaoSocket(self, url)
        self.sockets.append(socket)
        try:
            yield socket
        finally:
            socket.closed = True

    def error_response(self, exc: FakePlatformError) -> httpx.Response:
        return httpx.Response(500, json={"code": 50000, "msg": exc.text})

    # -- frames to the adapter -------------------------------------------------------------------

    def push_frame(self, data: bytes) -> None:
        self._frames.append(data)
        self._ready.set()

    def respond(self, request: frames.Frame, data: bytes = b"", *, status: int = 0) -> None:
        kind = frames.RESPONSE
        if self.push_answers and request.head.cmd.startswith("send_"):
            kind = frames.PUSH  # some answers come back as a push with the request's msg id
        head = frames.Head(
            kind,
            request.head.cmd,
            request.head.seq_no,
            request.head.msg_id,
            request.head.module,
            status=status,
        )
        self.push_frame(frames.encode_frame(head, data))

    def drop_socket(self, code: int | None = None) -> None:
        self._frames.append(SocketClosed(code, "dropped by the fake"))
        self._ready.set()

    def kick(self) -> None:
        head = frames.Head(frames.PUSH, frames.KICKOUT, 0, "kick-1", frames.CONN_MODULE)
        self.push_frame(frames.encode_frame(head))

    async def next_frame(self) -> bytes | SocketClosed:
        while True:
            if self._frames:
                return self._frames.pop(0)
            item = self.platform.next_inbound()
            if item is not None:
                self._render(item)
                continue
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
                self._render(getter.result())

    def send_push(self, push: Push, *, need_ack: bool = True) -> str:
        """Queue one push; its frame msg id."""
        msg_id = f"push-{next(self._ids)}"
        if self.json_pushes:
            body = {
                "from_account": push.from_account,
                "sender_nickname": push.sender_nickname,
                "group_code": push.group_code,
                "group_name": push.group_name,
                "msg_id": push.msg_id,
                "msg_time": push.msg_time,
                "msg_body": [e.as_json() for e in push.elements],
                "callback_command": push.callback_command,
            }
            data = json.dumps(body, ensure_ascii=False).encode()
        else:
            data = frames.push_bytes(push)
        head = frames.Head(
            frames.PUSH, "inbound_message", 0, msg_id, frames.BIZ_MODULE, need_ack=need_ack
        )
        self.push_frame(frames.encode_frame(head, data))
        return msg_id

    def _media_elements(self, item: FakeInbound) -> list[Element]:
        out: list[Element] = []
        for media in item.media:
            rid = f"rid{next(self._ids)}"
            direct = f"https://{MEDIA_HOST}/{media.ref}"
            self.resources[rid] = direct
            self.media_files[media.ref] = f"bytes of {media.ref}".encode()
            url = f"https://{API_HOST}/api/resource/download?resourceId={rid}"
            if media.kind == "photo":
                infos = [{"type": 1, "url": url}, {"type": 2, "url": url}]
                out.append(Element("TIMImageElem", {"image_info_array": infos}))
            elif media.kind == "voice":
                out.append(Element("TIMSoundElem", {"url": url}))
            elif media.kind == "video":
                out.append(Element("TIMVideoFileElem", {"url": url}))
            else:
                out.append(Element("TIMFileElem", {"url": url, "file_name": media.ref}))
            if media.caption:
                out.append(frames.text_element(media.caption))
        return out

    def push_for(self, item: FakeInbound) -> Push | None:
        """The push Yuanbao would send for ``item`` (None: no such shape on Yuanbao)."""
        if item.event != "message" or item.thread_id or item.is_bot:
            return None
        elements: list[Element] = []
        group = ""
        if item.chat_id.startswith("group:"):
            group = item.chat_id[len("group:") :]
            if item.mentions_bot:
                mention = {"elem_type": 1002, "text": "@IndusBot", "user_id": self.bot_id}
                elements.append(frames.custom_element(mention))
        if item.media:
            elements += self._media_elements(item)
        elif item.text:
            elements.append(frames.text_element(item.text))
        return Push(
            from_account=item.user_id,
            sender_nickname=item.user_name,
            group_code=group,
            group_name=item.chat_name if group else "",
            msg_id=item.message_id,
            msg_time=int(item.at.timestamp()),
            elements=tuple(elements),
        )

    def _render(self, item: FakeInbound) -> None:
        push = self.push_for(item)
        if push is not None:
            self.send_push(push)

    # -- frames from the adapter -----------------------------------------------------------------

    async def on_client_frame(self, frame: frames.Frame) -> None:
        head = frame.head
        if head.cmd_type == frames.PUSH_ACK:
            self.acks.append(head.msg_id)
            return
        if head.cmd == frames.AUTH_BIND:
            request = frames.Fields.parse(frame.data)
            self.auth_requests.append(request)
            token = request.sub(2).text(3)
            if self.refuse_auth or token != self.token:
                answer = frames.Writer().num(1, 41001).text(2, "token invalid").build()
            else:
                answer = frames.Writer().text(3, f"conn-{len(self.sockets)}").build()
            self.respond(frame, answer)
            return
        if head.cmd == frames.PING:
            self.pings += 1
            if self.answer_pings:
                self.respond(frame)
            return
        handler = {
            "send_c2c_message": self._on_send,
            "send_group_message": self._on_send,
            "send_private_heartbeat": self._on_heartbeat,
            "send_group_heartbeat": self._on_heartbeat,
            "query_group_info": self._on_group_info,
            "get_group_member_list": self._on_members,
        }.get(head.cmd)
        if handler is not None:
            await handler(frame)

    async def _on_send(self, frame: frames.Frame) -> None:
        fields = frames.Fields.parse(frame.data)
        group = frame.head.cmd == "send_group_message"
        body_field = 6 if group else 5
        elements = [frames.decode_element(raw) for raw in fields.raws(body_field)]
        record: dict[str, Any] = {
            "cmd": frame.head.cmd,
            "module": frame.head.module,
            "msg_id": fields.text(1),
            "to": fields.text(2),
            "from": fields.text(3),
            "elements": elements,
            "quote": fields.text(7) if group else "",
            "group_code": "" if group else fields.text(6),
        }
        self.sends.append(record)
        chat = f"group:{fields.text(2)}" if group else fields.text(2)
        text = " ".join(
            str(e.content.get("text") or "")
            if e.kind == "TIMTextElem"
            else str(json.loads(str(e.content.get("data") or "{}")).get("text") or "")
            for e in elements
            if e.kind in ("TIMTextElem", "TIMCustomElem")
        )
        media = [e for e in elements if e.kind in ("TIMImageElem", "TIMFileElem", "TIMFaceElem")]
        try:
            if media:
                first = media[0]
                kind: Literal["photo", "sticker", "document"] = "document"
                if first.kind == "TIMImageElem":
                    kind = "photo"
                elif first.kind == "TIMFaceElem":
                    kind = "sticker"
                ref = str(first.content.get("file_name") or first.content.get("data") or "img")
                sent = await self.platform.upload(
                    chat, FakeMedia(kind, ref, caption=text), reply_to=record["quote"] or None
                )
            else:
                sent = await self.platform.post(chat, text, reply_to=record["quote"] or None)
        except FakePlatformError as exc:
            if exc.kind is FailureKind.CONNECT_TIMEOUT:
                self.sends.pop()
                raise ConnectionError("the frame never reached Yuanbao") from None
            if exc.kind is FailureKind.READ_TIMEOUT:
                return  # no answer ever comes
            answer = frames.Writer().num(1, 40001).text(2, exc.text).build()
            self.respond(frame, answer, status=1)
            return
        _rename(self.platform, sent, record["msg_id"])
        self.respond(frame, frames.Writer().num(1, 0).build())

    async def _on_heartbeat(self, frame: frames.Frame) -> None:
        fields = frames.Fields.parse(frame.data)
        if frame.head.cmd == "send_group_heartbeat":
            chat, value = f"group:{fields.text(3)}", fields.num(5)
        else:
            chat, value = fields.text(2), fields.num(3)
        self.heartbeats.append((chat, value))
        if value == frames.RUNNING:
            await self.platform.typing(chat)
        else:
            await self.platform.typing_stop(chat)
        self.respond(frame)

    async def _on_group_info(self, frame: frames.Frame) -> None:
        code = frames.Fields.parse(frame.data).text(1)
        group = self.groups.get(code)
        if group is None:
            answer = frames.Writer().num(1, 40400).text(2, "group not found").build()
            self.respond(frame, answer, status=1)
            return
        info = (
            frames.Writer()
            .text(1, group.get("name", ""))
            .text(2, group.get("owner_id", ""))
            .text(3, group.get("owner_nickname", ""))
            .num(4, len(group.get("members", [])))
        )
        self.respond(frame, frames.Writer().sub(3, info).build())

    async def _on_members(self, frame: frames.Frame) -> None:
        fields = frames.Fields.parse(frame.data)
        group = self.groups.get(fields.text(1))
        if group is None:
            answer = frames.Writer().num(1, 40400).text(2, "group not found").build()
            self.respond(frame, answer, status=1)
            return
        offset, limit = fields.num(2), fields.num(3) or 200
        members: list[dict[str, Any]] = list(group.get("members", []))
        page = members[offset : offset + limit]
        out = frames.Writer()
        for member in page:
            out.sub(
                3,
                frames.Writer()
                .text(1, member["user_id"])
                .text(2, member.get("nickname", ""))
                .num(3, member.get("role", 0))
                .text(5, member.get("name_card", "")),
            )
        end = offset + len(page)
        out.num(4, end if end < len(members) else 0).num(5, 1 if end >= len(members) else 0)
        self.respond(frame, out.build())

    # -- HTTP -----------------------------------------------------------------------------------

    @api("POST", "/api/v5/robotLogic/sign-token")
    async def sign_token(self, call: ApiCall) -> httpx.Response:
        body = call.json() or {}
        self.signs.append(body)
        self.sign_headers.append(call.headers)
        expected = _sign(
            self.app_id, self.secret, str(body.get("nonce")), str(body.get("timestamp"))
        )
        if body.get("app_key") != self.app_id or body.get("signature") != expected:
            return httpx.Response(200, json={"code": 40101, "msg": "signature mismatch"})
        if self.refuse_sign:
            return httpx.Response(200, json={"code": 40102, "msg": "app disabled"})
        await self.platform.connect()
        if self.retry_codes > 0:
            self.retry_codes -= 1
            return httpx.Response(200, json={"code": 10099, "msg": "busy, try again"})
        data = {
            "token": self.token,
            "bot_id": self.bot_id,
            "duration": self.lifetime,
            "source": "bot",
            "product": "yuanbao",
        }
        return httpx.Response(200, json={"code": 0, "data": data})

    def _authorized(self, call: ApiCall) -> bool:
        return call.headers.get("x-token") == self.token and call.headers.get("x-id") == BOT_ID

    @api("POST", "/api/resource/genUploadInfo")
    async def upload_info(self, call: ApiCall) -> httpx.Response:
        if not self._authorized(call):
            return httpx.Response(401, json={"code": 401, "msg": "bad token"})
        body = call.json() or {}
        self.uploads += 1
        key = f"/bot/{body.get('fileId')}/{body.get('fileName')}"
        data = {
            "bucketName": BUCKET,
            "region": "ap-guangzhou",
            "location": key,
            "encryptTmpSecretId": "".join(["AKID", "fake", "0" * 8]),
            "encryptTmpSecretKey": "".join(["tmp", "key", "1" * 8]),
            "encryptToken": "".join(["sess", "tok", "2" * 8]),
            "startTime": 1767250000,
            "expiredTime": 1767253600,
            "resourceUrl": f"https://{API_HOST}/api/resource/download?resourceId=up{self.uploads}",
        }
        return httpx.Response(200, json={"code": 0, "data": data})

    @api("GET", "/api/resource/v1/download")
    async def resource(self, call: ApiCall) -> httpx.Response:
        if not self._authorized(call):
            return httpx.Response(401, json={"code": 401, "msg": "bad token"})
        rid = call.params.get("resourceId", "")
        url = self.resources.get(rid)
        if url is None:
            return httpx.Response(200, json={"code": 40400, "msg": "no such resource"})
        return httpx.Response(200, json={"code": 0, "data": {"url": url}})

    async def media(self, request: httpx.Request) -> httpx.Response:
        if "x-token" in {k.lower() for k in request.headers}:
            return httpx.Response(400, text="no credentials on media hosts")
        name = request.url.path.lstrip("/")
        if name not in self.media_files:
            return httpx.Response(404)
        return httpx.Response(200, content=self.media_files[name])

    async def cos_put(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        headers = {k.lower(): v for k, v in request.headers.items()}
        auth = headers.get("authorization", "")
        fields = dict(part.split("=", 1) for part in auth.split("&") if "=" in part)
        start, _, end = fields.get("q-sign-time", "0;0").partition(";")
        expected = cos_authorization(
            method="put",
            path=request.url.raw_path.decode().split("?")[0],
            headers={
                "host": request.url.host,
                "content-type": headers.get("content-type", ""),
                "x-cos-security-token": headers.get("x-cos-security-token", ""),
            },
            secret_id="".join(["AKID", "fake", "0" * 8]),
            secret_key="".join(["tmp", "key", "1" * 8]),
            start=int(start),
            end=int(end),
        )
        if auth != expected:
            return httpx.Response(403, text="SignatureDoesNotMatch")
        key = request.url.path
        self.objects[key] = body
        self.object_headers[key] = headers
        return httpx.Response(200)


class _Transport(httpx.MockTransport):
    def __init__(self, wire: YuanbaoWire) -> None:
        super().__init__(self._route)
        self._wire = wire

    async def _route(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == MEDIA_HOST:
            return await self._wire.media(request)
        if host == COS_HOST and request.method == "PUT":
            return await self._wire.cos_put(request)
        return await self._wire._handle(request)  # pyright: ignore[reportPrivateUsage]

    async def aclose(self) -> None:
        await self._wire.platform.disconnect()


def group_member(user_id: str, nickname: str, role: int = 0) -> dict[str, Any]:
    return {"user_id": user_id, "nickname": nickname, "role": role}


def query_of(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


__all__ = [
    "API_HOST",
    "APP_ID",
    "BOT_ID",
    "BUCKET",
    "COS_HOST",
    "MAX_LEN",
    "MEDIA_HOST",
    "WS_URL",
    "FakeYuanbaoSocket",
    "YuanbaoWire",
    "fake_secret",
    "fake_token",
    "group_member",
    "query_of",
]
