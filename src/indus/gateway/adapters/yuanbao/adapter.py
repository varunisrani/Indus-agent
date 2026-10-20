"""The Yuanbao adapter (P16-21): one signed WebSocket both ways, HTTPS for tokens and media.

Connecting buys a sign token (``client.YuanbaoApi``; a refused App ID / Secret fails the connect
for good) and starts a supervisor that owns the socket from then on:

1. open the socket (15 s) and send ``auth-bind`` with the token; the answer (10 s) must carry
   code 0 and a connect id;
2. read frames: responses resolve the request waiting on their ``msg_id``; pushes are
   acknowledged when they ask for it and become `InboundEvent`s; a ``kickout`` push ends the
   socket (another connection took the bot over);
3. every 30 s send a ``ping``; an answer must come within 10 s; two misses in a row end the socket;
4. a socket that ends is replaced after 1, 2, 4 … 60 s with a fresh token; after 100 failed
   replacements in a row the platform reports fatal (``yuanbao_reconnect``); a permanent close
   code (4012, 4013, 4014, 4018, 4019, 4021) or a refused token reports fatal at once.
All waiting goes through the injected clock.

Chats: groups are ``group:<group code>``, DMs ``direct:<account>`` (a bare account in ``send`` is a
DM). Inbound: repeats (message id, 300 s) and the bot's own messages are dropped, as are recall
callbacks; text, image, voice, video, file, sticker and custom elements become text and
attachments; an @mention of the bot marks the event ``mentioned`` (the core's mention gate
decides in groups). Own DM / group policies: ``open``, ``allowlist`` (DMs: ``allow_from``; groups:
``group_allowed_chats``), ``pairing``, ``disabled``.

Outbound: Markdown, 4,000 characters per message, as ``send_c2c_message`` / ``send_group_message``
answered within 30 s (no answer: maybe delivered, never retried). ``@nickname`` in a group becomes
a mention when the member list is cached. While a turn runs, ``typing_start`` keeps a "reply in
progress" heartbeat going every 2 s (it stops after 30 s without renewal); a FINISH heartbeat
follows every delivered reply. Media: an upload slot from ``genUploadInfo``, a signed COS PUT, then
an image or file element (captions as a text element). Stickers: `send_sticker`.

A send-only adapter (``indus send``) opens the socket and authenticates but never reads or
acknowledges pushes.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import itertools
import logging
import mimetypes
import re
import struct
import sys
import uuid
from collections import OrderedDict, deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Final, Literal, cast
from urllib.parse import parse_qs, urlsplit

import httpx

from indus import __version__
from indus.foundation.blocking import run_blocking
from indus.foundation.clock import Clock
from indus.gateway.adapters._http import SeenIds
from indus.gateway.adapters.base import (
    REPLY_TO_KEY,
    Adapter,
    AdapterConnectError,
    Capabilities,
    ChatInfo,
    FetcherSeat,
    InboundSink,
    MediaKind,
)
from indus.gateway.adapters.yuanbao import frames
from indus.gateway.adapters.yuanbao.client import (
    DEFAULT_API,
    DEFAULT_WS_URL,
    ApiError,
    SignToken,
    SocketClosed,
    SocketOpener,
    TokenError,
    YuanbaoApi,
    YuanbaoSocket,
    check_api_base,
    check_ws_url,
    cos_authorization,
    cos_url,
    cos_window,
    host_allowed,
    httpx_ws_opener,
    json_data,
    utc_now_ms,
)
from indus.gateway.adapters.yuanbao.frames import Element, FrameError, Push
from indus.gateway.adapters.yuanbao.stickers import Sticker
from indus.gateway.core.events import Attachment, ChatType, InboundEvent, MessageKind, Source
from indus.gateway.core.send import SendResult, is_ambiguous_timeout
from indus.gateway.outbound.chunker import split_message

if TYPE_CHECKING:
    from indus.gateway.config import PlatformSettings

__all__ = [
    "APP_ID_ENV",
    "BACKOFF_MAX_S",
    "HEARTBEAT_S",
    "MAX_RECONNECTS",
    "MAX_TEXT",
    "PERMANENT_CLOSE_CODES",
    "SECRET_ENV",
    "Target",
    "YuanbaoAdapter",
    "YuanbaoOptions",
    "YuanbaoRefused",
    "live_adapter",
    "parse_chat",
]

log = logging.getLogger(__name__)

APP_ID_ENV: Final = "YUANBAO_APP_ID"
SECRET_ENV: Final = "YUANBAO_APP_SECRET"
BOT_ID_ENV: Final = "YUANBAO_BOT_ID"
WS_URL_ENV: Final = "YUANBAO_WS_URL"
API_ENV: Final = "YUANBAO_API_DOMAIN"
ROUTE_ENV: Final = "YUANBAO_ROUTE_ENV"

CONNECT_TIMEOUT_S: Final = 15.0
AUTH_TIMEOUT_S: Final = 10.0
HEARTBEAT_S: Final = 30.0
PONG_TIMEOUT_S: Final = 10.0
MISSED_PONGS: Final = 2
MAX_RECONNECTS: Final = 100
BACKOFF_MAX_S: Final = 60.0
SEND_TIMEOUT_S: Final = 30.0
QUERY_TIMEOUT_S: Final = 15.0
REPLY_BEAT_S: Final = 2.0
REPLY_BEAT_IDLE_S: Final = 30.0
MAX_TEXT: Final = 4000
SEEN_TTL_S: Final = 300.0
SEEN_MAX: Final = 2000
REMEMBERED: Final = 1000
# Messages kept while the gateway has not installed its handler yet (oldest dropped past it).
EARLY_MAX: Final = 200
MEMBER_CACHE_S: Final = 300.0
DEFAULT_MAX_MEDIA: Final = 50 * 1024 * 1024
DEFAULT_INSTANCE_ID: Final = "17"
# Close codes Yuanbao uses for problems a new socket cannot fix.
PERMANENT_CLOSE_CODES: Final = frozenset({4012, 4013, 4014, 4018, 4019, 4021})
RECALLS: Final = frozenset({"Group.CallbackAfterRecallMsg", "C2C.CallbackAfterMsgWithDraw"})
# Text Yuanbao clients put in place of media; alone, with no media, it is not a message.
PLACEHOLDERS: Final = frozenset(
    {"[image]", "[file]", "[video]", "[voice]", "[图片]", "[文件]", "[视频]", "[语音]"}
)
# Hosts inbound media may come from (and their subdomains); more via ``media_hosts``.
MEDIA_HOSTS: Final = (
    "yuanbao.tencent.com",
    "myqcloud.com",
    "tencentcos.cn",
    "qq.com",
    "qpic.cn",
    "gtimg.cn",
)
_POLICIES: Final = frozenset({"open", "allowlist", "pairing", "disabled"})
_STATED_WAIT = re.compile(r"retry after\s+(\d+(?:\.\d+)?)", re.IGNORECASE)
_AT_NICK = re.compile(r"(?:(?<=\s)|^)@(\S+?)(?=\s|$)", re.MULTILINE)
_ID = re.compile(r"^[A-Za-z0-9_.@=+-]{1,128}$")
_MEDIA_ELEMENTS: Final[Mapping[str, tuple[str, MessageKind, str]]] = {
    "TIMImageElem": ("image", MessageKind.PHOTO, "image/jpeg"),
    "TIMSoundElem": ("voice", MessageKind.VOICE, "audio/mpeg"),
    "TIMVideoFileElem": ("video", MessageKind.VIDEO, "video/mp4"),
    "TIMFileElem": ("file", MessageKind.DOCUMENT, "application/octet-stream"),
}
_IMAGE_FORMATS: Final[Mapping[str, int]] = {
    "image/jpeg": 1,
    "image/gif": 2,
    "image/png": 3,
    "image/bmp": 4,
}

type Policy = Literal["open", "allowlist", "pairing", "disabled"]
type ChatKind = Literal["group", "direct"]


# -- the live adapters (the P16-22 tools reach the running bot through this) ---------------------

_LIVE: dict[str, YuanbaoAdapter] = {}


def live_adapter(key: str) -> YuanbaoAdapter | None:
    """The connected Yuanbao adapter of the profile ``key`` (``tools.registry.scope_key``)."""
    found = _LIVE.get(key)
    return found if found is not None and found.connected else None


# -- chats -----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    kind: ChatKind
    id: str

    @property
    def chat_id(self) -> str:
        return f"{self.kind}:{self.id}"


def parse_chat(chat_id: str) -> Target:
    """``group:<code>`` / ``direct:<account>`` / a bare account (a DM); ``ValueError`` otherwise."""
    text = chat_id.strip()
    kind: ChatKind = "direct"
    for prefix in ("group:", "direct:"):
        if text.startswith(prefix):
            kind = "group" if prefix == "group:" else "direct"
            text = text[len(prefix) :]
            break
    if not _ID.match(text):
        raise ValueError(f"{chat_id!r} is not a Yuanbao chat id")
    return Target(kind, text)


# -- options ---------------------------------------------------------------------------------------


def _items(value: object) -> list[str]:
    if isinstance(value, str):
        raw: Sequence[object] = value.split(",")
    elif isinstance(value, list | tuple | set | frozenset):
        raw = list(cast(Sequence[object], value))
    else:
        return []
    return [str(v).strip() for v in raw if str(v).strip()]


def _policy(value: object, default: Policy) -> Policy:
    text = str(value or "").strip().lower()
    return cast(Policy, text) if text in _POLICIES else default


@dataclass(frozen=True)
class YuanbaoOptions:
    """``platforms.yuanbao.*`` settings (all optional; the URL ones also from the secret store /
    environment)."""

    ws_url: str = DEFAULT_WS_URL
    api_base: str = DEFAULT_API
    route_env: str = ""
    bot_id: str = ""
    instance_id: str = DEFAULT_INSTANCE_ID
    dm_policy: Policy = "pairing"
    group_policy: Policy = "open"
    allow_from: frozenset[str] = frozenset()
    group_allowed_chats: frozenset[str] = frozenset()
    media_hosts: tuple[str, ...] = MEDIA_HOSTS
    max_media_bytes: int = DEFAULT_MAX_MEDIA

    @classmethod
    def read(
        cls, extra: Mapping[str, Any], value: Callable[[str], str | None] = lambda _n: None
    ) -> YuanbaoOptions:
        """``ValueError`` for a URL that would carry the token in clear or to a bad place."""

        def text(key: str, env: str = "") -> str:
            found = str(extra.get(key) or "").strip()
            return found or ((value(env) or "").strip() if env else "")

        try:
            cap = int(extra.get("max_media_bytes") or DEFAULT_MAX_MEDIA)
        except (TypeError, ValueError):
            cap = DEFAULT_MAX_MEDIA
        extra_hosts = tuple(h.lower().strip(".") for h in _items(extra.get("media_hosts")))
        chats = {
            c if c.startswith("group:") else f"group:{c}"
            for c in _items(extra.get("group_allowed_chats"))
        }
        return cls(
            ws_url=check_ws_url(text("ws_url", WS_URL_ENV) or DEFAULT_WS_URL),
            api_base=check_api_base(text("api_domain", API_ENV) or DEFAULT_API),
            route_env=text("route_env", ROUTE_ENV),
            bot_id=text("bot_id", BOT_ID_ENV),
            instance_id=text("instance_id") or DEFAULT_INSTANCE_ID,
            dm_policy=_policy(extra.get("dm_policy"), "pairing"),
            group_policy=_policy(extra.get("group_policy"), "open"),
            allow_from=frozenset(_items(extra.get("allow_from"))),
            group_allowed_chats=frozenset(chats),
            media_hosts=(*MEDIA_HOSTS, *extra_hosts),
            max_media_bytes=max(1, cap),
        )


# -- errors ----------------------------------------------------------------------------------------


class YuanbaoRefused(Exception):
    """Yuanbao answered a request with a failure: the head status and/or the answer's code."""

    def __init__(self, code: int, message: str) -> None:
        self.code = code
        self.message = " ".join(message.split())[:300]
        text = f"Yuanbao refused the request (code {code})"
        super().__init__(f"{text}: {self.message}" if self.message else text)

    @property
    def retry_after(self) -> float | None:
        found = _STATED_WAIT.search(self.message)
        return float(found.group(1)) if found else None


