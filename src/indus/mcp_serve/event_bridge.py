"""The event bridge of ``indus mcp serve`` (P19-15): new chat messages as a queue a client polls.

``EventBridge(reader, clock)`` runs as a task on the server's loop:

- **start.** The baseline: every routed conversation's newest message id, and the store's newest
  id overall (a conversation that only appears later starts after it). History is never replayed
  as events. A store that does not exist yet has an empty baseline: everything it holds once it
  appears arrived after the start.
- **tick, every 200 ms on the injected clock.** Read the store's change counter (``PRAGMA
  data_version`` on the reader's connection); when it has not moved, stop there. When it moved,
  reload the routing index and read, per conversation, the live user and assistant rows after its
  last seen id, in id order (R03.14, never timestamps). Rows copied by a compaction (same message
  uid as an older row of the session or its parent) and compression handoff rows are not news.
  Each new text becomes one ``message`` event (content cut to 500 characters).
- **queue.** At most 1,000 events; the oldest go first. Cursors are integers that only grow, so
  a caller whose cursor fell behind the oldest kept event learns how many it missed (``dropped``).
- **poll / wait.** ``poll(after, key, limit)`` answers at once; ``wait(after, key, timeout_ms)``
  answers as soon as a matching event is queued, re-checking at least every 200 ms, or with
  nothing at its deadline.

Other parts of the server add events with ``publish`` (the approval watcher, P19-17). Store reads
run on the blocking pool; everything else runs on the loop, so no locks are needed for the queue.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from indus.foundation.blocking import run_blocking
from indus.foundation.clock import Clock
from indus.foundation.errors import StoreUnavailable
from indus.mcp_serve.read_tools import TEXT_ROLES, conversations_in, iso_time, message_text
from indus.mcp_serve.store import StoreReader
from indus.state.handoff import not_handoff_sql
from indus.state.messages import decode_content

__all__ = [
    "EVENT_CHARS",
    "POLL_SECONDS",
    "QUEUE_LIMIT",
    "EventBridge",
    "QueueEvent",
]

log = logging.getLogger(__name__)

POLL_SECONDS: Final = 0.2
QUEUE_LIMIT: Final = 1000
EVENT_CHARS: Final = 500

type Offload = Callable[[Callable[[], Any]], Awaitable[Any]]

_NEWEST: Final = "SELECT COALESCE(MAX(id), 0) FROM messages"
_NEW_ROWS: Final = f"""
SELECT m.id, m.role, m.content, m.timestamp FROM messages AS m
WHERE m.session_id = ? AND m.id > ? AND m.active = 1 AND m.role IN ('user', 'assistant')
  AND {not_handoff_sql("m.")}
  AND (m.message_uid IS NULL OR NOT EXISTS (
    SELECT 1 FROM messages AS o
    WHERE o.message_uid = m.message_uid AND o.id < m.id
      AND o.session_id IN (
        m.session_id,
        COALESCE((SELECT s.parent_session_id FROM sessions AS s WHERE s.id = m.session_id), '')
      )
  ))
