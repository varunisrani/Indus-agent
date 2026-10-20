"""The Weixin adapter: a personal WeChat account through Tencent's iLink Bot API (P16-19).

Connecting proves the bot token with one ``getconfig`` call. The long poll (``getupdates``) starts
when an inbound sink is installed and never in a send-only adapter (``indus send``). The poll
cursor is saved after the messages it covered were handed on; on a restart the poll resumes from
it, so messages are neither lost nor repeated.

Inbound rules (each message is at most one `InboundEvent`):

- dropped: our own messages, empty ones, repeats of a message id within 300 s and repeats of the
  same text from the same sender within 60 s (iLink re-delivers some messages under new ids);
- a message with a ``room_id`` is a group message, kept only under ``group_policy: open`` (or
  ``allowlist`` naming the room); one that names the bot (``mention_names``) is marked mentioned
  and the name removed; everything else is a DM with the sender;
- the conversation's ``context_token`` is stored for the chat (on disk, 0600) — replies must echo
  it, and a chat without one cannot be written to;
- text, quoted text, and media (image, voice, file, video) as attachments the intake fetches with
  `fetch_media` (downloaded from the CDN and decrypted, `media`); a voice note is always fetched as
  audio for the gateway's own transcription, Weixin's transcript is used only when there is none.

Outbound: text in pieces of at most 2,000 characters (long lines wrapped for copying, runs of blank
lines collapsed, code blocks untouched), each piece one ``sendmessage`` with the stored context
token; files are encrypted with a fresh key, uploaded to the CDN, then sent as an item (a caption
goes first as text). iLink cannot edit, delete or show buttons. A frequency limit opens a 30 s
pause for further sends; an expired or unready session is a final failure that asks the person
to write first. The bot token and the context tokens never appear in logs, errors or reprs.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import mimetypes
import os
import re
import textwrap
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal

import httpx

from indus.foundation.blocking import run_blocking
from indus.foundation.clock import Clock
from indus.foundation.redact import redact_text
from indus.gateway.adapters.base import (
    Adapter,
    AdapterConnectError,
    Capabilities,
    ChatInfo,
    FetcherSeat,
    MediaKind,
)
from indus.gateway.adapters.weixin import media as wxmedia
from indus.gateway.adapters.weixin.api import (
    CDN_BASE,
    ILINK_BASE,
    POLL_TIMEOUT_MS,
    ILinkClient,
    ILinkError,
    answer_error,
)
from indus.gateway.core.events import Attachment, ChatType, InboundEvent, MessageKind, Source
from indus.gateway.core.send import SendResult, is_ambiguous_timeout
from indus.gateway.outbound.chunker import split_message

if TYPE_CHECKING:
    from indus.gateway.config import PlatformSettings

__all__ = [
    "MAX_CHARS",
    "MEDIA_SCHEME",
    "NO_TOKEN",
    "ContextTokens",
    "WeixinAdapter",
    "WeixinOptions",
    "prepare_text",
]

log = logging.getLogger(__name__)

MAX_CHARS = 2_000
COPY_WIDTH = 120
DEDUPE_TTL_S = 300.0
CONTENT_DEDUPE_TTL_S = 60.0
DEDUPE_CAP = 2_000
TYPING_TICKET_TTL_S = 600.0
RATE_PAUSE_S = 30.0
RATE_RETRY_S = 3.0
SESSION_PAUSE_S = 600.0
RETRY_S = 2.0
BACKOFF_S = 30.0
FAILURES_BEFORE_BACKOFF = 3
DEFAULT_MAX_MEDIA_BYTES = 100 * 1024 * 1024
MEDIA_REFS = 1_000
MEDIA_SCHEME = "weixin-media://"
NO_TOKEN = (
    "no conversation token for this Weixin chat: the person has to message the bot first "
    "(iLink only lets a bot answer a conversation it has a token for)"
)

ITEM_TEXT, ITEM_IMAGE, ITEM_VOICE, ITEM_FILE, ITEM_VIDEO = 1, 2, 3, 4, 5
MEDIA_IMAGE, MEDIA_VIDEO, MEDIA_FILE, MEDIA_VOICE = 1, 2, 3, 4
_ITEM_KEYS: Mapping[int, str] = {
    ITEM_IMAGE: "image_item",
    ITEM_VOICE: "voice_item",
    ITEM_FILE: "file_item",
    ITEM_VIDEO: "video_item",
}
_ITEM_KINDS: Mapping[int, MessageKind] = {
    ITEM_IMAGE: MessageKind.PHOTO,
    ITEM_VOICE: MessageKind.VOICE,
    ITEM_FILE: MessageKind.DOCUMENT,
    ITEM_VIDEO: MessageKind.VIDEO,
}
_ITEM_MIMES: Mapping[int, str] = {
    ITEM_IMAGE: "image/jpeg",
    ITEM_VOICE: "audio/silk",
    ITEM_VIDEO: "video/mp4",
}
_FENCE = re.compile(r"^```")
_TABLE_RULE = re.compile(r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*$")

type GroupPolicy = Literal["disabled", "open", "allowlist"]


def _ids(value: object) -> frozenset[str]:
    if isinstance(value, str):
        items: Iterable[object] = value.split(",")
    elif isinstance(value, list | tuple | set | frozenset):
        items = value  # pyright: ignore[reportUnknownVariableType]
    else:
        return frozenset()
    return frozenset(str(v).strip() for v in items if str(v).strip())  # pyright: ignore[reportUnknownArgumentType, reportUnknownVariableType]


@dataclass(frozen=True)
class WeixinOptions:
    """``platforms.weixin.*`` settings the adapter reads (all optional)."""

    group_policy: GroupPolicy = "disabled"
    group_allow_from: frozenset[str] = frozenset()
    mention_names: tuple[str, ...] = ()
    max_media_bytes: int = DEFAULT_MAX_MEDIA_BYTES

    @classmethod
    def read(cls, extra: Mapping[str, Any], env: Mapping[str, str]) -> WeixinOptions:
        policy = str(extra.get("group_policy") or env.get("WEIXIN_GROUP_POLICY") or "disabled")
        policy = policy.strip().lower()
        try:
            cap = int(extra.get("max_media_bytes") or DEFAULT_MAX_MEDIA_BYTES)
        except (TypeError, ValueError):
            cap = DEFAULT_MAX_MEDIA_BYTES
        groups = _ids(extra.get("group_allow_from")) | _ids(
            env.get("WEIXIN_GROUP_ALLOWED_USERS", "")
        )
        return cls(
            group_policy=policy if policy in ("open", "allowlist") else "disabled",  # pyright: ignore[reportArgumentType]
            group_allow_from=groups,
            mention_names=tuple(sorted(_ids(extra.get("mention_names")))),
            max_media_bytes=max(1, cap),
        )


# -- small stores ----------------------------------------------------------------------------------


def _write_private(path: Path, data: Mapping[str, Any]) -> None:
    """Atomically replace ``path`` with ``data`` as JSON, 0600, in a 0700 directory."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(dict(data), handle)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return dict(loaded) if isinstance(loaded, dict) else {}  # pyright: ignore[reportUnknownArgumentType]


