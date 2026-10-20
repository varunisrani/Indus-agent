"""The LINE adapter (P16-23): signed webhook in, reply token or push out.

Inbound: LINE posts chat events to our callback listener (P16-01 kit; ``/line/webhook`` on port
8646, body ≤1 MiB). ``X-Line-Signature`` is the base64 HMAC-SHA256 of the raw body under the
channel secret; it is checked in constant time before the body is parsed (bad → 401). The answer
is 200 at once and the events are handled in the background, in order: standby-mode events,
repeats (``webhookEventId``) and our own bot are dropped; user chats are DMs, groups and rooms
are groups; a mention of the bot (``isSelf``) sets ``mentioned`` and is cut out of the text;
images, video, audio and files become ``line:<message id>`` attachments the media intake fetches
through `LineAdapter.fetch_media`; a postback carries its data as ``metadata["button"]``.

Outbound: each event leaves a single-use reply token for its chat. A send younger than 50 s after
it spends the token (free); otherwise, or when LINE refuses the token, it pushes (metered). Text
is cut into bubbles of at most 4,500 UTF-16 units and sent five per call; a call is all or
nothing, so a failure in a later call reports the earlier bubbles as delivered with the rest as
the remainder. A read timeout is never followed by a push of the same bubbles (R06.4); a long
server wait is handed back (inline wait cap 5 s, R06.3). Approval and clarify prompts are
template button cards (≤4 postback actions).

Slow turns: when the runner shows typing and the turn is still running 45 s (configurable, 0 =
off) after the user's message, the still-valid reply token is spent on a "still working" card
with a postback button. Final sends to that chat are then held until the user taps it (the tap's
fresh reply token delivers them for free) or, after ``slow_reply_hold_seconds`` without a tap,
pushed. Interim sends pass straight through.

Media: files we send are served to LINE from the same listener under unguessable tokens
(``/line/media?t=…``, valid 1,800 s) below the public HTTPS origin of ``webhook_public_url``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import mimetypes
import re
import secrets
import struct
import zlib
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Final

import httpx

from indus.foundation.blocking import run_blocking
from indus.foundation.clock import Clock
from indus.gateway.adapters._http import (
    BadPayload,
    CallbackListener,
    CallbackRequest,
    ListenerSettings,
    Reply,
    SeenIds,
    failure_from_exception,
    failure_from_status,
    verify_hmac_sha256_base64,
)
from indus.gateway.adapters.base import (
    FINAL_KEY,
    Adapter,
    AdapterConnectError,
    ApprovalChoice,
    ApprovalPrompt,
    Capabilities,
    ChatInfo,
    ClarifyPrompt,
    FetcherSeat,
    MediaKind,
    approval_callback,
    clarify_callback,
    utf16_units,
)
from indus.gateway.adapters.line.api import LineApi, LineError
from indus.gateway.core.events import Attachment, ChatType, InboundEvent, MessageKind, Source
from indus.gateway.core.send import SendResult
from indus.gateway.outbound.chunker import split_message

if TYPE_CHECKING:
    from indus.gateway.config import PlatformSettings

__all__ = [
    "API_TARGET",
    "BUBBLE_UNITS",
    "DEFAULT_PORT",
    "MAX_BODY_BYTES",
    "MEDIA_PATH",
    "MEDIA_TTL_S",
    "PER_CALL",
    "REPLY_TOKEN_TTL_S",
    "WEBHOOK_PATH",
    "LineAdapter",
    "LineOptions",
    "parse_ref",
]

log = logging.getLogger(__name__)

API_TARGET: Final = "indus.gateway.adapters.line.api:LineApi"

DEFAULT_PORT: Final = 8646
WEBHOOK_PATH: Final = "/line/webhook"
MEDIA_PATH: Final = "/line/media"
MAX_BODY_BYTES: Final = 1024 * 1024
# A text bubble holds 5,000 characters; we cut at 4,500 UTF-16 units to stay clear of it.
BUBBLE_UNITS: Final = 4500
PER_CALL: Final = 5
# One send carries this much (a reply this size fits one call, so one free reply token).
MAX_PER_SEND: Final = 20_000
# LINE keeps a reply token for about a minute; we stop using it earlier.
REPLY_TOKEN_TTL_S: Final = 50.0
MEDIA_TTL_S: Final = 1800.0
SLOW_REPLY_S: Final = 45.0
SLOW_HOLD_S: Final = 600.0
DEDUPE_TTL_S: Final = 24 * 3600.0
DEDUPE_MAX: Final = 5000
LOADING_SECONDS: Final = 20
IMAGE_MAX_BYTES: Final = 10 * 1024 * 1024
# LINE takes 200 MB of audio or video; the listener buffers what it serves, so we cap lower.
AV_MAX_BYTES: Final = 50 * 1024 * 1024
DEFAULT_AUDIO_MS: Final = 60_000
TEMPLATE_TEXT_MAX: Final = 160
ALT_TEXT_MAX: Final = 400
LABEL_MAX: Final = 20
MAX_ACTIONS: Final = 4
REF_PREFIX: Final = "line:"
ANSWER_PREFIX: Final = "indus-line:answer:"
_MEMORY: Final = 500

STILL_WORKING_TEXT: Final = "Still working on your answer. Tap the button when you want to see it."
SHOW_ANSWER_LABEL: Final = "Show answer"
NOT_READY_TEXT: Final = "Not ready yet. Tap the button again in a little while."
GONE_TEXT: Final = "That answer is no longer waiting here."
CARD_PROMPT: Final = "Choose one:"

_SHORT_LABELS: Final[Mapping[ApprovalChoice, str]] = {
    "once": "Allow once",
    "session": "Allow this session",
    "always": "Always allow",
    "deny": "Deny",
}
_MEDIA_KINDS: Final[Mapping[str, MessageKind]] = {
    "image": MessageKind.PHOTO,
    "video": MessageKind.VIDEO,
    "audio": MessageKind.VOICE,
    "file": MessageKind.DOCUMENT,
}
_MEDIA_MIME: Final[Mapping[str, str]] = {
    "image": "image/jpeg",
    "video": "video/mp4",
    "audio": "audio/mp4",
}
# What LINE accepts for each outbound kind: suffix → content type.
_OUTBOUND: Final[Mapping[str, Mapping[str, str]]] = {
    "image": {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"},
    "voice": {".m4a": "audio/mp4", ".mp3": "audio/mpeg"},
    "video": {".mp4": "video/mp4"},
}


def _tiny_png() -> bytes:
    """A 1×1 grey PNG: the preview LINE requires for a video when we have none."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    pixels = zlib.compress(b"\x00\x60\x60\x60")
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", pixels) + chunk(b"IEND", b"")
    )


