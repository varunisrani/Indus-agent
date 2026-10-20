"""The DingTalk adapter (P16-16): Stream Mode for what we receive, the conversation's session
webhook (else the robot REST API) for what we send.

Connecting proves the app credentials with an access-token request; unless the adapter is
send-only (``indus send``), a background task then keeps the Stream Mode WebSocket open (our own
client, ``stream.py``): every ``CALLBACK`` frame is acknowledged at once and handled in the
background, ``ping`` frames are answered with their data, a ``disconnect`` frame reopens the stream.
Failures reopen it after 2, 5, 10, 30, 60 s; the fifth identical failure in a row trips a 300 s wait
(and log lines that repeat within 300 s are held back). A refused ``connections/open`` (401/403) is
reported through `Adapter.report_fatal` (``dingtalk_auth``), a missing httpx-ws as
``missing_dependency``.

Inbound (robot topic ``/v1.0/im/bot/messages/get``; one `InboundEvent` per message):

- dropped: repeats (the last 1,000 message ids), empty messages, group messages outside
  ``allowed_chats`` (when set) and, with ``require_mention``, group messages that do not mention the
  bot unless the chat is in ``free_response_chats``; DMs are never gated here;
- ``conversationType`` 1 → dm, 2 → group; the chat id is the conversation id; the user id is the
  sender's staff id (``senderId`` as the alternate id);
- ``text``, ``richText`` (text items joined, pictures as photos), ``picture``, ``audio`` (the
  platform's recognition text plus the voice file), ``file`` and ``video``; attachments are refs
  ``dingtalk://download/<code>/<name>`` that `fetch_media` resolves through
  ``robot/messageFiles/download``.

Outbound: markdown, cut at 20,000 characters, sent through the conversation's newest session
webhook while it is valid (stored per chat, at most 500, dropped 300 s before its stated expiry,
only on ``oapi.dingtalk.com`` / ``api.dingtalk.com``); without one, or when the webhook refuses,
through the robot API: ``groupMessages/send`` for groups, ``oToMessages/batchSend`` for people (a
DM seen before, or a chat id ``user:<staffId>``). No editing, typing or buttons (approvals use the
text card).
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import mimetypes
import re
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import parse_qsl, quote, unquote, urlsplit, urlunsplit

import httpx

from indus import __version__
from indus.foundation.clock import Clock
from indus.foundation.redact import redact_text
from indus.gateway.adapters._http import failure_from_exception, failure_from_status
from indus.gateway.adapters.base import (
    Adapter,
    AdapterConnectError,
    Capabilities,
    ChatInfo,
    FetcherSeat,
)
from indus.gateway.adapters.dingtalk.stream import (
    API_BASE,
    BOT_TOPIC,
    BREAKER_TRIPS,
    INSTALL_HINT,
    QUERY_EXTENSION,
    TOKEN_PATH,
    Breaker,
    LogCollapse,
    SecretQuery,
    SocketOpener,
    StreamFrame,
    StreamRefused,
    ack,
    httpx_ws_opener,
    open_connection,
    socket_url,
)
from indus.gateway.core.events import Attachment, ChatType, InboundEvent, MessageKind, Source
from indus.gateway.core.send import SendResult
from indus.gateway.outbound.chunker import split_message

if TYPE_CHECKING:
    from indus.gateway.config import PlatformSettings

__all__ = [
    "CLIENT_ID_ENV",
    "CLIENT_SECRET_ENV",
    "MAX_MESSAGE_CHARS",
    "DingTalkAdapter",
    "DingTalkOptions",
    "download_ref",
    "parse_download_ref",
]

log = logging.getLogger(__name__)

CLIENT_ID_ENV = "DINGTALK_CLIENT_ID"
CLIENT_SECRET_ENV = "DINGTALK_CLIENT_SECRET"
MAX_MESSAGE_CHARS = 20_000
SEEN_MESSAGES = 1_000
WEBHOOKS_MAX = 500
ROUTES_MAX = 1_000
# A session webhook is dropped this long before the expiry DingTalk states.
WEBHOOK_MARGIN_S = 300.0
WEBHOOK_HOSTS = frozenset({"oapi.dingtalk.com", "api.dingtalk.com"})
# The access token is renewed this long before DingTalk says it expires.
TOKEN_MARGIN_S = 300.0
DEFAULT_MAX_MEDIA_BYTES = 100 * 1024 * 1024
GROUP_SEND_PATH = "/v1.0/robot/groupMessages/send"
DM_SEND_PATH = "/v1.0/robot/oToMessages/batchSend"
DOWNLOAD_PATH = "/v1.0/robot/messageFiles/download"
TOKEN_HEADER = "x-acs-dingtalk-access-token"
DOWNLOAD_SCHEME = "dingtalk://download/"
USER_PREFIX = "user:"
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_NUMBERED = re.compile(r"^\d+\.\s")


def _ids(value: object) -> frozenset[str]:
    if isinstance(value, str):
        items: Iterable[object] = value.split(",")
    elif isinstance(value, list | tuple | set | frozenset):
        items = value  # pyright: ignore[reportUnknownVariableType]
    else:
        return frozenset()
    return frozenset(str(v).strip() for v in items if str(v).strip())  # pyright: ignore[reportUnknownArgumentType, reportUnknownVariableType]


@dataclass(frozen=True)
class DingTalkOptions:
    """``platforms.dingtalk.*`` settings the adapter reads (all optional)."""

    robot_code: str = ""
    require_mention: bool = False
    allowed_chats: frozenset[str] = frozenset()
    free_response_chats: frozenset[str] = frozenset()
    max_media_bytes: int = DEFAULT_MAX_MEDIA_BYTES

    @classmethod
    def read(cls, extra: Mapping[str, Any], env: Mapping[str, str]) -> DingTalkOptions:
        try:
            cap = int(extra.get("max_media_bytes") or DEFAULT_MAX_MEDIA_BYTES)
        except (TypeError, ValueError):
            cap = DEFAULT_MAX_MEDIA_BYTES
        return cls(
            robot_code=str(extra.get("robot_code") or "").strip(),
            require_mention=str(extra.get("require_mention", "")).strip().lower() in _TRUTHY,
            allowed_chats=_ids(extra.get("allowed_chats"))
            | _ids(env.get("INDUS_DINGTALK_ALLOWED_CHATS", "")),
            free_response_chats=_ids(extra.get("free_response_chats")),
            max_media_bytes=max(1, cap),
        )


def download_ref(code: str, name: str = "") -> str:
    return f"{DOWNLOAD_SCHEME}{quote(code, safe='')}/{quote(name or 'file', safe='')}"


def parse_download_ref(ref: str) -> str | None:
    if not ref.startswith(DOWNLOAD_SCHEME):
        return None
    code = unquote(ref[len(DOWNLOAD_SCHEME) :].split("/", 1)[0])
    return code or None


def normalise_markdown(text: str) -> str:
    """DingTalk's renderer needs a blank line before a numbered list and unindented fences."""
    lines = text.split("\n")
    out: list[str] = []
    for n, line in enumerate(lines):
        before = lines[n - 1].strip() if n else ""
        stripped = line.strip()
        if before and _NUMBERED.match(stripped) and not _NUMBERED.match(before):
            out.append("")
        out.append(line.lstrip() if stripped.startswith("```") else line)
    return "\n".join(out)


