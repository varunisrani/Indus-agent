"""SimpleX Chat through a user-run ``simplex-chat`` CLI daemon (P16-28).

The user installs the SimpleX Chat CLI (AGPL-3.0: never bundled, imported or started by us) and
runs it with its WebSocket API on loopback (``simplex-chat -p 5225``). This adapter keeps one
WebSocket to it (httpx-ws, the ``simplex`` extra, imported only when the socket opens):

- frames out are ``{"corrId": …, "cmd": …}``; answers carry the same ``corrId``, events none.
  Every command waits for its answer (on the injected clock), so sends report the item ids
  SimpleX assigned and its errors; a write that fails sent nothing (transient), no answer in 30 s
  means the daemon may have sent it (never retried);
- the socket opens with a ``/user`` probe; a dropped socket is reopened after 2 s, doubling to
  60 s (plus up to 20 % jitter); the wait starts over once a socket passed its probe. Every 30 s a
  health check looks at the last frame: after 300 s of silence it probes again and reopens the
  socket when no answer comes within 10 s;
- received messages in direct chats (chat id = the numeric contact id) and in allowed groups (chat
  id ``group:<groupId>``; ``group_allowed_chats``, ``*`` for any) become events; our own items,
  other item kinds and groups not listed are dropped; edits of received items become edited
  events; a quote gives the reply id;
- contact requests are accepted only with ``auto_accept: true`` (an accepted contact still meets
  the gateway's allowlist or pairing for every message); otherwise the request is logged with the
  command that accepts it;
- files: inbound files are fetched lazily for admitted messages through the media intake
  (``/freceive``, then the path the daemon reports, read only inside ``files_folder``); outbound
  files are passed as paths the daemon reads (it runs on this machine);
- the daemon must be on loopback unless ``allow_remote: true`` (the API has no authentication);
- send-only (``indus send``): opens the socket and probes, handles no events and starts no health
  check.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import mimetypes
import os
import random
import re
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Protocol
from urllib.parse import urlparse

from indus.foundation.clock import Clock
from indus.foundation.digits import is_digits
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
    "COMMAND_TIMEOUT",
    "DEFAULT_URL",
    "FILE_REF",
    "GROUP_PREFIX",
    "HEALTH_EVERY",
    "INSTALL_HINT",
    "MAX_TEXT",
    "RETRY_FIRST",
    "RETRY_MAX",
    "STALE_AFTER",
    "URL_ENV",
    "SimplexAdapter",
    "SimplexOptions",
    "SocketOpener",
    "WebSocketLike",
    "httpx_ws_opener",
]

log = logging.getLogger(__name__)

URL_ENV = "SIMPLEX_WS_URL"
DEFAULT_URL = "ws://127.0.0.1:5225"
INSTALL_HINT = "pip install 'indus-agent[simplex]'"
MAX_TEXT = 8000
RETRY_FIRST = 2.0
RETRY_MAX = 60.0
JITTER = 0.2
HEALTH_EVERY = 30.0
STALE_AFTER = 300.0
PROBE_TIMEOUT = 10.0
COMMAND_TIMEOUT = 30.0
FILE_WAIT = 120.0
MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_FRAME_BYTES = 8 * 1024 * 1024
GROUP_PREFIX = "group:"
FILE_REF = "simplex-file:"
CORR_PREFIX = "indus-"

_RETRY_AFTER = re.compile(r"retry after (\d+(?:\.\d+)?)", re.IGNORECASE)
_OWN_DIRECTIONS = ("directSnd", "groupSnd", "localSnd")
_NOT_FOUND = ("contactnotfound", "groupnotfound", "chatnotfound", "chatitemnotfound")
_KINDS: Mapping[str, MessageKind] = {
    "image": MessageKind.PHOTO,
    "voice": MessageKind.VOICE,
    "video": MessageKind.VIDEO,
    "file": MessageKind.DOCUMENT,
}


class WebSocketLike(Protocol):
    """What the adapter needs from a WebSocket session (httpx-ws's ``AsyncWebSocketSession``)."""

    async def receive_text(self) -> str: ...

    async def send_text(self, data: str) -> None: ...


type SocketOpener = Callable[[str], contextlib.AbstractAsyncContextManager[WebSocketLike]]


class _MissingDependency(Exception):
    pass


class _NotSent(Exception):
    """The command never left: the socket was missing or the write failed."""


class _NoAnswer(Exception):
    """The command left but no answer came in time (the daemon may have run it)."""


@contextlib.asynccontextmanager
async def _httpx_ws_session(url: str) -> AsyncIterator[WebSocketLike]:
    try:
        import httpx_ws  # pyright: ignore[reportMissingImports]
    except ModuleNotFoundError:
        raise _MissingDependency(f"the SimpleX adapter needs httpx-ws: {INSTALL_HINT}") from None
    import httpx

    http_url = "http" + url[2:] if url.startswith("ws") else url
    async with (
        httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(30.0, connect=10.0)) as client,
        httpx_ws.aconnect_ws(  # pyright: ignore[reportUnknownMemberType]
            http_url, client, max_message_size_bytes=MAX_FRAME_BYTES
        ) as socket,
    ):
        yield socket  # pyright: ignore[reportUnknownArgumentType]


