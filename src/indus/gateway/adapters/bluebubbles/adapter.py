"""iMessage through a BlueBubbles server on a Mac (P16-24).

The user runs BlueBubbles Server (≥ 1.9) on a Mac signed in to iMessage. This adapter talks to its
REST API v1 over ``httpx`` and receives its webhooks through the P16-01 listener kit:

- connect: ``GET /api/v1/ping``, then ``GET /api/v1/server/info`` (is the Private API helper
  there?), then the callback listener (port 8645, path ``/bluebubbles-webhook``, body ≤1 MiB,
  loopback unless opened on purpose) and the webhook registration (``/api/v1/webhook``; an
  existing row for our URL is reused, every row for it is removed again on disconnect);
- the server password is the API's only credential and travels as the ``password`` query
  parameter, added inside our transport (`_PasswordQuery`) so httpx's request log line,
  ``response.request`` and error texts never carry it;
- the server cannot put headers on its webhook calls, so the registered URL carries a token in its
  query string. The token is derived from the password (`webhook_token`), never the password
  itself, and is compared in constant time before the body is parsed;
- ``new-message`` events become inbound events (chat id = the chat GUID, user id = the sender's
  phone number or email); ``updated-message`` events with an edit time become edited events; our
  own messages, tapbacks and status-only updates are dropped; a replayed delivery is dropped;
- replies: each paragraph is its own bubble and a long one is cut at 4,000 code points without
  ``(i/n)`` markers; a bare address is resolved to its chat GUID (exact ``chatIdentifier`` match,
  never by participant, LRU of 500), and an address without a chat gets one through
  ``/api/v1/chat/new`` when the Private API is there;
- typing, read receipts, quoted replies and tapback reactions need the Private API helper and are
  skipped without it;
- attachments are fetched lazily by the media intake (`register_fetchers`), so nothing is
  downloaded for a message the gateway refuses.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import re
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, Literal
from urllib.parse import quote, urlparse

import httpx

from indus.foundation.clock import Clock
from indus.foundation.digits import is_digits
from indus.gateway.adapters._http import (
    BadPayload,
    CallbackListener,
    CallbackRequest,
    ListenerSettings,
    Reply,
    SeenIds,
    constant_time_equal,
    failure_from_exception,
    failure_from_status,
)
from indus.gateway.adapters.base import (
    REPLY_TO_KEY,
    Adapter,
    AdapterConnectError,
    Capabilities,
    ChannelEntry,
    ChatInfo,
    FetcherSeat,
    MediaKind,
)
from indus.gateway.config import PlatformSettings
from indus.gateway.core.events import Attachment, InboundEvent, MessageKind, Source
from indus.gateway.core.send import SendResult
from indus.gateway.http.serving import is_loopback
from indus.gateway.intake.media import MediaRejected
from indus.gateway.outbound.chunker import split_message

__all__ = [
    "ATTACHMENT_REF",
    "DEFAULT_WAKE_WORDS",
    "GUID_CACHE_SIZE",
    "MAX_TEXT",
    "PASSWORD_ENV",
    "PROCESSING_REACTIONS",
    "URL_ENV",
    "WEBHOOK_EVENTS",
    "WEBHOOK_MAX_BODY",
    "WEBHOOK_PATH",
    "WEBHOOK_PORT",
    "BlueBubblesAdapter",
    "BlueBubblesOptions",
    "GuidCache",
    "mask_address",
    "webhook_token",
]

log = logging.getLogger(__name__)

URL_ENV = "BLUEBUBBLES_SERVER_URL"
PASSWORD_ENV = "BLUEBUBBLES_PASSWORD"
MAX_TEXT = 4000
WEBHOOK_PORT = 8645
WEBHOOK_PATH = "/bluebubbles-webhook"
WEBHOOK_MAX_BODY = 1024 * 1024
WEBHOOK_EVENTS = ("new-message", "updated-message")
GUID_CACHE_SIZE = 500
SEEN_TTL = 24 * 3600.0
SEEN_MAX = 5000
SENT_MAX = 500
MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024
ATTACHMENT_REF = "bluebubbles-attachment:"
# Wake words for groups: iMessage has no mention of a bot account, so a group message addresses us
# when it opens with (or contains) one of these patterns.
DEFAULT_WAKE_WORDS: tuple[str, ...] = (
    r"(?<![\w@])@?indus\s+agent\b[,:\-]?",
    r"(?<![\w@])@?indus\b[,:\-]?",
)
# Tapbacks that show a message's processing state (Private API only).
PROCESSING_REACTIONS: Mapping[str, str] = {
    "started": "emphasize",
    "ok": "like",
    "failed": "dislike",
}

_TOKEN_LABEL = b"indus bluebubbles webhook v1"
_PLACEHOLDER = "￼"
_PARAGRAPH = re.compile(r"\S(?:.*?\S)?(?=\n[ \t]*\n|\s*\Z)", re.DOTALL)
_PHONE = re.compile(r"^\+?[0-9][0-9 ().\-]{5,}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def webhook_token(password: str) -> str:
    """The secret the registered webhook URL carries: derived from the password, never it."""
    return hmac.new(password.encode("utf-8"), _TOKEN_LABEL, hashlib.sha256).hexdigest()


def mask_address(value: str) -> str:
    """A phone number or email for logs: ``+155****4567`` / ``a***@example.com``."""
    text = (value or "").strip()
    if "@" in text:
        local, _, domain = text.partition("@")
        return f"{local[:1]}***@{domain}"
    digits = re.sub(r"[^0-9+]", "", text)
    if len(digits) >= 8:
        return f"{digits[:4]}****{digits[-4:]}"
    return text


def _is_address(value: str) -> bool:
    text = value.strip()
    return bool(_PHONE.match(text) or _EMAIL.match(text))


def _address_key(value: str) -> str:
    text = value.strip()
    if "@" in text:
        return text.lower()
    return re.sub(r"[ ().\-]", "", text)


def _truthy(value: object, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _as_dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}  # pyright: ignore[reportUnknownArgumentType]


def _as_list(value: object) -> list[Any]:
    return list(value) if isinstance(value, list) else []  # pyright: ignore[reportUnknownArgumentType]


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _ms_to_datetime(value: object) -> datetime | None:
    if not isinstance(value, int | float) or isinstance(value, bool) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _no_marker(index: int, total: int) -> str:
    return ""


def compile_patterns(raw: object) -> tuple[re.Pattern[str], ...]:
    """Wake-word regexes from a list, a JSON list, or a comma/newline-separated string; our
    defaults when nothing is configured. A pattern that does not compile is skipped (logged)."""
    entries: list[str] = []
    if isinstance(raw, str) and raw.strip():
        text = raw.strip()
        parsed: object = None
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
        if isinstance(parsed, list):
            entries = [str(p) for p in _as_list(parsed)]
        else:
            entries = [p for p in re.split(r"[\n,]", text)]
    elif isinstance(raw, list | tuple):
        entries = [str(p) for p in raw]  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    entries = [e.strip() for e in entries if e.strip()] or list(DEFAULT_WAKE_WORDS)
    out: list[re.Pattern[str]] = []
    for entry in entries:
        try:
            out.append(re.compile(entry, re.IGNORECASE))
        except re.error:
            log.warning("bluebubbles: mention pattern %r does not compile; skipped", entry)
    return tuple(out)


@dataclass(frozen=True)
class BlueBubblesOptions:
    send_read_receipts: bool = True
    split_paragraphs: bool = True
    mention_patterns: tuple[re.Pattern[str], ...] = ()

    @classmethod
    def read(cls, extra: Mapping[str, Any]) -> BlueBubblesOptions:
        return cls(
            send_read_receipts=_truthy(extra.get("send_read_receipts"), True),
            split_paragraphs=_truthy(extra.get("split_paragraphs"), True),
            mention_patterns=compile_patterns(extra.get("mention_patterns")),
        )


class GuidCache:
    """Address → chat GUID, least recently used dropped beyond ``size``."""

    def __init__(self, size: int = GUID_CACHE_SIZE) -> None:
        self.size = size
        self._items: OrderedDict[str, str] = OrderedDict()

    def get(self, address: str) -> str | None:
        key = _address_key(address)
        found = self._items.get(key)
        if found is not None:
            self._items.move_to_end(key)
        return found

    def put(self, address: str, guid: str) -> None:
        key = _address_key(address)
        self._items[key] = guid
        self._items.move_to_end(key)
        while len(self._items) > self.size:
            self._items.popitem(last=False)

    def __contains__(self, address: object) -> bool:
        return isinstance(address, str) and _address_key(address) in self._items

    def __len__(self) -> int:
        return len(self._items)


class _PasswordQuery(httpx.AsyncBaseTransport):
    """Adds ``password=<server password>`` to every request inside the transport.

    httpx logs a request, keeps it on ``response.request`` and names its URL in error texts from
    its own copy, never from the transport's: the password reaches the server and nothing else.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport, password: str) -> None:
        self._inner = inner
        self._password = password

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        signed = httpx.Request(
            request.method,
            request.url.copy_merge_params({"password": self._password}),
            headers=request.headers,
            stream=request.stream,
            extensions=request.extensions,
        )
        return await self._inner.handle_async_request(signed)

    async def aclose(self) -> None:
        await self._inner.aclose()


