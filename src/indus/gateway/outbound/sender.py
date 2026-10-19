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
   user's message; the thread id travels in the metadata; a piece refused for its markup is
   resent once as plain text; a failure stops the delivery and the report carries the pieces
   not delivered (never the whole reply again, R06.4);
7. bookkeeping: sent pieces go to the sent-message index (P15-03), the dead-target list learns
   from the result, and ephemeral system notices are deleted after their TTL where the
   platform can delete.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from indus.foundation.scope import Scope
from indus.gateway.adapters.base import Adapter
from indus.gateway.core.send import SendResult
from indus.gateway.core.sent_index import SentIndex
from indus.gateway.outbound import ports
from indus.gateway.outbound.chunker import split_message
from indus.gateway.outbound.dead_targets import DeadTargets
from indus.gateway.outbound.filters import SILENT_NOTICE, decide_silence, redact_final
from indus.gateway.outbound.media_policy import MediaPolicy, RemoteFiles, extract_media

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
        self._expiring: set[asyncio.Task[None]] = set()

    def _reply_to(self, reply: FinalReply, index: int) -> str | None:
        if reply.reply_to is None or self._reply_to_mode == "off":
            return None
        if self._reply_to_mode == "all" or index == 0:
            return reply.reply_to
        return None

    def _learn(self, chat_id: str, result: SendResult) -> None:
        if self._dead is not None:
            self._dead.note(self._platform, chat_id, result)

    async def deliver(self, chat_id: str, reply: FinalReply) -> DeliveryReport:
        if (
            reply.unsolicited
            and self._dead is not None
            and self._dead.is_dead(self._platform, chat_id)
        ):
            return DeliveryReport("skipped_dead")
        decision = decide_silence(
            reply.text,
            failed=reply.failed,
            reply_expected=reply.reply_expected,
            internal=reply.internal,
        )
        if decision == "silent":
            return DeliveryReport("silent")
        text = SILENT_NOTICE if decision == "notice" else reply.text
        clean, refs = extract_media(text)
        items, notices = await self._media.resolve(refs, self._remote)
        clean = redact_final(clean, self._scope)
        report = DeliveryReport("empty")

        meta = dict(reply.meta)
        if reply.thread_id is not None:
            meta["thread_id"] = reply.thread_id
        fmt = ports.formatter(self._port)
        sends = 0
        if clean.strip():
            chunked = split_message(
                fmt.format(clean),
                ports.max_length(self._port),
                len_fn=ports.length_fn(self._port),
                marker=fmt.marker,
            )
            for index, chunk in enumerate(chunked.chunks):
                reply_to = self._reply_to(reply, sends)
                result = await ports.send_text(
                    self._port, chat_id, chunk.text, final=True, reply_to=reply_to, meta=meta
                )
                if not result.ok and ports.is_bad_format(result):
                    result = await ports.send_text(
                        self._port,
                        chat_id,
                        fmt.plain(chunk.text),
                        final=True,
                        reply_to=reply_to,
                        meta=meta,
                    )
                self._learn(chat_id, result)
                if not result.ok:
                    report.outcome = "failed"
                    report.failure = result
                    report.undelivered = [c.text for c in chunked.chunks[index:]]
                    return report
                sends += 1
                ids = [*result.continuation_ids, result.message_id]
                report.message_ids.extend(i for i in ids if i)
                if self._sent_index is not None and result.message_id:
                    self._sent_index.record(
                        self._platform, chat_id, result.message_id, fmt.plain(chunk.text)
                    )

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

        for notice in notices:
            masked = redact_final(notice, self._scope)
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
