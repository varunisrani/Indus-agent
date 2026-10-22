"""Signal through a user-run signal-cli daemon in HTTP mode (P16-05).

signal-cli (GPL-3.0) is never imported or bundled: the user links it to a phone number and runs
``signal-cli --account <number> daemon --http 127.0.0.1:8080``; this adapter only speaks HTTP to
it through ``httpx``:

- ``GET /api/v1/check`` at connect, and from the health monitor when the event stream has been
  quiet for ``STALE_AFTER`` seconds (checked every ``HEALTH_EVERY`` seconds);
- ``GET /api/v1/events`` (server-sent events) for inbound envelopes; a dropped stream is reopened
  after 2 s, doubling to 60 s, and the wait starts over once a stream delivered something or
  stayed up ``STABLE_AFTER`` seconds;
- ``POST /api/v1/rpc`` (JSON-RPC 2.0) for ``RPC_METHODS``.

Message ids are the send timestamps Signal uses to name messages. Groups are chat ids
``group.<groupId>`` and are dropped unless listed (``group_allowed_chats``); phone numbers are
personal data and appear in logs only masked. Inbound attachments are fetched lazily through
``fetch_attachment`` (registered with the media intake by `register_fetchers`), so nothing is
downloaded for a message the gateway does not admit. A send-only adapter
(``AdapterConfig.send_only``, ``indus send``) checks the daemon and sends; it opens no event stream
and no health monitor.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import logging
import os
import random
import re
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, ClassVar, Literal
from urllib.parse import quote, unquote, urlparse

import httpx

from indus.foundation.clock import Clock
from indus.foundation.digits import is_digits
from indus.gateway.adapters.base import (
    REPLY_TO_KEY,
    Adapter,
    AdapterConnectError,
    Capabilities,
    ChatInfo,
    FetcherSeat,
    MediaKind,
)
from indus.gateway.config import AdapterConfig, PlatformSettings, read_secret
from indus.gateway.core.authz import allow_set
from indus.gateway.core.events import Attachment, InboundEvent, MessageKind, Source
from indus.gateway.core.send import ErrorKind, SendResult, is_ambiguous_timeout
from indus.gateway.intake.media import MediaRejected
from indus.gateway.outbound.chunker import split_message, utf16_units

__all__ = [
    "ATTACHMENT_REF",
    "GROUP_PREFIX",
    "MAX_ATTACHMENTS_PER_SEND",
    "MAX_ATTACHMENT_BYTES",
    "MAX_TEXT",
    "RPC_METHODS",
    "SignalAdapter",
    "SignalRpcError",
    "mask_number",
    "sniff_family",
]

log = logging.getLogger(__name__)

MAX_TEXT = 8000
MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024
MAX_ATTACHMENTS_PER_SEND = 32
HEALTH_EVERY = 30.0
STALE_AFTER = 120.0
RETRY_FIRST = 2.0
RETRY_MAX = 60.0
STABLE_AFTER = 30.0
DEFAULT_REFILL = 4.0
CONTACTS_EVERY = 600.0
ECHO_TTL = 300.0
ECHO_MAX = 512
SENT_MAX = 500
AUTHORS_MAX = 1000
TYPING_PAUSE_AFTER = 3

ATTACHMENT_REF = "signal-attachment:"
GROUP_PREFIX = "group."
RPC_METHODS: tuple[str, ...] = (
    "send",
    "sendTyping",
    "sendReaction",
    "getAttachment",
    "listContacts",
)

# signal-cli's JSON-RPC error code for a rate-limit refusal.
_RATE_LIMIT_CODE = -5
_PLACEHOLDER = "\ufffc"
_RETRY_AFTER = re.compile(r"retry after (\d+(?:\.\d+)?)\s*s", re.IGNORECASE)
_E164 = re.compile(r"^\+\d{7,15}$")
_SPACES = re.compile(r"[ \t]{2,}")

# Per-recipient result types of a ``send``, as classifier-readable text (P15-04 rules).
_RESULT_TEXT: Mapping[str, str] = {
    "UNREGISTERED_FAILURE": "chat not found: the recipient is not registered on Signal",
    "IDENTITY_FAILURE": (
        "forbidden: the recipient's safety number changed; trust the new identity in signal-cli"
    ),
    "NETWORK_FAILURE": "connection reset: signal-cli could not reach the Signal servers",
    "PROOF_REQUIRED_FAILURE": (
        "the Signal server wants a captcha proof before more messages go out"
    ),
}
_GROUP_GONE = ("invalid group id", "group not found", "unknown group")


def mask_number(value: str) -> str:
    """A phone number for logs: ``+155****4567``; anything else unchanged."""
    if _E164.match(value or ""):
        return f"{value[:4]}****{value[-4:]}"
    return value


def _is_number(value: str) -> bool:
    return bool(_E164.match(value or ""))


Family = Literal["image", "av", "pdf", "zip", "html", "executable", "unknown"]


def sniff_family(head: bytes) -> Family:
    """The coarse file family the first bytes show (magic numbers only)."""
    if (
        head.startswith((b"\x89PNG", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"BM"))
        or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")
        or head[4:12] in (b"ftypheic", b"ftypavif")
    ):
        return "image"
    if (
        head.startswith((b"OggS", b"ID3", b"fLaC", b"\x1aE\xdf\xa3"))
        or head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2", b"\xff\xf1", b"\xff\xf9")
        or (head[:4] == b"RIFF" and head[8:12] in (b"WAVE", b"AVI "))
        or head[4:8] == b"ftyp"
    ):
        return "av"
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        return "zip"
    if head.startswith((b"MZ", b"\x7fELF", b"#!", b"\xca\xfe\xba\xbe", b"\xcf\xfa\xed\xfe")):
        return "executable"
    text = head.lstrip()[:15].lower()
    if text.startswith((b"<!doctype html", b"<html", b"<script")):
        return "html"
    return "unknown"


def _declared_family(mime: str, ref: str) -> str:
    major = (mime or "").split("/", 1)[0].strip().lower()
    if major == "image":
        return "image"
    if major in ("audio", "video"):
        return "av"
    return ""


def _check_bytes(attachment: Attachment, head: bytes) -> None:
    """Refuse bytes that contradict the attachment's declared kind (type confusion)."""
    declared = _declared_family(attachment.mime, attachment.ref)
    found = sniff_family(head)
    if declared == "image" and found != "image":
        raise MediaRejected("it is not an image")
    if declared == "av" and found in ("image", "pdf", "zip", "html", "executable"):
        raise MediaRejected("it is not audio or video")


