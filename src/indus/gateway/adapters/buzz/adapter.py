"""Buzz: a Nostr community relay (Block's human+agent platform) for the gateway (P16-29).

The bot is a Nostr identity (``BUZZ_PRIVATE_KEY``, ``nsec`` or hex, from the secret store) that is a
member of a Buzz community relay (``BUZZ_RELAY_URL``). We speak Nostr to the relay ourselves over
one WebSocket (httpx-ws) and sign with coincurve (both in the ``buzz`` extra, imported lazily):

- the relay may send a NIP-42 ``AUTH`` challenge at any time; it is answered with a signed
  kind-22242 event, and a request the relay refused with ``auth-required:`` is retried once
  authenticated. An ``AUTH`` the relay rejects means the key is not a member: fatal (``buzz_auth``);
- polling: every ``poll_interval`` seconds (4 by default, never under 1) each watched channel is
  asked for kind-9 chat events (NIP-29: scoped by the channel's ``h`` tag) since its high-water
  mark, at most 50 per request. At connect every channel is seeded from its newest events, so
  history is never answered. Each channel keeps its last 500 event ids (dedupe) and their authors
  (to tell a reply to us);
- every event is checked before use: its id must hash its contents and its BIP-340 signature must
  verify against its pubkey (`crypto.verify_event`); anything else is dropped. Other kinds are
  ignored, as are our own events;
- every 5th sweep discovers conversations: groups that list us as a member (kind 39002) and
  membership events naming us (kind 44100). A new conversation whose metadata (kind 39000) is named
  ``DM`` with no description is watched as a DM from its start; a new named channel is watched
  (seeded) when no channel list is configured;
- channel messages carry ``mentioned`` when they p-tag us or name us (npub, hex or ``@name``); a
  leading mention is stripped so commands work. NIP-10 thread roots become the thread id; a
  top-level channel message offers its own id as the prospective thread, so the answer opens a
  thread under it (``reply_in_thread: false`` posts flat);
- sends publish signed kind-9 events with the ``h`` tag, NIP-10 ``e`` tags and ``p`` tags for
  ``@Name`` mentions resolved against the channel's member list (cached 60 s; names 5 min); the
  relay's ``OK`` is the result. A target that is a pubkey or npub goes to the DM conversation that
  has exactly that member and us;
- inbound files (NIP-94 ``imeta``) are fetched lazily by the media intake for admitted messages:
  HTTPS from the relay's origin or a listed ``attachment_hosts`` origin, no redirects, size and
  SHA-256 as declared, at most 4 per message and 20 MiB in all;
- allowlists (``INDUS_BUZZ_ALLOWED_USERS``, ``allow_from``) may list npubs: the hex pubkeys events
  carry are authorised through `resolved_allowlist_ids`, and every event names its sender's npub
  as ``user_id_alt``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import random
import re
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, ClassVar, Protocol
from urllib.parse import urlsplit

import httpx

from indus.foundation.clock import Clock
from indus.foundation.digits import is_digits
from indus.gateway.adapters._identity import expand_nostr_allowlist, hex_to_npub, nostr_pubkey
from indus.gateway.adapters.base import (
    Adapter,
    AdapterConnectError,
    Capabilities,
    ChatInfo,
    FetcherSeat,
)
from indus.gateway.adapters.buzz.crypto import (
    INSTALL_HINT,
    MissingCrypto,
    NostrCrypto,
    NostrKeys,
    load_crypto,
    signed_event,
    verify_event,
)
from indus.gateway.config import PlatformSettings
from indus.gateway.core.events import Attachment, InboundEvent, MessageKind, Source
from indus.gateway.core.send import SendResult
from indus.gateway.http.serving import is_loopback
from indus.gateway.intake.media import MediaRejected
from indus.gateway.outbound.chunker import split_message

__all__ = [
    "ALLOWLIST_ENV",
    "AUTH_KIND",
    "BAD_CAP",
    "CHAT_KIND",
    "DISCOVER_EVERY",
    "FETCH_LIMIT",
    "KEY_ENV",
    "MAX_TEXT",
    "MEDIA_REF",
    "MEMBERS_KIND",
    "MEMBERS_TTL",
    "MEMBERSHIP_KIND",
    "METADATA_KIND",
    "POLL_EVERY",
    "POLL_FLOOR",
    "SEEN_CAP",
    "URL_ENV",
    "BuzzAdapter",
    "BuzzOptions",
    "SocketOpener",
    "WebSocketLike",
    "httpx_ws_opener",
]

log = logging.getLogger(__name__)

URL_ENV = "BUZZ_RELAY_URL"
KEY_ENV = "BUZZ_PRIVATE_KEY"
ALLOWLIST_ENV = "INDUS_BUZZ_ALLOWED_USERS"
PROFILE_KIND = 0
CHAT_KIND = 9
AUTH_KIND = 22242
METADATA_KIND = 39000
MEMBERS_KIND = 39002
MEMBERSHIP_KIND = 44100
POLL_EVERY = 4.0
POLL_FLOOR = 1.0
FETCH_LIMIT = 50
SEEN_CAP = 500
# Ids of dropped (forged or malformed) events remembered only to log each once; a relay that
# streams junk must not grow memory without bound.
BAD_CAP = 2048
DISCOVER_EVERY = 5
MEMBERS_TTL = 60.0
NAMES_TTL = 300.0
REQUEST_TIMEOUT = 15.0
PUBLISH_TIMEOUT = 15.0
RETRY_FIRST = 2.0
RETRY_MAX = 60.0
JITTER = 0.2
MAX_TEXT = 16000
MAX_FRAME_BYTES = 2 * 1024 * 1024
MAX_ATTACHMENTS = 4
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
MEDIA_REF = "buzz-media:"
# Audio that chat clients record as voice notes; other audio is a plain audio file.
VOICE_MIMES = frozenset(
    {"audio/ogg", "audio/opus", "audio/webm", "audio/mp4", "audio/x-m4a", "audio/aac"}
)

_HEX64 = re.compile(r"[0-9a-f]{64}")
_RETRY_AFTER = re.compile(r"retry after (\d+(?:\.\d+)?)", re.IGNORECASE)
_DROP_CHANNEL = ("restricted", "not a channel member", "not a member")


class WebSocketLike(Protocol):
    async def receive_text(self) -> str: ...

    async def send_text(self, data: str) -> None: ...


type SocketOpener = Callable[[str], contextlib.AbstractAsyncContextManager[WebSocketLike]]


class _MissingDependency(Exception):
    pass


class _NotSent(Exception):
    """The frame never left: no socket, or the write failed."""


class _NoAnswer(Exception):
    """The frame left but the relay did not answer in time."""


class _Refused(Exception):
    """The relay closed a request (``CLOSED`` with a reason)."""


class _AuthRefused(Exception):
    """The relay rejected our NIP-42 authentication."""


def httpx_ws_opener(proxy: str | None = None) -> SocketOpener:
    """The production opener: an httpx-ws session to the relay (``proxy`` from the config)."""

    @contextlib.asynccontextmanager
    async def opened(url: str) -> AsyncIterator[WebSocketLike]:
        try:
            import httpx_ws  # pyright: ignore[reportMissingImports]
        except ModuleNotFoundError:
            raise _MissingDependency(f"Buzz needs httpx-ws for the relay: {INSTALL_HINT}") from None
        http_url = "http" + url[2:] if url.startswith("ws") else url
        async with (
            httpx.AsyncClient(
                proxy=proxy, trust_env=False, timeout=httpx.Timeout(30.0, connect=15.0)
            ) as client,
            httpx_ws.aconnect_ws(  # pyright: ignore[reportUnknownMemberType]
                http_url, client, max_message_size_bytes=MAX_FRAME_BYTES
            ) as socket,
        ):
            yield socket  # pyright: ignore[reportUnknownArgumentType]

    return opened


def _as_dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}  # pyright: ignore[reportUnknownArgumentType]


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _truthy(value: object, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _list(value: object) -> list[str]:
    if isinstance(value, str):
        return [p.strip() for p in value.split(",") if p.strip()]
    if isinstance(value, list | tuple):
        return [str(p).strip() for p in value if str(p).strip()]  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    return []


def _created(event: Mapping[str, Any]) -> int:
    value = event.get("created_at")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def tag_values(event: Mapping[str, Any], name: str) -> list[str]:
    """The first values of every ``name`` tag of an event."""
    out: list[str] = []
    tags = event.get("tags")
    if isinstance(tags, list):
        for tag in tags:  # pyright: ignore[reportUnknownVariableType]
            if isinstance(tag, list) and len(tag) >= 2 and tag[0] == name:  # pyright: ignore[reportUnknownArgumentType]
                out.append(str(tag[1]))  # pyright: ignore[reportUnknownArgumentType]
    return out


def thread_of(event: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """NIP-10 ``(thread root, direct parent)`` of an event (both None when top level)."""
    root = reply = None
    positional: list[str] = []
    tags = event.get("tags")
    for tag in tags if isinstance(tags, list) else []:  # pyright: ignore[reportUnknownVariableType]
        if not (isinstance(tag, list) and len(tag) >= 2 and tag[0] == "e"):  # pyright: ignore[reportUnknownArgumentType]
            continue
        target = str(tag[1]).strip()  # pyright: ignore[reportUnknownArgumentType]
        marker = str(tag[3]) if len(tag) > 3 else ""  # pyright: ignore[reportUnknownArgumentType]
        if not target:
            continue
        if marker == "root":
            root = target
        elif marker == "reply":
            reply = target
        elif not marker:
            positional.append(target)
    if root is None and reply is None and positional:
        root, reply = positional[0], positional[-1]
    root = root or reply
    return root, reply or root


@dataclass(frozen=True)
class BuzzOptions:
    channels: tuple[str, ...] = ()
    poll_interval: float = POLL_EVERY
    reply_in_thread: bool = True
    attachment_hosts: tuple[str, ...] = ()

    @classmethod
    def read(cls, extra: Mapping[str, Any], env: Mapping[str, str]) -> BuzzOptions:
        channels = _list(extra.get("channels")) or _list(env.get("BUZZ_CHANNELS"))
        raw = extra.get("poll_interval") or env.get("BUZZ_POLL_INTERVAL")
        try:
            interval = float(raw) if raw not in (None, "") else POLL_EVERY  # pyright: ignore[reportArgumentType]
        except (TypeError, ValueError):
            interval = POLL_EVERY
        return cls(
            channels=tuple(channels),
            poll_interval=max(POLL_FLOOR, interval),
            reply_in_thread=_truthy(extra.get("reply_in_thread"), True),
            attachment_hosts=tuple(_list(extra.get("attachment_hosts"))),
        )


@dataclass
class _Channel:
    chat_type: str = "group"
    last_ts: int = 0
    name: str = ""
    peer: str = ""
    seen: OrderedDict[str, None] = field(default_factory=OrderedDict[str, None])
    authors: OrderedDict[str, str] = field(default_factory=OrderedDict[str, str])

    def remember(self, event_id: str, author: str) -> None:
        self.seen[event_id] = None
        self.authors[event_id] = author
        while len(self.seen) > SEEN_CAP:
            self.seen.popitem(last=False)
        while len(self.authors) > SEEN_CAP:
            self.authors.popitem(last=False)


@dataclass
class _Sub:
    future: asyncio.Future[tuple[str, str]]
    events: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])


def _origin(value: str) -> tuple[str, int] | None:
    """The secure ``(host, port)`` a host setting or URL names; None for anything else."""
    text = value.strip()
    if text and "://" not in text:
        text = f"https://{text}"
    parts = urlsplit(text)
    if parts.scheme not in ("https", "wss"):
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        return None
    return host, 443 if port is None else port


class BuzzAdapter(Adapter):
    platform: ClassVar[str] = "buzz"
    capabilities: ClassVar[Capabilities] = Capabilities(
        max_len=MAX_TEXT,
        markup="markdown",
        splits_natively=True,
        reply_opens_thread=True,
    )
    vendor_names: ClassVar[Mapping[str, tuple[str, ...]]] = {
        "coincurve:PrivateKey": ("sign_schnorr",),
        "coincurve:PublicKeyXOnly": ("from_secret", "verify", "format"),
        "httpx_ws:AsyncWebSocketSession": ("receive_text", "send_text", "close"),
    }

    def __init__(
        self,
        *,
        clock: Clock,
        relay_url: str,
        private_key: str,
        settings: PlatformSettings | None = None,
        options: BuzzOptions | None = None,
        allowlist: Iterable[str] = (),
        crypto: NostrCrypto | None = None,
        opener: SocketOpener | None = None,
        proxy: str | None = None,
        media_transport: httpx.AsyncBaseTransport | None = None,
        rng: Callable[[], float] = random.random,
        send_only: bool = False,
    ) -> None:
        self._private_key = (private_key or "").strip()
        super().__init__(clock=clock, settings=settings, secrets=(self._private_key,))
        self.relay_url = (relay_url or "").strip().rstrip("/")
        self.options = options or BuzzOptions()
        self.send_only = send_only
        self._crypto = crypto
        self._opener: SocketOpener = opener or httpx_ws_opener(proxy)
        self._proxy = proxy
        self._media_transport = media_transport
        self._rng = rng
        self._allowed = frozenset(
            e for e in expand_nostr_allowlist(allowlist) if _HEX64.fullmatch(e)
        )
        self.keys: NostrKeys | None = None
        self.display_name = ""
        self._running = False
        self._socket: WebSocketLike | None = None
        self._runner: asyncio.Task[None] | None = None
        self._reader: asyncio.Task[None] | None = None
        self._poller: asyncio.Task[None] | None = None
        self._auth_task: asyncio.Task[None] | None = None
        self._auth_ready = asyncio.Event()
        self._auth_error: str | None = None
        self._subs: dict[str, _Sub] = {}
        self._oks: dict[str, asyncio.Future[tuple[bool, str]]] = {}
        self._ids = 0
        self.channels: dict[str, _Channel] = {}
        self._restricted: set[str] = set()
        self._dm_by_peer: dict[str, str] = {}
        self._profiles: dict[str, tuple[float, str, bool]] = {}
        self._members: dict[str, tuple[float, frozenset[str]]] = {}
        self._membership_since = 0
        self._sweeps = 0
        self._bad: set[str] = set()

    def __repr__(self) -> str:
        state = "connected" if self.connected else "disconnected"
        who = self.keys.pubkey[:12] if self.keys is not None else "?"
        return f"<BuzzAdapter {self.relay_url or '?'} {who} {state}>"

    @property
    def pubkey(self) -> str:
        return self.keys.pubkey if self.keys is not None else ""

    # -- lifecycle ---------------------------------------------------------------------------------

    def ws_url(self) -> str:
        parsed = urlsplit(self.relay_url)
        scheme = {"https": "wss", "http": "ws"}.get(parsed.scheme, parsed.scheme)
        return parsed._replace(scheme=scheme, fragment="").geturl()

    def _check_config(self) -> NostrCrypto:
        wanted = ((URL_ENV, self.relay_url), (KEY_ENV, self._private_key))
        missing = [name for name, value in wanted if not value]
        if missing:
            raise AdapterConnectError(
                f"Buzz is not configured: set {', '.join(missing)}", retryable=False, code="config"
            )
        parsed = urlsplit(self.relay_url)
        secure = parsed.scheme in ("https", "wss")
        local = parsed.scheme in ("http", "ws") and is_loopback(parsed.hostname)
        if not parsed.hostname or not (secure or local):
            raise AdapterConnectError(
                f"{URL_ENV} must be an https:// (or wss://) address; http only on this machine",
                retryable=False,
                code="config",
            )
        try:
            crypto = self._crypto or load_crypto()
        except MissingCrypto as exc:
            raise AdapterConnectError(
                str(exc), retryable=False, code="missing_dependency"
            ) from None
        try:
            self.keys = NostrKeys.load(crypto, self._private_key)
        except ValueError as exc:
            raise AdapterConnectError(str(exc), retryable=False, code="config") from None
        self._secrets = (*self._secrets, self.keys.secret.hex())
        self._crypto = crypto
        return crypto

    async def _open(self, is_reconnect: bool) -> None:
        self._check_config()
        self._running = True
        self._auth_ready = asyncio.Event()
        self._auth_error = None
        first: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._runner = asyncio.ensure_future(self._run(first))
        try:
            await first
        except BaseException as exc:
            await self._stop_tasks()
            if isinstance(exc, asyncio.CancelledError):
                raise
            if isinstance(exc, _MissingDependency):
                raise AdapterConnectError(
                    str(exc), retryable=False, code="missing_dependency"
                ) from None
            if isinstance(exc, _AuthRefused):
                raise AdapterConnectError(
                    f"the Buzz relay refused our identity: {exc}", retryable=False, code="buzz_auth"
                ) from None
            raise AdapterConnectError(
                f"cannot reach the Buzz relay at {self.relay_url}: {type(exc).__name__}",
                retryable=True,
            ) from None
        if self.send_only:
            log.info("buzz: connected to %s for sending", self.relay_url)
            return
        try:
            await self._prepare()
        except Exception as exc:
            log.warning("buzz: preparing the watch list failed: %s", self.scrub(str(exc)))
        self._poller = asyncio.ensure_future(self._poll_loop())
        log.info(
            "buzz: connected to %s as %s, watching %d conversation(s)",
            self.relay_url,
            self.display_name or self.pubkey[:12],
            len(self.channels),
        )

    async def _close(self) -> None:
        await self._stop_tasks()

    async def _stop_tasks(self) -> None:
        self._running = False
        for task in (self._poller, self._auth_task, self._reader, self._runner):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._poller = self._auth_task = self._reader = self._runner = None
        self._socket = None
        self._fail_pending()

    def _fail_pending(self) -> None:
        for sub in self._subs.values():
            if not sub.future.done():
                sub.future.set_exception(ConnectionError("the relay socket closed"))
                sub.future.exception()
        for future in self._oks.values():
            if not future.done():
                future.set_exception(ConnectionError("the relay socket closed"))
                future.exception()

    async def _run(self, first: asyncio.Future[None]) -> None:
        delay = RETRY_FIRST
        while self._running:
            try:
                async with self._opener(self.ws_url()) as socket:
                    self._socket = socket
                    reader = self._reader = asyncio.ensure_future(self._read(socket))
                    try:
                        await self._probe()
                        if not first.done():
                            first.set_result(None)
                        delay = RETRY_FIRST
                        await asyncio.wait({reader})
                        if reader.cancelled():
                            raise ConnectionError("the relay socket was closed")
                        failure = reader.exception()
                        if failure is not None:
                            raise failure
                    finally:
                        self._socket = None
                        if not reader.done():
                            reader.cancel()
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await reader
                        self._fail_pending()
                        self._auth_ready = asyncio.Event()
            except asyncio.CancelledError:
                raise
            except _AuthRefused as exc:
                if not first.done():
                    first.set_exception(exc)
                else:
                    self.report_fatal("buzz_auth", f"the Buzz relay refused our identity: {exc}")
                return
            except Exception as exc:
                if not first.done():
                    first.set_exception(exc)
                    return
                log.warning(
                    "buzz: relay socket lost (%s); reopening in %.0f s", type(exc).__name__, delay
                )
            else:
                if not first.done():
                    first.set_exception(ConnectionError("the relay closed the socket"))
                    return
            if not self._running:
                return
            await self.clock.sleep(delay * (1 + JITTER * self._rng()))
            delay = min(delay * 2, RETRY_MAX)

    async def _read(self, socket: WebSocketLike) -> None:
        while True:
            raw = await socket.receive_text()
            try:
                self._on_frame(raw)
            except Exception:
                log.exception("buzz: a relay frame could not be handled")

    def _on_frame(self, raw: str) -> None:
        try:
            frame = json.loads(raw)
        except ValueError:
            return
        if not isinstance(frame, list) or not frame:
            return
        message: list[Any] = list(frame)  # pyright: ignore[reportUnknownArgumentType]
        head = message[0]
        if head == "EVENT" and len(message) >= 3 and isinstance(message[2], Mapping):
            sub = self._subs.get(str(message[1]))
            if sub is not None and not sub.future.done():
                sub.events.append(_as_dict(message[2]))
        elif head in ("EOSE", "CLOSED") and len(message) >= 2:
            sub = self._subs.get(str(message[1]))
            reason = str(message[2]) if len(message) > 2 else ""
            if sub is not None and not sub.future.done():
                sub.future.set_result(("eose" if head == "EOSE" else "closed", reason))
        elif head == "OK" and len(message) >= 3:
            future = self._oks.get(str(message[1]))
            if future is not None and not future.done():
                future.set_result((message[2] is True, str(message[3]) if len(message) > 3 else ""))
        elif head == "AUTH" and len(message) >= 2 and isinstance(message[1], str):
            if self._auth_task is None or self._auth_task.done():
                self._auth_task = asyncio.ensure_future(self._authenticate(message[1]))
        elif head == "NOTICE":
            log.info("buzz: relay notice: %s", self.scrub(str(message[-1]))[:200])

    # -- relay requests ----------------------------------------------------------------------------

    def _next_id(self) -> str:
        self._ids += 1
        return f"indus-{self._ids}"

    async def _within[T](self, future: asyncio.Future[T], seconds: float) -> T:
        timer = asyncio.ensure_future(self.clock.sleep(seconds))
        try:
            await asyncio.wait({future, timer}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            timer.cancel()
        if future.done():
            return future.result()
        raise _NoAnswer()

    async def _authenticate(self, challenge: str) -> None:
        assert self._crypto is not None and self.keys is not None
        tags = [["relay", self.ws_url()], ["challenge", challenge]]
        event = signed_event(
            self._crypto, self.keys, kind=AUTH_KIND, tags=tags, content="", created_at=self._now()
        )
        try:
            accepted, reason = await self._send_event(event, head="AUTH")
        except (_NotSent, _NoAnswer, ConnectionError) as exc:
            log.warning("buzz: authentication did not complete: %s", type(exc).__name__)
            return
        if accepted:
            self._auth_ready.set()
            return
        self._auth_error = reason or "authentication refused"
        self._auth_ready.set()
        log.error("buzz: the relay refused our authentication: %s", self.scrub(self._auth_error))
        reader = self._reader
        if self.connected:
            self.report_fatal(
                "buzz_auth", f"the Buzz relay refused our identity: {self._auth_error}"
            )
        if reader is not None and not reader.done():
            self._running = False
            reader.cancel()

    async def _wait_auth(self) -> None:
        try:
            await self._within(asyncio.ensure_future(self._auth_ready.wait()), REQUEST_TIMEOUT)
        except _NoAnswer:
            raise _Refused("auth-required: the relay sent no challenge") from None
        if self._auth_error is not None:
            raise _AuthRefused(self._auth_error)

    async def request(self, flt: Mapping[str, Any]) -> list[dict[str, Any]]:
        """The events matching ``flt`` (until ``EOSE``); ``_Refused`` when the relay closes it."""
        for attempt in (0, 1):
            socket = self._socket
            if socket is None:
                raise _NotSent("the relay is not connected")
            sub_id = self._next_id()
            sub = _Sub(asyncio.get_running_loop().create_future())
            self._subs[sub_id] = sub
            try:
                try:
                    await socket.send_text(json.dumps(["REQ", sub_id, dict(flt)]))
                except Exception:
                    raise _NotSent("the request could not be written") from None
                kind, reason = await self._within(sub.future, REQUEST_TIMEOUT)
            finally:
                self._subs.pop(sub_id, None)
            if kind == "eose":
                with contextlib.suppress(Exception):
                    await socket.send_text(json.dumps(["CLOSE", sub_id]))
                return sub.events
            if reason.startswith("auth-required") and attempt == 0:
                await self._wait_auth()
                continue
            raise _Refused(reason or "closed")
        raise _Refused("auth-required")

    async def _send_event(
        self, event: Mapping[str, Any], *, head: str = "EVENT"
    ) -> tuple[bool, str]:
        socket = self._socket
        if socket is None:
            raise _NotSent("the relay is not connected")
        ident = str(event["id"])
        future: asyncio.Future[tuple[bool, str]] = asyncio.get_running_loop().create_future()
        self._oks[ident] = future
        try:
            try:
                await socket.send_text(json.dumps([head, dict(event)], ensure_ascii=False))
            except Exception:
                raise _NotSent("the event could not be written") from None
            return await self._within(future, PUBLISH_TIMEOUT)
        finally:
            self._oks.pop(ident, None)

    def _verified(self, events: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        assert self._crypto is not None
        out: list[dict[str, Any]] = []
        for event in events:
            if verify_event(self._crypto, event):
                out.append(_as_dict(event))
            else:
                self._note_bad(event)
        return out

    def _note_bad(self, event: Mapping[str, Any]) -> None:
        ident = str(event.get("id") or "?")[:64]
        if ident not in self._bad:
            if len(self._bad) >= BAD_CAP:
                self._bad.clear()
            self._bad.add(ident)
            log.debug("buzz: dropped event %s: bad id or signature", ident[:16])

    def _now(self) -> int:
        return int(self.clock.now().timestamp())

    async def _probe(self) -> None:
        """A first request (our own profile): proves the relay answers, and learns our name."""
        flt = {"kinds": [PROFILE_KIND], "authors": [self.pubkey], "limit": 1}
        events = await self.request(flt)
        name, _ = self._profile_of(self._verified(events))
        self.display_name = name

    # -- watching ----------------------------------------------------------------------------------

    async def _prepare(self) -> None:
        for channel in self.options.channels:
            if channel not in self.channels:
                self.channels[channel] = await self._seed(channel, "group")
        await self._names_for(list(self.options.channels))
        self._membership_since = self._now()
        await self._discover(seed=True)

    async def _seed(self, channel: str, chat_type: str) -> _Channel:
        state = _Channel(chat_type)
        try:
            events = await self.request(
                {"kinds": [CHAT_KIND], "#h": [channel], "limit": FETCH_LIMIT}
            )
        except Exception as exc:
            log.warning("buzz: could not seed %s (%s)", channel, type(exc).__name__)
            state.last_ts = self._now()  # never replay a channel's history later
            return state
        for event in self._verified(events):
            state.remember(str(event["id"]), str(event["pubkey"]))
            state.last_ts = max(state.last_ts, int(event["created_at"]))
        return state

    async def _names_for(self, channels: Sequence[str]) -> None:
        if not channels:
            return
        try:
            events = await self.request({"kinds": [METADATA_KIND], "#d": list(channels)})
        except Exception:
            return
        for event in self._verified(events):
            for conv in tag_values(event, "d"):
                state = self.channels.get(conv)
                name = next(iter(tag_values(event, "name")), "")
                if state is not None and name and state.chat_type != "dm":
                    state.name = name

    async def _discover(self, *, seed: bool) -> None:
        """Adopt conversations the relay says we are in (see the module notes)."""
        members: dict[str, frozenset[str]] = {}
        for event in self._verified(
            await self.request({"kinds": [MEMBERS_KIND], "#p": [self.pubkey]})
        ):
            for conv in tag_values(event, "d"):
                members[conv] = frozenset(tag_values(event, "p"))
        since = self._membership_since
        joined = await self.request(
            {"kinds": [MEMBERSHIP_KIND], "#p": [self.pubkey], "since": since}
        )
        for event in self._verified(joined):
            self._membership_since = max(self._membership_since, int(event["created_at"]))
            for conv in tag_values(event, "h") or tag_values(event, "d"):
                members.setdefault(conv, frozenset())
        new = [c for c in members if c not in self.channels and c not in self._restricted]
        if not new:
            return
        meta: dict[str, tuple[str, str]] = {}
        for event in self._verified(await self.request({"kinds": [METADATA_KIND], "#d": new})):
            name = next(iter(tag_values(event, "name")), "")
            about = next(iter(tag_values(event, "about")), "")
            for conv in tag_values(event, "d"):
                meta[conv] = (name, about)
        for conv in new:
            name, about = meta.get(conv, ("", ""))
            if name == "DM" and not about.strip():
                state = await self._seed(conv, "dm") if seed else _Channel("dm")
                state.name = "DM"
                peers = sorted(members[conv] - {self.pubkey})
                state.peer = peers[0] if len(peers) == 1 else ""
                if state.peer:
                    self._dm_by_peer[state.peer] = conv
                self.channels[conv] = state
                log.info("buzz: watching a new DM conversation %s", conv)
            elif not self.options.channels and name:
                state = await self._seed(conv, "group")
                state.name = name
                self.channels[conv] = state
                log.info("buzz: watching newly joined channel %s (%s)", conv, name)

    async def _poll_loop(self) -> None:
        while self._running:
            await self.clock.sleep(self.options.poll_interval)
            self._sweeps += 1
            try:
                if self._sweeps % DISCOVER_EVERY == 0:
                    await self._discover(seed=False)
                for channel in list(self.channels):
                    await self._poll(channel)
            except asyncio.CancelledError:
                raise
            except _NoAnswer:
                # A socket the relay stopped answering: drop it, the runner opens a new one.
                log.warning("buzz: the relay stopped answering; reconnecting")
                reader = self._reader
                if reader is not None and not reader.done():
                    reader.cancel()
            except Exception as exc:
                log.warning("buzz: poll sweep failed: %s", type(exc).__name__)

    async def _poll(self, channel: str) -> None:
        state = self.channels.get(channel)
        if state is None:
            return
        flt: dict[str, Any] = {"kinds": [CHAT_KIND], "#h": [channel], "limit": FETCH_LIMIT}
        if state.last_ts:
            flt["since"] = state.last_ts  # inclusive: same-second events come back, deduped by id
        try:
            events = await self.request(flt)
        except _Refused as exc:
            reason = str(exc).lower()
            if any(n in reason for n in _DROP_CHANNEL) or reason.startswith("auth-required"):
                log.warning("buzz: the relay refused %s (%s); no longer watched", channel, exc)
                self.channels.pop(channel, None)
                self._restricted.add(channel)
            return
        ordered = sorted(events, key=_created)
        for event in ordered:
            await self._handle(channel, state, event)

    async def _handle(self, channel: str, state: _Channel, event: Mapping[str, Any]) -> None:
        assert self._crypto is not None
        ident = event.get("id")
        if not isinstance(ident, str) or ident in state.seen:
            return
        if not verify_event(self._crypto, event):
            state.seen[ident] = None
            self._note_bad(event)
            return
        author = str(event["pubkey"])
        state.remember(ident, author)
        state.last_ts = max(state.last_ts, int(event["created_at"]))
        if event["kind"] != CHAT_KIND:
            return  # reactions, edits, housekeeping: not chat
        groups = tag_values(event, "h")
        if groups and channel not in groups:
            return
        if author == self.pubkey:
            return
        content = str(event["content"])
        media, kind = self._media(event)
        if not content.strip() and not media:
            return
        name, is_bot = await self._profile(author)
        is_dm = state.chat_type == "dm"
        root, parent = thread_of(event)
        thread = None if is_dm else root
        metadata: dict[str, Any] = {}
        if not is_dm and self._addressed(event, content):
            metadata["mentioned"] = True
        npub = hex_to_npub(author)
        chat_type = "dm" if is_dm else ("thread" if thread else "group")
        prospective = ident if chat_type == "group" and self.options.reply_in_thread else None
        source = Source(
            platform=self.platform,
            chat_id=channel,
            chat_type=chat_type,
            chat_name=state.name or None,
            user_id=author,
            user_id_alt=npub,
            user_name=name or npub[:16],
            thread_id=thread,
            is_bot=is_bot,
            prospective_thread_id=prospective,
        )
        await self.emit(
            InboundEvent(
                source=source,
                text=self._strip_mention(content),
                kind=kind,
                message_id=ident,
                media=tuple(media),
                reply_to_id=parent,
                reply_to_is_own=parent is not None and state.authors.get(parent) == self.pubkey,
                metadata=metadata,
                timestamp=datetime.fromtimestamp(int(event["created_at"]), tz=UTC),
            )
        )

    # -- people ------------------------------------------------------------------------------------

    @staticmethod
    def _profile_of(events: Sequence[Mapping[str, Any]]) -> tuple[str, bool]:
        newest = max(events, key=lambda e: int(e.get("created_at") or 0), default=None)
        if newest is None:
            return "", False
        try:
            data = _as_dict(json.loads(str(newest.get("content") or "{}")))
        except ValueError:
            return "", False
        name = _text(data.get("display_name")) or _text(data.get("name"))
        return name[:64], data.get("bot") is True

    async def _profile(self, pubkey: str) -> tuple[str, bool]:
        """A pubkey's display name and NIP-24 ``bot`` flag (cached 5 min, failures too)."""
        now = self.clock.monotonic()
        cached = self._profiles.get(pubkey)
        if cached is not None and now - cached[0] < NAMES_TTL:
            return cached[1], cached[2]
        try:
            events = await self.request({"kinds": [PROFILE_KIND], "authors": [pubkey], "limit": 1})
            name, bot = self._profile_of(
                [e for e in self._verified(events) if e.get("pubkey") == pubkey]
            )
        except Exception:
            name, bot = "", False
        self._profiles[pubkey] = (now, name, bot)
        return name, bot

    def _mention_patterns(self) -> list[str]:
        patterns: list[str] = []
        if self.pubkey:
            patterns.append(rf"@?{re.escape(self.pubkey)}(?![0-9a-f])")
            patterns.append(rf"@?{re.escape(hex_to_npub(self.pubkey))}(?![a-z0-9])")
        if self.display_name:
            patterns.append(rf"(?<![\w@])@{re.escape(self.display_name)}(?=$|[\s,;.!?:)\]}}])")
        return patterns

    def _addressed(self, event: Mapping[str, Any], content: str) -> bool:
        if self.pubkey and self.pubkey in tag_values(event, "p"):
            return True
        return any(re.search(p, content, re.IGNORECASE) for p in self._mention_patterns())

    def _strip_mention(self, content: str) -> str:
        text = content.strip()
        patterns = self._mention_patterns()
        if not patterns:
            return text
        lead = re.compile(rf"^(?:{'|'.join(patterns)})[\s:,]*", re.IGNORECASE)
        return lead.sub("", text, count=1).strip() or text

    def resolved_allowlist_ids(self) -> frozenset[str]:
        """The hex pubkeys of the allowlist's npub (and hex) entries (P15-08 reads them)."""
        return self._allowed

    # -- media -------------------------------------------------------------------------------------

    def _media(self, event: Mapping[str, Any]) -> tuple[list[Attachment], MessageKind]:
        out: list[Attachment] = []
        total = 0
        kind = MessageKind.TEXT
        tags = event.get("tags")
        for tag in tags if isinstance(tags, list) else []:  # pyright: ignore[reportUnknownVariableType]
            if not (isinstance(tag, list) and tag and tag[0] == "imeta"):  # pyright: ignore[reportUnknownArgumentType]
                continue
            if len(out) >= MAX_ATTACHMENTS:
                break
            fields: dict[str, str] = {}
            for entry in tag[1:]:  # pyright: ignore[reportUnknownVariableType]
                key, sep, value = str(entry).partition(" ")  # pyright: ignore[reportUnknownArgumentType]
                if sep and key not in fields:
                    fields[key] = value.strip()
            url, digest, mime = (
                fields.get("url", ""),
                fields.get("x", "").lower(),
                fields.get("m", ""),
            )
            size = int(fields["size"]) if is_digits(fields.get("size", "")) else 0
            if not url or not _HEX64.fullmatch(digest) or not 0 < size <= MAX_ATTACHMENT_BYTES:
                continue
            if total + size > MAX_ATTACHMENT_BYTES:
                continue
            total += size
            mime = mime.lower()[:100]
            out.append(Attachment(ref=f"{MEDIA_REF}{digest}:{size}:{url}", mime=mime))
            if kind is MessageKind.TEXT:
                kind = self._kind_of(mime)
        return out, kind

    @staticmethod
    def _kind_of(mime: str) -> MessageKind:
        if mime.startswith("image/"):
            return MessageKind.PHOTO
        if mime.startswith("video/"):
            return MessageKind.VIDEO
        if mime.startswith("audio/"):
            return MessageKind.VOICE if mime.split(";")[0] in VOICE_MIMES else MessageKind.AUDIO
        return MessageKind.DOCUMENT

    def _allowed_origins(self) -> set[tuple[str, int]]:
        """The relay's own secure origin and every listed ``attachment_hosts`` entry."""
        found: set[tuple[str, int]] = set()
        for entry in (self.relay_url, *self.options.attachment_hosts):
            origin = _origin(entry)
            if origin is not None:
                found.add(origin)
        return found

    def register_fetchers(self, seat: FetcherSeat) -> Callable[[], None] | None:
        """Register `fetch_media` with the media intake (P15-23); returns the undo."""
        return seat.register_fetcher(self.platform, self.fetch_media)

    async def fetch_media(self, attachment: Attachment) -> AsyncIterator[bytes]:
        """One ``imeta`` file: HTTPS from a trusted origin, size and SHA-256 as declared."""
        if not attachment.ref.startswith(MEDIA_REF):
            raise MediaRejected("it is not a Buzz attachment")
        digest, _, rest = attachment.ref[len(MEDIA_REF) :].partition(":")
        size_text, _, url = rest.partition(":")
        if not _HEX64.fullmatch(digest) or not is_digits(size_text):
            raise MediaRejected("it is not a Buzz attachment")
        size = int(size_text)
        parts = urlsplit(url)
        trusted = (
            parts.scheme == "https"
            and not (parts.username or parts.password or parts.fragment)
            and _origin(url) in self._allowed_origins()
        )
        if not trusted:
            raise MediaRejected("it is not on a trusted host")
        if not 0 < size <= MAX_ATTACHMENT_BYTES:
            raise MediaRejected("it is larger than 20 MB")
        transport = self._media_transport or httpx.AsyncHTTPTransport(
            proxy=self._proxy, trust_env=False
        )
        data = bytearray()
        async with (
            httpx.AsyncClient(
                transport=transport,
                follow_redirects=False,
                trust_env=False,
                timeout=httpx.Timeout(30.0),
                headers={"Accept-Encoding": "identity"},
            ) as client,
            client.stream("GET", url) as response,
        ):
            if response.status_code != 200:
                raise MediaRejected("it could not be downloaded")
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > size:
                    raise MediaRejected("it is larger than declared")
        if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
            raise MediaRejected("it does not match its declared size and checksum")
        yield bytes(data)

    # -- outbound ----------------------------------------------------------------------------------

    def _sign(self, kind: int, tags: Sequence[Sequence[str]], content: str) -> dict[str, Any]:
        assert self._crypto is not None and self.keys is not None
        return signed_event(
            self._crypto, self.keys, kind=kind, tags=tags, content=content, created_at=self._now()
        )

    async def dm_with(self, pubkey: str) -> str | None:
        """The DM conversation whose members are exactly ``pubkey`` and us."""
        known = self._dm_by_peer.get(pubkey)
        if known is not None:
            return known
        try:
            events = self._verified(await self.request({"kinds": [MEMBERS_KIND], "#p": [pubkey]}))
        except Exception:
            return None
        for event in events:
            if frozenset(tag_values(event, "p")) != {pubkey, self.pubkey}:
                continue
            for conv in tag_values(event, "d"):
                self._dm_by_peer[pubkey] = conv
                if conv not in self.channels and not self.send_only:
                    self.channels[conv] = _Channel(
                        "dm", last_ts=self._now(), name="DM", peer=pubkey
                    )
                return conv
        return None

    async def _members_of(self, conv: str) -> frozenset[str]:
        now = self.clock.monotonic()
        cached = self._members.get(conv)
        if cached is not None and now - cached[0] < MEMBERS_TTL:
            return cached[1]
        found: set[str] = set()
        try:
            flt = {"kinds": [MEMBERS_KIND], "#d": [conv], "limit": 1}
            for event in self._verified(await self.request(flt)):
                found.update(tag_values(event, "p"))
        except Exception:
            return frozenset()
        result = frozenset(found)
        self._members[conv] = (now, result)
        return result

    async def _mention_tags(self, conv: str, text: str) -> list[list[str]]:
        """``p`` tags for ``@Name`` tokens naming exactly one member of ``conv``."""
        if "@" not in text:
            return []
        by_name: dict[str, list[str]] = {}
        for member in sorted(await self._members_of(conv) - {self.pubkey}):
            name, _ = await self._profile(member)
            if name:
                by_name.setdefault(name.lower(), []).append(member)
        tags: list[list[str]] = []
        rest = text
        for name in sorted(by_name, key=len, reverse=True):
            pattern = re.compile(rf"(?<![\w@])@{re.escape(name)}(?!\w)", re.IGNORECASE)
            if pattern.search(rest):
                if len(by_name[name]) == 1:
                    tags.append(["p", by_name[name][0]])
                rest = pattern.sub("\x00", rest)
        return tags

    def _refusal(self, reason: str) -> SendResult:
        prefix = reason.split(":", 1)[0].strip().lower()
        text = reason or "the relay refused the event"
        if prefix == "rate-limited":
            text = f"rate limit: {reason}"
        elif prefix in ("blocked", "restricted") and "not found" not in reason.lower():
            text = f"forbidden: {reason}"
        found = _RETRY_AFTER.search(reason)
        return SendResult.failure(text, retry_after=float(found.group(1)) if found else None)

    async def publish(self, kind: int, tags: Sequence[Sequence[str]], content: str) -> SendResult:
        """Sign and publish one event; the relay's ``OK`` decides the result."""
        event = self._sign(kind, tags, content)
        for attempt in (0, 1):
            try:
                accepted, reason = await self._send_event(event)
            except _NotSent as exc:
                return SendResult.failure(f"connect error: {exc}; nothing was sent")
            except (_NoAnswer, ConnectionError):
                return SendResult.failure(
                    "read timeout: the relay did not confirm the event; it may have it"
                )
            if accepted or reason.startswith("duplicate"):
                ident = str(event["id"])
                for conv in tag_values(event, "h"):
                    state = self.channels.get(conv)
                    if state is not None:
                        state.remember(ident, self.pubkey)
                return SendResult.sent(ident)
            if reason.startswith("auth-required") and attempt == 0:
                try:
                    await self._wait_auth()
                except (_Refused, _AuthRefused):
                    break
                continue
            return self._refusal(reason)
        return SendResult.failure("forbidden: the relay wants authentication we could not give")

    async def _send(
        self, chat_id: str, text: str, reply_to: str | None, meta: dict[str, Any]
    ) -> SendResult:
        target = chat_id.strip()
        peer = nostr_pubkey(target)
        conv: str | None = target
        if peer is not None:
            conv = await self.dm_with(peer)
            if conv is None:
                return SendResult.failure("chat not found: no Buzz DM with that member")
        assert conv is not None
        state = self.channels.get(conv)
        is_dm = peer is not None or (state is not None and state.chat_type == "dm")
        base: list[list[str]] = [["h", conv]]
        thread = meta.get("thread_id")
        if self.options.reply_in_thread and not is_dm and isinstance(thread, str) and thread:
            base.append(["e", thread, "", "root"])
            if reply_to and reply_to != thread:
                base.append(["e", reply_to, "", "reply"])
        chunked = split_message(text, MAX_TEXT)
        ids: list[str] = []
        for index, chunk in enumerate(chunked.chunks):
            if index:
                await self.slots.pace(chat_id)
            tags = base + await self._mention_tags(conv, chunk.text)
            result = await self.publish(CHAT_KIND, tags, chunk.text)
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
            assert result.message_id is not None
            ids.append(result.message_id)
        return SendResult.sent(ids[-1], tuple(ids[:-1]))

    # -- descriptive -------------------------------------------------------------------------------

    async def _chat_info(self, chat_id: str) -> ChatInfo:
        state = self.channels.get(chat_id)
        if state is None:
            return ChatInfo(name=chat_id, type="group")
        kind = "dm" if state.chat_type == "dm" else "group"
        return ChatInfo(name=state.name or chat_id, type=kind)
