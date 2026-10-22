"""The adapter contract every platform implements, and its capability flags (P15-05).

The public methods of `Adapter` are templates around small abstract or overridable hooks
(``_open``, ``_close``, ``_send``, ``_edit``, ...), so every adapter gets the same guarantees:

- ``connect`` / ``disconnect`` are idempotent: the hooks run only on a real transition;
- an exception from a hook becomes a classified `SendResult` whose error text has this adapter's
  credentials masked (tokens appear in vendor URLs and error messages);
- optional operations a platform lacks are refused cleanly and never reach the platform;
- sends and edits to one chat share one send slot (R06.13): sends wait, interim edits are skipped
  while it is busy, the final edit is never gated;
- inbound events leave through ``emit`` to the admission pipeline the runner installs;
- the outbound ``interim`` / ``final`` metadata marks (P15-16, R06.12) are taken off before any
  hook sees the metadata, so they never reach the wire (an edit's ``final`` mark also gates it).

The rest of the gateway asks `Capabilities` what a platform can do instead of asking which
platform it is. ``dm_is_private`` is False for broadcast surfaces that report every chat as a DM
(R06.24): pairing codes and login links must never go there.
"""

from __future__ import annotations

import logging
import random
import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol

from indus.foundation.clock import Clock
from indus.foundation.digits import is_digits
from indus.foundation.redact import redact_text
from indus.gateway.adapters._native import NativeStream
from indus.gateway.core.events import Attachment, ChatType, InboundEvent
from indus.gateway.core.send import DELIVERY_FAILED_NOTICE, RetryPolicy, SendResult, send_with_retry
from indus.gateway.core.send_slot import SendSlots
from indus.gateway.outbound.chunker import LenFn, code_points, utf16_units
from indus.gateway.outbound.formatting import Markup, formatter_for

if TYPE_CHECKING:
    from indus.gateway.config import PlatformSettings

log = logging.getLogger(__name__)

InboundSink = Callable[[InboundEvent], Awaitable[None]]
# Told when a running connection is lost for good (a revoked token): ``(code, message)``.
FatalListener = Callable[[str, str], object]
# Truthy while the gateway drains (the daemon installs ``Lifecycle.notice``): adapters whose
# platform redelivers (a 503 to a webhook, an unacknowledged pull) can leave new events with it.
DrainProbe = Callable[[], object]
# A platform's download of one inbound attachment, as chunks (Telegram ``getFile`` and friends).
type Fetcher = Callable[[Attachment], AsyncIterator[bytes]]


class FetcherSeat(Protocol):
    """Where adapters register their media downloads (``intake.media.MediaIntake``)."""

    def register_fetcher(self, platform: str, fetcher: Fetcher) -> Callable[[], None]: ...


MediaKind = Literal["image", "file", "voice", "video", "document"]
ApprovalChoice = Literal["once", "session", "always", "deny"]
APPROVAL_CHOICES: tuple[ApprovalChoice, ...] = ("once", "session", "always", "deny")

# Telegram caps callback data at 64 bytes; every platform gets the same bound.
CALLBACK_MAX_BYTES = 64

# Outbound metadata marks set by the egress doors (``gateway.outbound.ports``); stripped here.
INTERIM_KEY = "interim"
FINAL_KEY = "final"
# Media hooks find the message to reply to here (text hooks get it as an argument).
REPLY_TO_KEY = "reply_to"