def _ms_to_datetime(value: object) -> datetime | None:
    if not isinstance(value, int | float) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _utf16_index(text: str, units: int) -> int:
    """The code-point index at ``units`` UTF-16 units into ``text`` (Signal counts in UTF-16)."""
    seen = 0
    for index, char in enumerate(text):
        if seen >= units:
            return index
        seen += 2 if ord(char) > 0xFFFF else 1
    return len(text)


def _as_dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}  # pyright: ignore[reportUnknownArgumentType]


def _as_list(value: object) -> list[Any]:
    return list(value) if isinstance(value, list) else []  # pyright: ignore[reportUnknownArgumentType]


# -- errors ----------------------------------------------------------------------------------------


class SignalRpcError(Exception):
    """signal-cli refused a call. ``str()`` is text the P15-04 classifier reads."""

    def __init__(
        self,
        message: str,
        *,
        rate_limited: bool = False,
        retry_after: float | None = None,
        transient: bool = False,
    ) -> None:
        super().__init__(message)
        self.rate_limited = rate_limited
        self.retry_after = retry_after
        self.transient = transient

    @classmethod
    def from_error(cls, method: str, error: object) -> SignalRpcError:
        data = _as_dict(error)
        message = str(data.get("message") or error or "error")
        lowered = message.lower()
        waits = [
            float(r["retryAfterSeconds"])
            for r in _as_list(_as_dict(_as_dict(data.get("data")).get("response")).get("results"))
            if isinstance(r, Mapping) and isinstance(r.get("retryAfterSeconds"), int | float)
        ]
        found = _RETRY_AFTER.search(message)
        if found is not None:
            waits.append(float(found.group(1)))
        limited = (
            data.get("code") == _RATE_LIMIT_CODE
            or "[429]" in message
            or any(n in lowered for n in ("ratelimit", "rate limit", "retrylaterexception"))
            or bool(found)
        )
        if limited:
            wait = max(waits) if waits else DEFAULT_REFILL
            return cls(f"rate limit: {message}", rate_limited=True, retry_after=wait)
        if any(n in lowered for n in _GROUP_GONE):
            return cls(f"chat not found: {message}")
        return cls(f"signal-cli {method}: {message}")

    @classmethod
    def from_result(cls, result: Mapping[str, Any]) -> SignalRpcError:
        kind = str(result.get("type") or "")
        if kind == "RATE_LIMIT_FAILURE":
            raw = result.get("retryAfterSeconds")
            wait = float(raw) if isinstance(raw, int | float) and raw > 0 else DEFAULT_REFILL
            return cls(
                "rate limit: Signal is rate limiting this sender",
                rate_limited=True,
                retry_after=wait,
            )
        if kind == "NETWORK_FAILURE":
            return cls(_RESULT_TEXT[kind], transient=True)
        text = _RESULT_TEXT.get(kind) or f"signal-cli send: recipient result {kind or 'failure'}"
        return cls(text)


# -- server-sent events ----------------------------------------------------------------------------

EVENTS_PATH = "/api/v1/events"


