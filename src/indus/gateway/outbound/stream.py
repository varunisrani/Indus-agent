"""Streaming a reply into one chat message that grows by edits (P15-16, R06.12, R06.13).

``StreamConsumer`` takes model deltas (``delta`` / ``feed`` with ``message.delta`` events) and keeps
one live message per segment up to date:

- an edit goes out when ``edit_interval`` (0.8 s) has passed with new text, or ``buffer_threshold``
  (24) new code points are waiting; a cursor marks a message still being written; the first
  message waits for a few visible characters;
- interim frames show text only up to the last whitespace (a half-streamed token, possibly a
  secret, is never shown before the redactor can see it whole), with media directives removed and
  open fences closed; text that could still become a silence token is held back;
- text that outgrows the platform limit (measured after formatting) seals the live message with
  the head and continues in a new one, carrying an open fence across;
- a tool call or an approval prompt seals the live message so later text lands below it;
- without edit support nothing is shown until a segment is sealed, then it goes out once;
- ``finish(final_text)`` reconciles the authoritative final with what is on screen: the live
  message is edited to it (post-stream additions ride along); a new message is sent only when
  nothing is editable, and after edits stopped only the missing tail is sent;
- flood refusals stretch the edit interval (doubling, or the server's wait, capped at 30 s); three
  in a row stop interim edits for this stream.

Every send and edit except the final is marked interim (``ports``). Text is masked by ``redact``
before it is formatted.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import threading
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from indus.foundation.clock import Clock
from indus.gateway.adapters.base import Adapter
from indus.gateway.core.send import SendResult
from indus.gateway.outbound import ports
from indus.gateway.outbound.chunker import close_fences, fence_state
from indus.gateway.outbound.filters import could_become_silence, is_silence
from indus.gateway.outbound.media_policy import strip_media_tags

__all__ = [
    "STREAMING_DEFAULTS",
    "StreamConsumer",
    "StreamOutcome",
    "StreamSettings",
    "streaming_default",
]

STREAMING_DEFAULTS = {"telegram": True, "discord": False, "slack": False}
_MIN_FIRST_CHARS = 4
_EPSILON = 1e-6  # clock arithmetic: wake a hair early rather than miss a deadline
_CLOSER = "\n```"


def streaming_default(platform: str) -> bool:
    """Whether a platform streams by default (Telegram on, Discord and Slack off)."""
    return STREAMING_DEFAULTS.get(platform, False)


@dataclass(frozen=True)
class StreamSettings:
    edit_interval: float = 0.8
    buffer_threshold: int = 24
    cursor: str = " ▉"
    max_backoff: float = 30.0
    flood_limit: int = 3


@dataclass
class StreamOutcome:
    """What the stream put on screen, for the runner and the ledger."""

    message_ids: list[str] = field(default_factory=list[str])
    final_delivered: bool = False
    silenced: bool = False
    final_message_id: str | None = None


type _Item = tuple[str, Any]
type Redact = Callable[[str], str]


def _identity(text: str) -> str:
    return text


class StreamConsumer:
    def __init__(
        self,
        port: Adapter,
        chat_id: str,
        *,
        clock: Clock,
        settings: StreamSettings | None = None,
        redact: Redact | None = None,
        reply_to: str | None = None,
        meta: Mapping[str, Any] | None = None,
        turn_id: str | None = None,
        on_new_message: Callable[[str], object] | None = None,
    ) -> None:
        self._port = port
        self._chat_id = chat_id
        self._clock = clock
        self._cfg = settings or StreamSettings()
        self._redact = redact or _identity
        self._reply_to = reply_to
        self._meta = dict(meta or {})
        self._turn_id = turn_id
        self._on_new_message = on_new_message
        self._len = ports.length_fn(port)
        self._limit = ports.max_length(port)
        self._fmt = ports.formatter(port)
        self._inbox: deque[_Item] = deque()
        self._wake = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: int | None = None
        self.outcome = StreamOutcome()
        # edit state for the whole stream
        self._edits = ports.can_edit(port)
        self._interval = self._cfg.edit_interval
        self._strikes = 0
        self._last_edit = -math.inf
        # the current segment
        self._src = ""
        self._sealed = 0
        self._carry: str | None = None
        self._msg_id: str | None = None
        self._shown = ""
        self._shown_upto = 0
        self._src_at_edit = 0
        self._timer = False

    # -- input (any thread) ----------------------------------------------------------------------

    def _put_item(self, item: _Item) -> None:
        self._inbox.append(item)
        loop = self._loop
        if loop is None or threading.get_ident() == self._loop_thread:
            self._wake.set()
        else:
            loop.call_soon_threadsafe(self._wake.set)

    def delta(self, text: str) -> None:
        if text:
            self._put_item(("delta", text))

    def tool_boundary(self) -> None:
        """A tool call started: the current text is a preamble; later text goes below."""
        self._put_item(("boundary", None))

    def approval_boundary(self) -> asyncio.Future[bool]:
        """An approval prompt is about to be shown; resolves once the live message is sealed."""
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._put_item(("approval", future))
        return future

    def finish(self, final_text: str | None = None) -> None:
        """The turn ended; ``final_text`` is the authoritative reply (None: what streamed)."""
        self._put_item(("finish", final_text))

    def feed(self, event: object) -> None:
        """Route a session event: ``message.delta``, ``tool.started``, ``turn.done``."""
        if self._turn_id is not None and getattr(event, "turn_id", self._turn_id) != self._turn_id:
            return
        kind = getattr(event, "type", None)
        if kind == "message.delta":
            self.delta(str(getattr(event, "text", "")))
        elif kind == "tool.started":
            self.tool_boundary()
        elif kind == "turn.done":
            self.finish(None)

    # -- the task --------------------------------------------------------------------------------

    async def run(self) -> StreamOutcome:
        self._loop = asyncio.get_running_loop()
        self._loop_thread = threading.get_ident()
        try:
            while True:
                while self._inbox:
                    kind, value = self._inbox.popleft()
                    if kind == "delta":
                        self._src += value
                    elif kind == "boundary":
                        await self._end_segment()
                    elif kind == "approval":
                        try:
                            await self._end_segment()
                        finally:
                            if not value.done():
                                value.set_result(True)
                    else:
                        await self._finish(value)
                        return self.outcome
                await self._interim_update()
                await self._wait()
        except asyncio.CancelledError:
            await self._on_cancel()
            raise

    async def _wait(self) -> None:
        if self._inbox:
            return
        self._wake.clear()
        if self._inbox:
            return
        if not self._timer:
            await self._wake.wait()
            return
        delay = max(0.0, self._last_edit + self._interval - self._clock.monotonic() - _EPSILON)
        waiter = asyncio.ensure_future(self._wake.wait())
        sleeper = asyncio.ensure_future(self._clock.sleep(delay))
        try:
            await asyncio.wait({waiter, sleeper}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (waiter, sleeper):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    # -- text views ------------------------------------------------------------------------------

    def _live(self) -> str:
        prefix = "" if self._carry is None else f"```{self._carry}\n"
        return prefix + self._src[self._sealed :]

    def _render(self, raw: str, *, interim: bool) -> str:
        """Raw segment text → what is sent: media directives out, secrets masked, formatted."""
        text = close_fences(strip_media_tags(raw).rstrip())
        text = self._fmt.format(self._redact(text))
        return text + self._cfg.cursor if interim and text else text

    def _fits(self, raw: str) -> bool:
        return self._len(self._render(raw, interim=True)) <= self._limit

    # -- edits and sends -------------------------------------------------------------------------

    async def _show(self, upto: int, text: str, *, final: bool) -> bool:
        """Edit the live message to ``text`` (rendered), or send it as a new one. ``upto`` is the
        source offset the text shows, so a later fallback knows what the user has seen."""
        if not text.strip():
            return True
        if self._msg_id is not None:
            if text == self._shown:
                self._shown_upto = upto
                return True
            result = await ports.edit_text(
                self._port, self._chat_id, self._msg_id, text, final=final, meta=self._meta
            )
            if not result.ok and ports.is_bad_format(result):
                plain = self._fmt.plain(text)
                result = await ports.edit_text(
                    self._port, self._chat_id, self._msg_id, plain, final=final, meta=self._meta
                )
            self._last_edit = self._clock.monotonic()
            if result.ok:
                self._strikes = 0
                if not result.skipped:  # the per-chat slot was busy
                    self._shown, self._shown_upto = text, upto
                if result.continuation_ids and result.message_id:
                    self._record_ids(result)
                    self._msg_id, self._shown = result.message_id, ""
                return True
            if ports.is_flood(result):
                self._strikes += 1
                wait = result.retry_after or 0.0
                self._interval = min(max(self._interval * 2, wait), self._cfg.max_backoff)
                if self._strikes >= self._cfg.flood_limit:
                    self._edits = False
            else:
                self._edits = False
            return False
        reply_to = self._reply_to if not self.outcome.message_ids else None
        result = await ports.send_text(
            self._port, self._chat_id, text, final=final, reply_to=reply_to, meta=self._meta
        )
        if not result.ok and ports.is_bad_format(result):
            plain = self._fmt.plain(text)
            result = await ports.send_text(
                self._port, self._chat_id, plain, final=final, reply_to=reply_to, meta=self._meta
            )
        self._last_edit = self._clock.monotonic()
        if not result.ok:
            if not final:
                self._edits = False  # a preview that cannot be updated would go stale
            return False
        self._record_ids(result)
        # no id: the message cannot be edited; "" marks a live message we can only add below
        self._msg_id = result.message_id or ""
        self._shown, self._shown_upto = text, upto
        if not result.message_id:
            self._edits = False
        elif self._on_new_message is not None:
            with contextlib.suppress(Exception):
                self._on_new_message(result.message_id)
        return True

    def _record_ids(self, result: SendResult) -> None:
        ids = [*result.continuation_ids, result.message_id]
        self.outcome.message_ids.extend(i for i in ids if i and i not in self.outcome.message_ids)

    def _advance(self, upto: int) -> None:
        """Seal the source up to ``upto``: the live message is done, later text goes below."""
        state = fence_state(self._src[self._sealed : upto], self._carry)
        self._sealed = upto
        if state is None:
            while self._sealed < len(self._src) and self._src[self._sealed].isspace():
                self._sealed += 1
        elif self._src[self._sealed : self._sealed + 1] == "\n":
            self._sealed += 1
        self._carry = state
        self._msg_id, self._shown, self._shown_upto = None, "", self._sealed

    async def _detach(self) -> None:
        """Edits stopped: keep what the live message shows, continue below it."""
        if self._msg_id is not None:
            await self._strip_cursor()
            self._advance(max(self._sealed, self._shown_upto))

    def _prefix_len(self) -> int:
        return 0 if self._carry is None else len(f"```{self._carry}\n")

    def _cut_point(self, live: str, prefix_len: int) -> int:
        """Where to seal: the largest cut whose rendering fits, snapped to a line break or space."""
        low, high = prefix_len + 1, len(live)
        while low < high:
            middle = (low + high + 1) // 2
            if self._fits(live[:middle]):
                low = middle
            else:
                high = middle - 1
        cut = low
        newline = live.rfind("\n", prefix_len, cut)
        if newline > prefix_len and newline >= cut // 2:
            return newline
        space = live.rfind(" ", prefix_len, cut)
        return space if space > prefix_len else cut

    async def _seal_overflow(self) -> bool:
        """Seal heads while the live text is too long for one message; False when one failed."""
        if self._msg_id is not None and not self._edits:
            await self._detach()
        while not self._fits(self._live()):
            live = self._live()
            prefix_len = self._prefix_len()
            split = self._cut_point(live, prefix_len)
            upto = self._sealed + split - prefix_len
            if not await self._show(upto, self._render(live[:split], interim=False), final=False):
                if self._msg_id is not None and not self._edits:
                    await self._detach()
                    continue
                return False
            self._advance(upto)
        return True

    async def _complete(self, *, final: bool) -> bool:
        """Show the whole live segment without a cursor; when the live message cannot take it,
        send only what the user has not seen yet."""
        if not await self._seal_overflow() and self._msg_id is not None:
            self._edits = False
            await self._detach()
            await self._seal_overflow()
        text = self._render(self._live(), interim=False)
        ok = await self._show(len(self._src), text, final=final)
        if not ok and self._msg_id is not None:
            self._edits = False
            await self._detach()
            ok = await self._show(
                len(self._src), self._render(self._live(), interim=False), final=final
            )
        return ok

    async def _strip_cursor(self) -> None:
        """One best-effort edit that takes the cursor off a message edits can no longer reach
        (the tail goes out below it)."""
        cursor = self._cfg.cursor
        if not cursor or not self._shown.endswith(cursor) or not self._msg_id:
            return
        with contextlib.suppress(Exception):
            text = self._shown.removesuffix(cursor)
            await ports.edit_text(
                self._port, self._chat_id, self._msg_id, text, final=False, meta=self._meta
            )

    async def _interim_update(self) -> None:
        self._timer = False
        if not self._edits or len(self._src) == self._src_at_edit:
            return
        if could_become_silence(self._src):
            return
        waited = self._clock.monotonic() - self._last_edit >= self._interval - _EPSILON
        enough = len(self._src) - self._src_at_edit >= self._cfg.buffer_threshold
        if not (waited or (enough and self._strikes == 0)):
            self._timer = True
            return
        if not await self._seal_overflow():
            self._timer = self._edits
            return
        live = self._live()
        prefix_len = self._prefix_len()
        cut = max(live.rfind(" "), live.rfind("\n"))
        if cut < prefix_len:
            return  # only a partial word so far
        shown_raw = live[: cut + 1]
        if self._msg_id is None and len(strip_media_tags(shown_raw).strip()) < _MIN_FIRST_CHARS:
            return
        upto = self._sealed + cut + 1 - prefix_len
        if await self._show(upto, self._render(shown_raw, interim=True), final=False):
            self._src_at_edit = len(self._src)
        else:
            self._timer = self._edits

    def _reset_segment(self) -> None:
        self._src, self._sealed, self._carry = "", 0, None
        self._msg_id, self._shown, self._shown_upto = None, "", 0
        self._src_at_edit = 0

    async def _end_segment(self) -> None:
        body = self._src[self._sealed :]
        if body.strip() and not is_silence(self._src):
            await self._complete(final=False)
        self._reset_segment()

    async def _finish(self, final_text: str | None) -> None:
        final = self._src if final_text is None else final_text
        if is_silence(final):
            self.outcome.silenced = True
            if self._msg_id:
                await ports.delete_message(self._port, self._chat_id, self._msg_id)
            return
        if final.startswith(self._src[: self._sealed]):
            self._src = final  # post-stream additions ride the final
        else:  # unrelated to what was sealed: deliver it whole below
            self._reset_segment()
            self._src = final
        if not strip_media_tags(self._src[self._sealed :]).strip():
            self.outcome.final_delivered = bool(self.outcome.message_ids)
            return
        ok = await self._complete(final=True)
        self.outcome.final_delivered = ok
        self.outcome.final_message_id = (self._msg_id or None) if ok else None

    async def _on_cancel(self) -> None:
        if not self._msg_id or not self._edits:
            return
        with contextlib.suppress(Exception):
            text = self._render(self._live(), interim=False)
            await self._show(len(self._src), text, final=False)