@dataclass(frozen=True)
class Capabilities:
    max_len: int = 4096
    len_fn: LenFn = code_points
    # Markup the agent's Markdown is converted to (``outbound.formatting.formatter_for``).
    markup: Markup = "plain"
    # ``send`` splits long text itself and reports every chunk id.
    splits_natively: bool = False
    supports_edit: bool = False
    supports_delete: bool = False
    supports_buttons: bool = False
    # The adapter enforces its own DM/group policy (P15-08 asks it first).
    owns_access_policy: bool = False
    # Whoever reaches the adapter was authorised upstream (a relay, a signed webhook).
    auth_upstream: bool = False
    # False for broadcast surfaces that report every chat as a DM (R06.24).
    dm_is_private: bool = True
    # Can start a conversation later (background completions, cron) — False for stateless HTTP.
    push_capable: bool = True
    # Seconds between operations on one chat (Telegram 1.0).
    slot_spacing: float = 0.0
    # Longest server wait slept inline before handing it to the ledger (Telegram 5).
    inline_wait_cap: float = 60.0
    # Replying into the source's prospective thread opens that thread (Slack: ``thread_ts`` =
    # the message ``ts``), so every reply piece, card and typing status may address it. Off where
    # the thread does not exist until the adapter opens it (Discord).
    reply_opens_thread: bool = False


@dataclass(frozen=True)
class ChatInfo:
    name: str
    type: ChatType | None = None


@dataclass(frozen=True)
class ChannelEntry:
    """A chat this bot can post to, for the channel directory (P15-20)."""

    id: str
    name: str
    type: str = "channel"
    guild: str = ""
    thread_id: str | None = None


@dataclass(frozen=True)
class ApprovalAction:
    label: str
    choice: ApprovalChoice
    style: Literal["primary", "danger", ""] = ""


@dataclass(frozen=True)
class ApprovalPrompt:
    """A dangerous-command approval card; wording comes from the prompts module (P15-22)."""

    request_id: str
    session_key: str
    text: str
    # The same card for platforms without buttons, ending with the /approve and /deny hints.
    fallback_text: str
    actions: tuple[ApprovalAction, ...] = ()


@dataclass(frozen=True)
class ClarifyPrompt:
    clarify_id: str
    session_key: str
    question: str
    choices: tuple[str, ...] = ()
    multi: bool = False
    # Numbered choices for platforms without buttons.
    fallback_text: str = ""


class AdapterConnectError(Exception):
    """``_open`` failed. ``retryable`` false means a configuration problem (fatal)."""

    def __init__(self, message: str, *, retryable: bool = True, code: str = "connect_failed"):
        super().__init__(message)
        self.retryable = retryable
        self.code = code


# -- button callback data --------------------------------------------------------------------------

_CALLBACK_ID = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")


def _callback(prefix: str, ident: str, tail: str) -> str:
    if not _CALLBACK_ID.match(ident):
        raise ValueError(f"callback id {ident!r} must be 1-40 characters of [A-Za-z0-9_.-]")
    data = f"{prefix}:{ident}:{tail}"
    if len(data.encode("utf-8")) > CALLBACK_MAX_BYTES:
        raise ValueError(f"callback data longer than {CALLBACK_MAX_BYTES} bytes")
    return data


def approval_callback(request_id: str, choice: ApprovalChoice) -> str:
    if choice not in APPROVAL_CHOICES:
        raise ValueError(f"unknown approval choice {choice!r}")
    return _callback("apv", request_id, choice)


def parse_approval_callback(data: str) -> tuple[str, ApprovalChoice] | None:
    prefix, _, rest = data.partition(":")
    request_id, _, choice = rest.partition(":")
    if prefix != "apv" or not _CALLBACK_ID.match(request_id):
        return None
    for known in APPROVAL_CHOICES:
        if choice == known:
            return request_id, known
    return None


def clarify_callback(clarify_id: str, index: int) -> str:
    if index < 0:
        raise ValueError("choice index must be >= 0")
    return _callback("clr", clarify_id, str(index))


def parse_clarify_callback(data: str) -> tuple[str, int] | None:
    prefix, _, rest = data.partition(":")
    clarify_id, _, index = rest.partition(":")
    if prefix != "clr" or not _CALLBACK_ID.match(clarify_id) or not is_digits(index):
        return None
    return clarify_id, int(index)


# -- the adapter -----------------------------------------------------------------------------------


def _merged_meta(meta: Mapping[str, Any] | None) -> dict[str, Any]:
    """A private copy of ``meta`` without the outbound interim/final marks."""
    merged = dict(meta) if meta else {}
    merged.pop(INTERIM_KEY, None)
    merged.pop(FINAL_KEY, None)
    return merged


