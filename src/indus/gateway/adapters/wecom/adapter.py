"""The WeCom smart-robot adapter (P16-17): one WebSocket for everything, in both directions.

Connecting opens the socket (20 s) and subscribes with the bot id and secret (``aibot_subscribe``);
a background supervisor owns the socket from then on: it reads frames, sends a ``ping`` every 30 s
and reopens the socket (2, 5, 10, 30, 60 s) when it drops or a ping goes unanswered for 15 s. A
refused subscribe is fatal (``auth_failed`` at connect, ``report_fatal("wecom_auth")`` later).
WeCom allows one connection per bot: when another one takes over (``disconnected_event``) the
adapter waits 60 s and reconnects once (an ``indus send`` borrowed the bot); a second takeover
within 10 minutes means two gateways share the bot, and the adapter stops with
``report_fatal("wecom_replaced")``.

Inbound (``aibot_msg_callback``): one `InboundEvent` per message; repeats (the last 1,000 message
ids) dropped; ``single`` chats are DMs keyed by the user id, ``group`` chats by ``chatid`` (a group
message reaches a smart robot only when it mentions it: the leading ``@name`` is cut and the event
is marked mentioned); ``text``, ``mixed`` (text items joined, images attached), ``voice`` (WeCom's
transcript, no audio), ``image``, ``file`` and ``video`` (attachments ``wecom://media/<token>``
that `fetch_media` downloads and AES-decrypts with the message's ``aeskey``). Own DM/group
policies (``dm_policy`` / ``group_policy``: ``open``, ``allowlist``, ``pairing``, ``disabled``).

Outbound: markdown, 4,000 characters per message. A chat that wrote to us recently is answered
passively (``aibot_respond_msg`` with the message's ``req_id``); otherwise, or when WeCom says that
``req_id`` has expired, proactively (``aibot_send_msg``). WeCom answers no message id, so a
proactive send is named by its ``req_id`` and the n-th reply to one inbound message
``<req_id>:<n>``. At most 30 messages a minute per chat: beyond that a send answers rate limited
with the wait (never slept inline, R06.3). Native streams: `open_stream` (``stream.py``). Media:
chunked upload (``aibot_upload_media_*``) then a media message.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import itertools
import json
import logging
import math
import re
import uuid
from collections import OrderedDict, deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol
from urllib.parse import urlsplit

import httpx

from indus.foundation.blocking import run_blocking
from indus.foundation.clock import Clock
from indus.gateway.adapters.base import (
    REPLY_TO_KEY,
    Adapter,
    AdapterConnectError,
    Capabilities,
    ChatInfo,
    FetcherSeat,
    MediaKind,
)
from indus.gateway.adapters.wecom.crypto import BLOCK, aes_cbc_decrypt, unpad
from indus.gateway.adapters.wecom.stream import WeComStream
from indus.gateway.core.events import Attachment, ChatType, InboundEvent, MessageKind, Source
from indus.gateway.core.send import SendResult
from indus.gateway.outbound.chunker import split_message

if TYPE_CHECKING:
    from indus.gateway.config import PlatformSettings

__all__ = [
    "BOT_ID_ENV",
    "DEFAULT_WS_URL",
    "MAX_MESSAGE_CHARS",
    "SECRET_ENV",
    "ChatWindow",
    "SocketOpener",
    "WeComAdapter",
    "WeComOptions",
    "httpx_ws_opener",
]

log = logging.getLogger(__name__)

BOT_ID_ENV = "WECOM_BOT_ID"
SECRET_ENV = "WECOM_SECRET"
DEFAULT_WS_URL = "wss://openws.work.weixin.qq.com"
INSTALL_HINT = "pip install 'indus-agent[wecom]'"
MAX_MESSAGE_CHARS = 4000
CONNECT_TIMEOUT_S = 20.0
REQUEST_TIMEOUT_S = 15.0
HEARTBEAT_S = 30.0
BACKOFF_S: tuple[float, ...] = (2.0, 5.0, 10.0, 30.0, 60.0)
TAKEOVER_WAIT_S = 60.0
TAKEOVER_WINDOW_S = 600.0
SEEN_MESSAGES = 1_000
REMEMBERED = 1_000
WINDOW_LIMIT = 30
WINDOW_S = 60.0
MEDIA_TTL_S = 300.0
MEDIA_REFS = 500
UPLOAD_CHUNK = 512 * 1024
UPLOAD_MAX_CHUNKS = 100
UPLOAD_MIN_BYTES = 5
MEDIA_LIMITS: Mapping[str, int] = {
    "image": 10 * 1024 * 1024,
    "video": 10 * 1024 * 1024,
    "voice": 2 * 1024 * 1024,
    "file": 20 * 1024 * 1024,
}
MEDIA_SCHEME = "wecom://media/"

CMD_SUBSCRIBE = "aibot_subscribe"
CMD_CALLBACK = "aibot_msg_callback"
CMD_CALLBACK_OLD = "aibot_callback"
CMD_EVENT = "aibot_event_callback"
CMD_RESPOND = "aibot_respond_msg"
CMD_SEND = "aibot_send_msg"
CMD_PING = "ping"
CMD_UPLOAD_INIT = "aibot_upload_media_init"
CMD_UPLOAD_CHUNK = "aibot_upload_media_chunk"
CMD_UPLOAD_FINISH = "aibot_upload_media_finish"
CALLBACKS = frozenset({CMD_CALLBACK, CMD_CALLBACK_OLD, CMD_EVENT})

SUBSCRIPTION_LOST = 846609
# WeCom error codes → the reading the send classifier (P15-04) recognises.
_RATE_CODES = frozenset({846607, 45009, 45033})
_READINGS: Mapping[int, str] = {
    93006: "chat not found",
    86004: "chat not found",
    40003: "chat not found",
    846604: "message to reply not found",
    846608: "message to reply not found",
    60011: "forbidden",
    48002: "forbidden",
    40058: "can't parse the message",
    40008: "can't parse the message",
    44004: "can't parse the message",
    45002: "message is too long",
}
_STATED_WAIT = re.compile(r"(?:retry after|wait)\s+(\d+(?:\.\d+)?)\s*s", re.IGNORECASE)
_LEADING_MENTION = re.compile(r"^@\S+\s*")
_POLICIES = frozenset({"open", "allowlist", "pairing", "disabled"})
_TRUTHY = frozenset({"1", "true", "yes", "on"})

type Policy = Literal["open", "allowlist", "pairing", "disabled"]


class WebSocketLike(Protocol):
    async def receive_text(self) -> str: ...

    async def send_text(self, data: str) -> None: ...


type SocketOpener = Callable[
    [httpx.AsyncClient, str], contextlib.AbstractAsyncContextManager[WebSocketLike]
]


def httpx_ws_opener(client: httpx.AsyncClient, url: str) -> Any:
    """The production opener: ``httpx_ws.aconnect_ws`` (the ``wecom`` extra)."""
    try:
        import httpx_ws  # pyright: ignore[reportMissingImports]
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            f"the WeCom socket needs httpx-ws: {INSTALL_HINT}", name=exc.name
        ) from None
    return httpx_ws.aconnect_ws(  # pyright: ignore[reportUnknownMemberType]
        url, client, max_message_size_bytes=8 * 1024 * 1024
    )


def _http_url(ws_url: str) -> str:
    """httpx-ws upgrades an http(s) URL; WeCom documents the ``wss://`` form."""
    parts = urlsplit(ws_url.strip())
    scheme = {"wss": "https", "ws": "http"}.get(parts.scheme, parts.scheme)
    return parts._replace(scheme=scheme).geturl()