_KINDS: tuple[tuple[str, MessageKind], ...] = (
    ("image/", MessageKind.PHOTO),
    ("video/", MessageKind.VIDEO),
    ("audio/", MessageKind.AUDIO),
)


class BlueBubblesAdapter(Adapter):
    platform: ClassVar[str] = "bluebubbles"
    capabilities: ClassVar[Capabilities] = Capabilities(
        max_len=MAX_TEXT,
        markup="plain",
        splits_natively=True,
    )
    vendor_names: ClassVar[Mapping[str, tuple[str, ...]]] = {
        "httpx:AsyncClient": ("request", "stream", "aclose"),
        "httpx:Response": ("aiter_bytes", "json"),
    }

    def __init__(
        self,
        *,
        clock: Clock,
        server_url: str,
        password: str,
        listener: ListenerSettings | None = None,
        settings: PlatformSettings | None = None,
        options: BlueBubblesOptions | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        proxy: str | None = None,
        serve: bool = True,
        send_only: bool = False,
    ) -> None:
        self.password = (password or "").strip()
        self.token = webhook_token(self.password) if self.password else ""
        super().__init__(clock=clock, settings=settings, secrets=(self.password, self.token))
        raw = (server_url or "").strip()
        if raw and "://" not in raw:
            raw = f"http://{raw}"
        self.server_url = raw.rstrip("/")
        self.options = options or BlueBubblesOptions(mention_patterns=compile_patterns(None))
        self.send_only = send_only
        self.listener_settings = listener or ListenerSettings(
            port=WEBHOOK_PORT, max_body_bytes=WEBHOOK_MAX_BODY, path=WEBHOOK_PATH
        )
        self.listener = CallbackListener(
            self.listener_settings,
            name=self.platform,
            clock=clock,
            scrub=self.scrub,
            serve=serve,
        )
        self.listener.route(self.listener_settings.path, self._callback, methods=("POST",))
        self._transport = transport
        self._proxy = proxy
        self._client: httpx.AsyncClient | None = None
        self.private_api = False
        self.helper_connected = False
        self.guids = GuidCache()
        self._seen = SeenIds(clock, SEEN_TTL, SEEN_MAX)
        self._sent: OrderedDict[str, None] = OrderedDict()

    def __repr__(self) -> str:
        state = "connected" if self.connected else "disconnected"
        return f"<BlueBubblesAdapter {self.server_url or '?'} {state}>"

    # -- lifecycle ---------------------------------------------------------------------------------

    @property
    def can_use_private_api(self) -> bool:
        return self.private_api and self.helper_connected

    def webhook_url(self) -> str:
        """The URL the server calls (token included): ``webhook_public_url`` when set, else the
        listener's own address."""
        settings = self.listener_settings
        base = settings.public_url
        if not base:
            address = self.listener.address
            port = address[1] if address is not None else settings.port
            host = "localhost" if is_loopback(settings.host) else settings.host
            if host in ("0.0.0.0", "::"):
                host = "localhost"
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            base = f"http://{host}:{port}{settings.path}"
        separator = "&" if "?" in base else "?"
        return f"{base}{separator}token={self.token}"

    async def _open(self, is_reconnect: bool) -> None:
        wanted = ((URL_ENV, self.server_url), (PASSWORD_ENV, self.password))
        missing = [name for name, value in wanted if not value]
        if missing:
            raise AdapterConnectError(
                f"BlueBubbles is not configured: set {', '.join(missing)}",
                retryable=False,
                code="config",
            )
        if urlparse(self.server_url).scheme not in ("http", "https"):
            raise AdapterConnectError(
                "BLUEBUBBLES_SERVER_URL must be an http:// or https:// address",
                retryable=False,
                code="config",
            )
        inner = self._transport or httpx.AsyncHTTPTransport(proxy=self._proxy, trust_env=False)
        client = httpx.AsyncClient(
            base_url=self.server_url,
            transport=_PasswordQuery(inner, self.password),
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(30.0, connect=10.0),
        )
        try:
            await self._check_server(client)
        except BaseException:
            await client.aclose()
            raise
        self._client = client
        if self.send_only:
            log.info("bluebubbles: connected to %s for sending", self.server_url)
            return
        try:
            await self.listener.start()
            await self._register_webhook()
        except BaseException:
            await self.listener.stop()
            self._client = None
            await client.aclose()
            raise
        log.info(
            "bluebubbles: connected to %s (private API %s)",
            self.server_url,
            "on" if self.can_use_private_api else "off",
        )

    async def _check_server(self, client: httpx.AsyncClient) -> None:
        for path in ("/api/v1/ping", "/api/v1/server/info"):
            try:
                response = await client.get(path, timeout=10.0)
            except httpx.HTTPError as exc:
                raise AdapterConnectError(
                    f"cannot reach BlueBubbles at {self.server_url}: {type(exc).__name__}",
                    retryable=True,
                ) from None
            if response.status_code in (401, 403):
                raise AdapterConnectError(
                    "BlueBubbles refused the server password",
                    retryable=False,
                    code="bluebubbles_auth",
                )
            if response.status_code >= 400:
                raise AdapterConnectError(
                    f"BlueBubbles answered HTTP {response.status_code} to {path}", retryable=True
                )
            if path.endswith("/info"):
                data = _as_dict(_as_dict(self._json(response)).get("data"))
                self.private_api = data.get("private_api") is True
                self.helper_connected = data.get("helper_connected") is True

    async def _close(self) -> None:
        await self.listener.stop()
        client, self._client = self._client, None
        if client is None:
            return
        try:
            if not self.send_only:
                await self._unregister_webhook(client)
        finally:
            await client.aclose()

    @staticmethod
    def _json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            return None

    async def _webhook_rows(self, client: httpx.AsyncClient, url: str) -> list[dict[str, Any]]:
        try:
            response = await client.get("/api/v1/webhook")
        except httpx.HTTPError:
            return []
        if response.status_code >= 400:
            return []
        rows = _as_list(_as_dict(self._json(response)).get("data"))
        return [_as_dict(r) for r in rows if _as_dict(r).get("url") == url]

    async def _register_webhook(self) -> None:
        client = self._client
        assert client is not None
        url = self.webhook_url()
        if await self._webhook_rows(client, url):
            log.info("bluebubbles: webhook already registered")
            return
        try:
            response = await client.post(
                "/api/v1/webhook", json={"url": url, "events": list(WEBHOOK_EVENTS)}
            )
        except httpx.HTTPError as exc:
            log.warning("bluebubbles: webhook registration failed: %s", type(exc).__name__)
            return
        if response.status_code >= 400:
            log.warning(
                "bluebubbles: webhook registration answered HTTP %d; no messages will arrive",
                response.status_code,
            )
            return
        log.info("bluebubbles: webhook registered (token in the URL, not logged)")

    async def _unregister_webhook(self, client: httpx.AsyncClient) -> None:
        for row in await self._webhook_rows(client, self.webhook_url()):
            ident = row.get("id")
            if ident is None:
                continue
            try:
                await client.delete(f"/api/v1/webhook/{quote(str(ident), safe='')}")
            except httpx.HTTPError as exc:
                log.debug("bluebubbles: webhook removal failed: %s", type(exc).__name__)

    # -- inbound -----------------------------------------------------------------------------------

    async def _callback(self, request: CallbackRequest) -> Reply:
        offered = request.query_params().get("token", "")
        if not self.token or not constant_time_equal(offered, self.token):
            return Reply.json({"error": "unauthorized"}, 401)
        try:
            payload = request.json()
        except BadPayload:
            return Reply.json({"error": "bad payload"}, 400)
        if not isinstance(payload, Mapping):
            return Reply.json({"error": "bad payload"}, 400)
        body = _as_dict(payload)
        kind = _text(body.get("type"))
        if kind not in WEBHOOK_EVENTS:
            return Reply.json({"status": "ignored"})
        data = body.get("data")
        if isinstance(data, list):
            data = next((d for d in _as_list(data) if isinstance(d, Mapping)), None)
        if not isinstance(data, Mapping):
            return Reply.json({"error": "bad payload"}, 400)
        try:
            found = self.normalise(kind, _as_dict(data))
        except ValueError:
            return Reply.json({"error": "missing message fields"}, 400)
        if found is None:
            return Reply.json({"status": "ignored"})
        event, key, chat_guid = found
        if not self._seen.first_time(key):
            return Reply.json({"status": "duplicate"})
        self.listener.background(self._deliver(event, chat_guid))
        return Reply.json({"status": "ok"})

    def normalise(self, kind: str, data: Mapping[str, Any]) -> tuple[InboundEvent, str, str] | None:
        """``(event, dedupe key, chat GUID)`` for a message webhook; None when it is not one we
        answer; ``ValueError`` when it lacks a chat or a sender."""
        if data.get("isFromMe") is True:
            return None
        if data.get("associatedMessageType") not in (None, "", 0):
            return None  # tapbacks and other reactions arrive as messages
        guid = _text(data.get("guid"))
        edited_at = data.get("dateEdited")
        edited = kind == "updated-message"
        if edited and not edited_at:
            return None  # delivery or read status of a message, nothing new to answer
        chat = _as_dict(next(iter(_as_list(data.get("chats"))), None))
        chat_guid = _text(chat.get("guid")) or _text(data.get("chatGuid"))
        handle = _as_dict(data.get("handle"))
        sender = _text(handle.get("address")) or _text(data.get("sender"))
        if not guid or not chat_guid or not sender:
            raise ValueError("a message needs a GUID, a chat and a sender")
        is_group = ";+;" in chat_guid or chat.get("style") == 43
        text = (_text(data.get("text")) or "").replace(_PLACEHOLDER, "").strip()
        metadata: dict[str, Any] = {}
        if is_group:
            text, mentioned = self._mention(text)
            if mentioned:
                metadata["mentioned"] = True
        else:
            self.guids.put(sender, chat_guid)
        media, message_kind = self._attachments(data)
        if not text and not media:
            return None
        if edited:
            metadata["edited"] = True
        identifier = _text(chat.get("chatIdentifier"))
        source = Source(
            platform=self.platform,
            chat_id=chat_guid,
            chat_type="group" if is_group else "dm",
            chat_name=_text(chat.get("displayName")) or identifier or sender,
            user_id=sender,
            user_name=_text(handle.get("displayName")) or sender,
        )
        quoted = _text(data.get("threadOriginatorGuid")) or None
        event = InboundEvent(
            source=source,
            text=text,
            kind=message_kind,
            message_id=guid,
            media=tuple(media),
            reply_to_id=quoted,
            reply_to_is_own=quoted is not None and quoted in self._sent,
            metadata=metadata,
            timestamp=_ms_to_datetime(data.get("dateCreated")),
        )
        key = f"{guid}:edit:{edited_at}" if edited else guid
        return event, key, chat_guid

    def _mention(self, text: str) -> tuple[str, bool]:
        """``(text without a leading wake word, matched)``."""
        patterns = self.options.mention_patterns
        if not text or not any(p.search(text) for p in patterns):
            return text, False
        stripped = text.lstrip()
        for pattern in patterns:
            found = pattern.match(stripped)
            if found is not None:
                rest = stripped[found.end() :].lstrip(" ,:-")
                return (rest or text), True
        return text, True

    def _attachments(self, data: Mapping[str, Any]) -> tuple[list[Attachment], MessageKind]:
        media: list[Attachment] = []
        kind = MessageKind.TEXT
        for raw in _as_list(data.get("attachments")):
            item = _as_dict(raw)
            att_guid = _text(item.get("guid"))
            if not att_guid:
                continue
            mime = _text(item.get("mimeType")).lower()
            if not mime:
                mime = mimetypes.guess_type(_text(item.get("transferName")))[0] or ""
            media.append(Attachment(ref=f"{ATTACHMENT_REF}{quote(att_guid, safe='')}", mime=mime))
            if kind is MessageKind.TEXT:
                kind = MessageKind.DOCUMENT
                voice = data.get("isAudioMessage") is True or _text(item.get("uti")).endswith("caf")
                if voice:
                    kind = MessageKind.VOICE
                else:
                    for prefix, found in _KINDS:
                        if mime.startswith(prefix):
                            kind = found
                            break
        return media, kind

    async def _deliver(self, event: InboundEvent, chat_guid: str) -> None:
        await self.emit(event)
        if self.options.send_read_receipts and self.can_use_private_api:
            await self._chat_action(chat_guid, "read", "POST")

    def register_fetchers(self, seat: FetcherSeat) -> Callable[[], None] | None:
        """Register `fetch_attachment` with the media intake (P15-23); returns the undo."""
        return seat.register_fetcher(self.platform, self.fetch_attachment)

    async def fetch_attachment(self, attachment: Attachment) -> AsyncIterator[bytes]:
        """One inbound attachment's bytes from the server (≤100 MiB, no redirects)."""
        if not attachment.ref.startswith(ATTACHMENT_REF):
            raise MediaRejected("it is not a BlueBubbles attachment")
        client = self._client
        if client is None:
            raise MediaRejected("BlueBubbles is not connected")
        att = attachment.ref[len(ATTACHMENT_REF) :]
        if not att or "/" in att:
            raise MediaRejected("it is not a BlueBubbles attachment")
        received = 0
        path = f"/api/v1/attachment/{att}/download"
        async with client.stream("GET", path, timeout=120.0) as resp:
            if resp.status_code != 200:
                raise MediaRejected("it could not be downloaded")
            declared = resp.headers.get("content-length")
            if declared and is_digits(declared) and int(declared) > MAX_ATTACHMENT_BYTES:
                raise MediaRejected("it is larger than 100 MB")
            async for chunk in resp.aiter_bytes():
                received += len(chunk)
                if received > MAX_ATTACHMENT_BYTES:
                    raise MediaRejected("it is larger than 100 MB")
                yield chunk

    # -- REST helpers ------------------------------------------------------------------------------

    async def _call(
        self, method: str, path: str, *, timeout: float = 30.0, **kw: Any
    ) -> tuple[SendResult, dict[str, Any]]:
        client = self._client
        if client is None:
            return SendResult.failure("connect error: BlueBubbles is not connected"), {}
        try:
            response = await client.request(method, path, timeout=timeout, **kw)
        except httpx.HTTPError as exc:
            return failure_from_exception(exc), {}
        body = _as_dict(self._json(response))
        if response.status_code >= 400:
            error = _as_dict(body.get("error"))
            detail = _text(error.get("message")) or _text(body.get("message")) or response.text
            result = failure_from_status(
                response.status_code, detail, response.headers, now=self.clock.now()
            )
            return result, body
        data = _as_dict(body.get("data"))
        ident = _text(data.get("guid")) or _text(data.get("messageGuid"))
        if ident:
            self._remember_sent(ident)
        return SendResult.sent(ident or f"bluebubbles-{uuid.uuid4().hex[:12]}"), data

    def _remember_sent(self, guid: str) -> None:
        self._sent[guid] = None
        self._sent.move_to_end(guid)
        while len(self._sent) > SENT_MAX:
            self._sent.popitem(last=False)

    async def resolve_chat(self, target: str) -> str | None:
        """The chat GUID for ``target``: a GUID as is, an address through the cache or an exact
        ``chatIdentifier`` match (never a participant match: one contact is in many chats)."""
        text = (target or "").strip()
        if not text:
            return None
        if ";" in text:
            return text
        cached = self.guids.get(text)
        if cached is not None:
            return cached
        client = self._client
        if client is None:
            return None
        try:
            response = await client.post("/api/v1/chat/query", json={"limit": 100, "offset": 0})
        except httpx.HTTPError as exc:
            log.debug("bluebubbles: chat lookup failed: %s", type(exc).__name__)
            return None
        if response.status_code >= 400:
            return None
        wanted = _address_key(text)
        for raw in _as_list(_as_dict(self._json(response)).get("data")):
            chat = _as_dict(raw)
            ident = _text(chat.get("chatIdentifier")) or _text(chat.get("identifier"))
            guid = _text(chat.get("guid"))
            if ident and guid and _address_key(ident) == wanted:
                self.guids.put(text, guid)
                return guid
        return None

    async def _chat_action(self, chat_id: str, action: str, method: str) -> bool:
        """A Private API chat action (typing, read); True when the call was made."""
        if not self.can_use_private_api or self._client is None:
            return False
        guid = await self.resolve_chat(chat_id)
        if guid is None:
            return False
        result, _ = await self._call(
            method, f"/api/v1/chat/{quote(guid, safe='')}/{action}", timeout=5.0
        )
        return result.ok

    # -- outbound ----------------------------------------------------------------------------------

    def bubbles(self, text: str) -> list[tuple[str, int]]:
        """``(piece, offset in text)``: a bubble per paragraph, long ones cut without markers."""
        if self.options.split_paragraphs:
            paragraphs = [(m.group(0), m.start()) for m in _PARAGRAPH.finditer(text)]
        else:
            paragraphs = [(text.strip(), len(text) - len(text.lstrip()))]
        out: list[tuple[str, int]] = []
        for para, offset in paragraphs or [(text, 0)]:
            if len(para) <= MAX_TEXT:
                out.append((para, offset))
                continue
            chunked = split_message(para, MAX_TEXT, marker=_no_marker)
            for chunk in chunked.chunks:
                piece = chunk.text.rstrip()
                if piece:
                    out.append((piece, offset + chunk.start))
        return out

    async def _send(
        self, chat_id: str, text: str, reply_to: str | None, meta: dict[str, Any]
    ) -> SendResult:
        pieces = self.bubbles(text)
        guid = await self.resolve_chat(chat_id)
        ids: list[str] = []
        for index, (piece, offset) in enumerate(pieces):
            if index:
                await self.slots.pace(chat_id)
            if guid is None:
                result, guid = await self._new_chat(chat_id, piece)
            else:
                result = await self._post_text(guid, piece, reply_to if index == 0 else None)
            if not result.ok:
                return self._stopped(result, ids, text[offset:])
            assert result.message_id is not None
            ids.append(result.message_id)
        if not ids:
            return SendResult(ok=False, error="nothing to send")
        return SendResult.sent(ids[-1], tuple(ids[:-1]))

    @staticmethod
    def _stopped(result: SendResult, ids: Sequence[str], rest: str) -> SendResult:
        if not ids:
            return result
        return replace(
            result,
            message_id=ids[-1],
            continuation_ids=tuple(ids[:-1]),
            partial=True,
            remainder=None if result.maybe_delivered else rest,
        )

    async def _post_text(self, guid: str, text: str, reply_to: str | None) -> SendResult:
        body: dict[str, Any] = {"chatGuid": guid, "tempGuid": self._temp(), "message": text}
        if reply_to and self.can_use_private_api:
            body.update(method="private-api", selectedMessageGuid=reply_to, partIndex=0)
        result, _ = await self._call("POST", "/api/v1/message/text", json=body)
        return result

    async def _new_chat(self, target: str, text: str) -> tuple[SendResult, str | None]:
        if not (self.private_api and _is_address(target)):
            return SendResult.failure(
                f"chat not found: no iMessage chat with {mask_address(target)}"
            ), None
        result, data = await self._call(
            "POST",
            "/api/v1/chat/new",
            json={"addresses": [target], "message": text, "tempGuid": self._temp()},
        )
        if not result.ok:
            return result, None
        chat_guid = _text(data.get("guid")) or None
        messages = [_as_dict(m) for m in _as_list(data.get("messages"))]
        message_guid = next((_text(m.get("guid")) for m in messages if _text(m.get("guid"))), "")
        if chat_guid:
            self.guids.put(target, chat_guid)
        if message_guid:
            self._remember_sent(message_guid)
            result = SendResult.sent(message_guid)
        return result, chat_guid

    @staticmethod
    def _temp() -> str:
        return f"temp-{uuid.uuid4().hex}"

    async def _send_media(
        self, kind: MediaKind, chat_id: str, path: str, caption: str | None, meta: dict[str, Any]
    ) -> SendResult:
        guid = await self.resolve_chat(chat_id)
        if guid is None:
            return SendResult.failure(f"chat not found: no iMessage chat for {chat_id}")
        try:
            size = os.stat(path).st_size
        except OSError:
            return SendResult(ok=False, error="the file to send was not found")
        if size > MAX_ATTACHMENT_BYTES:
            return SendResult.failure("file too large: BlueBubbles takes at most 100 MB")
        data = await asyncio.to_thread(Path(path).read_bytes)
        name = os.path.basename(path)
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        form = {"chatGuid": guid, "tempGuid": self._temp(), "name": name}
        if kind == "voice":
            form["isAudioMessage"] = "true"
        result, _ = await self._call(
            "POST",
            "/api/v1/message/attachment",
            data=form,
            files={"attachment": (name, data, mime)},
            timeout=120.0,
        )
        if not result.ok or not caption:
            return result
        reply = meta.get(REPLY_TO_KEY)
        follow = await self._post_text(guid, caption, reply if isinstance(reply, str) else None)
        if not follow.ok:
            return replace(follow, message_id=result.message_id, partial=True, remainder=caption)
        assert result.message_id is not None and follow.message_id is not None
        return SendResult.sent(follow.message_id, (result.message_id,))

    async def _typing(self, chat_id: str, meta: dict[str, Any]) -> None:
        await self._chat_action(chat_id, "typing", "POST")

    async def _typing_stop(self, chat_id: str) -> None:
        await self._chat_action(chat_id, "typing", "DELETE")

    async def mark_read(self, chat_id: str) -> bool:
        return await self._chat_action(chat_id, "read", "POST")

    async def send_tapback(
        self, chat_id: str, message_id: str, reaction: str, *, remove: bool = False
    ) -> bool:
        """A tapback (``love``, ``like``, ``dislike``, ``laugh``, ``emphasize``, ``question``) on
        a message; Private API only. Never raises."""
        if not self.can_use_private_api or not message_id:
            return False
        guid = await self.resolve_chat(chat_id)
        if guid is None:
            return False
        body = {
            "chatGuid": guid,
            "selectedMessageGuid": message_id,
            "reaction": f"-{reaction}" if remove else reaction,
            "partIndex": 0,
        }
        result, _ = await self._call("POST", "/api/v1/message/react", json=body, timeout=10.0)
        return result.ok

    async def processing_started(self, chat_id: str, message_id: str) -> None:
        await self.send_tapback(chat_id, message_id, PROCESSING_REACTIONS["started"])

    async def processing_finished(self, chat_id: str, message_id: str, *, ok: bool) -> None:
        await self.send_tapback(chat_id, message_id, PROCESSING_REACTIONS["started"], remove=True)
        await self.send_tapback(chat_id, message_id, PROCESSING_REACTIONS["ok" if ok else "failed"])

    # -- descriptive -------------------------------------------------------------------------------

    async def _chat_info(self, chat_id: str) -> ChatInfo:
        guid = await self.resolve_chat(chat_id)
        kind: Literal["group", "dm"] = "group" if guid and ";+;" in guid else "dm"
        if guid is None:
            return ChatInfo(name=chat_id, type=kind)
        result, data = await self._call("GET", f"/api/v1/chat/{quote(guid, safe='')}")
        if not result.ok:
            return ChatInfo(name=chat_id, type=kind)
        name = _text(data.get("displayName")) or _text(data.get("chatIdentifier")) or chat_id
        return ChatInfo(name=name, type=kind)

    async def list_channels(self) -> Sequence[ChannelEntry] | None:
        client = self._client
        if client is None:
            return None
        try:
            response = await client.post("/api/v1/chat/query", json={"limit": 100, "offset": 0})
        except httpx.HTTPError:
            return None
        if response.status_code >= 400:
            return None
        return list(self._entries(_as_list(_as_dict(self._json(response)).get("data"))))

    @staticmethod
    def _entries(rows: Iterable[Any]) -> Iterable[ChannelEntry]:
        for raw in rows:
            chat = _as_dict(raw)
            guid = _text(chat.get("guid"))
            if not guid:
                continue
            name = _text(chat.get("displayName")) or _text(chat.get("chatIdentifier")) or guid
            yield ChannelEntry(id=guid, name=name, type="group" if ";+;" in guid else "dm")
