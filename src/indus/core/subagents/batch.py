"""Running a batch of subagents and collecting their results (P10-03).

``run_batch(runs, ...)`` starts every run as an asyncio task in the given ``Scope`` (a copied
context per task, R01.16) and lets at most ``cap`` of them work at once — the rest wait for a
slot (a shared ``slots`` semaphore makes the cap span several batches, as background delegation
does). It returns one ``ChildOutcome`` per run, in the order the runs were given.

- A run that raises is ``failed`` with the error text; the others go on.
- ``stop`` set (the parent's interrupt) interrupts every run that started — each ends through its
  own turn funnel, so its stored transcript stays well formed — and runs still waiting for a slot
  never start (``interrupted``, "not started").
- Liveness: every ``HEARTBEAT_S`` (30 s) each working run is asked for its progress. Unchanged
  progress for ``STALE_IDLE_CYCLES`` (15) checks between tools, or ``STALE_TOOL_CYCLES`` (40)
  checks inside one tool, interrupts it and reports it ``stale``. There is no wall-clock limit.
  ``on_beat`` is called on every heartbeat (the parent turn touches its own liveness with it).

``clip_summary`` keeps the head (70%) and tail of an over-long summary with a marker naming the
cut; ``result_entry`` is the JSON shape one child takes in the tool result.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Protocol

from indus.foundation.clock import Clock
from indus.foundation.scope import Scope, spawn_with_scope

__all__ = [
    "HEARTBEAT_S",
    "STALE_IDLE_CYCLES",
    "STALE_TOOL_CYCLES",
    "ChildOutcome",
    "ChildRun",
    "Progress",
    "Status",
    "clip_summary",
    "result_entry",
    "run_batch",
]

log = logging.getLogger(__name__)

HEARTBEAT_S = 30.0
STALE_IDLE_CYCLES = 15  # 450 s without progress between tools
STALE_TOOL_CYCLES = 40  # 1,200 s inside the same tool

HEAD_SHARE = 0.7
NOT_STARTED = "The subagent never started: the parent was interrupted first."
STALE_NOTE = (
    "The subagent made no progress for too long and was stopped; the summary is what it had."
)

type Status = Literal["completed", "failed", "interrupted", "stale"]


@dataclass(frozen=True)
class Progress:
    """What a run reports at each heartbeat: an opaque marker that changes whenever the child
    does something, and whether it is inside a tool call right now."""

    marker: object
    in_tool: bool = False


@dataclass(frozen=True)
class ChildOutcome:
    status: Status
    summary: str = ""
    schema_valid: bool | None = None
    errors: tuple[str, ...] = ()
    note: str = ""
    orphaned_processes: tuple[Mapping[str, Any], ...] = ()
    worktree: Mapping[str, Any] | None = None
    iterations: int = 0
    extra: Mapping[str, Any] = field(default_factory=dict[str, Any])
    # The child's turns saw outside text: its summary reaches the parent fenced (SEC-4).
    read_untrusted: bool = False


class ChildRun(Protocol):
    """One subagent as the batch runner sees it."""

    @property
    def subagent_id(self) -> str: ...

    @property
    def goal(self) -> str: ...

    async def run(self) -> ChildOutcome: ...

    def interrupt(self) -> None: ...

    def progress(self) -> Progress: ...


def clip_summary(text: str, cap: int) -> str:
    """``text`` cut to at most ``cap`` characters: head and tail kept, a marker in the middle."""
    if cap <= 0 or len(text) <= cap:
        return text
    cut = len(text) - cap
    marker = f"\n\n[... {cut:,} characters of this summary were cut here ...]\n\n"
    while True:  # the marker's own length moves the cut; settle it
        room = max(0, cap - len(marker))
        cut_now = len(text) - room
        settled = f"\n\n[... {cut_now:,} characters of this summary were cut here ...]\n\n"
        if settled == marker:
            break
        marker = settled
    head = int(room * HEAD_SHARE)
    tail = room - head
    return text[:head] + marker + (text[-tail:] if tail else "")


def result_entry(run: ChildRun, outcome: ChildOutcome, cap: int) -> dict[str, Any]:
    """The JSON shape of one child in the ``delegate_task`` result."""
    entry: dict[str, Any] = {
        "subagent_id": run.subagent_id,
        "goal": run.goal,
        "status": outcome.status,
        "summary": clip_summary(outcome.summary, cap),
    }
    if outcome.schema_valid is not None:
        entry["schema_valid"] = outcome.schema_valid
    if outcome.errors:
        entry["errors"] = list(outcome.errors)
    if outcome.note:
        entry["note"] = outcome.note
    if outcome.orphaned_processes:
        entry["orphaned_processes"] = [dict(p) for p in outcome.orphaned_processes]
    if outcome.worktree is not None:
        entry["worktree"] = dict(outcome.worktree)
    entry.update(outcome.extra)
    return entry


@dataclass
class _Watch:
    run: ChildRun
    started: bool = False
    done: bool = False
    stale: bool = False
    last: object = None
    idle: int = 0
    in_tool: int = 0


def _beat(watch: _Watch) -> bool:
    """One liveness check; True when the run has just gone stale."""
    try:
        seen = watch.run.progress()
    except Exception:
        log.debug("progress of %s unreadable", watch.run.subagent_id, exc_info=True)
        return False
    if seen.marker != watch.last:
        watch.last, watch.idle, watch.in_tool = seen.marker, 0, 0
        return False
    if seen.in_tool:
        watch.in_tool += 1
    else:
        watch.idle += 1
    return watch.idle >= STALE_IDLE_CYCLES or watch.in_tool >= STALE_TOOL_CYCLES


async def run_batch(
    runs: Sequence[ChildRun],
    *,
    cap: int,
    clock: Clock,
    scope: Scope | None = None,
    stop: asyncio.Event | None = None,
    slots: asyncio.Semaphore | None = None,
    on_beat: Callable[[], object] | None = None,
    on_start: Callable[[ChildRun], object] | None = None,
) -> list[ChildOutcome]:
    """Run ``runs`` (see the module notes); outcomes in the order of ``runs``."""
    gate = slots if slots is not None else asyncio.Semaphore(max(1, cap))
    watches = [_Watch(run) for run in runs]

    async def one(watch: _Watch) -> ChildOutcome:
        async with gate:
            if stop is not None and stop.is_set():
                return ChildOutcome("interrupted", note=NOT_STARTED)
            watch.started = True
            watch.last = _marker(watch.run)
            if on_start is not None:
                try:
                    on_start(watch.run)
                except Exception:
                    log.warning("subagent start callback failed", exc_info=True)
            try:
                outcome = await watch.run.run()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("subagent %s failed", watch.run.subagent_id, exc_info=True)
                outcome = ChildOutcome("failed", summary="", note=f"{type(exc).__name__}: {exc}")
            finally:
                watch.done = True
        if watch.stale and outcome.status == "interrupted":
            note = f"{outcome.note} {STALE_NOTE}".strip() if outcome.note else STALE_NOTE
            outcome = replace(outcome, status="stale", note=note)
        return outcome

    tasks = [
        spawn_with_scope(one(w), scope=scope, name=f"subagent {w.run.subagent_id}") for w in watches
    ]

    def interrupt_started() -> None:
        for watch in watches:
            if watch.started and not watch.done:
                _interrupt(watch.run)

    async def heartbeat() -> None:
        while True:
            await clock.sleep(HEARTBEAT_S)
            if on_beat is not None:
                try:
                    on_beat()
                except Exception:
                    log.debug("heartbeat callback failed", exc_info=True)
            for watch in watches:
                if watch.started and not watch.done and not watch.stale and _beat(watch):
                    watch.stale = True
                    log.warning(
                        "subagent %s shows no progress; stopping it as stale",
                        watch.run.subagent_id,
                    )
                    _interrupt(watch.run)

    async def stopper() -> None:
        if stop is None:
            return
        await stop.wait()
        interrupt_started()

    helpers = [
        spawn_with_scope(heartbeat(), scope=scope, name="subagent heartbeat"),
        spawn_with_scope(stopper(), scope=scope, name="subagent stop"),
    ]
    try:
        await asyncio.wait(tasks)
    except asyncio.CancelledError:
        interrupt_started()
        with contextlib.suppress(Exception):
            await asyncio.shield(asyncio.wait(tasks))
        raise
    finally:
        for helper in helpers:
            helper.cancel()
    outcomes: list[ChildOutcome] = []
    for task in tasks:
        if task.cancelled():
            outcomes.append(ChildOutcome("interrupted", note="The subagent task was cancelled."))
            continue
        error = task.exception()
        if error is not None:
            outcomes.append(ChildOutcome("failed", note=f"{type(error).__name__}: {error}"))
            continue
        outcomes.append(task.result())
    return outcomes


def _marker(run: ChildRun) -> object:
    try:
        return run.progress().marker
    except Exception:
        return None


def _interrupt(run: ChildRun) -> None:
    try:
        run.interrupt()
    except Exception:
        log.warning("could not interrupt subagent %s", run.subagent_id, exc_info=True)
