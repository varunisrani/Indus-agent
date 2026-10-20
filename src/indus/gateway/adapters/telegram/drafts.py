"""Telegram native drafts (P16-33, R06.12, R06.13).

``sendMessageDraft`` shows an animated preview of a reply in a private chat: every frame with the
same draft id updates one draft. A draft is only a preview, so the reply itself still goes out once
as a normal message (MarkdownV2, plain on a markup refusal); Telegram then shows the message where
the draft was. `DraftStream` is the `NativeStream` (``adapters._native``) over it:

- frames carry plain text (no MarkdownV2 escaping, no fence closing, no cursor) and only ever
  extend the previous one; a frame that does not is skipped;
- a frame takes the chat's send slot like an interim edit (R06.13): while a send holds it, the
  frame is skipped and a later one carries the text;
- text over 4,096 UTF-16 units freezes the draft at what it shows (the final send splits);
- a flood answer holds frames back (``FloodGate``: the server's wait or a doubling hold, capped at
  30 s; three in a row stop frames); a lost answer (timeout, network) skips the frame, three in a
  row end the stream; any other refusal ends it (``push`` → False, the caller edits instead), and a
  refusal that says drafts cannot work here (method unknown, not a private chat, topics) also turns
  drafts off for the adapter;
- ``finish`` sends the final once; a second final with the same text answers the first result, one
  with other text edits the sent message (never a second post).

Each segment of a reply is its own draft (``one_message_per_turn`` is False): the caller finishes
the stream at a tool call and opens a new one, so each text block becomes its own message.
"""

from __future__ import annotations

import itertools
import logging
import secrets
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Protocol

from indus.foundation.clock import Clock
from indus.gateway.adapters._native import FloodGate, extends
from indus.gateway.adapters.telegram.bot_api import (
    NetworkError,
    RetryAfter,
    TelegramError,
    Unauthorized,
)
from indus.gateway.core.send import ErrorKind, SendResult
from indus.gateway.outbound.chunker import utf16_units

if TYPE_CHECKING:
    from indus.gateway.adapters.telegram.bot_api import BotApi
    from indus.gateway.core.send_slot import SendSlots

__all__ = [
    "DRAFT_MAX_UNITS",
    "LOST_LIMIT",
    "DraftHost",
    "DraftStream",
    "draft_unsupported",
    "next_draft_id",
]

log = logging.getLogger(__name__)

DRAFT_MAX_UNITS = 4096
LOST_LIMIT = 3
# Refusals that mean drafts cannot work for this bot or chat at all (a Bot API server without the
# method answers 404 "Not Found", which our client raises as ``Unauthorized``).
_UNSUPPORTED = (
    "draft",
    "peer_invalid",
    "not supported",
    "private chat",
    "can't be used",
    "cannot be used",
    "topic",
)

# Draft ids: non-zero, unique per stream in this process, inside 31 bits. A random start keeps
# two processes (a restart) from reusing each other's ids for one chat.
_ids = itertools.count(secrets.randbelow(1 << 29) + 1)


def next_draft_id() -> int:
    value = next(_ids)
    return (value % ((1 << 31) - 1)) or 1


def draft_unsupported(exc: TelegramError) -> bool:
    """A refusal saying drafts cannot be used here (as opposed to one bad frame)."""
    text = exc.message.lower()
    if isinstance(exc, Unauthorized):
        return text.strip() == "not found"
    return any(marker in text for marker in _UNSUPPORTED)


