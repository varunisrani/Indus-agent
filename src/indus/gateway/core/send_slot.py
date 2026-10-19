"""One outbound slot per (adapter, chat), shared by sends and edits (P15-05, R06.13).

Platforms count edits against the same per-chat allowance as sends, so pacing them separately
draws flood penalties. Every send to a chat waits for the chat's slot and then for the adapter's
spacing since the previous operation started. An interim edit (a streaming refresh) never waits:
when the slot is busy, a send is queued for it, or the spacing has not passed, the edit is
skipped and a later one carries the text. The final edit is never gated; it only stamps the time
so the next send keeps its distance. Chats never block each other.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field

from indus.foundation.clock import Clock

# Idle per-chat records are dropped once this many exist.
_PRUNE_ABOVE = 256


@dataclass
class _Chat:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    waiting: int = 0
    last_start: float | None = None


class SendSlots:
    def __init__(self, clock: Clock, spacing: float = 0.0) -> None:
        if spacing < 0:
            raise ValueError("spacing must be >= 0")
        self.clock = clock
        self.spacing = spacing
        self._chats: dict[str, _Chat] = {}

    def _chat(self, chat_id: str) -> _Chat:
        key = str(chat_id)
        state = self._chats.get(key)
        if state is None:
            if len(self._chats) >= _PRUNE_ABOVE:
                self._prune()
            state = _Chat()
            self._chats[key] = state
        return state

    def _idle(self, state: _Chat, now: float) -> bool:
        if state.lock.locked() or state.waiting:
            return False
        return state.last_start is None or now - state.last_start >= self.spacing

    def _prune(self) -> None:
        now = self.clock.monotonic()
        for key in [k for k, s in self._chats.items() if self._idle(s, now)]:
            del self._chats[key]

    def _due_in(self, state: _Chat) -> float:
        if self.spacing <= 0 or state.last_start is None:
            return 0.0
        return state.last_start + self.spacing - self.clock.monotonic()

    async def _pace(self, state: _Chat) -> None:
        wait = self._due_in(state)
        if wait > 0:
            await self.clock.sleep(wait)
        state.last_start = self.clock.monotonic()

    @asynccontextmanager
    async def hold(self, chat_id: str) -> AsyncIterator[None]:
        """Wait for the chat's slot and its spacing, then hold the slot for one operation."""
        state = self._chat(chat_id)
        state.waiting += 1
        try:
            await state.lock.acquire()
        finally:
            state.waiting -= 1
        try:
            await self._pace(state)
            yield
        finally:
            state.lock.release()

    async def pace(self, chat_id: str) -> None:
        """Inside ``hold``: wait out the spacing before the next platform call (split chunks)."""
        await self._pace(self._chat(chat_id))

    def try_interim(self, chat_id: str) -> AbstractAsyncContextManager[None] | None:
        """A held slot for an interim edit, or None when the edit should be skipped."""
        state = self._chat(chat_id)
        if state.lock.locked() or state.waiting or self._due_in(state) > 0:
            return None
        return self._interim(state)

    @asynccontextmanager
    async def _interim(self, state: _Chat) -> AsyncIterator[None]:
        await state.lock.acquire()
        try:
            state.last_start = self.clock.monotonic()
            yield
        finally:
            state.lock.release()

    def mark(self, chat_id: str) -> None:
        """A final edit: never waits, but the next send keeps its distance from it."""
        self._chat(chat_id).last_start = self.clock.monotonic()

    def busy(self, chat_id: str) -> bool:
        state = self._chats.get(str(chat_id))
        return state is not None and (state.lock.locked() or state.waiting > 0)


__all__ = ["SendSlots"]
