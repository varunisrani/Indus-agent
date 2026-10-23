"""Room events pushed to the connections that watch a room (wave 47, RS3).

``RoomWatch(read)`` keeps, per room, the connections that called ``groups.watch`` and each one's
cursor (the last sequence number it was sent). ``poll`` reads every watched room's events after
the lowest cursor (``read(room_id, since_seq)`` → a ``LogPage``, run off the loop) and sends each
watcher the events past its own cursor as ``groups.event`` notifications (``owner.notify``), in
order and once; one poll runs at a time. A page read from ``since`` goes only to the watchers
whose cursor is at least ``since``: one that started watching (or re-watched from an earlier
cursor) while the read ran gets its events on the next poll, never a gap. The log is read
rather than hooked, so events written by any store of the install (the room loop's turns,
replies and approvals; another endpoint's messages) reach every watcher. ``start`` runs
``poll`` every ``WATCH_POLL_S`` (sooner after ``nudge``) while anyone watches; ``unwatch`` and
``drop`` (a closed connection) stop it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol

from indus.foundation.blocking import run_blocking
from indus.protocol.groups import GROUPS_EVENT, GroupsEvent
from indus.state.rooms import LogPage, RoomError

__all__ = ["WATCH_POLL_S", "RoomWatch"]

log = logging.getLogger(__name__)

WATCH_POLL_S: Final = 0.5
_PAGES_PER_POLL: Final = 20


class _Notifiable(Protocol):
    def notify(self, method: str, params: Mapping[str, Any]) -> None: ...


type LogReader = Callable[[str, int], LogPage]


@dataclass
class _Watcher:
    owner: _Notifiable
    cursor: int


class RoomWatch:
    """See the module notes."""

    def __init__(self, read: LogReader, *, poll_s: float = WATCH_POLL_S) -> None:
        self._read = read
        self._poll_s = poll_s
        self._rooms: dict[str, dict[int, _Watcher]] = {}
        self._lock = asyncio.Lock()
        self._nudged = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def watching(self) -> bool:
        return any(self._rooms.values())

    def watch(self, owner: _Notifiable, room_id: str, cursor: int) -> None:
        """Send ``owner`` every event of ``room_id`` after ``cursor`` (again: a new cursor)."""
        self._rooms.setdefault(room_id, {})[id(owner)] = _Watcher(owner, cursor)
        self.start()
        self.nudge()

    def unwatch(self, owner: object, room_id: str) -> bool:
        watchers = self._rooms.get(room_id)
        if watchers is None or watchers.pop(id(owner), None) is None:
            return False
        if not watchers:
            self._rooms.pop(room_id, None)
        return True

    def drop(self, owner: object) -> None:
        """A closed connection: every watch it held ends."""
        for room_id in list(self._rooms):
            self.unwatch(owner, room_id)

    def nudge(self) -> None:
        """Poll soon (something was just written)."""
        self._nudged.set()

    async def poll(self) -> int:
        """Send every watcher the events past its cursor; how many notifications went out."""
        async with self._lock:
            sent = 0
            for room_id in list(self._rooms):
                sent += await self._poll_room(room_id)
            return sent

    async def _poll_room(self, room_id: str) -> int:
        watchers = self._rooms.get(room_id)
        if not watchers:
            return 0
        since = min(w.cursor for w in watchers.values())
        sent = 0
        for _ in range(_PAGES_PER_POLL):
            try:
                page = await run_blocking(
                    lambda s=since: self._read(room_id, s), label="room watch"
                )
            except RoomError as exc:  # gone or disbanded and purged: the watch ends
                log.debug("room watch %s ended: %s", room_id, exc)
                self._rooms.pop(room_id, None)
                return sent
            for event in page.events:
                frame = GroupsEvent(room_id=room_id, event=event.to_dict()).to_wire()
                for watcher in list(self._rooms.get(room_id, {}).values()):
                    # A watcher added during the read may be behind the page: the next poll.
                    if watcher.cursor < since or event.seq <= watcher.cursor:
                        continue
                    watcher.cursor = event.seq
                    try:
                        watcher.owner.notify(GROUPS_EVENT, frame)
                    except Exception:
                        log.debug("a room event was not sent to a watcher", exc_info=True)
                    sent += 1
            if not page.has_more or not page.events:
                return sent
            since = page.events[-1].seq
        return sent

    # -- running ---------------------------------------------------------------------------------

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._run())

    async def _run(self) -> None:
        while self.watching():
            self._nudged.clear()
            try:
                await self.poll()
            except Exception:
                log.warning("room watch: a poll failed; trying again", exc_info=True)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._nudged.wait(), self._poll_s)

    async def aclose(self) -> None:
        self._rooms.clear()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task

    def close(self) -> None:
        """Stop without waiting (from a synchronous close)."""
        self._rooms.clear()
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
