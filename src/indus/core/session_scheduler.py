"""The idle-session scheduler shared by loops and heartbeats (P10-25, P10-26), and the two ports
goals, loops and heartbeats work through.

- ``MetaStore``: per-session metadata (P02-23 ``session_meta``) as async calls. ``StoreMeta`` runs
  them on a store handle (a session that has no row yet gets a bare one first, so the foreign key
  holds); ``MemoryMeta`` keeps them in a dict for hosts without a store and for tests.
- ``SessionHost``: what these features need from the host of live sessions (the session service):
  which sessions are attached, whether one is idle (no running turn and nothing waiting in its
  input queue), starting a turn with an ordinary user message, and sending an event.
- ``SessionScheduler``: one asyncio task per host. Every ``POLL_SECONDS`` on the host's clock it
  asks each ``TickSource`` (loops, heartbeats) for its due rows (``session_meta.due``, earliest
  first) and lets the source fire the ones whose session is attached here and idle. A session gets
  at most one fire per poll: the fired turn makes it busy for the rest of the poll.

Nothing here is durable across processes: rows for sessions this process does not host are left
alone (durable schedules are cron's, P14).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from indus.foundation.blocking import run_blocking
from indus.foundation.clock import Clock
from indus.protocol.events import EventBase
from indus.state import session_meta, sessions
from indus.state.registry import StoreHandle
from indus.state.session_meta import DueEntry

__all__ = [
    "POLL_SECONDS",
    "DueEntry",
    "MemoryMeta",
    "MetaStore",
    "SessionHost",
    "SessionScheduler",
    "StoreMeta",
    "TickSource",
    "TurnEnd",
    "TurnEndListener",
]

log = logging.getLogger(__name__)

POLL_SECONDS = 5.0
"""How often the scheduler looks for due loops and heartbeats."""


# -- metadata ------------------------------------------------------------------------------------


class MetaStore(Protocol):
    """Per-session metadata, one JSON value per ``(session, namespace, key)``."""

    async def get(self, session_id: str, namespace: str, key: str) -> Any: ...

    async def put(
        self, session_id: str, namespace: str, key: str, value: object, *, due_at: float | None
    ) -> None: ...

    async def delete(self, session_id: str, namespace: str, key: str) -> bool: ...

    async def entries(self, session_id: str, namespace: str) -> dict[str, Any]: ...

    async def due(self, namespace: str, before: float) -> list[DueEntry]: ...


class StoreMeta:
    """``MetaStore`` over a P02 store handle."""

    def __init__(self, handle: StoreHandle) -> None:
        self.handle = handle

    def _now(self) -> float:
        return self.handle.scope.clock.now().timestamp()

    async def get(self, session_id: str, namespace: str, key: str) -> Any:
        return await run_blocking(
            session_meta.get, self.handle, session_id, namespace, key, label="session meta"
        )

    async def put(
        self, session_id: str, namespace: str, key: str, value: object, *, due_at: float | None
    ) -> None:
        session_meta.encode_value(value)  # refuse before taking the write lock
        now = self._now()

        def write(conn: Any) -> None:
            # A fresh session has no row until its first turn is stored; metadata needs one.
            if sessions.get_in(conn, session_id) is None:
                sessions.create_in(
                    conn, session_id, "unknown", now=now, profile_id=self.handle.scope.profile_id
                )
            session_meta.put_in(conn, session_id, namespace, key, value, now=now, due_at=due_at)

        await self.handle.awrite(write)

    async def delete(self, session_id: str, namespace: str, key: str) -> bool:
        return await self.handle.awrite(
            lambda conn: session_meta.delete_in(conn, session_id, namespace, key)
        )

    async def entries(self, session_id: str, namespace: str) -> dict[str, Any]:
        return await run_blocking(
            session_meta.list_entries, self.handle, session_id, namespace, label="session meta"
        )

    async def due(self, namespace: str, before: float) -> list[DueEntry]:
        return await run_blocking(
            session_meta.due, self.handle, namespace, before, label="session meta"
        )


class MemoryMeta:
    """``MetaStore`` in a dict: ``rows[(session, namespace, key)] = (value, due_at)``."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str], tuple[Any, float | None]] = {}

    async def get(self, session_id: str, namespace: str, key: str) -> Any:
        found = self.rows.get((session_id, namespace, key))
        return None if found is None else _copy(found[0])

    async def put(
        self, session_id: str, namespace: str, key: str, value: object, *, due_at: float | None
    ) -> None:
        session_meta.encode_value(value)
        self.rows[(session_id, namespace, key)] = (_copy(value), due_at)

    async def delete(self, session_id: str, namespace: str, key: str) -> bool:
        return self.rows.pop((session_id, namespace, key), None) is not None

    async def entries(self, session_id: str, namespace: str) -> dict[str, Any]:
        return {
            k: _copy(v)
            for (sid, ns, k), (v, _due) in sorted(self.rows.items())
            if sid == session_id and ns == namespace
        }

    async def due(self, namespace: str, before: float) -> list[DueEntry]:
        found = [
            DueEntry(sid, key, due, _copy(value))
            for (sid, ns, key), (value, due) in self.rows.items()
            if ns == namespace and due is not None and due <= before
        ]
        return sorted(found, key=lambda e: (e.due_at, e.session_id, e.key))


