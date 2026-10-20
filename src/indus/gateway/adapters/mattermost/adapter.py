"""The Mattermost adapter (P16-08): REST API v4 for everything we send, the WebSocket for what we
receive, a bot or personal-access token.

Connecting proves the token (``GET users/me``) and nothing more. The WebSocket listener starts
once an inbound sink is installed (the daemon does that right after connecting) and stops when it
is removed; a send-only adapter (``AdapterConfig.send_only``, ``indus send``) never opens the
socket. A dropped socket is opened again after 2 s, doubling to 60 s (with up to 20 % jitter); a
refused upgrade (401/403) stops the listener and is reported through `Adapter.report_fatal`
(``mattermost_auth``).

Inbound (``posted`` events only; one `InboundEvent` per human post):

- dropped: our own posts, system posts (a non-empty ``type``), repeats (the last 1,000 post ids)
  and posts in channels outside ``allowed_channels`` (when set; DMs exempt);
- channel types: ``D`` → dm, ``G`` / ``P`` → group, ``O`` → channel; a ``root_id`` makes a thread
  (``chat_type`` ``thread`` outside DMs); ``reply_mode: thread`` makes every top-level channel post
  a thread root;
- the bot is mentioned when the event's ``mentions`` list names it or the text has ``@username``
  (removed from the text); ``from_bot`` / ``from_webhook`` posts are marked ``is_bot``;
- a slash command typed after a space (Mattermost clients keep ``/`` for their own commands)
  counts as a command;
- files become attachments (``mattermost://file/<id>/<name>``); `fetch_media` streams them.

Outbound: text is cut at 4,000 characters; ``![alt](url)`` becomes the bare URL (the client
previews it); ``@channel`` / ``@all`` / ``@here`` outside code are defused so a reply never pings a
whole channel; threads by ``root_id`` (in ``reply_mode: thread`` a reply to a post threads under
that post's root).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import mimetypes
import random
import re
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal
from urllib.parse import quote, unquote

from indus.foundation.blocking import run_blocking
from indus.foundation.clock import Clock
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
from indus.gateway.adapters.mattermost.client import (
    INSTALL_HINT,
    MattermostClient,
    MattermostError,
    SocketOpener,
    httpx_ws_opener,
)
from indus.gateway.core.events import Attachment, ChatType, InboundEvent, MessageKind, Source
from indus.gateway.core.send import SendResult, is_ambiguous_timeout
from indus.gateway.outbound.chunker import split_message

if TYPE_CHECKING:
    import httpx

    from indus.gateway.config import PlatformSettings

__all__ = [
    "MAX_POST_CHARS",
    "MattermostAdapter",
    "MattermostOptions",
    "file_ref",
    "parse_file_ref",
]

log = logging.getLogger(__name__)

# The server takes up to 16,383 characters; 4,000 keeps a reply readable.
MAX_POST_CHARS = 4000
SEEN_POSTS = 1_000
DEFAULT_MAX_MEDIA_BYTES = 100 * 1024 * 1024
BACKOFF_FIRST_S = 2.0
BACKOFF_CAP_S = 60.0
BACKOFF_JITTER = 0.2
TOKEN_ENV = "MATTERMOST_TOKEN"
URL_ENV = "MATTERMOST_URL"
FILE_SCHEME = "mattermost://file/"

_CHANNEL_TYPES: Mapping[str, ChatType] = {"D": "dm", "G": "group", "P": "group", "O": "channel"}
_IMAGE_MARKDOWN = re.compile(r"!\[[^\]\n]*\]\(([^)\s]+)\)")
_MASS_MENTION = re.compile(r"(?<![\w@])@(channel|all|here)\b", re.IGNORECASE)
_CODE = re.compile(r"```.*?```|`[^`\n]+`", re.DOTALL)
_TRUTHY = frozenset({"1", "true", "yes", "on"})

type ReplyMode = Literal["off", "thread"]


def _ids(value: object) -> frozenset[str]:
    if isinstance(value, str):
        items: Iterable[object] = value.split(",")
    elif isinstance(value, list | tuple | set | frozenset):
        items = value  # pyright: ignore[reportUnknownVariableType]
    else:
        return frozenset()
    return frozenset(str(v).strip() for v in items if str(v).strip())  # pyright: ignore[reportUnknownArgumentType, reportUnknownVariableType]


@dataclass(frozen=True)
class MattermostOptions:
    """``platforms.mattermost.*`` settings the adapter reads (all optional)."""

    allowed_channels: frozenset[str] = frozenset()
    reply_mode: ReplyMode = "off"
    max_media_bytes: int = DEFAULT_MAX_MEDIA_BYTES
    reactions: bool = True

    @classmethod
    def read(cls, extra: Mapping[str, Any], env: Mapping[str, str]) -> MattermostOptions:
        mode = str(extra.get("reply_mode") or "off").strip().lower()
        try:
            cap = int(extra.get("max_media_bytes") or DEFAULT_MAX_MEDIA_BYTES)
        except (TypeError, ValueError):
            cap = DEFAULT_MAX_MEDIA_BYTES
        reactions = extra.get("reactions")
        return cls(
            allowed_channels=_ids(extra.get("allowed_channels"))
            | _ids(env.get("INDUS_MATTERMOST_ALLOWED_CHANNELS", "")),
            reply_mode="thread" if mode == "thread" else "off",
            max_media_bytes=max(1, cap),
            reactions=True if reactions is None else str(reactions).lower() in _TRUTHY,
        )


def file_ref(file_id: str, name: str) -> str:
    """The attachment ref for a Mattermost file (the name keeps the intake's file type)."""
    return f"{FILE_SCHEME}{quote(file_id, safe='')}/{quote(name or file_id, safe='')}"


def parse_file_ref(ref: str) -> str | None:
    if not ref.startswith(FILE_SCHEME):
        return None
    file_id = unquote(ref[len(FILE_SCHEME) :].split("/", 1)[0])
    return file_id or None


def _outbound(text: str) -> str:
    """Image markdown → bare URL; mass mentions defused outside code."""
    pieces: list[str] = []
    last = 0
    for code in _CODE.finditer(text):
        pieces.append(_plain_part(text[last : code.start()]))
        pieces.append(code.group(0))
        last = code.end()
    pieces.append(_plain_part(text[last:]))
    return "".join(pieces)


def _plain_part(text: str) -> str:
    text = _IMAGE_MARKDOWN.sub(r"\1", text)
    return _MASS_MENTION.sub("@​\\1", text)


def _json_field(value: object) -> Any:
    """Mattermost sends some event fields as JSON text inside the JSON frame."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _handshake_status(exc: BaseException) -> int | None:
    """The HTTP status of a refused WebSocket upgrade, looking inside exception groups."""
    stack: list[BaseException] = [exc]
    while stack:
        current = stack.pop()
        if isinstance(current, BaseExceptionGroup):
            stack.extend(current.exceptions)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
            continue
        response = getattr(current, "response", None)
        status = getattr(response, "status_code", None)
        if isinstance(status, int):
            return status
    return None


class MattermostAdapter(Adapter):
    platform: ClassVar[str] = "mattermost"
    capabilities: ClassVar[Capabilities] = Capabilities(
        max_len=MAX_POST_CHARS,
        markup="markdown",
        splits_natively=True,
        supports_edit=True,
        supports_delete=True,
        supports_buttons=False,
        dm_is_private=True,
        push_capable=True,
    )
    vendor_names: ClassVar[Mapping[str, tuple[str, ...]]] = {
        # Our REST client (no SDK); its routes are ``client.ROUTES``, served by the wire fake.
        "indus.gateway.adapters.mattermost.client:MattermostClient": (
            "me",
            "create_post",
            "patch_post",
            "delete_post",
            "get_post",
            "typing",
            "channel",
            "upload",
            "download",
            "teams",
            "team_channels",
            "add_reaction",
            "remove_reaction",
        ),
        "httpx_ws:AsyncWebSocketSession": ("receive_text", "send_text", "close"),
    }

    def __init__(
        self,
        *,
        clock: Clock,
        base_url: str,
        token: str,
        settings: PlatformSettings | None = None,
        options: MattermostOptions | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        proxy: str | None = None,
        opener: SocketOpener | None = None,
        rng: Callable[[], float] = random.random,
        send_only: bool = False,
    ) -> None:
        super().__init__(clock=clock, settings=settings, secrets=(token,))
        # ``indus send`` (``AdapterConfig.send_only``): ``users/me`` only, never the socket.
        self.send_only = send_only
        self._base_url = base_url.strip().rstrip("/")
        self._token = token.strip()
        self.options = options or MattermostOptions()
        self._transport = transport
        self._proxy = proxy
        self._opener: SocketOpener = opener or httpx_ws_opener
        self._rng = rng
        self.bot_id = ""
        self.username = ""
        self.socket_opens = 0
        self._client: MattermostClient | None = None
        self._listener: asyncio.Task[None] | None = None
        self._seen: OrderedDict[str, None] = OrderedDict()

    def __repr__(self) -> str:
        state = "connected" if self.connected else "disconnected"
        return f"<MattermostAdapter {self._base_url or '?'} {state}>"

    # -- lifecycle ---------------------------------------------------------------------------------

    async def _open(self, is_reconnect: bool) -> None:
        if not self._base_url or not self._token:
            missing = [
                name
                for name, value in ((URL_ENV, self._base_url), (TOKEN_ENV, self._token))
                if not value
            ]
            raise AdapterConnectError(
                "Mattermost is not configured: set " + " and ".join(missing),
                retryable=False,
                code="config",
            )
        if not self._base_url.startswith(("https://", "http://")):
            raise AdapterConnectError(
                "MATTERMOST_URL must be an http(s) URL", retryable=False, code="config"
            )
        client = MattermostClient(
            self._base_url, self._token, transport=self._transport, proxy=self._proxy
        )
        try:
            me = await client.me()
        except MattermostError as exc:
            await client.aclose()
            if exc.is_auth:
                raise AdapterConnectError(
                    "the Mattermost server refused the token (HTTP 401)",
                    retryable=False,
                    code="auth_failed",
                ) from None
            raise AdapterConnectError(str(exc), retryable=True) from None
        except BaseException:
            await client.aclose()
            raise
        bot_id = str(me.get("id") or "")
        if not bot_id:
            await client.aclose()
            raise AdapterConnectError("the Mattermost server did not say who we are")
        self.bot_id = bot_id
        self.username = str(me.get("username") or "")
        self._client = client
        log.info("mattermost: signed in as @%s on %s", self.username or "?", self._base_url)
        if self._sink is not None:
            self._start_listening()

    async def _close(self) -> None:
        self._stop_listening()
        listener, self._listener = self._listener, None
        if listener is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await listener
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    def set_sink(self, sink: Any) -> None:
        """Installing a sink opens the WebSocket (never when send-only); removing it closes
        it."""
        super().set_sink(sink)
        if sink is None:
            self._stop_listening()
        elif self.connected and self._client is not None:
            self._start_listening()

    @property
    def listening(self) -> bool:
        return self._listener is not None and not self._listener.done()

    def _start_listening(self) -> None:
        if not self.listening and not self.send_only:
            self._listener = asyncio.ensure_future(self._listen())

    def _stop_listening(self) -> None:
        if self._listener is not None and not self._listener.done():
            self._listener.cancel()

    def is_final_failure(self, result: SendResult) -> bool:
        """A refused token: no retry and no plain-text resend can help."""
        return "unauthorized" in (result.error or "")

    def _require_client(self) -> MattermostClient:
        if self._client is None:
            raise ConnectionError("not connected to the Mattermost server")
        return self._client

    # -- inbound -----------------------------------------------------------------------------------

    async def _listen(self) -> None:
        delay = BACKOFF_FIRST_S
        while True:
            client = self._client
            if client is None:
                return
            try:
                async with self._opener(client.http, client.websocket_url()) as socket:
                    self.socket_opens += 1
                    delay = BACKOFF_FIRST_S
                    log.info("mattermost: event socket open")
                    while True:
                        await self._on_frame(await socket.receive_text())
            except asyncio.CancelledError:
                raise
            except ModuleNotFoundError:
                log.error("mattermost: the event socket needs httpx-ws: %s", INSTALL_HINT)
                self.report_fatal(
                    "missing_dependency", f"the event socket needs httpx-ws ({INSTALL_HINT})"
                )
                return
            except Exception as exc:
                status = _handshake_status(exc)
                if status in (401, 403):
                    log.error("mattermost: the server refused the event socket (HTTP %s)", status)
                    self.report_fatal(
                        "mattermost_auth",
                        f"the server refused the event socket (HTTP {status}); check "
                        "MATTERMOST_TOKEN",
                    )
                    return
                log.warning(
                    "mattermost: event socket closed (%s); reopening in %.0f s",
                    self.scrub(f"{type(exc).__name__}: {exc}"),
                    delay,
                )
            await self.clock.sleep(delay * (1 + BACKOFF_JITTER * self._rng()))
            delay = min(delay * 2, BACKOFF_CAP_S)

    async def _on_frame(self, raw: str) -> None:
        try:
            frame = json.loads(raw)
        except ValueError:
            return
        if not isinstance(frame, dict) or frame.get("event") != "posted":
            return
        event = self.normalise(frame.get("data"))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        if event is not None:
            await self.emit(event)

    def _seen_before(self, post_id: str) -> bool:
        if post_id in self._seen:
            return True
        self._seen[post_id] = None
        while len(self._seen) > SEEN_POSTS:
            self._seen.popitem(last=False)
        return False

    def mentions_me(self, text: str, mentions: object) -> bool:
        if isinstance(mentions, list) and self.bot_id in mentions:
            return True
        names = [n for n in (self.username, self.bot_id) if n]
        return any(re.search(rf"(?<![\w@])@{re.escape(n)}\b", text, re.IGNORECASE) for n in names)

    def normalise(self, data: object) -> InboundEvent | None:
        """One ``posted`` event's ``data`` → an `InboundEvent`, or None when it is dropped."""
        if not isinstance(data, Mapping):
            return None
        fields: Mapping[str, Any] = data  # pyright: ignore[reportUnknownVariableType]
        post = _json_field(fields.get("post"))
        if not isinstance(post, dict):
            return None
        post_id = str(post.get("id") or "")  # pyright: ignore[reportUnknownMemberType]
        user_id = str(post.get("user_id") or "")  # pyright: ignore[reportUnknownMemberType]
        if not post_id or not user_id or user_id == self.bot_id or post.get("type"):  # pyright: ignore[reportUnknownMemberType]
            return None
        if self._seen_before(post_id):
            return None
        channel_id = str(post.get("channel_id") or "")  # pyright: ignore[reportUnknownMemberType]
        kind_code = str(fields.get("channel_type") or "O")
        dm = kind_code == "D"
        allowed = self.options.allowed_channels
        if not dm and allowed and channel_id not in allowed:
            log.debug("mattermost: %s is not in allowed_channels; post ignored", channel_id)
            return None
        props = post.get("props")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        props = props if isinstance(props, dict) else {}
        is_bot = any(
            str(props.get(key, "")).lower() == "true"  # pyright: ignore[reportUnknownMemberType]
            for key in ("from_bot", "from_webhook")
        )
        text = str(post.get("message") or "")  # pyright: ignore[reportUnknownMemberType]
        metadata: dict[str, Any] = {}
        if self.mentions_me(text, _json_field(fields.get("mentions"))):
            metadata["mentioned"] = True
            for name in (self.username, self.bot_id):
                if name:
                    pattern = rf"(?<![\w@])@{re.escape(name)}\b[:,]?"
                    text = re.sub(pattern, "", text, flags=re.IGNORECASE)
            text = re.sub(r"[ \t]{2,}", " ", text).strip()
        if text[:1].isspace() and text.lstrip().startswith("/"):
            text = text.lstrip()
        chat_type: ChatType = _CHANNEL_TYPES.get(kind_code, "channel")
        thread_id = str(post.get("root_id") or "") or None  # pyright: ignore[reportUnknownMemberType]
        if thread_id is None and not dm and self.options.reply_mode == "thread":
            thread_id = post_id
        if thread_id is not None and not dm:
            chat_type = "thread"
        media = self._attachments(post)  # pyright: ignore[reportUnknownArgumentType]
        kind = MessageKind.TEXT
        if media:
            mime = media[0].mime
            kind = (
                MessageKind.PHOTO
                if mime.startswith("image/")
                else MessageKind.VOICE
                if mime.startswith("audio/")
                else MessageKind.VIDEO
                if mime.startswith("video/")
                else MessageKind.DOCUMENT
            )
        created = post.get("create_at")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        when = None
        if isinstance(created, int | float):
            when = datetime.fromtimestamp(created / 1000, UTC)
        sender = str(fields.get("sender_name") or "").lstrip("@")
        channel_name = str(fields.get("channel_display_name") or fields.get("channel_name") or "")
        source = Source(
            platform=self.platform,
            chat_id=channel_id,
            chat_type=chat_type,
            chat_name=channel_name or None,
            user_id=user_id,
            user_name=sender or user_id,
            thread_id=thread_id,
            is_bot=is_bot,
            scope_id=None,
            parent_chat_id=channel_id if chat_type == "thread" else None,
        )
        return InboundEvent(
            source=source,
            text=text,
            kind=kind,
            message_id=post_id,
            media=media,
            metadata=metadata,
            timestamp=when,
        )

    def _attachments(self, post: Mapping[str, Any]) -> tuple[Attachment, ...]:
        meta = post.get("metadata")
        described = meta.get("files") if isinstance(meta, Mapping) else None  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        found: dict[str, Attachment] = {}
        if isinstance(described, list):
            for info in described:  # pyright: ignore[reportUnknownVariableType]
                if not isinstance(info, Mapping):
                    continue
                file_id = str(info.get("id") or "")  # pyright: ignore[reportUnknownMemberType]
                size = info.get("size")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
                if not file_id:
                    continue
                if isinstance(size, int) and size > self.options.max_media_bytes:
                    log.warning(
                        "mattermost: file %s is too large (%d bytes); skipped", file_id, size
                    )
                    continue
                name = str(info.get("name") or file_id)  # pyright: ignore[reportUnknownMemberType]
                mime = str(info.get("mime_type") or "")  # pyright: ignore[reportUnknownMemberType]
                found[file_id] = Attachment(ref=file_ref(file_id, name), mime=mime)
        listed = post.get("file_ids")
        if isinstance(listed, list):
            for raw in listed:  # pyright: ignore[reportUnknownVariableType]
                file_id = str(raw)  # pyright: ignore[reportUnknownArgumentType]
                if file_id and file_id not in found:
                    found[file_id] = Attachment(ref=file_ref(file_id, file_id))
        return tuple(found.values())

    # -- media for the intake ----------------------------------------------------------------------

    async def fetch_media(self, attachment: Attachment) -> AsyncIterator[bytes]:
        """The bytes of an inbound attachment (registered by `register_fetchers`)."""
        file_id = parse_file_ref(attachment.ref)
        if file_id is None:
            raise ValueError("not a Mattermost file reference")
        client = self._require_client()
        async for chunk in client.download(file_id, cap=self.options.max_media_bytes):
            yield chunk

    def register_fetchers(self, seat: FetcherSeat) -> Callable[[], None] | None:
        """Register `fetch_media` with the media intake (P15-23); returns the undo."""
        return seat.register_fetcher(self.platform, self.fetch_media)

    # -- outbound ----------------------------------------------------------------------------------

    async def _root_for(self, reply_to: str | None, meta: Mapping[str, Any]) -> str | None:
        thread = str(meta.get("thread_id") or "")
        if thread:
            return thread
        if not reply_to or self.options.reply_mode != "thread":
            return None
        try:
            post = await self._require_client().get_post(reply_to)
        except MattermostError:
            return reply_to
        # A reply's own id is not a valid root: thread under its root.
        return str(post.get("root_id") or "") or reply_to

    async def _send(
        self, chat_id: str, text: str, reply_to: str | None, meta: dict[str, Any]
    ) -> SendResult:
        client = self._require_client()
        root = await self._root_for(reply_to, meta)
        chunked = split_message(_outbound(text), MAX_POST_CHARS)
        sent: list[str] = []
        for n, chunk in enumerate(chunked.chunks):
            if n:
                await self.slots.pace(chat_id)
            try:
                post = await client.create_post(chat_id, chunk.text, root_id=root)
            except Exception as exc:
                remainder = None if is_ambiguous_timeout(exc) else chunked.remainder(n)
                return SendResult.failure(
                    exc=exc,
                    retry_after=getattr(exc, "retry_after", None),
                    delivered_ids=tuple(sent),
                    remainder=remainder if sent else None,
                )
            post_id = str(post.get("id") or "")
            if not post_id:
                return SendResult.failure(
                    "unexpected answer from the server: no post id", delivered_ids=tuple(sent)
                )
            sent.append(post_id)
        return SendResult.sent(sent[-1], tuple(sent[:-1]))

    async def _edit(
        self, chat_id: str, message_id: str, text: str, final: bool, meta: dict[str, Any]
    ) -> SendResult:
        body = _outbound(text)
        if len(body) > MAX_POST_CHARS:
            return SendResult.failure("message is too long for one Mattermost edit")
        try:
            await self._require_client().patch_post(message_id, body)
        except Exception as exc:
            return SendResult.failure(exc=exc, retry_after=getattr(exc, "retry_after", None))
        return SendResult.sent(message_id)

    async def _delete(self, chat_id: str, message_id: str) -> bool:
        await self._require_client().delete_post(message_id)
        return True

    async def _typing(self, chat_id: str, meta: dict[str, Any]) -> None:
        parent = str(meta.get("thread_id") or "")
        await self._require_client().typing(self.bot_id, chat_id, parent)

    async def _send_media(
        self, kind: MediaKind, chat_id: str, path: str, caption: str | None, meta: dict[str, Any]
    ) -> SendResult:
        client = self._require_client()
        file = Path(path)
        size = (await run_blocking(file.stat, label="mattermost upload")).st_size
        if size > self.options.max_media_bytes:
            return SendResult.failure(
                f"the file is too large for Mattermost ({size} bytes, the cap is "
                f"{self.options.max_media_bytes})"
            )
        data = await run_blocking(file.read_bytes, label="mattermost upload")
        mime = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        reply_to = meta.get(REPLY_TO_KEY)
        try:
            file_id = await client.upload(chat_id, data, name=file.name, mime=mime)
            root = await self._root_for(str(reply_to) if reply_to else None, meta)
            post = await client.create_post(
                chat_id, _outbound(caption or ""), root_id=root, file_ids=[file_id]
            )
        except Exception as exc:
            return SendResult.failure(exc=exc, retry_after=getattr(exc, "retry_after", None))
        return SendResult.sent(str(post.get("id") or ""))

    async def _chat_info(self, chat_id: str) -> ChatInfo:
        channel = await self._require_client().channel(chat_id)
        name = str(channel.get("display_name") or channel.get("name") or chat_id)
        return ChatInfo(name=name, type=_CHANNEL_TYPES.get(str(channel.get("type") or "O")))

    async def list_channels(self) -> list[ChannelEntry] | None:
        client = self._client
        if client is None:
            return None
        try:
            teams = await client.teams()
            entries: dict[str, ChannelEntry] = {}
            for team in teams:
                team_id = str(team.get("id") or "")
                if not team_id:
                    continue
                team_name = str(team.get("display_name") or team.get("name") or "")
                for channel in await client.team_channels(team_id):
                    channel_id = str(channel.get("id") or "")
                    if not channel_id or channel_id in entries:
                        continue
                    entries[channel_id] = ChannelEntry(
                        id=channel_id,
                        name=str(channel.get("display_name") or channel.get("name") or channel_id),
                        type=_CHANNEL_TYPES.get(str(channel.get("type") or "O"), "channel"),
                        guild=team_name,
                    )
        except Exception as exc:
            log.warning("mattermost: listing channels failed: %s", self.scrub(str(exc)))
            return None
        return list(entries.values())

    # -- reactions (processing state) --------------------------------------------------------------

    async def react(self, message_id: str, emoji: str, *, remove: bool = False) -> bool:
        try:
            client = self._require_client()
            if remove:
                await client.remove_reaction(self.bot_id, message_id, emoji)
            else:
                await client.add_reaction(self.bot_id, message_id, emoji)
        except Exception as exc:
            log.debug("mattermost: reaction failed: %s", self.scrub(str(exc)))
            return False
        return True

    async def processing_started(self, chat_id: str, message_id: str) -> None:
        if self.options.reactions:
            await self.react(message_id, "eyes")

    async def processing_finished(self, chat_id: str, message_id: str, *, ok: bool) -> None:
        if not self.options.reactions:
            return
        await self.react(message_id, "eyes", remove=True)
        await self.react(message_id, "white_check_mark" if ok else "x")
