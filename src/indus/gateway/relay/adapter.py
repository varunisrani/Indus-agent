# pyright: strict
"""Relay adapters: one gateway adapter per platform a relay connector fronts (P24-09).

``RelayFront`` owns the shared `RelayTransport` (P24-08) and hands each connector event to the
adapter of its platform. ``create_relay_adapter`` builds that adapter from the platform's
descriptor: the class is specialised per descriptor, so ``platform`` (the fronted platform's own
name, e.g. ``telegram``) and ``capabilities`` stay class-level facts like every other adapter's.
The first adapter that connects starts the transport, the last one to disconnect stops it.

Operations (``op`` requests on the transport, answered by ``result``):

- always: ``send``, ``edit`` (when the descriptor says edits work), ``typing``, ``follow_up``;
- only when advertised: ``delete``, ``send_media`` (a local file, at most 25 MB, uploaded to the
  connector's media endpoint first and sent by reference), ``prompt`` (buttons for approval
  cards and clarify choices; each option carries our callback data verbatim, so a press comes
  back as an ordinary ``button`` event), ``react``, ``thread_create``, ``thread_rename``,
  ``chat_info``.

An operation the descriptor does not advertise never reaches the wire: it answers a capability
refusal (``SendResult.unsupported``), which the outbound layer treats as "use the fallback", not as
an error for the user. Text longer than the descriptor's cap (in its unit) is refused locally as
too long; the gateway's chunker splits before sending.

Relay events are authorised by the connector (``auth_upstream``). The profile the connector routed
a chat to (``profile`` on the event), and the chat's ``scope_id`` / sender, are remembered per chat
and stamped on every later operation's ``meta``, so a follow-up (a button press after a command)
lands in the same profile. Result codes from the connector map onto the send classifier
(``to_send_result``); ``declined`` (the connector refused the destination) reads as forbidden and
is never turned into a different operation.

Router rule (``resolve_transport``, used by the delivery router): an enabled native adapter wins;
the relay is used only when it advertises fronting the platform; a native adapter explicitly
disabled never hides an enabled relay.
"""

from __future__ import annotations

import logging
import mimetypes
import secrets
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Final, Literal, cast

import httpx

