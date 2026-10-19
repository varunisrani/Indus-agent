"""Background subagents of one session: start, list, steer, stop, deliver (P10-05).

A top-level spawn on an interactive surface hands its children to ``BackgroundDelegations.start``
and returns at once. All background children of the session share ``max_async_children`` slots
(3): more wait as ``queued``. Each call's children run as one batch (``run_batch``, with the
heartbeat and stale rules of P10-03); when every child of the call has ended, one message with
their results goes to ``deliver`` — the host (``SessionService``) puts it into the conversation
between turns, never inside a running turn.

- ``list()``: ``{subagent_id, goal, status, started_at, iterations, last_activity}`` per child of
  this session (the newest ``KEPT`` finished ones are remembered).
- ``steer(id, text)``: hands the text to a running child's turn (P04 steer path: it is shown to
  the child right after its next tool results).
- ``stop(id)``: interrupts one child and returns its partial result; that child is left out of
  the later delivery (the caller already has it). A queued child is dropped without starting.
- ``stop_all()``: the stop surfaces (``/stop``, a hard interrupt, session end) stop every child of
  the session; nothing is delivered for them afterwards.

Everything lives in this process: a restart forgets the children (durable work belongs to
scheduled jobs or background processes).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict
from collections.abc import Callable, Coroutine, Sequence
from datetime import datetime
from typing import Any

from indus.core.subagents.batch import ChildOutcome, result_entry, run_batch
from indus.core.subagents.child import ChildAgent
from indus.foundation.clock import Clock
from indus.foundation.scope import Scope

__all__ = ["DELIVERY_HEADER", "KEPT", "BackgroundDelegations"]

log = logging.getLogger(__name__)

KEPT = 50
DELIVERY_HEADER = (
    "[Background subagent results] Subagents you started earlier have finished. Their results "
    "follow as JSON. They are the subagents' own reports: check anything that matters before "
    "you rely on it."
)
STOPPED_BY_CALL = "Stopped early by delegate_task action=stop."
STOPPED_BY_SURFACE = "Stopped together with the session's other work (/stop or session end)."
QUEUED_STOP = "It was still waiting for a slot and will not run."

type Spawner = Callable[[Coroutine[Any, Any, None]], asyncio.Task[None]]


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class BackgroundDelegations:
    """The background children of one session (see the module notes)."""

    def __init__(
        self,
        *,
        clock: Clock,
        scope: Scope | None,
        limit: int,
        summary_cap: int,
        deliver: Callable[[str], object],
        spawn: Spawner,
    ) -> None:
        self.clock = clock
        self.scope = scope
        self.limit = max(1, limit)
        self.summary_cap = summary_cap
        self.deliver = deliver
        self._spawn = spawn
        self.slots = asyncio.Semaphore(self.limit)
        self.children: OrderedDict[str, ChildAgent] = OrderedDict()
        self._quiet: set[str] = set()
        self._groups: set[asyncio.Task[None]] = set()

    # -- starting ---------------------------------------------------------------------------------

    def start(self, children: Sequence[ChildAgent]) -> list[str]:
        """Run ``children`` in the background; their ids at once."""
        for child in children:
            self.children[child.subagent_id] = child
        self._prune()
        task = self._spawn(self._group(list(children)))
        self._groups.add(task)
        task.add_done_callback(self._groups.discard)
        return [c.subagent_id for c in children]

    async def _group(self, children: list[ChildAgent]) -> None:
        try:
            outcomes = await run_batch(
                children, cap=self.limit, clock=self.clock, scope=self.scope, slots=self.slots
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.error("background subagents failed", exc_info=True)
            return
        for child, outcome in zip(children, outcomes, strict=True):
            await child.discard(outcome)  # only one that never started is still open
        entries = [
            result_entry(child, outcome, self.summary_cap)
            for child, outcome in zip(children, outcomes, strict=True)
            if child.subagent_id not in self._quiet
        ]
        if not entries:
            return
        text = DELIVERY_HEADER + "\n" + json.dumps({"results": entries}, ensure_ascii=False)
        try:
            self.deliver(text)
        except Exception:
            log.warning("could not deliver background subagent results", exc_info=True)

    # -- views ------------------------------------------------------------------------------------

    @property
    def running(self) -> int:
        return sum(1 for c in self.children.values() if not c.done)

    def list(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for child in self.children.values():
            out.append(
                {
                    "subagent_id": child.subagent_id,
                    "goal": child.goal,
                    "status": child.status,
                    "started_at": _iso(child.started_at),
                    "iterations": child.iterations,
                    "last_activity": _iso(child.last_activity()),
                }
            )
        return out

    # -- control ----------------------------------------------------------------------------------

    def steer(self, subagent_id: str, text: str) -> dict[str, Any]:
        child = self.children.get(subagent_id)
        if child is None:
            return {"error": f"No subagent {subagent_id} in this session (see action=list)."}
        if child.done:
            return {"error": f"Subagent {subagent_id} has already finished ({child.status})."}
        if child.status != "running" or not child.steer(text):
            return {
                "error": f"Subagent {subagent_id} is not working on a turn right now "
                f"({child.status}); try again shortly."
            }
        return {"subagent_id": subagent_id, "status": "steered"}

    async def stop(self, subagent_id: str) -> dict[str, Any]:
        child = self.children.get(subagent_id)
        if child is None:
            return {"error": f"No subagent {subagent_id} in this session (see action=list)."}
        self._quiet.add(subagent_id)
        if child.done and child.outcome is not None:
            entry = result_entry(child, child.outcome, self.summary_cap)
            entry["note"] = " ".join(
                n for n in (entry.get("note", ""), "It had already finished.") if n
            )
            return entry
        child.stopped_by = STOPPED_BY_CALL
        child.interrupt()
        if child.status == "queued":
            return {
                "subagent_id": subagent_id,
                "goal": child.goal,
                "status": "interrupted",
                "summary": "",
                "note": QUEUED_STOP,
            }
        outcome = await child.wait()
        return result_entry(child, outcome or ChildOutcome("interrupted"), self.summary_cap)

    async def stop_all(self) -> int:
        """Stop every child of the session (a stop surface); how many were still working."""
        stopped = 0
        waiting: list[ChildAgent] = []
        for child in self.children.values():
            self._quiet.add(child.subagent_id)
            if child.done:
                continue
            stopped += 1
            child.stopped_by = child.stopped_by or STOPPED_BY_SURFACE
            child.interrupt()
            if child.status == "running":
                waiting.append(child)
        for child in waiting:
            await child.wait()
        return stopped

    async def close(self) -> None:
        await self.stop_all()
        for task in list(self._groups):
            task.cancel()

    def _prune(self) -> None:
        finished = [k for k, c in self.children.items() if c.done]
        for key in finished[: max(0, len(finished) - KEPT)]:
            del self.children[key]