def _title(text: str) -> str:
    """The notification title DingTalk shows for a markdown message."""
    for line in text.split("\n"):
        plain = re.sub(r"[#*_`>\[\]()!~|-]", "", line).strip()
        if plain:
            return plain[:30]
    return "Indus Agent"


def _dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}  # pyright: ignore[reportUnknownArgumentType]


@dataclass(frozen=True)
class _Webhook:
    url: str  # scheme, host and path only
    query: tuple[tuple[str, str], ...]  # the session secret, added inside the transport
    expires_ms: int


class DingTalkAdapter(Adapter):
    platform: ClassVar[str] = "dingtalk"
    capabilities: ClassVar[Capabilities] = Capabilities(
        max_len=MAX_MESSAGE_CHARS,
        markup="markdown",
        splits_natively=True,
        supports_edit=False,
        supports_delete=False,
        supports_buttons=False,
        dm_is_private=True,
        push_capable=True,
    )
    vendor_names: ClassVar[Mapping[str, tuple[str, ...]]] = {
        # Our own Stream Mode client (no SDK); its requests are served by the wire fake.
        "indus.gateway.adapters.dingtalk.stream:StreamFrame": ("parse", "payload"),
        "indus.gateway.adapters.dingtalk.stream:SecretQuery": ("expect", "handle_async_request"),
        "httpx_ws:AsyncWebSocketSession": ("receive_text", "send_text", "close"),
    }

    def __init__(
        self,
        *,
        clock: Clock,
        client_id: str,
        client_secret: str,
        settings: PlatformSettings | None = None,
        options: DingTalkOptions | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        proxy: str | None = None,
        opener: SocketOpener | None = None,
        api_base: str = API_BASE,
        send_only: bool = False,
    ) -> None:
        super().__init__(clock=clock, settings=settings, secrets=(client_id, client_secret))
        self.send_only = send_only
        self._client_id = client_id.strip()
        self._client_secret = client_secret.strip()
        self.options = options or DingTalkOptions()
        self._transport = transport
        self._proxy = proxy
        self._opener: SocketOpener = opener or httpx_ws_opener
        self._api_base = api_base.rstrip("/")
        self._client: httpx.AsyncClient | None = None
        self._query: SecretQuery | None = None
        self._token: str | None = None
        self._token_until = 0.0
        self._stream: asyncio.Task[None] | None = None
        self._work: set[asyncio.Task[Any]] = set()
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._webhooks: OrderedDict[str, _Webhook] = OrderedDict()
        self._dm_users: OrderedDict[str, str] = OrderedDict()
        self._local_ids = itertools.count(1)
        self.breaker = Breaker()
        self.log_collapse = LogCollapse(clock)
        self.stream_opens = 0

    def __repr__(self) -> str:
        state = "connected" if self.connected else "disconnected"
        return f"<DingTalkAdapter {state}>"

    @property
    def robot_code(self) -> str:
        return self.options.robot_code or self._client_id

    def scrub(self, text: str) -> str:
        """Credentials, the current access token and session secrets masked."""
        held = [self._token or ""]
        for hook in self._webhooks.values():
            held.extend(value for _, value in hook.query)
        return redact_text(super().scrub(text), [h for h in held if h])

    # -- lifecycle ---------------------------------------------------------------------------------

    async def _open(self, is_reconnect: bool) -> None:
        missing = [
            name
            for name, value in (
                (CLIENT_ID_ENV, self._client_id),
                (CLIENT_SECRET_ENV, self._client_secret),
            )
            if not value
        ]
        if missing:
            raise AdapterConnectError(
                "DingTalk is not configured: set " + " and ".join(missing),
                retryable=False,
                code="config",
            )
        inner = self._transport or httpx.AsyncHTTPTransport(proxy=self._proxy, trust_env=False)
        query = SecretQuery(inner)
        client = httpx.AsyncClient(
            base_url=self._api_base,
            transport=query,
            headers={"User-Agent": f"IndusAgent/{__version__}"},
            timeout=httpx.Timeout(30.0, connect=10.0),
            trust_env=False,
            follow_redirects=False,
        )
        self._client, self._query = client, query
        try:
            await self._access_token(force=True)
        except StreamRefused as exc:
            await self._drop_client()
            if exc.status in (400, 401, 403):
                raise AdapterConnectError(
                    f"DingTalk refused the app credentials (HTTP {exc.status})",
                    retryable=False,
                    code="auth_failed",
                ) from None
            raise AdapterConnectError(str(exc), retryable=True) from None
        except httpx.HTTPError as exc:
            await self._drop_client()
            raise AdapterConnectError(
                f"cannot reach DingTalk: {type(exc).__name__}", retryable=True
            ) from None
        except BaseException:
            await self._drop_client()
            raise
        log.info("dingtalk: app credentials accepted")
        if not self.send_only:
            self._stream = asyncio.ensure_future(self._run_stream())

    async def _drop_client(self) -> None:
        client, self._client, self._query = self._client, None, None
        self._token = None
        if client is not None:
            await client.aclose()

    async def _close(self) -> None:
        stream, self._stream = self._stream, None
        tasks = [t for t in (stream, *self._work) if t is not None and not t.done()]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._work.clear()
        await self._drop_client()

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise ConnectionError("not connected to DingTalk")
        return self._client

    @property
    def streaming(self) -> bool:
        return self._stream is not None and not self._stream.done()

    # -- access token ------------------------------------------------------------------------------

    async def _access_token(self, *, force: bool = False) -> str:
        now = self.clock.monotonic()
        if self._token and not force and now < self._token_until:
            return self._token
        client = self._require_client()
        body = {"appKey": self._client_id, "appSecret": self._client_secret}
        response = await client.post(TOKEN_PATH, json=body, timeout=10.0)
        if response.status_code >= 400:
            raise StreamRefused(response.status_code)
        data = _dict(response.json())
        token = str(data.get("accessToken") or "")
        if not token:
            raise StreamRefused(response.status_code, "no access token in the answer")
        try:
            lifetime = float(data.get("expireIn") or 7200)
        except (TypeError, ValueError):
            lifetime = 7200.0
        self._token = token
        self._token_until = now + max(lifetime - TOKEN_MARGIN_S, 60.0)
        return token

    # -- the stream --------------------------------------------------------------------------------

    def _warn(self, key: str, message: str, *args: object) -> None:
        held = self.log_collapse.note(key)
        if held is None:
            return
        suffix = f" ({held} identical lines held back)" if held else ""
        log.warning(message + suffix, *args)

    async def _run_stream(self) -> None:
        while True:
            client, query = self._client, self._query
            if client is None or query is None:
                return
            failure: str
            try:
                endpoint, ticket = await open_connection(
                    client,
                    self._client_id,
                    self._client_secret,
                    ua=f"indus-agent/{__version__}",
                )
                url = socket_url(endpoint)
                query.expect(url, {"ticket": ticket})
                async with self._opener(client, url) as socket:
                    self.stream_opens += 1
                    self.breaker.succeeded()
                    log.info("dingtalk: stream open")
                    while True:
                        frame = StreamFrame.parse(await socket.receive_text())
                        if frame is None:
                            continue
                        if not await self._on_frame(frame, socket):
                            break
                continue  # DingTalk asked for a fresh connection: open one at once
            except asyncio.CancelledError:
                raise
            except ModuleNotFoundError:
                log.error("dingtalk: Stream Mode needs httpx-ws: %s", INSTALL_HINT)
                self.report_fatal(
                    "missing_dependency", f"Stream Mode needs httpx-ws ({INSTALL_HINT})"
                )
                return
            except StreamRefused as exc:
                if exc.status in (401, 403):
                    log.error("dingtalk: the stream was refused (HTTP %s)", exc.status)
                    self.report_fatal(
                        "dingtalk_auth",
                        f"DingTalk refused the stream connection (HTTP {exc.status}); check "
                        f"{CLIENT_ID_ENV} and {CLIENT_SECRET_ENV}",
                    )
                    return
                failure = f"StreamRefused:{exc.status}"
            except Exception as exc:
                failure = f"{type(exc).__name__}: {exc}"
            delay = self.breaker.failed(failure)
            if self.breaker.same == BREAKER_TRIPS:
                log.error(
                    "dingtalk: the stream failed %d times the same way (%s); retrying every "
                    "%.0f s without further warnings until the failure changes",
                    BREAKER_TRIPS,
                    self.scrub(failure),
                    delay,
                )
            elif self.breaker.tripped:
                log.debug("dingtalk: stream still failing; next try in %.0f s", delay)
            else:
                self._warn(
                    "failure:" + failure,
                    "dingtalk: stream closed (%s); reopening in %.0f s",
                    self.scrub(failure),
                    delay,
                )
            await self.clock.sleep(delay)

    async def _on_frame(self, frame: StreamFrame, socket: Any) -> bool:
        """Acknowledge and dispatch one frame; False when the stream should be reopened."""
        if frame.type == "SYSTEM":
            if frame.topic == "ping":
                await socket.send_text(ack(frame, data=frame.data))
                return True
            await socket.send_text(ack(frame))
            return frame.topic != "disconnect"
        if frame.type == "CALLBACK" and frame.topic == BOT_TOPIC:
            await socket.send_text(ack(frame))
            event = self.normalise(frame.payload())
            if event is not None:
                self._spawn(self.emit(event))
            return True
        await socket.send_text(ack(frame, code=404, message="not implemented"))
        return True

    def _spawn(self, work: Any) -> None:
        task: asyncio.Task[Any] = asyncio.ensure_future(work)
        self._work.add(task)
        task.add_done_callback(self._work.discard)

    # -- inbound -----------------------------------------------------------------------------------

    def _seen_before(self, message_id: str) -> bool:
        if message_id in self._seen:
            return True
        self._seen[message_id] = None
        while len(self._seen) > SEEN_MESSAGES:
            self._seen.popitem(last=False)
        return False

    def _remember_webhook(self, chat_id: str, url: str, expires: object) -> None:
        parts = urlsplit(url.strip())
        if parts.scheme != "https" or (parts.hostname or "").lower() not in WEBHOOK_HOSTS:
            log.debug("dingtalk: session webhook on an unexpected host ignored")
            return
        if parts.username or parts.password or parts.port not in (None, 443):
            return
        try:
            expires_ms = int(str(expires or 0))
        except ValueError:
            expires_ms = 0
        base = urlunsplit(("https", parts.hostname or "", parts.path, "", ""))
        query = tuple(parse_qsl(parts.query, keep_blank_values=True))
        self._webhooks.pop(chat_id, None)
        self._webhooks[chat_id] = _Webhook(base, query, expires_ms)
        while len(self._webhooks) > WEBHOOKS_MAX:
            self._webhooks.popitem(last=False)

    def _webhook_for(self, chat_id: str) -> _Webhook | None:
        hook = self._webhooks.get(chat_id)
        if hook is None:
            return None
        now_ms = self.clock.now().timestamp() * 1000
        if hook.expires_ms and now_ms + WEBHOOK_MARGIN_S * 1000 >= hook.expires_ms:
            self._webhooks.pop(chat_id, None)
            return None
        return hook

    def _remember_dm(self, chat_id: str, staff_id: str) -> None:
        self._dm_users.pop(chat_id, None)
        self._dm_users[chat_id] = staff_id
        while len(self._dm_users) > ROUTES_MAX:
            self._dm_users.popitem(last=False)

    def normalise(self, data: Mapping[str, Any]) -> InboundEvent | None:
        """One robot message → an `InboundEvent`, or None when it is dropped."""
        message_id = str(data.get("msgId") or "")
        if not message_id or self._seen_before(message_id):
            return None
        group = str(data.get("conversationType") or "1") == "2"
        staff_id = str(data.get("senderStaffId") or "")
        sender_id = str(data.get("senderId") or "")
        user_id = staff_id or sender_id
        chat_id = str(data.get("conversationId") or "") or user_id
        if not chat_id or not user_id:
            return None
        mentioned = bool(data.get("isInAtList"))
        if group:
            allowed = self.options.allowed_chats
            if allowed and chat_id not in allowed:
                log.debug("dingtalk: %s is not in allowed_chats; message ignored", chat_id)
                return None
            free = chat_id in self.options.free_response_chats
            if self.options.require_mention and not mentioned and not free:
                return None
        hook = str(data.get("sessionWebhook") or "")
        if hook:
            self._remember_webhook(chat_id, hook, data.get("sessionWebhookExpiredTime"))
        if not group and staff_id:
            self._remember_dm(chat_id, staff_id)
        text, media = self._content(data)
        if not text and not media:
            return None
        kind = MessageKind.TEXT
        if media:
            kind = _kind_of(media[0].mime)
        when = None
        created = data.get("createAt")
        if isinstance(created, int | float) and created > 0:
            when = datetime.fromtimestamp(created / 1000, UTC)
        chat_type: ChatType = "group" if group else "dm"
        title = str(data.get("conversationTitle") or "")
        source = Source(
            platform=self.platform,
            chat_id=chat_id,
            chat_type=chat_type,
            chat_name=title or None,
            user_id=user_id,
            user_name=str(data.get("senderNick") or "") or user_id,
            user_id_alt=sender_id if staff_id and sender_id else None,
        )
        metadata: dict[str, Any] = {"mentioned": True} if mentioned else {}
        return InboundEvent(
            source=source,
            text=text,
            kind=kind,
            message_id=message_id,
            media=media,
            metadata=metadata,
            timestamp=when,
        )

    def _content(self, data: Mapping[str, Any]) -> tuple[str, tuple[Attachment, ...]]:
        kind = str(data.get("msgtype") or "text")
        content = _dict(data.get("content"))
        media: list[Attachment] = []

        def attach(code: object, name: str, mime: str) -> None:
            if isinstance(code, str) and code:
                media.append(Attachment(ref=download_ref(code, name), mime=mime))

        text = ""
        if kind == "text":
            text = str(_dict(data.get("text")).get("content") or "")
        elif kind == "richText":
            parts: list[str] = []
            items = content.get("richText")
            for item in items if isinstance(items, list) else []:  # pyright: ignore[reportUnknownVariableType]
                entry = _dict(item)
                if entry.get("text"):
                    parts.append(str(entry["text"]))
                code = entry.get("downloadCode") or entry.get("pictureDownloadCode")
                if code:
                    attach(code, "picture.jpg", "image/jpeg")
            text = " ".join(p.strip() for p in parts if p.strip())
        elif kind == "picture":
            code = content.get("downloadCode") or content.get("pictureDownloadCode")
            attach(code, "picture.jpg", "image/jpeg")
        elif kind == "audio":
            text = str(content.get("recognition") or "")
            attach(content.get("downloadCode"), "voice.amr", "audio/amr")
        elif kind == "video":
            attach(content.get("downloadCode"), "video.mp4", "video/mp4")
        elif kind == "file":
            name = str(content.get("fileName") or "file")
            mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
            attach(content.get("downloadCode"), name, mime)
        else:
            text = str(_dict(data.get("text")).get("content") or "")
        return text.strip(), tuple(media)

    # -- media for the intake ----------------------------------------------------------------------

    async def fetch_media(self, attachment: Attachment) -> AsyncIterator[bytes]:
        """The bytes of an inbound attachment (registered by `register_fetchers`)."""
        code = parse_download_ref(attachment.ref)
        if code is None:
            raise ValueError("not a DingTalk download reference")
        client = self._require_client()
        token = await self._access_token()
        response = await client.post(
            DOWNLOAD_PATH,
            json={"downloadCode": code, "robotCode": self.robot_code},
            headers={TOKEN_HEADER: token},
        )
        if response.status_code >= 400:
            raise ConnectionError(f"DingTalk refused the download (HTTP {response.status_code})")
        url = str(_dict(response.json()).get("downloadUrl") or "")
        if not url.startswith("https://"):
            raise ValueError("DingTalk gave no https download address")
        cap = self.options.max_media_bytes
        async with client.stream("GET", url, follow_redirects=True, timeout=120.0) as body:
            if body.status_code >= 400:
                raise ConnectionError(f"the download failed (HTTP {body.status_code})")
            declared = body.headers.get("Content-Length", "")
            if declared.isdigit() and int(declared) > cap:
                raise ValueError(f"the file is too large ({declared} bytes, the cap is {cap})")
            seen = 0
            async for chunk in body.aiter_bytes():
                seen += len(chunk)
                if seen > cap:
                    raise ValueError(f"the file is too large (the cap is {cap} bytes)")
                yield chunk

    def register_fetchers(self, seat: FetcherSeat) -> Callable[[], None] | None:
        return seat.register_fetcher(self.platform, self.fetch_media)

    # -- outbound ----------------------------------------------------------------------------------

    async def _send(
        self, chat_id: str, text: str, reply_to: str | None, meta: dict[str, Any]
    ) -> SendResult:
        chunked = split_message(normalise_markdown(text), MAX_MESSAGE_CHARS)
        sent: list[str] = []
        for n, chunk in enumerate(chunked.chunks):
            if n:
                await self.slots.pace(chat_id)
            result = await self._send_one(chat_id, chunk.text)
            if not result.ok:
                remainder = None if result.maybe_delivered else chunked.remainder(n)
                if not sent:
                    return result
                return SendResult.failure(
                    result.error,
                    retry_after=result.retry_after,
                    delivered_ids=tuple(sent),
                    remainder=remainder,
                )
            sent.append(result.message_id or "")
        return SendResult.sent(sent[-1], tuple(sent[:-1]))

    def _markdown(self, text: str) -> dict[str, Any]:
        return {"title": _title(text), "text": text}

    async def _send_one(self, chat_id: str, text: str) -> SendResult:
        hook = self._webhook_for(chat_id)
        if hook is not None:
            result = await self._via_webhook(chat_id, hook, text)
            if result is not None:
                return result
        return await self._via_api(chat_id, text)

    async def _via_webhook(self, chat_id: str, hook: _Webhook, text: str) -> SendResult | None:
        """Send through the session webhook; None when it cannot be used (then the API is)."""
        client = self._require_client()
        body = {"msgtype": "markdown", "markdown": self._markdown(text)}
        try:
            response = await client.post(
                hook.url,
                json=body,
                timeout=15.0,
                extensions={QUERY_EXTENSION: dict(hook.query)},
            )
        except httpx.HTTPError as exc:
            result = failure_from_exception(exc)
            if result.maybe_delivered:
                return result  # it may have landed: never send it a second way
            return None
        errcode: object = None
        if response.status_code < 400:
            with contextlib.suppress(ValueError, UnicodeDecodeError):
                errcode = _dict(response.json()).get("errcode")
        if response.status_code < 400 and errcode in (0, None):
            return SendResult.sent(f"wh-{next(self._local_ids)}-{uuid.uuid4().hex[:8]}")
        log.info(
            "dingtalk: the session webhook for %s refused the message (HTTP %s, errcode %s); "
            "using the robot API",
            chat_id,
            response.status_code,
            errcode,
        )
        self._webhooks.pop(chat_id, None)
        return None

    async def _via_api(self, chat_id: str, text: str, *, retried: bool = False) -> SendResult:
        client = self._require_client()
        try:
            token = await self._access_token()
        except StreamRefused as exc:
            return SendResult.failure(f"unauthorized: DingTalk refused the app credentials ({exc})")
        except httpx.HTTPError as exc:
            return failure_from_exception(exc)
        param = json.dumps(self._markdown(text), ensure_ascii=False)
        user = chat_id[len(USER_PREFIX) :] if chat_id.startswith(USER_PREFIX) else None
        user = user or self._dm_users.get(chat_id)
        if user:
            path = DM_SEND_PATH
            body: dict[str, Any] = {"robotCode": self.robot_code, "userIds": [user]}
        else:
            path = GROUP_SEND_PATH
            body = {"robotCode": self.robot_code, "openConversationId": chat_id}
        body.update({"msgKey": "sampleMarkdown", "msgParam": param})
        try:
            response = await client.post(path, json=body, headers={TOKEN_HEADER: token})
        except httpx.HTTPError as exc:
            return failure_from_exception(exc)
        if response.status_code == 401 and not retried:
            self._token = None
            return await self._via_api(chat_id, text, retried=True)
        if response.status_code >= 400:
            return self._api_failure(response)
        key = str(_dict(response.json()).get("processQueryKey") or "")
        return SendResult.sent(key or f"api-{next(self._local_ids)}")

    def _api_failure(self, response: httpx.Response) -> SendResult:
        status = response.status_code
        body: dict[str, Any] = {}
        with contextlib.suppress(ValueError, UnicodeDecodeError):
            body = _dict(response.json())
        code = str(body.get("code") or "").lower()
        reading: str | None = None
        if status == 429 or any(k in code for k in ("throttl", "qpslimit", "flowcontrol")):
            return failure_from_status(429, response.text, response.headers, now=self.clock.now())
        if status == 401:
            reading = "unauthorized: the access token was refused"
        elif status == 403:
            reading = "forbidden"
        elif status == 404:
            reading = "message not found" if "message" in code else "chat not found"
        elif status == 400 and any(k in code for k in ("length", "toolong", "too_long")):
            reading = "message is too long"
        elif status == 400 and any(k in code for k in ("param", "msgkey", "invalid")):
            reading = "can't parse the message"
        return failure_from_status(
            status, response.text, response.headers, now=self.clock.now(), reading=reading
        )

    def is_final_failure(self, result: SendResult) -> bool:
        return "unauthorized" in (result.error or "")

    async def _chat_info(self, chat_id: str) -> ChatInfo:
        if chat_id.startswith(USER_PREFIX) or chat_id in self._dm_users:
            return ChatInfo(name=chat_id, type="dm")
        return ChatInfo(name=chat_id)


def _kind_of(mime: str) -> MessageKind:
    if mime.startswith("image/"):
        return MessageKind.PHOTO
    if mime.startswith("audio/"):
        return MessageKind.VOICE
    if mime.startswith("video/"):
        return MessageKind.VIDEO
    return MessageKind.DOCUMENT