class _Permanent(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(f"Yuanbao closed the socket with permanent code {code}")
        self.code = code


class _Silent(Exception):
    """Two pings went unanswered."""


class _Kicked(Exception):
    """Another connection took the bot over."""


class _AuthRefused(Exception):
    """``auth-bind`` was answered with a failure."""


# -- media helpers ---------------------------------------------------------------------------------


def _image_size(data: bytes) -> tuple[int, int]:
    """Width and height from a PNG, GIF, JPEG or WebP header ((0, 0) when unknown)."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        return int(width), int(height)
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        width, height = struct.unpack("<HH", data[6:10])
        return int(width), int(height)
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP" and len(data) >= 30:
        chunk = data[12:16]
        if chunk == b"VP8X":
            return 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(
                data[27:30], "little"
            )
        if chunk == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
            width, height = struct.unpack("<HH", data[26:30])
            return int(width) & 0x3FFF, int(height) & 0x3FFF
        if chunk == b"VP8L" and len(data) >= 25:
            bits = int.from_bytes(data[21:25], "little")
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if data[:2] == b"\xff\xd8":
        pos = 2
        while pos + 9 < len(data):
            if data[pos] != 0xFF:
                pos += 1
                continue
            marker = data[pos + 1]
            if marker in (0xC0, 0xC1, 0xC2):
                height, width = struct.unpack(">HH", data[pos + 5 : pos + 9])
                return int(width), int(height)
            length = struct.unpack(">H", data[pos + 2 : pos + 4])[0]
            pos += 2 + int(length)
    return 0, 0


def _read_file(path: str, cap: int) -> bytes:
    target = Path(path)
    size = target.stat().st_size
    if size > cap:
        raise ValueError(f"the file is too large ({size} bytes, the cap is {cap})")
    data = target.read_bytes()
    if not data:
        raise ValueError("the file is empty")
    return data


def _resource_id(url: str) -> str:
    try:
        query = parse_qs(urlsplit(url).query)
    except ValueError:
        return ""
    for key in ("resourceId", "resourceid"):
        values = query.get(key)
        if values and values[0].strip():
            return values[0].strip()
    return ""


def _image_url(content: Mapping[str, Any]) -> str:
    raw = content.get("image_info_array")
    infos = (
        [i for i in cast(Sequence[Any], raw) if isinstance(i, Mapping)]
        if isinstance(raw, list)
        else []
    )
    if not infos:
        return str(content.get("url") or "").strip()
    pick = cast(Mapping[str, Any], infos[1] if len(infos) > 1 else infos[0])
    return str(pick.get("url") or "").strip()


def _guess(url: str, name: str, default: str) -> str:
    for text in (name, urlsplit(url).path):
        if text:
            found = mimetypes.guess_type(text)[0]
            if found:
                return found
    return default


@dataclass
class _Beat:
    renewed: float
    task: asyncio.Task[None] | None = None


@dataclass
class _Members:
    at: float
    members: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])


# -- the adapter -----------------------------------------------------------------------------------


class YuanbaoAdapter(Adapter):
    platform: ClassVar[str] = "yuanbao"
    capabilities: ClassVar[Capabilities] = Capabilities(
        max_len=MAX_TEXT,
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
        "httpx_ws:AsyncWebSocketSession": ("receive_bytes", "send_bytes"),
        # Our own REST client (no SDK); its routes are ``client.ROUTES``, served by the wire fake.
        "indus.gateway.adapters.yuanbao.client:YuanbaoApi": (
            "token",
            "upload_info",
            "resource_url",
            "put_object",
            "download",
        ),
    }

    def __init__(
        self,
        *,
        clock: Clock,
        app_id: str,
        secret: str,
        settings: PlatformSettings | None = None,
        options: YuanbaoOptions | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        proxy: str | None = None,
        opener: SocketOpener | None = None,
        send_only: bool = False,
        live_key: str = "",
    ) -> None:
        super().__init__(clock=clock, settings=settings, secrets=(secret,))
        self.app_id = app_id.strip()
        self._secret = secret.strip()
        self.options = options or YuanbaoOptions()
        self.send_only = send_only
        self.live_key = live_key
        self._transport = transport
        self._proxy = proxy
        self._opener: SocketOpener = opener or httpx_ws_opener
        self._http: httpx.AsyncClient | None = None
        self.api: YuanbaoApi | None = None
        self.bot_id = self.options.bot_id
        self.connect_id = ""
        self._socket: YuanbaoSocket | None = None
        self._send_lock = asyncio.Lock()
        self._runner: asyncio.Task[None] | None = None
        self._work: set[asyncio.Task[Any]] = set()
        self._pending: dict[str, asyncio.Future[frames.Frame]] = {}
        self._by_cmd: dict[str, asyncio.Future[frames.Frame]] = {}
        self._seen = SeenIds(clock, SEEN_TTL_S, SEEN_MAX)
        self._beats: dict[str, _Beat] = {}
        self._members: OrderedDict[str, _Members] = OrderedDict()
        self._dm_group: OrderedDict[str, str] = OrderedDict()
        self._ids = itertools.count(1)
        self._tokens: tuple[str, ...] = ()
        self._early: deque[InboundEvent] = deque(maxlen=EARLY_MAX)
        # Counters the tests read.
        self.sockets_opened = 0
        self.reconnects = 0
        self.waits: list[float] = []

    def __repr__(self) -> str:
        state = "connected" if self.connected else "disconnected"
        return f"<YuanbaoAdapter app {self.app_id or '?'} {state}>"

    # -- access policy -----------------------------------------------------------------------------

    def access_policy(self, kind: Literal["dm", "group"]) -> str:
        return self.options.dm_policy if kind == "dm" else self.options.group_policy

    def allows_dm(self, user_id: str) -> bool:
        allowed = self.options.allow_from
        return "*" in allowed or user_id.strip() in allowed

    # -- lifecycle ---------------------------------------------------------------------------------

    async def _open(self, is_reconnect: bool) -> None:
        missing = [n for n, v in ((APP_ID_ENV, self.app_id), (SECRET_ENV, self._secret)) if not v]
        if missing:
            raise AdapterConnectError(
                "Yuanbao is not configured: set " + " and ".join(missing),
                retryable=False,
                code="config",
            )
        http = httpx.AsyncClient(
            transport=self._transport,
            proxy=None if self._transport is not None else self._proxy,
            timeout=httpx.Timeout(30.0, connect=CONNECT_TIMEOUT_S),
            trust_env=False,
            follow_redirects=False,
        )
        api = YuanbaoApi(
            http,
            app_id=self.app_id,
            secret=self._secret,
            base=self.options.api_base,
            clock=self.clock,
            route_env=self.options.route_env,
            version=__version__,
            instance_id=self.options.instance_id,
            os_name=sys.platform,
        )
        try:
            token = await api.token()
        except TokenError as exc:
            await http.aclose()
            raise AdapterConnectError(
                str(exc), retryable=exc.retryable, code="yuanbao_auth"
            ) from None
        except BaseException:
            await http.aclose()
            raise
        self._http, self.api = http, api
        self._adopt(token)
        ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._runner = asyncio.ensure_future(self._supervise(ready, token))
        try:
            await self.wait_bounded(ready, CONNECT_TIMEOUT_S + AUTH_TIMEOUT_S + 1.0)
        except TimeoutError:
            await self._stop()
            raise AdapterConnectError(
                "the Yuanbao socket did not come up in time", retryable=True
            ) from None
        except BaseException:
            await self._stop()
            raise
        if self.live_key and not self.send_only:
            _LIVE[self.live_key] = self
        log.info("yuanbao: connected as bot %s", self.bot_id or "?")

    def _adopt(self, token: SignToken) -> None:
        """Use the token's bot id and mask the token in everything this adapter reports."""
        if token.bot_id:
            self.bot_id = token.bot_id
        base = tuple(v for v in self._secrets if v not in self._tokens)
        self._tokens = (*self._tokens[-3:], token.token)
        self._secrets = (*base, *self._tokens)

    async def _close(self) -> None:
        if self.live_key and _LIVE.get(self.live_key) is self:
            del _LIVE[self.live_key]
        await self._stop()

    async def _stop(self) -> None:
        tasks = [t for t in (self._runner, *self._work) if t is not None and not t.done()]
        for beat in self._beats.values():
            if beat.task is not None and not beat.task.done():
                tasks.append(beat.task)
        self._beats.clear()
        self._runner = None
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._work.clear()
        self._fail_pending(ConnectionError("the Yuanbao socket closed"))
        http, self._http = self._http, None
        self.api = None
        if http is not None:
            await http.aclose()

    async def wait_bounded(self, awaitable: Awaitable[Any], seconds: float) -> Any:
        """``awaitable``'s result, or `TimeoutError` after ``seconds`` on the adapter's clock."""
        future = asyncio.ensure_future(awaitable)
        timer = asyncio.ensure_future(self.clock.sleep(seconds))
        try:
            done, _ = await asyncio.wait({future, timer}, return_when=asyncio.FIRST_COMPLETED)
        except BaseException:
            timer.cancel()
            future.cancel()
            raise
        timer.cancel()
        if future in done:
            return future.result()
        future.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await future
        raise TimeoutError(f"no answer within {seconds:.0f} s")

    def _spawn(self, work: Awaitable[Any]) -> None:
        task: asyncio.Task[Any] = asyncio.ensure_future(work)
        self._work.add(task)
        task.add_done_callback(self._work.discard)

    # -- the supervisor ----------------------------------------------------------------------------

    async def _supervise(self, ready: asyncio.Future[None], first: SignToken) -> None:
        token: SignToken | None = first
        failures = 0
        while True:
            initial = not ready.done()
            established = False
            try:
                established = await self._one_socket(token, ready)
            except asyncio.CancelledError:
                raise
            except _Permanent as exc:
                self._give_up(ready, "yuanbao_closed", str(exc))
                return
            except TokenError as exc:
                if not exc.retryable:
                    message = f"{exc}; check {APP_ID_ENV} and {SECRET_ENV}"
                    self._give_up(ready, "yuanbao_auth", message)
                    return
                self._note_failure(ready, exc)
            except ModuleNotFoundError as exc:
                self._give_up(ready, "missing_dependency", str(exc))
                return
            except Exception as exc:
                self._note_failure(ready, exc)
            if initial and not established:
                return  # the connect failed; ``_open`` raises what ``ready`` carries
            if established:
                failures = 0
            if failures >= MAX_RECONNECTS:
                log.error("yuanbao: %d reconnects failed in a row; giving up", failures)
                self.report_fatal(
                    "yuanbao_reconnect",
                    f"the Yuanbao socket could not be reopened after {failures} attempts",
                )
                return
            wait = min(2.0**failures, BACKOFF_MAX_S)
            failures += 1
            self.waits.append(wait)
            log.info("yuanbao: reopening the socket in %.0f s (attempt %d)", wait, failures)
            await self.clock.sleep(wait)
            self.reconnects += 1
            token = None

    def _give_up(self, ready: asyncio.Future[None], code: str, message: str) -> None:
        if not ready.done():
            ready.set_exception(AdapterConnectError(message, retryable=False, code=code))
            return
        log.error("yuanbao: %s", self.scrub(message))
        self.report_fatal(code, message)

    def _note_failure(self, ready: asyncio.Future[None], exc: BaseException) -> None:
        detail = self.scrub(f"{type(exc).__name__}: {exc}")
        if not ready.done():
            ready.set_exception(
                AdapterConnectError(f"cannot open the Yuanbao socket: {detail}", retryable=True)
            )
            return
        log.warning("yuanbao: opening the socket failed: %s", detail)

    async def _one_socket(self, token: SignToken | None, ready: asyncio.Future[None]) -> bool:
        """Open, authenticate and serve one socket. True when it was authenticated and later
        ended (the caller reconnects); raises when it never got that far."""
        api, http = self.api, self._http
        if api is None or http is None:
            raise ConnectionError("the adapter is closed")
        if token is None:
            token = await api.token(force=True)
            self._adopt(token)
        manager = self._opener(http, self.options.ws_url)
        socket: YuanbaoSocket = await self.wait_bounded(manager.__aenter__(), CONNECT_TIMEOUT_S)
        self.sockets_opened += 1
        self._socket = socket
        reader = asyncio.ensure_future(self._read(socket))
        established = False
        try:
            auth = asyncio.ensure_future(self._authenticate(token))
            done, _ = await asyncio.wait({reader, auth}, return_when=asyncio.FIRST_COMPLETED)
            if auth not in done:
                auth.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await auth
                reader.result()
                raise ConnectionError("the socket closed during auth-bind")
            auth.result()
            established = True
            if not ready.done():
                ready.set_result(None)
            beater = asyncio.ensure_future(self._heartbeat())
            try:
                done, _ = await asyncio.wait({reader, beater}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                beater.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await beater
            for task in done:
                task.result()  # raises what ended the socket
            raise ConnectionError("the Yuanbao socket ended")
        except Exception as exc:
            if not established or isinstance(exc, _Permanent):
                raise
            log.warning("yuanbao: the socket ended: %s", self.scrub(f"{type(exc).__name__}: {exc}"))
            return True
        finally:
            self._socket = None
            if not reader.done():
                reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reader
            with contextlib.suppress(Exception):
                await self.wait_bounded(manager.__aexit__(None, None, None), 1.0)

    async def _authenticate(self, token: SignToken) -> None:
        msg_id = uuid.uuid4().hex
        body = frames.auth_bind(
            uid=self.bot_id or token.bot_id,
            source=token.source or "bot",
            token=token.token,
            app_version=__version__,
            os_name=sys.platform,
            instance_id=self.options.instance_id,
            bot_version=__version__,
            route_env=self.options.route_env,
        )
        frame = frames.request(frames.AUTH_BIND, msg_id, frames.CONN_MODULE, body)
        try:
            answer = await self._request(frame, msg_id, AUTH_TIMEOUT_S, cmd=frames.AUTH_BIND)
        except TimeoutError:
            raise ConnectionError("no answer to auth-bind") from None
        code, message, connect_id = frames.auth_bind_answer(answer.data)
        if answer.head.status != 0 or code != 0 or not connect_id:
            reason = " ".join(message.split())[:200]
            raise _AuthRefused(
                f"Yuanbao refused auth-bind (code {code or answer.head.status})"
                + (f": {reason}" if reason else "")
            )
        self.connect_id = connect_id

    async def _heartbeat(self) -> None:
        missed = 0
        while True:
            await self.clock.sleep(HEARTBEAT_S)
            msg_id = uuid.uuid4().hex
            frame = frames.request(frames.PING, msg_id, frames.CONN_MODULE)
            try:
                await self._request(frame, msg_id, PONG_TIMEOUT_S, cmd=frames.PING)
            except TimeoutError:
                missed += 1
                log.warning("yuanbao: a ping went unanswered (%d of %d)", missed, MISSED_PONGS)
                if missed >= MISSED_PONGS:
                    raise _Silent("two pings went unanswered") from None
                continue
            missed = 0

    async def _read(self, socket: YuanbaoSocket) -> None:
        while True:
            try:
                raw = await socket.receive_bytes()
            except SocketClosed as exc:
                if exc.code in PERMANENT_CLOSE_CODES:
                    raise _Permanent(exc.code) from None
                raise
            try:
                frame = frames.decode_frame(raw)
            except FrameError:
                log.debug("yuanbao: a frame that is not a ConnMsg was dropped")
                continue
            head = frame.head
            if head.cmd_type == frames.RESPONSE:
                future = self._pending.pop(head.msg_id, None)
                if future is None:
                    future = self._by_cmd.pop(head.cmd, None)
                if future is not None and not future.done():
                    future.set_result(frame)
                continue
            if head.cmd_type != frames.PUSH:
                continue
            waiting = self._pending.pop(head.msg_id, None) if head.msg_id else None
            if waiting is not None:
                # Some answers arrive as a push carrying the request's msg id.
                if not waiting.done():
                    waiting.set_result(frame)
                continue
            if self.send_only:
                continue
            if head.need_ack:
                ack = frames.encode_frame(
                    frames.Head(
                        frames.PUSH_ACK, head.cmd, frames.next_seq(), head.msg_id, head.module
                    )
                )
                with contextlib.suppress(Exception):
                    await self._write(ack)
            if head.cmd == frames.KICKOUT:
                log.warning("yuanbao: another connection took the bot over; reconnecting")
                raise _Kicked()
            if frame.data:
                self._on_push(frame.data)

    def _fail_pending(self, exc: Exception) -> None:
        waiting = [*self._pending.values(), *self._by_cmd.values()]
        self._pending, self._by_cmd = {}, {}
        for future in waiting:
            if not future.done():
                future.set_exception(exc)
                future.exception()  # retrieved: nobody may be waiting any more

    async def _write(self, data: bytes) -> None:
        socket = self._socket
        if socket is None:
            raise ConnectionError("the Yuanbao socket is not open")
        async with self._send_lock:
            try:
                await socket.send_bytes(data)
            except Exception as exc:
                raise ConnectionError(
                    f"the frame could not be sent: {type(exc).__name__}"
                ) from None

    async def _request(
        self, frame: bytes, msg_id: str, timeout: float, *, cmd: str = ""
    ) -> frames.Frame:
        """Send one request and wait for its response. `ConnectionError`: it never left;
        `TimeoutError`: it left and no answer came (Yuanbao may have acted on it)."""
        future: asyncio.Future[frames.Frame] = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = future
        if cmd:
            self._by_cmd[cmd] = future
        try:
            await self._write(frame)
            return await self.wait_bounded(asyncio.shield(future), timeout)
        finally:
            self._pending.pop(msg_id, None)
            if cmd and self._by_cmd.get(cmd) is future:
                del self._by_cmd[cmd]
            if not future.done():
                future.cancel()

    async def _business(self, method: str, prefix: str, body: bytes, timeout: float) -> bytes:
        """A business request (module ``yuanbao_openclaw_proxy``); its answer's bytes, or
        `YuanbaoRefused`."""
        msg_id = f"{prefix}_{next(self._ids)}_{uuid.uuid4().hex[:8]}"
        frame = frames.request(method, msg_id, frames.BIZ_MODULE, body)
        answer = await self._request(frame, msg_id, timeout)
        try:
            code, message = frames.result_answer(answer.data)
        except FrameError:
            code, message = 0, ""
        if answer.head.status != 0 or code != 0:
            raise YuanbaoRefused(code or answer.head.status, message)
        return answer.data

    # -- inbound -----------------------------------------------------------------------------------

    def _on_push(self, data: bytes) -> None:
        push: Push | None
        try:
            parsed = json_data(data.decode("utf-8"))
        except UnicodeDecodeError:
            parsed = None
        if parsed is not None:
            push = frames.push_from_json(parsed)
        else:
            try:
                push = frames.decode_push(data)
            except FrameError:
                push = None
        if push is None:
            log.debug("yuanbao: a push without a message was dropped")
            return
        event = self.normalise(push)
        if event is None:
            return
        if self._sink is None:
            # Connected but the gateway has not installed its handler yet: keep it for then.
            self._early.append(event)
            return
        self._spawn(self.emit(event))

    def set_sink(self, sink: InboundSink | None) -> None:
        """Installing the handler also hands it the messages that arrived before it."""
        super().set_sink(sink)
        if sink is not None and self._early:
            waiting = list(self._early)
            self._early.clear()

            async def flush() -> None:
                for event in waiting:
                    await self.emit(event)

            self._spawn(flush())

    def _content(
        self, elements: Sequence[Element]
    ) -> tuple[str, list[Attachment], bool, MessageKind]:
        parts: list[str] = []
        media: list[Attachment] = []
        mentioned = False
        kind = MessageKind.TEXT
        for element in elements:
            content = element.content
            if element.kind == "TIMTextElem":
                parts.append(str(content.get("text") or ""))
            elif element.kind in _MEDIA_ELEMENTS:
                what, media_kind, default = _MEDIA_ELEMENTS[element.kind]
                name = str(content.get("file_name") or "").strip()
                url = _image_url(content) if what == "image" else str(content.get("url") or "")
                url = url.strip()
                if url:
                    media.append(Attachment(ref=url, mime=_guess(url, name, default)))
                    if kind is MessageKind.TEXT:
                        kind = media_kind
                else:
                    parts.append(f"[{what}: {name}]" if name else f"[{what}]")
            elif element.kind == "TIMFaceElem":
                data = json_data(str(content.get("data") or "")) or {}
                name = str(data.get("name") or "").strip()
                parts.append(f"[sticker: {name}]" if name else "[sticker]")
            elif element.kind == "TIMCustomElem":
                data = json_data(str(content.get("data") or "")) or {}
                if data.get("elem_type") == 1002:
                    target = str(data.get("user_id") or "")
                    if self.bot_id and target == self.bot_id:
                        mentioned = True
                    else:
                        parts.append(str(data.get("text") or "@someone"))
                else:
                    parts.append(_custom_text(data))
            elif element.kind:
                parts.append(f"[{element.kind}]")
        text = " ".join(p.strip() for p in parts if p.strip())
        if text.startswith("／"):  # a full-width slash from a Chinese keyboard
            text = "/" + text[1:]
        return text, media, mentioned, kind

    def normalise(self, push: Push) -> InboundEvent | None:
        """One push → an `InboundEvent`, or None when it is dropped."""
        if push.callback_command in RECALLS:
            return None
        sender = push.from_account.strip()
        if not sender or (self.bot_id and sender == self.bot_id):
            return None
        message_id = push.message_id
        if message_id and not self._seen.first_time(message_id):
            log.debug("yuanbao: message %s seen before; dropped", message_id)
            return None
        group = push.group_code.strip()
        if group:
            chat_id = f"group:{group}"
            policy = self.options.group_policy
            if policy == "disabled" or (
                policy == "allowlist" and chat_id not in self.options.group_allowed_chats
            ):
                log.debug("yuanbao: group %s is not allowed by the group policy", group)
                return None
        else:
            if self.options.dm_policy == "disabled":
                return None
            chat_id = f"direct:{sender}"
            origin = push.private_from_group_code.strip()
            if origin:
                self._remember(self._dm_group, chat_id, origin)
        text, media, mentioned, kind = self._content(push.elements)
        if not media and text.strip() in PLACEHOLDERS:
            return None  # a client's stand-in text for media that did not come along
        if not text and not media:
            return None
        chat_type: ChatType = "group" if group else "dm"
        nickname = push.sender_nickname.strip() or None
        source = Source(
            platform=self.platform,
            chat_id=chat_id,
            chat_type=chat_type,
            chat_name=(push.group_name.strip() or group) if group else nickname,
            user_id=sender,
            user_name=nickname,
        )
        metadata: dict[str, Any] = {}
        if group:
            metadata["mentioned"] = mentioned
        timestamp = (
            datetime.fromtimestamp(push.msg_time, UTC) if 0 < push.msg_time < 10**11 else None
        )
        return InboundEvent(
            source=source,
            text=text,
            kind=kind,
            message_id=message_id or None,
            media=tuple(media),
            metadata=metadata,
            reply_expected=mentioned if group else True,
            timestamp=timestamp,
        )

    @staticmethod
    def _remember(store: OrderedDict[str, str], key: str, value: str) -> None:
        store.pop(key, None)
        store[key] = value
        while len(store) > REMEMBERED:
            store.popitem(last=False)

    # -- inbound media -----------------------------------------------------------------------------

    async def fetch_media(self, attachment: Attachment) -> AsyncIterator[bytes]:
        """The bytes of an inbound attachment: a ``resourceId`` URL is exchanged for its direct
        URL first; only https media hosts are fetched, without credentials."""
        api = self.api
        if api is None:
            raise ConnectionError("not connected to Yuanbao")
        url = attachment.ref
        if not host_allowed(url, self.options.media_hosts):
            raise ValueError("refusing to fetch Yuanbao media from a host off the allowlist")
        resource = _resource_id(url)
        if resource:
            url = await api.resource_url(resource)
        if not host_allowed(url, self.options.media_hosts):
            raise ValueError("refusing to fetch Yuanbao media from a host off the allowlist")
        async for chunk in api.download(url, cap=self.options.max_media_bytes):
            yield chunk

    def register_fetchers(self, seat: FetcherSeat) -> Callable[[], None] | None:
        return seat.register_fetcher(self.platform, self.fetch_media)

    # -- outbound ----------------------------------------------------------------------------------

    def _failure(
        self, exc: BaseException, *, sent: list[str] | None = None, remainder: str | None = None
    ) -> SendResult:
        if isinstance(exc, YuanbaoRefused):
            base = SendResult.failure(str(exc), retry_after=exc.retry_after)
        elif isinstance(exc, TimeoutError):
            base = SendResult.failure(
                f"the send timed out: no answer from Yuanbao within {SEND_TIMEOUT_S:.0f} s"
            )
        elif isinstance(exc, ValueError):
            base = SendResult.failure(f"chat not found: {exc}")
        else:
            base = SendResult.failure(exc=exc)
        ids = tuple(sent or ())
        if not ids:
            return base
        keep = None if (base.maybe_delivered or is_ambiguous_timeout(exc)) else remainder
        return replace(
            base, message_id=ids[-1], continuation_ids=ids[:-1], partial=True, remainder=keep
        )

    def cached_members(self, group_code: str) -> list[dict[str, Any]] | None:
        found = self._members.get(group_code)
        if found is None:
            return None
        if self.clock.monotonic() - found.at >= MEMBER_CACHE_S:
            del self._members[group_code]
            return None
        return found.members

    def _text_elements(self, target: Target, text: str) -> list[Element]:
        """Text, with ``@nickname`` turned into mentions of members we know (groups only)."""
        members = self.cached_members(target.id) if target.kind == "group" else None
        if not members:
            return [frames.text_element(text)]
        by_nick: dict[str, tuple[str, str]] = {}
        for member in members:
            nick = str(member.get("nickname") or "")
            user = str(member.get("user_id") or "")
            if nick and user:
                by_nick.setdefault(nick.lower(), (nick, user))
        out: list[Element] = []
        last = 0
        for match in _AT_NICK.finditer(text):
            found = by_nick.get(match.group(1).lower())
            if found is None:
                continue
            before = text[last : match.start()]
            if before.strip():
                out.append(frames.text_element(before))
            nick, user = found
            mention = {"elem_type": 1002, "text": f"@{nick}", "user_id": user}
            out.append(frames.custom_element(mention))
            last = match.end()
        tail = text[last:]
        if tail.strip():
            out.append(frames.text_element(tail))
        return out or [frames.text_element(text)]

    async def deliver(
        self,
        target: Target,
        elements: Sequence[Element],
        *,
        quote: str | None = None,
        group_code: str = "",
    ) -> str:
        """Send one message body; the id Yuanbao answered for (our request id)."""
        sender = self.bot_id or ""
        if target.kind == "group":
            msg_id = f"grp_{next(self._ids)}_{uuid.uuid4().hex[:8]}"
            body = frames.group_message(
                msg_id=msg_id,
                group_code=target.id,
                from_account=sender,
                elements=elements,
                quote=quote or "",
            )
            method = "send_group_message"
        else:
            msg_id = f"c2c_{next(self._ids)}_{uuid.uuid4().hex[:8]}"
            origin = group_code or self._dm_group.get(target.chat_id, "")
            body = frames.c2c_message(
                msg_id=msg_id,
                to_account=target.id,
                from_account=sender,
                elements=elements,
                group_code=origin,
            )
            method = "send_c2c_message"
        frame = frames.request(method, msg_id, frames.BIZ_MODULE, body)
        answer = await self._request(frame, msg_id, SEND_TIMEOUT_S)
        try:
            code, message = frames.result_answer(answer.data)
        except FrameError:
            code, message = 0, ""
        if answer.head.status != 0 or code != 0:
            raise YuanbaoRefused(code or answer.head.status, message)
        return msg_id

    async def _send(
        self, chat_id: str, text: str, reply_to: str | None, meta: dict[str, Any]
    ) -> SendResult:
        try:
            target = parse_chat(chat_id)
        except ValueError as exc:
            return self._failure(exc)
        chunked = split_message(text, MAX_TEXT)
        group_code = str(meta.get("group_code") or "")
        sent: list[str] = []
        for n, chunk in enumerate(chunked.chunks):
            if n:
                await self.slots.pace(chat_id)
            elements = self._text_elements(target, chunk.text)
            try:
                sent.append(
                    await self.deliver(
                        target,
                        elements,
                        quote=reply_to if n == 0 else None,
                        group_code=group_code,
                    )
                )
            except Exception as exc:
                return self._failure(exc, sent=sent, remainder=chunked.remainder(n))
        self._after_delivery(target)
        return SendResult.sent(sent[-1], tuple(sent[:-1]))

    def _after_delivery(self, target: Target) -> None:
        """The reply is out: stop the "in progress" heartbeat and send FINISH."""
        beat = self._beats.pop(target.chat_id, None)
        if beat is not None and beat.task is not None and not beat.task.done():
            beat.task.cancel()
        self._spawn(self._beat_once(target, frames.FINISH))

    async def _send_media(
        self, kind: MediaKind, chat_id: str, path: str, caption: str | None, meta: dict[str, Any]
    ) -> SendResult:
        try:
            target = parse_chat(chat_id)
        except ValueError as exc:
            return self._failure(exc)
        api = self.api
        if api is None:
            return self._failure(ConnectionError("not connected to Yuanbao"))
        try:
            data = await run_blocking(
                _read_file, path, self.options.max_media_bytes, label="yuanbao media read"
            )
        except (OSError, ValueError) as exc:
            return SendResult.failure(f"cannot send the file: {exc}")
        name = Path(path).name or "file"
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        image = kind == "image" and mime.startswith("image/")
        try:
            slot = await api.upload_info(name)
            url, host, signed_path = cos_url(
                str(slot.get("bucketName") or ""), str(slot.get("location") or "")
            )
            security = str(slot.get("encryptToken") or "")
            start, end = cos_window(slot, self.clock.now().timestamp())
            headers = {"host": host, "content-type": mime, "x-cos-security-token": security}
            authorization = cos_authorization(
                method="put",
                path=signed_path,
                headers=headers,
                secret_id=str(slot.get("encryptTmpSecretId") or ""),
                secret_key=str(slot.get("encryptTmpSecretKey") or ""),
                start=start,
                end=end,
            )
            await api.put_object(
                url,
                data,
                {
                    "Authorization": authorization,
                    "Content-Type": mime,
                    "x-cos-security-token": security,
                },
            )
        except (ApiError, ValueError, httpx.HTTPError) as exc:
            return SendResult.failure(f"the Yuanbao upload failed: {self.scrub(str(exc))}")
        resource = str(slot.get("resourceUrl") or "")
        link = resource if resource.startswith("https://") else url
        digest = hashlib.md5(data, usedforsecurity=False).hexdigest()
        if image:
            width, height = _image_size(data)
            elements = [
                frames.image_element(
                    link,
                    uuid=digest,
                    size=len(data),
                    width=width,
                    height=height,
                    image_format=_IMAGE_FORMATS.get(mime, 255),
                )
            ]
        else:
            elements = [frames.file_element(link, uuid=digest, name=name, size=len(data))]
        if caption:
            elements.append(frames.text_element(caption[:MAX_TEXT]))
        reply_to = meta.get(REPLY_TO_KEY)
        try:
            message_id = await self.deliver(
                target, elements, quote=str(reply_to) if reply_to else None
            )
        except Exception as exc:
            return self._failure(exc)
        self._after_delivery(target)
        return SendResult.sent(message_id)

    async def send_sticker(self, chat_id: str, sticker: Sticker) -> SendResult:
        """Send one catalogue sticker (a ``TIMFaceElem``) through the chat's send slot."""
        try:
            target = parse_chat(chat_id)
        except ValueError as exc:
            return self._failure(exc)
        try:
            async with self.slots.hold(chat_id):
                message_id = await self.deliver(target, [frames.face_element(sticker.face_data())])
        except Exception as exc:
            return self._scrubbed(self._failure(exc))
        return SendResult.sent(message_id)

    # -- reply heartbeats ("typing") ---------------------------------------------------------------

    async def _beat_once(self, target: Target, value: int) -> None:
        if self._socket is None or not self.bot_id:
            return
        if target.kind == "group":
            method = "send_group_heartbeat"
            body = frames.group_heartbeat(
                from_account=self.bot_id,
                group_code=target.id,
                value=value,
                at_ms=utc_now_ms(self.clock),
            )
        else:
            method = "send_private_heartbeat"
            body = frames.private_heartbeat(
                from_account=self.bot_id, to_account=target.id, value=value
            )
        msg_id = f"hb_{next(self._ids)}"
        try:
            await self._write(frames.request(method, msg_id, frames.BIZ_MODULE, body))
        except ConnectionError as exc:
            log.debug("yuanbao: a reply heartbeat was not sent: %s", exc)

    async def _typing(self, chat_id: str, meta: dict[str, Any]) -> None:
        target = parse_chat(chat_id)
        if self._socket is None or not self.bot_id:
            return
        now = self.clock.monotonic()
        beat = self._beats.get(target.chat_id)
        if beat is None:
            beat = self._beats[target.chat_id] = _Beat(now)
        beat.renewed = now
        await self._beat_once(target, frames.RUNNING)
        if beat.task is None or beat.task.done():
            beat.task = asyncio.ensure_future(self._keep_beating(target, beat))

    async def _keep_beating(self, target: Target, beat: _Beat) -> None:
        """RUNNING every 2 s until 30 s pass without a renewal (then FINISH)."""
        while True:
            await self.clock.sleep(REPLY_BEAT_S)
            if self._beats.get(target.chat_id) is not beat:
                return
            if self.clock.monotonic() - beat.renewed > REPLY_BEAT_IDLE_S or self._socket is None:
                self._beats.pop(target.chat_id, None)
                await self._beat_once(target, frames.FINISH)
                return
            await self._beat_once(target, frames.RUNNING)

    async def _typing_stop(self, chat_id: str) -> None:
        """Stop the RUNNING heartbeat; FINISH follows the delivered reply."""
        target = parse_chat(chat_id)
        beat = self._beats.pop(target.chat_id, None)
        if beat is not None and beat.task is not None and not beat.task.done():
            beat.task.cancel()

    # -- group queries (the P16-22 tools) ----------------------------------------------------------

    async def group_info(self, group_code: str) -> dict[str, Any]:
        """The group's name, owner and size (`YuanbaoRefused` / `TimeoutError` /
        `ConnectionError` on failure)."""
        data = await self._business(
            "query_group_info", "qgi", frames.group_info_query(group_code), QUERY_TIMEOUT_S
        )
        return frames.group_info_answer(data)

    async def group_members(
        self, group_code: str, *, offset: int = 0, limit: int = 200
    ) -> dict[str, Any]:
        """One page of the group's members; a first page also fills the mention cache."""
        data = await self._business(
            "get_group_member_list",
            "gml",
            frames.member_list_query(group_code, offset, limit),
            QUERY_TIMEOUT_S,
        )
        answer = frames.member_list_answer(data)
        if offset == 0:
            self._members.pop(group_code, None)
            self._members[group_code] = _Members(self.clock.monotonic(), list(answer["members"]))
            while len(self._members) > REMEMBERED:
                self._members.popitem(last=False)
        return answer

    async def _chat_info(self, chat_id: str) -> ChatInfo:
        target = parse_chat(chat_id)
        return ChatInfo(name=chat_id, type="group" if target.kind == "group" else "dm")


def _custom_text(data: Mapping[str, Any]) -> str:
    """Text for a custom element other than a mention (shared links, cards, chat records)."""
    kind = data.get("elem_type")
    if kind == 1010:
        title = str(data.get("title") or "").strip()
        link = str(data.get("link") or "").strip()
        label = " ".join(part for part in (title, link) if part)
        return f"[shared link: {label}]" if label else "[shared link]"
    if kind == 1009:
        return str(data.get("text") or "[forwarded chat history]")
    if kind == 1007:
        inner = json_data(str(data.get("content") or "")) or {}
        link = str(inner.get("link") or "").strip()
        return f"[link: {link}]" if link else "[link]"
    text = str(data.get("text") or "").strip()
    return text or "[unsupported message]"