def _items(value: object) -> list[str]:
    if isinstance(value, str):
        raw: Iterable[object] = value.split(",")
    elif isinstance(value, list | tuple | set | frozenset):
        raw = value  # pyright: ignore[reportUnknownVariableType]
    else:
        return []
    return [str(v).strip() for v in raw if str(v).strip()]  # pyright: ignore[reportUnknownArgumentType, reportUnknownVariableType]


def _policy(value: object, default: Policy) -> Policy:
    text = str(value or "").strip().lower()
    return text if text in _POLICIES else default  # pyright: ignore[reportReturnType]


@dataclass(frozen=True)
class WeComOptions:
    """``platforms.wecom.*`` settings the adapter reads (all optional)."""

    websocket_url: str = DEFAULT_WS_URL
    dm_policy: Policy = "pairing"
    group_policy: Policy = "open"
    allow_from: frozenset[str] = frozenset()
    native_stream: bool = True

    @classmethod
    def read(cls, extra: Mapping[str, Any], env: Mapping[str, str]) -> WeComOptions:
        url = str(extra.get("websocket_url") or env.get("WECOM_WEBSOCKET_URL") or "").strip()
        stream = extra.get("native_stream")
        return cls(
            websocket_url=url or DEFAULT_WS_URL,
            dm_policy=_policy(extra.get("dm_policy"), "pairing"),
            group_policy=_policy(extra.get("group_policy"), "open"),
            allow_from=frozenset(e.lower() for e in _items(extra.get("allow_from"))),
            native_stream=True if stream is None else str(stream).strip().lower() in _TRUTHY,
        )