class ContextTokens:
    """The newest ``context_token`` per chat; with ``path`` it is kept on disk (0600)."""

    def __init__(self, path: Path | None = None, seed: Mapping[str, str] | None = None) -> None:
        self.path = path
        self._tokens: dict[str, str] = dict(seed or {})
        self._lock = asyncio.Lock()

    def __repr__(self) -> str:
        return f"<ContextTokens {len(self._tokens)} chats>"

    def get(self, chat_id: str) -> str | None:
        return self._tokens.get(chat_id)

    def values(self) -> tuple[str, ...]:
        return tuple(self._tokens.values())

    async def load(self) -> None:
        path = self.path
        if path is None:
            return
        stored = await run_blocking(lambda: _read_json(path), label="weixin tokens")
        for chat, token in stored.items():
            if isinstance(token, str) and token:
                self._tokens.setdefault(str(chat), token)

    async def put(self, chat_id: str, token: str) -> None:
        if not token or self._tokens.get(chat_id) == token:
            return
        self._tokens[chat_id] = token
        await self._save()

    async def drop(self, chat_id: str) -> None:
        if self._tokens.pop(chat_id, None) is not None:
            await self._save()

    async def _save(self) -> None:
        path = self.path
        if path is None:
            return
        async with self._lock:
            snapshot = dict(self._tokens)
            try:
                await run_blocking(lambda: _write_private(path, snapshot), label="weixin tokens")
            except OSError as exc:
                log.warning("weixin: conversation tokens could not be saved: %s", exc.strerror)