def httpx_ws_opener(url: str) -> contextlib.AbstractAsyncContextManager[WebSocketLike]:
    """The production opener: an httpx-ws session to the daemon (no proxy, no env)."""
    return _httpx_ws_session(url)


def _as_dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}  # pyright: ignore[reportUnknownArgumentType]


def _as_list(value: object) -> list[Any]:
    return list(value) if isinstance(value, list) else []  # pyright: ignore[reportUnknownArgumentType]


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _ident(value: object) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return _text(value)


def _truthy(value: object, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _when(value: object) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _error_text(answer: Mapping[str, Any]) -> str:
    """A classifier-readable text for a ``chatCmdError`` / ``chatError`` answer."""
    names: list[str] = []
    details: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, Mapping):
            for key, value in _as_dict(node).items():
                if key == "type" and isinstance(value, str):
                    names.append(value)
                elif key in ("message", "errorMessage") and isinstance(value, str):
                    details.append(value)
                else:
                    walk(value)

    walk(answer.get("chatError") or answer)
    joined = " ".join(names[1:] or names)
    detail = "; ".join(details)
    text = f"SimpleX error {joined}".strip() + (f": {detail}" if detail else "")
    if any(n.lower() in _NOT_FOUND for n in names):
        text = f"chat not found: {text}"
    return text


@dataclass(frozen=True)
class SimplexOptions:
    group_chats: frozenset[str] = frozenset()
    auto_accept: bool = False
    allow_remote: bool = False
    files_folder: Path | None = None

    @classmethod
    def read(cls, extra: Mapping[str, Any], env: Mapping[str, str]) -> SimplexOptions:
        from indus.gateway.core.authz import allow_set

        groups = allow_set(extra.get("group_allowed_chats")) | allow_set(
            env.get("INDUS_SIMPLEX_GROUP_ALLOWED_CHATS")
        )
        folder = _text(extra.get("files_folder")) or _text(env.get("SIMPLEX_FILES_FOLDER"))
        auto = extra.get("auto_accept")
        if auto is None:
            auto = env.get("SIMPLEX_AUTO_ACCEPT")
        return cls(
            group_chats=frozenset(groups),
            auto_accept=_truthy(auto, False),
            allow_remote=_truthy(extra.get("allow_remote"), False),
            files_folder=Path(folder).expanduser() if folder else None,
        )


