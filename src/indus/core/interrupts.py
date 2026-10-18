"""Interrupt, steer and redirect for a running turn (R01.4).

A turn runs as an asyncio task with a **generation** number. ``TurnControl`` is the handle other
code uses while it runs; every method is called on the event loop thread (the in-process host of
P04-21 marshals calls from other threads).

- ``interrupt(generation, hard=..., by_system=...)``: a stale generation is ignored. Otherwise the
  in-flight provider call is cancelled (P03 closes the HTTP stream as its task is cancelled), open
  server requests are withdrawn through the broker (it emits one ``server_request.cancelled`` event
  each), and ``stop`` is set so the executor gives running tools ``cancelled`` and unstarted ones
  ``skipped`` results. A hard interrupt also drops buffered steer text.
- ``steer(text, ticket=None)``: buffered; several join with newlines. The loop delivers it as its
  own ``user`` row (``display_kind="steer"``, wrapped in the steer markers) after the current batch;
  leftovers become the next turn's message. ``withdraw_steer(ticket)`` takes back exactly one
  submission that no drain has consumed yet.
- ``redirect(text)``: during a model call, cancels only that call; the loop then adds a checkpoint
  and the correction (``apply_redirect``) and retries. During tools it degrades to ``steer``. With
  no live model call, or a stop already pending, it returns False.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from indus.core import loop_texts as texts
from indus.core.prompt.texts import STEER_CLOSE, STEER_OPEN
from indus.core.response_checks import is_repetition_dominated
from indus.core.transcript import DISPLAY_STEER, TranscriptRow
from indus.protocol.events import CancelReason
from indus.protocol.requests_port import ServerRequestPort

log = logging.getLogger(__name__)

__all__ = [
    "DISPLAY_HIDDEN",
    "Phase",
    "TurnControl",
    "is_steer_text",
    "redirect_rows",
    "steer_row",
]

type Phase = Literal["idle", "prologue", "model", "tools", "finalize", "done"]
DISPLAY_HIDDEN = "hidden"


def steer_row(text: str) -> TranscriptRow:
    """The standalone user row a steer is delivered as."""
    return TranscriptRow(
        role="user", content=f"{STEER_OPEN}\n{text}\n{STEER_CLOSE}", display_kind=DISPLAY_STEER
    )


def is_steer_text(content: object) -> bool:
    return isinstance(content, str) and content.startswith(STEER_OPEN)


@dataclass
class _Steer:
    ticket: object | None
    text: str


@dataclass
class TurnControl:
    """Control surface of one running turn (see the module notes)."""

    generation: int = 0
    broker: ServerRequestPort | None = None
    phase: Phase = "idle"
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    interrupted: bool = False
    hard: bool = False
    by_system: bool = False
    intervened: bool = False
    _steers: list[_Steer] = field(default_factory=list[_Steer])
    _consumed: dict[object, bool] = field(default_factory=dict[object, bool])
    _redirect: str | None = None
    _model_task: asyncio.Future[Any] | None = None

    # -- interrupt -----------------------------------------------------------------------------

    def interrupt(self, generation: int, *, hard: bool = False, by_system: bool = False) -> bool:
        """Stop the turn of ``generation``; False when that turn is not the one running."""
        if generation != self.generation or self.phase == "done":
            log.debug(
                "interrupt for generation %s ignored (running %s)", generation, self.generation
            )
            return False
        if not self.interrupted:
            self.by_system = by_system
        self.interrupted = True
        self.intervened = True
        self._redirect = None
        if hard:
            self.hard = True
            for steer in self._steers:
                if steer.ticket is not None:
                    self._consumed[steer.ticket] = False
            self._steers.clear()
        self.stop.set()
        self._cancel_model_call()
        self.withdraw_requests(CancelReason.INTERRUPTED)
        log.info("turn %s interrupted (%s)", generation, "hard" if hard else "soft")
        return True

    def withdraw_requests(self, reason: CancelReason) -> int:
        if self.broker is None:
            return 0
        try:
            return self.broker.cancel_all(reason)
        except Exception:
            log.warning("could not withdraw open server requests", exc_info=True)
            return 0

    def human_wait_seconds(self) -> float:
        if self.broker is None:
            return 0.0
        try:
            return float(self.broker.human_wait_seconds())
        except Exception:
            return 0.0

    # -- steer ---------------------------------------------------------------------------------

    def steer(self, text: str, *, ticket: object | None = None) -> bool:
        cleaned = (text or "").strip()
        if not cleaned or self.hard:
            return False
        self._steers.append(_Steer(ticket, cleaned))
        self.intervened = True
        return True

    @property
    def steer_pending(self) -> bool:
        return bool(self._steers)

    def drain_steer(self) -> str | None:
        """All buffered steer text (newline-joined), consumed."""
        if not self._steers:
            return None
        for steer in self._steers:
            if steer.ticket is not None:
                self._consumed[steer.ticket] = True
        text = "\n".join(s.text for s in self._steers)
        self._steers.clear()
        return text

    def leftover_steer(self) -> str | None:
        """Finalize's drain: ticketless text becomes the next message; ticketed text stays
        withdrawable (its submitter decides). Nothing after a hard stop."""
        if self.hard:
            self._steers.clear()
            return None
        loose = [s for s in self._steers if s.ticket is None]
        self._steers = [s for s in self._steers if s.ticket is not None]
        return "\n".join(s.text for s in loose) or None

    def withdraw_steer(self, ticket: object) -> bool:
        """True when ``ticket``'s text was never delivered (taken back now, or dropped unread)."""
        if ticket is None:
            return False
        kept = [s for s in self._steers if s.ticket != ticket]
        if len(kept) != len(self._steers):
            self._steers = kept
            return True
        return self._consumed.pop(ticket, True) is False

    # -- redirect ------------------------------------------------------------------------------

    def redirect(self, text: str) -> bool:
        cleaned = (text or "").strip()
        if not cleaned or self.interrupted or self.phase == "done":
            return False
        if self.phase == "tools":
            return self.steer(cleaned)
        if self.phase != "model" or self._model_task is None or self._model_task.done():
            return False
        self._redirect = (
            f"{self._redirect}\n\n{texts.REDIRECT_MORE}\n{cleaned}" if self._redirect else cleaned
        )
        self.intervened = True
        self._cancel_model_call()
        return True

    def take_redirect(self) -> str | None:
        text, self._redirect = self._redirect, None
        return text

    @property
    def redirect_pending(self) -> bool:
        return self._redirect is not None

    # -- the in-flight call ----------------------------------------------------------------------

    def bind_model_call(self, task: asyncio.Future[Any] | None) -> None:
        self._model_task = task
        if task is not None and self.interrupted:
            task.cancel()

    def _cancel_model_call(self) -> None:
        task = self._model_task
        if task is not None and not task.done():
            task.cancel()


