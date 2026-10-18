"""Run planned tool segments: bounded concurrency, deadlines, interrupts (R01.14).

A parallel segment runs as asyncio tasks, at most ``max_workers`` at once (``image_workers`` when
the segment has an image-generation call), under one batch deadline. Workers begin in call order:
each waits until the previous one has started, but no longer than the start-order gate, so a
wedged worker cannot hold the rest back. A sequential call runs alone under its own deadline.

Every deadline is measured on the injected clock and extended by the time a person spent answering
approval prompts meanwhile (``human_wait`` is the broker's cumulative counter, open waits
included). When the turn is stopped (``stop`` is set) or a deadline passes, unfinished work is
cancelled and never awaited again; a sync handler's worker thread is abandoned, not joined.

Each call ends as exactly one ``SlotOutcome``: its real result, or a synthesized one — ``timeout``
(deadline), ``cancelled`` (it was running when the turn stopped), ``skipped`` (it never started),
``error`` (the worker itself failed). While tools run, ``touch`` is called every
``heartbeat_s`` so the session's activity stamp stays fresh.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from indus.core import loop_texts as texts
from indus.core.tool_plan import Segment
from indus.tools.calls import ToolResult

if TYPE_CHECKING:
    from indus.foundation.clock import Clock

log = logging.getLogger(__name__)

__all__ = ["ExecLimits", "Executor", "SlotOutcome", "SlotStatus"]

type SlotStatus = Literal["ok", "error", "timeout", "cancelled", "skipped"]
IMAGE_TOOL = "image_generate"


@dataclass(frozen=True)
class ExecLimits:
    max_workers: int = 8
    image_workers: int = 4
    batch_deadline_s: float | None = 420.0
    call_deadline_s: float | None = 420.0
    start_gate_s: float = 120.0
    heartbeat_s: float = 30.0

    def gate_seconds(self) -> float:
        """The start-order gate sits under the batch deadline (half of it at most)."""
        if self.batch_deadline_s is None:
            return self.start_gate_s
        return min(self.start_gate_s, self.batch_deadline_s / 2)


@dataclass(frozen=True)
class SlotOutcome:
    index: int
    result: ToolResult
    status: SlotStatus
    duration_s: float = 0.0
    started: bool = False

    @property
    def ok(self) -> bool:
        return self.status == "ok" and not self.result.is_error


type RunOne = Callable[[int], Awaitable[ToolResult]]
type Commit = Callable[[SlotOutcome], Awaitable[None]]


class _Gate:
    """Start order: worker ``pos`` waits for worker ``pos - 1`` to start (bounded)."""

    def __init__(self, count: int, clock: Clock, timeout_s: float) -> None:
        self.started = [asyncio.Event() for _ in range(count)]
        self.abandoned = asyncio.Event()
        self._clock = clock
        self._timeout = timeout_s

    async def wait_turn(self, pos: int) -> bool:
        if pos == 0 or self.started[pos - 1].is_set():
            return not self.abandoned.is_set()
        waits = {
            asyncio.ensure_future(self.started[pos - 1].wait()),
            asyncio.ensure_future(self.abandoned.wait()),
            asyncio.ensure_future(self._clock.sleep(self._timeout)),
        }
        try:
            await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waits:
                waiter.cancel()
        if not self.started[pos - 1].is_set() and not self.abandoned.is_set():
            log.warning("tool start-order gate timed out at position %d; starting anyway", pos)
        return not self.abandoned.is_set()

    def abandon(self) -> None:
        self.abandoned.set()


class Executor:
    """Runs segments for one tool batch. ``names[i]`` is call ``i``'s tool name."""

    def __init__(
        self,
        *,
        clock: Clock,
        run_one: RunOne,
        names: Sequence[str],
        stop: asyncio.Event,
        limits: ExecLimits | None = None,
        call_deadline: Callable[[int], float | None] | None = None,
        human_wait: Callable[[], float] | None = None,
        touch: Callable[[str], object] | None = None,
        on_start: Callable[[int], object] | None = None,
    ) -> None:
        self.clock = clock
        self.limits = limits or ExecLimits()
        self._run_one = run_one
        self._names = list(names)
        self._stop = stop
        self._call_deadline = call_deadline
        self._human_wait = human_wait or (lambda: 0.0)
        self._touch = touch
        self._on_start = on_start
        self.max_running = 0
        self._running = 0
        self._last_touch = clock.monotonic()

    # -- public ----------------------------------------------------------------------------------

    async def run(self, segments: Sequence[Segment], commit: Commit) -> None:
        """Run every segment in order; ``commit`` gets each outcome in call order."""
        for segment in segments:
            if segment.kind == "parallel" and not self._stop.is_set():
                for outcome in await self.run_parallel(segment.indices):
                    await commit(outcome)
                continue
            for index in segment.indices:
                await commit(await self.run_single(index))

    async def run_single(self, index: int) -> SlotOutcome:
        if self._stop.is_set():
            return self._skipped(index)
        deadline = self._deadline_for(index)
        task: asyncio.Future[Any] = asyncio.ensure_future(self._call(index))
        started_at = self.clock.monotonic()
        how = await self._await(
            {task}, None if deadline is None else started_at + deadline, f"tool {self._name(index)}"
        )
        if how == "done":
            return task.result()
        await self._abandon({task})
        elapsed = self.clock.monotonic() - started_at
        if how == "timeout":
            return self._timed_out(index, deadline, elapsed)
        return self._cancelled(index, elapsed)

    async def run_parallel(self, indices: Sequence[int]) -> list[SlotOutcome]:
        limit = self.limits.max_workers
        if any(self._name(i) == IMAGE_TOOL for i in indices):
            limit = min(limit, self.limits.image_workers)
        limit = max(1, min(limit, len(indices)))
        semaphore = asyncio.Semaphore(limit)
        gate = _Gate(len(indices), self.clock, self.limits.gate_seconds())
        slots: dict[int, SlotOutcome] = {}

        async def worker(pos: int, index: int) -> None:
            async with semaphore:
                if not await gate.wait_turn(pos):
                    return
                gate.started[pos].set()
                slots[index] = await self._call(index)

        started_at = self.clock.monotonic()
        tasks: set[asyncio.Future[Any]] = {
            asyncio.ensure_future(worker(pos, i)) for pos, i in enumerate(indices)
        }
        batch = self.limits.batch_deadline_s
        how = await self._await(
            tasks, None if batch is None else started_at + batch, f"{len(indices)} tools"
        )
        if how != "done":
            gate.abandon()
            await self._abandon(tasks)
        elapsed = self.clock.monotonic() - started_at
        out: list[SlotOutcome] = []
        for pos, index in enumerate(indices):
            found = slots.get(index)
            if found is not None:
                out.append(found)
            elif how == "timeout":
                out.append(self._timed_out(index, batch, elapsed))
            elif gate.started[pos].is_set():
                out.append(self._cancelled(index, elapsed))
            else:
                out.append(self._skipped(index))
        if how == "timeout":
            log.warning("tool batch hit its %.0f s deadline; %d unfinished", batch or 0, len(out))
        return out

    # -- one call --------------------------------------------------------------------------------

    def _name(self, index: int) -> str:
        return self._names[index] if 0 <= index < len(self._names) else "tool"

    def _deadline_for(self, index: int) -> float | None:
        if self._call_deadline is not None:
            return self._call_deadline(index)
        return self.limits.call_deadline_s

    async def _call(self, index: int) -> SlotOutcome:
        began = self.clock.monotonic()
        self._running += 1
        self.max_running = max(self.max_running, self._running)
        if self._on_start is not None:
            try:
                self._on_start(index)
            except Exception:
                log.debug("tool start callback failed", exc_info=True)
        try:
            result = await self._run_one(index)
            status: SlotStatus = "ok"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("tool worker for %s failed", self._name(index), exc_info=exc)
            result = ToolResult.error(
                f"Tool failed: {type(exc).__name__}: {exc}", error_type="exception"
            )
            status = "error"
        finally:
            self._running -= 1
        return SlotOutcome(index, result, status, self.clock.monotonic() - began, True)

    def _timed_out(self, index: int, deadline: float | None, elapsed: float) -> SlotOutcome:
        text = texts.TOOL_TIMED_OUT.format(name=self._name(index), seconds=deadline or elapsed)
        return SlotOutcome(
            index, ToolResult.error(text, error_type="timeout"), "timeout", elapsed, True
        )

    def _cancelled(self, index: int, elapsed: float) -> SlotOutcome:
        text = texts.TOOL_CANCELLED.format(name=self._name(index))
        return SlotOutcome(
            index, ToolResult.error(text, error_type="cancelled"), "cancelled", elapsed, True
        )

    def _skipped(self, index: int) -> SlotOutcome:
        text = texts.TOOL_SKIPPED.format(name=self._name(index))
        return SlotOutcome(index, ToolResult.error(text, error_type="skipped"), "skipped")

    # -- waiting ---------------------------------------------------------------------------------

    async def _await(
        self, tasks: set[asyncio.Future[Any]], deadline_at: float | None, label: str
    ) -> Literal["done", "timeout", "stopped"]:
        pending: set[asyncio.Future[Any]] = set(tasks)
        baseline = self._human_wait()
        stopper = asyncio.ensure_future(self._stop.wait())
        try:
            while True:
                pending = {t for t in pending if not t.done()}
                if not pending:
                    return "done"
                if self._stop.is_set():
                    return "stopped"
                now = self.clock.monotonic()
                slice_s = self.limits.heartbeat_s
                if deadline_at is not None:
                    left = deadline_at + max(0.0, self._human_wait() - baseline) - now
                    if left <= 0:
                        return "timeout"
                    slice_s = min(slice_s, left)
                sleeper = asyncio.ensure_future(self.clock.sleep(slice_s))
                try:
                    await asyncio.wait(
                        pending | {sleeper, stopper}, return_when=asyncio.FIRST_COMPLETED
                    )
                finally:
                    sleeper.cancel()
                self._heartbeat(label)
        finally:
            stopper.cancel()

    def _heartbeat(self, label: str) -> None:
        now = self.clock.monotonic()
        if self._touch is None or now - self._last_touch < self.limits.heartbeat_s:
            return
        self._last_touch = now
        try:
            self._touch(f"{label} running")
        except Exception:
            log.debug("activity heartbeat failed", exc_info=True)

    async def _abandon(self, tasks: set[asyncio.Future[Any]]) -> None:
        """Cancel unfinished work and let each task see its cancellation once (no join)."""
        live = [t for t in tasks if not t.done()]
        for task in live:
            task.cancel()
        for task in live:
            with contextlib.suppress(BaseException):
                await asyncio.wait({task}, timeout=0)
