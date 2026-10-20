"""The QQ Bot adapter (P16-20): QQ Bot API v2, the gateway WebSocket in, REST out.

Credentials: ``QQ_APP_ID`` and ``QQ_CLIENT_SECRET`` (secret store first, then the environment)
buy the access token (``client.QQTokens``). Connecting fetches a token and the gateway URL (proving
both); the WebSocket (``gateway.QQGateway``) starts once an inbound sink is installed and stops
when it is removed; a send-only adapter (``indus send``) never opens it.

Chats, by id (C2C bare, the others prefixed so a reply or a cron target always finds its endpoint):

=================  =====================  ==========================  =========================
QQ chat            chat id                inbound event               outbound path
=================  =====================  ==========================  =========================
C2C (private)      ``<user_openid>``      ``C2C_MESSAGE_CREATE``      ``/v2/users/<id>/messages``
group              ``group:<openid>``     ``GROUP_AT_MESSAGE_CREATE`` ``/v2/groups/<id>/messages``
guild channel      ``channel:<id>``       ``AT_MESSAGE_CREATE``,      ``/channels/<id>/messages``
                                          ``GUILD_MESSAGE_CREATE``
guild DM           ``dm:<guild_id>``      ``DIRECT_MESSAGE_CREATE``   ``/dms/<guild>/messages``
=================  =====================  ==========================  =========================

Inbound: repeats (message id, 1,000 for 300 s) dropped; a leading ``@name`` / ``<@!id>`` cut from
group and channel text (groups only deliver @-mentions: ``mentioned``); attachments become
`Attachment` refs to QQ's CDN (protocol-relative URLs get ``https:``; voice prefers the WAV copy
and QQ's own transcript goes to ``metadata["transcript"]``, and becomes the text when there is
none); button presses (``INTERACTION_CREATE``) are acknowledged and become button events.

Outbound: QQ answers a message "passively" when the send names it (``msg_id``) with a fresh
``msg_seq``. The adapter remembers each chat's last inbound message for 5 minutes and sends every
piece of a reply with it and a growing ``msg_seq``; later sends go out "active" (rate-limited by
QQ). Text is cut at 4,000 characters; plain text unless ``platforms.qqbot.markdown`` (needs QQ's
permission). No edits. Typing (``input_notify``) only in C2C, once per 50 s, while the last inbound
message is fresh. Approvals and clarify choices are inline keyboards in C2C and groups (text in
guild chats). Files go through the chunked upload (``upload.py``), C2C and groups only.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import mimetypes
import re
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal, cast

import httpx

from indus.foundation.clock import Clock
from indus.gateway.adapters._http import SeenIds, failure_from_exception, failure_from_status
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
from indus.gateway.adapters.qqbot.client import (
    API_BASE,
    SANDBOX_BASE,
    QQApiError,
    QQClient,
    QQTokens,
    SocketOpener,
    TokenError,
    httpx_ws_opener,
    safe_id,
)
from indus.gateway.adapters.qqbot.gateway import QQGateway
from indus.gateway.adapters.qqbot.upload import ChunkedUploader, DailyLimitReached, UploadFailed
from indus.gateway.core.events import Attachment, ChatType, InboundEvent, MessageKind, Source
from indus.gateway.core.send import SendResult, is_ambiguous_timeout
from indus.gateway.outbound.chunker import split_message

if TYPE_CHECKING:
    from indus.gateway.config import PlatformSettings

__all__ = [
    "APP_ID_ENV",
    "MAX_TEXT",
    "SECRET_ENV",
    "QQBotAdapter",
    "QQBotMarkdownAdapter",
    "QQBotOptions",
    "Target",
    "chat_id_of",
    "parse_target",
]

log = logging.getLogger(__name__)

APP_ID_ENV = "QQ_APP_ID"
SECRET_ENV = "QQ_CLIENT_SECRET"
MAX_TEXT = 4000
SEEN_TTL_S = 300.0
SEEN_MAX = 1000
PASSIVE_WINDOW_S = 300.0
TYPING_EVERY_S = 50.0
TYPING_SECONDS = 60
DEFAULT_MAX_MEDIA_BYTES = 100 * 1024 * 1024
KEYBOARD_ROW = 5
KEYBOARD_ROWS = 5
LABEL_MAX = 20

MSG_TEXT = 0
MSG_MARKDOWN = 2
MSG_TYPING = 6
MSG_MEDIA = 7
_FILE_TYPES: Mapping[str, int] = {"image": 1, "video": 2, "voice": 3, "file": 4, "document": 4}
_LEADING_MENTION = re.compile(r"^\s*(?:<@!?\d+>|@\S+)\s*")

type Kind = Literal["c2c", "group", "channel", "dm"]
_PREFIXES: Mapping[str, Kind] = {
    "group:": "group",
    "channel:": "channel",
    "dm:": "dm",
    "c2c:": "c2c",
}
_CHAT_TYPES: Mapping[Kind, ChatType] = {
    "c2c": "dm",
    "dm": "dm",
    "group": "group",
    "channel": "channel",
}


@dataclass(frozen=True)
class Target:
    kind: Kind
    id: str


def parse_target(chat_id: str) -> Target:
    """A chat id → endpoint kind and QQ id (``ValueError`` for an id QQ never issues)."""
    text = chat_id.strip()
    for prefix, kind in _PREFIXES.items():
        if text.startswith(prefix):
            return Target(kind, safe_id(text[len(prefix) :]))
    return Target("c2c", safe_id(text))


def chat_id_of(kind: Kind, qq_id: str) -> str:
    return qq_id if kind == "c2c" else f"{kind}:{qq_id}"


@dataclass(frozen=True)
class QQBotOptions:
    """``platforms.qqbot.*`` settings (all optional)."""

    markdown: bool = False
    sandbox: bool = False
    max_media_bytes: int = DEFAULT_MAX_MEDIA_BYTES

    @classmethod
    def read(cls, extra: Mapping[str, Any]) -> QQBotOptions:
        def flag(key: str) -> bool:
            value = extra.get(key)
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return value is True

        try:
            cap = int(extra.get("max_media_bytes") or DEFAULT_MAX_MEDIA_BYTES)
        except (TypeError, ValueError):
            cap = DEFAULT_MAX_MEDIA_BYTES
        return cls(markdown=flag("markdown"), sandbox=flag("sandbox"), max_media_bytes=max(1, cap))


@dataclass
class _Passive:
    """The last inbound message of a chat: what passive replies name."""

    msg_id: str
    at: float
    seq: itertools.count[int]


def _map(value: object) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _timestamp(value: object) -> datetime | None:
    if isinstance(value, str) and value.strip():
        text = value.strip()
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            pass
        value = int(text) if text.isdigit() else None
    if isinstance(value, int | float) and not isinstance(value, bool):
        seconds = value / 1000 if value > 10**11 else value
        return datetime.fromtimestamp(seconds, UTC)
    return None


def _attachment_url(raw: str) -> str:
    url = raw.strip()
    return f"https:{url}" if url.startswith("//") else url


def _attachments(data: Mapping[str, Any]) -> tuple[tuple[Attachment, ...], str]:
    """The message's attachments, and QQ's transcript of a voice note ('' when none)."""
    found: list[Attachment] = []
    transcript = ""
    raw = data.get("attachments")
    for item in cast(list[object], raw) if isinstance(raw, list) else ():
        fields = _map(item)
        kind = str(fields.get("content_type") or "").strip().lower()
        url = _attachment_url(str(fields.get("url") or ""))
        name = str(fields.get("filename") or "")
        voice = kind == "voice" or kind.startswith("audio/")
        if voice:
            wav = _attachment_url(str(fields.get("voice_wav_url") or ""))
            text = str(fields.get("asr_refer_text") or "").strip()
            transcript = transcript or text
            if wav:
                found.append(Attachment(ref=wav, mime="audio/wav"))
                continue
            if url:
                found.append(Attachment(ref=url, mime=kind if "/" in kind else "audio/silk"))
            continue
        if not url:
            continue
        if "/" not in kind:
            kind = mimetypes.guess_type(name)[0] or "application/octet-stream"
        found.append(Attachment(ref=url, mime=kind))
    return tuple(found), transcript


def _kind_of(media: Sequence[Attachment]) -> MessageKind:
    if not media:
        return MessageKind.TEXT
    mime = media[0].mime
    if mime.startswith("image/"):
        return MessageKind.PHOTO
    if mime.startswith("audio/"):
        return MessageKind.VOICE
    if mime.startswith("video/"):
        return MessageKind.VIDEO
    return MessageKind.DOCUMENT


class QQBotAdapter(Adapter):
    platform: ClassVar[str] = "qqbot"
    capabilities: ClassVar[Capabilities] = Capabilities(
        max_len=MAX_TEXT,
        markup="plain",
        splits_natively=True,
        supports_edit=False,
        supports_delete=False,
        supports_buttons=True,
        dm_is_private=True,
        push_capable=True,
    )
    vendor_names: ClassVar[Mapping[str, tuple[str, ...]]] = {
        # Our own REST client (no SDK); its routes are ``client.ROUTES``, served by the wire fake.
        "indus.gateway.adapters.qqbot.client:QQClient": (
            "gateway_url",
            "send_message",
            "files_call",
            "ack_interaction",
            "put_part",
            "download",
        ),
        "httpx_ws:AsyncWebSocketSession": ("receive_text", "send_text"),
    }

    def __init__(
        self,
        *,
        clock: Clock,
        app_id: str,
        secret: str,
        settings: PlatformSettings | None = None,
        options: QQBotOptions | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        proxy: str | None = None,
        opener: Callable[[httpx.AsyncClient], SocketOpener] | None = None,
        send_only: bool = False,
    ) -> None:
        super().__init__(clock=clock, settings=settings, secrets=(secret,))
        self.app_id = app_id.strip()
        self._secret = secret.strip()
        self.options = options or QQBotOptions()
        self.send_only = send_only
        self._transport = transport
        self._proxy = proxy
        self._opener_factory = opener or httpx_ws_opener
        self._http: httpx.AsyncClient | None = None
        self._tokens: QQTokens | None = None
        self.client: QQClient | None = None
        self.gateway: QQGateway | None = None
        self._listener: asyncio.Task[None] | None = None
        self._seen = SeenIds(clock, SEEN_TTL_S, SEEN_MAX)
        self._passive: dict[str, _Passive] = {}
        self._active_seq = itertools.count(1)
        self._typing_at: dict[str, float] = {}

    def __repr__(self) -> str:
        state = "connected" if self.connected else "disconnected"
        return f"<{type(self).__name__} app {self.app_id or '?'} {state}>"

    # -- lifecycle ---------------------------------------------------------------------------------

    async def _open(self, is_reconnect: bool) -> None:
        missing = [n for n, v in ((APP_ID_ENV, self.app_id), (SECRET_ENV, self._secret)) if not v]
        if missing:
            raise AdapterConnectError(
                f"QQ Bot is not configured: set {' and '.join(missing)}",
                retryable=False,
                code="config",
            )
        http = httpx.AsyncClient(
            transport=self._transport,
            proxy=None if self._transport is not None else self._proxy,
            timeout=httpx.Timeout(30.0),
            trust_env=False,
            follow_redirects=False,
        )
        tokens = QQTokens(self.app_id, self._secret, http, clock=self.clock)
        client = QQClient(http, tokens, base=SANDBOX_BASE if self.options.sandbox else API_BASE)
        try:
            await tokens.token()
            if not self.send_only:
                await client.gateway_url()
        except TokenError as exc:
            await http.aclose()
            raise AdapterConnectError(
                str(exc), retryable=exc.retryable, code="qqbot_auth"
            ) from None
        except QQApiError as exc:
            await http.aclose()
            raise AdapterConnectError(str(exc), retryable=exc.status != 401) from None
        except BaseException:
            await http.aclose()
            raise
        self._http, self._tokens, self.client = http, tokens, client
        log.info("qqbot: signed in as app %s", self.app_id)
        if self._sink is not None:
            self._start_listening()

    async def _close(self) -> None:
        self._stop_listening()
        listener, self._listener = self._listener, None
        if listener is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await listener
        http, self._http = self._http, None
        self.client = None
        self._tokens = None
        if http is not None:
            await http.aclose()

    def set_sink(self, sink: InboundSink | None) -> None:
        """Installing a sink opens the gateway socket (never when send-only); removing it
        closes it."""
        super().set_sink(sink)
        if sink is None:
            self._stop_listening()
        elif self.connected:
            self._start_listening()

    @property
    def listening(self) -> bool:
        return self._listener is not None and not self._listener.done()

    def _start_listening(self) -> None:
        client, http = self.client, self._http
        if self.listening or self.send_only or client is None or http is None:
            return
        opener = self._opener_factory(http)
        tokens = client.tokens

        async def open_socket() -> contextlib.AbstractAsyncContextManager[Any]:
            return opener(await client.gateway_url())

        self.gateway = QQGateway(
            open_socket=open_socket,
            token=tokens.token,
            drop_token=tokens.invalidate,
            on_event=self._on_event,
            on_fatal=self.report_fatal,
            clock=self.clock,
        )
        self._listener = asyncio.ensure_future(self.gateway.run())

    def _stop_listening(self) -> None:
        if self._listener is not None and not self._listener.done():
            self._listener.cancel()

    def _require_client(self) -> QQClient:
        if self.client is None:
            raise ConnectionError("not connected to QQ")
        return self.client

    def is_final_failure(self, result: SendResult) -> bool:
        """A refused token (401 twice): no retry or plain-text resend can help."""
        return (result.error or "").startswith("HTTP 401")

    # -- inbound -----------------------------------------------------------------------------------

    async def _on_event(self, kind: str, data: dict[str, Any]) -> None:
        if kind == "INTERACTION_CREATE":
            await self._on_interaction(data)
            return
        event = self.normalise(kind, data)
        if event is None:
            return
        if event.message_id:
            self._passive[event.source.chat_id] = _Passive(
                event.message_id, self.clock.monotonic(), itertools.count(1)
            )
        await self.emit(event)

    def normalise(self, kind: str, data: Mapping[str, Any]) -> InboundEvent | None:
        """One dispatch → an `InboundEvent`, or None when it is dropped."""
        message_id = str(data.get("id") or "")
        if not message_id:
            return None
        author = _map(data.get("author"))
        content = str(data.get("content") or "")
        metadata: dict[str, Any] = {}
        user_name: str | None = None
        guild: str | None = None
        bot = False
        chat_type: ChatType
        if kind == "C2C_MESSAGE_CREATE":
            user = str(author.get("user_openid") or author.get("id") or "")
            qq_kind: Kind = "c2c"
            qq_id, chat_type = user, "dm"
        elif kind == "GROUP_AT_MESSAGE_CREATE":
            user = str(author.get("member_openid") or author.get("id") or "")
            qq_kind, qq_id, chat_type = "group", str(data.get("group_openid") or ""), "group"
            content = _LEADING_MENTION.sub("", content, count=1)
            metadata["mentioned"] = True
        elif kind in ("AT_MESSAGE_CREATE", "GUILD_MESSAGE_CREATE"):
            user = str(author.get("id") or "")
            qq_kind, qq_id, chat_type = "channel", str(data.get("channel_id") or ""), "channel"
            if _LEADING_MENTION.match(content):
                metadata["mentioned"] = True
            content = _LEADING_MENTION.sub("", content, count=1)
            nick = str(_map(data.get("member")).get("nick") or "")
            user_name = nick or str(author.get("username") or "") or None
            guild = str(data.get("guild_id") or "") or None
            bot = author.get("bot") is True
        elif kind == "DIRECT_MESSAGE_CREATE":
            user = str(author.get("id") or "")
            qq_kind, qq_id, chat_type = "dm", str(data.get("guild_id") or ""), "dm"
            user_name = str(author.get("username") or "") or None
            bot = author.get("bot") is True
        else:
            log.debug("qqbot: event %s ignored", kind)
            return None
        if not user or not qq_id:
            return None
        if not self._seen.first_time(message_id):
            log.debug("qqbot: message %s seen before; dropped", message_id)
            return None
        media, transcript = _attachments(data)
        text = content.strip()
        if transcript:
            metadata["transcript"] = transcript
            text = text or transcript
        if not text and not media:
            return None
        source = Source(
            platform=self.platform,
            chat_id=chat_id_of(qq_kind, qq_id),
            chat_type=chat_type,
            user_id=user,
            user_name=user_name,
            is_bot=bot,
            guild_id=guild,
        )
        return InboundEvent(
            source=source,
            text=text,
            kind=_kind_of(media),
            message_id=message_id,
            media=media,
            metadata=metadata,
            timestamp=_timestamp(data.get("timestamp")),
        )

    async def _on_interaction(self, data: Mapping[str, Any]) -> None:
        interaction = str(data.get("id") or "")
        resolved = _map(_map(data.get("data")).get("resolved"))
        button = str(resolved.get("button_data") or "")
        scene = data.get("chat_type")
        if scene == 1:
            qq_kind: Kind = "group"
            qq_id = str(data.get("group_openid") or "")
            user = str(data.get("group_member_openid") or resolved.get("user_id") or "")
            chat_type: ChatType = "group"
        elif scene == 2:
            qq_kind, chat_type = "c2c", "dm"
            qq_id = user = str(data.get("user_openid") or resolved.get("user_id") or "")
        else:
            qq_kind, chat_type = "channel", "channel"
            qq_id = str(data.get("channel_id") or "")
            user = str(resolved.get("user_id") or "")
        if interaction:
            try:
                await self._require_client().ack_interaction(interaction)
            except Exception as exc:
                log.warning("qqbot: acknowledging a button press failed: %s", self.scrub(str(exc)))
        if not button or not user or not qq_id:
            return
        source = Source(
            platform=self.platform,
            chat_id=chat_id_of(qq_kind, qq_id),
            chat_type=chat_type,
            user_id=user,
            guild_id=str(data.get("guild_id") or "") or None,
        )
        await self.emit(
            InboundEvent(
                source=source,
                text="",
                message_id=str(resolved.get("message_id") or "") or None,
                metadata={"button": button},
            )
        )

    # -- media for the intake ----------------------------------------------------------------------

    async def fetch_media(self, attachment: Attachment) -> AsyncIterator[bytes]:
        """The bytes of an inbound attachment (registered by `register_fetchers`)."""
        client = self._require_client()
        async for chunk in client.download(attachment.ref, cap=self.options.max_media_bytes):
            yield chunk

    def register_fetchers(self, seat: FetcherSeat) -> Callable[[], None] | None:
        return seat.register_fetcher(self.platform, self.fetch_media)

    # -- outbound ----------------------------------------------------------------------------------

    def _reply_fields(self, chat_id: str, reply_to: str | None) -> dict[str, Any]:
        """``msg_id`` + ``msg_seq`` for a passive reply, or a bare ``msg_seq`` (active)."""
        remembered = self._passive.get(chat_id)
        fresh = remembered is not None and self.clock.monotonic() - remembered.at < PASSIVE_WINDOW_S
        if remembered is not None and fresh and (reply_to in (None, remembered.msg_id)):
            return {"msg_id": remembered.msg_id, "msg_seq": next(remembered.seq)}
        if reply_to:
            return {"msg_id": reply_to, "msg_seq": next(self._active_seq)}
        return {"msg_seq": next(self._active_seq)}

    def _text_body(self, target: Target, text: str, fields: dict[str, Any]) -> dict[str, Any]:
        if target.kind in ("channel", "dm"):
            body: dict[str, Any] = {"content": text}
            if "msg_id" in fields:
                body["msg_id"] = fields["msg_id"]
            return body
        if self.capabilities.markup == "markdown":
            return {"markdown": {"content": text}, "msg_type": MSG_MARKDOWN, **fields}
        return {"content": text, "msg_type": MSG_TEXT, **fields}

    def _failure(
        self, exc: BaseException, *, sent: list[str] | None = None, remainder: str | None = None
    ) -> SendResult:
        if isinstance(exc, QQApiError):
            reading = "chat not found" if exc.status == 404 else None
            base = failure_from_status(
                exc.status, exc.detail, exc.headers, now=self.clock.now(), reading=reading
            )
        elif isinstance(exc, httpx.HTTPError):
            base = failure_from_exception(exc)
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

    async def _send(
        self, chat_id: str, text: str, reply_to: str | None, meta: dict[str, Any]
    ) -> SendResult:
        client = self._require_client()
        try:
            target = parse_target(chat_id)
        except ValueError as exc:
            return self._failure(exc)
        chunked = split_message(text, MAX_TEXT)
        sent: list[str] = []
        for n, chunk in enumerate(chunked.chunks):
            if n:
                await self.slots.pace(chat_id)
            body = self._text_body(target, chunk.text, self._reply_fields(chat_id, reply_to))
            try:
                answer = await client.send_message(target.kind, target.id, body)
            except Exception as exc:
                return self._failure(exc, sent=sent, remainder=chunked.remainder(n))
            sent.append(str(answer.get("id") or ""))
        return SendResult.sent(sent[-1], tuple(sent[:-1]))

    async def _typing(self, chat_id: str, meta: dict[str, Any]) -> None:
        target = parse_target(chat_id)
        remembered = self._passive.get(chat_id)
        now = self.clock.monotonic()
        if target.kind != "c2c" or remembered is None or now - remembered.at >= PASSIVE_WINDOW_S:
            return
        if now - self._typing_at.get(chat_id, -TYPING_EVERY_S) < TYPING_EVERY_S:
            return
        self._typing_at[chat_id] = now
        body = {
            "msg_type": MSG_TYPING,
            "msg_id": remembered.msg_id,
            "msg_seq": next(remembered.seq),
            "input_notify": {"input_type": 1, "input_second": TYPING_SECONDS},
        }
        await self._require_client().send_message("c2c", target.id, body)

    def _keyboard(self, buttons: Sequence[tuple[str, str]]) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        for start in range(0, len(buttons), KEYBOARD_ROW):
            row = buttons[start : start + KEYBOARD_ROW]
            rows.append(
                {
                    "buttons": [
                        {
                            "id": str(start + n + 1),
                            "render_data": {
                                "label": _label(label),
                                "visited_label": _label(label),
                                "style": 1,
                            },
                            "action": {
                                "type": 1,
                                "data": data,
                                "permission": {"type": 2},
                                "click_limit": 1,
                            },
                            "group_id": "choices",
                        }
                        for n, (label, data) in enumerate(row)
                    ]
                }
            )
        return {"content": {"rows": rows}}

    async def _send_keyboard(
        self, chat_id: str, text: str, fallback: str, buttons: Sequence[tuple[str, str]]
    ) -> SendResult:
        try:
            target = parse_target(chat_id)
        except ValueError as exc:
            return self._failure(exc)
        too_many = len(buttons) > KEYBOARD_ROW * KEYBOARD_ROWS
        if target.kind not in ("c2c", "group") or too_many or len(text) > MAX_TEXT:
            # Guild chats have no keyboards: the text card with its typed answers.
            return await self._send(chat_id, fallback, None, {})
        body = self._text_body(target, text, self._reply_fields(chat_id, None))
        body["keyboard"] = self._keyboard(buttons)
        try:
            answer = await self._require_client().send_message(target.kind, target.id, body)
        except Exception as exc:
            return self._failure(exc)
        return SendResult.sent(str(answer.get("id") or ""))

    async def _send_approval(
        self, chat_id: str, prompt: ApprovalPrompt, meta: dict[str, Any]
    ) -> SendResult:
        buttons = [
            (action.label, approval_callback(prompt.request_id, action.choice))
            for action in prompt.actions
        ]
        return await self._send_keyboard(chat_id, prompt.text, prompt.fallback_text, buttons)

    async def _send_clarify(
        self, chat_id: str, prompt: ClarifyPrompt, meta: dict[str, Any]
    ) -> SendResult:
        buttons = [
            (choice, clarify_callback(prompt.clarify_id, n))
            for n, choice in enumerate(prompt.choices)
        ]
        fallback = prompt.fallback_text or prompt.question
        return await self._send_keyboard(chat_id, prompt.question, fallback, buttons)

    async def _send_media(
        self, kind: MediaKind, chat_id: str, path: str, caption: str | None, meta: dict[str, Any]
    ) -> SendResult:
        try:
            target = parse_target(chat_id)
        except ValueError as exc:
            return self._failure(exc)
        if target.kind not in ("c2c", "group"):
            return SendResult.refused("sending files to QQ guild chats")
        client = self._require_client()

        async def api(endpoint: str, body: Mapping[str, Any]) -> dict[str, Any]:
            return await client.files_call(target.kind, target.id, endpoint, body)

        uploader = ChunkedUploader(api=api, put=client.put_part, clock=self.clock)
        try:
            done = await uploader.upload(
                path,
                file_type=_FILE_TYPES.get(kind, 4),
                file_name=Path(path).name,
                max_bytes=self.options.max_media_bytes,
            )
        except DailyLimitReached as exc:
            return SendResult.failure(str(exc))
        except UploadFailed as exc:
            return SendResult.failure(f"the QQ upload did not finish: {exc}")
        except Exception as exc:
            return self._failure(exc)
        info = done.get("file_info") or _map(done.get("data")).get("file_info")
        if not info:
            return SendResult.failure("the QQ upload answered without file_info")
        reply_to = meta.get(REPLY_TO_KEY)
        fields = self._reply_fields(chat_id, str(reply_to) if reply_to else None)
        body: dict[str, Any] = {"msg_type": MSG_MEDIA, "media": {"file_info": info}, **fields}
        if caption:
            body["content"] = caption[:MAX_TEXT]
        try:
            answer = await client.send_message(target.kind, target.id, body)
        except Exception as exc:
            return self._failure(exc)
        return SendResult.sent(str(answer.get("id") or ""))

    async def _chat_info(self, chat_id: str) -> ChatInfo:
        target = parse_target(chat_id)
        return ChatInfo(name=chat_id, type=_CHAT_TYPES[target.kind])


class QQBotMarkdownAdapter(QQBotAdapter):
    """The same adapter sending QQ Markdown (``platforms.qqbot.markdown: true``)."""

    capabilities: ClassVar[Capabilities] = replace(QQBotAdapter.capabilities, markup="markdown")


def _label(text: str) -> str:
    clean = " ".join(text.split())
    return clean if len(clean) <= LABEL_MAX else clean[: LABEL_MAX - 1] + "…"
