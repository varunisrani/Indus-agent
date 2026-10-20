"""Shared pieces of native reply streams (P16-33, R06.12).

A native stream shows a reply while it is written through the platform's own streaming primitive
(Telegram drafts, Slack ``chat.*Stream``) instead of a message the gateway keeps editing. Each
platform's stream object has the shape `NativeStream`:

- ``push(text)``: the whole text so far. Only frames that extend what was already sent go out; an
  identical or rewriting frame is skipped (a later frame or the final carries it). ``True`` while
  the stream is usable, ``False`` once it is not (the caller goes on with edits or plain sends).
- ``finish(text)``: the final, declared by the caller with any text added after streaming. Its
  result names the reply's message. A second final never posts again: the same text answers the
  first result, other text edits the sealed message.
- ``abandon()``: the turn was cancelled; close what is open, best effort.
- ``one_message_per_turn``: True when the stream is the reply message itself and stays open across
  tool calls (Slack); False when each text segment is its own message (Telegram drafts).

``FloodGate`` holds interim frames back after a flood answer: until the server's wait (or the
doubling interval, capped) has passed on the clock; three floods in a row stop interim frames.
Nothing sleeps. ``native_stream_enabled`` reads ``display.platforms.<platform>.native_stream``
(off unless set true).
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from indus.foundation import config as config_mod
from indus.foundation.clock import Clock
from indus.foundation.scope import Scope
from indus.gateway.core.send import SendResult

__all__ = [
    "FLOOD_LIMIT",
    "MAX_HOLD_S",
    "FloodGate",
    "NativeStream",
    "extends",
    "native_stream_enabled",
]

log = logging.getLogger(__name__)

FLOOD_LIMIT = 3
FIRST_HOLD_S = 1.0
MAX_HOLD_S = 30.0


class NativeStream(Protocol):
    """What the stream consumer drives (see the module notes)."""

    @property
    def one_message_per_turn(self) -> bool: ...

    @property
    def frames(self) -> list[str]: ...

    async def push(self, text: str) -> bool: ...

    async def finish(self, text: str) -> SendResult: ...

    async def abandon(self) -> None: ...


def extends(sent: str, text: str) -> bool:
    """``text`` is a strict continuation of ``sent`` (a frame worth sending)."""
    return len(text) > len(sent) and text.startswith(sent)


def native_stream_enabled(scope: Scope, platform: str) -> bool:
    """``display.platforms.<platform>.native_stream`` from the profile's config; False when it is
    unset, not a boolean or unreadable."""
    try:
        view: Any = scope.config if scope.config is not None else config_mod.load(scope)
        value = view.get(f"display.platforms.{platform}.native_stream")
    except Exception as exc:
        log.debug("%s: native stream setting unreadable: %s", platform, type(exc).__name__)
        return False
    return value is True


class FloodGate:
    """Interim frames held back after flood answers (see the module notes)."""

    def __init__(self, clock: Clock, *, limit: int = FLOOD_LIMIT, cap: float = MAX_HOLD_S) -> None:
        self.clock = clock
        self.limit = limit
        self.cap = cap
        self.strikes = 0
        self.hold = FIRST_HOLD_S
        self._until = 0.0
        self.waits: list[float] = []

    @property
    def stopped(self) -> bool:
        """Interim frames stopped for this stream (the final still goes out)."""
        return self.strikes >= self.limit

    def open(self) -> bool:
        """May an interim frame go out now?"""
        return not self.stopped and self.clock.monotonic() >= self._until

    def flood(self, retry_after: float | None) -> None:
        """A flood answer: wait the server's time or the doubling hold, whichever is longer."""
        self.strikes += 1
        wait = min(max(self.hold, retry_after or 0.0), self.cap)
        self.waits.append(wait)
        self._until = self.clock.monotonic() + wait
        self.hold = min(self.hold * 2, self.cap)

    def passed(self) -> None:
        """A frame went through: the strike count starts over (the hold keeps its size)."""
        self.strikes = 0