def _marked_final(meta: Mapping[str, Any] | None) -> bool:
    return meta is not None and meta.get(FINAL_KEY) is True


class Adapter(ABC):
    """One platform connection: inbound normaliser plus outbound sender."""

    platform: ClassVar[str] = ""
    capabilities: ClassVar[Capabilities] = Capabilities()
    # ``"module:Object"`` → attribute names this adapter calls on the vendor library; the drift
    # check (P15-01) proves each exists in the pinned version.
    vendor_names: ClassVar[Mapping[str, tuple[str, ...]]] = {}

    def __init__(
        self,
        *,
        clock: Clock,
        settings: PlatformSettings | None = None,
        secrets: Sequence[str] = (),
        sink: InboundSink | None = None,
    ) -> None:
        self.clock = clock
        self.settings = settings
        held = list(secrets)
        if settings is not None:
            held.extend(settings.credentials.values())
        self._secrets: tuple[str, ...] = tuple(v for v in held if v)
        self._sink = sink
        self._sink_warned = False
        self.connected = False
        self._fatal: FatalListener | None = None
        self._drain_probe: DrainProbe | None = None
        self.slots = SendSlots(clock, self.capabilities.slot_spacing)

    def __repr__(self) -> str:
        state = "connected" if self.connected else "disconnected"
        return f"<{type(self).__name__} {self.platform or '?'} {state}>"

    # -- secrets -----------------------------------------------------------------------------------

    def scrub(self, text: str) -> str:
        """``text`` with this adapter's credentials and credential shapes masked."""
        return redact_text(text, self._secrets)

    def _scrubbed(self, result: SendResult) -> SendResult:
        if result.error is None:
            return result
        clean = self.scrub(result.error)
        return result if clean == result.error else replace(result, error=clean)

    def _failed(self, exc: BaseException) -> SendResult:
        log.warning(
            "%s: platform call failed: %s",
            self.platform,
            self.scrub(f"{type(exc).__name__}: {exc}"),
        )
        return self._scrubbed(SendResult.failure(exc=exc))

    # -- lifecycle ---------------------------------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> None:
        """Connect and start receiving; a second call while connected does nothing."""
        if self.connected:
            return
        try:
            await self._open(is_reconnect)
        except AdapterConnectError as exc:
            raise AdapterConnectError(
                self.scrub(str(exc)), retryable=exc.retryable, code=exc.code
            ) from None
        except Exception as exc:
            raise AdapterConnectError(
                self.scrub(f"{type(exc).__name__}: {exc}"), retryable=True
            ) from None
        self.connected = True

    async def disconnect(self) -> None:
        """Stop receiving and close; a second call (or one before connect) does nothing."""
        if not self.connected:
            return
        self.connected = False
        try:
            await self._close()
        except Exception as exc:
            log.warning("%s: disconnect failed: %s", self.platform, self.scrub(str(exc)))

    @abstractmethod
    async def _open(self, is_reconnect: bool) -> None:
        """Connect to the platform and start listening; raise `AdapterConnectError` on failure.

        ``is_reconnect``: keep any server-side update queue so outage-time messages survive."""

    @abstractmethod
    async def _close(self) -> None:
        """Stop listeners and close connections."""

    def set_fatal_listener(self, listener: FatalListener | None) -> None:
        """Who to tell when the running connection is lost for good (the daemon parks the
        platform as ``fatal`` instead of retrying it)."""
        self._fatal = listener

    def report_fatal(self, code: str, message: str) -> None:
        """The connection is gone and retrying cannot help (e.g. the token was revoked)."""
        listener = self._fatal
        if listener is None:
            return
        try:
            listener(code, self.scrub(message))
        except Exception:
            log.warning("%s: the fatal-state listener failed", self.platform, exc_info=True)

    def set_drain_probe(self, probe: DrainProbe | None) -> None:
        """How to tell that the gateway is draining (the daemon installs it on platform up for
        every adapter; most ignore it and let the core answer new turns with its pause notice)."""
        self._drain_probe = probe

    def draining(self) -> bool:
        """True while the installed drain probe says so; a failing probe reads as running."""
        probe = self._drain_probe
        if probe is None:
            return False
        try:
            return bool(probe())
        except Exception:
            log.warning("%s: the drain probe failed; treating as running", self.platform)
            return False

    # -- inbound -----------------------------------------------------------------------------------

    def register_fetchers(self, seat: FetcherSeat) -> Callable[[], None] | None:
        """Register this platform's media downloads with the intake (P15-23); the undo, or None
        when the adapter downloads nothing."""
        return None

    def set_sink(self, sink: InboundSink | None) -> None:
        self._sink = sink

    async def emit(self, event: InboundEvent) -> None:
        """Hand one normalised event to the admission pipeline; never raises into the adapter."""
        if self._sink is None:
            if not self._sink_warned:
                self._sink_warned = True
                log.warning("%s: inbound event dropped: no handler installed", self.platform)
            return
        try:
            await self._sink(event)
        except Exception:
            log.exception("%s: inbound handler failed", self.platform)

    # -- outbound: text ----------------------------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        text: str,
        *,
        reply_to: str | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> SendResult:
        """Send ``text`` to a chat. Never raises: failures come back classified."""
        if not text or not text.strip():
            return SendResult(ok=False, error="nothing to send")
        try:
            async with self.slots.hold(chat_id):
                result = await self._send(chat_id, text, reply_to, _merged_meta(meta))
        except Exception as exc:
            return self._failed(exc)
        return self._scrubbed(result)

    @abstractmethod
    async def _send(
        self, chat_id: str, text: str, reply_to: str | None, meta: dict[str, Any]
    ) -> SendResult:
        """Send inside the chat's slot. Adapters that split call ``await self.slots.pace(chat_id)``
        before each further chunk; ``meta["plain"]`` asks for no formatting."""

    async def edit(
        self,
        chat_id: str,
        message_id: str,
        text: str,
        *,
        final: bool = False,
        meta: Mapping[str, Any] | None = None,
    ) -> SendResult:
        """Edit a sent message. Interim edits are skipped while the chat's slot is busy;
        the final edit is never gated."""
        if not self.capabilities.supports_edit:
            return SendResult.refused("editing")
        final = final or _marked_final(meta)
        try:
            if final:
                self.slots.mark(chat_id)
                result = await self._edit(chat_id, message_id, text, True, _merged_meta(meta))
            else:
                held = self.slots.try_interim(chat_id)
                if held is None:
                    return SendResult.skip()
                async with held:
                    result = await self._edit(chat_id, message_id, text, False, _merged_meta(meta))
        except Exception as exc:
            return self._failed(exc)
        return self._scrubbed(result)

    async def _edit(
        self, chat_id: str, message_id: str, text: str, final: bool, meta: dict[str, Any]
    ) -> SendResult:
        return SendResult.refused("editing")

    async def delete(self, chat_id: str, message_id: str) -> bool:
        if not self.capabilities.supports_delete:
            return False
        try:
            async with self.slots.hold(chat_id):
                return await self._delete(chat_id, message_id)
        except Exception as exc:
            log.warning("%s: delete failed: %s", self.platform, self.scrub(str(exc)))
            return False

    async def _delete(self, chat_id: str, message_id: str) -> bool:
        return False

    def open_stream(
        self,
        chat_id: str,
        *,
        reply_to: str | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> NativeStream | None:
        """A native reply stream into ``chat_id`` (P16-33: Telegram drafts, Slack streams, WeCom
        reply streams), or None: the stream consumer then edits a message instead."""
        return None

    async def send_with_retry(
        self,
        chat_id: str,
        text: str,
        *,
        reply_to: str | None = None,
        meta: Mapping[str, Any] | None = None,
        rng: Callable[[], float] = random.random,
        notice: str | None = DELIVERY_FAILED_NOTICE,
    ) -> SendResult:
        """``send`` under the P15-04 retry policy with this platform's inline wait cap."""

        async def attempt(body: str, plain: bool) -> SendResult:
            extra = _merged_meta(meta)
            if plain:
                extra["plain"] = True
            return await self.send(chat_id, body, reply_to=reply_to, meta=extra)

        policy = RetryPolicy(inline_wait_cap=self.capabilities.inline_wait_cap)
        return await send_with_retry(
            attempt,
            text,
            clock=self.clock,
            policy=policy,
            rng=rng,
            notice=notice,
            is_final=self.is_final_failure,
        )

    def is_final_failure(self, result: SendResult) -> bool:
        """A failure neither a retry nor plain text can fix (an auth refusal). Default: none."""
        return False

    # -- outbound: typing --------------------------------------------------------------------------

    async def typing_start(self, chat_id: str, meta: Mapping[str, Any] | None = None) -> None:
        """Show (or refresh) the typing indicator; failures are logged, never raised."""
        try:
            await self._typing(chat_id, _merged_meta(meta))
        except Exception as exc:
            log.debug("%s: typing failed: %s", self.platform, self.scrub(str(exc)))

    async def typing_stop(self, chat_id: str) -> None:
        try:
            await self._typing_stop(chat_id)
        except Exception as exc:
            log.debug("%s: typing stop failed: %s", self.platform, self.scrub(str(exc)))

    async def _typing(self, chat_id: str, meta: dict[str, Any]) -> None:
        return None

    async def _typing_stop(self, chat_id: str) -> None:
        return None

    # -- outbound: media ---------------------------------------------------------------------------

    async def _media(
        self,
        kind: MediaKind,
        chat_id: str,
        path: str,
        caption: str | None,
        meta: Mapping[str, Any] | None,
        reply_to: str | None = None,
    ) -> SendResult:
        extra = _merged_meta(meta)
        if reply_to is not None:
            extra[REPLY_TO_KEY] = reply_to
        try:
            async with self.slots.hold(chat_id):
                result = await self._send_media(kind, chat_id, path, caption, extra)
        except Exception as exc:
            return self._failed(exc)
        return self._scrubbed(result)

    async def _send_media(
        self, kind: MediaKind, chat_id: str, path: str, caption: str | None, meta: dict[str, Any]
    ) -> SendResult:
        """Send one validated file; ``meta["reply_to"]`` names the message to thread under."""
        return SendResult.refused(f"sending {kind}s")

    async def send_image(
        self,
        chat_id: str,
        path: str,
        *,
        caption: str | None = None,
        reply_to: str | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> SendResult:
        return await self._media("image", chat_id, path, caption, meta, reply_to)

    async def send_file(
        self,
        chat_id: str,
        path: str,
        *,
        caption: str | None = None,
        reply_to: str | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> SendResult:
        return await self._media("file", chat_id, path, caption, meta, reply_to)

    async def send_voice(
        self,
        chat_id: str,
        path: str,
        *,
        caption: str | None = None,
        reply_to: str | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> SendResult:
        return await self._media("voice", chat_id, path, caption, meta, reply_to)

    async def send_video(
        self,
        chat_id: str,
        path: str,
        *,
        caption: str | None = None,
        reply_to: str | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> SendResult:
        return await self._media("video", chat_id, path, caption, meta, reply_to)

    async def send_document(
        self,
        chat_id: str,
        path: str,
        *,
        caption: str | None = None,
        reply_to: str | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> SendResult:
        return await self._media("document", chat_id, path, caption, meta, reply_to)

    # -- outbound: prompts -------------------------------------------------------------------------

    async def send_approval(
        self, chat_id: str, prompt: ApprovalPrompt, meta: Mapping[str, Any] | None = None
    ) -> SendResult:
        """Buttons where the platform has them, else the text card with /approve hints."""
        if not self.capabilities.supports_buttons or not prompt.actions:
            return await self.send(chat_id, prompt.fallback_text, meta=meta)
        try:
            async with self.slots.hold(chat_id):
                result = await self._send_approval(chat_id, prompt, _merged_meta(meta))
        except Exception as exc:
            return self._failed(exc)
        return self._scrubbed(result)

    async def _send_approval(
        self, chat_id: str, prompt: ApprovalPrompt, meta: dict[str, Any]
    ) -> SendResult:
        return SendResult.refused("approval buttons")

    async def send_clarify(
        self, chat_id: str, prompt: ClarifyPrompt, meta: Mapping[str, Any] | None = None
    ) -> SendResult:
        """Buttons for the choices where the platform has them, else numbered text."""
        if not self.capabilities.supports_buttons or not prompt.choices:
            return await self.send(chat_id, prompt.fallback_text or prompt.question, meta=meta)
        try:
            async with self.slots.hold(chat_id):
                result = await self._send_clarify(chat_id, prompt, _merged_meta(meta))
        except Exception as exc:
            return self._failed(exc)
        return self._scrubbed(result)

    async def _send_clarify(
        self, chat_id: str, prompt: ClarifyPrompt, meta: dict[str, Any]
    ) -> SendResult:
        return SendResult.refused("clarify buttons")

    async def send_notice_private(
        self,
        chat_id: str,
        user_id: str,
        text: str,
        meta: Mapping[str, Any] | None = None,
    ) -> SendResult:
        """A notice only ``user_id`` sees. Refused unless the platform has private notices:
        a plain send into a group would show it to everyone."""
        try:
            async with self.slots.hold(chat_id):
                result = await self._send_private(chat_id, user_id, text, _merged_meta(meta))
        except Exception as exc:
            return self._failed(exc)
        return self._scrubbed(result)

    async def _send_private(
        self, chat_id: str, user_id: str, text: str, meta: dict[str, Any]
    ) -> SendResult:
        return SendResult.refused("private notices")

    # -- access policy (read by the authorization chain, P15-08) -----------------------------------

    def access_policy(self, kind: Literal["dm", "group"]) -> str:
        """This adapter's own ``dm`` / ``group`` policy (``open``, ``allowlist``, ``pairing``,
        ``disabled``); ``""`` when it has none. Only read when ``owns_access_policy``."""
        return ""

    def allows_dm(self, user_id: str) -> bool:
        """Under an own ``allowlist`` DM policy: is ``user_id`` on it?"""
        return False

    def resolved_allowlist_ids(self) -> frozenset[str]:
        """User ids the adapter resolved from names in the platform allowlist (Discord)."""
        return frozenset()

    # -- descriptive -------------------------------------------------------------------------------

    async def chat_info(self, chat_id: str) -> ChatInfo:
        """Name and type of a chat; type None when the platform cannot tell."""
        try:
            return await self._chat_info(chat_id)
        except Exception as exc:
            log.debug("%s: chat info failed: %s", self.platform, self.scrub(str(exc)))
            return ChatInfo(name=str(chat_id))

    async def _chat_info(self, chat_id: str) -> ChatInfo:
        return ChatInfo(name=str(chat_id))

    async def list_channels(self) -> Sequence[ChannelEntry] | None:
        """Chats this bot can post to (channel directory, P15-20); None when the platform
        cannot list them (the directory then uses the chats it has seen)."""
        return None

    def format(self, text: str) -> str:
        """Markdown from the agent → this platform's markup (P15-15)."""
        return formatter_for(self.capabilities.markup).format(text)


__all__ = [
    "APPROVAL_CHOICES",
    "CALLBACK_MAX_BYTES",
    "FINAL_KEY",
    "INTERIM_KEY",
    "REPLY_TO_KEY",
    "Adapter",
    "AdapterConnectError",
    "ApprovalAction",
    "ApprovalChoice",
    "ApprovalPrompt",
    "Capabilities",
    "ChannelEntry",
    "ChatInfo",
    "ClarifyPrompt",
    "DrainProbe",
    "FatalListener",
    "Fetcher",
    "FetcherSeat",
    "InboundSink",
    "LenFn",
    "MediaKind",
    "approval_callback",
    "clarify_callback",
    "code_points",
    "parse_approval_callback",
    "parse_clarify_callback",
    "utf16_units",
]