from indus.foundation.clock import Clock
from indus.gateway.adapters.base import (
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
from indus.gateway.core.events import Attachment, ChatType, InboundEvent, MessageKind, Source
from indus.gateway.core.send import SendResult
from indus.gateway.relay.descriptor import Descriptor
from indus.gateway.relay.transport import (
    RelayConnectError,
    RelayTransport,
    make_hello,
    media_base_url,
)

if TYPE_CHECKING:
    from indus.gateway.config import PlatformSettings

__all__ = [
    "MEDIA_MAX_BYTES",
    "MEDIA_REF_PREFIX",
    "RelayAdapter",
    "RelayFront",
    "RelayMediaClient",
    "create_relay_adapter",
    "resolve_transport",
    "to_send_result",
]

log = logging.getLogger(__name__)

MEDIA_MAX_BYTES: Final = 25 * 1024 * 1024
MEDIA_REF_PREFIX: Final = "relay-media:"
# Per-chat routing facts remembered from inbound events (oldest dropped past this).
ROUTE_CACHE_MAX: Final = 4096
_MAX_ERROR: Final = 300
_NAME_MAX: Final = 100

type InterruptCallback = Callable[[str, str, str | None], Awaitable[None]]

# -- the router rule ------------------------------------------------------------------------------


def resolve_transport(native: bool | None, relay_fronts: bool) -> Literal["native", "relay"] | None:
    """Which transport delivers to a platform.

    ``native``: True when the platform's own adapter is enabled, False when it is explicitly
    disabled, None when it is not configured. An enabled native adapter always wins (even while it
    is down: replies never switch to another bot identity behind the user's back); otherwise the
    relay serves the platform only when it advertises fronting it.
    """
    if native is True:
        return "native"
    return "relay" if relay_fronts else None


# -- results -------------------------------------------------------------------------------------

# Connector result code → the words our send classifier reads (P15-04).
_CODE_TEXT: Mapping[str, str] = {
    "unavailable": "connection refused",
    "timeout": "timed out",
    "rate_limited": "rate limit",
    "chat_not_found": "chat not found",
    "thread_not_found": "thread not found",
    "message_not_found": "message not found",
    "forbidden": "forbidden",
    "declined": "forbidden (the connector declined this destination)",
    "bad_format": "can't parse",
    "too_long": "too long",
}


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if value >= 0 else None


def to_send_result(result: Mapping[str, Any], *, what: str = "this operation") -> SendResult:
    """A connector ``result`` as a classified `SendResult`."""
    if result.get("ok") is True:
        ident = result.get("message_id") or result.get("thread_id")
        return SendResult.sent(str(ident)) if ident not in (None, "") else SendResult(ok=True)
    code = str(result.get("code") or "error")
    detail = str(result.get("error") or "").strip()[:_MAX_ERROR]
    if code == "unsupported":
        return SendResult.refused(what)
    needle = _CODE_TEXT.get(code, "relay operation failed")
    retry_after = _number(result.get("retry_after")) if code == "rate_limited" else None
    failed = SendResult.failure(f"relay: {needle}", retry_after=retry_after)
    return replace(failed, error=f"relay: {needle}: {detail}" if detail else failed.error)


# -- media ---------------------------------------------------------------------------------------


class RelayMediaClient:
    """Uploads to and downloads from the connector's media endpoint, signed like the hello."""

    def __init__(
        self,
        relay_url: str,
        gateway_id: str,
        secret: str,
        *,
        clock: Clock,
        transport: httpx.AsyncBaseTransport | None = None,
        nonce: Callable[[], str] | None = None,
    ) -> None:
        self.base = media_base_url(relay_url)
        self._gateway_id = gateway_id
        self._secret = secret
        self.clock = clock
        self._transport = transport
        self._nonce = nonce or (lambda: secrets.token_hex(16))

    def __repr__(self) -> str:
        return f"<RelayMediaClient {self.base}>"

    def _headers(self) -> dict[str, str]:
        hello = make_hello(
            self._gateway_id, self._secret, now=self.clock.now(), nonce=self._nonce()
        )
        return {
            "x-indus-relay-gateway": self._gateway_id,
            "x-indus-relay-ts": str(hello["ts"]),
            "x-indus-relay-nonce": str(hello["nonce"]),
            "x-indus-relay-sig": str(hello["sig"]),
        }

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self._transport, trust_env=False, timeout=httpx.Timeout(60.0, connect=10.0)
        )

    async def upload(self, path: Path, kind: str) -> str:
        """Upload one file; the reference the connector gave it. Raises ``ValueError`` for a file
        that cannot be sent and ``httpx.HTTPError`` for transport failures."""
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise ValueError(f"the file cannot be read: {type(exc).__name__}") from None
        if size <= 0:
            raise ValueError("the file is empty")
        if size > MEDIA_MAX_BYTES:
            raise ValueError(f"the file is larger than {MEDIA_MAX_BYTES // (1024 * 1024)} MB")
        data = path.read_bytes()
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        headers = {
            **self._headers(),
            "content-type": mime,
            "x-indus-relay-filename": path.name[:255],
        }
        async with self._client() as client:
            response = await client.post(
                self.base, content=data, headers={**headers, "x-indus-relay-kind": kind}
            )
        response.raise_for_status()
        try:
            body: object = response.json()
        except ValueError:
            raise ValueError("the connector's media answer is not JSON") from None
        ref = cast(Mapping[str, Any], body).get("ref") if isinstance(body, Mapping) else None
        if not isinstance(ref, str) or not ref:
            raise ValueError("the connector did not return a media reference")
        return ref

    async def download(self, ref: str) -> AsyncIterator[bytes]:
        """The bytes of a connector-held attachment, at most ``MEDIA_MAX_BYTES``."""
        if not ref.startswith(MEDIA_REF_PREFIX):
            raise ValueError("not a relay media reference")
        ident = ref[len(MEDIA_REF_PREFIX) :]
        if not ident.replace("-", "").replace("_", "").isalnum():
            raise ValueError("not a relay media reference")
        async with (
            self._client() as client,
            client.stream("GET", f"{self.base}/{ident}", headers=self._headers()) as reply,
        ):
            reply.raise_for_status()
            total = 0
            async for chunk in reply.aiter_bytes():
                total += len(chunk)
                if total > MEDIA_MAX_BYTES:
                    raise ValueError("the attachment is larger than the relay media cap")
                yield chunk


