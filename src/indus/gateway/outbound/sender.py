"""The one path for user-facing final replies (P15-17).

``OutboundSender.deliver`` runs, in order:

1. dead-target check (unsolicited deliveries only: a reply to an inbound message proves the chat
   alive);
2. the silence rule (``filters.decide_silence``): send nothing, or a short notice;
3. media directives out (``media_policy``): cleared files are sent natively after the text,
   refused ones become short notices;
4. secrets masked (P05 redactor, forced) — before formatting and chunking, so a secret split
   across two pieces is still masked;
5. platform markup (``formatting``) and chunking (``chunker``) under the adapter's limit and
   length function;
6. sends: ``reply_to_mode`` (``off`` / ``first`` / ``all``) decides which pieces thread to the
   user's message; the thread id travels in the metadata; each piece goes out under the P15-04
   retry policy (``send_with_retry``: connection failures and short rate limits retried, a
   piece refused for its markup resent once as plain text, a long rate limit handed back); a
   failure stops the delivery and the report carries the pieces not delivered (never the whole
   reply again, R06.4);
7. bookkeeping: sent pieces go to the sent-message index (P15-03), the dead-target list learns
   from the result, and ephemeral system notices are deleted after their TTL where the
   platform can delete.

The delivery ledger (P15-19): a turn's final (``FinalReply.session_key`` and ``message_ref``
set) is recorded ``pending`` before the first send, ``attempting`` just before it, then
``delivered`` or ``failed`` with the pieces still owed; the ledger then owns any retry, so no
"delivery failed" notice is sent for it. A redelivery names its row (``ledger_row``) and may
carry only the pieces still owed (``resume_pieces``), sent after its label.

``allow_media=False`` sends media tags as plain text: callers whose text is not the agent's own
reply to this chat (cron output, ``indus send``) attach only the cleared ``files`` they pass.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from indus.foundation.scope import Scope
from indus.gateway.adapters.base import Adapter
from indus.gateway.core.send import DELIVERY_FAILED_NOTICE, ErrorKind, SendResult
from indus.gateway.core.sent_index import SentIndex
from indus.gateway.outbound import ports
from indus.gateway.outbound.chunker import split_message
from indus.gateway.outbound.dead_targets import DeadTargets
from indus.gateway.outbound.filters import SILENT_NOTICE, decide_silence, redact_final
from indus.gateway.outbound.media_policy import (
    MediaItem,
    MediaPolicy,
    MediaRef,
    RemoteFiles,
    extract_media,
)

if TYPE_CHECKING:
    from indus.gateway.delivery.ledger import DeliveryLedger

__all__ = ["DeliveryReport", "FinalReply", "OutboundSender", "ReplyToMode"]

type ReplyToMode = Literal["off", "first", "all"]
type Outcome = Literal["sent", "silent", "skipped_dead", "failed", "empty"]


@dataclass(frozen=True)
class FinalReply:
    text: str
    failed: bool = False
    """The turn failed: never silenced."""
    reply_expected: bool | None = None
    """What the adapter knows about the inbound message (None: unknown, treated as a person)."""
    internal: bool = False
    """The turn was an internal wake, not a person's message."""
    reply_to: str | None = None
    thread_id: str | None = None
    unsolicited: bool = False
    """Cron output, notices: no inbound message proves the chat alive."""
    ephemeral: bool = False
    """A system notice that deletes itself after the TTL."""
    ttl: float | None = None
    """Overrides the sender's ``ephemeral_ttl`` for this notice."""
    meta: Mapping[str, Any] = field(default_factory=dict[str, Any])
    session_key: str | None = None
    message_ref: str | None = None
    """A turn's final: with both set (and a ledger) the reply is recorded before it is sent."""
    allow_media: bool = True
    """False: media tags in ``text`` are sent as text, never resolved."""
    files: tuple[MediaItem, ...] = ()
    """Attachments the caller already cleared (sent after the text)."""
    ledger_row: str | None = None
    """A redelivery of this ledger row (no new row is recorded)."""
    resume_pieces: tuple[str, ...] = ()
    """Already formatted pieces still owed by an earlier, partly sent delivery."""


@dataclass
class DeliveryReport:
    outcome: Outcome
    message_ids: list[str] = field(default_factory=list[str])
    failure: SendResult | None = None
    undelivered: list[str] = field(default_factory=list[str])
    """Formatted pieces not delivered after a failure (resume these, never the whole reply)."""
    notices: list[str] = field(default_factory=list[str])


