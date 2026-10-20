"""A fake DingTalk: the OpenAPI and session webhooks behind ``httpx.MockTransport`` plus a fake
Stream Mode socket (P16-16).

- ``POST /v1.0/oauth2/accessToken`` (the platform connects here; wrong app credentials → 400),
  ``POST /v1.0/gateway/connections/open`` (an ``endpoint`` and a fresh ``ticket``; ``open_refusals``
  queue error statuses), ``POST /v1.0/robot/groupMessages/send`` and ``oToMessages/batchSend``
  (``processQueryKey`` = the platform message id), ``POST /v1.0/robot/messageFiles/download``
  (a ``downloadUrl`` on ``files.dingtalk-fake.test`` whose GET returns the bytes), and the session
  webhook ``POST https://oapi.dingtalk.com/robot/sendBySession?session=<s>`` (``errcode`` 0, or
  300001 for an unknown or revoked session).
- ``open`` stands in for ``httpx_ws.aconnect_ws``: it sends the upgrade request through the
  adapter's client (the ticket must arrive, once), then the socket delivers one ``CALLBACK``
  frame per inbound update (each with a session webhook valid for 90 minutes) and records every
  ack the adapter sends. ``push_frame`` queues raw frames (``SYSTEM`` ping / disconnect),
  ``drop_socket`` closes the current one, ``socket_failures`` make the next opens fail.
- DingTalk has no threads, no edit events, no bot flag and no buttons: such updates are dropped.
- Platform failures become the OpenAPI's answers: 429 ``Throttling.Api`` with ``Retry-After``, 404
  ``resource.not.found`` / ``message.not.found``, 403 ``Forbidden.AccessDenied``, 400
  ``invalidParameter.msgParam`` / ``invalidParameter.content.length``; connect and read timeouts
  raise httpx's own exceptions.
- Every request URL the transport saw (secrets included: this is the far side) is in ``urls``.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
from collections.abc import AsyncIterator, Mapping
from typing import TYPE_CHECKING, Any

import httpx

from tests.fakes.clock import FakeClock
from tests.fakes.platform.core import (
    FailureKind,
    FakeChatPlatform,
    FakeInbound,
    FakePlatformError,
)

if TYPE_CHECKING:
    from indus.gateway.adapters.dingtalk.adapter import DingTalkAdapter, DingTalkOptions

API = "https://api.dingtalk.com"
WEBHOOK_URL = "https://oapi.dingtalk.com/robot/sendBySession"
ENDPOINT = "wss://wss-open-connection.dingtalk.com:443/connect"
FILES_HOST = "files.dingtalk-fake.test"
MAX_LEN = 20_000
WEBHOOK_LIFETIME_MS = 90 * 60 * 1000


def fake_client_id() -> str:
    return "".join(["ding", "app", "0" * 12])


def fake_secret() -> str:
    """Built at runtime: never a credential-shaped literal."""
    return "".join(["dt", "fake", "sec", "7" * 24])


def _error(status: int, code: str, message: str, **headers: str) -> httpx.Response:
    return httpx.Response(
        status, json={"code": code, "message": message, "requestid": "req1"}, headers=headers
    )


def _failure(exc: FakePlatformError, request: httpx.Request) -> httpx.Response:
    kind = exc.kind
    if kind is FailureKind.CONNECT_TIMEOUT:
        raise httpx.ConnectTimeout(exc.text, request=request)
    if kind is FailureKind.READ_TIMEOUT:
        raise httpx.ReadTimeout(exc.text, request=request)
    if kind is FailureKind.RATE_LIMIT:
        wait = exc.retry_after if exc.retry_after is not None else 5.0
        return _error(429, "Throttling.Api", "Too many requests", **{"Retry-After": str(int(wait))})
    if kind is FailureKind.CHAT_NOT_FOUND:
        return _error(404, "resource.not.found", "The conversation does not exist")
    if kind is FailureKind.THREAD_NOT_FOUND:
        return _error(404, "message.not.found", "The message does not exist")
    if kind is FailureKind.FORBIDDEN:
        return _error(
            403, "Forbidden.AccessDenied.RobotNotInGroup", "The robot is not in the group"
        )
    if kind is FailureKind.PARSE_ERROR:
        return _error(400, "invalidParameter.msgParam", "msgParam is invalid")
    return _error(400, "invalidParameter.content.length", "The content exceeds the limit")


class FakeStreamSocket:
    """One open Stream Mode socket."""

    def __init__(self, wire: DingTalkWire) -> None:
        self.wire = wire
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def receive_text(self) -> str:
        while True:
            frame = await self.wire.next_frame()
            if frame is not None:
                return frame

    async def send_text(self, data: str) -> None:
        self.sent.append(json.loads(data))
        self.wire.acks.append(json.loads(data))

    async def close(self) -> None:
        self.closed = True


class _Transport(httpx.MockTransport):
    def __init__(self, wire: DingTalkWire) -> None:
        super().__init__(wire.handle)
        self._wire = wire

    async def aclose(self) -> None:
        await self._wire.platform.disconnect()


_DROP = object()


class DingTalkWire:
    """The fake service (see the module notes)."""

    def __init__(
        self,
        platform: FakeChatPlatform | None = None,
        *,
        clock: FakeClock | None = None,
        client_id: str | None = None,
        secret: str | None = None,
    ) -> None:
        self.platform = platform or FakeChatPlatform(
            "dingtalk", clock=clock, max_len=MAX_LEN, bot_user_id="robot", bot_name="IndusBot"
        )
        self.clock: FakeClock = self.platform.clock
        self.client_id = client_id or fake_client_id()
        self.secret = secret or fake_secret()
        self.token = "".join(["dt", "tok", str(id(self) % 997)])
        self.urls: list[str] = []
        self.requests: list[tuple[str, str]] = []
        self.bodies: list[tuple[str, dict[str, Any]]] = []
        self.acks: list[dict[str, Any]] = []
        self.sessions: dict[str, str] = {}
        self.tickets: list[str] = []
        self.used_tickets: set[str] = set()
        self.open_refusals: list[int] = []
        self.socket_failures: list[BaseException] = []
        self.sockets: list[FakeStreamSocket] = []
        self.socket_urls: list[str] = []
        self.socket_attempts: list[float] = []
        self.files: dict[str, bytes] = {}
        self.webhook_lifetime_ms = WEBHOOK_LIFETIME_MS
        self._frames: list[object] = []
        self._ready = asyncio.Event()
        self._ids = itertools.count(1)

    # -- wiring ------------------------------------------------------------------------------------

    def transport(self) -> httpx.AsyncBaseTransport:
        return _Transport(self)

    def adapter(self, *, options: DingTalkOptions | None = None, **kw: Any) -> DingTalkAdapter:
        from indus.gateway.adapters.dingtalk.adapter import DingTalkAdapter

        kw.setdefault("clock", self.clock)
        return DingTalkAdapter(
            client_id=self.client_id,
            client_secret=self.secret,
            transport=self.transport(),
            opener=self.open,
            options=options,
            **kw,
        )

    @contextlib.asynccontextmanager
    async def open(self, client: httpx.AsyncClient, url: str) -> AsyncIterator[FakeStreamSocket]:
        """Stands in for ``httpx_ws.aconnect_ws(url, client)``."""
        self.socket_urls.append(url)
        self.socket_attempts.append(self.clock.monotonic())
        if self.socket_failures:
            raise self.socket_failures.pop(0)
        # The upgrade is an HTTP request on the adapter's client, as with httpx-ws: the ticket is
        # added by the adapter's transport and checked here (one use each).
        upgrade = await client.get(url)
        if upgrade.status_code != 101:
            raise ConnectionRefusedError(f"upgrade refused (HTTP {upgrade.status_code})")
        socket = FakeStreamSocket(self)
        self.sockets.append(socket)
        try:
            yield socket
        finally:
            socket.closed = True

    def push_frame(self, frame: Mapping[str, Any] | str) -> None:
        self._frames.append(frame if isinstance(frame, str) else json.dumps(frame))
        self._ready.set()

    def system(self, topic: str, data: Mapping[str, Any] | None = None) -> None:
        """Queue a ``SYSTEM`` frame (``ping`` carries ``{"opaque": ...}``)."""
        self.push_frame(
            {
                "specVersion": "1.0",
                "type": "SYSTEM",
                "headers": {"topic": topic, "messageId": f"sys{next(self._ids)}"},
                "data": json.dumps(dict(data or {})),
            }
        )

    def callback(self, payload: Mapping[str, Any], *, message_id: str | None = None) -> None:
        """Queue a robot ``CALLBACK`` frame around ``payload``."""
        self.push_frame(self._callback_frame(dict(payload), message_id))

    def drop_socket(self) -> None:
        self._frames.append(_DROP)
        self._ready.set()

    async def next_frame(self) -> str | None:
        while True:
            if self._frames:
                frame = self._frames.pop(0)
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

    def _callback_frame(self, payload: dict[str, Any], message_id: str | None) -> str:
        return json.dumps(
            {
                "specVersion": "1.0",
                "type": "CALLBACK",
                "headers": {
                    "topic": "/v1.0/im/bot/messages/get",
                    "messageId": message_id or f"frame{next(self._ids)}",
                    "contentType": "application/json",
                    "time": str(int(self.clock.now().timestamp() * 1000)),
                },
                "data": json.dumps(payload),
            }
        )

    def new_session(self, chat_id: str) -> str:
        session = f"s{next(self._ids)}x{abs(hash(chat_id)) % 9973}"
        self.sessions[session] = chat_id
        return session

    def payload(self, item: FakeInbound) -> dict[str, Any] | None:
        """The robot message DingTalk would send for ``item`` (None: no such thing exists)."""
        if item.thread_id or item.is_bot or item.event != "message":
            return None
        now_ms = int(self.clock.now().timestamp() * 1000)
        session = self.new_session(item.chat_id)
        data: dict[str, Any] = {
            "conversationId": item.chat_id,
            "conversationType": "1" if item.chat_kind == "dm" else "2",
            "conversationTitle": item.chat_name,
            "msgId": item.message_id,
            "senderId": f"$:LWCP_v1:${item.user_id}",
            "senderStaffId": item.user_id,
            "senderNick": item.user_name or item.user_id,
            "isInAtList": item.mentions_bot,
            "chatbotUserId": self.platform.bot_user_id,
            "robotCode": self.client_id,
            "createAt": int(item.at.timestamp() * 1000),
            "sessionWebhook": f"{WEBHOOK_URL}?session={session}",
            "sessionWebhookExpiredTime": now_ms + self.webhook_lifetime_ms,
        }
        if not item.media:
            data["msgtype"] = "text"
            data["text"] = {"content": item.text}
            return data
        media = item.media[0]
        code = f"code{next(self._ids)}"
        self.files[code] = f"bytes of {media.ref}".encode()
        if media.kind == "photo" and media.caption:
            data["msgtype"] = "richText"
            data["content"] = {
                "richText": [{"text": media.caption}, {"type": "picture", "downloadCode": code}]
            }
        elif media.kind == "photo":
            data["msgtype"] = "picture"
            data["content"] = {"downloadCode": code}
        elif media.kind in ("voice", "audio"):
            data["msgtype"] = "audio"
            data["content"] = {"downloadCode": code, "duration": 3000, "recognition": media.caption}
        elif media.kind == "video":
            data["msgtype"] = "video"
            data["content"] = {"downloadCode": code, "videoType": "mp4"}
        else:
            data["msgtype"] = "file"
            data["content"] = {"downloadCode": code, "fileName": media.ref}
        return data

    def _frame_of(self, item: FakeInbound) -> str | None:
        payload = self.payload(item)
        if payload is None:
            return None
        return self._callback_frame(payload, None)

    # -- HTTP ------------------------------------------------------------------------------------

    def _json(self, request: httpx.Request) -> dict[str, Any]:
        body = json.loads(request.content or b"{}")
        self.bodies.append((request.url.path, body))
        return body

    def _authorised(self, request: httpx.Request) -> bool:
        return request.headers.get("x-acs-dingtalk-access-token") == self.token

    async def handle(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        self.urls.append(str(request.url))
        self.requests.append((request.method, f"{request.url.host}{request.url.path}"))
        host, path = request.url.host, request.url.path
        if host == "oapi.dingtalk.com" and path == "/robot/sendBySession":
            return await self._webhook(request)
        if host == "wss-open-connection.dingtalk.com" and path == "/connect":
            ticket = request.url.params.get("ticket", "")
            if ticket not in self.tickets or ticket in self.used_tickets:
                return httpx.Response(401)
            self.used_tickets.add(ticket)
            return httpx.Response(101)
        if host == FILES_HOST:
            data = self.files.get(path.rsplit("/", 1)[-1])
            if data is None:
                return httpx.Response(404)
            return httpx.Response(200, content=data, headers={"Content-Type": "audio/amr"})
        if host != "api.dingtalk.com":
            return httpx.Response(404)
        routes = {
            "/v1.0/oauth2/accessToken": self._token,
            "/v1.0/gateway/connections/open": self._open_connection,
            "/v1.0/robot/groupMessages/send": self._group_send,
            "/v1.0/robot/oToMessages/batchSend": self._dm_send,
            "/v1.0/robot/messageFiles/download": self._download,
        }
        route = routes.get(path)
        if route is None or request.method != "POST":
            return _error(404, "resource.not.found", "no such API")
        return await route(request)

    async def _token(self, request: httpx.Request) -> httpx.Response:
        body = self._json(request)
        if body.get("appKey") != self.client_id or body.get("appSecret") != self.secret:
            return _error(400, "invalidClientIdOrSecret", "invalid client id or secret")
        try:
            await self.platform.connect()
        except FakePlatformError as exc:
            return _failure(exc, request)
        return httpx.Response(200, json={"accessToken": self.token, "expireIn": 7200})

    async def _open_connection(self, request: httpx.Request) -> httpx.Response:
        body = self._json(request)
        if self.open_refusals:
            status = self.open_refusals.pop(0)
            return _error(status, "Forbidden.AccessDenied", "refused")
        if body.get("clientId") != self.client_id or body.get("clientSecret") != self.secret:
            return _error(401, "InvalidAuthentication", "invalid credentials")
        ticket = f"tk{next(self._ids)}"
        self.tickets.append(ticket)
        return httpx.Response(200, json={"endpoint": ENDPOINT, "ticket": ticket})

    async def _post(self, request: httpx.Request, chat_id: str) -> httpx.Response:
        body = self.bodies[-1][1]
        param = json.loads(body.get("msgParam") or "{}")
        try:
            sent = await self.platform.post(chat_id, str(param.get("text") or ""))
        except FakePlatformError as exc:
            return _failure(exc, request)
        return httpx.Response(200, json={"processQueryKey": sent.message_id})

    async def _group_send(self, request: httpx.Request) -> httpx.Response:
        body = self._json(request)
        if not self._authorised(request):
            return _error(401, "InvalidAuthentication", "invalid access token")
        return await self._post(request, str(body.get("openConversationId") or ""))

    async def _dm_send(self, request: httpx.Request) -> httpx.Response:
        body = self._json(request)
        if not self._authorised(request):
            return _error(401, "InvalidAuthentication", "invalid access token")
        users = body.get("userIds") or [""]
        return await self._post(request, str(users[0]))

    async def _download(self, request: httpx.Request) -> httpx.Response:
        body = self._json(request)
        if not self._authorised(request):
            return _error(401, "InvalidAuthentication", "invalid access token")
        code = str(body.get("downloadCode") or "")
        if code not in self.files:
            return _error(404, "resource.not.found", "unknown download code")
        return httpx.Response(200, json={"downloadUrl": f"https://{FILES_HOST}/d/{code}"})

    async def _webhook(self, request: httpx.Request) -> httpx.Response:
        body = self._json(request)
        chat_id = self.sessions.get(request.url.params.get("session", ""))
        if chat_id is None:
            return httpx.Response(200, json={"errcode": 300001, "errmsg": "session expired"})
        text = str((body.get("markdown") or {}).get("text") or "")
        try:
            await self.platform.post(chat_id, text)
        except FakePlatformError as exc:
            if exc.kind in (FailureKind.CONNECT_TIMEOUT, FailureKind.READ_TIMEOUT):
                return _failure(exc, request)
            return httpx.Response(200, json={"errcode": 130101, "errmsg": exc.text})
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})


__all__ = [
    "API",
    "ENDPOINT",
    "MAX_LEN",
    "WEBHOOK_URL",
    "DingTalkWire",
    "FakeStreamSocket",
    "fake_client_id",
    "fake_secret",
]
