"""Native streaming replies for the WeCom smart robot (P16-17, R06.12).

A WeCom reply stream is a series of ``aibot_respond_msg`` frames answering one inbound message
(same ``req_id``), all with one ``stream.id`` and the whole text so far in ``stream.content``; the
frame with ``finish: true`` ends it. The stream *is* the message: there is no separate final send.

`WeComStream` keeps the frames prefix-stable: an interim text that does not extend what was last
sent is skipped (the next extending one, or the final, carries it), so every frame on the wire is a
prefix of the next. Further rules:

- interim frames do not wait for WeCom's answer; while one is unanswered the next interim is
  skipped (its text rides the next frame); at most 85 interim frames per stream (WeCom keeps 100
  per ``req_id`` and the final needs room);
- a frame carries at most 20,480 UTF-8 bytes; a longer text stops the interim frames, the final
  frame carries the longest prefix that fits (cut on a character boundary) and the rest goes out as
  ordinary messages;
- the final waits (bounded) for the unanswered interim, then for its own answer; an answer that
  never comes is treated as delivered ("maybe delivered", never re-sent); a final identical to the
  last interim gets a zero-width space so WeCom does not drop it;
- a stream older than 330 s refuses its final (WeCom closes reply streams after a few minutes; the
  caller sends the text normally), as do the "stream expired" codes 846604 / 846608.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from indus.foundation.clock import Clock
from indus.gateway.core.send import SendResult

__all__ = [
    "EXPIRED_CODES",
    "MAX_FRAME_BYTES",
    "MAX_INTERIM_FRAMES",
    "SAFE_AGE_S",
    "VERSION_CONFLICT",
    "FrameSender",
    "WeComStream",
    "cut_to_bytes",
]

log = logging.getLogger(__name__)

MAX_FRAME_BYTES = 20_480
MAX_INTERIM_FRAMES = 85
SAFE_AGE_S = 330.0
ACK_WAIT_S = 15.0
EXPIRED_CODES = frozenset({846604, 846608})
# "A newer frame already replaced the bubble": the content is on screen.
VERSION_CONFLICT = 6000
ZERO_WIDTH_SPACE = "​"


def cut_to_bytes(text: str, limit: int) -> str:
    """The longest prefix of ``text`` whose UTF-8 form fits ``limit`` bytes."""
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[:limit].decode("utf-8", errors="ignore")


class FrameSender(Protocol):
    """What a stream needs from the adapter."""

    clock: Clock

    def send_frame(self, req_id: str, body: dict[str, Any]) -> Awaitable[dict[str, Any]]:
        """Send one ``aibot_respond_msg`` frame; the awaitable is WeCom's answer (raises
        ``TimeoutError`` when none comes in time, ``ConnectionError`` when it never left)."""
        ...

    async def wait_bounded(self, awaitable: Awaitable[Any], seconds: float) -> Any: ...


class WeComStream:
    """One native reply stream (see the module notes)."""

    def __init__(
        self,
        sender: FrameSender,
        chat_id: str,
        req_id: str,
        *,
        on_rest: Callable[[str], Awaitable[SendResult]] | None = None,
        stream_id: str | None = None,
    ) -> None:
        self.sender = sender
        self.chat_id = chat_id
        self.req_id = req_id
        self.stream_id = stream_id or f"stream-{uuid.uuid4().hex[:16]}"
        self.started = sender.clock.monotonic()
        self.sent = ""
        self.frames: list[str] = []
        self.interim = 0
        self.finished = False
        self.expired = False
        self._on_rest = on_rest
        self._pending: asyncio.Future[dict[str, Any]] | None = None

    def __repr__(self) -> str:
        return f"<WeComStream {self.stream_id} frames={len(self.frames)}>"

    @property
    def age(self) -> float:
        return self.sender.clock.monotonic() - self.started

    def _body(self, content: str, finish: bool) -> dict[str, Any]:
        return {
            "msgtype": "stream",
            "stream": {"id": self.stream_id, "finish": finish, "content": content},
        }

    def _unanswered(self) -> bool:
        pending = self._pending
        return pending is not None and not pending.done()

    async def push(self, text: str) -> bool:
        """One interim frame with the whole text so far. False when the stream is closed or the
        text does not extend what was sent (it was skipped to keep frames prefix-stable)."""
        if self.finished or self.expired:
            return False
        if text == self.sent:
            return True
        if not text.startswith(self.sent):
            log.debug("wecom: stream %s skipped a frame that rewrites sent text", self.stream_id)
            return False
        if (
            self.interim >= MAX_INTERIM_FRAMES
            or len(text.encode("utf-8")) > MAX_FRAME_BYTES
            or self._unanswered()
        ):
            return True  # the final carries it
        try:
            pending = asyncio.ensure_future(
                self.sender.send_frame(self.req_id, self._body(text, False))
            )
        except ConnectionError:
            return True
        pending.add_done_callback(_quiet)
        self._pending = pending
        self.interim += 1
        self.sent = text
        self.frames.append(text)
        return True

    async def finish(self, text: str) -> SendResult:
        """The final frame (``finish: true``). A refused result (``unsupported``) means the caller
        should send ``text`` as an ordinary message instead."""
        if self.finished:
            return SendResult.refused("a second final on a finished stream")
        if self.expired or self.age >= SAFE_AGE_S:
            self.expired = True
            return SendResult.refused("finishing an expired WeCom reply stream")
        body_text = cut_to_bytes(text, MAX_FRAME_BYTES)
        rest = text[len(body_text) :]
        if body_text == self.sent:
            body_text += ZERO_WIDTH_SPACE
        if self._pending is not None and not self._pending.done():
            try:
                await self.sender.wait_bounded(self._pending, ACK_WAIT_S)
            except Exception:
                log.debug("wecom: stream %s: the interim answer never came", self.stream_id)
        try:
            answer = await self.sender.send_frame(self.req_id, self._body(body_text, True))
        except TimeoutError:
            # The frame left; WeCom shows it. Never send it a second way.
            self.finished = True
            self.frames.append(body_text)
            return SendResult.sent(self.stream_id)
        except ConnectionError as exc:
            return SendResult.failure(f"connect error: {exc}; the final frame never left")
        code = _code(answer)
        if code in EXPIRED_CODES:
            self.expired = True
            return SendResult.refused("finishing an expired WeCom reply stream")
        if code not in (0, VERSION_CONFLICT):
            return SendResult.failure(f"WeCom errcode {code}: {answer.get('errmsg') or ''}")
        self.finished = True
        self.frames.append(body_text)
        if rest and self._on_rest is not None:
            more = await self._on_rest(rest)
            if more.ok:
                return SendResult.sent(more.message_id or self.stream_id, (self.stream_id,))
            return SendResult.failure(
                more.error,
                retry_after=more.retry_after,
                delivered_ids=(self.stream_id,),
                remainder=rest,
            )
        return SendResult.sent(self.stream_id)


def _code(answer: dict[str, Any]) -> int:
    try:
        return int(answer.get("errcode") or 0)
    except (TypeError, ValueError):
        return -1


def _quiet(task: asyncio.Future[Any]) -> None:
    if not task.cancelled():
        task.exception()
