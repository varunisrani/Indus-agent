"""What a message does to a conversation that is already replying (P15-12, R06.1).

Two guards, one policy:

- **Guard 1** (adapter level, ``AdmissionPipeline.accept``): a busy key does not start a handler
  task. The message is authorized and counted by the bot-loop guard once, offered to pending
  prompts, then stored in the key's pending queue (or merged) while the running turn is signalled
  according to the busy mode.
- **Guard 2** (runner level, inside ``AdmissionPipeline.handle``): after authorization, the pause
  gate and prompt replies, a recognised command runs its mid-run variant (stop, new, queue, steer,
  goal, loop), else its normal handler when the registry's busy policy allows it and it is in the
  any-time table, else one fixed "not while I'm replying" answer (P15-13 owns the tables).
- ``/stop``, ``/new``, ``/queue``, ``/status``, ``/approve`` and ``/deny`` (``CONTROL_COMMANDS``)
  bypass both: ``accept`` awaits them inline, never in a background task.

Busy modes (``display.busy_input_mode``):

- ``interrupt`` (default): text redirects the running model call (``SessionService.redirect``:
  what was shown is kept as a checkpoint, finished tool results stay, a foreground command moves
  to the background); a redirect the turn refuses queues the text as the next turn instead (an
  interrupt could cut off working subagents). Media interrupts the turn and runs next.
- ``queue``: every message becomes its own next turn, in arrival order; only a photo burst merges
  into the photo before it.
- ``steer``: text joins the running turn after its next tool call (``SessionService.steer``);
  before the turn has started, and for media, it falls back to the queue.

Text in interrupt and steer modes merges for a quiet window (0.35 s, at most 1.0 s from the first
piece). One short acknowledgement is sent per merge (per message in queue mode) unless
``display.busy_ack`` is off; the first one of the install carries a tip about the setting.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import TYPE_CHECKING, Final

from indus.gateway.core.batching import Batcher, merge_events
from indus.gateway.core.events import InboundEvent, MessageKind
from indus.protocol.methods import ProtocolError, RedirectOutcome
from indus.service.commands.registry import REGISTRY

if TYPE_CHECKING:
    from indus.gateway.core.admission import AdmissionPipeline

__all__ = [
    "BUSY_MODES",
    "CONTROL_COMMANDS",
    "MID_RUN_COMMANDS",
    "ONE_TIME_TIP",
    "QUEUE_CAP",
    "BusyGuard",
    "PendingQueue",
    "ack_text",
    "busy_mode",
    "canonical_command",
    "is_control",
]

log = logging.getLogger(__name__)

CONTROL_COMMANDS: Final = frozenset({"stop", "new", "queue", "status", "approve", "deny"})
MID_RUN_COMMANDS: Final = ("stop", "new", "queue", "steer", "goal", "loop")
BUSY_MODES: Final = ("interrupt", "queue", "steer")
QUEUE_CAP: Final = 20

ONE_TIME_TIP: Final = (
    "Tip: what you send while I'm replying {effect}. To change that, set "
    "display.busy_input_mode to interrupt, queue or steer."
)
_EFFECT = {
    "interrupt": "redirects the reply in progress",
    "queue": "waits its turn and gets its own reply",
    "steer": "is passed to the reply in progress after its current step",
}
_ACKS = {
    "interrupt": "Got it, changing course with your new message.",
    "redirected": "Got it, changing course with your new message.",
    "interrupted": "Stopping the current reply to answer this.",
    "queue": "Queued: I'll answer this after the current reply.",
    "steer": "Noted: I'll take this into account after the current step.",
}
QUEUE_FULL = "Too many messages are waiting already; this one was dropped. Please resend later."


def busy_mode(raw: object) -> str:
    text = str(raw).strip().lower() if raw is not None else ""
    return text if text in BUSY_MODES else "interrupt"


def ack_text(what: str) -> str:
    return _ACKS.get(what, _ACKS["queue"])


def canonical_command(event: InboundEvent) -> str | None:
    """The registry name of the event's command (aliases resolved), or None."""
    name = event.command
    if not name:
        return None
    cmd = REGISTRY.resolve(name)
    return cmd.name if cmd is not None else None


def is_control(event: InboundEvent) -> bool:
    """A control command that bypasses both busy guards and runs inline."""
    return not event.internal and canonical_command(event) in CONTROL_COMMANDS


def _photo_burst(first: InboundEvent, later: InboundEvent) -> bool:
    kinds = {first.kind, later.kind}
    return (
        MessageKind.PHOTO in kinds
        and kinds <= {MessageKind.PHOTO, MessageKind.TEXT}
        and first.internal == later.internal
        and first.allow_control == later.allow_control
    )


def text_only(event: InboundEvent) -> bool:
    return event.kind is MessageKind.TEXT and not event.media and bool(event.text.strip())


class PendingQueue:
    """Per-key FIFO of messages waiting for the key's next turn."""

    def __init__(self, cap: int = QUEUE_CAP) -> None:
        self._cap = cap
        self._queues: dict[str, deque[InboundEvent]] = {}

    def depth(self, key: str) -> int:
        queue = self._queues.get(key)
        return len(queue) if queue else 0

    def items(self, key: str) -> list[InboundEvent]:
        return list(self._queues.get(key, ()))

    def push(self, key: str, event: InboundEvent) -> bool:
        """Append (a photo burst merges into the last photo); False when the queue is full."""
        queue = self._queues.setdefault(key, deque())
        if queue and _photo_burst(queue[-1], event):
            queue[-1] = merge_events(queue[-1], event)
            return True
        if len(queue) >= self._cap:
            log.warning("pending queue of %s is full; a message was dropped", key)
            return False
        queue.append(event)
        return True

    def push_front(self, key: str, event: InboundEvent) -> None:
        self._queues.setdefault(key, deque()).appendleft(event)

    def pop(self, key: str) -> InboundEvent | None:
        queue = self._queues.get(key)
        if not queue:
            return None
        event = queue.popleft()
        if not queue:
            del self._queues[key]
        return event

    def clear(self, key: str) -> int:
        queue = self._queues.pop(key, None)
        return len(queue) if queue else 0