class OutboundSender:
    def __init__(
        self,
        port: Adapter,
        *,
        scope: Scope,
        platform: str | None = None,
        dead_targets: DeadTargets | None = None,
        sent_index: SentIndex | None = None,
        media: MediaPolicy | None = None,
        remote: RemoteFiles | None = None,
        reply_to_mode: ReplyToMode = "first",
        ephemeral_ttl: float = 0.0,
        ledger: DeliveryLedger | None = None,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._port = port
        self._scope = scope
        self._platform = platform or ports.platform_of(port)
        self._dead = dead_targets
        self._sent_index = sent_index
        self._media = media if media is not None else MediaPolicy.for_scope(scope)
        self._remote = remote
        self._reply_to_mode: ReplyToMode = reply_to_mode
        self._ephemeral_ttl = ephemeral_ttl
        self._ledger = ledger
        self._rng = rng
        self._expiring: set[asyncio.Task[None]] = set()

    @property
    def platform(self) -> str:
        return self._platform

    @property
    def adapter(self) -> Adapter:
        return self._port

    def _reply_to(self, reply: FinalReply, index: int) -> str | None:
        if reply.reply_to is None or self._reply_to_mode == "off":
            return None
        if self._reply_to_mode == "all" or index == 0:
            return reply.reply_to
        return None

    def _learn(self, chat_id: str, result: SendResult) -> None:
        if self._dead is not None:
            self._dead.note(self._platform, chat_id, result)

    async def _open_row(self, chat_id: str, reply: FinalReply, content: str) -> str | None:
        ledger = self._ledger
        if ledger is None:
            return None
        if reply.ledger_row is not None:
            return reply.ledger_row
        if not reply.session_key or not reply.message_ref or not content.strip():
            return None
        return await ledger.open(
            session_key=reply.session_key,
            message_ref=reply.message_ref,
            platform=self._platform,
            chat_id=chat_id,
            thread_id=reply.thread_id,
            content=content,
        )

    async def _settle_failure(
        self, row: str | None, result: SendResult, owed: list[str], landed: bool
    ) -> None:
        if row is None or self._ledger is None:
            return
        reconnect_only = result.kind is ErrorKind.TRANSIENT and not self._port.connected
        await self._ledger.failed(
            row, result, pieces=owed if landed else (), reconnect_only=reconnect_only
        )

    async def deliver(self, chat_id: str, reply: FinalReply) -> DeliveryReport:
        if (
            reply.unsolicited
            and self._dead is not None
            and self._dead.is_dead(self._platform, chat_id)
        ):
            return DeliveryReport("skipped_dead")
        resuming = bool(reply.resume_pieces)
        decision = (
            "deliver"
            if resuming
            else decide_silence(
                reply.text,
                failed=reply.failed,
                reply_expected=reply.reply_expected,
                internal=reply.internal,
            )
        )
        if decision == "silent":
            return DeliveryReport("silent")
        text = SILENT_NOTICE if decision == "notice" else reply.text
        refs: list[MediaRef] = []
        clean = text
        if reply.allow_media:
            clean, refs = extract_media(text)
        items, notices = await self._media.resolve(refs, self._remote)
        items = [*items, *reply.files]
        clean = redact_final(clean, self._scope)
        report = DeliveryReport("empty")

        meta = dict(reply.meta)
        if reply.thread_id is not None:
            meta["thread_id"] = reply.thread_id
        fmt = ports.formatter(self._port)
        pieces: list[tuple[str, str]] = []
        if clean.strip():
            chunked = split_message(
                fmt.format(clean),
                ports.max_length(self._port),
                len_fn=ports.length_fn(self._port),
                marker=fmt.marker,
            )
            pieces.extend((c.text, fmt.plain(c.text)) for c in chunked.chunks)
        pieces.extend((p, fmt.plain(p)) for p in reply.resume_pieces if p.strip())
        row = await self._open_row(chat_id, reply, clean) if pieces else None
        notice = None if row is not None else DELIVERY_FAILED_NOTICE
        sends = 0
        if row is not None and self._ledger is not None:
            await self._ledger.attempting(row)
        for index, (wire, plain) in enumerate(pieces):
            reply_to = self._reply_to(reply, sends)
            result = await ports.send_final_with_retry(
                self._port,
                chat_id,
                wire,
                plain_text=plain,
                reply_to=reply_to,
                meta=meta,
                notice=notice,
                rng=self._rng,
            )
            self._learn(chat_id, result)
            ids = [i for i in result.ids if i]
            report.message_ids.extend(ids)
            if not result.ok:
                rest = [p for p, _ in pieces[index + 1 :]]
                if result.partial:
                    # only a remainder the adapter proves undelivered may be sent again
                    head = [result.remainder] if result.remainder else []
                else:
                    head = [wire]
                report.outcome = "failed"
                report.failure = result
                report.undelivered = [*head, *rest]
                landed = index > 0 or result.partial
                await self._settle_failure(row, result, report.undelivered, landed)
                return report
            sends += 1
            if self._sent_index is not None and result.message_id:
                self._sent_index.record(self._platform, chat_id, result.message_id, plain)
        if row is not None and self._ledger is not None:
            await self._ledger.delivered(row)

        for item in items:
            result = await ports.send_media_file(
                self._port,
                chat_id,
                item.kind,
                item.target,
                reply_to=self._reply_to(reply, sends),
                meta=meta,
            )
            if not result.ok:
                notices.append(f"Could not send {item.name}.")
                if not result.unsupported:
                    self._learn(chat_id, result)
                continue
            sends += 1
            report.message_ids.extend(i for i in (result.message_id,) if i)

        for notice_text in notices:
            masked = redact_final(notice_text, self._scope)
            result = await ports.send_text(self._port, chat_id, masked, final=True, meta=meta)
            if result.ok:
                sends += 1
                report.message_ids.extend(i for i in (result.message_id,) if i)
            report.notices.append(masked)

        if sends:
            report.outcome = "sent"
        ttl = reply.ttl if reply.ttl is not None else self._ephemeral_ttl
        if reply.ephemeral and ttl > 0 and ports.can_delete(self._port):
            self._expire_later(chat_id, list(report.message_ids), ttl)
        return report

    # -- ephemeral notices -----------------------------------------------------------------------

    def _expire_later(self, chat_id: str, message_ids: list[str], ttl: float) -> None:
        async def expire() -> None:
            await self._scope.clock.sleep(ttl)
            for message_id in message_ids:
                await ports.delete_message(self._port, chat_id, message_id)

        task = asyncio.create_task(expire())
        self._expiring.add(task)
        task.add_done_callback(self._expiring.discard)

    async def aclose(self) -> None:
        """Cancel pending ephemeral deletions (the messages simply stay)."""
        tasks = list(self._expiring)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
