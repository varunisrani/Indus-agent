"""WhatsApp through our own bridge process (P16-03; the bridge is `apps/whatsapp-bridge`, P16-02).

The adapter owns the bridge (``sidecar.SidecarOwner``: adopt this profile's running bridge or
start one, pidfile identity, never touch another profile's) and talks to it over loopback HTTP
with the bridge's per-start bearer token:

- connect: the bridge must report ``open``; ``pairing`` is a fatal ``whatsapp_not_paired`` (pair
  with ``indus gateway setup whatsapp``), ``logged_out`` a fatal ``whatsapp_logged_out``; a bridge
  still connecting after 30 s is accepted with a warning (sends answer "not connected" until it is);
- inbound: ``GET /events`` long-polls (25 s) with a cursor; a new bridge run (another ``epoch``)
  restarts the cursor. A bridge that died is started again after 2 s, doubling to 60 s; the wait
  starts over once a bridge stayed up 60 s;
- outbound: ``/send`` per piece of ≤4,096 code points (Markdown turned into WhatsApp markup),
  ``/send-media`` by file path, ``/typing``, ``/read``, ``/react``. No edits, no buttons: streaming
  falls back to segments and approvals are the text card.

Access: the adapter has its own DM and group policies (``dm_policy`` ``pairing`` by default,
``group_policy`` ``open``; ``open``, ``allowlist``, ``pairing``, ``disabled``) and the core asks it
(``owns_access_policy``). Senders are normalised through ``_identity.WhatsAppIdentities`` so a
phone JID and its LID are one person; allowlists match any alias. In groups a message passes only
when it is addressed to us (``require_mention``, default on): a slash command, a mention of us, a
reply to one of our messages, a ``mention_patterns`` match or a ``free_response_chats`` chat.

Media are fetched lazily (``register_fetchers``): an event carries ``wa-media:<ref>`` refs and the
intake downloads only what it admits; text documents up to 100 KiB are inlined and voice notes
transcribed there. Phone numbers are masked in every log line and error text; message text is
never logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol
from urllib.parse import quote, unquote

import httpx

from indus.foundation.clock import Clock
from indus.gateway.adapters._http import failure_from_exception, failure_from_status
from indus.gateway.adapters._identity import (
    WhatsAppIdentities,
    is_whatsapp_broadcast,
    mask_numbers,
    strip_whatsapp_device,
    to_whatsapp_jid,
    whatsapp_user_id,
)
from indus.gateway.adapters.base import (
    REPLY_TO_KEY,
    Adapter,
    AdapterConnectError,
    Capabilities,
    FetcherSeat,
    InboundSink,
    MediaKind,
)
from indus.gateway.adapters.whatsapp.sidecar import BridgeEndpoint, BridgeError, Health, ProbeFn
from indus.gateway.adapters.whatsapp_cloud.markup import to_whatsapp
from indus.gateway.core.events import Attachment, InboundEvent, MessageKind, Source
from indus.gateway.core.send import SendResult
from indus.gateway.intake.media import MediaRejected
from indus.gateway.outbound.chunker import code_points, plain_marker, split_message

if TYPE_CHECKING:
    from indus.gateway.config import PlatformSettings

__all__ = [
    "MAX_LEN",
    "MEDIA_REF",
    "BridgeLauncher",
    "WhatsAppAdapter",
    "WhatsAppOptions",
    "http_probe",
]

log = logging.getLogger(__name__)

MAX_LEN = 4096
POLL_WAIT_S = 25.0
OPEN_WAIT_S = 30.0
OPEN_CHECK_S = 0.5
POLL_PAUSE_S = 1.0
RESTART_FIRST_S = 2.0
RESTART_MAX_S = 60.0
STABLE_AFTER_S = 60.0
MAPPING_RELOAD_S = 60.0
MAX_MEDIA_BYTES = 100 * 1024 * 1024
MEDIA_REF = "wa-media:"
_MEDIA_ID = re.compile(r"[0-9a-f]{24}")

type Policy = Literal["open", "allowlist", "pairing", "disabled"]
_POLICIES: frozenset[str] = frozenset({"open", "allowlist", "pairing", "disabled"})
_TRUTHY = frozenset({"1", "true", "yes", "on"})

_KINDS: Mapping[str, MessageKind] = {
    "image": MessageKind.PHOTO,
    "video": MessageKind.VIDEO,
    "voice": MessageKind.VOICE,
    "audio": MessageKind.AUDIO,
    "document": MessageKind.DOCUMENT,
    "sticker": MessageKind.STICKER,
}
_OUTBOUND: Mapping[MediaKind, str] = {
    "image": "image",
    "video": "video",
    "voice": "voice",
    "file": "document",
    "document": "document",
}
# How the bridge's error codes read to the P15-04 classifier.
_READINGS: Mapping[str, str] = {
    "chat_not_found": "chat not found",
    "message_not_found": "message not found",
    "bad_request": "can't parse the request",
    "forbidden": "forbidden",
    "too_large": "too long",
    "not_connected": "connection reset: WhatsApp is not connected",
}


def _items(value: object) -> list[str]:
    if isinstance(value, str):
        raw: Iterable[object] = value.replace("\n", ",").split(",")
    elif isinstance(value, list | tuple | set | frozenset):
        raw = value  # pyright: ignore[reportUnknownVariableType]
    else:
        return []
    return [str(v).strip() for v in raw if str(v).strip()]  # pyright: ignore[reportUnknownArgumentType, reportUnknownVariableType]


def _policy(value: object, default: Policy) -> Policy:
    text = str(value or "").strip().lower()
    return text if text in _POLICIES else default  # pyright: ignore[reportReturnType]


def _flag(value: object, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in _TRUTHY


def _patterns(value: object) -> tuple[re.Pattern[str], ...]:
    found: list[re.Pattern[str]] = []
    for text in _items(value) if not isinstance(value, str) else [value]:
        try:
            found.append(re.compile(text, re.IGNORECASE))
        except re.error:
            log.warning("whatsapp: mention pattern %r is not a regular expression; ignored", text)
    return tuple(found)


@dataclass(frozen=True)
class WhatsAppOptions:
    """``platforms.whatsapp.*`` settings the adapter reads (all optional)."""

    dm_policy: Policy = "pairing"
    group_policy: Policy = "open"
    allow_from: tuple[str, ...] = ()
    group_allow_from: tuple[str, ...] = ()
    require_mention: bool = True
    mention_patterns: tuple[re.Pattern[str], ...] = ()
    free_response_chats: frozenset[str] = frozenset()
    send_read_receipts: bool = False
    self_chat: bool = False
    bridge_port: int | None = None
    bridge_script: str = ""
    provider: str = ""

    @classmethod
    def read(
        cls, extra: Mapping[str, Any], env: Mapping[str, str] | None = None
    ) -> WhatsAppOptions:
        port = extra.get("bridge_port")
        provider = str(extra.get("provider") or (env or {}).get("INDUS_WHATSAPP_PROVIDER") or "")
        return cls(
            dm_policy=_policy(extra.get("dm_policy"), "pairing"),
            group_policy=_policy(extra.get("group_policy"), "open"),
            allow_from=tuple(_items(extra.get("allow_from"))),
            group_allow_from=tuple(_items(extra.get("group_allow_from"))),
            require_mention=_flag(extra.get("require_mention"), True),
            mention_patterns=_patterns(extra.get("mention_patterns")),
            free_response_chats=frozenset(
                strip_whatsapp_device(c) for c in _items(extra.get("free_response_chats"))
            ),
            send_read_receipts=_flag(extra.get("send_read_receipts"), False),
            self_chat=_flag(extra.get("self_chat"), False),
            bridge_port=int(port) if isinstance(port, int | str) and str(port).isdigit() else None,
            bridge_script=str(extra.get("bridge_script") or ""),
            provider=provider.strip(),
        )


class BridgeLauncher(Protocol):
    """Who starts and stops the bridge (``sidecar.SidecarOwner``; tests pass their own)."""

    async def ensure(self) -> BridgeEndpoint: ...

    async def attach(self) -> BridgeEndpoint: ...

    def exited(self) -> bool: ...

    async def restart(self) -> BridgeEndpoint: ...

    async def stop(self) -> None: ...


def _dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}  # pyright: ignore[reportUnknownArgumentType]


def http_probe(transport: httpx.AsyncBaseTransport | None = None) -> ProbeFn:
    """``/health`` of a bridge at ``url`` with ``token`` (see ``sidecar.Health``)."""

    async def probe(url: str, token: str) -> Health:
        async with httpx.AsyncClient(
            transport=transport or httpx.AsyncHTTPTransport(),
            trust_env=False,
            timeout=httpx.Timeout(3.0),
        ) as client:
            try:
                response = await client.get(
                    f"{url}/health", headers={"Authorization": f"Bearer {token}"}
                )
            except httpx.HTTPError:
                return Health("unreachable")
            if response.status_code == 401:
                return Health("unauthorized")
            if response.status_code != 200:
                return Health("unreachable")
            try:
                body = _dict(response.json())
            except ValueError:
                return Health("unreachable")
            own = _dict(body.get("self"))
            return Health(
                "ok",
                status=str(body.get("status") or ""),
                profile=str(body.get("profile") or ""),
                epoch=str(body.get("epoch") or ""),
                self_jid=str(own.get("jid") or ""),
                self_lid=str(own.get("lid") or ""),
            )

    return probe


@dataclass
class _Bridge:
    """The connection in use: its endpoint, client and what its ``/health`` said."""

    endpoint: BridgeEndpoint
    client: httpx.AsyncClient
    self_ids: tuple[str, ...] = ()
    epoch: str = ""
    cursor: int = 0
    started: float = 0.0


class WhatsAppAdapter(Adapter):
    platform: ClassVar[str] = "whatsapp"
    capabilities: ClassVar[Capabilities] = Capabilities(
        max_len=MAX_LEN,
        len_fn=code_points,
        # The sender's Markdown pass; each piece is turned into WhatsApp markup here.
        markup="markdown",
        splits_natively=True,
        owns_access_policy=True,
        # A short gap between pieces sent to one chat (WhatsApp limits bursts).
        slot_spacing=0.3,
    )
    vendor_names: ClassVar[Mapping[str, tuple[str, ...]]] = {
        "httpx:AsyncClient": ("get", "post", "stream", "aclose"),
    }

    def __init__(
        self,
        *,
        clock: Clock,
        launcher: BridgeLauncher,
        options: WhatsAppOptions | None = None,
        settings: PlatformSettings | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        send_only: bool = False,
        auth_dir: Path | None = None,
        identities: WhatsAppIdentities | None = None,
        sink: InboundSink | None = None,
    ) -> None:
        super().__init__(clock=clock, settings=settings, sink=sink)
        self.launcher = launcher
        self.options = options or WhatsAppOptions()
        # ``indus send`` (``AdapterConfig.send_only``): the running bridge only, no polling.
        self.send_only = send_only
        self.auth_dir = auth_dir
        self.identities = identities or WhatsAppIdentities()
        self._transport = transport
        self._fixed_secrets = self._secrets
        self._bridge: _Bridge | None = None
        self._running = False
        self._poller: asyncio.Task[None] | None = None
        self._restart_wait = RESTART_FIRST_S
        self._mapping_at: float | None = None
        self.restarts = 0

    def __repr__(self) -> str:
        state = "connected" if self.connected else "disconnected"
        return f"<WhatsAppAdapter {state}>"

    def scrub(self, text: str) -> str:
        return mask_numbers(super().scrub(text))

    # -- policy (read by the authorization chain) --------------------------------------------------

    def access_policy(self, kind: Literal["dm", "group"]) -> str:
        return self.options.dm_policy if kind == "dm" else self.options.group_policy

    def allows_dm(self, user_id: str) -> bool:
        return self.identities.allowlist_matches(user_id, self.options.allow_from)

    # -- lifecycle ---------------------------------------------------------------------------------

    def _client(self, endpoint: BridgeEndpoint) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=endpoint.url,
            transport=self._transport or httpx.AsyncHTTPTransport(),
            trust_env=False,
            headers={"Authorization": f"Bearer {endpoint.token}"},
            timeout=httpx.Timeout(30.0, connect=5.0),
        )

    async def _attach(self, endpoint: BridgeEndpoint) -> _Bridge:
        self._secrets = (*self._fixed_secrets, endpoint.token)
        bridge = _Bridge(endpoint, self._client(endpoint), started=self.clock.monotonic())
        try:
            await self._wait_open(bridge)
        except BaseException:
            await bridge.client.aclose()
            raise
        return bridge

    async def _open(self, is_reconnect: bool) -> None:
        try:
            endpoint = await (self.launcher.attach() if self.send_only else self.launcher.ensure())
        except BridgeError as exc:
            raise AdapterConnectError(str(exc), retryable=exc.retryable, code=exc.code) from None
        try:
            self._bridge = await self._attach(endpoint)
        except BaseException:
            if not self.send_only:
                await self.launcher.stop()
            raise
        self._running = True
        self._restart_wait = RESTART_FIRST_S
        if not self.send_only:
            self._reload_mapping(force=True)
            self._poller = asyncio.ensure_future(self._poll())
        log.info("whatsapp: connected to the bridge on port %d", endpoint.port)

    async def _close(self) -> None:
        self._running = False
        poller, self._poller = self._poller, None
        if poller is not None and not poller.done():
            poller.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await poller
        bridge, self._bridge = self._bridge, None
        if bridge is not None:
            await bridge.client.aclose()
        if not self.send_only:
            await self.launcher.stop()

    async def _wait_open(self, bridge: _Bridge) -> None:
        """Until the bridge says ``open``; refuses an unpaired or logged-out session."""
        waited = 0.0
        while True:
            try:
                response = await bridge.client.get("/health", timeout=5.0)
            except httpx.HTTPError as exc:
                raise AdapterConnectError(
                    f"cannot reach the WhatsApp bridge: {type(exc).__name__}", retryable=True
                ) from None
            if response.status_code == 401:
                raise AdapterConnectError(
                    "the process on the bridge port refused this profile's token",
                    retryable=False,
                    code="whatsapp_bridge_port_taken",
                )
            if response.status_code != 200:
                raise AdapterConnectError(
                    f"the WhatsApp bridge health check failed (HTTP {response.status_code})",
                    retryable=True,
                )
            body = _dict(response.json())
            own = _dict(body.get("self"))
            bridge.self_ids = tuple(
                x for x in (str(own.get("jid") or ""), str(own.get("lid") or "")) if x
            )
            bridge.epoch = str(body.get("epoch") or "")
            status = str(body.get("status") or "")
            if status == "open":
                return
            if status == "pairing":
                raise AdapterConnectError(
                    "WhatsApp is not paired with this profile: run `indus gateway setup whatsapp`",
                    retryable=False,
                    code="whatsapp_not_paired",
                )
            if status == "logged_out":
                raise AdapterConnectError(
                    "the WhatsApp session was logged out on the phone: pair again with "
                    "`indus gateway setup whatsapp`",
                    retryable=False,
                    code="whatsapp_logged_out",
                )
            if waited >= OPEN_WAIT_S:
                log.warning("whatsapp: the bridge is still connecting to WhatsApp (%s)", status)
                return
            await self.clock.sleep(OPEN_CHECK_S)
            waited += OPEN_CHECK_S

    # -- the event poll ----------------------------------------------------------------------------

    async def _poll(self) -> None:
        while self._running:
            bridge = self._bridge
            if bridge is None:
                await self._restart()
                continue
            try:
                response = await bridge.client.get(
                    "/events",
                    params={"after": bridge.cursor, "epoch": bridge.epoch, "wait": POLL_WAIT_S},
                    timeout=httpx.Timeout(POLL_WAIT_S + 10.0, connect=5.0),
                )
            except httpx.HTTPError as exc:
                if self.launcher.exited():
                    self._bridge = None
                    await bridge.client.aclose()
                    continue
                log.debug("whatsapp: event poll failed: %s", type(exc).__name__)
                await self.clock.sleep(POLL_PAUSE_S)
                continue
            if response.status_code == 401:
                self._running = False
                self.report_fatal(
                    "whatsapp_bridge_port_taken",
                    "the process on the bridge port no longer accepts this profile's token",
                )
                return
            if response.status_code != 200:
                await self.clock.sleep(POLL_PAUSE_S)
                continue
            await self._take_page(bridge, _dict(response.json()))

    async def _take_page(self, bridge: _Bridge, page: dict[str, Any]) -> None:
        cursor = page.get("cursor")
        bridge.epoch = str(page.get("epoch") or "")
        if isinstance(cursor, int) and cursor >= 0:
            bridge.cursor = cursor
        if self.clock.monotonic() - bridge.started >= STABLE_AFTER_S:
            self._restart_wait = RESTART_FIRST_S
        events = page.get("events")
        for raw in events if isinstance(events, list) else []:  # pyright: ignore[reportUnknownVariableType]
            item = _dict(raw)
            try:
                event = self.normalise(item)
            except Exception as exc:
                # The type only: an error text could quote the message.
                log.warning("whatsapp: a bridge event could not be read (%s)", type(exc).__name__)
                continue
            if event is None:
                continue
            await self.emit(event)
            if self.options.send_read_receipts and not item.get("fromMe"):
                await self._read_receipt(item)

    async def _restart(self) -> None:
        """Start the bridge that died again, after a wait that doubles up to 60 s."""
        wait = self._restart_wait
        self._restart_wait = min(wait * 2, RESTART_MAX_S)
        log.warning("whatsapp: the bridge is gone; starting it again in %.0f s", wait)
        await self.clock.sleep(wait)
        if not self._running:
            return
        try:
            endpoint = await self.launcher.restart()
            self._bridge = await self._attach(endpoint)
        except (BridgeError, AdapterConnectError) as exc:
            if not exc.retryable:
                self._running = False
                self.report_fatal(exc.code, str(exc))
                return
            log.warning("whatsapp: the bridge did not come back: %s", self.scrub(str(exc)))
            return
        self.restarts += 1
        self._reload_mapping(force=True)

    # -- inbound -----------------------------------------------------------------------------------

    def _reload_mapping(self, *, force: bool = False) -> None:
        if self.auth_dir is None:
            return
        now = self.clock.monotonic()
        if not force and self._mapping_at is not None and now - self._mapping_at < MAPPING_RELOAD_S:
            return
        self._mapping_at = now
        self.identities.load_mapping_dir(self.auth_dir)

    def _own(self, jid: str | None) -> bool:
        bridge = self._bridge
        if bridge is None or not jid:
            return False
        return any(self.identities.same_user(jid, own) for own in bridge.self_ids)

    def _own_digits(self) -> list[str]:
        bridge = self._bridge
        found = [whatsapp_user_id(j) for j in (bridge.self_ids if bridge is not None else ())]
        return [d for d in found if d]

    def _group_allowed(self, chat: str, sender: str) -> bool:
        entries = self.options.group_allow_from
        if chat in entries or "*" in entries:
            return True
        return self.identities.allowlist_matches(sender, entries)

    def _addressed(self, item: Mapping[str, Any], chat: str, text: str) -> bool:
        """A group message counts only when it is meant for us (see the module notes)."""
        if chat in self.options.free_response_chats or not self.options.require_mention:
            return True
        if text.lstrip().startswith("/") or item.get("mentionsMe") is True:
            return True
        if _dict(item.get("quoted")).get("fromMe") is True:
            return True
        if any(f"@{digits}" in text for digits in self._own_digits()):
            return True
        return any(p.search(text) for p in self.options.mention_patterns)

    def _without_own_mention(self, text: str) -> str:
        cleaned = text
        for digits in self._own_digits():
            cleaned = re.sub(rf"@{re.escape(digits)}\b[,:\-]*\s*", "", cleaned)
        return cleaned.strip() or text

    def _attachments(self, item: Mapping[str, Any]) -> tuple[list[Attachment], MessageKind]:
        found: list[Attachment] = []
        kind = MessageKind.TEXT
        media = item.get("media")
        for raw in media if isinstance(media, list) else []:  # pyright: ignore[reportUnknownVariableType]
            entry = _dict(raw)
            ref = str(entry.get("ref") or "")
            if not _MEDIA_ID.fullmatch(ref):
                continue
            size = entry.get("size")
            if isinstance(size, int) and size > MAX_MEDIA_BYTES:
                log.warning("whatsapp: an attachment of %d bytes was skipped (limit 100 MiB)", size)
                continue
            name = str(entry.get("name") or "")
            full = MEDIA_REF + ref + (f"#{quote(name, safe='')}" if name else "")
            if not found:
                kind = _KINDS.get(str(entry.get("kind") or ""), MessageKind.DOCUMENT)
            found.append(Attachment(ref=full, mime=str(entry.get("mime") or "")))
        return found, kind

    def normalise(self, item: Mapping[str, Any]) -> InboundEvent | None:
        """One bridge event as an `InboundEvent`, or None when it is dropped."""
        chat = strip_whatsapp_device(str(item.get("chat") or ""))
        sender = strip_whatsapp_device(str(item.get("sender") or ""))
        if not chat or not sender or is_whatsapp_broadcast(chat):
            return None
        alt = strip_whatsapp_device(str(item.get("senderAlt") or ""))
        if alt:
            phone, lid = (alt, sender) if sender.endswith("@lid") else (sender, alt)
            self.identities.link(phone, lid)
        if sender.endswith("@lid") and len(self.identities.aliases(sender)) < 2:
            self._reload_mapping()
        user = self.identities.canonical(sender)
        if user is None:
            return None
        group = chat.endswith("@g.us") or item.get("group") is True
        text = str(item.get("text") or "")
        if group:
            if self.options.group_policy == "disabled":
                return None
            if self.options.group_policy == "allowlist" and not self._group_allowed(chat, sender):
                log.debug("whatsapp: a group message from a sender not on the list was dropped")
                return None
            if not self._addressed(item, chat, text):
                return None
            text = self._without_own_mention(text)
        elif self.options.dm_policy == "disabled":
            return None
        media, kind = self._attachments(item)
        if not text.strip() and not media:
            return None
        metadata: dict[str, Any] = {}
        if item.get("edited") is True:
            metadata["edited"] = True
        if item.get("mentionsMe") is True:
            metadata["mentioned"] = True
        if item.get("fromMe") is True:
            metadata["note_to_self"] = True
        quoted = _dict(item.get("quoted"))
        quoted_author = strip_whatsapp_device(str(quoted.get("sender") or ""))
        others = sorted(self.identities.aliases(sender) - {user})
        name = str(item.get("senderName") or "").strip()
        stamp = item.get("timestamp")
        source = Source(
            platform=self.platform,
            chat_id=chat,
            chat_type="group" if group else "dm",
            chat_name=(str(item.get("chatName") or "") or None) if group else (name or None),
            user_id=user,
            user_name=name or user,
            user_id_alt=others[0] if others else None,
        )
        return InboundEvent(
            source=source,
            text=text,
            kind=kind,
            message_id=str(item.get("id") or "") or None,
            media=tuple(media),
            reply_to_id=str(quoted.get("id") or "") or None,
            reply_to_text=str(quoted.get("text") or "") or None,
            reply_to_author_id=(
                self.identities.canonical(quoted_author) if quoted_author else None
            ),
            reply_to_is_own=quoted.get("fromMe") is True,
            metadata=metadata,
            timestamp=(
                datetime.fromtimestamp(stamp, tz=UTC)
                if isinstance(stamp, int | float) and stamp > 0
                else None
            ),
        )

    async def _read_receipt(self, item: Mapping[str, Any]) -> None:
        chat = str(item.get("chat") or "")
        body: dict[str, Any] = {"chat": chat, "id": str(item.get("id") or "")}
        if chat.endswith("@g.us"):
            body["participant"] = str(item.get("sender") or "")
        with contextlib.suppress(Exception):
            await self._post("/read", body, timeout=5.0)

    def register_fetchers(self, seat: FetcherSeat) -> Callable[[], None] | None:
        """Register `fetch_media` with the media intake (P15-23); returns the undo."""
        return seat.register_fetcher(self.platform, self.fetch_media)

    async def fetch_media(self, attachment: Attachment) -> AsyncIterator[bytes]:
        """The intake's fetcher: one attachment's bytes from the bridge."""
        if not attachment.ref.startswith(MEDIA_REF):
            raise MediaRejected("it is not a WhatsApp attachment")
        ref = unquote(attachment.ref[len(MEDIA_REF) :].split("#", 1)[0])
        bridge = self._bridge
        if not _MEDIA_ID.fullmatch(ref) or bridge is None:
            raise MediaRejected("it could not be downloaded")
        total = 0
        async with bridge.client.stream(
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
        """One bridge call as a result: ``sent`` with the message id, else a classified failure."""
        bridge = self._bridge
        if bridge is None:
            return SendResult.failure("connection reset: the WhatsApp bridge is not connected")
        try:
            response = await bridge.client.post(
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
                "read timeout: the bridge's WhatsApp call timed out; the message may be out"
            )
        return failure_from_status(
            response.status_code,
            code,
            response.headers,
            now=self.clock.now(),
            reading=_READINGS.get(code),
        )

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

    async def _send(
        self, chat_id: str, text: str, reply_to: str | None, meta: dict[str, Any]
    ) -> SendResult:
        chat = to_whatsapp_jid(chat_id)
        plain = meta.get("plain") is True
        chunked = split_message(text, MAX_LEN, len_fn=code_points, marker=plain_marker)
        ids: list[str] = []
        for n, chunk in enumerate(chunked.chunks):
            if n:
                await self.slots.pace(chat_id)
            body: dict[str, Any] = {
                "chat": chat,
                "text": chunk.text if plain else to_whatsapp(chunk.text),
            }
            if n == 0 and reply_to:
                body["replyTo"] = reply_to
            result = await self._post("/send", body)
            if not result.ok or result.message_id is None:
                return self._partial(result, ids, chunked.remainder(n))
            ids.append(result.message_id)
        return SendResult.sent(ids[-1], tuple(ids[:-1]))

    async def _send_media(
        self, kind: MediaKind, chat_id: str, path: str, caption: str | None, meta: dict[str, Any]
    ) -> SendResult:
        body: dict[str, Any] = {
            "chat": to_whatsapp_jid(chat_id),
            "path": str(Path(path).resolve()),
            "kind": _OUTBOUND[kind],
        }
        if caption:
            body["caption"] = to_whatsapp(caption)
        if kind in ("file", "document"):
            body["fileName"] = Path(path).name
        reply_to = meta.get(REPLY_TO_KEY)
        if isinstance(reply_to, str) and reply_to:
            body["replyTo"] = reply_to
        return await self._post("/send-media", body, timeout=180.0)

    async def _typing(self, chat_id: str, meta: dict[str, Any]) -> None:
        await self._post(
            "/typing", {"chat": to_whatsapp_jid(chat_id), "state": "composing"}, timeout=5.0
        )

    async def _typing_stop(self, chat_id: str) -> None:
        await self._post(
            "/typing", {"chat": to_whatsapp_jid(chat_id), "state": "paused"}, timeout=5.0
        )

    async def react(
        self, chat_id: str, message_id: str, emoji: str, *, participant: str | None = None
    ) -> bool:
        """React to a message (an empty emoji removes ours); False when it did not work."""
        body: dict[str, Any] = {"chat": to_whatsapp_jid(chat_id), "id": message_id, "emoji": emoji}
        if participant:
            body["participant"] = to_whatsapp_jid(participant)
        result = await self._post("/react", body, timeout=10.0)
        if not result.ok:
            log.debug("whatsapp: reaction failed: %s", self.scrub(result.error or ""))
        return result.ok