PREVIEW_PNG: Final = _tiny_png()


def parse_ref(ref: str) -> str | None:
    """The message id of a ``line:<id>`` attachment ref, else None."""
    if not ref.startswith(REF_PREFIX):
        return None
    ident = ref[len(REF_PREFIX) :]
    return ident if ident.isascii() and ident.isdigit() else None


def _clamp(text: str, limit: int) -> str:
    """``text`` cut to ``limit`` UTF-16 units, an ellipsis marking the cut."""
    if utf16_units(text) <= limit:
        return text
    out = ""
    for char in text:
        if utf16_units(out + char) > limit - 1:
            break
        out += char
    return out.rstrip() + "…"


def _cut_utf16(text: str, spans: Sequence[tuple[int, int]]) -> str:
    """``text`` without the spans given in UTF-16 units (LINE's mention offsets)."""
    data = text.encode("utf-16-le", "surrogatepass")
    keep = bytearray()
    pos = 0
    for start, length in sorted(spans):
        lo, hi = max(start * 2, pos), max(start * 2, (start + length) * 2)
        keep += data[pos:lo]
        pos = max(pos, hi)
    keep += data[pos:]
    return bytes(keep).decode("utf-16-le", "ignore")


def _text_object(text: str, quote_token: str | None = None) -> dict[str, Any]:
    obj: dict[str, Any] = {"type": "text", "text": text}
    if quote_token:
        obj["quoteToken"] = quote_token
    return obj


def _card(text: str, actions: Sequence[tuple[str, str]], alt: str) -> dict[str, Any]:
    """A template buttons card; ``actions`` are (label, postback data)."""
    return {
        "type": "template",
        "altText": _clamp(alt or text, ALT_TEXT_MAX),
        "template": {
            "type": "buttons",
            "text": _clamp(text, TEMPLATE_TEXT_MAX),
            "actions": [
                {"type": "postback", "label": label, "data": data, "displayText": label}
                for label, data in actions
            ],
        },
    }