class SimplexAdapter(Adapter):
    platform: ClassVar[str] = "simplex"
    capabilities: ClassVar[Capabilities] = Capabilities(
        max_len=MAX_TEXT,
        markup="markdown",
        splits_natively=True,
    )
    vendor_names: ClassVar[Mapping[str, tuple[str, ...]]] = {
        "httpx_ws:AsyncWebSocketSession": ("receive_text", "send_text", "close"),
    }

    def __init__(
        self,
        *,
        clock: Clock,
        ws_url: str = DEFAULT_URL,
        settings: PlatformSettings | None = None,
        options: SimplexOptions | None = None,
        opener: SocketOpener | None = None,
        rng: Callable[[], float] = random.random,
        send_only: bool = False,
    ) -> None:
        super().__init__(clock=clock, settings=settings)
        self.ws_url = (ws_url or "").strip().rstrip("/")
        self.options = options or SimplexOptions()
        self.send_only = send_only
        self._opener: SocketOpener = opener or httpx_ws_opener
        self._rng = rng
        self._running = False
        self._socket: WebSocketLike | None = None
        self._runner: asyncio.Task[None] | None = None
        self._reader: asyncio.Task[None] | None = None
        self._health: asyncio.Task[None] | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._corr = 0
        self._last_frame = 0.0
        self._file_paths: dict[str, str] = {}
        self._file_waits: dict[str, asyncio.Future[str]] = {}
        self._told_requests: set[str] = set()
        self._names: dict[str, str] = {}
        self.user_name = ""

    def __repr__(self) -> str:
        state = "connected" if self.connected else "disconnected"
        return f"<SimplexAdapter {self.ws_url or '?'} {state}>"

    # -- lifecycle ---------------------------------------------------------------------------------

    def _check_url(self) -> None:
        parsed = urlparse(self.ws_url)
        if parsed.scheme not in ("ws", "wss") or not parsed.hostname:
            raise AdapterConnectError(
                f"{URL_ENV} must be a ws:// or wss:// address (the daemon's WebSocket port)",
                retryable=False,
                code="config",
            )
        if not is_loopback(parsed.hostname) and not self.options.allow_remote:
            raise AdapterConnectError(
                f"{URL_ENV} points at {parsed.hostname}, not this machine; the SimpleX API has "
                "no authentication, so a remote daemon needs platforms.simplex.allow_remote: true",
                retryable=False,
                code="config",
            )

    async def _open(self, is_reconnect: bool) -> None:
        self._check_url()
        self._running = True
        first: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._runner = asyncio.ensure_future(self._run(first))
        try:
            await first
        except _MissingDependency as exc:
            await self._stop_tasks()
            raise AdapterConnectError(
                str(exc), retryable=False, code="missing_dependency"
            ) from None
        except BaseException as exc:
            await self._stop_tasks()
            if isinstance(exc, AdapterConnectError):
                raise
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise AdapterConnectError(
                f"cannot reach the SimpleX daemon at {self.ws_url}: {type(exc).__name__}",
                retryable=True,
            ) from None
        if not self.send_only:
            self._health = asyncio.ensure_future(self._watch())
        log.info("simplex: connected to %s as %s", self.ws_url, self.user_name or "?")

    async def _close(self) -> None:
        await self._stop_tasks()

    async def _stop_tasks(self) -> None:
        self._running = False
        for task in (self._health, self._reader, self._runner):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._health = self._reader = self._runner = None
        self._socket = None
        self._fail_pending(ConnectionError("the SimpleX socket closed"))

    def _fail_pending(self, exc: BaseException) -> None:
        for future in [*self._pending.values(), *self._file_waits.values()]:
            if not future.done():
                future.set_exception(exc)
                future.exception()  # retrieved: nobody may be waiting any more

    async def _run(self, first: asyncio.Future[None]) -> None:
        """Open, probe and read the socket; reopen with backoff while running."""
        delay = RETRY_FIRST
        while self._running:
            try:
                async with self._opener(self.ws_url) as socket:
                    self._socket = socket
                    self._last_frame = self.clock.monotonic()
                    reader = self._reader = asyncio.ensure_future(self._read(socket))
                    try:
                        await self._probe()
                        if not first.done():
                            first.set_result(None)
                        delay = RETRY_FIRST
                        await asyncio.wait({reader})
                        if reader.cancelled():
                            raise ConnectionError("the health probe went unanswered")
                        failure = reader.exception()
                        if failure is not None:
                            raise failure
                    finally:
                        self._socket = None
                        if not reader.done():
                            reader.cancel()
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await reader
                        self._fail_pending(ConnectionError("the SimpleX socket closed"))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not first.done():
                    first.set_exception(exc)
                    return
                log.warning(
                    "simplex: socket lost (%s); reopening in %.0f s", type(exc).__name__, delay
                )
            else:
                if not first.done():
                    first.set_exception(ConnectionError("the daemon closed the socket"))
                    return
                log.warning("simplex: the daemon closed the socket; reopening in %.0f s", delay)
            if not self._running:
                return
            await self.clock.sleep(delay * (1 + JITTER * self._rng()))
            delay = min(delay * 2, RETRY_MAX)

    async def _probe(self) -> None:
        answer = await self._command("/user", timeout=PROBE_TIMEOUT)
        if _text(answer.get("type")) != "activeUser":
            raise ConnectionError(f"unexpected answer to /user: {_text(answer.get('type'))}")
        user = _as_dict(answer.get("user"))
        profile = _as_dict(user.get("profile"))
        self.user_name = _text(user.get("localDisplayName")) or _text(profile.get("displayName"))

    async def _read(self, socket: WebSocketLike) -> None:
        while True:
            raw = await socket.receive_text()
            self._last_frame = self.clock.monotonic()
            try:
                await self._on_frame(raw)
            except Exception:
                log.exception("simplex: an event could not be handled")

    async def _watch(self) -> None:
        """Health check: probe a socket that has been silent too long; reopen it if mute."""
        while self._running:
            await self.clock.sleep(HEALTH_EVERY)
            if self._socket is None:
                continue
            if self.clock.monotonic() - self._last_frame <= STALE_AFTER:
                continue
            try:
                await self._probe()
            except Exception:
                log.warning("simplex: the daemon did not answer a health probe; reopening")
                reader = self._reader
                if reader is not None and not reader.done():
                    reader.cancel()

    # -- commands ----------------------------------------------------------------------------------

    def _next_corr(self) -> str:
        self._corr += 1
        return f"{CORR_PREFIX}{self._corr}"

    async def _within[T](self, future: asyncio.Future[T], seconds: float) -> T:
        timer = asyncio.ensure_future(self.clock.sleep(seconds))
        try:
            await asyncio.wait({future, timer}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            timer.cancel()
        if future.done():
            return future.result()
        raise _NoAnswer()

    async def _command(self, command: str, *, timeout: float = COMMAND_TIMEOUT) -> dict[str, Any]:
        """Send one command and wait for its answer (``_NotSent`` / ``_NoAnswer`` otherwise)."""
        socket = self._socket
        if socket is None:
            raise _NotSent("the SimpleX daemon is not connected")
        corr = self._next_corr()
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[corr] = future
        try:
            try:
                await socket.send_text(json.dumps({"corrId": corr, "cmd": command}))
            except Exception as exc:
                raise _NotSent(f"the command could not be written: {type(exc).__name__}") from None
            return await self._within(future, timeout)
        finally:
            self._pending.pop(corr, None)

    # -- inbound -----------------------------------------------------------------------------------

    async def _on_frame(self, raw: str) -> None:
        try:
            frame = json.loads(raw)
        except ValueError:
            return
        if not isinstance(frame, Mapping):
            return
        body = _as_dict(frame)
        answer = _as_dict(body.get("resp")) if isinstance(body.get("resp"), Mapping) else body
        corr = body.get("corrId")
        if isinstance(corr, str) and corr:
            future = self._pending.get(corr)
            if future is not None and not future.done():
                future.set_result(answer)
            return  # an answer to one of our commands (or a late one), never an event
        kind = _text(answer.get("type"))
        if kind == "rcvFileComplete":
            self._file_done(answer)
            return
        if kind in ("rcvFileError", "rcvFileSndCancelled", "rcvFileCancelled"):
            self._file_failed(answer)
            return
        if self.send_only:
            return
        if kind in ("newChatItems", "newChatItem"):
            rows = _as_list(answer.get("chatItems")) or [answer]
            for row in rows:
                event = self.normalise(_as_dict(row))
                if event is not None:
                    await self.emit(event)
        elif kind == "chatItemUpdated":
            event = self.normalise(_as_dict(answer.get("chatItem")), edited=True)
            if event is not None:
                await self.emit(event)
        elif kind in ("contactRequest", "receivedContactRequest"):
            await self._contact_request(_as_dict(answer.get("contactRequest")))

    def _group_allowed(self, group_id: str) -> bool:
        groups = self.options.group_chats
        return "*" in groups or group_id in groups or f"{GROUP_PREFIX}{group_id}" in groups

    def normalise(self, row: Mapping[str, Any], *, edited: bool = False) -> InboundEvent | None:
        """The event for one ``{chatInfo, chatItem}`` row, or None when it is not ours to answer."""
        info = _as_dict(row.get("chatInfo"))
        item = _as_dict(row.get("chatItem"))
        direction = _as_dict(item.get("chatDir"))
        if _text(direction.get("type")) in _OWN_DIRECTIONS:
            return None
        content = _as_dict(item.get("content"))
        if _text(content.get("type")) != "rcvMsgContent":
            return None
        message = _as_dict(content.get("msgContent"))
        meta = _as_dict(item.get("meta"))
        chat_kind = _text(info.get("type"))
        if chat_kind == "direct":
            contact = _as_dict(info.get("contact"))
            chat_id = user_id = _ident(contact.get("contactId"))
            profile = _as_dict(contact.get("profile"))
            name = _text(contact.get("localDisplayName")) or _text(profile.get("displayName"))
            chat_name = name
            chat_type = "dm"
        elif chat_kind == "group":
            group = _as_dict(info.get("groupInfo"))
            group_id = _ident(group.get("groupId"))
            if not group_id or not self._group_allowed(group_id):
                return None
            member = _as_dict(direction.get("groupMember"))
            profile = _as_dict(member.get("memberProfile"))
            chat_id = f"{GROUP_PREFIX}{group_id}"
            user_id = _ident(member.get("memberId"))
            name = _text(member.get("localDisplayName")) or _text(profile.get("displayName"))
            chat_name = _text(group.get("localDisplayName")) or chat_id
            chat_type = "group"
        else:
            return None
        if not chat_id or not user_id:
            return None
        self._names[chat_id] = chat_name or chat_id
        text = _text(message.get("text"))
        media, kind = self._file(item, _text(message.get("type")))
        if not text and not media:
            return None
        metadata: dict[str, Any] = {}
        if meta.get("userMention") is True:
            metadata["mentioned"] = True
        if edited:
            metadata["edited"] = True
        quote = _as_dict(item.get("quotedItem"))
        quote_dir = _text(_as_dict(quote.get("chatDir")).get("type"))
        quoted = _ident(quote.get("itemId")) or None
        source = Source(
            platform=self.platform,
            chat_id=chat_id,
            chat_type=chat_type,
            chat_name=chat_name or None,
            user_id=user_id,
            user_name=name or None,
            is_bot=_text(profile.get("peerType")) == "bot",
        )
        return InboundEvent(
            source=source,
            text=text,
            kind=kind,
            message_id=_ident(meta.get("itemId")) or None,
            media=tuple(media),
            reply_to_id=quoted,
            reply_to_is_own=quoted is not None and quote_dir in _OWN_DIRECTIONS,
            reply_to_text=_text(_as_dict(quote.get("content")).get("text")) or None,
            metadata=metadata,
            timestamp=_when(meta.get("itemTs")) or _when(meta.get("createdAt")),
        )

    def _file(
        self, item: Mapping[str, Any], content_type: str
    ) -> tuple[list[Attachment], MessageKind]:
        data = _as_dict(item.get("file"))
        file_id = _ident(data.get("fileId"))
        if not file_id:
            return [], MessageKind.TEXT
        name = _text(data.get("fileName"))
        path = _text(_as_dict(data.get("fileSource")).get("filePath"))
        if path:
            self._file_paths[file_id] = path
        mime = mimetypes.guess_type(name)[0] or ""
        kind = _KINDS.get(content_type, MessageKind.DOCUMENT)
        return [Attachment(ref=f"{FILE_REF}{file_id}", mime=mime)], kind

    async def _contact_request(self, request: Mapping[str, Any]) -> None:
        ident = _ident(request.get("contactRequestId"))
        if not ident:
            return
        name = _text(request.get("localDisplayName")) or "someone"
        if not self.options.auto_accept:
            if ident not in self._told_requests:
                self._told_requests.add(ident)
                log.info(
                    "simplex: contact request %s from %s not accepted (accept it in the CLI with "
                    "/_accept %s, or set platforms.simplex.auto_accept: true)",
                    ident,
                    name,
                    ident,
                )
            return
        try:
            answer = await self._command(f"/_accept {ident}")
        except (_NotSent, _NoAnswer, ConnectionError) as exc:
            log.warning("simplex: accepting contact request %s failed: %s", ident, exc)
            return
        if "error" in _text(answer.get("type")).lower():
            log.warning(
                "simplex: accepting contact request %s failed: %s", ident, _error_text(answer)
            )
            return
        log.info("simplex: accepted contact request %s from %s", ident, name)

    # -- inbound files -----------------------------------------------------------------------------

    def _file_id_of(self, answer: Mapping[str, Any]) -> tuple[str, str]:
        row = _as_dict(answer.get("chatItem"))
        item = _as_dict(row.get("chatItem"))
        data = _as_dict(item.get("file"))
        transfer = _as_dict(answer.get("rcvFileTransfer"))
        file_id = _ident(data.get("fileId")) or _ident(transfer.get("fileId"))
        path = _text(_as_dict(data.get("fileSource")).get("filePath"))
        return file_id, path

    def _file_done(self, answer: Mapping[str, Any]) -> None:
        file_id, path = self._file_id_of(answer)
        if not file_id or not path:
            return
        self._file_paths[file_id] = path
        waiter = self._file_waits.pop(file_id, None)
        if waiter is not None and not waiter.done():
            waiter.set_result(path)

    def _file_failed(self, answer: Mapping[str, Any]) -> None:
        file_id, _ = self._file_id_of(answer)
        waiter = self._file_waits.pop(file_id, None)
        if waiter is not None and not waiter.done():
            waiter.set_exception(MediaRejected("the file transfer failed"))

    def register_fetchers(self, seat: FetcherSeat) -> Callable[[], None] | None:
        """Register `fetch_file` with the media intake (P15-23); returns the undo."""
        return seat.register_fetcher(self.platform, self.fetch_file)

    async def _file_path(self, file_id: str) -> str:
        known = self._file_paths.get(file_id)
        if known:
            return known
        waiter = self._file_waits.get(file_id)
        if waiter is None:
            waiter = asyncio.get_running_loop().create_future()
            self._file_waits[file_id] = waiter
            try:
                answer = await self._command(f"/freceive {file_id}")
            except (_NotSent, _NoAnswer, ConnectionError):
                self._file_waits.pop(file_id, None)
                raise MediaRejected("the SimpleX daemon did not accept the transfer") from None
            if "error" in _text(answer.get("type")).lower():
                self._file_waits.pop(file_id, None)
                raise MediaRejected("the SimpleX daemon refused the transfer")
        try:
            return await self._within(waiter, FILE_WAIT)
        except (_NoAnswer, ConnectionError):
            self._file_waits.pop(file_id, None)
            raise MediaRejected("the file did not arrive in time") from None

    async def fetch_file(self, attachment: Attachment) -> AsyncIterator[bytes]:
        """One inbound file's bytes, read from the daemon's files folder (≤100 MiB)."""
        if not attachment.ref.startswith(FILE_REF):
            raise MediaRejected("it is not a SimpleX file")
        file_id = attachment.ref[len(FILE_REF) :]
        if not is_digits(file_id):
            raise MediaRejected("it is not a SimpleX file")
        folder = self.options.files_folder
        if folder is None:
            raise MediaRejected(
                "set platforms.simplex.files_folder to the folder the SimpleX CLI saves files in"
            )
        path = Path(await self._file_path(file_id))
        try:
            real = path.resolve(strict=True)
            real.relative_to(folder.resolve(strict=True))
        except (OSError, ValueError):
            raise MediaRejected("the file is not in the SimpleX files folder") from None
        if not real.is_file():
            raise MediaRejected("the file is not in the SimpleX files folder")
        if real.stat().st_size > MAX_FILE_BYTES:
            raise MediaRejected("it is larger than 100 MB")
        data = await asyncio.to_thread(real.read_bytes)
        if len(data) > MAX_FILE_BYTES:
            raise MediaRejected("it is larger than 100 MB")
        yield data

    # -- outbound ----------------------------------------------------------------------------------

    @staticmethod
    def _target(chat_id: str) -> str | None:
        text = chat_id.strip()
        if text.startswith(GROUP_PREFIX):
            group = text[len(GROUP_PREFIX) :]
            return f"#{group}" if is_digits(group) else None
        return f"@{text}" if is_digits(text) else None

    async def _compose(self, target: str, items: Sequence[Mapping[str, Any]]) -> SendResult:
        command = f"/_send {target} json {json.dumps(list(items), ensure_ascii=False)}"
        try:
            answer = await self._command(command)
        except _NotSent as exc:
            return SendResult.failure(f"connect error: {exc}; nothing was sent")
        except _NoAnswer:
            return SendResult.failure(
                "read timeout: the SimpleX daemon did not answer; it may have sent the message"
            )
        except ConnectionError:
            return SendResult.failure(
                "read timeout: the socket closed before the answer; the message may have gone"
            )
        kind = _text(answer.get("type"))
        if kind in ("newChatItems", "newChatItem"):
            rows = _as_list(answer.get("chatItems")) or [answer]
            ids = [
                _ident(_as_dict(_as_dict(_as_dict(r).get("chatItem")).get("meta")).get("itemId"))
                for r in rows
            ]
            ids = [i for i in ids if i]
            if not ids:
                return SendResult.failure("SimpleX answered without an item id")
            return SendResult.sent(ids[-1], tuple(ids[:-1]))
        text = _error_text(answer) if "error" in kind.lower() else f"unexpected answer {kind}"
        found = _RETRY_AFTER.search(text)
        return SendResult.failure(text, retry_after=float(found.group(1)) if found else None)

    async def _send(
        self, chat_id: str, text: str, reply_to: str | None, meta: dict[str, Any]
    ) -> SendResult:
        target = self._target(chat_id)
        if target is None:
            return SendResult.failure(
                f"chat not found: SimpleX chats are numeric contact ids or {GROUP_PREFIX}<id>"
            )
        chunked = split_message(text, MAX_TEXT)
        ids: list[str] = []
        for index, chunk in enumerate(chunked.chunks):
            if index:
                await self.slots.pace(chat_id)
            composed: dict[str, Any] = {"msgContent": {"type": "text", "text": chunk.text}}
            if index == 0 and reply_to and is_digits(reply_to):
                composed["quotedItemId"] = int(reply_to)
            result = await self._compose(target, [composed])
            if not result.ok:
                if not ids:
                    return result
                return replace(
                    result,
                    message_id=ids[-1],
                    continuation_ids=tuple(ids[:-1]),
                    partial=True,
                    remainder=None if result.maybe_delivered else chunked.remainder(index),
                )
            ids.extend(result.ids)
        return SendResult.sent(ids[-1], tuple(ids[:-1]))

    async def _send_media(
        self, kind: MediaKind, chat_id: str, path: str, caption: str | None, meta: dict[str, Any]
    ) -> SendResult:
        target = self._target(chat_id)
        if target is None:
            return SendResult.failure("chat not found: not a SimpleX chat id")
        full = os.path.abspath(path)
        if not os.path.isfile(full):
            return SendResult(ok=False, error="the file to send was not found")
        content: dict[str, Any] = {"type": "file", "text": caption or ""}
        if kind == "voice":
            content = {"type": "voice", "text": caption or "", "duration": 0}
        composed: dict[str, Any] = {"fileSource": {"filePath": full}, "msgContent": content}
        reply = meta.get(REPLY_TO_KEY)
        if is_digits(reply):
            composed["quotedItemId"] = int(reply)
        return await self._compose(target, [composed])

    # -- descriptive -------------------------------------------------------------------------------

    async def _chat_info(self, chat_id: str) -> ChatInfo:
        group = chat_id.startswith(GROUP_PREFIX)
        return ChatInfo(name=self._names.get(chat_id, chat_id), type="group" if group else "dm")

    async def list_channels(self) -> Sequence[ChannelEntry] | None:
        try:
            contacts = await self._command("/contacts", timeout=PROBE_TIMEOUT)
            groups = await self._command("/groups", timeout=PROBE_TIMEOUT)
        except (_NotSent, _NoAnswer, ConnectionError):
            return None
        return [*self._contact_entries(contacts), *self._group_entries(groups)]

    @staticmethod
    def _contact_entries(answer: Mapping[str, Any]) -> Iterable[ChannelEntry]:
        for raw in _as_list(answer.get("contacts")):
            contact = _as_dict(raw)
            ident = _ident(contact.get("contactId"))
            if ident:
                name = _text(contact.get("localDisplayName")) or ident
                yield ChannelEntry(id=ident, name=name, type="dm")

    def _group_entries(self, answer: Mapping[str, Any]) -> Iterable[ChannelEntry]:
        for raw in _as_list(answer.get("groups")):
            group = _as_dict(raw[0] if isinstance(raw, list) and raw else raw)
            ident = _ident(group.get("groupId"))
            if ident and self._group_allowed(ident):
                name = _text(group.get("localDisplayName")) or ident
                yield ChannelEntry(id=f"{GROUP_PREFIX}{ident}", name=name, type="group")