# -- the front: one transport, many adapters -----------------------------------------------------


class RelayFront:
    """The shared transport plus the adapters of the platforms it fronts."""

    def __init__(
        self,
        transport: RelayTransport,
        *,
        media: RelayMediaClient | None = None,
        on_interrupt: InterruptCallback | None = None,
    ) -> None:
        self.transport = transport
        self.media = media
        self.on_interrupt = on_interrupt
        self._adapters: dict[str, RelayAdapter] = {}
        transport.set_event_handler(self._event)
        transport.set_interrupt_handler(self._interrupt)
        transport.set_fatal_handler(self._fatal)

    def adapters(self) -> list[RelayAdapter]:
        return list(self._adapters.values())

    def attach(self, adapter: RelayAdapter) -> None:
        self._adapters[adapter.platform] = adapter

    def detach(self, adapter: RelayAdapter) -> None:
        if self._adapters.get(adapter.platform) is adapter:
            del self._adapters[adapter.platform]

    def fronts(self, platform: str) -> bool:
        return self.transport.fronts(platform)

    async def _event(self, platform: str, event: Mapping[str, Any]) -> None:
        adapter = self._adapters.get(platform)
        if adapter is None:
            log.warning("relay: dropped an event for %s: no adapter is connected for it", platform)
            return
        await adapter.receive(event)

    async def _interrupt(self, platform: str, chat_id: str, session_key: str | None) -> None:
        if self.on_interrupt is not None:
            await self.on_interrupt(platform, chat_id, session_key)

    def _fatal(self, code: str, message: str) -> None:
        for adapter in self.adapters():
            adapter.report_fatal(code, message)


def create_relay_adapter(
    front: RelayFront,
    descriptor: Descriptor,
    *,
    clock: Clock,
    settings: PlatformSettings | None = None,
    sink: InboundSink | None = None,
) -> RelayAdapter:
    """The adapter for one fronted platform (a subclass carrying its platform and capabilities)."""
    attrs: dict[str, object] = {
        "platform": descriptor.platform,
        "capabilities": descriptor.capabilities(),
        "descriptor": descriptor,
        "__module__": __name__,
    }
    cls = cast(
        type[RelayAdapter], type(f"RelayAdapter_{descriptor.platform}", (RelayAdapter,), attrs)
    )
    return cls(front, clock=clock, settings=settings, sink=sink)


# -- the adapter ---------------------------------------------------------------------------------

_KINDS: Mapping[str, MessageKind] = {
    "photo": MessageKind.PHOTO,
    "image": MessageKind.PHOTO,
    "voice": MessageKind.VOICE,
    "audio": MessageKind.AUDIO,
    "video": MessageKind.VIDEO,
    "document": MessageKind.DOCUMENT,
    "file": MessageKind.DOCUMENT,
    "sticker": MessageKind.STICKER,
}
_CHAT_TYPES: frozenset[str] = frozenset({"dm", "group", "channel", "thread", "forum"})
_ROUTE_KEYS: tuple[str, ...] = ("profile", "scope_id", "user_id")


def _text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


def _wire_meta(meta: Mapping[str, Any]) -> dict[str, Any]:
    """The JSON-safe part of outbound metadata."""
    return {
        str(k): v for k, v in meta.items() if v is None or isinstance(v, str | int | float | bool)
    }