def _label(text: str, short: str | None = None) -> str:
    if utf16_units(text) <= LABEL_MAX:
        return text
    if short is not None and utf16_units(short) <= LABEL_MAX:
        return short
    return _clamp(text, LABEL_MAX)


def _chat_of(source: Mapping[str, Any]) -> tuple[str, ChatType]:
    kind = source.get("type")
    if kind == "user":
        return str(source.get("userId") or ""), "dm"
    if kind == "group":
        return str(source.get("groupId") or ""), "group"
    if kind == "room":
        return str(source.get("roomId") or ""), "group"
    return "", "dm"


def _number(extra: Mapping[str, Any], key: str, default: float) -> float:
    raw = extra.get(key)
    if raw is None or raw == "" or isinstance(raw, bool):
        return default
    try:
        value = float(str(raw).strip())
    except ValueError:
        raise ValueError(f"platforms.line.{key} must be a number of seconds") from None
    if value < 0 or value != value:
        raise ValueError(f"platforms.line.{key} must not be negative")
    return value


@dataclass(frozen=True)
class LineOptions:
    # Seconds after the user's message before the "still working" card (0 = never).
    slow_reply_s: float = SLOW_REPLY_S
    # How long a held answer waits for a tap before it is pushed.
    hold_s: float = SLOW_HOLD_S
    # Public HTTPS origin (plus any path prefix) our media URLs start with; None = no media.
    media_base: str | None = None

    @classmethod
    def from_extra(
        cls, extra: Mapping[str, Any], *, public_url: str | None, webhook_path: str
    ) -> LineOptions:
        hold = _number(extra, "slow_reply_hold_seconds", SLOW_HOLD_S)
        return cls(
            slow_reply_s=_number(extra, "slow_reply_seconds", SLOW_REPLY_S),
            hold_s=hold if hold > 0 else SLOW_HOLD_S,
            media_base=media_base(public_url, webhook_path),
        )


def media_base(public_url: str | None, webhook_path: str) -> str | None:
    """The base our media URLs hang under: the public webhook URL without its webhook path.
    Only an https URL qualifies (LINE fetches media over HTTPS only)."""
    url = (public_url or "").strip().split("?", 1)[0].rstrip("/")
    if not url.lower().startswith("https://"):
        return None
    path = webhook_path.rstrip("/")
    if path and url.endswith(path):
        url = url[: -len(path)]
    return url.rstrip("/")


@dataclass
class _Token:
    token: str
    at: float


@dataclass
class _Served:
    expires: float
    mime: str
    path: Path | None = None
    data: bytes | None = None
    cap: int = AV_MAX_BYTES


@dataclass
class _Held:
    """A slow turn's answer waiting behind the "still working" button."""

    request_id: str
    chat_id: str
    pieces: list[str] = field(default_factory=list[str])
    timer: asyncio.Task[None] | None = None

    @property
    def ready(self) -> bool:
        return bool(self.pieces)


class _CallFailed(Exception):
    def __init__(self, result: SendResult) -> None:
        super().__init__(result.error or "LINE call failed")
        self.result = result


def _read_capped(path: Path, cap: int) -> bytes | None:
    try:
        if not path.is_file() or path.stat().st_size > cap:
            return None
        return path.read_bytes()
    except OSError:
        return None


def _stat_size(path: Path) -> int | None:
    try:
        return path.stat().st_size if path.is_file() else None
    except OSError:
        return None


