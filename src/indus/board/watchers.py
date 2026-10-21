"""Board watchers for the gateway daemon: chat notices, the dispatcher loop and upkeep (P24-12).

``BoardWatchers`` serves one board:

- ``notify_once()`` posts each subscribed chat the task events it has not heard yet
  (``NOTIFY_KINDS``: completed, blocked, escalated, review requested, changes requested, gave up,
  timed out, crashed). The subscription's cursor moves past an event only after the send worked;
  a failed send leaves the cursor and counts a failure, and after ``MAX_SEND_FAILURES`` in a row
  the subscription is dropped. A subscription ends when its task is archived (``done`` can be
  reopened, so it is not a reason to stop listening).
- ``gc_once()`` (hourly from ``run``) forgets the worker processes of runs that ended more than
  ``GC_RETENTION_S`` ago, removes the logs and prompt files of tasks finished that long ago, and
  drops subscriptions of archived tasks.
- ``run(stop)`` loops until ``stop`` is set: a dispatcher tick every ``dispatch_interval_s`` (in a
  worker thread, so the loop never blocks), notifications every ``notify_interval_s``, upkeep every
  ``GC_INTERVAL_S``. One failing pass is logged and the next one runs.

``deliver(target, text)`` is the daemon's send function: ``target`` is
``{"platform", "chat_id", "thread_id", "profile"}``; it returns True when the message was sent.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from indus.board.dispatcher import Dispatcher
from indus.foundation.blocking import run_blocking
from indus.state.board import BoardStore, Subscription, TaskEvent

__all__ = [
    "GC_INTERVAL_S",
    "GC_RETENTION_S",
    "MAX_SEND_FAILURES",
    "NOTIFY_KINDS",
    "BoardWatchers",
    "Deliver",
    "WatchReport",
    "format_event",
]

log = logging.getLogger(__name__)

GC_INTERVAL_S = 3600.0
GC_RETENTION_S = 30 * 24 * 3600
MAX_SEND_FAILURES = 12
NOTIFY_KINDS: tuple[str, ...] = (
    "completed",
    "blocked",
    "block_loop",
    "review_requested",
    "changes_requested",
    "gave_up",
    "timed_out",
    "crashed",
)

type Deliver = Callable[[Mapping[str, str | None], str], Awaitable[bool]]


def _text(payload: object, key: str, limit: int = 300) -> str:
    if not isinstance(payload, dict):
        return ""
    value: Any = payload.get(key)
    text = str(value).strip() if value is not None else ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def format_event(event: TaskEvent, title: str) -> str | None:
    """The chat line for one event, or None for kinds that are not posted."""
    head = f"[{event.task_id}] {title}"
    p = event.payload
    if event.kind == "completed":
        summary = _text(p, "summary")
        return f"{head}: done." + (f" {summary}" if summary else "")
    if event.kind == "blocked":
        kind = _text(p, "kind", 40)
        return f"{head}: blocked" + (f" ({kind})" if kind else "") + f": {_text(p, 'reason')}"
    if event.kind == "block_loop":
        return (
            f"{head}: blocked again for the same reason ({_text(p, 'kind', 40) or 'untyped'}); "
            f"it needs a person: {_text(p, 'reason')}"
        )
    if event.kind == "review_requested":
        who = _text(p, "reviewer", 60)
        return (
            f"{head}: ready for review" + (f" by {who}" if who else "") + f". {_text(p, 'summary')}"
        )
    if event.kind == "changes_requested":
        return f"{head}: changes requested: {_text(p, 'reason')}"
    if event.kind == "gave_up":
        return f"{head}: stopped after repeated failures: {_text(p, 'error')}"
    if event.kind == "timed_out":
        return f"{head}: the worker ran out of time; it will be retried."
    if event.kind == "crashed":
        return f"{head}: the worker stopped unexpectedly; it will be retried."
    return None


@dataclass
class WatchReport:
    sent: int = 0
    failed: int = 0
    dropped: list[Subscription] = field(default_factory=lambda: [])


class BoardWatchers:
    def __init__(
        self,
        store: BoardStore,
        *,
        deliver: Deliver,
        dispatcher: Dispatcher | None = None,
        notify_interval_s: float = 5.0,
        dispatch_interval_s: float = 60.0,
    ) -> None:
        self.store = store
        self.deliver = deliver
        self.dispatcher = dispatcher
        self.notify_interval_s = max(notify_interval_s, 1.0)
        self.dispatch_interval_s = max(dispatch_interval_s, 1.0)

    # -- notifications ------------------------------------------------------------------------

    async def notify_once(self) -> WatchReport:
        report = WatchReport()
        for sub in self.store.subscriptions():
            task = self.store.get(sub.task_id)
            if task is None or task.status == "archived":
                self.store.unsubscribe(sub)
                report.dropped.append(sub)
                continue
            cursor = sub.cursor
            failed = False
            for event in self.store.events_after(cursor, task_id=sub.task_id, kinds=NOTIFY_KINDS):
                text = format_event(event, task.title)
                if text is None:
                    cursor = event.id
                    continue
                target = {
                    "platform": sub.platform,
                    "chat_id": sub.chat_id,
                    "thread_id": sub.thread_id or None,
                    "profile": sub.profile,
                }
                try:
                    ok = await self.deliver(target, text)
                except Exception as exc:
                    log.warning(
                        "board %s: notifying %s about %s failed: %s",
                        self.store.slug,
                        sub.platform,
                        sub.task_id,
                        type(exc).__name__,
                    )
                    ok = False
                if not ok:
                    failed = True
                    break
                cursor = event.id
                report.sent += 1
            if failed:
                report.failed += 1
                if sub.failures + 1 >= MAX_SEND_FAILURES:
                    self.store.unsubscribe(sub)
                    report.dropped.append(sub)
                else:
                    self.store.move_cursor(sub, cursor, failed=True)
            elif cursor != sub.cursor or sub.failures:
                self.store.move_cursor(sub, cursor)
        return report

    # -- upkeep -------------------------------------------------------------------------------

    def gc_once(self, *, retention_s: int = GC_RETENTION_S) -> dict[str, int]:
        cutoff = self.store.now() - retention_s
        forgotten = 0
        for run in self.store.finished_runs_with_workers(cutoff):
            self.store.forget_worker(run.id)
            forgotten += 1
        removed = 0
        logs = self.store.logs_dir()
        for task in self.store.list(include_archived=True):
            finished = task.status in ("done", "archived")
            if not finished or (task.completed_at or task.created_at) >= cutoff:
                continue
            for path in logs.glob(f"{task.id}.*"):
                with contextlib.suppress(OSError):
                    path.unlink()
                    removed += 1
        dropped = self.store.drop_archived_subscriptions()
        return {"workers_forgotten": forgotten, "files_removed": removed, "subs_dropped": dropped}

    # -- the loop -----------------------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        clock = self.store.clock
        next_gc = clock.monotonic()
        next_dispatch = clock.monotonic()
        while not stop.is_set():
            now = clock.monotonic()
            if self.dispatcher is not None and now >= next_dispatch:
                next_dispatch = now + self.dispatch_interval_s
                try:
                    await run_blocking(self.dispatcher.tick, label="board dispatch")
                except Exception:
                    log.warning("board %s: dispatcher tick failed", self.store.slug, exc_info=True)
            try:
                await self.notify_once()
            except Exception:
                log.warning("board %s: notifier pass failed", self.store.slug, exc_info=True)
            if now >= next_gc:
                next_gc = now + GC_INTERVAL_S
                try:
                    await run_blocking(self.gc_once, label="board gc")
                except Exception:
                    log.warning("board %s: upkeep failed", self.store.slug, exc_info=True)
            if stop.is_set():
                break
            await clock.sleep(self.notify_interval_s)