class RelayAdapter(Adapter):
    """One fronted platform over the shared relay transport (see the module notes)."""

    platform: ClassVar[str] = "relay"
    capabilities: ClassVar[Capabilities] = Descriptor("relay").capabilities()
    descriptor: ClassVar[Descriptor] = Descriptor("relay")

    def __init__(
        self,
        front: RelayFront,
        *,
        clock: Clock,
        settings: PlatformSettings | None = None,
        sink: InboundSink | None = None,
    ) -> None:
        super().__init__(clock=clock, settings=settings, sink=sink)
        self.front = front
        self._routes: OrderedDict[str, dict[str, str]] = OrderedDict()

    # -- lifecycle --------------------------------------------------------------------------------

    async def _open(self, is_reconnect: bool) -> None:
        transport = self.front.transport
        try:
            await transport.acquire()
        except RelayConnectError as exc:
            raise AdapterConnectError(str(exc), retryable=exc.retryable, code=exc.code) from None
        if not transport.fronts(self.platform):
            await transport.release()
            raise AdapterConnectError(
                f"the relay connector does not front {self.platform}",
                retryable=False,
                code="relay_not_fronted",
            )
        self.front.attach(self)

    async def _close(self) -> None:
        self.front.detach(self)
        await self.front.transport.release()

    # -- ops --------------------------------------------------------------------------------------

    def advertises(self, op: str) -> bool:
        """The connector's current word on ``op`` for this platform (a re-advertised descriptor
        counts; before the first handshake the build-time descriptor does)."""
        live = self.front.transport.descriptor(self.platform)
        return (live or self.descriptor).supports(op)

    def _scoped(self, chat_id: str, meta: Mapping[str, Any] | None = None) -> dict[str, Any]:
        out = _wire_meta(meta or {})
        for key, value in self._routes.get(chat_id, {}).items():
            out.setdefault(key, value)
        return out

    async def _op(self, op: str, action: dict[str, Any]) -> dict[str, Any]:
        return await self.front.transport.request(self.platform, {"op": op, **action})

    async def _send(
        self, chat_id: str, text: str, reply_to: str | None, meta: dict[str, Any]
    ) -> SendResult:
        caps = self.capabilities
        if caps.len_fn(text) > caps.max_len:
            return SendResult.failure(f"relay: message too long for {self.platform}")
        action: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "meta": self._scoped(chat_id, meta),
        }
        if reply_to:
            action["reply_to"] = reply_to
        return to_send_result(await self._op("send", action), what="sending")

    async def _edit(
        self, chat_id: str, message_id: str, text: str, final: bool, meta: dict[str, Any]
    ) -> SendResult:
        if not self.advertises("edit"):
            return SendResult.refused("editing")
        caps = self.capabilities
        if caps.len_fn(text) > caps.max_len:
            return SendResult.failure(f"relay: message too long for {self.platform}")
        action = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "final": final,
            "meta": self._scoped(chat_id, meta),
        }
        result = to_send_result(await self._op("edit", action), what="editing")
        if result.ok and result.message_id is None:
            return SendResult.sent(message_id)
        return result

    async def _delete(self, chat_id: str, message_id: str) -> bool:
        if not self.advertises("delete"):
            return False
        action = {"chat_id": chat_id, "message_id": message_id, "meta": self._scoped(chat_id)}
        return (await self._op("delete", action)).get("ok") is True

    async def _typing(self, chat_id: str, meta: dict[str, Any]) -> None:
        await self._op("typing", {"chat_id": chat_id, "meta": self._scoped(chat_id, meta)})

    async def follow_up(
        self,
        chat_id: str,
        kind: str,
        text: str,
        *,
        session_key: str,
        meta: Mapping[str, Any] | None = None,
    ) -> SendResult:
        """Act through a capability the connector holds for this session (it never reaches us:
        we name the session and the capability ``kind``, the connector does the rest)."""
        action = {
            "chat_id": chat_id,
            "kind": kind,
            "text": text,
            "session_key": session_key,
            "meta": self._scoped(chat_id, meta),
        }
        try:
            return self._scrubbed(to_send_result(await self._op("follow_up", action)))
        except Exception as exc:
            return self._failed(exc)

    async def _send_media(
        self, kind: MediaKind, chat_id: str, path: str, caption: str | None, meta: dict[str, Any]
    ) -> SendResult:
        what = f"sending {kind}s"
        if not self.advertises("send_media"):
            return SendResult.refused(what)
        media = self.front.media
        if media is None:
            return SendResult.refused(what)
        file = Path(path)
        try:
            ref = await media.upload(file, kind)
        except ValueError as exc:
            return SendResult(ok=False, error=f"relay media: {exc}")
        except httpx.HTTPError as exc:
            return SendResult.failure(f"relay media upload failed: {type(exc).__name__}")
        reply_to = meta.pop("reply_to", None)
        action: dict[str, Any] = {
            "chat_id": chat_id,
            "media_kind": kind,
            "ref": ref,
            "filename": file.name,
            "meta": self._scoped(chat_id, meta),
        }
        if caption:
            action["caption"] = caption
        if isinstance(reply_to, str) and reply_to:
            action["reply_to"] = reply_to
        return to_send_result(await self._op("send_media", action), what=what)

    async def _prompt(
        self,
        chat_id: str,
        kind: str,
        text: str,
        options: list[dict[str, str]],
        meta: dict[str, Any],
    ) -> SendResult:
        action = {
            "chat_id": chat_id,
            "kind": kind,
            "text": text,
            "options": options,
            "meta": self._scoped(chat_id, meta),
        }
        return to_send_result(await self._op("prompt", action), what="buttons")

    async def _send_approval(
        self, chat_id: str, prompt: ApprovalPrompt, meta: dict[str, Any]
    ) -> SendResult:
        if not self.advertises("prompt"):
            return await self._send(chat_id, prompt.fallback_text, None, meta)
        options: list[dict[str, str]] = []
        for action in prompt.actions:
            option = {
                "id": action.choice,
                "label": action.label,
                "data": approval_callback(prompt.request_id, action.choice),
            }
            if action.style:
                option["style"] = action.style
            options.append(option)
        return await self._prompt(chat_id, "approval", prompt.text, options, meta)

    async def _send_clarify(
        self, chat_id: str, prompt: ClarifyPrompt, meta: dict[str, Any]
    ) -> SendResult:
        if not self.advertises("prompt"):
            return await self._send(chat_id, prompt.fallback_text or prompt.question, None, meta)
        options = [
            {
                "id": f"c{n}",
                "label": choice[:_NAME_MAX],
                "data": clarify_callback(prompt.clarify_id, n),
            }
            for n, choice in enumerate(prompt.choices)
        ]
        return await self._prompt(chat_id, "clarify", prompt.question, options, meta)

    async def react(
        self, chat_id: str, message_id: str, emoji: str, *, remove: bool = False
    ) -> SendResult:
        """Add (or remove) our reaction; cosmetic, so a refusal is never an error."""
        if not self.advertises("react"):
            return SendResult.refused("reactions")
        action = {
            "chat_id": chat_id,
            "message_id": message_id,
            "emoji": emoji,
            "remove": remove,
            "meta": self._scoped(chat_id),
        }
        try:
            return self._scrubbed(to_send_result(await self._op("react", action), what="reactions"))
        except Exception as exc:
            return self._failed(exc)

    async def create_thread(
        self, chat_id: str, name: str, *, anchor: str | None = None
    ) -> SendResult:
        """Open a thread (topic) under ``chat_id``; the new thread's id is the result's
        ``message_id``."""
        if not self.advertises("thread_create"):
            return SendResult.refused("creating threads")
        action: dict[str, Any] = {
            "chat_id": chat_id,
            "name": (" ".join(name.split()) or "thread")[:_NAME_MAX],
            "meta": self._scoped(chat_id),
        }
        if anchor:
            action["anchor"] = anchor
        try:
            result = await self._op("thread_create", action)
            return self._scrubbed(to_send_result(result, what="creating threads"))
        except Exception as exc:
            return self._failed(exc)

    async def rename_thread(self, chat_id: str, thread_id: str, name: str) -> SendResult:
        if not self.advertises("thread_rename"):
            return SendResult.refused("renaming threads")
        clean = " ".join(name.split())[:_NAME_MAX]
        if not clean:
            return SendResult(ok=False, error="a thread name cannot be empty")
        action = {
            "chat_id": chat_id,
            "thread_id": thread_id,
            "name": clean,
            "meta": self._scoped(chat_id),
        }
        try:
            result = await self._op("thread_rename", action)
            return self._scrubbed(to_send_result(result, what="renaming threads"))
        except Exception as exc:
            return self._failed(exc)

    async def _chat_info(self, chat_id: str) -> ChatInfo:
        if not self.advertises("chat_info"):
            return ChatInfo(name=chat_id)
        result = await self._op("chat_info", {"chat_id": chat_id})
        name = _text(result.get("name")) if result.get("ok") is True else None
        kind = result.get("chat_type")
        typed = cast(ChatType, kind) if isinstance(kind, str) and kind in _CHAT_TYPES else None
        return ChatInfo(name=name or chat_id, type=typed)

    # -- inbound ----------------------------------------------------------------------------------

    def register_fetchers(self, seat: FetcherSeat) -> Callable[[], None] | None:
        media = self.front.media
        if media is None:
            return None

        async def fetch(attachment: Attachment) -> AsyncIterator[bytes]:
            async for chunk in media.download(attachment.ref):
                yield chunk

        return seat.register_fetcher(self.platform, fetch)

    def remember(self, chat_id: str, facts: Mapping[str, str]) -> None:
        """Keep the chat's routing facts (profile, scope, sender) for later operations."""
        if not facts:
            return
        self._routes[chat_id] = {**self._routes.get(chat_id, {}), **facts}
        self._routes.move_to_end(chat_id)
        while len(self._routes) > ROUTE_CACHE_MAX:
            self._routes.popitem(last=False)

    def normalise(self, event: Mapping[str, Any]) -> InboundEvent | None:
        """One connector event as an `InboundEvent` (None for an event without a chat)."""
        chat_id = _text(event.get("chat_id"))
        if chat_id is None:
            return None
        thread_id = _text(event.get("thread_id"))
        raw_type = event.get("chat_type")
        chat_type = cast(ChatType, raw_type) if raw_type in _CHAT_TYPES else "dm"
        if thread_id and chat_type not in ("thread", "forum"):
            chat_type = "thread"
        source = Source(
            platform=self.platform,
            chat_id=chat_id,
            chat_type=chat_type,
            chat_name=_text(event.get("chat_name")),
            user_id=_text(event.get("user_id")),
            user_name=_text(event.get("user_name")),
            thread_id=thread_id,
            user_id_alt=_text(event.get("user_id_alt")),
            is_bot=event.get("is_bot") is True,
            scope_id=_text(event.get("scope_id")),
            guild_id=_text(event.get("guild_id")),
            parent_chat_id=_text(event.get("parent_chat_id")),
        )
        media: list[Attachment] = []
        kind = MessageKind.TEXT
        raw_media = event.get("media")
        for item in cast(list[object], raw_media) if isinstance(raw_media, list) else []:
            if not isinstance(item, Mapping):
                continue
            entry = cast(Mapping[str, Any], item)
            ref = _text(entry.get("ref"))
            if ref is None:
                continue
            mime = entry.get("mime")
            media.append(Attachment(ref=ref, mime=mime if isinstance(mime, str) else ""))
            if kind is MessageKind.TEXT:
                kind = _KINDS.get(str(entry.get("kind") or ""), MessageKind.DOCUMENT)
        metadata: dict[str, Any] = {}
        if event.get("edited") is True:
            metadata["edited"] = True
        button = _text(event.get("button"))
        if button is not None:
            metadata["button"] = button
        if event.get("mentioned") is True:
            metadata["mentioned"] = True
        profile = _text(event.get("profile"))
        if profile is not None:
            metadata["profile"] = profile
        when = event.get("ts")
        stamp = (
            datetime.fromtimestamp(float(when), UTC)
            if isinstance(when, int | float) and not isinstance(when, bool) and when > 0
            else None
        )
        reply = event.get("reply_to")
        return InboundEvent(
            source=source,
            text=str(event.get("text") or ""),
            kind=kind,
            message_id=_text(event.get("message_id")),
            media=tuple(media),
            reply_to_id=_text(reply),
            reply_to_text=_text(event.get("reply_to_text")),
            reply_to_is_own=event.get("reply_to_is_own") is True,
            metadata=metadata,
            timestamp=stamp,
            reply_expected=True if event.get("mentioned") is True else None,
        )

    async def receive(self, event: Mapping[str, Any]) -> None:
        """A connector event for this platform: remember its routing facts, then admit it."""
        normalised = self.normalise(event)
        if normalised is None:
            log.warning("%s (relay): dropped an event without a chat id", self.platform)
            return
        chat_id = normalised.source.chat_id
        facts = {k: v for k in _ROUTE_KEYS if (v := _text(event.get(k))) is not None}
        self.remember(chat_id, facts)
        await self.emit(normalised)