class BusyGuard:
    """The two busy guards of one intake (see the module notes)."""

    def __init__(self, pipeline: AdmissionPipeline) -> None:
        self.pipeline = pipeline
        self._merger: Batcher[InboundEvent] = Batcher(
            pipeline.clock,
            flush=self._apply_merged,
            merge=merge_events,
            delay_for=lambda _last: pipeline.settings().busy_quiet,
            cap=lambda: pipeline.settings().busy_cap,
        )

    @staticmethod
    def is_control(event: InboundEvent) -> bool:
        return is_control(event)

    async def aclose(self) -> None:
        await self._merger.aclose()

    def discard(self, key: str) -> None:
        """Forget busy text still being merged for ``key`` (``/stop``, ``/new``)."""
        self._merger.discard(key)

    # -- guard 1 -----------------------------------------------------------------------------------

    async def adapter_level(self, event: InboundEvent, key: str) -> None:
        """A message for a busy key, straight from the adapter."""
        pipeline = self.pipeline
        if not event.internal:
            if not await pipeline.authorize(event):
                return
            if await pipeline.intercept(event, key):
                return
        await self.while_busy(event, key)

    # -- guard 2 -----------------------------------------------------------------------------------

    async def runner_level(self, event: InboundEvent, key: str) -> None:
        """A message for a busy key inside the handler (already authorized)."""
        await self.while_busy(event, key)

    # -- the policy --------------------------------------------------------------------------------

    async def while_busy(self, event: InboundEvent, key: str) -> None:
        pipeline = self.pipeline
        if event.is_command:
            reply = await pipeline.commands.dispatch_busy(event, key)
            if reply:
                await pipeline.reply(event.source, reply, reply_to=event.message_id)
            return
        if event.internal:
            pipeline.pending.push(key, event)
            pipeline.drain(key)
            return
        settings = pipeline.settings()
        mode = busy_mode(settings.busy_mode)
        if mode == "queue" or (mode == "steer" and not text_only(event)):
            await self._queue(event, key)
            return
        if text_only(event):
            self._merger.add(key, event)
            return
        # interrupt mode, media: stop the running turn; this message goes first.
        self._interrupt(key)
        pipeline.pending.push_front(key, event)
        await self._ack(event, "interrupted")
        pipeline.drain(key)

    async def _queue(self, event: InboundEvent, key: str) -> None:
        pipeline = self.pipeline
        if not pipeline.pending.push(key, event):
            await pipeline.reply(event.source, QUEUE_FULL, reply_to=event.message_id, kind="ack")
            return
        await self._ack(event, "queue")
        pipeline.drain(key)  # the turn may have ended meanwhile

    def _interrupt(self, key: str) -> bool:
        pipeline = self.pipeline
        sid = pipeline.cached_session(key)
        if not sid or not pipeline.is_running(sid):
            return False
        try:
            return pipeline.service.interrupt(sid).interrupted
        except ProtocolError:
            return False

    async def _apply_merged(self, key: str, event: InboundEvent) -> None:
        """A merged burst of busy text: redirect / steer, else queue (one acknowledgement)."""
        pipeline = self.pipeline
        mode = busy_mode(pipeline.settings().busy_mode)
        sid = pipeline.cached_session(key)
        running = pipeline.is_running(sid)
        text = event.text.strip()
        if mode == "steer":
            if running and sid and self._steer(sid, text):
                await self._ack(event, "steer")
                return
            await self._queue(event, key)
            return
        if running and sid:
            outcome = self._redirect(sid, text)
            if outcome in (RedirectOutcome.REDIRECTED, RedirectOutcome.STEERED):
                await self._ack(event, "redirected")
                return
            # The turn refused the redirect (between phases, compressing, children working):
            # never force an interrupt for text; it runs as the next turn instead.
            await self._queue(event, key)
            return
        if pipeline.busy(key):
            # Claimed but not started yet: the message runs as the next turn.
            await self._queue(event, key)
            return
        pipeline.pending.push(key, event)
        pipeline.drain(key)

    def _steer(self, sid: str, text: str) -> bool:
        try:
            return self.pipeline.service.steer(sid, text).queued
        except ProtocolError:
            return False

    def _redirect(self, sid: str, text: str) -> RedirectOutcome:
        try:
            return self.pipeline.service.redirect(sid, text).outcome
        except ProtocolError:
            return RedirectOutcome.REJECTED

    async def _ack(self, event: InboundEvent, what: str) -> None:
        pipeline = self.pipeline
        settings = pipeline.settings()
        if not settings.busy_ack:
            return
        text = ack_text(what)
        if pipeline.take_tip(settings):
            mode = busy_mode(settings.busy_mode)
            text = f"{text}\n\n{ONE_TIME_TIP.format(effect=_EFFECT[mode])}"
        await pipeline.reply(event.source, text, reply_to=event.message_id, kind="ack")
