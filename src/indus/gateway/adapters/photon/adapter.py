"""iMessage through Photon, over our own sidecar process (P16-25; the sidecar is
``apps/photon-sidecar``).

The adapter owns the sidecar (``sidecar.PhotonSidecar``: adopt this profile's running sidecar or
start one, pidfile identity, never touch another profile's) and talks to it over loopback HTTP with
the sidecar's per-start bearer token:

- connect: the sidecar must report ``open`` (or ``degraded``); ``auth_failed`` is a fatal
  ``photon_auth``; a sidecar still connecting after 30 s is accepted with a warning (sends answer
  "not connected" until it is up);
- inbound: one long-lived ``GET /events`` NDJSON stream with a cursor (``seq``) and the run's
  ``epoch``; lines are split on LF only (message text may hold other line separators). A dropped or
  stalled stream (no line for 60 s; the sidecar sends a heartbeat every 15 s) is reopened after
  1 s, doubling to 30 s. A sidecar that died is started again after 2 s, doubling to 60 s (the wait
  starts over once a sidecar stayed up 60 s). ``/health`` every 30 s: an upstream link reported
  ``degraded`` twice in a row restarts the sidecar;
- outbound: ``/send`` per piece of ≤8,000 code points (markdown; a piece holding a URL goes as plain
  text), ``/send-link`` for a message that is one URL (plain text if that is refused),
  ``/send-file`` (captions follow as their own bubble), ``/typing``, ``/react``, ``/unreact``,
  ``/read``. No edits, no buttons: streaming falls back to segments and approvals are the text
  card.

The stream is at-least-once, so events are deduped by id (4,000 ids, 48 hours). A text that is only
U+FFFC (iMessage's object-replacement placeholder) waits up to 15 s for the real attachment in that
chat and is merged into it. Rich-link preview art (``.pluginPayloadAttachment`` files) is dropped
for 30 s after a link in that chat, sent by us or by them. Access policy is the core's
(``INDUS_PHOTON_ALLOWED_USERS``, pairing). Media are fetched lazily (``register_fetchers``). Phone
numbers are masked in every log line and error text; message text is never logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import mimetypes
import re
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Protocol
from urllib.parse import quote, unquote, urlparse

import httpx

from indus.foundation.clock import Clock
from indus.foundation.digits import is_digits
from indus.gateway.adapters._http import SeenIds, failure_from_exception, failure_from_status
from indus.gateway.adapters._identity import mask_numbers
from indus.gateway.adapters.base import (
    Adapter,
    AdapterConnectError,
    Capabilities,
    FetcherSeat,
    InboundSink,
    MediaKind,
)
from indus.gateway.adapters.photon.sidecar import SidecarEndpoint, SidecarError
from indus.gateway.core.events import Attachment, InboundEvent, MessageKind, Source
from indus.gateway.core.send import SendResult
from indus.gateway.intake.media import MediaRejected
from indus.gateway.outbound.chunker import code_points, plain_marker, split_message
from indus.gateway.outbound.formatting import strip_markdown

if TYPE_CHECKING:
    from indus.gateway.config import PlatformSettings

__all__ = [
    "MAX_LEN",
    "MEDIA_REF",
    "PhotonAdapter",
    "PhotonOptions",
    "SidecarLauncher",
    "ndjson_lines",
]

log = logging.getLogger(__name__)

MAX_LEN = 8000
STREAM_IDLE_S = 60.0
STREAM_RETRY_FIRST_S = 1.0
STREAM_RETRY_MAX_S = 30.0
RESTART_FIRST_S = 2.0
RESTART_MAX_S = 60.0
STABLE_AFTER_S = 60.0
HEALTH_EVERY_S = 30.0
DEGRADED_LIMIT = 2
HUNG_LIMIT = 3
SENT_TEXT_MAX = 500
OPEN_WAIT_S = 30.0
OPEN_CHECK_S = 0.5
DEDUPE_MAX = 4000
DEDUPE_TTL_S = 48 * 3600.0
PLACEHOLDER_WAIT_S = 15.0
PREVIEW_WINDOW_S = 30.0
SENT_MAX = 1000
LINKS_MAX = 200
MAX_LINE_BYTES = 1024 * 1024
MAX_MEDIA_BYTES = 100 * 1024 * 1024
MEDIA_REF = "photon-media:"
PLACEHOLDER = "￼"
DEFAULT_MENTION_PATTERNS = (r"(?<![\w@])@?indus(?:\s+agent)?\b[,:\-]?",)
_MEDIA_ID = re.compile(r"[0-9a-f]{24}")
_URL = re.compile(r"https?://[^\s<>\"')]+", re.IGNORECASE)
_TRUTHY = frozenset({"1", "true", "yes", "on"})

_KINDS: Mapping[str, MessageKind] = {
    "image": MessageKind.PHOTO,
    "video": MessageKind.VIDEO,
    "voice": MessageKind.VOICE,
    "audio": MessageKind.AUDIO,
    "file": MessageKind.DOCUMENT,
}
# How the sidecar's error codes read to the P15-04 classifier.
_READINGS: Mapping[str, str] = {
    "space_not_found": "chat not found",
    "message_not_found": "message not found",
    "bad_request": "can't parse the request",
    "forbidden": "forbidden",
    "target_not_allowed": (
        "forbidden: this Photon line cannot start a conversation with that recipient"
    ),
    "auth_failed": "forbidden: Photon refused the project credentials",
    "too_large": "too long",
    "not_connected": "connection reset: Photon is not connected",
}
# Failures neither a retry nor a plain-text resend can fix.
_FINAL_MARKS = ("cannot start a conversation", "refused the project credentials")


def _items(value: object) -> list[str]:
    if isinstance(value, str):
        raw: Iterable[object] = value.replace("\n", ",").split(",")
    elif isinstance(value, list | tuple | set | frozenset):
        raw = value  # pyright: ignore[reportUnknownVariableType]
    else:
        return []
    return [str(v).strip() for v in raw if str(v).strip()]  # pyright: ignore[reportUnknownArgumentType, reportUnknownVariableType]


def _flag(value: object, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in _TRUTHY


def _patterns(value: object) -> tuple[re.Pattern[str], ...]:
    texts = _items(value) if value not in (None, "", []) else list(DEFAULT_MENTION_PATTERNS)
    found: list[re.Pattern[str]] = []
    for text in texts:
        try:
            found.append(re.compile(text, re.IGNORECASE))
        except re.error:
            log.warning("photon: mention pattern %r is not a regular expression; ignored", text)
    return tuple(found)


def _port(value: object) -> int | None:
    text = (
        str(value).strip() if isinstance(value, int | str) and not isinstance(value, bool) else ""
    )
    return int(text) if is_digits(text) else None


@dataclass(frozen=True)
class PhotonOptions:
    """``platforms.photon.*`` settings the adapter reads (all optional)."""

    provider: str = ""
    sidecar_port: int | None = None
    sidecar_script: str = ""
    require_mention: bool = False
    mention_patterns: tuple[re.Pattern[str], ...] = field(default_factory=lambda: _patterns(None))
    read_receipts: bool = True
    reactions: bool = False
    markdown: bool = True

    @classmethod
    def read(cls, extra: Mapping[str, Any], env: Mapping[str, str] | None = None) -> PhotonOptions:
        provider = str(extra.get("provider") or (env or {}).get("INDUS_PHOTON_PROVIDER") or "")
        return cls(
            provider=provider.strip(),
            sidecar_port=_port(extra.get("sidecar_port")),
            sidecar_script=str(extra.get("sidecar_script") or ""),
            require_mention=_flag(extra.get("require_mention"), False),
            mention_patterns=_patterns(extra.get("mention_patterns")),
            read_receipts=_flag(extra.get("read_receipts"), True),
            reactions=_flag(extra.get("reactions"), False),
            markdown=_flag(extra.get("markdown"), True),
        )


class SidecarLauncher(Protocol):
    """Who starts and stops the sidecar (``sidecar.PhotonSidecar``; tests pass their own)."""

    async def ensure(self) -> SidecarEndpoint: ...

    async def attach(self) -> SidecarEndpoint: ...

    def exited(self) -> bool: ...

    async def restart(self) -> SidecarEndpoint: ...

    async def stop(self) -> None: ...


def _dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}  # pyright: ignore[reportUnknownArgumentType]


def _list(value: object) -> list[Any]:
    return list(value) if isinstance(value, list) else []  # pyright: ignore[reportUnknownArgumentType]


async def ndjson_lines(chunks: AsyncIterator[bytes]) -> AsyncIterator[str]:
    """Lines of an NDJSON byte stream, split on LF only (a JSON line may hold U+2028 or other
    separators ``str.splitlines`` would cut at). A line over 1 MiB is dropped, never buffered."""
    pending = bytearray()
    skipping = False
    async for chunk in chunks:
        start = 0
        while True:
            end = chunk.find(b"\n", start)
            if end < 0:
                if not skipping:
                    pending += chunk[start:]
                    if len(pending) > MAX_LINE_BYTES:
                        log.warning("photon: an event line over 1 MiB was dropped")
                        pending.clear()
                        skipping = True
                break
            if skipping:
                skipping = False
            else:
                pending += chunk[start:end]
                yield pending.decode("utf-8", "replace")
            pending.clear()
            start = end + 1
    if pending and not skipping:
        yield pending.decode("utf-8", "replace")


def _url_only(text: str) -> str | None:
    candidate = text.strip()
    if not candidate or any(c.isspace() for c in candidate):
        return None
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return None
    if parsed.scheme.lower() in ("http", "https") and parsed.netloc:
        return candidate
    return None


@dataclass
class _Sidecar:
    """The connection in use: its endpoint, client, run and cursor."""

    endpoint: SidecarEndpoint
    client: httpx.AsyncClient
    epoch: str = ""
    cursor: int = 0
    started: float = 0.0


@dataclass
class _Held:
    """A placeholder waiting for its attachment."""

    item: dict[str, Any]
    words: str
    timer: asyncio.Task[None] | None = None


class PhotonAdapter(Adapter):
    platform: ClassVar[str] = "photon"
    capabilities: ClassVar[Capabilities] = Capabilities(
        max_len=MAX_LEN,
        len_fn=code_points,
        markup="markdown",
        splits_natively=True,
    )
    vendor_names: ClassVar[Mapping[str, tuple[str, ...]]] = {
        "httpx:AsyncClient": ("get", "post", "stream", "aclose"),
    }

    def __init__(
        self,
        *,
        clock: Clock,
        launcher: SidecarLauncher,
        options: PhotonOptions | None = None,
        settings: PlatformSettings | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        send_only: bool = False,
        secrets: Iterable[str] = (),
        sink: InboundSink | None = None,
    ) -> None:
        super().__init__(clock=clock, settings=settings, secrets=tuple(secrets), sink=sink)
        self.launcher = launcher
        self.options = options or PhotonOptions()
        # ``indus send`` (``AdapterConfig.send_only``): the running sidecar only, no stream.
        self.send_only = send_only
        self._transport = transport
        self._fixed_secrets = self._secrets
        self._sidecar: _Sidecar | None = None
        self._running = False
        self._streamer: asyncio.Task[None] | None = None
        self._watcher: asyncio.Task[None] | None = None
        self._restart_wait = RESTART_FIRST_S
        self._degraded = 0
        self._hung = 0
        self.seen = SeenIds(clock, DEDUPE_TTL_S, DEDUPE_MAX)
        # Our sent message ids → the start of their text (a reply to one quotes it).
        self._sent: OrderedDict[str, str] = OrderedDict()
        self._links: OrderedDict[str, float] = OrderedDict()
        self._held: dict[str, _Held] = {}
        self.restarts = 0

    def __repr__(self) -> str:
        state = "connected" if self.connected else "disconnected"
        return f"<PhotonAdapter {state}>"

    def scrub(self, text: str) -> str:
        return mask_numbers(super().scrub(text))

    # -- lifecycle ---------------------------------------------------------------------------------

    def _client(self, endpoint: SidecarEndpoint) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=endpoint.url,
            transport=self._transport or httpx.AsyncHTTPTransport(),
            trust_env=False,
            headers={"Authorization": f"Bearer {endpoint.token}"},
            timeout=httpx.Timeout(30.0, connect=5.0),
        )

    async def _attach(self, endpoint: SidecarEndpoint) -> _Sidecar:
        self._secrets = (*self._fixed_secrets, endpoint.token)
        sidecar = _Sidecar(endpoint, self._client(endpoint), started=self.clock.monotonic())
        try:
            await self._wait_open(sidecar)
        except BaseException:
            await sidecar.client.aclose()
            raise
        return sidecar

    async def _open(self, is_reconnect: bool) -> None:
        try:
            endpoint = await (self.launcher.attach() if self.send_only else self.launcher.ensure())
        except SidecarError as exc:
            raise AdapterConnectError(str(exc), retryable=exc.retryable, code=exc.code) from None
        try:
            self._sidecar = await self._attach(endpoint)
        except BaseException:
            if not self.send_only:
                await self.launcher.stop()
            raise
        self._running = True
        self._restart_wait = RESTART_FIRST_S
        self._degraded = 0
        if not self.send_only:
            self._streamer = asyncio.ensure_future(self._stream())
            self._watcher = asyncio.ensure_future(self._watch())
        log.info("photon: connected to the sidecar on port %d", endpoint.port)

    async def _close(self) -> None:
        self._running = False
        for task in (self._streamer, self._watcher):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._streamer = self._watcher = None
        for held in self._held.values():
            if held.timer is not None:
                held.timer.cancel()
        self._held.clear()
        sidecar, self._sidecar = self._sidecar, None
        if sidecar is not None:
            await sidecar.client.aclose()
        if not self.send_only:
            await self.launcher.stop()

    async def _wait_open(self, sidecar: _Sidecar) -> None:
        """Until the sidecar's link is up; refuses a refused login and a foreign sidecar."""
        waited = 0.0
        while True:
            try:
                response = await sidecar.client.get("/health", timeout=5.0)
            except httpx.HTTPError as exc:
                raise AdapterConnectError(
                    f"cannot reach the Photon sidecar: {type(exc).__name__}", retryable=True
                ) from None
            if response.status_code == 401:
                raise AdapterConnectError(
                    "the process on the sidecar port refused this profile's token",
                    retryable=False,
                    code="photon_sidecar_port_taken",
                )
            if response.status_code != 200:
                raise AdapterConnectError(
                    f"the Photon sidecar health check failed (HTTP {response.status_code})",
                    retryable=True,
                )
            body = _dict(response.json())
            sidecar.epoch = str(body.get("epoch") or "")
            status = str(body.get("status") or "")
            if status in ("open", "degraded"):
                return
            if status == "auth_failed":
                raise AdapterConnectError(
                    "Photon refused the project credentials (PHOTON_PROJECT_ID / "
                    "PHOTON_PROJECT_SECRET)",
                    retryable=False,
                    code="photon_auth",
                )
            if waited >= OPEN_WAIT_S:
                log.warning("photon: the sidecar is still connecting to Photon (%s)", status)
                return
            await self.clock.sleep(OPEN_CHECK_S)
            waited += OPEN_CHECK_S

    # -- the event stream --------------------------------------------------------------------------

    async def _stream(self) -> None:
        wait = STREAM_RETRY_FIRST_S
        while self._running:
            sidecar = self._sidecar
            if sidecar is None:
                await self._restart()
                continue
            lines = 0
            try:
                async with sidecar.client.stream(
                    "GET",
                    "/events",
                    params={"after": sidecar.cursor, "epoch": sidecar.epoch},
                    timeout=httpx.Timeout(10.0, connect=5.0, read=STREAM_IDLE_S),
                ) as response:
                    if response.status_code == 401:
                        self._running = False
                        self.report_fatal(
                            "photon_sidecar_port_taken",
                            "the process on the sidecar port no longer accepts this profile's "
                            "token",
                        )
                        return
                    if response.status_code != 200:
                        raise httpx.HTTPStatusError(
                            f"HTTP {response.status_code}",
                            request=response.request,
                            response=response,
                        )
                    epoch = response.headers.get("x-photon-epoch")
                    if epoch:
                        sidecar.epoch = epoch
                    wait = STREAM_RETRY_FIRST_S
                    async for line in ndjson_lines(response.aiter_bytes()):
                        lines += 1
                        if not self._running:
                            break
                        if line.strip():
                            await self._take(sidecar, line)
            except httpx.HTTPError as exc:
                if not self._running:
                    break
                if self.launcher.exited():
                    self._sidecar = None
                    await sidecar.client.aclose()
                    continue
                log.warning(
                    "photon: the event stream dropped (%s); reopening in %.0f s",
                    type(exc).__name__,
                    wait,
                )
                await self.clock.sleep(wait)
                wait = min(wait * 2, STREAM_RETRY_MAX_S)
                continue
            if lines == 0 and self._running:
                # A stream that ended without a line (not even a heartbeat): do not spin.
                await self.clock.sleep(STREAM_RETRY_FIRST_S)

    async def _take(self, sidecar: _Sidecar, line: str) -> None:
        try:
            item = _dict(json.loads(line))
        except ValueError:
            log.warning("photon: an event line was not JSON; skipped")
            return
        seq = item.get("seq")
        if isinstance(seq, int) and seq > sidecar.cursor:
            sidecar.cursor = seq
        if self.clock.monotonic() - sidecar.started >= STABLE_AFTER_S:
            self._restart_wait = RESTART_FIRST_S
        ident = str(item.get("id") or "")
        if not ident:
            return
        if not self.seen.first_time(ident):
            log.debug("photon: a repeated event was ignored")
            return
        try:
            await self._dispatch(item)
        except Exception as exc:
            # The type only: an error text could quote the message.
            log.warning("photon: an event could not be handled (%s)", type(exc).__name__)

    async def _restart(self) -> None:
        """Start the sidecar that died again, after a wait that doubles up to 60 s."""
        wait = self._restart_wait
        self._restart_wait = min(wait * 2, RESTART_MAX_S)
        log.warning("photon: the sidecar is gone; starting it again in %.0f s", wait)
        await self.clock.sleep(wait)
        if not self._running:
            return
        try:
            endpoint = await self.launcher.restart()
            self._sidecar = await self._attach(endpoint)
        except (SidecarError, AdapterConnectError) as exc:
            if not exc.retryable:
                self._running = False
                self.report_fatal(exc.code, str(exc))
                return
            log.warning("photon: the sidecar did not come back: %s", self.scrub(str(exc)))
            return
        self.restarts += 1
        self._degraded = self._hung = 0

    async def _watch(self) -> None:
        """``/health`` every 30 s: a refused login is fatal; a link reported degraded twice in a
        row, or a sidecar that let three health checks time out (a hung process), is restarted.
        Other failures prove nothing (a dead process is the stream loop's to find)."""
        while self._running:
            await self.clock.sleep(HEALTH_EVERY_S)
            sidecar = self._sidecar
            if not self._running or sidecar is None:
                continue
            try:
                response = await sidecar.client.get("/health", timeout=5.0)
                body = _dict(response.json()) if response.status_code == 200 else {}
            except httpx.TimeoutException:
                self._hung += 1
                if self._hung >= HUNG_LIMIT and self._sidecar is sidecar:
                    await self._replace(sidecar, "stopped answering its health check")
                continue
            except (httpx.HTTPError, ValueError):
                continue
            self._hung = 0
            status = str(body.get("status") or "")
            if status == "auth_failed":
                self._running = False
                self.report_fatal("photon_auth", "Photon refused the project credentials")
                return
            self._degraded = self._degraded + 1 if status == "degraded" else 0
            if self._degraded >= DEGRADED_LIMIT and self._sidecar is sidecar:
                await self._replace(sidecar, "reports its upstream link degraded")

    async def _replace(self, sidecar: _Sidecar, why: str) -> None:
        """Drop ``sidecar`` and let a fresh stream loop start a new one (with back-off)."""
        log.warning("photon: the sidecar %s; restarting it", why)
        self._degraded = self._hung = 0
        self._sidecar = None
        await sidecar.client.aclose()
        streamer = self._streamer
        if streamer is not None and not streamer.done():
            streamer.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await streamer
        self._streamer = asyncio.ensure_future(self._stream())

    # -- inbound -----------------------------------------------------------------------------------

    def _note_link(self, chat: str) -> None:
        self._links[chat] = self.clock.monotonic()
        self._links.move_to_end(chat)
        while len(self._links) > LINKS_MAX:
            self._links.popitem(last=False)

    def _recent_link(self, chat: str) -> bool:
        at = self._links.get(chat)
        return at is not None and self.clock.monotonic() - at <= PREVIEW_WINDOW_S

    def _note_sent(self, message_id: str | None, text: str = "") -> None:
        if not message_id:
            return
        self._sent[message_id] = text[:SENT_TEXT_MAX]
        self._sent.move_to_end(message_id)
        while len(self._sent) > SENT_MAX:
            self._sent.popitem(last=False)

    def _addressed(self, text: str) -> bool:
        if not self.options.require_mention or text.lstrip().startswith("/"):
            return True
        return any(p.search(text) for p in self.options.mention_patterns)

    def _without_wake_word(self, text: str) -> str:
        stripped = text.lstrip()
        for pattern in self.options.mention_patterns:
            found = pattern.match(stripped)
            if found:
                return stripped[found.end() :].lstrip(" ,:-") or text
        return text

    async def _dispatch(self, item: dict[str, Any]) -> None:
        space = _dict(item.get("space"))
        chat = str(space.get("id") or "")
        if not chat:
            return
        if not str(_dict(item.get("sender")).get("id") or ""):
            if space.get("type") == "group":
                return  # a group message from nobody in particular
            # A direct chat has one other person: its space names them.
            item = {**item, "sender": {**_dict(item.get("sender")), "id": chat}}
        if item.get("kind") == "reaction":
            await self._reaction(item)
            return
        text = str(item.get("text") or "")
        media = [_dict(m) for m in _list(item.get("media"))]
        metadata: dict[str, Any] = {}
        if PLACEHOLDER in text and not media:
            self._hold(chat, item, text.replace(PLACEHOLDER, "").strip())
            return
        text = text.replace(PLACEHOLDER, "").strip()
        held = self._held.pop(chat, None) if media else None
        if held is not None:
            if held.timer is not None:
                held.timer.cancel()
            text = "\n".join(t for t in (held.words, text) if t)
            metadata["placeholder_id"] = str(held.item.get("id") or "")
        previews = bool(media) and all(m.get("preview") is True for m in media)
        if previews and not text and self._recent_link(chat):
            log.info("photon: rich-link preview art was dropped")
            return
        await self._deliver(item, text, media, metadata)

    async def _deliver(
        self, item: dict[str, Any], text: str, media: list[dict[str, Any]], metadata: dict[str, Any]
    ) -> None:
        chat = str(_dict(item.get("space")).get("id") or "")
        group = _dict(item.get("space")).get("type") == "group"
        link = _dict(item.get("link"))
        url = str(link.get("url") or "")
        if url and url not in text:
            title, summary = str(link.get("title") or ""), str(link.get("summary") or "")
            parts = [text, title, summary if summary != title else "", url]
            text = "\n".join(p for p in parts if p)
        if group:
            if not self._addressed(text):
                return
            if self.options.require_mention:
                text = self._without_wake_word(text)
        event = self._event(item, text, media, metadata)
        if event is None:
            return
        if url or _URL.search(text):
            self._note_link(chat)
        await self.emit(event)
        if self.options.read_receipts and event.message_id:
            await self._post("/read", {"space": chat, "id": event.message_id}, timeout=5.0)

    def _attachments(
        self, media: list[dict[str, Any]]
    ) -> tuple[list[Attachment], MessageKind, list[str]]:
        """Attachments, the event kind, and a note for each one too large to fetch."""
        found: list[Attachment] = []
        notes: list[str] = []
        kind = MessageKind.TEXT
        for entry in media:
            ref = str(entry.get("ref") or "")
            if not _MEDIA_ID.fullmatch(ref):
                continue
            name = str(entry.get("name") or "")
            size = entry.get("size")
            if isinstance(size, int) and size > MAX_MEDIA_BYTES:
                log.warning("photon: an attachment of %d bytes was skipped (limit 100 MiB)", size)
                notes.append(f"[an attachment too large to fetch: {name or 'unnamed'}]")
                continue
            full = MEDIA_REF + ref + (f"#{quote(name, safe='')}" if name else "")
            if not found:
                kind = _KINDS.get(str(entry.get("kind") or ""), MessageKind.DOCUMENT)
            found.append(Attachment(ref=full, mime=str(entry.get("mime") or "")))
        return found, kind, notes

    def _event(
        self,
        item: dict[str, Any],
        text: str,
        media: list[dict[str, Any]],
        metadata: dict[str, Any],
        *,
        reply: Mapping[str, Any] | None = None,
    ) -> InboundEvent | None:
        space = _dict(item.get("space"))
        sender = _dict(item.get("sender"))
        group = space.get("type") == "group"
        attachments, kind, notes = self._attachments(media)
        text = "\n".join(t for t in (text, *notes) if t)
        if not text.strip() and not attachments:
            return None
        quoted = _dict(item.get("reply") if reply is None else reply)
        quoted_id = str(quoted.get("id") or "") or None
        ours = self._sent.get(quoted_id) if quoted_id is not None else None
        own = quoted.get("fromUs") is True or ours is not None
        name = str(sender.get("name") or "").strip()
        user = str(sender.get("id") or "")
        stamp = item.get("at")
        source = Source(
            platform=self.platform,
            chat_id=str(space.get("id") or ""),
            chat_type="group" if group else "dm",
            chat_name=(str(space.get("name") or "") or None) if group else (name or None),
            user_id=user,
            user_name=name or user,
        )
        return InboundEvent(
            source=source,
            text=text,
            kind=kind,
            message_id=str(item.get("id") or "") or None,
            media=tuple(attachments),
            reply_to_id=quoted_id,
            reply_to_text=str(quoted.get("text") or "") or ours or None,
            reply_to_is_own=own,
            metadata=metadata,
            timestamp=(
                datetime.fromtimestamp(stamp / 1000, tz=UTC)
                if isinstance(stamp, int | float) and stamp > 0
                else None
            ),
        )

    async def _reaction(self, item: dict[str, Any]) -> None:
        """A tapback reaches the agent only with ``reactions`` on, and only on our messages."""
        if not self.options.reactions:
            return
        reaction = _dict(item.get("reaction"))
        target = str(reaction.get("target") or "")
        emoji = str(reaction.get("emoji") or "")
        if not target or not emoji:
            return
        if reaction.get("targetFromUs") is not True and target not in self._sent:
            return
        text = f"[reacted {emoji} to your message]"
        quoted = {"id": target, "text": reaction.get("targetText") or "", "fromUs": True}
        event = self._event(item, text, [], {"reaction": emoji}, reply=quoted)
        if event is not None:
            await self.emit(event.model_copy(update={"allow_control": False}))

    def _hold(self, chat: str, item: dict[str, Any], words: str) -> None:
        """A placeholder: wait up to 15 s for the attachment it stands for."""
        previous = self._held.pop(chat, None)
        if previous is not None:
            if previous.timer is not None:
                previous.timer.cancel()
            words = "\n".join(w for w in (previous.words, words) if w)
        held = _Held(item, words)
        held.timer = asyncio.ensure_future(self._expire(chat, held))
        self._held[chat] = held

    async def _expire(self, chat: str, held: _Held) -> None:
        await self.clock.sleep(PLACEHOLDER_WAIT_S)
        if self._held.get(chat) is not held:
            return
        del self._held[chat]
        if not held.words:
            log.warning("photon: an attachment placeholder came without its attachment")
            return
        try:
            await self._deliver(held.item, held.words, [], {})
        except Exception as exc:
            log.warning("photon: a held message could not be handled (%s)", type(exc).__name__)

    def register_fetchers(self, seat: FetcherSeat) -> Callable[[], None] | None:
        """Register `fetch_media` with the media intake (P15-23); returns the undo."""
        return seat.register_fetcher(self.platform, self.fetch_media)

    async def fetch_media(self, attachment: Attachment) -> AsyncIterator[bytes]:
        """The intake's fetcher: one attachment's bytes from the sidecar."""
        if not attachment.ref.startswith(MEDIA_REF):
            raise MediaRejected("it is not a Photon attachment")
        ref = unquote(attachment.ref[len(MEDIA_REF) :].split("#", 1)[0])
        sidecar = self._sidecar
        if not _MEDIA_ID.fullmatch(ref) or sidecar is None:
            raise MediaRejected("it could not be downloaded")
        total = 0
        async with sidecar.client.stream(
            "GET", "/media", params={"ref": ref}, timeout=httpx.Timeout(120.0, connect=5.0)
        ) as response:
            if response.status_code == 413:
                raise MediaRejected("it is larger than 100 MB")
            if response.status_code != 200:
                raise MediaRejected("it is no longer available")
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > MAX_MEDIA_BYTES:
                    raise MediaRejected("it is larger than 100 MB")
                yield chunk
        if total == 0:
            raise MediaRejected("it is empty")

    # -- outbound ----------------------------------------------------------------------------------

    async def _post(
        self, path: str, body: Mapping[str, Any], *, timeout: float = 60.0
    ) -> SendResult:
        """One sidecar call as a result: ``sent`` with the message id, else a classified failure."""
        sidecar = self._sidecar
        if sidecar is None:
            return SendResult.failure("connection reset: the Photon sidecar is not connected")
        try:
            response = await sidecar.client.post(
                path, json=dict(body), timeout=httpx.Timeout(timeout, connect=5.0)
            )
        except httpx.HTTPError as exc:
            return failure_from_exception(exc)
        if response.status_code == 200:
            ident = str(_dict(response.json()).get("id") or "")
            return SendResult.sent(ident) if ident else SendResult(ok=True)
        try:
            code = str(_dict(response.json()).get("error") or "")
        except ValueError:
            code = ""
        if response.status_code == 504:
            return SendResult.failure(
                "read timeout: the sidecar's Photon call timed out; the message may be out"
            )
        return failure_from_status(
            response.status_code,
            code,
            response.headers,
            now=self.clock.now(),
            reading=_READINGS.get(code),
        )

    def is_final_failure(self, result: SendResult) -> bool:
        error = (result.error or "").lower()
        return not result.ok and any(mark in error for mark in _FINAL_MARKS)

    @staticmethod
    def _partial(result: SendResult, ids: list[str], remainder: str) -> SendResult:
        if not ids:
            return result
        return replace(
            result,
            message_id=ids[-1],
            continuation_ids=tuple(ids[:-1]),
            partial=True,
            remainder=None if result.maybe_delivered else remainder,
        )

    def _wire_text(self, piece: str, plain: bool) -> tuple[str, str]:
        """The text and format of one piece: markdown unless asked plain, turned off, or the
        piece holds a URL (the markers would show around it)."""
        if plain:
            return piece, "plain"
        if not self.options.markdown or _URL.search(piece):
            return strip_markdown(piece), "plain"
        return piece, "markdown"

    async def _send(
        self, chat_id: str, text: str, reply_to: str | None, meta: dict[str, Any]
    ) -> SendResult:
        plain = meta.get("plain") is True
        url = _url_only(text) if self.options.markdown and not plain else None
        if url is not None:
            result = await self._post("/send-link", {"space": chat_id, "url": url})
            if result.ok:
                self._note_sent(result.message_id, url)
                self._note_link(chat_id)
                return result
            if self.is_final_failure(result) or result.maybe_delivered:
                return result
            log.warning("photon: a link send failed; sending it as plain text")
            plain = True
        chunked = split_message(text, MAX_LEN, len_fn=code_points, marker=plain_marker)
        ids: list[str] = []
        for n, chunk in enumerate(chunked.chunks):
            if n:
                await self.slots.pace(chat_id)
            wire, fmt = self._wire_text(chunk.text, plain)
            result = await self._post("/send", {"space": chat_id, "text": wire, "format": fmt})
            if not result.ok or result.message_id is None:
                return self._partial(result, ids, chunked.remainder(n))
            ids.append(result.message_id)
            self._note_sent(result.message_id, chunk.text)
            if _URL.search(wire):
                self._note_link(chat_id)
        return SendResult.sent(ids[-1], tuple(ids[:-1]))

    async def _send_media(
        self, kind: MediaKind, chat_id: str, path: str, caption: str | None, meta: dict[str, Any]
    ) -> SendResult:
        file = Path(path).resolve()
        body: dict[str, Any] = {"space": chat_id, "path": str(file), "voice": kind == "voice"}
        if kind in ("file", "document"):
            body["name"] = file.name
        mime = mimetypes.guess_type(file.name)[0]
        if mime:
            body["mime"] = mime
        result = await self._post("/send-file", body, timeout=180.0)
        if not result.ok:
            return result
        self._note_sent(result.message_id, f"[{file.name}]")
        if caption and caption.strip():
            wire, fmt = self._wire_text(caption, False)
            after = await self._post("/send", {"space": chat_id, "text": wire, "format": fmt})
            if after.ok:
                self._note_sent(after.message_id, caption)
            else:
                log.warning("photon: a caption after a file was not sent")
        return result

    async def _typing(self, chat_id: str, meta: dict[str, Any]) -> None:
        await self._post("/typing", {"space": chat_id, "on": True}, timeout=5.0)

    async def _typing_stop(self, chat_id: str) -> None:
        await self._post("/typing", {"space": chat_id, "on": False}, timeout=5.0)

    async def react(self, chat_id: str, message_id: str, emoji: str) -> bool:
        """Tapback ``emoji`` on a message; False when it did not work."""
        result = await self._post(
            "/react", {"space": chat_id, "id": message_id, "emoji": emoji}, timeout=10.0
        )
        if not result.ok:
            log.debug("photon: reaction failed: %s", self.scrub(result.error or ""))
        return result.ok

    async def unreact(self, chat_id: str, message_id: str) -> bool:
        """Take our tapback off a message; False when it did not work."""
        result = await self._post("/unreact", {"space": chat_id, "id": message_id}, timeout=10.0)
        return result.ok
