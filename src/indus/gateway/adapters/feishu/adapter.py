"""The Feishu / Lark adapter (P16-14): the Open Platform REST API for everything we send, the long
connection (default) or an event webhook for what we receive, an app id and secret.

Connecting fetches a tenant token (a refused app id or secret is a configuration problem, not
retried), reads the bot's own open id for mention detection, then opens the inbound transport:

- ``websocket``: ask ``/callback/ws/endpoint`` for a socket URL and open it (httpx-ws, the
  ``feishu`` extra). Connect waits for the first link, three attempts 1 s and 2 s apart. Data
  frames are acknowledged at once and their events handled in arrival order by one worker; a ping
  goes out every ``PingInterval`` (from the server, 120 s by default); a lost link is opened again
  after 2 s, doubling to 60 s with up to 20 % jitter. A refused app later on is reported through
  `Adapter.report_fatal` (``feishu_auth``), a missing extra as ``missing_dependency``.
- ``webhook``: a listener from the P16-01 kit (loopback by default, port 8765,
  ``/feishu/webhook``, 1 MiB). Signature on the raw body first, then decryption, then the
  verification token, then the challenge or the event (see ``_webhook``).

A send-only adapter (``AdapterConfig.send_only``, ``indus send``) fetches the token and nothing
else. Events that arrive before the daemon installs its sink wait (up to 1,000) and are handed on
when it does.

Inbound and outbound shapes, batching and errors: see the package notes in ``content``,
``batching``, ``comments`` and ``client``; the task note ``docs/notes/P16/feishu.md`` has the list.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import mimetypes
import random
from collections import OrderedDict, deque
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal, cast
from urllib.parse import quote, unquote

import httpx

from indus.foundation.blocking import run_blocking
from indus.foundation.clock import Clock
from indus.foundation.digits import is_digits
from indus.gateway.adapters._http import (
    CallbackListener,
    CallbackRequest,
    ListenerSettings,
    ReplayWindow,
    Reply,
    SeenIds,
    constant_time_equal,
    failure_from_exception,
)
from indus.gateway.adapters.base import (
    REPLY_TO_KEY,
    Adapter,
    AdapterConnectError,
    ApprovalPrompt,
    Capabilities,
    ChatInfo,
    ClarifyPrompt,
    FetcherSeat,
    InboundSink,
    MediaKind,
    approval_callback,
    clarify_callback,
)
from indus.gateway.adapters.feishu import comments
from indus.gateway.adapters.feishu.batching import Batcher, BatchSettings
from indus.gateway.adapters.feishu.client import (
    INSTALL_HINT,
    Endpoint,
    FeishuClient,
    FeishuError,
    SocketLike,
    SocketOpener,
    base_url_for,
    httpx_ws_opener,
)
from indus.gateway.adapters.feishu.content import (
    ButtonStyle,
    Resource,
    button_data,
    card,
    has_markdown,
    media_post,
    outbound_content,
    parse_content,
    post_content,
    text_content,
)
from indus.gateway.adapters.feishu.crypto import DecryptError, decrypt, verify_signature
from indus.gateway.adapters.feishu.frames import (
    BIZ_RT,
    METHOD_CONTROL,
    METHOD_DATA,
    Frame,
    FrameError,
    PartAssembler,
    decode_frame,
    encode_frame,
    ping_frame,
)
from indus.gateway.core.events import Attachment, ChatType, InboundEvent, MessageKind, Source
from indus.gateway.core.send import SendResult, is_ambiguous_timeout
from indus.gateway.outbound.chunker import split_message

if TYPE_CHECKING:
    from indus.gateway.config import PlatformSettings

__all__ = [
    "APP_ID_ENV",
    "APP_SECRET_ENV",
    "ENCRYPT_KEY_ENV",
    "MAX_MESSAGE_CHARS",
    "VERIFICATION_TOKEN_ENV",
    "FeishuAdapter",
    "FeishuOptions",
    "parse_resource_ref",
    "resource_ref",
]

log = logging.getLogger(__name__)

APP_ID_ENV = "FEISHU_APP_ID"
APP_SECRET_ENV = "FEISHU_APP_SECRET"
ENCRYPT_KEY_ENV = "FEISHU_ENCRYPT_KEY"
VERIFICATION_TOKEN_ENV = "FEISHU_VERIFICATION_TOKEN"
DOMAIN_ENV = "FEISHU_DOMAIN"
MODE_ENV = "FEISHU_CONNECTION_MODE"

MAX_MESSAGE_CHARS = 8000
CONNECT_ATTEMPTS = 3
BACKOFF_FIRST_S = 2.0
BACKOFF_CAP_S = 60.0
BACKOFF_JITTER = 0.2
DEFAULT_PING_S = 120.0
DEFAULT_MAX_MEDIA_BYTES = 100 * 1024 * 1024
WEBHOOK_PORT = 8765
WEBHOOK_PATH = "/feishu/webhook"
WEBHOOK_MAX_BODY = 1024 * 1024
WEBHOOK_WINDOW_S = 300.0
SEEN_TTL_S = 24 * 3600.0
SEEN_MAX = 2048
INBOX_MAX = 1000
EARLY_MAX = 1000
CACHE_MAX = 1000
NAME_TTL_S = 600.0
QUOTE_CACHE = 512
RESOURCE_SCHEME = "feishu://resource/"
REACTION_WORKING = "Typing"
REACTION_FAILED = "CrossMark"
APPROVAL_TITLE = "Approval needed"
QUESTION_TITLE = "Question"

MESSAGE_EVENT = "im.message.receive_v1"
CARD_EVENT = "card.action.trigger"
_MEMBERSHIP_EVENTS = frozenset({"im.chat.member.bot.added_v1", "im.chat.member.bot.deleted_v1"})
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_ID_TYPES: tuple[tuple[str, str], ...] = (("ou_", "open_id"), ("on_", "union_id"))
_FILE_TYPES: Mapping[str, tuple[str, str]] = {
    ".opus": ("opus", "audio"),
    ".ogg": ("opus", "audio"),
    ".mp4": ("mp4", "media"),
    ".mov": ("mp4", "media"),
    ".m4v": ("mp4", "media"),
    ".avi": ("mp4", "media"),
    ".pdf": ("pdf", "file"),
    ".doc": ("doc", "file"),
    ".docx": ("doc", "file"),
    ".xls": ("xls", "file"),
    ".xlsx": ("xls", "file"),
    ".ppt": ("ppt", "file"),
    ".pptx": ("ppt", "file"),
}
_DEFAULT_MIME: Mapping[str, str] = {
    "image": "image/jpeg",
    "audio": "audio/ogg",
    "media": "video/mp4",
    "file": "application/octet-stream",
}

type ConnectionMode = Literal["websocket", "webhook"]


def _truthy(value: object, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in _TRUTHY


def _number(value: object, default: float, *, low: float, high: float) -> float:
    try:
        found = float(str(value)) if value not in (None, "") else default
    except ValueError:
        return default
    return min(max(found, low), high)


@dataclass(frozen=True)
class FeishuOptions:
    """``platforms.feishu.extra`` settings (all optional)."""

    domain: str = "feishu"
    connection_mode: str = "websocket"
    reactions: bool = True
    user_names: bool = True
    max_media_bytes: int = DEFAULT_MAX_MEDIA_BYTES
    batching: BatchSettings = BatchSettings()

    @classmethod
    def read(cls, extra: Mapping[str, Any], env: Mapping[str, str]) -> FeishuOptions:
        domain = str(extra.get("domain") or env.get(DOMAIN_ENV) or "feishu").strip().lower()
        mode = str(extra.get("connection_mode") or env.get(MODE_ENV) or "websocket")
        defaults = BatchSettings()
        batching = BatchSettings(
            text_delay_s=_number(
                extra.get("text_batch_delay_seconds"), defaults.text_delay_s, low=0.0, high=10.0
            ),
            text_max_messages=int(
                _number(
                    extra.get("text_batch_max_messages"),
                    defaults.text_max_messages,
                    low=1,
                    high=100,
                )
            ),
            text_max_chars=int(
                _number(
                    extra.get("text_batch_max_chars"), defaults.text_max_chars, low=1, high=50_000
                )
            ),
            media_delay_s=_number(
                extra.get("media_batch_delay_seconds"), defaults.media_delay_s, low=0.0, high=10.0
            ),
        )
        return cls(
            domain=domain,
            connection_mode=mode.strip().lower(),
            reactions=_truthy(extra.get("reactions"), True),
            user_names=_truthy(extra.get("user_names"), True),
            max_media_bytes=int(
                _number(
                    extra.get("max_media_bytes"),
                    DEFAULT_MAX_MEDIA_BYTES,
                    low=1,
                    high=float(DEFAULT_MAX_MEDIA_BYTES),
                )
            ),
            batching=batching,
        )

    def problem(self, *, verification_token: str) -> str | None:
        """A configuration the adapter cannot run with (None: fine)."""
        try:
            base_url_for(self.domain)
        except ValueError as exc:
            return str(exc)
        if self.connection_mode not in ("websocket", "webhook"):
            return f"connection_mode {self.connection_mode!r} is not websocket or webhook"
        if self.connection_mode == "webhook" and not verification_token:
            return f"webhook mode needs {VERIFICATION_TOKEN_ENV}"
        return None

    @property
    def locale(self) -> str:
        return "en_us" if self.domain == "lark" else "zh_cn"


def resource_ref(message_id: str, resource: Resource) -> str:
    """The attachment ref of one message resource (the name keeps the intake's file type)."""
    parts = (message_id, resource.request_type, resource.key, resource.name or resource.key)
    return RESOURCE_SCHEME + "/".join(quote(p, safe="") for p in parts)


def parse_resource_ref(ref: str) -> tuple[str, str, str] | None:
    """(message_id, request type, key) of a resource ref; None for any other ref."""
    if not ref.startswith(RESOURCE_SCHEME):
        return None
    parts = [unquote(p) for p in ref[len(RESOURCE_SCHEME) :].split("/")]
    if len(parts) < 3 or not all(parts[:3]) or parts[1] not in ("image", "file"):
        return None
    return parts[0], parts[1], parts[2]


def _object(value: object) -> dict[str, Any]:
    return dict(cast(Mapping[str, Any], value)) if isinstance(value, Mapping) else {}


def _receive_type(chat_id: str) -> str:
    for prefix, kind in _ID_TYPES:
        if chat_id.startswith(prefix):
            return kind
    return "chat_id"


class _Lru[V]:
    def __init__(self, size: int = CACHE_MAX) -> None:
        self.size = size
        self._items: OrderedDict[str, V] = OrderedDict()

    def get(self, key: str) -> V | None:
        value = self._items.get(key)
        if value is not None:
            self._items.move_to_end(key)
        return value

    def put(self, key: str, value: V) -> None:
        self._items[key] = value
        self._items.move_to_end(key)
        while len(self._items) > self.size:
            self._items.popitem(last=False)

    def pop(self, key: str) -> V | None:
        return self._items.pop(key, None)

    def __contains__(self, key: object) -> bool:
        return key in self._items


class FeishuAdapter(Adapter):
    platform: ClassVar[str] = "feishu"
    capabilities: ClassVar[Capabilities] = Capabilities(
        max_len=MAX_MESSAGE_CHARS,
        markup="markdown",
        splits_natively=True,
        supports_edit=True,
        supports_delete=True,
        supports_buttons=True,
        dm_is_private=True,
        push_capable=True,
    )
    vendor_names: ClassVar[Mapping[str, tuple[str, ...]]] = {
        # Our own REST client (no SDK); its routes are ``client.ROUTES``, served by the wire fake.
        "indus.gateway.adapters.feishu.client:FeishuClient": (
            "tenant_token",
            "bot_info",
            "ws_endpoint",
            "create_message",
            "reply_message",
            "update_message",
            "patch_card",
            "delete_message",
            "get_message",
            "get_chat",
            "get_user",
            "add_reaction",
            "delete_reaction",
            "upload_image",
            "upload_file",
            "download",
            "get_comment",
            "reply_comment",
            "add_comment",
        ),
        "httpx_ws:AsyncWebSocketSession": ("receive_bytes", "send_bytes", "close"),
    }

    def __init__(
        self,
        *,
        clock: Clock,
        app_id: str,
        app_secret: str,
        settings: PlatformSettings | None = None,
        options: FeishuOptions | None = None,
        encrypt_key: str = "",
        verification_token: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
        proxy: str | None = None,
        opener: SocketOpener | None = None,
        rng: Callable[[], float] = random.random,
        send_only: bool = False,
        serve: bool = True,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(
            clock=clock,
            settings=settings,
            secrets=(app_secret, encrypt_key, verification_token),
        )
        self.send_only = send_only
        self._app_id = app_id.strip()
        self._app_secret = app_secret.strip()
        self._encrypt_key = encrypt_key.strip()
        self._verification_token = verification_token.strip()
        self.options = options or FeishuOptions()
        self._transport = transport
        self._proxy = proxy
        self._opener: SocketOpener = opener or httpx_ws_opener(proxy)
        self._rng = rng
        self.bot_open_id = ""
        self.bot_name = ""
        self.link_opens = 0
        self.client: FeishuClient | None = None
        self._link: asyncio.Task[None] | None = None
        self._link_up = asyncio.Event()
        self._link_error: BaseException | None = None
        self._worker: asyncio.Task[None] | None = None
        self._inbox: deque[tuple[str, dict[str, Any]]] = deque()
        self._inbox_ready = asyncio.Event()
        self._early: deque[InboundEvent] = deque()
        self._parts = PartAssembler()
        self._seen_messages = SeenIds(clock, SEEN_TTL_S, SEEN_MAX)
        self._seen_events = SeenIds(clock, SEEN_TTL_S, SEEN_MAX * 2)
        self._replay = ReplayWindow(clock, WEBHOOK_WINDOW_S)
        self._chats: _Lru[tuple[str, ChatType]] = _Lru()
        self._names: _Lru[tuple[str, float]] = _Lru()
        self._threads: _Lru[str] = _Lru()
        self._quotes: _Lru[str] = _Lru(QUOTE_CACHE)
        # Card message id → the thread it was posted in ("" outside threads).
        self._cards: _Lru[str] = _Lru()
        self._reactions: _Lru[str] = _Lru()
        self.batcher = Batcher(clock, self._hand_on, self.options.batching)
        self.listener: CallbackListener | None = None
        if self.options.connection_mode == "webhook" and not send_only:
            settings_ = ListenerSettings.from_extra(
                extra or (settings.extra if settings is not None else {}),
                port=WEBHOOK_PORT,
                max_body_bytes=WEBHOOK_MAX_BODY,
                path=WEBHOOK_PATH,
            )
            self.listener = CallbackListener(
                settings_, name=self.platform, clock=clock, scrub=self.scrub, serve=serve
            )
            self.listener.route(settings_.path, self._webhook, methods=("POST",))

    def __repr__(self) -> str:
        state = "connected" if self.connected else "disconnected"
        return f"<FeishuAdapter {self.options.domain} {self.options.connection_mode} {state}>"

    def _remember_secret(self, value: str) -> None:
        if value and value not in self._secrets:
            # Keep the static credentials plus the few newest tokens / socket URLs.
            self._secrets = (*self._secrets[:3], *self._secrets[3:][-7:], value)

    # -- lifecycle ---------------------------------------------------------------------------------

    def _check_config(self) -> None:
        missing = [
            name
            for name, value in ((APP_ID_ENV, self._app_id), (APP_SECRET_ENV, self._app_secret))
            if not value
        ]
        if missing:
            raise AdapterConnectError(
                "Feishu is not configured: set " + " and ".join(missing),
                retryable=False,
                code="config",
            )
        problem = self.options.problem(verification_token=self._verification_token)
        if problem is not None:
            raise AdapterConnectError(problem, retryable=False, code="config")

    async def _open(self, is_reconnect: bool) -> None:
        self._check_config()
        client = FeishuClient(
            base_url_for(self.options.domain),
            self._app_id,
            self._app_secret,
            clock=self.clock,
            transport=self._transport,
            proxy=self._proxy,
            on_secret=self._remember_secret,
        )
        try:
            await client.tenant_token(fresh=True)
            if not self.send_only:
                await self._who_am_i(client)
        except FeishuError as exc:
            await client.aclose()
            if exc.is_auth:
                raise AdapterConnectError(
                    f"Feishu refused the app credentials: {exc}",
                    retryable=False,
                    code="auth_failed",
                ) from None
            raise AdapterConnectError(str(exc), retryable=True) from None
        except httpx.HTTPError as exc:
            await client.aclose()
            raise AdapterConnectError(
                failure_from_exception(exc).error or "connect error", retryable=True
            ) from None
        except BaseException:
            await client.aclose()
            raise
        self.client = client
        if self.send_only:
            log.info("feishu: ready to send (%s)", self.options.domain)
            return
        self._worker = asyncio.ensure_future(self._work())
        try:
            if self.listener is not None:
                await self.listener.start()
            else:
                await self._start_link()
        except BaseException:
            await self._shutdown()
            raise
        log.info(
            "feishu: connected as %s (%s, %s)",
            self.bot_name or "?",
            self.options.domain,
            self.options.connection_mode,
        )

    async def _who_am_i(self, client: FeishuClient) -> None:
        try:
            bot = await client.bot_info()
        except FeishuError as exc:
            if exc.is_auth:
                raise
            log.warning("feishu: could not read the bot's own id: %s", self.scrub(str(exc)))
            return
        self.bot_open_id = str(bot.get("open_id") or "")
        self.bot_name = str(bot.get("app_name") or "")

    async def _start_link(self) -> None:
        self._link_up.clear()
        self._link_error = None
        self._link = asyncio.ensure_future(self._run_link())
        waiter = asyncio.ensure_future(self._link_up.wait())
        done, _ = await asyncio.wait({waiter, self._link}, return_when=asyncio.FIRST_COMPLETED)
        if waiter not in done:
            waiter.cancel()
        if self._link_up.is_set() and self._link_error is None:
            return
        error = self._link_error
        if isinstance(error, FeishuError) and error.is_auth:
            raise AdapterConnectError(
                f"Feishu refused the app credentials: {error}",
                retryable=False,
                code="auth_failed",
            )
        if isinstance(error, ModuleNotFoundError):
            raise AdapterConnectError(
                f"the Feishu long connection needs httpx-ws: {INSTALL_HINT}",
                retryable=False,
                code="missing_dependency",
            )
        detail = self.scrub(f"{type(error).__name__}: {error}") if error else "no link"
        raise AdapterConnectError(
            f"the Feishu long connection did not open after {CONNECT_ATTEMPTS} attempts: {detail}",
            retryable=True,
        )

    async def _close(self) -> None:
        await self._shutdown()

    async def _shutdown(self) -> None:
        if self.listener is not None:
            await self.listener.stop()
        link, self._link = self._link, None
        if link is not None:
            link.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await link
        await self._drain_inbox()
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await worker
        with contextlib.suppress(Exception):
            await self.batcher.aclose()
        client, self.client = self.client, None
        if client is not None:
            await client.aclose()

    def set_sink(self, sink: InboundSink | None) -> None:
        """Installing a sink hands on the events that arrived before it."""
        super().set_sink(sink)
        if sink is not None and self._early:
            waiting = list(self._early)
            self._early.clear()
            asyncio.ensure_future(self._replay_early(waiting))

    async def _replay_early(self, events: list[InboundEvent]) -> None:
        for event in events:
            await self.emit(event)

    async def _hand_on(self, event: InboundEvent) -> None:
        if self._sink is None:
            if len(self._early) >= EARLY_MAX:
                self._early.popleft()
                log.warning("feishu: too many events before the gateway was ready; oldest dropped")
            self._early.append(event)
            return
        await self.emit(event)

    def is_final_failure(self, result: SendResult) -> bool:
        """Refused credentials: no retry and no plain-text resend can help."""
        return "unauthorized" in (result.error or "")

    def _require_client(self) -> FeishuClient:
        if self.client is None:
            raise ConnectionError("not connected to Feishu")
        return self.client

    # -- the long connection -----------------------------------------------------------------------

    async def _run_link(self) -> None:
        delay = BACKOFF_FIRST_S
        attempts = 0
        while True:
            client = self.client
            if client is None:
                return
            try:
                endpoint = await client.ws_endpoint()
                async with self._opener(endpoint.url) as socket:
                    self.link_opens += 1
                    self._link_up.set()
                    attempts = 0
                    delay = BACKOFF_FIRST_S
                    log.info("feishu: long connection open")
                    await self._serve(socket, endpoint)
                failure: BaseException | None = ConnectionError("the server closed the link")
            except asyncio.CancelledError:
                raise
            except ModuleNotFoundError as exc:
                if not self._link_up.is_set():
                    self._link_error = exc
                    return
                log.error("feishu: the long connection needs httpx-ws: %s", INSTALL_HINT)
                self.report_fatal(
                    "missing_dependency", f"the long connection needs httpx-ws ({INSTALL_HINT})"
                )
                return
            except FeishuError as exc:
                if exc.is_auth:
                    if not self._link_up.is_set():
                        self._link_error = exc
                        return
                    log.error("feishu: the app credentials were refused; giving up")
                    self.report_fatal("feishu_auth", f"Feishu refused the app credentials: {exc}")
                    return
                failure = exc
            except Exception as exc:
                failure = exc
            text = self.scrub(f"{type(failure).__name__}: {failure}")
            if not self._link_up.is_set():
                attempts += 1
                if attempts >= CONNECT_ATTEMPTS:
                    self._link_error = failure
                    return
                log.warning(
                    "feishu: link attempt %d of %d failed (%s)", attempts, CONNECT_ATTEMPTS, text
                )
                await self.clock.sleep(float(2 ** (attempts - 1)))
                continue
            log.warning("feishu: long connection lost (%s); reopening in %.0f s", text, delay)
            await self.clock.sleep(delay * (1 + BACKOFF_JITTER * self._rng()))
            delay = min(delay * 2, BACKOFF_CAP_S)

    async def _serve(self, socket: SocketLike, endpoint: Endpoint) -> None:
        service = endpoint.service_id
        interval = [endpoint.number("PingInterval", DEFAULT_PING_S)]
        pinger = asyncio.ensure_future(self._ping(socket, service, interval))
        try:
            while True:
                data = await socket.receive_bytes()
                await self._on_frame(socket, data, interval)
        finally:
            pinger.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pinger

    async def _ping(self, socket: SocketLike, service: int, interval: list[float]) -> None:
        while True:
            await self.clock.sleep(interval[0])
            try:
                await socket.send_bytes(encode_frame(ping_frame(service)))
            except Exception as exc:
                log.debug("feishu: ping failed: %s", type(exc).__name__)
                return

    async def _on_frame(self, socket: SocketLike, data: bytes, interval: list[float]) -> None:
        try:
            frame = decode_frame(data)
        except FrameError as exc:
            log.debug("feishu: unreadable frame dropped: %s", exc)
            return
        if frame.method == METHOD_CONTROL:
            if frame.kind == "pong" and frame.payload:
                with contextlib.suppress(ValueError, UnicodeDecodeError):
                    config = _object(json.loads(frame.payload))
                    found = config.get("PingInterval")
                    if isinstance(found, int | float) and not isinstance(found, bool) and found > 0:
                        interval[0] = float(found)
            return
        if frame.method != METHOD_DATA:
            return
        payload = self._parts.add(frame, self.clock.monotonic())
        if payload is None:
            return
        started = self.clock.monotonic()
        try:
            body = _object(json.loads(payload))
        except (ValueError, UnicodeDecodeError):
            body = {}
        if body:
            self._enqueue("card" if frame.kind == "card" else "event", body)
        elapsed_ms = int((self.clock.monotonic() - started) * 1000)
        reply = frame.with_header(BIZ_RT, str(elapsed_ms))
        ack = Frame(
            seq_id=reply.seq_id,
            log_id=reply.log_id,
            service=reply.service,
            method=reply.method,
            headers=reply.headers,
            payload=json.dumps({"code": 200}).encode(),
            log_id_new=reply.log_id_new,
        )
        try:
            await socket.send_bytes(encode_frame(ack))
        except Exception as exc:
            log.debug("feishu: acknowledgement failed: %s", type(exc).__name__)

    # -- webhook -----------------------------------------------------------------------------------

    async def _webhook(self, request: CallbackRequest) -> Reply:
        kind = (request.header("content-type") or "application/json").split(";")[0].strip()
        if kind.lower() != "application/json":
            return Reply.json({"msg": "JSON only"}, 415)
        offered = request.header("x-lark-signature")
        signed = False
        if self._encrypt_key and offered is not None:
            timestamp = request.header("x-lark-request-timestamp")
            nonce = request.header("x-lark-request-nonce")
            if not verify_signature(timestamp, nonce, self._encrypt_key, request.body, offered):
                return Reply.json({"msg": "bad signature"}, 401)
            if not self._replay.fresh(timestamp):
                return Reply.json({"msg": "stale request"}, 401)
            signed = True
        try:
            body = _object(request.json())
        except ValueError:
            return Reply.json({"msg": "bad request"}, 400)
        encrypted = body.get("encrypt")
        if self._encrypt_key:
            if not isinstance(encrypted, str):
                return Reply.json({"msg": "an encrypted body is required"}, 400)
            try:
                body = _object(json.loads(decrypt(self._encrypt_key, encrypted)))
            except (DecryptError, ValueError, UnicodeDecodeError):
                return Reply.json({"msg": "bad request"}, 400)
        elif encrypted is not None:
            return Reply.json({"msg": "no encrypt key is configured"}, 400)
        header = _object(body.get("header"))
        token = str(header.get("token") or body.get("token") or "")
        if not _same(token, self._verification_token):
            return Reply.json({"msg": "bad token"}, 401)
        if body.get("type") == "url_verification":
            return Reply.json({"challenge": str(body.get("challenge") or "")})
        if self._encrypt_key and not signed:
            return Reply.json({"msg": "unsigned request"}, 401)
        event_type = str(header.get("event_type") or "")
        self._enqueue("card" if event_type == CARD_EVENT else "event", body)
        return Reply.json({})

    # -- inbound -----------------------------------------------------------------------------------

    def _enqueue(self, kind: str, body: dict[str, Any]) -> None:
        header = _object(body.get("header"))
        event_id = str(header.get("event_id") or "")
        if event_id and not self._seen_events.first_time(event_id):
            return
        if len(self._inbox) >= INBOX_MAX:
            self._inbox.popleft()
            log.warning("feishu: inbound queue full; the oldest event was dropped")
        self._inbox.append((kind, body))
        self._inbox_ready.set()

    async def _work(self) -> None:
        while True:
            if not self._inbox:
                self._inbox_ready.clear()
                await self._inbox_ready.wait()
                continue
            kind, body = self._inbox.popleft()
            try:
                await self._dispatch(kind, body)
            except Exception:
                log.exception("feishu: handling an inbound event failed")

    async def _drain_inbox(self) -> None:
        while self._inbox:
            kind, body = self._inbox.popleft()
            with contextlib.suppress(Exception):
                await self._dispatch(kind, body)

    async def _dispatch(self, kind: str, body: dict[str, Any]) -> None:
        header = _object(body.get("header"))
        event_type = str(header.get("event_type") or "")
        event = _object(body.get("event"))
        if kind == "card" or event_type == CARD_EVENT:
            found = await self._card_action(event)
        elif event_type == MESSAGE_EVENT:
            found = await self._message(event)
        elif event_type == comments.COMMENT_EVENT:
            found = await self._comment(event)
        else:
            if event_type in _MEMBERSHIP_EVENTS:
                self._chats.pop(str(event.get("chat_id") or ""))
            log.debug("feishu: event %s ignored", event_type or "?")
            return
        if found is not None:
            await self.batcher.offer(found)

    async def _message(self, event: Mapping[str, Any]) -> InboundEvent | None:
        sender = _object(event.get("sender"))
        message = _object(event.get("message"))
        ids = _object(sender.get("sender_id"))
        open_id = str(ids.get("open_id") or "")
        message_id = str(message.get("message_id") or "")
        chat_id = str(message.get("chat_id") or "")
        if not message_id or not chat_id or not open_id:
            return None
        if self.bot_open_id and open_id == self.bot_open_id:
            return None
        if not self._seen_messages.first_time(message_id):
            return None
        raw_kind = str(message.get("chat_type") or "p2p")
        parsed = parse_content(
            str(message.get("message_type") or ""),
            str(message.get("content") or ""),
            message.get("mentions"),
            bot_open_id=self.bot_open_id,
        )
        if not parsed.text and not parsed.resources:
            return None
        thread_id = str(message.get("thread_id") or "") or None
        chat_type: ChatType = "dm" if raw_kind == "p2p" else "group"
        if thread_id and chat_type != "dm":
            chat_type = "thread"
        known = self._chats.get(chat_id)
        self._chats.put(
            chat_id, (known[0] if known else "", "dm" if raw_kind == "p2p" else "group")
        )
        if thread_id:
            self._threads.put(thread_id, message_id)
        media = tuple(
            Attachment(
                ref=resource_ref(message_id, r),
                mime=mimetypes.guess_type(r.name)[0] or _DEFAULT_MIME[r.kind]
                if r.name
                else _DEFAULT_MIME[r.kind],
            )
            for r in parsed.resources
        )
        kinds = {r.kind for r in parsed.resources}
        kind = MessageKind.TEXT
        if parsed.sticker:
            kind = MessageKind.STICKER
        elif "audio" in kinds:
            kind = MessageKind.VOICE
        elif "media" in kinds:
            kind = MessageKind.VIDEO
        elif "image" in kinds:
            kind = MessageKind.PHOTO
        elif "file" in kinds:
            kind = MessageKind.DOCUMENT
        is_bot = str(sender.get("sender_type") or "user") in ("app", "bot")
        reply_to = str(message.get("parent_id") or message.get("root_id") or "") or None
        metadata: dict[str, Any] = {}
        if parsed.mentioned:
            metadata["mentioned"] = True
        when = None
        created = str(message.get("create_time") or "")
        if is_digits(created):
            when = datetime.fromtimestamp(int(created) / 1000, UTC)
        user_name = await self._user_name(open_id) if not is_bot else None
        chat_name = await self._chat_name(chat_id) if chat_type != "dm" else user_name
        source = Source(
            platform=self.platform,
            chat_id=chat_id,
            chat_type=chat_type,
            chat_name=chat_name or None,
            user_id=open_id,
            user_name=user_name or None,
            thread_id=thread_id,
            user_id_alt=str(ids.get("union_id") or "") or None,
            is_bot=is_bot,
            parent_chat_id=chat_id if chat_type == "thread" else None,
        )
        return InboundEvent(
            source=source,
            text=parsed.text,
            kind=kind,
            message_id=message_id,
            media=media,
            reply_to_id=reply_to,
            reply_to_text=await self._quoted(reply_to),
            metadata=metadata,
            timestamp=when,
        )

    async def _card_action(self, event: Mapping[str, Any]) -> InboundEvent | None:
        action = _object(event.get("action"))
        data = button_data(action.get("value"))
        if data is None:
            log.debug("feishu: a press on a card we did not make ignored")
            return None
        operator = _object(event.get("operator"))
        context = _object(event.get("context"))
        open_id = str(operator.get("open_id") or "")
        chat_id = str(context.get("open_chat_id") or "")
        card_id = str(context.get("open_message_id") or "")
        if not open_id or not chat_id:
            return None
        token = str(event.get("token") or "")
        if token and not self._seen_messages.first_time(f"card:{token}"):
            return None
        chat_type = await self._chat_type(chat_id)
        thread_id = self._cards.get(card_id) or None
        if thread_id and chat_type != "dm":
            chat_type = "thread"
        source = Source(
            platform=self.platform,
            chat_id=chat_id,
            chat_type=chat_type,
            thread_id=thread_id,
            parent_chat_id=chat_id if chat_type == "thread" else None,
            user_id=open_id,
            user_name=(await self._user_name(open_id)) or None,
            user_id_alt=str(operator.get("union_id") or "") or None,
        )
        metadata: dict[str, Any] = {"button": data}
        if card_id:
            metadata["button_message_id"] = card_id
        return InboundEvent(source=source, text="", message_id=token or None, metadata=metadata)

    async def _comment(self, event: Mapping[str, Any]) -> InboundEvent | None:
        notice = comments.parse_notice(event)
        if notice is None:
            return None
        why = comments.wanted(notice, self.bot_open_id)
        if why is not None:
            log.debug("feishu: comment notice skipped: %s", why)
            return None
        key = f"comment:{notice.reply_id or notice.comment_id}"
        if not self._seen_messages.first_time(key):
            return None
        client = self.client
        if client is None:
            return None
        return await comments.event_for(
            client, notice, bot_open_id=self.bot_open_id, platform=self.platform
        )

    async def _quoted(self, message_id: str | None) -> str | None:
        """The text of the message a reply answers (cached; None when it cannot be read)."""
        if not message_id or self.client is None:
            return None
        cached = self._quotes.get(message_id)
        if cached is not None:
            return cached or None
        text = ""
        try:
            found = await self.client.get_message(message_id)
            body = _object(found.get("body"))
            parsed = parse_content(
                str(found.get("msg_type") or ""),
                str(body.get("content") or ""),
                found.get("mentions"),
                bot_open_id=self.bot_open_id,
            )
            text = parsed.text
        except Exception as exc:
            log.debug("feishu: the replied-to message could not be read: %s", type(exc).__name__)
        self._quotes.put(message_id, text)
        return text or None

    async def _user_name(self, open_id: str) -> str:
        if not self.options.user_names or not open_id or self.client is None:
            return ""
        cached = self._names.get(open_id)
        now = self.clock.monotonic()
        if cached is not None and now - cached[1] < NAME_TTL_S:
            return cached[0]
        name = ""
        try:
            user = await self.client.get_user(open_id)
            name = str(user.get("name") or user.get("en_name") or "")
        except Exception as exc:
            log.debug("feishu: no name for a sender: %s", type(exc).__name__)
        self._names.put(open_id, (name, now))
        return name

    async def _chat_name(self, chat_id: str) -> str:
        found = self._chats.get(chat_id)
        if found is not None and found[0]:
            return found[0]
        info = await self.chat_info(chat_id)
        return info.name if info.name != chat_id else ""

    async def _chat_type(self, chat_id: str) -> ChatType:
        found = self._chats.get(chat_id)
        if found is not None:
            return found[1]
        info = await self.chat_info(chat_id)
        return info.type or "group"

    # -- media for the intake ----------------------------------------------------------------------

    async def fetch_media(self, attachment: Attachment) -> AsyncIterator[bytes]:
        """The bytes of an inbound attachment (registered by `register_fetchers`)."""
        found = parse_resource_ref(attachment.ref)
        if found is None:
            raise ValueError("not a Feishu resource reference")
        message_id, request_type, key = found
        client = self._require_client()
        async for chunk in client.download(
            message_id, key, request_type, cap=self.options.max_media_bytes
        ):
            yield chunk

    def register_fetchers(self, seat: FetcherSeat) -> Callable[[], None] | None:
        """Register `fetch_media` with the media intake (P15-23); returns the undo."""
        return seat.register_fetcher(self.platform, self.fetch_media)

    # -- outbound ----------------------------------------------------------------------------------

    def _target(self, reply_to: str | None, meta: Mapping[str, Any]) -> tuple[str | None, bool]:
        """(message to reply to, inside its thread) for a send."""
        thread = str(meta.get("thread_id") or "")
        if thread:
            return reply_to or self._threads.get(thread), True
        return reply_to, False

    async def _post(
        self,
        client: FeishuClient,
        chat_id: str,
        msg_type: str,
        content: str,
        target: str | None,
        in_thread: bool,
    ) -> str:
        if target:
            try:
                data = await client.reply_message(target, msg_type, content, in_thread=in_thread)
                return str(data.get("message_id") or "")
            except FeishuError as exc:
                # The message we answer was withdrawn: post in the chat instead (never outside a
                # thread we were asked to stay in).
                if exc.reading != "message not found" or in_thread:
                    raise
        data = await client.create_message(_receive_type(chat_id), chat_id, msg_type, content)
        return str(data.get("message_id") or "")

    async def _send(
        self, chat_id: str, text: str, reply_to: str | None, meta: dict[str, Any]
    ) -> SendResult:
        client = self._require_client()
        if comments.parse_chat_id(chat_id) is not None:
            return await comments.send_reply(client, chat_id, meta.get("thread_id"), text)
        chunked = split_message(text, MAX_MESSAGE_CHARS)
        # One format for every piece: a piece of a Markdown reply may look like prose on its own.
        as_post = meta.get("plain") is not True and has_markdown(text)
        target, in_thread = self._target(reply_to, meta)
        sent: list[str] = []
        for n, chunk in enumerate(chunked.chunks):
            if n:
                await self.slots.pace(chat_id)
            if as_post:
                msg_type, content = "post", post_content(chunk.text, locale=self.options.locale)
            else:
                msg_type, content = "text", text_content(chunk.text)
            try:
                message_id = await self._post(
                    client, chat_id, msg_type, content, target if n == 0 or in_thread else None,
                    in_thread,
                )  # fmt: skip
            except Exception as exc:
                return self._failure(exc, sent, chunked.remainder(n))
            if not message_id:
                return SendResult.failure(
                    "unexpected answer from the server: no message id", delivered_ids=tuple(sent)
                )
            sent.append(message_id)
        return SendResult.sent(sent[-1], tuple(sent[:-1]))

    @staticmethod
    def _failure(exc: BaseException, sent: list[str], remainder: str | None) -> SendResult:
        if isinstance(exc, httpx.HTTPError):
            text = failure_from_exception(exc).error or type(exc).__name__
        else:
            text = f"{type(exc).__name__}: {exc}"
        keep = None if is_ambiguous_timeout(text=text) else remainder
        return SendResult.failure(
            text,
            retry_after=getattr(exc, "retry_after", None),
            delivered_ids=tuple(sent),
            remainder=keep if sent else None,
        )

    async def _edit(
        self, chat_id: str, message_id: str, text: str, final: bool, meta: dict[str, Any]
    ) -> SendResult:
        client = self._require_client()
        if len(text) > MAX_MESSAGE_CHARS:
            return SendResult.failure("message is too long for one Feishu edit")
        try:
            if message_id in self._cards:
                title = str(meta.get("card_title") or "Indus Agent")
                closed = card(text, title=title, template="grey")
                await client.patch_card(message_id, json.dumps(closed, ensure_ascii=False))
            else:
                msg_type, content = outbound_content(
                    text, plain=meta.get("plain") is True, locale=self.options.locale
                )
                try:
                    await client.update_message(message_id, msg_type, content)
                except FeishuError as exc:
                    # A post the server will not take: the same text as plain text.
                    if msg_type != "post" or not exc.reading.startswith("can't parse"):
                        raise
                    await client.update_message(message_id, "text", text_content(text))
        except Exception as exc:
            return self._failure(exc, [], None)
        return SendResult.sent(message_id)

    async def _delete(self, chat_id: str, message_id: str) -> bool:
        await self._require_client().delete_message(message_id)
        self._cards.pop(message_id)
        return True

    async def _send_card(
        self, chat_id: str, body: dict[str, Any], meta: dict[str, Any]
    ) -> SendResult:
        client = self._require_client()
        target, in_thread = self._target(None, meta)
        content = json.dumps(body, ensure_ascii=False)
        try:
            message_id = await self._post(
                client, chat_id, "interactive", content, target, in_thread
            )
        except Exception as exc:
            return self._failure(exc, [], None)
        if not message_id:
            return SendResult.failure("unexpected answer from the server: no message id")
        self._cards.put(message_id, str(meta.get("thread_id") or ""))
        return SendResult.sent(message_id)

    async def _send_approval(
        self, chat_id: str, prompt: ApprovalPrompt, meta: dict[str, Any]
    ) -> SendResult:
        if comments.parse_chat_id(chat_id) is not None:
            return await self._send(chat_id, prompt.fallback_text, None, meta)
        buttons: list[tuple[str, str, ButtonStyle]] = [
            (
                action.label,
                approval_callback(prompt.request_id, action.choice),
                action.style or "default",
            )
            for action in prompt.actions
        ]
        body = card(prompt.text, title=APPROVAL_TITLE, template="orange", buttons=buttons)
        return await self._send_card(chat_id, body, meta)

    async def _send_clarify(
        self, chat_id: str, prompt: ClarifyPrompt, meta: dict[str, Any]
    ) -> SendResult:
        if comments.parse_chat_id(chat_id) is not None:
            return await self._send(chat_id, prompt.fallback_text or prompt.question, None, meta)
        buttons: list[tuple[str, str, ButtonStyle]] = [
            (choice, clarify_callback(prompt.clarify_id, n), "default")
            for n, choice in enumerate(prompt.choices)
        ]
        body = card(prompt.question, title=QUESTION_TITLE, buttons=buttons)
        return await self._send_card(chat_id, body, meta)

    async def _send_media(
        self, kind: MediaKind, chat_id: str, path: str, caption: str | None, meta: dict[str, Any]
    ) -> SendResult:
        client = self._require_client()
        if comments.parse_chat_id(chat_id) is not None:
            return SendResult.refused("files in document comments")
        file = Path(path)
        size = (await run_blocking(file.stat, label="feishu upload")).st_size
        if size > self.options.max_media_bytes:
            return SendResult.failure(
                f"the file is too large for Feishu ({size} bytes, the cap is "
                f"{self.options.max_media_bytes})"
            )
        data = await run_blocking(file.read_bytes, label="feishu upload")
        reply = meta.get(REPLY_TO_KEY)
        target, in_thread = self._target(str(reply) if reply else None, meta)
        locale = self.options.locale
        try:
            if kind == "image":
                key = await client.upload_image(data, name=file.name)
                if caption:
                    msg_type = "post"
                    content = media_post(caption, {"tag": "img", "image_key": key}, locale=locale)
                else:
                    msg_type, content = "image", json.dumps({"image_key": key})
                message_id = await self._post(client, chat_id, msg_type, content, target, in_thread)
                return SendResult.sent(message_id)
            file_type, msg_type = _FILE_TYPES.get(file.suffix.lower(), ("stream", "file"))
            if kind == "voice" and file_type != "opus":
                file_type, msg_type = "stream", "file"
            key = await client.upload_file(data, name=file.name, file_type=file_type)
            content = json.dumps({"file_key": key})
            message_id = await self._post(client, chat_id, msg_type, content, target, in_thread)
            if caption and caption.strip():
                await self.slots.pace(chat_id)
                msg_type, content = outbound_content(caption, plain=False, locale=locale)
                await self._post(client, chat_id, msg_type, content, None, False)
        except Exception as exc:
            return self._failure(exc, [], None)
        return SendResult.sent(message_id)

    async def _chat_info(self, chat_id: str) -> ChatInfo:
        found = comments.parse_chat_id(chat_id)
        if found is not None:
            return ChatInfo(name=f"{found[0]} {found[1]}", type="thread")
        data = await self._require_client().get_chat(chat_id)
        mode = str(data.get("chat_mode") or "")
        kind: ChatType = "dm" if mode == "p2p" else "group"
        name = str(data.get("name") or "")
        self._chats.put(chat_id, (name, kind))
        return ChatInfo(name=name or chat_id, type=kind)

    # -- reactions (processing state) --------------------------------------------------------------

    async def processing_started(self, chat_id: str, message_id: str) -> None:
        if not self.options.reactions or self.client is None:
            return
        try:
            reaction = await self.client.add_reaction(message_id, REACTION_WORKING)
        except Exception as exc:
            log.debug("feishu: reaction failed: %s", self.scrub(str(exc)))
            return
        if reaction:
            self._reactions.put(message_id, reaction)

    async def processing_finished(self, chat_id: str, message_id: str, *, ok: bool) -> None:
        if not self.options.reactions or self.client is None:
            return
        reaction = self._reactions.pop(message_id)
        try:
            if reaction:
                await self.client.delete_reaction(message_id, reaction)
            if not ok:
                await self.client.add_reaction(message_id, REACTION_FAILED)
        except Exception as exc:
            log.debug("feishu: reaction failed: %s", self.scrub(str(exc)))


def _same(offered: str, expected: str) -> bool:
    return bool(expected) and constant_time_equal(offered, expected)
