"""A fake surface that answers server requests (P05-09).

``FakeBroker`` is the real ``ServerRequestBroker`` (so ids, withdrawal events and the
human-wait counter behave as in production) with a scripted client on the other end. Each
delivered request takes the next ``Reply`` (or ``default``):

- ``answer``: after ``delay`` seconds of fake time, answer with ``choice`` (+ ``reason``) for an
  approval, ``password`` for a sudo request;
- ``timeout``: never answer; ``run()`` moves the fake clock until the broker gives up;
- ``withdraw``: the request is withdrawn (``reason``, default ``interrupted``) before anyone
  answers.

Kinds outside ``kinds`` are refused at once by the broker (``RequestUnsupported``).
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Awaitable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from indus.protocol.events import CancelReason, ServerRequestCancelled
from indus.protocol.requests_port import AnswerModel
from indus.service.broker import RequestModel, ServerRequestBroker
from tests.fakes.clock import FakeClock, checkpoint

ALL_KINDS = ("approval", "clarify", "sudo", "secret")


@dataclass
class Reply:
    action: Literal["answer", "timeout", "withdraw"] = "answer"
    choice: str = "once"
    reason: str | None = None
    password: str | None = None
    delay: float = 0.0
    withdraw_reason: CancelReason = CancelReason.INTERRUPTED


@dataclass
class FakeBroker:
    clock: FakeClock
    replies: list[Reply] = field(default_factory=list[Reply])
    default: Reply = field(default_factory=Reply)
    kinds: Iterable[str] = ALL_KINDS
    session_id: str = "s1"

    def __post_init__(self) -> None:
        self.requests: list[RequestModel] = []
        self.cancelled: list[ServerRequestCancelled] = []
        seqs = itertools.count(1)
        ids = itertools.count(1)
        self.broker = ServerRequestBroker(
            session_id=self.session_id,
            deliver=self._deliver,
            emit=self.cancelled.append,
            next_seq=lambda: next(seqs),
            clock=self.clock,
            answerable_kinds=self.kinds,
            new_id=lambda: f"sr_{next(ids)}",
        )

    # -- the ServerRequestPort face ------------------------------------------------------------

    async def ask(
        self,
        kind: str,
        payload: Mapping[str, Any],
        *,
        timeout: float | None = None,
        turn_id: str | None = None,
    ) -> AnswerModel:
        return await self.broker.ask(kind, payload, timeout=timeout, turn_id=turn_id)

    def cancel_all(self, reason: CancelReason) -> int:
        return self.broker.cancel_all(reason)

    def human_wait_seconds(self) -> float:
        return self.broker.human_wait_seconds()

    # -- the scripted client -------------------------------------------------------------------

    def _deliver(self, request: RequestModel) -> None:
        self.requests.append(request)
        reply = self.replies.pop(0) if self.replies else self.default
        if reply.action == "timeout":
            return
        if reply.action == "withdraw":
            asyncio.get_running_loop().call_soon(self.broker.cancel_all, reply.withdraw_reason)
            return
        if reply.delay:
            self.clock.advance(reply.delay)
        if request.kind == "approval":
            answer: dict[str, Any] = {"choice": reply.choice}
            if reply.reason is not None:
                answer["reason"] = reply.reason
        elif request.kind == "sudo":
            answer = {"password": reply.password}
        else:
            raise AssertionError(f"the fake surface does not answer {request.kind} requests")
        self.broker.answer(request.request_id, answer)

    async def run[T](self, work: Awaitable[T], *, step: float = 30.0) -> T:
        """Await ``work``, moving the fake clock ``step`` seconds whenever something sleeps on
        it (so a request nobody answers times out without a real wait)."""
        task = asyncio.ensure_future(work)
        stalled = 0
        for _ in range(10_000):
            await checkpoint()
            if task.done():
                return task.result()
            # Only a sleeper that stays parked for a few loop turns is really waiting; a timer
            # about to be cancelled must not move time under an answered request.
            stalled = stalled + 1 if self.clock.pending else 0
            if stalled >= 5:
                self.clock.advance(step)
                stalled = 0
        task.cancel()
        raise AssertionError("the work did not finish")


__all__ = ["ALL_KINDS", "FakeBroker", "Reply"]