class ChatWindow:
    """WeCom's 30 messages a minute per chat, as a sliding window over the fake-able clock."""

    def __init__(self, clock: Clock, limit: int = WINDOW_LIMIT, period: float = WINDOW_S) -> None:
        self.clock = clock
        self.limit = limit
        self.period = period
        self._sent: dict[str, deque[float]] = {}

    def take(self, chat_id: str) -> float:
        """0 (and one send counted) when the chat may send now, else the seconds to wait."""
        now = self.clock.monotonic()
        times = self._sent.setdefault(chat_id, deque())
        while times and now - times[0] >= self.period:
            times.popleft()
        if len(times) >= self.limit:
            return max(self.period - (now - times[0]), 0.1)
        times.append(now)
        if len(self._sent) > REMEMBERED:
            for key in [k for k, v in self._sent.items() if not v][: len(self._sent) // 2]:
                del self._sent[key]
        return 0.0


class _Refused(Exception):
    def __init__(self, errcode: int, errmsg: str) -> None:
        super().__init__(f"WeCom refused the subscription (errcode {errcode}): {errmsg}")
        self.errcode = errcode


class _Taken(Exception):
    """Another connection subscribed the bot (``disconnected_event``)."""


class _Silent(Exception):
    """A ping went unanswered."""


@dataclass
class _Media:
    url: str
    aeskey: str
    at: float


def _remember(store: OrderedDict[str, str], key: str, value: str) -> None:
    store.pop(key, None)
    store[key] = value
    while len(store) > REMEMBERED:
        store.popitem(last=False)


def _dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}  # pyright: ignore[reportUnknownArgumentType]


def _code(answer: Mapping[str, Any]) -> int:
    try:
        return int(answer.get("errcode") or 0)
    except (TypeError, ValueError):
        return -1


def media_ref(token: str, name: str) -> str:
    return f"{MEDIA_SCHEME}{token}/{name}"


