"""Quiet-window batching for inbound chat messages (P15-12).

Chat clients cut one long paste into several messages, and people send a correction in three
quick lines. ``Batcher`` holds items per key and hands the merged result on once the key has been
quiet for a while: each new item restarts the wait, and an optional hard cap bounds the total
wait from the first item. All waiting goes through the injected clock.

Two uses in the gateway:

- split pastes (``TextBatchPolicy``): plain text is held for a 0.3 s quiet window, or 1.0 s when
  the last piece ended within 100 units of the platform's split threshold (a continuation is then
  almost certain). Platform ``extra`` keys ``text_batch_delay_seconds`` and
  ``text_batch_split_delay_seconds`` change the windows, clamped to 2.0 s and 4.0 s;
- busy text (``busy.py``): text sent while a reply runs waits 0.35 s of quiet, at most 1.0 s.

``merge_events`` joins two inbound events: texts on separate lines, media lists concatenated, a
photo anywhere makes the whole a photo message (an album with its caption).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from indus.foundation.clock import Clock
from indus.gateway.core.events import InboundEvent, MessageKind

__all__ = [
    "MAX_QUIET_S",
    "MAX_SPLIT_QUIET_S",
    "Batcher",
    "TextBatchPolicy",
    "merge_events",
    "merge_text",
]

log = logging.getLogger(__name__)

MAX_QUIET_S = 2.0
MAX_SPLIT_QUIET_S = 4.0


def merge_text(first: str, later: str) -> str:
    """Two pieces of text on separate lines; an empty side adds nothing."""
    if not first:
        return later
    if not later:
        return first
    return f"{first}\n{later}"


def merge_events(first: InboundEvent, later: InboundEvent) -> InboundEvent:
    """``later`` folded into ``first`` (ids and source of the first piece are kept)."""
    kinds = {first.kind, later.kind}
    if MessageKind.PHOTO in kinds:
        kind = MessageKind.PHOTO
    elif first.kind is MessageKind.TEXT:
        kind = later.kind
    else:
        kind = first.kind
    expected: bool | None
    if first.reply_expected is None and later.reply_expected is None:
        expected = None
    else:
        expected = bool(first.reply_expected) or bool(later.reply_expected)
    metadata: dict[str, Any] = {**later.metadata, **first.metadata}
    metadata["merged"] = int(first.metadata.get("merged", 1)) + int(later.metadata.get("merged", 1))
    return first.model_copy(
        update={
            "text": merge_text(first.text, later.text),
            "kind": kind,
            "media": (*first.media, *later.media),
            "reply_expected": expected,
            "metadata": metadata,
        }
    )


def _seconds(raw: object, default: float, *, low: float, high: float) -> float:
    value = default
    if not isinstance(raw, bool) and raw is not None:
        try:
            number = float(raw)  # pyright: ignore[reportArgumentType]
        except (TypeError, ValueError):
            number = default
        if math.isfinite(number) and number >= 0:
            value = number
    return min(max(value, low), high)


@dataclass(frozen=True)
class TextBatchPolicy:
    """How long split-paste pieces are held (see the module notes)."""

    quiet: float = 0.3
    split_quiet: float = 1.0
    split_threshold: int = 4000
    near: int = 100

    def delay_for(self, last_len: int) -> float:
        """The quiet window after a piece of ``last_len`` units."""
        if last_len >= self.split_threshold - self.near:
            return self.split_quiet
        return self.quiet

    @classmethod
    def from_extra(
        cls, extra: Mapping[str, Any] | None, *, split_threshold: int | None = None
    ) -> TextBatchPolicy:
        data: Mapping[str, Any] = extra if isinstance(extra, Mapping) else {}
        base = cls()
        quiet = _seconds(
            data.get("text_batch_delay_seconds"), base.quiet, low=0.0, high=MAX_QUIET_S
        )
        split = _seconds(
            data.get("text_batch_split_delay_seconds"),
            base.split_quiet,
            low=quiet,
            high=MAX_SPLIT_QUIET_S,
        )
        threshold = split_threshold if split_threshold and split_threshold > 0 else 4000
        return cls(quiet=quiet, split_quiet=split, split_threshold=threshold)


@dataclass
class _Entry[T]:
    value: T
    last: T
    first_at: float
    last_at: float
    token: int
    task: asyncio.Task[None] | None = None


class Batcher[T]:
    """Per-key quiet-window batching (see the module notes). Use on one event loop."""

    def __init__(
        self,
        clock: Clock,
        *,
        flush: Callable[[str, T], Awaitable[None]],
        merge: Callable[[T, T], T],
        delay_for: Callable[[T], float],
        cap: float | Callable[[], float | None] | None = None,
    ) -> None:
        self._clock = clock
        self._flush = flush
        self._merge = merge
        self._delay_for = delay_for
        self._cap = cap
        self._entries: dict[str, _Entry[T]] = {}
        self._tokens = 0
        self._flushing: set[asyncio.Task[None]] = set()

    def pending(self, key: str) -> T | None:
        entry = self._entries.get(key)
        return None if entry is None else entry.value

    def keys(self) -> list[str]:
        return list(self._entries)

    def add(self, key: str, item: T) -> None:
        """Hold ``item`` under ``key`` and restart the key's quiet window."""
        self._tokens += 1
        now = self._clock.monotonic()
        entry = self._entries.get(key)
        if entry is None:
            entry = _Entry(item, item, now, now, self._tokens)
            self._entries[key] = entry
        else:
            entry.value = self._merge(entry.value, item)
            entry.last = item
            entry.last_at = now
            entry.token = self._tokens
            if entry.task is not None:
                entry.task.cancel()
        entry.task = asyncio.ensure_future(self._wait(key, entry.token))

    def _delay(self, entry: _Entry[T]) -> float:
        """Seconds left from now: the quiet window counts from the last piece's arrival."""
        try:
            quiet = max(0.0, float(self._delay_for(entry.last)))
        except Exception:
            log.warning("batch delay lookup failed; flushing at once", exc_info=True)
            quiet = 0.0
        delay = max(0.0, entry.last_at + quiet - self._clock.monotonic())
        try:
            cap = self._cap() if callable(self._cap) else self._cap
        except Exception:
            cap = None
        if cap is not None:
            left = entry.first_at + cap - self._clock.monotonic()
            delay = min(delay, max(0.0, left))
        return delay

    async def _wait(self, key: str, token: int) -> None:
        entry = self._entries.get(key)
        if entry is None:
            return
        try:
            await self._clock.sleep(self._delay(entry))
        except asyncio.CancelledError:
            return
        current = self._entries.get(key)
        # A newer piece restarted the window: its own timer flushes (checked before any await).
        if current is None or current.token != token:
            return
        del self._entries[key]
        await self._run_flush(key, current.value)

    async def _run_flush(self, key: str, value: T) -> None:
        task = asyncio.ensure_future(self._flush(key, value))
        self._flushing.add(task)
        task.add_done_callback(self._flushing.discard)
        try:
            # A cancel of the timer must not abort the hand-off it already started.
            await asyncio.shield(task)
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("handing on a batch for %s failed", key)

    async def flush_now(self, key: str) -> bool:
        """Hand on ``key``'s batch at once (no quiet window); False when nothing was held."""
        entry = self._entries.pop(key, None)
        if entry is None:
            return False
        if entry.task is not None:
            entry.task.cancel()
        await self._run_flush(key, entry.value)
        return True

    def discard(self, key: str) -> T | None:
        """Drop ``key``'s batch without handing it on."""
        entry = self._entries.pop(key, None)
        if entry is None:
            return None
        if entry.task is not None:
            entry.task.cancel()
        return entry.value

    async def aclose(self) -> None:
        """Stop every timer; held items are dropped, hand-offs in flight finish."""
        for key in list(self._entries):
            self.discard(key)
        for task in list(self._flushing):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