class DraftHost(Protocol):
    """What a draft stream needs from the Telegram adapter."""

    clock: Clock
    slots: SendSlots
    bot: BotApi

    def format(self, text: str) -> str: ...

    def scrub(self, text: str) -> str: ...

    def drafts_unavailable(self, reason: str, *, in_topics: bool = False) -> None: ...

    async def send(
        self,
        chat_id: str,
        text: str,
        *,
        reply_to: str | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> SendResult: ...

    async def edit(
        self,
        chat_id: str,
        message_id: str,
        text: str,
        *,
        final: bool = False,
        meta: Mapping[str, Any] | None = None,
    ) -> SendResult: ...


class DraftStream:
    """One reply segment shown as a Telegram draft (see the module notes)."""

    one_message_per_turn = False

    def __init__(
        self,
        host: DraftHost,
        chat_id: str,
        *,
        thread: int | None = None,
        reply_to: str | None = None,
        draft_id: int | None = None,
    ) -> None:
        self.host = host
        self.chat_id = chat_id
        self.thread = thread
        self.reply_to = reply_to
        self.draft_id = draft_id if draft_id else next_draft_id()
        self.gate = FloodGate(host.clock)
        self.frames: list[str] = []
        self.sent = ""
        self.dead = False
        self.frozen = False
        self.lost = 0
        self.result: SendResult | None = None
        self.final_text: str | None = None

    def __repr__(self) -> str:
        return f"<DraftStream draft={self.draft_id} frames={len(self.frames)}>"

    @property
    def meta(self) -> dict[str, Any]:
        return {"thread_id": str(self.thread)} if self.thread is not None else {}

    # -- interim frames ----------------------------------------------------------------------------

    async def push(self, text: str) -> bool:
        if self.dead or self.result is not None:
            return False
        if self.frozen or not extends(self.sent, text):
            return True
        if utf16_units(text) > DRAFT_MAX_UNITS:
            self.frozen = True
            return True
        if not self.gate.open():
            return True
        held = self.host.slots.try_interim(self.chat_id)
        if held is None:
            return True  # a send holds the chat's slot; a later frame carries the text
        try:
            async with held:
                accepted = await self.host.bot.send_message_draft(
                    self.chat_id, self.draft_id, text, message_thread_id=self.thread
                )
        except RetryAfter as exc:
            self.gate.flood(exc.retry_after)
            log.info("telegram: draft frame flood-limited; holding frames %.1f s", exc.retry_after)
            return True
        except NetworkError as exc:  # timeouts included: the frame may or may not have shown
            self.lost += 1
            log.debug("telegram: draft frame lost: %s", self.host.scrub(str(exc)))
            if self.lost >= LOST_LIMIT:
                self.dead = True
                return False
            return True
        except TelegramError as exc:
            return self._refused(exc)
        if not accepted:
            return self._refused(TelegramError("sendMessageDraft answered false"))
        self.lost = 0
        self.gate.passed()
        self.sent = text
        self.frames.append(text)
        return True

    def _refused(self, exc: TelegramError) -> bool:
        self.dead = True
        reason = self.host.scrub(exc.message)
        if draft_unsupported(exc):
            self.host.drafts_unavailable(reason, in_topics=self.thread is not None)
        else:
            log.info("telegram: draft refused (%s); the reply continues without drafts", reason)
        return False

    # -- the final ---------------------------------------------------------------------------------

    async def finish(self, text: str) -> SendResult:
        if self.result is not None:
            return await self._again(text)
        result = await self.host.send(
            self.chat_id, self.host.format(text), reply_to=self.reply_to, meta=self.meta
        )
        if not result.ok and result.kind is ErrorKind.BAD_FORMAT:
            result = await self.host.send(
                self.chat_id, text, reply_to=self.reply_to, meta={**self.meta, "plain": True}
            )
        if result.ok:
            self.result, self.final_text = result, text
        return result

    async def _again(self, text: str) -> SendResult:
        """A final for a stream already finished: never a second message."""
        first = self.result
        assert first is not None
        if text == self.final_text or not first.message_id or first.continuation_ids:
            if text != self.final_text:
                log.info("telegram: a second final for a split reply was not applied")
            return first
        edited = await self.host.edit(
            self.chat_id, first.message_id, self.host.format(text), final=True, meta=self.meta
        )
        if not edited.ok and edited.kind is ErrorKind.BAD_FORMAT:
            edited = await self.host.edit(
                self.chat_id, first.message_id, text, final=True, meta={**self.meta, "plain": True}
            )
        if edited.ok:
            self.final_text = text
            return first
        return edited

    async def abandon(self) -> None:
        """Drafts disappear on their own; nothing is left open on Telegram's side."""
        self.dead = True
