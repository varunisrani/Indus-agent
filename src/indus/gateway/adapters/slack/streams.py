"""Slack native streams (P16-33, R06.12).

Slack can stream a reply into a thread: ``chat.startStream`` opens a message (with the first text),
``chat.appendStream`` adds to it and ``chat.stopStream`` seals it (adding a last piece). Both take
standard Markdown, not mrkdwn. The stream *is* the reply: once sealed nothing is posted again.
`SlackStream` is the `NativeStream` (``adapters._native``) over it:

- only text that extends what was streamed goes out (the new part as the append); a frame that
  would rewrite streamed text is skipped, the final carries it;
- the stream stays open across tool calls (``one_message_per_turn``);
- ``ratelimited`` holds frames back (``FloodGate``: ``Retry-After`` or a doubling hold, capped at
  30 s; three in a row stop frames), never sleeping;
- an app without Slack's AI streaming (``not_allowed``, ``missing_scope``, ``feature_not_enabled``,
  ``unknown_method`` …) switches native streams off for the adapter (logged once) and the stream
  ends (``push`` → False: the caller edits instead);
- a message Slack sealed on its own (``message_not_in_streaming_state``) is followed by a fresh
  stream in the same thread holding only the text after it;
- any other failure after the message exists stops the frames; the final then seals and edits;
- past the 39,000-character cap no more frames go out; the final seals what fits (cut at a line
  break or space) and the rest follows as ordinary thread messages (mrkdwn).

``finish(text)``: when the final continues what was streamed (text added afterwards rides along),
the missing part goes into ``chat.stopStream``; when it differs, the stream is sealed and its
message edited to the final (``chat.update``, mrkdwn). A failed seal is retried once only when
nothing was being added (an addition that landed would be repeated), then the message is edited to
the whole final instead. A second final with the same text answers the first result; with other
text it edits the message. Nothing is ever posted twice.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Final, Protocol

from indus.foundation.clock import Clock
from indus.gateway.adapters._native import FloodGate, extends
from indus.gateway.core.send import SendResult

__all__ = [
    "SEALED_BY_SLACK",
    "STREAM_UNAVAILABLE",
    "SlackStream",
    "StreamHost",
]

log = logging.getLogger(__name__)

MAX_LEN: Final = 39_000
SEALED_BY_SLACK: Final = "message_not_in_streaming_state"
RATE_LIMITED: Final = "ratelimited"
# Answers that mean this app cannot stream at all.
STREAM_UNAVAILABLE: Final = frozenset(
    {
        "not_allowed",
        "not_allowed_token_type",
        "missing_scope",
        "feature_not_enabled",
        "invalid_method",
        "unknown_method",
        "method_deprecated",
        "streaming_not_allowed",
    }
)


class StreamHost(Protocol):
    """What a stream needs from the Slack adapter."""

    clock: Clock

    def format(self, text: str) -> str: ...

    def stream_client(self) -> Any: ...

    async def stream_channel(self, chat_id: str) -> str: ...

    def stream_error(self, exc: BaseException) -> str: ...

    def stream_retry_after(self, exc: BaseException) -> float | None: ...

    def native_unavailable(self, code: str) -> None: ...

    def stream_opened(self, channel: str, thread_ts: str) -> None: ...

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


def _cut(text: str, room: int) -> int:
    """How much of ``text`` fits in ``room`` characters, cut at a line break or space."""
    if len(text) <= room:
        return len(text)
    if room <= 0:
        return 0
    newline = text.rfind("\n", 0, room)
    if newline >= room // 2:
        return newline
    space = text.rfind(" ", 0, room)
    return space if space > 0 else room


class SlackStream:
    """One reply streamed into a Slack thread (see the module notes)."""

    one_message_per_turn = True

    def __init__(
        self,
        host: StreamHost,
        chat_id: str,
        *,
        thread_ts: str,
        recipient_user: str | None = None,
        recipient_team: str | None = None,
    ) -> None:
        self.host = host
        self.chat_id = chat_id
        self.thread_ts = thread_ts
        self.recipient_user = recipient_user
        self.recipient_team = recipient_team
        self.gate = FloodGate(host.clock)
        self.frames: list[str] = []
        self.sent = ""
        # Where the open message starts in ``sent`` (after a message Slack sealed on its own).
        self.base = 0
        self.channel: str | None = None
        self.ts: str | None = None
        self.message_ids: list[str] = []
        self.dead = False
        self.stalled = False
        self.full = False
        self.result: SendResult | None = None
        self.final_text: str | None = None

    def __repr__(self) -> str:
        return f"<SlackStream {self.ts or 'unopened'} frames={len(self.frames)}>"

    @property
    def meta(self) -> dict[str, Any]:
        return {"thread_id": self.thread_ts}

    # -- wire --------------------------------------------------------------------------------------

    async def _channel(self) -> str:
        if self.channel is None:
            self.channel = await self.host.stream_channel(self.chat_id)
        return self.channel

    async def _start(self, text: str) -> None:
        """Open a stream message showing ``text[self.base:]``."""
        args: dict[str, Any] = {"channel": await self._channel(), "thread_ts": self.thread_ts}
        shown = text[self.base :]
        if shown.strip():
            args["markdown_text"] = shown
        if self.recipient_user:
            args["recipient_user_id"] = self.recipient_user
        if self.recipient_team:
            args["recipient_team_id"] = self.recipient_team
        response = await self.host.stream_client().chat_startStream(**args)
        ts = str(_get(response, "ts") or "")
        if not ts:
            raise RuntimeError("chat.startStream answered without a message ts")
        self.ts = ts
        self.message_ids.append(ts)
        self.host.stream_opened(args["channel"], self.thread_ts)

    async def _append(self, text: str) -> None:
        assert self.ts is not None
        delta = text[len(self.sent) :]
        try:
            await self.host.stream_client().chat_appendStream(
                channel=await self._channel(), ts=self.ts, markdown_text=delta
            )
        except Exception as exc:
            if self.host.stream_error(exc) != SEALED_BY_SLACK:
                raise
            log.info("slack: stream %s was sealed by Slack; continuing in a new one", self.ts)
            sealed, base = self.ts, self.base
            self.base, self.ts = len(self.sent), None
            try:
                await self._start(text)
            except Exception:
                self.base, self.ts = base, sealed  # the sealed message is still the reply's
                raise

    async def _stop(self, addition: str) -> bool:
        assert self.ts is not None
        args: dict[str, Any] = {"channel": await self._channel(), "ts": self.ts}
        if addition.strip():
            args["markdown_text"] = addition
        try:
            await self.host.stream_client().chat_stopStream(**args)
        except Exception as exc:
            log.info("slack: sealing stream %s failed: %s", self.ts, self.host.stream_error(exc))
            return False
        return True

    # -- interim frames ----------------------------------------------------------------------------

    async def push(self, text: str) -> bool:
        if self.dead or self.result is not None:
            return False
        if self.stalled or self.full or not extends(self.sent, text):
            return True
        if len(text) - self.base > MAX_LEN:
            self.full = True
            return True
        if not self.gate.open():
            return True
        try:
            if self.ts is None:
                await self._start(text)
            else:
                await self._append(text)
        except Exception as exc:
            return self._failed(exc)
        self.gate.passed()
        self.sent = text
        self.frames.append(text)
        return True

    def _failed(self, exc: BaseException) -> bool:
        code = self.host.stream_error(exc)
        if code == RATE_LIMITED:
            self.gate.flood(self.host.stream_retry_after(exc))
            return True
        if code in STREAM_UNAVAILABLE:
            self.host.native_unavailable(code)
        reason = code or type(exc).__name__
        if self.ts is None:
            self.dead = True
            log.info("slack: native stream not opened (%s)", reason)
            return False
        # The message exists but its state is unknown: no more frames; the final edits it.
        self.stalled = True
        log.info("slack: stream frame failed (%s); the final will settle the message", reason)
        return True

    # -- the final ---------------------------------------------------------------------------------

    async def finish(self, text: str) -> SendResult:
        if self.result is not None:
            return await self._again(text)
        if self.ts is None:
            # Nothing on screen: the caller sends the final as an ordinary reply.
            return SendResult.refused("no native stream message to finish")
        if not self.stalled and text.startswith(self.sent):
            result = await self._seal_with(text, text[len(self.sent) :])
        elif not self.stalled and self.sent.strip() and text.startswith(self.sent.rstrip()):
            result = await self._seal_with(text, text[len(self.sent.rstrip()) :])
        else:
            result = await self._replace(text)
        if result.ok:
            self.result, self.final_text = result, text
        return result

    async def _seal_with(self, text: str, addition: str) -> SendResult:
        """Seal, adding what the final has beyond the stream (what fits; the rest follows)."""
        room = MAX_LEN - (len(self.sent) - self.base)
        keep = _cut(addition, room)
        head, rest = addition[:keep], addition[keep:]
        sealed = await self._stop(head)
        if not sealed and not head.strip():
            sealed = await self._stop("")
        if not sealed:
            shown = text[self.base : len(text) - len(rest)] if rest else text[self.base :]
            edited = await self._edit(shown)
            if not edited.ok:
                return edited
        return await self._spill(rest)

    async def _replace(self, text: str) -> SendResult:
        """The final is not a continuation: seal, then edit the message to it."""
        await self._stop("")
        shown = text[self.base :] if text.startswith(self.sent[: self.base]) else text
        keep = _cut(shown, MAX_LEN)
        edited = await self._edit(shown[:keep])
        if not edited.ok:
            return edited
        return await self._spill(shown[keep:])

    async def _edit(self, markdown: str) -> SendResult:
        assert self.ts is not None
        return await self.host.edit(
            self.chat_id, self.ts, self.host.format(markdown), final=True, meta=self.meta
        )

    async def _spill(self, rest: str) -> SendResult:
        assert self.ts is not None
        if not rest.strip():
            return SendResult.sent(self.ts, tuple(self.message_ids[:-1]))
        more = await self.host.send(self.chat_id, self.host.format(rest.strip()), meta=self.meta)
        delivered = (*self.message_ids,)
        if more.ok and more.message_id:
            return SendResult.sent(more.message_id, (*delivered, *more.continuation_ids))
        return SendResult.failure(
            more.error,
            retry_after=more.retry_after,
            delivered_ids=delivered,
            remainder=rest.strip(),
        )

    async def _again(self, text: str) -> SendResult:
        """A final for a sealed stream: the same text is the first result, other text an edit."""
        first = self.result
        assert first is not None
        if text == self.final_text or self.ts is None:
            return first
        edited = await self._edit(text[: _cut(text, MAX_LEN)])
        if edited.ok:
            self.final_text = text
            return first
        return edited

    async def abandon(self) -> None:
        """The turn was cancelled: seal an open stream (best effort)."""
        if self.ts is not None and self.result is None and not self.dead:
            self.dead = True
            await self._stop("")


def _get(response: object, key: str) -> object:
    getter = getattr(response, "get", None)
    if callable(getter):
        try:
            return getter(key)
        except Exception:
            return None
    return None