class _Seen:
    """Keys seen within a time window (bounded)."""

    def __init__(self, clock: Clock, ttl: float) -> None:
        self.clock = clock
        self.ttl = ttl
        self._at: OrderedDict[str, float] = OrderedDict()

    def repeat(self, key: str) -> bool:
        now = self.clock.monotonic()
        while self._at:
            oldest, at = next(iter(self._at.items()))
            if now - at < self.ttl and len(self._at) <= DEDUPE_CAP:
                break
            del self._at[oldest]
        if key in self._at:
            return True
        self._at[key] = now
        return False


@dataclass(frozen=True)
class _MediaRef:
    param: str
    full_url: str
    key: bytes | None
    name: str


@dataclass
class _Ticket:
    value: str
    at: float


@dataclass
class _Inbound:
    text: str = ""
    quoted: str | None = None
    media: list[tuple[MessageKind, Attachment]] = field(default_factory=list[Any])
    voice_transcript: str = ""


# -- text ------------------------------------------------------------------------------------------


def prepare_text(text: str) -> str:
    """Trailing spaces off, blank-line runs collapsed to one and long prose lines wrapped at 120
    characters for copying; fenced code and table rows are left as they are."""
    out: list[str] = []
    in_code = False
    blank = False
    for raw in text.splitlines():
        line = raw.rstrip()
        fence = bool(_FENCE.match(line.strip()))
        if in_code or fence:
            out.append(line)
            blank = False
            if fence:
                in_code = not in_code
            continue
        if not line.strip():
            if not blank and out:
                out.append("")
            blank = True
            continue
        blank = False
        stripped = line.strip()
        if len(line) <= COPY_WIDTH or stripped.startswith("|") or _TABLE_RULE.match(stripped):
            out.append(line)
            continue
        out.extend(
            textwrap.wrap(line, width=COPY_WIDTH, break_long_words=False, break_on_hyphens=False)
            or [line]
        )
    return "\n".join(out).strip()


# -- the adapter -----------------------------------------------------------------------------------