ORDER BY m.id
"""


@dataclass
class QueueEvent:
    cursor: int
    type: str
    session_key: str = ""
    data: dict[str, Any] = field(default_factory=dict[str, Any])

    def as_json(self) -> dict[str, Any]:
        head = {"cursor": self.cursor, "type": self.type, "session_key": self.session_key}
        return {**head, **self.data}


@dataclass(frozen=True)
class _Found:
    row_id: int
    session_key: str
    role: str
    text: str
    timestamp: object


async def _on_pool(fn: Callable[[], Any]) -> Any:
    return await run_blocking(fn, label="mcp serve store read")


class EventBridge:
    """See the module notes."""

    def __init__(
        self,
        reader: StoreReader,
        clock: Clock,
        *,
        poll_seconds: float = POLL_SECONDS,
        queue_limit: int = QUEUE_LIMIT,
        offload: Offload | None = None,
    ) -> None:
        self.reader = reader
        self.clock = clock
        self.poll_seconds = poll_seconds
        self.queue_limit = queue_limit
        self._offload: Offload = offload or _on_pool
        self._queue: deque[QueueEvent] = deque()
        self._cursor = 0
        self._changed = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._version: int | None = None
        self._seen: dict[str, int] = {}
        self._floor = 0
        self._based = False
        self.index_loads = 0
        """How many times the routing index was reloaded (a change of the store was seen)."""
        self._listeners: list[Callable[[], Awaitable[None]]] = []

    # -- lifecycle --------------------------------------------------------------------------------

    async def baseline(self) -> None:
        """Record where history ends (see the module notes); ``start`` does it first."""
        await self._offload(self._baseline)

    async def start(self) -> None:
        """Take the baseline, then watch in the background."""
        await self.baseline()
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="mcp serve event bridge")

    async def aclose(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._wake()

    def on_tick(self, listener: Callable[[], Awaitable[None]]) -> None:
        """Run ``listener`` after every tick (the approval watcher)."""
        self._listeners.append(listener)

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.debug("event bridge tick failed", exc_info=True)
            for listener in list(self._listeners):
                try:
                    await listener()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.debug("event bridge listener failed", exc_info=True)
            await self.clock.sleep(self.poll_seconds)

    # -- the store side (blocking: runs on the pool) ----------------------------------------------

    def _baseline(self) -> None:
        try:
            self._version = self.reader.data_version()

            # Row ids only grow and one writer commits at a time, so "after the newest id now" is
            # "after the start" for every conversation, those the index names later included.
            self._floor = self.reader.read(lambda conn: int(conn.execute(_NEWEST).fetchone()[0]))
            self._seen = {}
            self._based = True
        except StoreUnavailable:
            self._version = None
            self._based = True  # nothing yet: everything that appears later is new
        except sqlite3.Error:
            self._version = None
            log.debug("event bridge baseline failed; retrying on the next tick", exc_info=True)

    def scan(self) -> list[_Found]:
        """One look at the store (see the module notes); the rows that are news, in id order."""
        if not self._based:
            self._baseline()
            if not self._based:
                return []
        try:
            version = self.reader.data_version()
        except (StoreUnavailable, sqlite3.Error):
            self._version = None
            return []
        if self._version is not None and version == self._version:
            return []
        self._version = version
        self.index_loads += 1

        def read(conn: sqlite3.Connection) -> list[_Found]:
            found: list[_Found] = []
            for conv in conversations_in(conn):
                if not conv.session_id:
                    continue
                since = self._seen.get(conv.session_key, self._floor)
                for row in conn.execute(_NEW_ROWS, (conv.session_id, since)).fetchall():
                    text = message_text(decode_content(row[2]))
                    if text and str(row[1]) in TEXT_ROLES:
                        found.append(
                            _Found(int(row[0]), conv.session_key, str(row[1]), text, row[3])
                        )
                newest = conn.execute(
                    "SELECT COALESCE(MAX(id), 0) FROM messages WHERE session_id = ? AND id > ?",
                    (conv.session_id, since),
                ).fetchone()[0]
                self._seen[conv.session_key] = max(since, int(newest))
            found.sort(key=lambda f: f.row_id)
            return found

        try:
            return self.reader.read(read)
        except (StoreUnavailable, sqlite3.Error):
            self._version = None  # read again in full on the next tick
            return []

    # -- the loop side ----------------------------------------------------------------------------

    async def tick(self) -> int:
        """One poll of the store; how many events it queued."""
        found: list[_Found] = await self._offload(self.scan)
        for item in found:
            self.publish(
                "message",
                item.session_key,
                {
                    "role": item.role,
                    "content": item.text[:EVENT_CHARS],
                    "timestamp": iso_time(item.timestamp),
                    "message_id": str(item.row_id),
                },
            )
        return len(found)

    def publish(self, kind: str, session_key: str, data: Mapping[str, Any]) -> QueueEvent:
        """Queue one event and wake the waiters."""
        self._cursor += 1
        event = QueueEvent(self._cursor, kind, session_key, dict(data))
        self._queue.append(event)
        while len(self._queue) > self.queue_limit:
            self._queue.popleft()
        self._wake()
        return event

    def _wake(self) -> None:
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()

    @property
    def cursor(self) -> int:
        return self._cursor

    def _dropped(self, after: int) -> int:
        if self._queue:
            return max(0, self._queue[0].cursor - 1 - after)
        return max(0, self._cursor - after)

    def _matching(self, after: int, session_key: str | None, limit: int) -> list[QueueEvent]:
        out: list[QueueEvent] = []
        for event in self._queue:
            if event.cursor <= after or (session_key and event.session_key != session_key):
                continue
            out.append(event)
            if len(out) >= limit:
                break
        return out

    def poll(self, after: int, session_key: str | None, limit: int) -> dict[str, Any]:
        events = self._matching(after, session_key, limit)
        answer: dict[str, Any] = {
            "events": [e.as_json() for e in events],
            "next_cursor": events[-1].cursor if events else after,
        }
        dropped = self._dropped(after)
        if dropped:
            answer["dropped"] = dropped
        return answer

    async def wait(self, after: int, session_key: str | None, timeout_ms: int) -> dict[str, Any]:
        deadline = self.clock.monotonic() + timeout_ms / 1000.0
        while True:
            changed = self._changed
            found = self._matching(after, session_key, 1)
            if found:
                answer: dict[str, Any] = {"event": found[0].as_json()}
                dropped = self._dropped(after)
                if dropped:
                    answer["dropped"] = dropped
                return answer
            remaining = deadline - self.clock.monotonic()
            if remaining <= 0:
                return {"event": None, "reason": "timeout"}
            await self._pause(changed, min(remaining, self.poll_seconds))

    async def _pause(self, changed: asyncio.Event, seconds: float) -> None:
        sleeper = asyncio.ensure_future(self.clock.sleep(seconds))
        woken = asyncio.ensure_future(changed.wait())
        try:
            await asyncio.wait({sleeper, woken}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (sleeper, woken):
                if not task.done():
                    task.cancel()
            for task in (sleeper, woken):
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