class _AccountQuery(httpx.AsyncBaseTransport):
    """Adds ``account=<number>`` to the event-stream request inside the transport.

    httpx logs a request, keeps it on ``response.request`` and names its URL in error texts before
    or after the transport runs, never from the transport's own copy: so the phone number reaches
    signal-cli and nothing else.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport, account: str) -> None:
        self._inner = inner
        self._account = account

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(EVENTS_PATH):
            request = httpx.Request(
                request.method,
                request.url.copy_merge_params({"account": self._account}),
                headers=request.headers,
                stream=request.stream,
                extensions=request.extensions,
            )
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


class _EventReader:
    """Collects ``data:`` lines into one payload per event (blank line ends an event)."""

    def __init__(self) -> None:
        self._data: list[str] = []

    def feed(self, line: str) -> str | None:
        if line == "":
            if not self._data:
                return None
            payload, self._data = "\n".join(self._data), []
            return payload
        if line.startswith(":"):
            return None
        field, _, value = line.partition(":")
        if field == "data":
            self._data.append(value[1:] if value.startswith(" ") else value)
        return None


# -- the adapter -----------------------------------------------------------------------------------


class SignalAdapter(Adapter):
    platform: ClassVar[str] = "signal"
    capabilities: ClassVar[Capabilities] = Capabilities(
        max_len=MAX_TEXT,
        len_fn=utf16_units,
        markup="plain",
        splits_natively=True,
    )
    vendor_names: ClassVar[Mapping[str, tuple[str, ...]]] = {
        "httpx:AsyncClient": ("get", "post", "stream", "aclose"),
        "httpx:Response": ("aiter_lines", "json"),
    }

    def __init__(
        self,
        *,
        clock: Clock,
        http_url: str,
        account: str,
        settings: PlatformSettings | None = None,
        group_chats: Iterable[str] = (),
        ignore_stories: bool = True,
        proxy: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        rng: Callable[[], float] = random.random,
        send_only: bool = False,
    ) -> None:
        super().__init__(clock=clock, settings=settings, secrets=(account,))
        self.http_url = (http_url or "").strip().rstrip("/")
        self.account = (account or "").strip()
        self.group_chats = frozenset(c.strip() for c in group_chats if c.strip())
        self.ignore_stories = ignore_stories
        # ``indus send`` (``AdapterConfig.send_only``): check the daemon and send, no stream.
        self.send_only = send_only
        self._proxy = proxy
        self._transport = transport
        self._rng = rng
        self._client: httpx.AsyncClient | None = None
        self._running = False
        self._listener: asyncio.Task[None] | None = None
        self._monitor: asyncio.Task[None] | None = None
        self._session: asyncio.Task[None] | None = None
        self._last_activity = 0.0
        self._delivered = False
        self._rpc_ids = 0
        self._own_uuid: str | None = None
        self._uuid_by_number: dict[str, str] = {}
        self._names: dict[str, str] = {}
        self._contacts_at: float | None = None
        self._contacts_lock = asyncio.Lock()
        # Our sends: timestamps for echo filtering (TTL) and for spotting quotes of our messages.
        self._echoes: OrderedDict[int, float] = OrderedDict()
        self._sent: OrderedDict[str, None] = OrderedDict()
        # Recent inbound message timestamp → author, so a reply can quote it.
        self._authors: OrderedDict[str, tuple[str, str]] = OrderedDict()
        self._typing_fails: dict[str, int] = {}
        self._typing_paused_until: dict[str, float] = {}

    @classmethod
    def from_config(cls, config: AdapterConfig) -> SignalAdapter:
        """Settings from the secret store / env first, then ``platforms.signal.extra``."""
        scope, extra = config.scope, config.extra
        http_url = read_secret(scope, "SIGNAL_HTTP_URL") or str(extra.get("http_url") or "")
        account = read_secret(scope, "SIGNAL_ACCOUNT") or str(extra.get("account") or "")
        chats = allow_set(extra.get("group_allowed_chats")) | allow_set(
            scope.env.get("INDUS_SIGNAL_GROUP_ALLOWED_CHATS")
        )
        ignore = extra.get("ignore_stories")
        host = urlparse(http_url).hostname or ""
        loopback = host in ("127.0.0.1", "::1", "localhost")
        return cls(
            clock=config.clock,
            http_url=http_url,
            account=account,
            settings=config.settings,
            group_chats=chats,
            ignore_stories=ignore is not False,
            proxy=None if loopback else config.proxy,
            send_only=config.send_only,
        )

    def __repr__(self) -> str:
        state = "connected" if self.connected else "disconnected"
        return f"<SignalAdapter {mask_number(self.account)} {state}>"

    # -- lifecycle ---------------------------------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self.http_url}{path}"

    async def _open(self, is_reconnect: bool) -> None:
        wanted = (("SIGNAL_HTTP_URL", self.http_url), ("SIGNAL_ACCOUNT", self.account))
        missing = [name for name, value in wanted if not value]
        if missing:
            raise AdapterConnectError(
                f"Signal is not configured: set {', '.join(missing)}",
                retryable=False,
                code="config",
            )
        if urlparse(self.http_url).scheme not in ("http", "https"):
            raise AdapterConnectError(
                "SIGNAL_HTTP_URL must be an http:// or https:// address",
                retryable=False,
                code="config",
            )
        inner = self._transport or httpx.AsyncHTTPTransport(proxy=self._proxy, trust_env=False)
        client = httpx.AsyncClient(
            transport=_AccountQuery(inner, self.account),
            trust_env=False,
            timeout=httpx.Timeout(30.0, connect=10.0),
        )
        try:
            response = await client.get(self._url("/api/v1/check"), timeout=10.0)
        except httpx.HTTPError as exc:
            await client.aclose()
            raise AdapterConnectError(
                f"cannot reach signal-cli at {self.http_url}: {type(exc).__name__}", retryable=True
            ) from None
        if response.status_code != 200:
            await client.aclose()
            raise AdapterConnectError(
                f"signal-cli health check failed (HTTP {response.status_code})", retryable=True
            )
        self._client = client
        self._running = True
        self._last_activity = self.clock.monotonic()
        if not self.send_only:
            self._listener = asyncio.ensure_future(self._listen())
            self._monitor = asyncio.ensure_future(self._watch())
        log.info("signal: connected to %s as %s", self.http_url, mask_number(self.account))

    async def _close(self) -> None:
        self._running = False
        for task in (self._monitor, self._listener):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._monitor = self._listener = self._session = None
        if self._client is not None:
            client, self._client = self._client, None
            await client.aclose()

    # -- the event stream --------------------------------------------------------------------------

    async def _listen(self) -> None:
        delay = RETRY_FIRST
        while self._running:
            started = self.clock.monotonic()
            self._delivered = False
            self._session = asyncio.ensure_future(self._stream_once())
            try:
                await self._session
                log.warning("signal: event stream ended")
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
                log.warning("signal: event stream restarted by the health monitor")
            except httpx.HTTPError as exc:
                log.warning("signal: event stream failed: %s", self.scrub(type(exc).__name__))
            except Exception as exc:
                log.warning("signal: event stream failed: %s", self.scrub(str(exc)))
            finally:
                self._session = None
            if not self._running:
                return
            if self._delivered or self.clock.monotonic() - started >= STABLE_AFTER:
                delay = RETRY_FIRST
            wait = delay * (1.0 + 0.2 * self._rng())
            log.info("signal: reopening the event stream in %.1f s", wait)
            await self.clock.sleep(wait)
            delay = min(delay * 2, RETRY_MAX)

    async def _stream_once(self) -> None:
        client = self._client
        if client is None:
            return
        url = self._url(EVENTS_PATH)  # the transport adds the account (`_AccountQuery`)
        timeout = httpx.Timeout(30.0, read=None)
        headers = {"Accept": "text/event-stream"}
        async with client.stream("GET", url, headers=headers, timeout=timeout) as response:
            if response.status_code != 200:
                raise SignalRpcError(f"event stream refused (HTTP {response.status_code})")
            self._last_activity = self.clock.monotonic()
            reader = _EventReader()
            async for line in response.aiter_lines():
                self._last_activity = self.clock.monotonic()
                if line.strip():
                    self._delivered = True
                payload = reader.feed(line.rstrip("\r"))
                if payload is not None:
                    await self._on_payload(payload)

    async def _watch(self) -> None:
        while self._running:
            await self.clock.sleep(HEALTH_EVERY)
            quiet = self.clock.monotonic() - self._last_activity
            if quiet <= STALE_AFTER:
                continue
            healthy = False
            client = self._client
            if client is not None:
                try:
                    response = await client.get(self._url("/api/v1/check"), timeout=10.0)
                    healthy = response.status_code == 200
                except httpx.HTTPError:
                    healthy = False
            if healthy:
                log.debug("signal: daemon healthy, stream quiet for %.0f s", quiet)
                self._last_activity = self.clock.monotonic()
                continue
            log.warning("signal: daemon check failed after %.0f s of silence; reconnecting", quiet)
            session = self._session
            if session is not None and not session.done():
                session.cancel()

    async def _on_payload(self, payload: str) -> None:
        try:
            data = json.loads(payload)
        except ValueError:
            log.debug("signal: event with invalid JSON ignored")
            return
        body = _as_dict(data)
        account = body.get("account")
        if isinstance(account, str) and account and account != self.account:
            return
        envelope = _as_dict(body.get("envelope", body))
        try:
            event = self.normalise(envelope)
        except Exception:
            log.exception("signal: envelope could not be read")
            return
        if event is not None:
            await self.emit(event)

    # -- inbound -----------------------------------------------------------------------------------

    def _own(self, value: str | None) -> bool:
        return bool(value) and value in (self.account, self._own_uuid)

    def _learn(self, number: str | None, service_id: str | None) -> None:
        if not number or not service_id:
            return
        try:
            uuid.UUID(service_id)
        except (ValueError, AttributeError, TypeError):
            return
        self._uuid_by_number[number] = service_id
        if number == self.account:
            self._own_uuid = service_id

    def _group_allowed(self, chat_id: str) -> bool:
        return "*" in self.group_chats or chat_id in self.group_chats

    def _is_echo(self, timestamp: object) -> bool:
        if not isinstance(timestamp, int):
            return False
        now = self.clock.monotonic()
        while self._echoes and next(iter(self._echoes.values())) < now - ECHO_TTL:
            self._echoes.popitem(last=False)
        return self._echoes.pop(timestamp, None) is not None

    def _remember_sent(self, timestamp: int) -> None:
        now = self.clock.monotonic()
        self._echoes.pop(timestamp, None)
        self._echoes[timestamp] = now
        while len(self._echoes) > ECHO_MAX:
            self._echoes.popitem(last=False)
        key = str(timestamp)
        self._sent.pop(key, None)
        self._sent[key] = None
        while len(self._sent) > SENT_MAX:
            self._sent.popitem(last=False)
        self._authors.pop(key, None)

    def _remember_author(self, message_id: str, chat_id: str, author: str) -> None:
        self._authors.pop(message_id, None)
        self._authors[message_id] = (chat_id, author)
        while len(self._authors) > AUTHORS_MAX:
            self._authors.popitem(last=False)

    def _text_and_mention(self, data: Mapping[str, Any]) -> tuple[str, bool]:
        """The message text with mentions rendered; True when one of them is us."""
        text = str(data.get("message") or "")
        mentions = [_as_dict(m) for m in _as_list(data.get("mentions"))]
        mentioned = False
        if not text or not mentions:
            return text, False
        spans: list[tuple[int, int, str]] = []
        for mention in mentions:
            start, length = mention.get("start"), mention.get("length")
            if not isinstance(start, int) or not isinstance(length, int) or length < 1:
                continue
            number = str(mention.get("number") or "")
            service = str(mention.get("uuid") or "")
            if self._own(number) or self._own(service):
                mentioned = True
                label = ""
            else:
                label = "@" + (number or service or str(mention.get("name") or "someone"))
            begin = _utf16_index(text, start)
            end = _utf16_index(text, start + length)
            spans.append((begin, end, label))
        for begin, end, label in sorted(spans, reverse=True):
            if _PLACEHOLDER in text[begin:end]:
                text = text[:begin] + label + text[end:]
        if mentioned:
            text = _SPACES.sub(" ", text).strip()
        return text, mentioned

    def _attachments(self, data: Mapping[str, Any]) -> tuple[list[Attachment], MessageKind]:
        found: list[Attachment] = []
        kind = MessageKind.TEXT
        for raw in _as_list(data.get("attachments")):
            item = _as_dict(raw)
            att_id = str(item.get("id") or "")
            if not att_id:
                continue
            size = item.get("size")
            if isinstance(size, int) and size > MAX_ATTACHMENT_BYTES:
                log.warning("signal: attachment of %d bytes skipped (limit 100 MiB)", size)
                continue
            if len(found) >= MAX_ATTACHMENTS_PER_SEND:
                break
            mime = str(item.get("contentType") or "")
            ref = ATTACHMENT_REF + quote(att_id, safe="")
            name = str(item.get("filename") or "")
            if name:
                ref += "#" + quote(name, safe="")
            if not found:
                major = mime.split("/", 1)[0].lower()
                if major == "audio":
                    kind = MessageKind.VOICE if item.get("voiceNote") else MessageKind.AUDIO
                elif major == "image":
                    kind = MessageKind.PHOTO
                elif major == "video":
                    kind = MessageKind.VIDEO
                else:
                    kind = MessageKind.DOCUMENT
            found.append(Attachment(ref=ref, mime=mime))
        return found, kind

    def normalise(self, envelope: Mapping[str, Any]) -> InboundEvent | None:
        """One signal-cli envelope as an event, or None when it is not a message for us."""
        number = str(envelope.get("sourceNumber") or "") or None
        service = str(envelope.get("sourceUuid") or "") or None
        legacy = str(envelope.get("source") or "") or None
        if number is None and legacy is not None and _is_number(legacy):
            number = legacy
        self._learn(number, service)
        sender = number or service or legacy
        if not sender:
            return None
        metadata: dict[str, Any] = {}
        message_id: str | None = None
        sync = envelope.get("syncMessage")
        if sync is not None:
            sent = _as_dict(_as_dict(sync).get("sentMessage"))
            if not sent:
                return None
            destination = str(
                sent.get("destinationNumber")
                or sent.get("destination")
                or sent.get("destinationUuid")
                or ""
            )
            in_group = bool(_as_dict(sent.get("groupInfo")).get("groupId"))
            if not (self._own(destination) or in_group) or not self._own(sender):
                return None
            if self._is_echo(sent.get("timestamp")):
                return None
            data = sent
            metadata["note_to_self"] = True
        else:
            if self._own(sender):
                return None
            if self.ignore_stories and envelope.get("storyMessage") is not None:
                return None
            data = _as_dict(envelope.get("dataMessage"))
            if not data:
                edit = _as_dict(envelope.get("editMessage"))
                data = _as_dict(edit.get("dataMessage"))
                if not data:
                    return None
                metadata["edited"] = True
                target = edit.get("targetSentTimestamp")
                if isinstance(target, int):
                    message_id = str(target)

        group = _as_dict(data.get("groupInfo"))
        group_id = str(group.get("groupId") or "")
        chat_id = f"{GROUP_PREFIX}{group_id}" if group_id else sender
        if group_id and not self._group_allowed(chat_id):
            log.debug("signal: message in a group that is not listed ignored")
            return None
        if metadata.get("note_to_self") and not group_id:
            chat_id = self.account

        text, mentioned = self._text_and_mention(data)
        if mentioned:
            metadata["mentioned"] = True
        media, kind = self._attachments(data)
        if not text.strip() and not media:
            return None

        stamp = envelope.get("timestamp") or data.get("timestamp")
        if message_id is None and isinstance(stamp, int):
            message_id = str(stamp)
        if message_id is not None:
            self._remember_author(message_id, chat_id, sender)

        quote_data = _as_dict(data.get("quote"))
        reply_to_id = str(quote_data["id"]) if quote_data.get("id") else None
        quote_author = next(
            (
                str(quote_data[k])
                for k in ("authorNumber", "authorUuid", "author")
                if quote_data.get(k)
            ),
            None,
        )
        own_quote = bool(reply_to_id and reply_to_id in self._sent) or self._own(quote_author)

        source = Source(
            platform=self.platform,
            chat_id=chat_id,
            chat_type="group" if group_id else "dm",
            chat_name=str(group.get("groupName") or "") or None
            if group_id
            else str(envelope.get("sourceName") or "") or None,
            user_id=sender,
            user_name=str(envelope.get("sourceName") or "") or sender,
            user_id_alt=service if service and service != sender else None,
        )
        return InboundEvent(
            source=source,
            text=text,
            kind=kind,
            message_id=message_id,
            media=tuple(media),
            reply_to_id=reply_to_id,
            reply_to_text=str(quote_data.get("text") or "") or None,
            reply_to_author_id=quote_author,
            reply_to_is_own=own_quote,
            metadata=metadata,
            timestamp=_ms_to_datetime(stamp),
        )

    def register_fetchers(self, seat: FetcherSeat) -> Callable[[], None] | None:
        """Register `fetch_attachment` with the media intake (P15-23); returns the undo."""
        return seat.register_fetcher(self.platform, self.fetch_attachment)

    async def fetch_attachment(self, attachment: Attachment) -> AsyncIterator[bytes]:
        """The intake's fetcher (registered by `register_fetchers`): one attachment's bytes."""
        if not attachment.ref.startswith(ATTACHMENT_REF):
            raise MediaRejected("it is not a Signal attachment")
        att_id = unquote(attachment.ref[len(ATTACHMENT_REF) :].split("#", 1)[0])
        result = await self._rpc(
            "getAttachment", {"account": self.account, "id": att_id}, timeout=120.0
        )
        encoded = _as_dict(result).get("data") if isinstance(result, Mapping) else result
        if not isinstance(encoded, str) or not encoded:
            raise MediaRejected("it could not be downloaded")
        if len(encoded) > (MAX_ATTACHMENT_BYTES // 3 + 1) * 4:
            raise MediaRejected("it is larger than 100 MB")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise MediaRejected("it could not be downloaded") from None
        if not raw:
            raise MediaRejected("it is empty")
        _check_bytes(attachment, raw[:32])
        yield raw

    # -- the RPC -----------------------------------------------------------------------------------

    async def _rpc(self, method: str, params: Mapping[str, Any], *, timeout: float = 30.0) -> Any:
        client = self._client
        if client is None:
            raise ConnectionError("signal-cli is not connected")
        self._rpc_ids += 1
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": dict(params),
            "id": str(self._rpc_ids),
        }
        response = await client.post(self._url("/api/v1/rpc"), json=payload, timeout=timeout)
        if response.status_code == 429:
            raw = response.headers.get("retry-after", "")
            wait = float(raw) if is_digits(raw.replace(".", "", 1)) else DEFAULT_REFILL
            raise SignalRpcError("rate limit: HTTP 429", rate_limited=True, retry_after=wait)
        if response.status_code >= 500:
            raise SignalRpcError(
                f"signal-cli {method}: HTTP {response.status_code}", transient=True
            )
        try:
            body = _as_dict(response.json())
        except ValueError:
            raise SignalRpcError(f"signal-cli {method}: unreadable answer") from None
        if "error" in body:
            raise SignalRpcError.from_error(method, body["error"])
        return body.get("result")

    def _failure(
        self, exc: BaseException, delivered: Sequence[str] = (), remainder: str | None = None
    ) -> SendResult:
        if isinstance(exc, SignalRpcError):
            result = SendResult.failure(
                str(exc),
                retry_after=exc.retry_after,
                delivered_ids=tuple(delivered),
                remainder=remainder,
            )
            if exc.transient:
                result = replace(result, kind=ErrorKind.TRANSIENT, retryable=True)
            return result
        if is_ambiguous_timeout(exc):
            remainder = None
        return SendResult.failure(exc=exc, delivered_ids=tuple(delivered), remainder=remainder)

    async def _target(self, chat_id: str) -> dict[str, Any]:
        if chat_id.startswith(GROUP_PREFIX):
            return {"account": self.account, "groupId": chat_id[len(GROUP_PREFIX) :]}
        return {"account": self.account, "recipient": [await self._recipient(chat_id)]}

    async def _recipient(self, chat_id: str) -> str:
        """A phone number upgraded to the UUID signal-cli knows for it, when it knows one."""
        if not _is_number(chat_id):
            return chat_id
        known = self._uuid_by_number.get(chat_id)
        if known is not None:
            return known
        async with self._contacts_lock:
            now = self.clock.monotonic()
            if self._contacts_at is None or now - self._contacts_at >= CONTACTS_EVERY:
                self._contacts_at = now
                await self._load_contacts()
        return self._uuid_by_number.get(chat_id, chat_id)

    async def _load_contacts(self) -> None:
        try:
            contacts = await self._rpc(
                "listContacts", {"account": self.account, "allRecipients": True}
            )
        except Exception as exc:
            log.debug("signal: contact list unavailable: %s", self.scrub(str(exc)))
            return
        for raw in _as_list(contacts):
            contact = _as_dict(raw)
            number = str(contact.get("number") or "")
            profile = _as_dict(contact.get("profile"))
            service = str(contact.get("uuid") or contact.get("serviceId") or "")
            self._learn(number or None, service or None)
            name = str(contact.get("name") or profile.get("givenName") or "")
            if number and name:
                self._names[number] = name

    def _accept(self, chat_id: str, result: object) -> str:
        """The send's timestamp, or the reason it failed (``SignalRpcError``)."""
        body = _as_dict(result)
        rows = [_as_dict(r) for r in _as_list(body.get("results"))]
        failed = [
            r for r in rows if r.get("type", "SUCCESS") != "SUCCESS" or r.get("success") is False
        ]
        if failed:
            delivered = len(rows) - len(failed)
            if chat_id.startswith(GROUP_PREFIX) and delivered > 0:
                log.warning("signal: group message reached %d of %d members", delivered, len(rows))
            else:
                raise SignalRpcError.from_result(failed[0])
        stamp = body.get("timestamp")
        if not isinstance(stamp, int):
            raise SignalRpcError("signal-cli send: no timestamp in the answer")
        self._remember_sent(stamp)
        return str(stamp)

    def _quote(self, reply_to: str | None) -> dict[str, Any]:
        if not reply_to or not is_digits(reply_to):
            return {}
        if reply_to in self._sent:
            return {"quoteTimestamp": int(reply_to), "quoteAuthor": self.account}
        found = self._authors.get(reply_to)
        if found is None:
            return {}
        return {"quoteTimestamp": int(reply_to), "quoteAuthor": found[1]}

    # -- outbound ----------------------------------------------------------------------------------

    async def _send(
        self, chat_id: str, text: str, reply_to: str | None, meta: dict[str, Any]
    ) -> SendResult:
        chunked = split_message(text, MAX_TEXT, len_fn=utf16_units)
        target = await self._target(chat_id)
        quoted = self._quote(reply_to)
        ids: list[str] = []
        for index, chunk in enumerate(chunked.chunks):
            if index:
                await self.slots.pace(chat_id)
            params = {**target, "message": chunk.text}
            if index == 0:
                params.update(quoted)
            try:
                ids.append(self._accept(chat_id, await self._rpc("send", params)))
            except Exception as exc:
                rest = chunked.remainder(index) if ids else None
                return self._failure(exc, ids, rest)
        return SendResult.sent(ids[-1], tuple(ids[:-1]))

    async def _send_media(
        self, kind: MediaKind, chat_id: str, path: str, caption: str | None, meta: dict[str, Any]
    ) -> SendResult:
        return await self._send_files(chat_id, [path], caption, meta.get(REPLY_TO_KEY))

    async def send_files(
        self,
        chat_id: str,
        paths: Sequence[str],
        *,
        caption: str | None = None,
        reply_to: str | None = None,
    ) -> SendResult:
        """Several files in as few messages as Signal allows (32 attachments per message)."""
        try:
            async with self.slots.hold(chat_id):
                result = await self._send_files(chat_id, list(paths), caption, reply_to)
        except Exception as exc:
            return self._failed(exc)
        return self._scrubbed(result)

    async def _send_files(
        self, chat_id: str, paths: list[str], caption: str | None, reply_to: object
    ) -> SendResult:
        usable: list[str] = []
        for path in paths:
            try:
                size = os.stat(path).st_size
            except OSError:
                log.warning("signal: attachment %s not found", os.path.basename(path))
                continue
            if size > MAX_ATTACHMENT_BYTES:
                log.warning("signal: attachment of %d bytes refused (limit 100 MiB)", size)
                continue
            usable.append(path)
        if not usable:
            return SendResult(
                ok=False, error="no attachment could be sent (missing or over 100 MB)"
            )
        target = await self._target(chat_id)
        quoted = self._quote(reply_to if isinstance(reply_to, str) else None)
        ids: list[str] = []
        for start in range(0, len(usable), MAX_ATTACHMENTS_PER_SEND):
            batch = usable[start : start + MAX_ATTACHMENTS_PER_SEND]
            params: dict[str, Any] = {
                **target,
                "message": (caption or "") if not start else "",
                "attachments": batch,
            }
            if not start:
                params.update(quoted)
            else:
                await self.slots.pace(chat_id)
            result = await self._send_batch(chat_id, params, len(batch))
            if not result.ok:
                return result.with_earlier(tuple(ids)) if ids else result
            assert result.message_id is not None
            ids.append(result.message_id)
        return SendResult.sent(ids[-1], tuple(ids[:-1]))

    async def _send_batch(self, chat_id: str, params: dict[str, Any], count: int) -> SendResult:
        """One ``send`` with attachments; a rate-limit reply is retried once after its refill."""
        timeout = max(60.0, 5.0 * count)
        for attempt in (1, 2):
            try:
                return SendResult.sent(
                    self._accept(chat_id, await self._rpc("send", params, timeout=timeout))
                )
            except SignalRpcError as exc:
                wait = exc.retry_after or DEFAULT_REFILL
                if not exc.rate_limited or attempt == 2:
                    return self._failure(exc)
                if wait > self.capabilities.inline_wait_cap:
                    log.warning("signal: attachment rate limit of %.0f s handed over", wait)
                    return self._failure(exc)
                log.info("signal: attachment rate limit; retrying once in %.0f s", wait)
                await self.clock.sleep(wait)
            except Exception as exc:
                return self._failure(exc)
        return SendResult(ok=False, error="send failed")

    async def _typing(self, chat_id: str, meta: dict[str, Any]) -> None:
        now = self.clock.monotonic()
        if now < self._typing_paused_until.get(chat_id, 0.0):
            return
        try:
            await self._rpc("sendTyping", await self._target(chat_id), timeout=10.0)
        except Exception as exc:
            fails = self._typing_fails.get(chat_id, 0) + 1
            self._typing_fails[chat_id] = fails
            level = logging.WARNING if fails == 1 else logging.DEBUG
            log.log(level, "signal: typing indicator failed: %s", self.scrub(str(exc)))
            if fails >= TYPING_PAUSE_AFTER:
                pause = min(RETRY_MAX, 16.0 * 2 ** (fails - TYPING_PAUSE_AFTER))
                self._typing_paused_until[chat_id] = now + pause
            return
        self._typing_fails.pop(chat_id, None)
        self._typing_paused_until.pop(chat_id, None)

    async def _typing_stop(self, chat_id: str) -> None:
        self._typing_fails.pop(chat_id, None)
        self._typing_paused_until.pop(chat_id, None)
        params = {**await self._target(chat_id), "stop": True}
        with contextlib.suppress(Exception):
            await self._rpc("sendTyping", params, timeout=10.0)

    async def send_reaction(
        self,
        chat_id: str,
        target_author: str,
        target_timestamp: int,
        emoji: str,
        *,
        remove: bool = False,
    ) -> bool:
        """React to a message (its author and timestamp); False when it did not work."""
        params: dict[str, Any] = {
            **await self._target(chat_id),
            "emoji": emoji,
            "targetAuthor": target_author,
            "targetTimestamp": target_timestamp,
        }
        if remove:
            params["remove"] = True
        try:
            await self._rpc("sendReaction", params)
        except Exception as exc:
            log.debug("signal: reaction failed: %s", self.scrub(str(exc)))
            return False
        return True

    async def _chat_info(self, chat_id: str) -> ChatInfo:
        if chat_id.startswith(GROUP_PREFIX):
            return ChatInfo(name=chat_id, type="group")
        if chat_id not in self._names and self._contacts_at is None:
            async with self._contacts_lock:
                self._contacts_at = self.clock.monotonic()
                await self._load_contacts()
        return ChatInfo(name=self._names.get(chat_id, chat_id), type="dm")