class LineAdapter(Adapter):
    """LINE Messaging API (see the module notes)."""

    platform: ClassVar[str] = "line"
    capabilities: ClassVar[Capabilities] = Capabilities(
        max_len=MAX_PER_SEND,
        len_fn=utf16_units,
        markup="plain",
        splits_natively=True,
        supports_buttons=True,
        inline_wait_cap=5.0,
    )
    vendor_names: ClassVar[Mapping[str, tuple[str, ...]]] = {
        API_TARGET: ("bot_info", "reply", "push", "start_loading", "content", "aclose"),
    }

    def __init__(
        self,
        api: LineApi,
        *,
        channel_secret: str,
        listener: ListenerSettings,
        clock: Clock,
        settings: PlatformSettings | None = None,
        secrets: Sequence[str] = (),
        options: LineOptions | None = None,
        send_only: bool = False,
        serve: bool = True,
    ) -> None:
        super().__init__(clock=clock, settings=settings, secrets=(*secrets, channel_secret))
        self.api = api
        self._channel_secret = channel_secret
        self.options = options or LineOptions()
        self.send_only = send_only
        self.listener = CallbackListener(
            listener, name=self.platform, clock=clock, scrub=self.scrub, serve=serve
        )
        self.listener.route(listener.path, self._callback, methods=("POST",))
        self.listener.route(MEDIA_PATH, self._serve_media, methods=("GET",))
        self.bot_user_id: str | None = None
        self._seen = SeenIds(clock, DEDUPE_TTL_S, DEDUPE_MAX)
        self._tokens: dict[str, _Token] = {}
        self._quotes: OrderedDict[str, str] = OrderedDict()
        self._sent: OrderedDict[str, None] = OrderedDict()
        self._served: dict[str, _Served] = {}
        self._slow: dict[str, asyncio.Task[None]] = {}
        self._held: dict[str, _Held] = {}

    # -- lifecycle ------------------------------------------------------------------------------

    async def _open(self, is_reconnect: bool) -> None:
        try:
            info = await self.api.bot_info()
        except LineError as exc:
            if exc.status in (401, 403):
                raise AdapterConnectError(
                    "LINE refused the channel access token", retryable=False, code="line_auth"
                ) from None
            raise AdapterConnectError(f"LINE answered {exc.status} to the bot info call") from None
        except httpx.HTTPError as exc:
            raise AdapterConnectError(f"cannot reach LINE: {type(exc).__name__}") from None
        self.bot_user_id = str(info.get("userId") or "") or None
        if not self.send_only:
            await self.listener.start()

    async def _close(self) -> None:
        await self.listener.stop()
        tasks = [*self._slow.values(), *(h.timer for h in self._held.values() if h.timer)]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._slow.clear()
        self._held.clear()
        self._served.clear()
        self._tokens.clear()
        await self.api.aclose()

    # -- inbound --------------------------------------------------------------------------------

    async def _callback(self, request: CallbackRequest) -> Reply:
        offered = request.header("x-line-signature")
        if not verify_hmac_sha256_base64(self._channel_secret, request.body, offered):
            log.info("line: refused a webhook call with a bad signature from %s", request.client)
            return Reply.json({"error": "bad signature"}, 401)
        try:
            data = request.json()
        except BadPayload:
            return Reply.json({"error": "bad payload"}, 400)
        events = data.get("events") if isinstance(data, dict) else None
        if not isinstance(events, list):
            return Reply.json({"error": "bad payload"}, 400)
        batch = [e for e in events if isinstance(e, dict)]
        if batch:
            self.listener.background(self._handle_events(batch))
        return Reply.json({"status": "ok"})

    async def _handle_events(self, events: list[dict[str, Any]]) -> None:
        for event in events:
            try:
                await self._handle_event(event)
            except Exception as exc:
                log.warning("line: an event could not be handled: %s", self.scrub(repr(exc)))

    async def _handle_event(self, event: dict[str, Any]) -> None:
        if event.get("mode") == "standby":
            return
        event_id = str(event.get("webhookEventId") or "")
        if event_id and not self._seen.first_time(event_id):
            return
        source = event.get("source")
        source = source if isinstance(source, dict) else {}
        chat_id, chat_type = _chat_of(source)
        if not chat_id:
            return
        user_id = str(source.get("userId") or "") or None
        if user_id is not None and user_id == self.bot_user_id:
            return
        token = event.get("replyToken")
        if isinstance(token, str) and token:
            self._tokens[chat_id] = _Token(token, self.clock.monotonic())
        kind = event.get("type")
        src = Source(
            platform=self.platform,
            chat_id=chat_id,
            chat_type=chat_type,
            user_id=user_id if user_id is not None else (chat_id if chat_type == "dm" else None),
        )
        stamp = self._stamp(event.get("timestamp"))
        if kind == "message":
            message = event.get("message")
            if isinstance(message, dict):
                inbound = self._message(src, message, stamp)
                if inbound is not None:
                    await self.emit(inbound)
        elif kind == "postback":
            postback = event.get("postback")
            data = str(postback.get("data") or "") if isinstance(postback, dict) else ""
            if data.startswith(ANSWER_PREFIX):
                await self._tapped(chat_id, data[len(ANSWER_PREFIX) :])
                return
            if data:
                await self.emit(
                    InboundEvent(
                        source=src,
                        message_id=event_id or None,
                        metadata={"button": data},
                        timestamp=stamp,
                    )
                )
        else:
            log.debug("line: ignored a %s event", kind)

    def _stamp(self, millis: object) -> datetime | None:
        if isinstance(millis, int | float) and not isinstance(millis, bool) and millis > 0:
            with contextlib.suppress(OverflowError, OSError, ValueError):
                return datetime.fromtimestamp(millis / 1000, tz=UTC)
        return None

    def _message(
        self, src: Source, message: dict[str, Any], stamp: datetime | None
    ) -> InboundEvent | None:
        kind = str(message.get("type") or "")
        message_id = str(message.get("id") or "") or None
        metadata: dict[str, Any] = {}
        media: tuple[Attachment, ...] = ()
        text = ""
        event_kind = MessageKind.TEXT
        if kind == "text":
            text = str(message.get("text") or "")
            spans = self._own_mentions(message)
            if spans:
                metadata["mentioned"] = True
                text = re.sub(r"[ \t]{2,}", " ", _cut_utf16(text, spans)).strip()
        elif kind in _MEDIA_KINDS:
            if message_id is None:
                return None
            event_kind = _MEDIA_KINDS[kind]
            name = str(message.get("fileName") or "")
            mime = (
                _MEDIA_MIME.get(kind) or mimetypes.guess_type(name)[0] or "application/octet-stream"
            )
            media = (Attachment(ref=f"{REF_PREFIX}{message_id}", mime=mime),)
            if name:
                metadata["file_name"] = name
        elif kind == "sticker":
            event_kind = MessageKind.STICKER
            words = [str(w) for w in message.get("keywords") or () if w]
            text = f"(sticker: {', '.join(words[:5])})" if words else "(sticker)"
        elif kind == "location":
            event_kind = MessageKind.LOCATION
            place = " ".join(str(message.get(k) or "") for k in ("title", "address")).strip()
            where = f"({message.get('latitude')}, {message.get('longitude')})"
            text = f"{place} {where}".strip()
        else:
            log.debug("line: ignored a %s message", kind)
            return None
        quote = message.get("quoteToken")
        if message_id and isinstance(quote, str) and quote:
            self._remember(self._quotes, message_id, quote)
        quoted = str(message.get("quotedMessageId") or "") or None
        return InboundEvent(
            source=src,
            text=text,
            kind=event_kind,
            message_id=message_id,
            media=media,
            reply_to_id=quoted,
            reply_to_is_own=quoted is not None and quoted in self._sent,
            metadata=metadata,
            timestamp=stamp,
        )

    def _own_mentions(self, message: Mapping[str, Any]) -> list[tuple[int, int]]:
        mention = message.get("mention")
        found: list[tuple[int, int]] = []
        mentionees = mention.get("mentionees") if isinstance(mention, dict) else None
        for item in mentionees if isinstance(mentionees, list) else ():
            if not isinstance(item, dict):
                continue
            mine = item.get("isSelf") is True or (
                self.bot_user_id is not None and item.get("userId") == self.bot_user_id
            )
            index, length = item.get("index"), item.get("length")
            if mine and isinstance(index, int) and isinstance(length, int):
                found.append((index, length))
        return found

    @staticmethod
    def _remember(book: OrderedDict[str, Any], key: str, value: Any) -> None:
        book.pop(key, None)
        book[key] = value
        while len(book) > _MEMORY:
            book.popitem(last=False)

    def register_fetchers(self, seat: FetcherSeat) -> Callable[[], None] | None:
        """Register ``fetch_media`` for ``line:`` refs with the media intake (P15-23)."""
        return seat.register_fetcher(self.platform, self.fetch_media)

    async def fetch_media(self, attachment: Attachment) -> AsyncIterator[bytes]:
        message_id = parse_ref(attachment.ref)
        if message_id is None:
            raise ValueError("not a LINE content reference")
        async for chunk in self.api.content(message_id):
            yield chunk

    # -- outbound: text -------------------------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        text: str,
        *,
        reply_to: str | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> SendResult:
        """As the base, except that a final send to a chat whose slow turn shows the "still
        working" button is held for the tap."""
        held = self._held_for(chat_id)
        if held is not None and text.strip() and meta is not None and meta.get(FINAL_KEY) is True:
            held.pieces.append(text)
            if held.timer is None:
                held.timer = asyncio.ensure_future(self._release_later(held))
            return SendResult.sent(f"held-{held.request_id}-{len(held.pieces)}")
        return await super().send(chat_id, text, reply_to=reply_to, meta=meta)

    def _bubbles(self, text: str) -> list[tuple[str, int]]:
        """``(bubble, source offset)`` pieces of at most ``BUBBLE_UNITS`` UTF-16 units."""
        chunked = split_message(text, BUBBLE_UNITS, len_fn=utf16_units)
        out: list[tuple[str, int]] = []
        for chunk in chunked.chunks:
            piece = text[chunk.start : chunk.end].strip()
            if piece:
                out.append((piece, chunk.start))
        return out

    async def _send(
        self, chat_id: str, text: str, reply_to: str | None, meta: dict[str, Any]
    ) -> SendResult:
        bubbles = self._bubbles(text)
        if not bubbles:
            return SendResult(ok=False, error="nothing to send")
        quote = self._quotes.get(reply_to) if reply_to else None
        objects = [_text_object(b, quote if n == 0 else None) for n, (b, _) in enumerate(bubbles)]
        starts = [start for _, start in bubbles]

        def remainder(index: int) -> str | None:
            return text[starts[index] :] if index < len(starts) else None

        return await self._deliver(chat_id, objects, remainder)

    def _take_token(self, chat_id: str) -> str | None:
        """The chat's reply token if it is fresh; it is spent either way (single use)."""
        held = self._tokens.pop(chat_id, None)
        if held is None or self.clock.monotonic() - held.at >= REPLY_TOKEN_TTL_S:
            return None
        return held.token

    async def _deliver(
        self,
        chat_id: str,
        objects: Sequence[dict[str, Any]],
        remainder: Callable[[int], str | None] = lambda _n: None,
    ) -> SendResult:
        """Send message objects five per call, the first call on the reply token when fresh."""
        self._cancel_slow(chat_id)
        token = self._take_token(chat_id)
        delivered: list[str] = []
        for start in range(0, len(objects), PER_CALL):
            if start:
                await self.slots.pace(chat_id)
            batch = objects[start : start + PER_CALL]
            try:
                ids = await self._call(chat_id, batch, token if start == 0 else None)
            except _CallFailed as failed:
                return self._partial(failed.result, delivered, remainder(start))
            delivered.extend(ids)
        for message_id in delivered:
            self._remember(self._sent, message_id, None)
        return SendResult.sent(delivered[-1], tuple(delivered[:-1]))

    async def _call(
        self, chat_id: str, batch: Sequence[dict[str, Any]], token: str | None
    ) -> tuple[str, ...]:
        if token is not None:
            try:
                return (await self.api.reply(token, batch)).ids
            except LineError as exc:
                if not (exc.status == 400 and "reply token" in exc.detail.lower()):
                    raise _CallFailed(self._status_failure(exc)) from None
                log.info("line: the reply token was refused; pushing instead")
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
                log.info("line: the reply never reached LINE; pushing instead")
            except httpx.HTTPError as exc:
                raise _CallFailed(failure_from_exception(exc)) from None
        try:
            return (await self.api.push(chat_id, batch)).ids
        except LineError as exc:
            raise _CallFailed(self._status_failure(exc)) from None
        except httpx.HTTPError as exc:
            raise _CallFailed(failure_from_exception(exc)) from None

    def _status_failure(self, exc: LineError) -> SendResult:
        detail = exc.detail
        if exc.status == 401:
            # A revoked or reissued channel token: no retry or reconnect brings it back.
            self.report_fatal(
                "line_auth",
                "LINE refused the channel access token; update LINE_CHANNEL_ACCESS_TOKEN",
            )
        if exc.status == 429 and "monthly limit" in detail.lower():
            return SendResult.failure(
                "HTTP 429: the LINE channel used up its monthly message quota"
            )
        reading = "chat not found" if exc.status == 404 else None
        return failure_from_status(
            exc.status, detail, exc.headers, now=self.clock.now(), reading=reading
        )

    @staticmethod
    def _partial(result: SendResult, delivered: list[str], rest: str | None) -> SendResult:
        if not delivered:
            return result
        return replace(
            result,
            message_id=delivered[-1],
            continuation_ids=tuple(delivered[:-1]),
            partial=True,
            remainder=None if result.maybe_delivered else rest,
        )

    # -- outbound: prompts ----------------------------------------------------------------------

    async def _send_card(
        self, chat_id: str, text: str, actions: list[tuple[str, str]], *, spell_out: bool
    ) -> SendResult:
        objects: list[dict[str, Any]] = []
        card_text = text
        if spell_out or utf16_units(text) > TEMPLATE_TEXT_MAX:
            objects.extend(_text_object(b) for b, _ in self._bubbles(text))
            card_text = CARD_PROMPT
        objects.append(_card(card_text, actions, alt=text))
        return await self._deliver(chat_id, objects)

    async def _send_approval(
        self, chat_id: str, prompt: ApprovalPrompt, meta: dict[str, Any]
    ) -> SendResult:
        if len(prompt.actions) > MAX_ACTIONS:
            return await self._send(chat_id, prompt.fallback_text, None, meta)
        actions = [
            (
                _label(a.label, _SHORT_LABELS.get(a.choice)),
                approval_callback(prompt.request_id, a.choice),
            )
            for a in prompt.actions
        ]
        return await self._send_card(chat_id, prompt.text, actions, spell_out=False)

    async def _send_clarify(
        self, chat_id: str, prompt: ClarifyPrompt, meta: dict[str, Any]
    ) -> SendResult:
        if len(prompt.choices) > MAX_ACTIONS:
            return await self._send(chat_id, prompt.fallback_text or prompt.question, None, meta)
        actions = [
            (_label(choice), clarify_callback(prompt.clarify_id, n))
            for n, choice in enumerate(prompt.choices)
        ]
        cut = any(
            label != choice for (label, _), choice in zip(actions, prompt.choices, strict=True)
        )
        text = prompt.fallback_text if cut and prompt.fallback_text else prompt.question
        return await self._send_card(chat_id, text, actions, spell_out=cut)

    # -- outbound: typing and the slow-turn card ------------------------------------------------

    async def _typing(self, chat_id: str, meta: dict[str, Any]) -> None:
        self._arm_slow(chat_id)
        if chat_id.startswith("U"):  # LINE shows the loading animation in user chats only
            await self.api.start_loading(chat_id, LOADING_SECONDS)

    def _held_for(self, chat_id: str) -> _Held | None:
        for held in self._held.values():
            if held.chat_id == chat_id:
                return held
        return None

    def _arm_slow(self, chat_id: str) -> None:
        if self.options.slow_reply_s <= 0 or chat_id in self._slow:
            return
        if self._held_for(chat_id) is not None:
            return
        token = self._tokens.get(chat_id)
        if token is None:
            return
        age = self.clock.monotonic() - token.at
        if age >= REPLY_TOKEN_TTL_S:
            return
        wait = max(self.options.slow_reply_s - age, 0.0)
        task = asyncio.ensure_future(self._offer_button(chat_id, token, wait))
        self._slow[chat_id] = task

        def forget(done: asyncio.Task[None]) -> None:
            if self._slow.get(chat_id) is done:
                del self._slow[chat_id]

        task.add_done_callback(forget)

    def _cancel_slow(self, chat_id: str) -> None:
        task = self._slow.pop(chat_id, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def _offer_button(self, chat_id: str, token: _Token, wait: float) -> None:
        await self.clock.sleep(wait)
        if self._tokens.get(chat_id) is not token:
            return  # the turn answered (or a newer message came) in time
        if self.clock.monotonic() - token.at >= REPLY_TOKEN_TTL_S:
            return
        del self._tokens[chat_id]
        held = _Held(secrets.token_hex(8), chat_id)
        card = _card(
            STILL_WORKING_TEXT,
            [(SHOW_ANSWER_LABEL, f"{ANSWER_PREFIX}{held.request_id}")],
            alt=STILL_WORKING_TEXT,
        )
        self._held[held.request_id] = held
        try:
            async with self.slots.hold(chat_id):
                await self.api.reply(token.token, [card])
        except (LineError, httpx.HTTPError) as exc:
            self._held.pop(held.request_id, None)
            log.info("line: the still-working card failed: %s", self.scrub(type(exc).__name__))

    async def _tapped(self, chat_id: str, request_id: str) -> None:
        held = self._held.get(request_id)
        if held is not None and held.chat_id == chat_id and held.ready:
            await self._release(held)
            return
        notice = GONE_TEXT if held is None or held.chat_id != chat_id else NOT_READY_TEXT
        async with self.slots.hold(chat_id):
            await self._deliver(chat_id, [_text_object(notice)])

    async def _release_later(self, held: _Held) -> None:
        await self.clock.sleep(self.options.hold_s)
        if self._held.get(held.request_id) is held:
            held.timer = None
            await self._release(held)

    async def _release(self, held: _Held) -> None:
        self._held.pop(held.request_id, None)
        timer, held.timer = held.timer, None
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()
        objects = [_text_object(b) for piece in held.pieces for b, _ in self._bubbles(piece)]
        if not objects:
            return
        async with self.slots.hold(held.chat_id):
            result = await self._deliver(held.chat_id, objects)
        if not result.ok:
            log.warning("line: a held answer could not be delivered: %s", result.error)

    # -- outbound: media ------------------------------------------------------------------------

    async def _send_media(
        self, kind: MediaKind, chat_id: str, path: str, caption: str | None, meta: dict[str, Any]
    ) -> SendResult:
        accepted = _OUTBOUND.get(kind)
        if accepted is None:
            return SendResult.refused(f"sending {kind}s")
        if self.send_only:
            return SendResult.refused(f"sending {kind}s without the gateway's listener running")
        base = self.options.media_base
        if base is None:
            return SendResult.failure(
                "LINE fetches media from a public https URL: set platforms.line.webhook_public_url"
            )
        file = Path(path)
        mime = accepted.get(file.suffix.lower())
        if mime is None:
            kinds = ", ".join(sorted(accepted))
            return SendResult.failure(f"LINE takes {kind} files of type {kinds} only")
        cap = IMAGE_MAX_BYTES if kind == "image" else AV_MAX_BYTES
        size = await run_blocking(_stat_size, file, label="line media")
        if size is None or size > cap:
            return SendResult.failure(f"the {kind} is missing or larger than {cap} bytes")
        url = self._serve(base, _Served(0.0, mime, path=file, cap=cap), file.name)
        obj: dict[str, Any]
        if kind == "image":
            obj = {"type": "image", "originalContentUrl": url, "previewImageUrl": url}
        elif kind == "voice":
            millis = meta.get("duration_ms")
            duration = (
                int(millis) if isinstance(millis, int | float) and millis > 0 else DEFAULT_AUDIO_MS
            )
            obj = {"type": "audio", "originalContentUrl": url, "duration": duration}
        else:
            preview = self._serve(base, _Served(0.0, "image/png", data=PREVIEW_PNG), "preview.png")
            obj = {"type": "video", "originalContentUrl": url, "previewImageUrl": preview}
        objects = [obj]
        if caption and caption.strip():
            objects.extend(_text_object(b) for b, _ in self._bubbles(caption))
        return await self._deliver(chat_id, objects)

    def _serve(self, base: str, entry: _Served, name: str) -> str:
        now = self.clock.monotonic()
        for token in [t for t, e in self._served.items() if e.expires <= now]:
            del self._served[token]
        token = secrets.token_urlsafe(32)
        entry.expires = now + MEDIA_TTL_S
        self._served[token] = entry
        query = httpx.QueryParams({"t": token, "n": name})
        return f"{base}{MEDIA_PATH}?{query}"

    async def _serve_media(self, request: CallbackRequest) -> Reply:
        token = request.query_params().get("t", "")
        entry = self._served.get(token)
        if entry is None:
            return Reply.json({"error": "not found"}, 404)
        if self.clock.monotonic() >= entry.expires:
            del self._served[token]
            return Reply.json({"error": "expired"}, 410)
        data = entry.data
        if data is None and entry.path is not None:
            data = await run_blocking(_read_capped, entry.path, entry.cap, label="line media")
        if data is None:
            return Reply.json({"error": "not found"}, 404)
        return Reply(200, data, entry.mime)

    # -- descriptive ----------------------------------------------------------------------------

    async def _chat_info(self, chat_id: str) -> ChatInfo:
        kinds: dict[str, ChatType] = {"U": "dm", "C": "group", "R": "group"}
        return ChatInfo(name=chat_id, type=kinds.get(chat_id[:1]))