class WeixinAdapter(Adapter):
    platform: ClassVar[str] = "weixin"
    capabilities: ClassVar[Capabilities] = Capabilities(
        max_len=MAX_CHARS,
        markup="markdown",
        splits_natively=True,
        supports_edit=False,
        supports_delete=False,
        supports_buttons=False,
        dm_is_private=True,
        push_capable=True,
        # The pieces of one long reply (and back-to-back replies) leave 1.5 s apart: iLink's
        # frequency limit trips on bursts.
        slot_spacing=1.5,
    )
    vendor_names: ClassVar[Mapping[str, tuple[str, ...]]] = {
        "indus.gateway.adapters.weixin.api:ILinkClient": (
            "get_updates",
            "send_message",
            "send_typing",
            "get_config",
            "get_upload_url",
            "cdn_upload",
            "cdn_download",
        ),
    }

    def __init__(
        self,
        *,
        clock: Clock,
        token: str,
        account_id: str,
        settings: PlatformSettings | None = None,
        base_url: str = ILINK_BASE,
        cdn_base_url: str = CDN_BASE,
        options: WeixinOptions | None = None,
        tokens: ContextTokens | None = None,
        state_dir: Path | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        proxy: str | None = None,
        send_only: bool = False,
    ) -> None:
        super().__init__(clock=clock, settings=settings, secrets=(token,))
        self._token = token.strip()
        self.account_id = account_id.strip()
        self._base_url = base_url.strip().rstrip("/") or ILINK_BASE
        self._cdn_base_url = cdn_base_url.strip().rstrip("/") or CDN_BASE
        self.options = options or WeixinOptions()
        self._state_dir = state_dir
        self.tokens = tokens or ContextTokens(
            state_dir / "context_tokens.json" if state_dir is not None else None
        )
        self._transport = transport
        self._proxy = proxy
        self.send_only = send_only
        self._client: ILinkClient | None = None
        self._listener: asyncio.Task[None] | None = None
        self._seen_ids = _Seen(clock, DEDUPE_TTL_S)
        self._seen_texts = _Seen(clock, CONTENT_DEDUPE_TTL_S)
        self._tickets: dict[str, _Ticket] = {}
        self._media_refs: OrderedDict[str, _MediaRef] = OrderedDict()
        self._media_ids = 0
        self._paused_until = 0.0

    def __repr__(self) -> str:
        state = "connected" if self.connected else "disconnected"
        return f"<WeixinAdapter {state}>"

    def scrub(self, text: str) -> str:
        """Credentials and every stored conversation token masked."""
        return redact_text(text, (*self._secrets, *self.tokens.values()))

    # -- lifecycle ---------------------------------------------------------------------------------

    async def _open(self, is_reconnect: bool) -> None:
        missing = [
            name
            for name, value in (
                ("WEIXIN_TOKEN", self._token),
                ("WEIXIN_ACCOUNT_ID", self.account_id),
            )
            if not value
        ]
        if missing:
            raise AdapterConnectError(
                "Weixin is not configured: set " + " and ".join(missing),
                retryable=False,
                code="config",
            )
        if not self._base_url.startswith("https://"):
            raise AdapterConnectError(
                "the iLink base URL must be https", retryable=False, code="config"
            )
        try:
            # Media moves only over https to Tencent's own CDN hosts, a configured base included.
            wxmedia.check_cdn_url(self._cdn_base_url)
        except wxmedia.MediaKeyError:
            raise AdapterConnectError(
                "the Weixin CDN base URL must be https on a Tencent media host",
                retryable=False,
                code="config",
            ) from None
        client = ILinkClient(
            self._base_url,
            self._token,
            cdn_base_url=self._cdn_base_url,
            transport=self._transport,
            proxy=self._proxy,
        )
        try:
            # A light authenticated call proves the token; its ``ret`` does not matter here.
            await client.get_config(self.account_id)
            await self.tokens.load()
        except ILinkError as exc:
            await client.aclose()
            if exc.is_auth:
                raise AdapterConnectError(
                    "iLink refused the bot token; log in again (indus gateway setup weixin)",
                    retryable=False,
                    code="auth_failed",
                ) from None
            raise AdapterConnectError(str(exc), retryable=True) from None
        except BaseException:
            await client.aclose()
            raise
        self._client = client
        log.info("weixin: connected to iLink (%s)", self._base_url)
        if self.options.group_policy != "disabled":
            log.info(
                "weixin: group messages are on (%s), but iLink delivers group events only for "
                "some bot accounts; DMs may be the only channel",
                self.options.group_policy,
            )
        if self._sink is not None:
            self._start_listening()

    async def _close(self) -> None:
        listener, self._listener = self._listener, None
        if listener is not None:
            listener.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await listener
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    def set_sink(self, sink: Any) -> None:
        """Installing a sink starts the long poll (never when send-only)."""
        super().set_sink(sink)
        if sink is None:
            if self._listener is not None:
                self._listener.cancel()
        elif self.connected and self._client is not None:
            self._start_listening()

    @property
    def listening(self) -> bool:
        return self._listener is not None and not self._listener.done()

    def _start_listening(self) -> None:
        if self.listening or self.send_only:
            return
        self._listener = asyncio.ensure_future(self._poll())

    def is_final_failure(self, result: SendResult) -> bool:
        error = result.error or ""
        return any(
            mark in error
            for mark in (
                "no conversation token",
                "session expired",
                "session not ready",
                "unauthorized",
            )
        )

    def _require_client(self) -> ILinkClient:
        if self._client is None:
            raise ConnectionError("not connected to iLink")
        return self._client

    # -- the long poll -----------------------------------------------------------------------------

    def _cursor_path(self) -> Path | None:
        return self._state_dir / "cursor.json" if self._state_dir is not None else None

    async def _load_cursor(self) -> str:
        path = self._cursor_path()
        if path is None:
            return ""
        stored = await run_blocking(lambda: _read_json(path), label="weixin cursor")
        value = stored.get("get_updates_buf")
        return value if isinstance(value, str) else ""

    async def _save_cursor(self, cursor: str) -> None:
        path = self._cursor_path()
        if path is None:
            return
        try:
            await run_blocking(
                lambda: _write_private(path, {"get_updates_buf": cursor}), label="weixin cursor"
            )
        except OSError as exc:
            log.warning("weixin: the poll cursor could not be saved: %s", exc.strerror)

    async def _poll(self) -> None:
        cursor = await self._load_cursor()
        timeout_ms = POLL_TIMEOUT_MS
        failures = 0
        while True:
            client = self._client
            if client is None:
                return
            try:
                answer = await client.get_updates(cursor, timeout_ms=timeout_ms)
                error = answer_error(answer)
                if error is not None:
                    raise error
            except asyncio.CancelledError:
                raise
            except ILinkError as exc:
                if exc.is_auth:
                    log.error("weixin: iLink refused the bot token; inbound stops")
                    self.report_fatal(
                        "weixin_auth",
                        "iLink refused the bot token while polling; log in again "
                        "(indus gateway setup weixin)",
                    )
                    return
                if exc.session_expired:
                    log.error(
                        "weixin: the iLink session expired; polling again in %.0f minutes",
                        SESSION_PAUSE_S / 60,
                    )
                    await self.clock.sleep(SESSION_PAUSE_S)
                    failures = 0
                    continue
                failures = await self._back_off(failures, self.scrub(str(exc)))
                continue
            except Exception as exc:
                failures = await self._back_off(
                    failures, self.scrub(f"{type(exc).__name__}: {exc}")
                )
                continue
            failures = 0
            suggested = answer.get("longpolling_timeout_ms")
            if isinstance(suggested, int) and suggested > 0:
                timeout_ms = suggested
            messages = answer.get("msgs")
            for message in messages if isinstance(messages, list) else []:  # pyright: ignore[reportUnknownVariableType]
                if not isinstance(message, Mapping):
                    continue
                try:
                    await self._handle(message)  # pyright: ignore[reportUnknownArgumentType]
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("weixin: an inbound message could not be handled")
            following = answer.get("get_updates_buf")
            if isinstance(following, str) and following and following != cursor:
                cursor = following
                await self._save_cursor(cursor)

    async def _back_off(self, failures: int, reason: str) -> int:
        failures += 1
        if failures >= FAILURES_BEFORE_BACKOFF:
            log.warning("weixin: polling failed %d times (%s); pausing", failures, reason)
            await self.clock.sleep(BACKOFF_S)
            return 0
        log.warning("weixin: polling failed (%s); retrying", reason)
        await self.clock.sleep(RETRY_S)
        return failures

    # -- inbound -----------------------------------------------------------------------------------

    def _mentioned(self, text: str) -> tuple[bool, str]:
        for name in self.options.mention_names:
            pattern = re.compile(rf"@{re.escape(name)}\b[:,]?\s*", re.IGNORECASE)
            if pattern.search(text):
                return True, pattern.sub("", text).strip()
        return False, text

    def _remember_media(self, ref: _MediaRef) -> str:
        self._media_ids += 1
        handle = f"{MEDIA_SCHEME}{self._media_ids}"
        self._media_refs[handle] = ref
        while len(self._media_refs) > MEDIA_REFS:
            self._media_refs.popitem(last=False)
        return handle

    def _media_item(
        self, kind: int, item: Mapping[str, Any]
    ) -> tuple[MessageKind, Attachment] | None:
        payload = _mapping(item.get(_ITEM_KEYS[kind]))
        block = _mapping(payload.get("media"))
        param = str(block.get("encrypt_query_param") or "")
        full_url = str(block.get("full_url") or "")
        if not param and not full_url:
            return None
        key_text = payload.get("aeskey") if kind == ITEM_IMAGE else None
        key: bytes | None
        try:
            if isinstance(key_text, str) and key_text:
                key = bytes.fromhex(key_text)
            elif isinstance(block.get("aes_key"), str) and block.get("aes_key"):
                key = wxmedia.parse_key(str(block.get("aes_key")))
            else:
                key = None
            if not param:
                wxmedia.check_cdn_url(full_url)
        except (ValueError, wxmedia.MediaKeyError) as exc:
            log.warning("weixin: an inbound attachment was refused: %s", exc)
            return None
        name = str(payload.get("file_name") or "")
        mime = _ITEM_MIMES.get(kind) or mimetypes.guess_type(name)[0] or "application/octet-stream"
        handle = self._remember_media(_MediaRef(param, full_url, key, name))
        return _ITEM_KINDS[kind], Attachment(ref=handle, mime=mime)

    def _read_items(self, items: Iterable[object]) -> _Inbound:
        found = _Inbound()
        for raw in items:
            item = _mapping(raw)
            kind = item.get("type")
            if kind == ITEM_TEXT and not found.text:
                found.text = str(_mapping(item.get("text_item")).get("text") or "")
                quote = _mapping(item.get("ref_msg"))
                inner = _mapping(quote.get("message_item"))
                parts = [str(quote.get("title") or "").strip()]
                if inner.get("type") == ITEM_TEXT:
                    parts.append(str(_mapping(inner.get("text_item")).get("text") or "").strip())
                quoted = " | ".join(p for p in parts if p)
                if quoted:
                    found.quoted = quoted
                if isinstance(inner.get("type"), int) and inner.get("type") in _ITEM_KEYS:
                    attached = self._media_item(int(inner["type"]), inner)
                    if attached is not None:
                        found.media.append(attached)
            elif isinstance(kind, int) and kind in _ITEM_KEYS:
                attached = self._media_item(kind, item)
                if attached is not None:
                    found.media.append(attached)
                elif kind == ITEM_VOICE:
                    transcript = str(_mapping(item.get("voice_item")).get("text") or "")
                    found.voice_transcript = transcript
        return found

    async def _handle(self, message: Mapping[str, Any]) -> None:
        sender = str(message.get("from_user_id") or "").strip()
        if not sender or sender == self.account_id:
            return
        message_id = str(message.get("message_id") or "").strip()
        if message_id and self._seen_ids.repeat(message_id):
            return
        items = message.get("item_list")
        found = self._read_items(items if isinstance(items, list) else [])  # pyright: ignore[reportUnknownArgumentType]
        text = found.text
        if not text and not found.media and found.voice_transcript:
            text = f"[voice message, Weixin transcript] {found.voice_transcript}"
        if not text and not found.media:
            return
        if text and not found.media:
            digest = hashlib.sha256(f"{sender}\n{text}".encode()).hexdigest()
            if self._seen_texts.repeat(digest):
                log.debug("weixin: the same text from the same sender again; dropped")
                return
        room = str(message.get("room_id") or message.get("chat_room_id") or "").strip()
        chat_type: ChatType = "group" if room else "dm"
        chat_id = room or sender
        if room and not self._group_allowed(room):
            return
        token = str(message.get("context_token") or "").strip()
        if token:
            await self.tokens.put(chat_id, token)
        metadata: dict[str, Any] = {}
        if room:
            named, text = self._mentioned(text)
            if named:
                metadata["mentioned"] = True
        kind = found.media[0][0] if found.media else MessageKind.TEXT
        sent_ms = message.get("create_time_ms")
        when = (
            datetime.fromtimestamp(sent_ms / 1000, UTC)
            if isinstance(sent_ms, int | float)
            else None
        )
        source = Source(
            platform=self.platform,
            chat_id=chat_id,
            chat_type=chat_type,
            user_id=sender,
            user_name=sender,
            is_bot=message.get("message_type") == 2,
        )
        await self.emit(
            InboundEvent(
                source=source,
                text=text,
                kind=kind,
                message_id=message_id or None,
                media=tuple(a for _, a in found.media),
                reply_to_text=found.quoted,
                metadata=metadata,
                timestamp=when,
            )
        )

    def _group_allowed(self, room: str) -> bool:
        policy = self.options.group_policy
        if policy == "open":
            return True
        if policy == "allowlist":
            return room in self.options.group_allow_from
        log.debug("weixin: a group message arrived and group_policy is disabled; dropped")
        return False

    # -- media for the intake ----------------------------------------------------------------------

    async def fetch_media(self, attachment: Attachment) -> AsyncIterator[bytes]:
        """The decrypted bytes of an inbound attachment (registered by `register_fetchers`)."""
        ref = self._media_refs.get(attachment.ref)
        if ref is None:
            raise ILinkError("message not found: the attachment is no longer known")
        client = self._require_client()
        url = (
            wxmedia.download_url(self._cdn_base_url, ref.param)
            if ref.param
            else wxmedia.check_cdn_url(ref.full_url)
        )
        chunks = [c async for c in client.cdn_download(url, cap=self.options.max_media_bytes + 16)]
        data = b"".join(chunks)
        if ref.key is not None:
            data = await run_blocking(
                lambda: wxmedia.decrypt(data, ref.key or b""), label="weixin media"
            )
        yield data

    def register_fetchers(self, seat: FetcherSeat) -> Callable[[], None] | None:
        return seat.register_fetcher(self.platform, self.fetch_media)

    # -- outbound ----------------------------------------------------------------------------------

    def _paused(self) -> float:
        return max(0.0, self._paused_until - self.clock.monotonic())

    async def _deliver(self, chat_id: str, items: list[dict[str, Any]], token: str) -> str:
        """One ``sendmessage``; the message id (the server's, else our client id)."""
        client = self._require_client()
        client_id = f"indus-weixin-{uuid.uuid4().hex}"
        try:
            answer = await client.send_message(
                chat_id, items, context_token=token, client_id=client_id
            )
        except ILinkError as exc:
            if exc.session_expired:
                # The stored token is dead: the next reply needs a fresh one from the person.
                await self.tokens.drop(chat_id)
            elif exc.frequency_limited:
                self._paused_until = self.clock.monotonic() + RATE_PAUSE_S
                exc.retry_after = RATE_RETRY_S
            raise
        return str(answer.get("message_id") or client_id)

    def _send_gate(self, chat_id: str) -> tuple[str | None, SendResult | None]:
        token = self.tokens.get(chat_id)
        if not token:
            return None, SendResult.failure(NO_TOKEN)
        wait = self._paused()
        if wait > 0:
            return None, SendResult.failure(
                "rate limited: iLink frequency limit, sends paused", retry_after=wait
            )
        return token, None

    async def _send(
        self, chat_id: str, text: str, reply_to: str | None, meta: dict[str, Any]
    ) -> SendResult:
        token, refused = self._send_gate(chat_id)
        if refused is not None or token is None:
            return refused or SendResult.failure(NO_TOKEN)
        body = text if meta.get("plain") is True else prepare_text(text)
        chunked = split_message(body or text, MAX_CHARS)
        sent: list[str] = []
        for n, chunk in enumerate(chunked.chunks):
            if n:
                await self.slots.pace(chat_id)
            items = [{"type": ITEM_TEXT, "text_item": {"text": chunk.text}}]
            try:
                sent.append(await self._deliver(chat_id, items, token))
            except Exception as exc:
                remainder = None if is_ambiguous_timeout(exc) else chunked.remainder(n)
                return SendResult.failure(
                    self.scrub(f"{exc}") if isinstance(exc, ILinkError) else None,
                    exc=None if isinstance(exc, ILinkError) else exc,
                    retry_after=getattr(exc, "retry_after", None),
                    delivered_ids=tuple(sent),
                    remainder=remainder if sent else None,
                )
        return SendResult.sent(sent[-1], tuple(sent[:-1]))

    async def _typing(self, chat_id: str, meta: dict[str, Any]) -> None:
        ticket = await self._ticket(chat_id)
        if ticket:
            await self._require_client().send_typing(chat_id, ticket, on=True)

    async def _typing_stop(self, chat_id: str) -> None:
        ticket = await self._ticket(chat_id)
        if ticket:
            await self._require_client().send_typing(chat_id, ticket, on=False)

    async def _ticket(self, chat_id: str) -> str | None:
        now = self.clock.monotonic()
        held = self._tickets.get(chat_id)
        if held is not None and now - held.at < TYPING_TICKET_TTL_S:
            return held.value
        answer = await self._require_client().get_config(
            chat_id, context_token=self.tokens.get(chat_id)
        )
        value = str(answer.get("typing_ticket") or "")
        if not value:
            return None
        self._tickets[chat_id] = _Ticket(value, now)
        return value

    async def _send_media(
        self, kind: MediaKind, chat_id: str, path: str, caption: str | None, meta: dict[str, Any]
    ) -> SendResult:
        token, refused = self._send_gate(chat_id)
        if refused is not None or token is None:
            return refused or SendResult.failure(NO_TOKEN)
        file = Path(path)
        size = (await run_blocking(file.stat, label="weixin upload")).st_size
        if size > self.options.max_media_bytes:
            return SendResult.failure(
                f"the file is too large for Weixin ({size} bytes, the cap is "
                f"{self.options.max_media_bytes})"
            )
        data = await run_blocking(file.read_bytes, label="weixin upload")
        mime = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        if kind == "image" or (
            kind not in ("voice", "file", "document") and mime.startswith("image/")
        ):
            media_type, item_type = MEDIA_IMAGE, ITEM_IMAGE
        elif kind == "video" or mime.startswith("video/"):
            media_type, item_type = MEDIA_VIDEO, ITEM_VIDEO
        elif kind == "voice" and file.suffix.lower() == ".silk":
            media_type, item_type = MEDIA_VOICE, ITEM_VOICE
        else:
            # Other audio and every other file travel as a file the person can open.
            media_type, item_type = MEDIA_FILE, ITEM_FILE
        key = wxmedia.new_key()
        cipher = await run_blocking(lambda: wxmedia.encrypt(data, key), label="weixin upload")
        raw_md5 = hashlib.md5(data, usedforsecurity=False).hexdigest()  # the vendor's checksum
        filekey = uuid.uuid4().hex
        client = self._require_client()
        sent: list[str] = []
        try:
            grant = await client.get_upload_url(
                filekey=filekey,
                media_type=media_type,
                to=chat_id,
                raw_size=len(data),
                raw_md5=raw_md5,
                size=len(cipher),
                key_hex=key.hex(),
            )
            direct = str(grant.get("upload_full_url") or "")
            param = str(grant.get("upload_param") or "")
            if direct:
                url = wxmedia.check_cdn_url(direct)
            elif param:
                url = wxmedia.upload_url(self._cdn_base_url, param, filekey)
            else:
                raise ILinkError("iLink gave no upload address for the file")
            download = await client.cdn_upload(url, cipher)
            block = {
                "encrypt_query_param": download,
                "aes_key": wxmedia.key_for_api(key),
                "encrypt_type": 1,
            }
            item = _media_payload(item_type, block, file.name, len(data), len(cipher), raw_md5)
            if caption:
                text_item = {"type": ITEM_TEXT, "text_item": {"text": prepare_text(caption)}}
                sent.append(await self._deliver(chat_id, [text_item], token))
            sent.append(await self._deliver(chat_id, [item], token))
        except Exception as exc:
            return SendResult.failure(
                self.scrub(str(exc))
                if isinstance(exc, ILinkError | wxmedia.MediaKeyError)
                else None,
                exc=None if isinstance(exc, ILinkError | wxmedia.MediaKeyError) else exc,
                retry_after=getattr(exc, "retry_after", None),
                delivered_ids=tuple(sent),
            )
        return SendResult.sent(sent[-1], tuple(sent[:-1]))

    async def _chat_info(self, chat_id: str) -> ChatInfo:
        is_group = chat_id.endswith("@chatroom")
        return ChatInfo(name=chat_id, type="group" if is_group else "dm")


def _media_payload(
    item_type: int,
    block: dict[str, Any],
    name: str,
    raw_size: int,
    cipher_size: int,
    raw_md5: str,
) -> dict[str, Any]:
    if item_type == ITEM_IMAGE:
        fields: dict[str, Any] = {"mid_size": cipher_size}
    elif item_type == ITEM_VIDEO:
        fields = {"video_size": cipher_size, "play_length": 0, "video_md5": raw_md5}
    elif item_type == ITEM_VOICE:
        fields = {"encode_type": 6, "sample_rate": 24000, "bits_per_sample": 16, "playtime": 0}
    else:
        fields = {"file_name": name, "len": str(raw_size)}
    return {"type": item_type, _ITEM_KEYS[item_type]: {"media": block, **fields}}


def _mapping(value: object) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value  # pyright: ignore[reportUnknownVariableType]
    return {}


def state_dir_for(home: Path, account_id: str) -> Path:
    """``<home>/platforms/weixin/<account>``: the account id made safe for a folder name."""
    safe = re.sub(r"[^A-Za-z0-9._@-]", "_", account_id) or "default"
    return home / "platforms" / "weixin" / safe