def redirect_rows(visible: str, correction: str, *, tail_role: str | None) -> list[TranscriptRow]:
    """The checkpoint and correction rows for a redirect (no reasoning ever goes in).

    ``visible`` is the reply text shown before the call was cut (think blocks already removed). A
    repetition-loop partial is left out. An assistant checkpoint row is added unless the history
    already ends with an assistant row: the visible text, or a hidden placeholder whose wire text
    is neutral. The correction row stores what the user typed; its ``api_content`` carries the
    interruption note, the partial text and the correction.
    """
    shown = visible.strip()
    parts = [texts.REDIRECT_NOTE]
    if shown and is_repetition_dominated(shown):
        parts.append(texts.REDIRECT_LOOPED)
        shown = ""
    elif shown:
        parts += [texts.REDIRECT_PARTIAL, shown]
    parts += [texts.REDIRECT_CORRECTION, correction]
    rows: list[TranscriptRow] = []
    if tail_role != "assistant":
        if shown:
            rows.append(TranscriptRow(role="assistant", content=shown, finish_reason="interrupted"))
        else:
            rows.append(
                TranscriptRow(
                    role="assistant",
                    content="",
                    api_content=texts.REDIRECT_PLACEHOLDER,
                    display_kind=DISPLAY_HIDDEN,
                    finish_reason="interrupted",
                )
            )
    rows.append(TranscriptRow(role="user", content=correction, api_content="\n\n".join(parts)))
    return rows