def _copy(value: Any) -> Any:
    return json.loads(json.dumps(value))


# -- the host --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TurnEnd:
    """A turn of an attached session ended. ``completed``: it ended normally with an answer."""

    session_id: str
    turn_id: str
    final_text: str
    completed: bool
    generation: int


type TurnEndListener = Callable[[TurnEnd], Awaitable[None] | None]


class SessionHost(Protocol):
    """What goals, loops and heartbeats need from the host of live sessions."""

    def attached(self) -> Sequence[str]:
        """Ids of the sessions this host runs now."""
        ...

    def is_idle(self, session_id: str) -> bool:
        """No turn running and nothing waiting in the session's input queue."""
        ...

    def generation(self, session_id: str) -> int:
        """How many turns the session has started in this process (grows by one per turn)."""
        ...

    async def inject(
        self, session_id: str, text: str, *, origin: str, if_generation: int | None = None
    ) -> bool:
        """Start a turn with ``text`` as an ordinary user message. False when the session is not
        idle, or (``if_generation``) another turn started since that generation."""
        ...

    def notify(self, session_id: str, event: type[EventBase], **fields: Any) -> None:
        """Send one event on the session's stream (seq is filled in)."""
        ...

    def on_turn_end(self, listener: TurnEndListener) -> Callable[[], None]:
        """Call ``listener`` after every turn of every attached session; returns the undo."""
        ...


class TickSource(Protocol):
    """Rows of one metadata namespace that come due (loops, heartbeats)."""

    namespace: str

    async def fire(self, entry: DueEntry, now: float) -> bool:
        """Fire one due row of an idle, attached session; True when a turn was started."""
        ...


# -- the scheduler -------------------------------------------------------------------------------


class SessionScheduler:
    """Polls for due loops and heartbeats across the host's sessions (see the module notes)."""

    def __init__(
        self,
        host: SessionHost,
        meta: MetaStore,
        clock: Clock,
        sources: Sequence[TickSource] = (),
        *,
        poll_s: float = POLL_SECONDS,
    ) -> None:
        self.host = host
        self.meta = meta
        self.clock = clock
        self.sources: list[TickSource] = list(sources)
        self.poll_s = poll_s
        self._task: asyncio.Task[None] | None = None

    def add(self, source: TickSource) -> None:
        self.sources.append(source)

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def poll_once(self) -> int:
        """Fire what is due now; returns how many turns were started."""
        now = self.clock.now().timestamp()
        attached = set(self.host.attached())
        fired_in: set[str] = set()
        started = 0
        for source in list(self.sources):
            try:
                rows = await self.meta.due(source.namespace, now)
            except Exception:
                log.warning("could not read due %s rows", source.namespace, exc_info=True)
                continue
            for entry in rows:
                sid = entry.session_id
                if sid not in attached or sid in fired_in or not self.host.is_idle(sid):
                    continue
                try:
                    fired = await source.fire(entry, now)
                except Exception:
                    log.warning(
                        "firing %s %s of session %s failed",
                        source.namespace,
                        entry.key,
                        sid,
                        exc_info=True,
                    )
                    continue
                if fired:
                    fired_in.add(sid)
                    started += 1
        return started

    async def _run(self) -> None:
        while True:
            await self.clock.sleep(self.poll_s)
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("the session scheduler poll failed", exc_info=True)

    def start(self, spawn: Callable[[Any], asyncio.Task[None]] | None = None) -> None:
        """Start polling (once). ``spawn`` runs the coroutine (default: ``create_task``)."""
        if self.running:
            return
        coro = self._run()
        self._task = spawn(coro) if spawn is not None else asyncio.ensure_future(coro)

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