class WeComAdapter(Adapter):
    platform: ClassVar[str] = "wecom"
    capabilities: ClassVar[Capabilities] = Capabilities(
        max_len=MAX_MESSAGE_CHARS,
        markup="markdown",
        splits_natively=True,
        supports_edit=False,
        supports_delete=False,
        supports_buttons=False,
        owns_access_policy=True,
        dm_is_private=True,
        push_capable=True,
    )
    vendor_names: ClassVar[Mapping[str, tuple[str, ...]]] = {
        "httpx_ws:AsyncWebSocketSession": ("receive_text", "send_text", "close"),
        "indus.gateway.adapters.wecom.stream:WeComStream": ("push", "finish"),
    }

    def __init__(
        self,
        *,
        clock: Clock,
        bot_id: str,
        secret: str,
        settings: PlatformSettings | None = None,
        options: WeComOptions | None = None,
        opener: SocketOpener | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        proxy: str | None = None,
        send_only: bool = False,
    ) -> None:
        super().__init__(clock=clock, settings=settings, secrets=(bot_id, secret))
        self.send_only = send_only
        self._bot_id = bot_id.strip()
        self._secret = secret.strip()
        self.options = options or WeComOptions()
        self._opener: SocketOpener = opener or httpx_ws_opener
        self._transport = transport
        self._proxy = proxy
        self._client: httpx.AsyncClient | None = None
        self._socket: WebSocketLike | None = None
        self._runner: asyncio.Task[None] | None = None
        self._work: set[asyncio.Task[Any]] = set()
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._chat_req: OrderedDict[str, str] = OrderedDict()
        self._message_req: OrderedDict[str, str] = OrderedDict()
        self._chat_kind: OrderedDict[str, str] = OrderedDict()
        self._replies: dict[str, int] = {}
        self._media_refs: OrderedDict[str, _Media] = OrderedDict()
        self._takeovers: deque[float] = deque()
        self._ids = itertools.count(1)
        self.window = ChatWindow(clock)
        self.socket_opens = 0

    def __repr__(self) -> str:
        state = "connected" if self.connected else "disconnected"
        return f"<WeComAdapter {state}>"

    # -- access policy -----------------------------------------------------------------------------

    def access_policy(self, kind: Literal["dm", "group"]) -> str:
        return self.options.dm_policy if kind == "dm" else self.options.group_policy

    def allows_dm(self, user_id: str) -> bool:
        allowed = self.options.allow_from
        return "*" in allowed or user_id.strip().lower() in allowed

    # -- lifecycle ---------------------------------------------------------------------------------

    async def _open(self, is_reconnect: bool) -> None:
        missing = [n for n, v in ((BOT_ID_ENV, self._bot_id), (SECRET_ENV, self._secret)) if not v]
        if missing:
            raise AdapterConnectError(
                "WeCom is not configured: set " + " and ".join(missing),
                retryable=False,
                code="config",
            )
        inner = self._transport or httpx.AsyncHTTPTransport(proxy=self._proxy, trust_env=False)
        self._client = httpx.AsyncClient(
            transport=inner,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(30.0, connect=CONNECT_TIMEOUT_S),
        )
        ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._runner = asyncio.ensure_future(self._supervise(ready))
        try:
            await self.wait_bounded(ready, CONNECT_TIMEOUT_S + 1.0)
        except TimeoutError:
            await self._stop_runner()
            raise AdapterConnectError(
                f"WeCom did not answer the subscription within {CONNECT_TIMEOUT_S:.0f} s",
                retryable=True,
            ) from None
        except BaseException:
            await self._stop_runner()
            raise
        log.info("wecom: subscribed as the bot")

    async def _stop_runner(self) -> None:
        tasks = [t for t in (self._runner, *self._work) if t is not None and not t.done()]
        self._runner = None
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._work.clear()
        self._fail_pending(ConnectionError("the WeCom socket closed"))
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def _close(self) -> None:
        await self._stop_runner()

    async def wait_bounded(self, awaitable: Awaitable[Any], seconds: float) -> Any:
        """``awaitable``'s result, or `TimeoutError` after ``seconds`` on the adapter's clock."""
        future = asyncio.ensure_future(awaitable)
        timer = asyncio.ensure_future(self.clock.sleep(seconds))
        try:
            done, _ = await asyncio.wait({future, timer}, return_when=asyncio.FIRST_COMPLETED)
        except BaseException:
            timer.cancel()
            raise
        timer.cancel()
        if future in done:
            return future.result()
        raise TimeoutError(f"no answer within {seconds:.0f} s")

    # -- the socket --------------------------------------------------------------------------------

    def _new_req(self, prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex[:20]}"

    async def _supervise(self, ready: asyncio.Future[None]) -> None:
        attempt = 0
        url = _http_url(self.options.websocket_url)
        while True:
            client = self._client
            if client is None:
                return
            wait = BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)]
            try:
                async with self._opener(client, url) as socket:
                    await self._session(socket, ready)
            except asyncio.CancelledError:
                raise
            except _Refused as exc:
                if not ready.done():
                    ready.set_exception(
                        AdapterConnectError(str(exc), retryable=False, code="auth_failed")
                    )
                else:
                    self.report_fatal("wecom_auth", f"{exc}; check {BOT_ID_ENV} and {SECRET_ENV}")
                return
            except ModuleNotFoundError as exc:
                if not ready.done():
                    ready.set_exception(
                        AdapterConnectError(str(exc), retryable=False, code="missing_dependency")
                    )
                else:
                    self.report_fatal("missing_dependency", str(exc))
                return
            except _Taken:
                now = self.clock.monotonic()
                self._takeovers.append(now)
                while self._takeovers and now - self._takeovers[0] > TAKEOVER_WINDOW_S:
                    self._takeovers.popleft()
                if len(self._takeovers) >= 2:
                    log.error("wecom: another connection keeps taking the bot over; stopping")
                    self.report_fatal(
                        "wecom_replaced",
                        "another connection subscribed this bot twice within 10 minutes (two "
                        "gateways share one bot?)",
                    )
                    return
                log.warning("wecom: another connection took the bot over; reconnecting later")
                wait = TAKEOVER_WAIT_S
            except Exception as exc:
                if not ready.done():
                    ready.set_exception(
                        AdapterConnectError(
                            self.scrub(f"cannot open the WeCom socket: {type(exc).__name__}"),
                            retryable=True,
                        )
                    )
                    return
                log.warning(
                    "wecom: socket closed (%s); reopening in %.0f s",
                    self.scrub(f"{type(exc).__name__}: {exc}"),
                    wait,
                )
            finally:
                self._socket = None
                self._fail_pending(ConnectionError("the WeCom socket closed"))
            attempt += 1
            await self.clock.sleep(wait)

    async def _session(self, socket: WebSocketLike, ready: asyncio.Future[None]) -> None:
        self._socket = socket
        reader = asyncio.ensure_future(self._read(socket))
        try:
            body = {"bot_id": self._bot_id, "secret": self._secret}
            try:
                answer = await self._request(CMD_SUBSCRIBE, body, timeout=CONNECT_TIMEOUT_S)
            except TimeoutError:
                raise ConnectionError("no answer to the subscription") from None
            code = _code(answer)
            if code != 0:
                raise _Refused(code, str(answer.get("errmsg") or ""))
            self.socket_opens += 1
            if not ready.done():
                ready.set_result(None)
            heartbeat = asyncio.ensure_future(self._heartbeat())
            try:
                done, _ = await asyncio.wait(
                    {reader, heartbeat}, return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                heartbeat.cancel()
            for task in done:
                task.result()  # raises what ended the session
            raise ConnectionError("the WeCom socket ended")
        finally:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reader

    async def _heartbeat(self) -> None:
        while True:
            await self.clock.sleep(HEARTBEAT_S)
            try:
                await self._request(CMD_PING, {}, timeout=REQUEST_TIMEOUT_S)
            except TimeoutError:
                log.warning("wecom: a ping went unanswered; reopening the socket")
                raise _Silent("ping unanswered") from None

    async def _read(self, socket: WebSocketLike) -> None:
        while True:
            raw = await socket.receive_text()
            try:
                frame = json.loads(raw)
            except ValueError:
                try:
                    frame = json.JSONDecoder(strict=False).decode(raw)
                except ValueError:
                    log.debug("wecom: a frame that is not JSON was dropped")
                    continue
            if not isinstance(frame, dict):
                continue
            self._dispatch(_dict(frame))

    def _dispatch(self, frame: dict[str, Any]) -> None:
        cmd = str(frame.get("cmd") or "")
        req_id = str(_dict(frame.get("headers")).get("req_id") or "")
        if cmd not in CALLBACKS and req_id in self._pending:
            future = self._pending.pop(req_id)
            if not future.done():
                future.set_result(frame)
            return
        if cmd in (CMD_CALLBACK, CMD_CALLBACK_OLD):
            if self.send_only:
                return
            event = self.normalise(req_id, _dict(frame.get("body")))
            if event is not None:
                self._spawn(self.emit(event))
            return
        if cmd == CMD_EVENT:
            body = _dict(frame.get("body"))
            kind = str(_dict(body.get("event")).get("eventtype") or body.get("event_type") or "")
            if kind == "disconnected_event":
                raise _Taken()

    def _spawn(self, work: Awaitable[Any]) -> None:
        task: asyncio.Task[Any] = asyncio.ensure_future(work)
        self._work.add(task)
        task.add_done_callback(self._work.discard)

    def _fail_pending(self, exc: Exception) -> None:
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(exc)
                future.exception()  # retrieved: nobody may be waiting any more

    async def _request(
        self,
        cmd: str,
        body: Mapping[str, Any],
        *,
        req_id: str | None = None,
        timeout: float = REQUEST_TIMEOUT_S,
    ) -> dict[str, Any]:
        """Send one frame and wait for WeCom's answer. `ConnectionError`: it never left;
        `TimeoutError`: it left and no answer came (WeCom may have acted on it)."""
        future = await self._submit(cmd, body, req_id=req_id)
        try:
            return await self.wait_bounded(future, timeout)
        finally:
            if not future.done():
                future.cancel()

    async def _submit(
        self, cmd: str, body: Mapping[str, Any], *, req_id: str | None = None
    ) -> asyncio.Future[dict[str, Any]]:
        socket = self._socket
        if socket is None:
            raise ConnectionError("the WeCom socket is not open")
        rid = req_id or self._new_req(cmd)
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[rid] = future
        frame = {"cmd": cmd, "headers": {"req_id": rid}, "body": dict(body)}
        try:
            await socket.send_text(json.dumps(frame, ensure_ascii=False))
        except Exception as exc:
            self._pending.pop(rid, None)
            raise ConnectionError(f"the frame could not be sent: {type(exc).__name__}") from None
        future.add_done_callback(lambda _f: self._pending.pop(rid, None))
        return future

    # -- inbound -----------------------------------------------------------------------------------

    def _seen_before(self, message_id: str) -> bool:
        if message_id in self._seen:
            return True
        self._seen[message_id] = None
        while len(self._seen) > SEEN_MESSAGES:
            self._seen.popitem(last=False)
        return False

    def _keep_media(self, kind: str, info: Mapping[str, Any]) -> Attachment | None:
        url = str(info.get("url") or "")
        if not url.startswith("https://"):
            return None
        token = uuid.uuid4().hex
        self._media_refs[token] = _Media(url, str(info.get("aeskey") or ""), self.clock.monotonic())
        while len(self._media_refs) > MEDIA_REFS:
            self._media_refs.popitem(last=False)
        mime, name = {
            "image": ("image/jpeg", "image.jpg"),
            "video": ("video/mp4", "video.mp4"),
        }.get(kind, ("application/octet-stream", "file.bin"))
        return Attachment(ref=media_ref(token, name), mime=mime)

    def normalise(self, req_id: str, body: Mapping[str, Any]) -> InboundEvent | None:
        """One ``aibot_msg_callback`` body → an `InboundEvent`, or None when it is dropped."""
        message_id = str(body.get("msgid") or req_id or "")
        if not message_id or self._seen_before(message_id):
            return None
        sender = str(_dict(body.get("from")).get("userid") or "").strip()
        group = str(body.get("chattype") or "").lower() == "group"
        chat_id = str(body.get("chatid") or "").strip() if group else sender
        if not chat_id or not sender:
            return None
        policy = self.options.group_policy if group else self.options.dm_policy
        if policy == "disabled":
            log.debug("wecom: %s messages are disabled; dropped", "group" if group else "DM")
            return None
        if req_id:
            _remember(self._chat_req, chat_id, req_id)
            _remember(self._message_req, message_id, req_id)
        _remember(self._chat_kind, chat_id, "group" if group else "dm")
        kind = str(body.get("msgtype") or "").lower()
        texts: list[str] = []
        media: list[Attachment] = []
        if kind == "mixed":
            items = _dict(body.get("mixed")).get("msg_item")
            for item in items if isinstance(items, list) else []:  # pyright: ignore[reportUnknownVariableType]
                entry = _dict(item)
                sub = str(entry.get("msgtype") or "").lower()
                if sub == "text":
                    texts.append(str(_dict(entry.get("text")).get("content") or ""))
                elif sub in ("image", "file", "video"):
                    found = self._keep_media(sub, _dict(entry.get(sub)))
                    if found is not None:
                        media.append(found)
        elif kind in ("text", "voice"):
            texts.append(str(_dict(body.get(kind)).get("content") or ""))
        elif kind in ("image", "file", "video"):
            found = self._keep_media(kind, _dict(body.get(kind)))
            if found is not None:
                media.append(found)
        text = "\n".join(t.strip() for t in texts if t.strip())
        if group:
            text = _LEADING_MENTION.sub("", text).strip()
        quote = _dict(body.get("quote"))
        quote_kind = str(quote.get("msgtype") or "").lower()
        quoted = ""
        if quote_kind in ("text", "voice"):
            quoted = str(_dict(quote.get(quote_kind)).get("content") or "").strip()
        if not text and not media and quoted:
            text, quoted = quoted, ""  # a bare quote: the quoted words are the message
        if not text and not media:
            return None
        event_kind = MessageKind.TEXT
        if kind == "voice":
            event_kind = MessageKind.VOICE
        elif media:
            mime = media[0].mime
            event_kind = (
                MessageKind.PHOTO
                if mime.startswith("image/")
                else MessageKind.VIDEO
                if mime.startswith("video/")
                else MessageKind.DOCUMENT
            )
        chat_type: ChatType = "group" if group else "dm"
        source = Source(
            platform=self.platform,
            chat_id=chat_id,
            chat_type=chat_type,
            user_id=sender,
            user_name=sender,
        )
        return InboundEvent(
            source=source,
            text=text,
            kind=event_kind,
            message_id=message_id,
            media=tuple(media),
            reply_to_text=quoted or None,
            metadata={"mentioned": True} if group else {},
        )

    # -- media for the intake ----------------------------------------------------------------------

    async def fetch_media(self, attachment: Attachment) -> AsyncIterator[bytes]:
        """Download and decrypt an inbound attachment (the URL lives 5 minutes)."""
        if not attachment.ref.startswith(MEDIA_SCHEME):
            raise ValueError("not a WeCom media reference")
        token = attachment.ref[len(MEDIA_SCHEME) :].split("/", 1)[0]
        found = self._media_refs.get(token)
        if found is None or self.clock.monotonic() - found.at > MEDIA_TTL_S:
            raise LookupError("the WeCom media link has expired")
        client = self._client
        if client is None:
            raise ConnectionError("not connected to WeCom")
        cap = MEDIA_LIMITS["file"] + 2 * BLOCK
        data = bytearray()
        async with client.stream("GET", found.url, timeout=120.0) as response:
            if response.status_code >= 400:
                raise ConnectionError(f"the media download failed (HTTP {response.status_code})")
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > cap:
                    raise ValueError("the media file is larger than WeCom allows")
        yield decrypt_media(bytes(data), found.aeskey) if found.aeskey else bytes(data)

    def register_fetchers(self, seat: FetcherSeat) -> Callable[[], None] | None:
        return seat.register_fetcher(self.platform, self.fetch_media)

    # -- outbound ----------------------------------------------------------------------------------

    def _reply_req(self, chat_id: str, reply_to: str | None) -> str | None:
        if reply_to and reply_to in self._message_req:
            return self._message_req[reply_to]
        return self._chat_req.get(chat_id)

    def _failure(self, answer: Mapping[str, Any]) -> SendResult:
        code = _code(answer)
        message = str(answer.get("errmsg") or "")
        if code in _RATE_CODES:
            stated = _STATED_WAIT.search(message)
            wait = float(stated.group(1)) if stated else WINDOW_S
            return SendResult.failure(
                f"rate limited: WeCom errcode {code}: {message}", retry_after=wait
            )
        if code == SUBSCRIPTION_LOST:
            # The socket lost its subscription: the reply anchors bound to it are dead.
            self._chat_req.clear()
            self._message_req.clear()
            return SendResult.failure(f"connection reset: WeCom errcode {code}: {message}")
        reading = _READINGS.get(code)
        text = f"WeCom errcode {code}: {message}"
        return SendResult.failure(f"{reading} ({text})" if reading else text)

    async def _deliver(
        self, chat_id: str, body: Mapping[str, Any], reply_to: str | None = None
    ) -> SendResult:
        """One message: passive reply when a ``req_id`` is known, else a proactive send."""
        wait = self.window.take(chat_id)
        if wait > 0:
            return SendResult.failure(
                f"rate limited: 30 messages a minute in {chat_id}", retry_after=math.ceil(wait)
            )
        req = self._reply_req(chat_id, reply_to)
        try:
            if req is not None:
                answer = await self._request(CMD_RESPOND, body, req_id=req)
                code = _code(answer)
                if code == 0:
                    count = self._replies.get(req, 0) + 1
                    self._replies[req] = count
                    if len(self._replies) > REMEMBERED:
                        self._replies.pop(next(iter(self._replies)))
                    return SendResult.sent(f"{req}:{count}")
                if code not in (846604, 846608):
                    return self._failure(answer)
                # The inbound message is too old to answer: write to the chat instead.
                self._chat_req.pop(chat_id, None)
            known = self._chat_kind.get(chat_id)
            chat_type = 2 if known == "group" else 1 if known == "dm" else 0
            rid = self._new_req(CMD_SEND)
            answer = await self._request(
                CMD_SEND, {"chatid": chat_id, "chat_type": chat_type, **body}, req_id=rid
            )
        except ConnectionError as exc:
            return SendResult.failure(f"connect error: {exc}; the message never reached WeCom")
        except TimeoutError:
            return SendResult.failure(
                "read timeout: WeCom did not answer within 15 s; it may have the message"
            )
        if _code(answer) != 0:
            return self._failure(answer)
        return SendResult.sent(rid)

    async def _send(
        self, chat_id: str, text: str, reply_to: str | None, meta: dict[str, Any]
    ) -> SendResult:
        chunked = split_message(text, MAX_MESSAGE_CHARS)
        sent: list[str] = []
        for n, chunk in enumerate(chunked.chunks):
            if n:
                await self.slots.pace(chat_id)
            body = {"msgtype": "markdown", "markdown": {"content": chunk.text}}
            result = await self._deliver(chat_id, body, reply_to)
            if not result.ok:
                if not sent:
                    return result
                return SendResult.failure(
                    result.error,
                    retry_after=result.retry_after,
                    delivered_ids=tuple(sent),
                    remainder=None if result.maybe_delivered else chunked.remainder(n),
                )
            sent.append(result.message_id or "")
        return SendResult.sent(sent[-1], tuple(sent[:-1]))

    # -- native streams ----------------------------------------------------------------------------

    def send_frame(self, req_id: str, body: dict[str, Any]) -> Awaitable[dict[str, Any]]:
        """One ``aibot_respond_msg`` frame for `WeComStream` (see `FrameSender`)."""
        return self._request(CMD_RESPOND, body, req_id=req_id)

    def open_stream(
        self,
        chat_id: str,
        *,
        reply_to: str | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> WeComStream | None:
        """A native reply stream into ``chat_id``, or None (streaming off, or no inbound message
        to answer: the caller sends normally)."""
        if not self.options.native_stream or self._socket is None:
            return None
        req = self._reply_req(chat_id, reply_to)
        if req is None:
            return None

        async def rest(text: str) -> SendResult:
            return await self.send(chat_id, text)

        return WeComStream(self, chat_id, req, on_rest=rest)

    # -- media out ---------------------------------------------------------------------------------

    async def _send_media(
        self, kind: MediaKind, chat_id: str, path: str, caption: str | None, meta: dict[str, Any]
    ) -> SendResult:
        file = Path(path)
        size = (await run_blocking(file.stat, label="wecom upload")).st_size
        suffix = file.suffix.lower()
        media_type = {"image": "image", "video": "video", "voice": "voice"}.get(kind, "file")
        if media_type == "voice" and suffix != ".amr":
            media_type = "file"  # WeCom plays AMR only
        if media_type == "video" and suffix != ".mp4":
            media_type = "file"
        if media_type == "image" and suffix not in (".png", ".jpg", ".jpeg", ".gif"):
            media_type = "file"
        limit = MEDIA_LIMITS[media_type]
        if size > limit and media_type != "file" and size <= MEDIA_LIMITS["file"]:
            media_type, limit = "file", MEDIA_LIMITS["file"]
        if size > limit:
            return SendResult.failure(
                f"the file is too long for WeCom ({size} bytes, the cap is {limit})"
            )
        if size < UPLOAD_MIN_BYTES:
            return SendResult.failure("the file is too small for WeCom (at least 5 bytes)")
        data = await run_blocking(file.read_bytes, label="wecom upload")
        try:
            media_id = await self._upload(data, media_type, file.name)
        except ConnectionError as exc:
            return SendResult.failure(f"connect error: {exc}; the file never reached WeCom")
        except TimeoutError:
            return SendResult.failure("read timeout: WeCom did not answer the upload in time")
        except _UploadFailed as exc:
            return self._failure(exc.answer)
        reply_to = meta.get(REPLY_TO_KEY)
        body = {"msgtype": media_type, media_type: {"media_id": media_id}}
        result = await self._deliver(chat_id, body, str(reply_to) if reply_to else None)
        if result.ok and caption and caption.strip():
            more = await self._send(chat_id, caption, None, {})
            if more.ok:
                return SendResult.sent(more.message_id or "", (result.message_id or "",))
        return result

    async def _upload(self, data: bytes, media_type: str, name: str) -> str:
        chunks = max(1, math.ceil(len(data) / UPLOAD_CHUNK))
        if chunks > UPLOAD_MAX_CHUNKS:
            raise _UploadFailed({"errcode": 45002, "errmsg": "too many chunks"})
        init = {
            "type": media_type,
            "filename": name,
            "total_size": len(data),
            "total_chunks": chunks,
        }
        answer = await self._request(CMD_UPLOAD_INIT, init)
        upload_id = str(_dict(answer.get("body")).get("upload_id") or "")
        if _code(answer) != 0 or not upload_id:
            raise _UploadFailed(answer)
        for index in range(chunks):
            piece = data[index * UPLOAD_CHUNK : (index + 1) * UPLOAD_CHUNK]
            part = {
                "upload_id": upload_id,
                "chunk_index": index,
                "base64_data": base64.b64encode(piece).decode("ascii"),
            }
            answer = await self._request(CMD_UPLOAD_CHUNK, part)
            if _code(answer) != 0:
                raise _UploadFailed(answer)
        answer = await self._request(CMD_UPLOAD_FINISH, {"upload_id": upload_id})
        media_id = str(_dict(answer.get("body")).get("media_id") or "")
        if _code(answer) != 0 or not media_id:
            raise _UploadFailed(answer)
        return media_id

    async def _chat_info(self, chat_id: str) -> ChatInfo:
        kind = self._chat_kind.get(chat_id)
        return ChatInfo(name=chat_id, type="group" if kind == "group" else "dm" if kind else None)


class _UploadFailed(Exception):
    def __init__(self, answer: Mapping[str, Any]) -> None:
        super().__init__(f"WeCom upload failed (errcode {_code(answer)})")
        self.answer = answer


def decrypt_media(data: bytes, aeskey: str) -> bytes:
    """An inbound file decrypted with its message's ``aeskey`` (AES-256-CBC, IV = the key's first
    16 bytes, PKCS#7 padding)."""
    text = aeskey.strip()
    try:
        key = base64.b64decode(text + "=" * (-len(text) % 4), validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("the media key is not base64") from None
    if len(key) != 32:
        raise ValueError("the media key is not 32 bytes")
    return unpad(aes_cbc_decrypt(key, data))
