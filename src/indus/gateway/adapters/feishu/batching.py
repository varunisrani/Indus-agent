"""Merging bursts of Feishu messages into one event (P16-14).

Feishu clients split a long paste into several messages and send albums as one message per image,
so a burst from one person would otherwise start several turns. `Batcher` holds such events for a
short quiet window and hands one merged event on:

- plain text (not a command, no button, no attachments): one batch per chat, thread, sender, reply
  target and mention state; it goes out ``text_delay_s`` after its newest message, or at once when
  one more message would pass ``text_max_messages`` or ``text_max_chars`` (that message then
  starts the next batch); texts are joined with newlines;
- photos, videos and documents: one batch per chat, thread, sender and kind, ``media_delay_s``
  after the newest; attachments are concatenated, captions joined;
- everything else (commands, button presses, voice notes, stickers) goes out at once, after any
  batch of the same chat, thread and sender has gone out, so order is kept.

Times run on the adapter's clock; ``flush_all`` sends every waiting batch (the adapter calls it
when it disconnects).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from indus.foundation.clock import Clock
from indus.gateway.core.events import InboundEvent, MessageKind

__all__ = ["BatchSettings", "Batcher"]

_MEDIA_KINDS = frozenset({MessageKind.PHOTO, MessageKind.VIDEO, MessageKind.DOCUMENT})

type Emit = Callable[[InboundEvent], Awaitable[None]]
type _Conversation = tuple[str, str, str]
type _Key = tuple[str, ...]


@dataclass(frozen=True)
class BatchSettings:
    text_delay_s: float = 0.6
    text_max_messages: int = 8
    text_max_chars: int = 4000
    media_delay_s: float = 0.8


@dataclass
class _Batch:
    key: _Key
    event: InboundEvent
    count: int
    timer: asyncio.Task[None] | None = None


def _conversation(event: InboundEvent) -> _Conversation:
    source = event.source
    return (source.chat_id, source.thread_id or "", source.user_id or "")


def _joined(first: str, second: str) -> str:
    if first and second:
        return f"{first}\n{second}"
    return first or second


class Batcher:
    """See the module notes."""

    def __init__(self, clock: Clock, emit: Emit, settings: BatchSettings | None = None) -> None:
        self.clock = clock
        self._emit = emit
        self.settings = settings or BatchSettings()
        self._batches: dict[_Key, _Batch] = {}

    def __len__(self) -> int:
        return len(self._batches)

    def _key_of(self, event: InboundEvent) -> tuple[str, _Key] | None:
        chat, thread, user = _conversation(event)
        if event.metadata.get("button") is not None or event.is_command:
            return None
        if event.kind is MessageKind.TEXT and not event.media:
            mentioned = "m" if event.metadata.get("mentioned") is True else ""
            return "text", ("text", chat, thread, user, event.reply_to_id or "", mentioned)
        if event.kind in _MEDIA_KINDS and event.media:
            return "media", ("media", chat, thread, user, event.kind.value)
        return None

    async def offer(self, event: InboundEvent) -> None:
        found = self._key_of(event)
        conversation = _conversation(event)
        # Anything waiting for this conversation under another key goes first (order kept).
        for batch in list(self._batches.values()):
            if (found is None or batch.key != found[1]) and batch.key[1:4] == conversation:
                await self._flush(batch)
        if found is None:
            await self._emit(event)
            return
        group, key = found
        current = self._batches.get(key)
        if current is None:
            self._start(key, event, group)
            return
        if group == "text":
            text = _joined(current.event.text, event.text)
            full = current.count + 1 > self.settings.text_max_messages or (
                len(text) > self.settings.text_max_chars
            )
            if full:
                await self._flush(current)
                self._start(key, event, group)
                return
            merged = self._merge(current.event, event, text=text, media=current.event.media)
        else:
            text = _joined(current.event.text, event.text)
            merged = self._merge(
                current.event, event, text=text, media=(*current.event.media, *event.media)
            )
        current.event = merged
        current.count += 1
        self._arm(current, group)

    @staticmethod
    def _merge(
        first: InboundEvent, newest: InboundEvent, *, text: str, media: tuple[object, ...]
    ) -> InboundEvent:
        metadata = dict(first.metadata)
        if newest.metadata.get("mentioned") is True:
            metadata["mentioned"] = True
        metadata["batched"] = int(first.metadata.get("batched") or 1) + 1
        return first.model_copy(
            update={
                "text": text,
                "media": media,
                "message_id": newest.message_id or first.message_id,
                "timestamp": newest.timestamp or first.timestamp,
                "metadata": metadata,
            }
        )

    def _start(self, key: _Key, event: InboundEvent, group: str) -> None:
        batch = _Batch(key, event, 1)
        self._batches[key] = batch
        self._arm(batch, group)

    def _arm(self, batch: _Batch, group: str) -> None:
        if batch.timer is not None and not batch.timer.done():
            batch.timer.cancel()
        delay = self.settings.text_delay_s if group == "text" else self.settings.media_delay_s
        batch.timer = asyncio.ensure_future(self._later(batch, delay))

    async def _later(self, batch: _Batch, delay: float) -> None:
        await self.clock.sleep(delay)
        if self._batches.get(batch.key) is batch:
            batch.timer = None  # running now: a flush must not cancel the emit below
            await self._flush(batch)

    async def _flush(self, batch: _Batch) -> None:
        if self._batches.get(batch.key) is not batch:
            return
        del self._batches[batch.key]
        timer, batch.timer = batch.timer, None
        if timer is not None and timer is not asyncio.current_task() and not timer.done():
            timer.cancel()
        await self._emit(batch.event)

    async def flush_all(self) -> None:
        for batch in list(self._batches.values()):
            await self._flush(batch)

    async def aclose(self) -> None:
        """Send what is waiting, then stop every timer."""
        timers = [b.timer for b in self._batches.values() if b.timer is not None]
        await self.flush_all()
        for timer in timers:
            if not timer.done():
                timer.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await timer
