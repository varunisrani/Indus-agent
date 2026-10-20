"""Keep-alive PTYs for the Chat tab (P18-08, R09.6).

A PTY started with an ``attach`` token outlives its WebSocket. Each ``PtySession`` has one drain
task that reads the child into a ring buffer (the last 1 MiB) and forwards every chunk to the
socket attached right now, if any. ``attach(viewer, redraw=)`` makes a socket the viewer: the one
before it is closed 4409 (superseded), the ring is replayed, and — for a PTY that already existed
— one Ctrl-L byte asks the TUI for a full repaint (a byte tail of an alternate-screen program is
not a picture). When the child exits the attached socket is closed 4410. ``detach`` only unhooks
the socket; input from any socket that is not the viewer is dropped, and input the child does not
take in time marks the PTY dead (the route closes that socket 1013).

``PtyRegistry`` maps ``PtyKey(token, profile, resume)`` to sessions, one PTY per key (the
get-or-spawn step holds one lock). It holds at most ``max_sessions`` (16): a new PTY first
evicts the one detached longest, and ``RegistryFull`` is raised when every PTY has a viewer.
``reap`` ends PTYs whose child is gone and those detached for ``ttl`` (30 min); ``run_reaper``
calls it every minute. ``close_others`` ends the other PTYs of one raw attach token (the tab
switched profile or resume target); ``close_orphans`` ends detached PTYs still holding the
conversation being resumed — same profile and resume id, or whose process tree contains the
session's turn-lease holder. Ending a PTY ends the TUI only: since P17 the TUI is a viewer of the
daemon, which owns and keeps any running turn.

Times come from the injected ``Clock``; closes that may wait out a child's grace run outside the
registry lock (dead remnants and evictions in the background).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from indus.foundation.clock import Clock
from indus.web.pty.bridge import PtyBridge
from indus.web.pty.resize import TermSize

__all__ = [
    "CLOSE_EXITED",
    "CLOSE_STALLED",
    "CLOSE_SUPERSEDED",
    "MAX_SESSIONS",
    "REAP_INTERVAL_S",
    "REDRAW",
    "RING_BYTES",
    "TTL_S",
    "PtyKey",
    "PtyRegistry",
    "PtySession",
    "RegistryFull",
    "RingBuffer",
    "Viewer",
    "process_ancestors",
]

log = logging.getLogger(__name__)

CLOSE_SUPERSEDED: Final = 4409
CLOSE_EXITED: Final = 4410
CLOSE_STALLED: Final = 1013
REDRAW: Final = b"\x0c"
RING_BYTES: Final = 1024 * 1024
TTL_S: Final = 30 * 60.0
MAX_SESSIONS: Final = 16
REAP_INTERVAL_S: Final = 60.0
_MAX_DEPTH: Final = 128

FULL_TEXT: Final = (
    "Too many chat terminals are open in other tabs; close one of them and try again."
)


class Viewer(Protocol):
    """The socket a PTY's output goes to (a Starlette ``WebSocket`` fits)."""

    async def send_bytes(self, data: bytes) -> None: ...

    async def close(self, code: int = 1000, reason: str | None = None) -> None: ...


async def close_quietly(viewer: Viewer | None, code: int, reason: str | None = None) -> None:
    """Close ``viewer`` with ``code``; a socket that is already gone is fine."""
    if viewer is None:
        return
    with contextlib.suppress(Exception):
        await viewer.close(code, reason)


@dataclass(frozen=True, slots=True)
class PtyKey:
    """What a keep-alive PTY is found by: the tab's attach token, the profile, and the session the
    tab asked to resume (empty for none)."""

    token: str
    profile: str = ""
    resume: str = ""

    def __repr__(self) -> str:  # the token is the tab's own handle: keep it out of logs
        return f"PtyKey(profile={self.profile!r}, resume={self.resume!r})"


class RegistryFull(Exception):
    """Every keep-alive slot holds a PTY that some tab is attached to."""

    def __init__(self, message: str = FULL_TEXT) -> None:
        super().__init__(message)


class RingBuffer:
    """The most recent ``capacity`` bytes appended."""

    def __init__(self, capacity: int = RING_BYTES) -> None:
        if capacity < 1:
            raise ValueError("ring capacity must be positive")
        self.capacity = capacity
        self._data = bytearray()
        self.truncated = False

    def append(self, chunk: bytes) -> None:
        self._data.extend(chunk)
        extra = len(self._data) - self.capacity
        if extra > 0:
            del self._data[:extra]
            self.truncated = True

    def snapshot(self) -> bytes:
        return bytes(self._data)

    def __len__(self) -> int:
        return len(self._data)


def process_ancestors(pid: int) -> set[int]:
    """The live ancestors of ``pid`` (Linux ``/proc``; empty elsewhere or when unreadable)."""
    found: set[int] = set()
    current = pid
    for _ in range(_MAX_DEPTH):
        try:
            raw = Path(f"/proc/{current}/stat").read_bytes()
        except OSError:
            break
        try:
            parent = int(raw.rsplit(b")", 1)[1].split()[1])
        except (IndexError, ValueError):
            break
        if parent <= 0 or parent in found:
            break
        found.add(parent)
        current = parent
    return found


class PtySession:
    """One keep-alive PTY (see the module notes)."""

    def __init__(
        self,
        key: PtyKey,
        bridge: PtyBridge,
        *,
        clock: Clock,
        ring_bytes: int = RING_BYTES,
        ancestors: Callable[[int], set[int]] = process_ancestors,
    ) -> None:
        self.key = key
        self.bridge = bridge
        self.ring = RingBuffer(ring_bytes)
        self.alive = True
        self.detached_at: float | None = None
        self.on_close: list[Callable[[], object]] = []
        self._clock = clock
        self._ancestors = ancestors
        self._viewer: Viewer | None = None
        self._generation = 0
        self._drain_task: asyncio.Task[None] | None = None
        self._out = asyncio.Lock()
        self._in = asyncio.Lock()
        self._closed = False

    @property
    def viewer(self) -> Viewer | None:
        return self._viewer

    @property
    def attached(self) -> bool:
        return self._viewer is not None

    def start(self) -> None:
        self._drain_task = asyncio.get_running_loop().create_task(
            self._drain(), name="indus-pty-drain"
        )

    async def _drain(self) -> None:
        while True:
            try:
                chunk = await self.bridge.read()
            except Exception as exc:
                log.warning("pty read failed: %s", type(exc).__name__)
                chunk = None
            if chunk is None:
                self.alive = False
                await close_quietly(self._viewer, CLOSE_EXITED, "the chat process exited")
                return
            async with self._out:
                self.ring.append(chunk)
                viewer = self._viewer
                if viewer is None:
                    continue
                try:
                    await viewer.send_bytes(chunk)
                except Exception:
                    # The viewer went away; a replacement that attached meanwhile keeps its place.
                    self.detach(viewer)

    async def attach(self, viewer: Viewer, *, redraw: bool) -> bool:
        """Make ``viewer`` the socket of this PTY and replay the ring. False when the viewer went
        away during the replay (it is detached again) or the redraw byte could not be written."""
        old = self._viewer
        if old is not None and old is not viewer:
            await close_quietly(old, CLOSE_SUPERSEDED, "opened in another tab")
        async with self._out:
            self._viewer = viewer
            self._generation += 1
            self.detached_at = None
            snapshot = self.ring.snapshot()
            if snapshot:
                try:
                    await viewer.send_bytes(snapshot)
                except Exception:
                    self.detach(viewer)
                    return False
        if redraw:
            return await self.write(viewer, REDRAW)
        return True

    def detach(self, viewer: Viewer) -> None:
        """Unhook ``viewer`` when it is still the attached socket (a superseded socket's late
        detach changes nothing)."""
        if self._viewer is not viewer:
            return
        self._viewer = None
        self.detached_at = self._clock.monotonic()

    async def write(self, viewer: Viewer, data: bytes) -> bool:
        """Write ``viewer``'s input. Input from a socket that is not the viewer is dropped (True).
        False when the child did not take it; the PTY is then dead unless another socket attached
        while the write waited."""
        async with self._in:
            if self._viewer is not viewer:
                return True
            generation = self._generation
            delivered = await self.bridge.write(data)
            if not delivered and self._viewer is viewer and self._generation == generation:
                self.alive = False
            return delivered

    def resize(self, viewer: Viewer, size: TermSize) -> None:
        if self._viewer is viewer:
            self.bridge.resize(size)

    def hosts_pid(self, pid: int | None) -> bool:
        """Whether ``pid`` runs inside this PTY: the leader or one of its descendants."""
        leader: object = getattr(self.bridge, "pid", None)
        if pid is None or not isinstance(leader, int):
            return False
        return pid == leader or leader in self._ancestors(pid)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.alive = False
        task = self._drain_task
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        try:
            await self.bridge.close()
        except Exception as exc:
            log.warning("pty close failed: %s", type(exc).__name__)
        callbacks, self.on_close = self.on_close, []
        for callback in callbacks:
            try:
                callback()
            except Exception as exc:
                log.warning("pty close callback failed: %s", type(exc).__name__)


type Spawn = Callable[[], Awaitable[PtyBridge]]


class PtyRegistry:
    """Keep-alive PTYs by key (see the module notes)."""

    def __init__(
        self,
        *,
        clock: Clock,
        ttl: float = TTL_S,
        max_sessions: int = MAX_SESSIONS,
        ring_bytes: int = RING_BYTES,
        ancestors: Callable[[int], set[int]] = process_ancestors,
    ) -> None:
        if max_sessions < 1:
            raise ValueError("the registry needs at least one slot")
        self._clock = clock
        self.ttl = ttl
        self.max_sessions = max_sessions
        self._ring_bytes = ring_bytes
        self._ancestors = ancestors
        self._sessions: dict[PtyKey, PtySession] = {}
        self._lock = asyncio.Lock()
        self._background: set[asyncio.Task[None]] = set()

    def __len__(self) -> int:
        return len(self._sessions)

    def get(self, key: PtyKey) -> PtySession | None:
        return self._sessions.get(key)

    def sessions(self) -> list[PtySession]:
        return list(self._sessions.values())

    async def attach_or_spawn(
        self, key: PtyKey, spawn: Spawn, *, on_close: Callable[[], object] | None = None
    ) -> tuple[PtySession, bool]:
        """The live PTY of ``key``, or a new one from ``spawn`` (``created`` True)."""
        await self.reap()
        async with self._lock:
            found = self._sessions.get(key)
            if found is not None and found.alive:
                return found, False
            if found is not None:
                del self._sessions[key]
                self._close_later(found)
            if len(self._sessions) >= self.max_sessions:
                self._evict_one()
            bridge = await spawn()
            session = PtySession(
                key,
                bridge,
                clock=self._clock,
                ring_bytes=self._ring_bytes,
                ancestors=self._ancestors,
            )
            if on_close is not None:
                session.on_close.append(on_close)
            session.start()
            self._sessions[key] = session
            return session, True

    def detach(self, key: PtyKey, viewer: Viewer) -> None:
        session = self._sessions.get(key)
        if session is not None:
            session.detach(viewer)

    async def close_others(self, token: str, *, keep: PtyKey) -> None:
        """End the PTYs of attach token ``token`` other than ``keep``; a tab still viewing one is
        told it was superseded."""
        async with self._lock:
            keys = [k for k in self._sessions if k.token == token and k != keep]
            doomed = [self._sessions.pop(k) for k in keys]
        for session in doomed:
            await close_quietly(session.viewer, CLOSE_SUPERSEDED, "opened in another tab")
            await session.close()

    async def close_orphans(self, keep: PtyKey, *, holder_pid: int | None = None) -> None:
        """End detached PTYs that hold the conversation ``keep`` resumes (see the module notes)."""
        if not keep.resume:
            return
        async with self._lock:
            keys = [
                k
                for k, session in self._sessions.items()
                if k != keep
                and not session.attached
                and (
                    (k.profile == keep.profile and k.resume == keep.resume)
                    or session.hosts_pid(holder_pid)
                )
            ]
            doomed = [self._sessions.pop(k) for k in keys]
        for session in doomed:
            await session.close()

    def _expired(self, session: PtySession, now: float) -> bool:
        if not session.alive:
            return True
        try:
            if not session.bridge.is_alive():
                return True
        except Exception:
            return True
        return (
            not session.attached
            and session.detached_at is not None
            and now - session.detached_at >= self.ttl
        )

    async def reap(self, now: float | None = None) -> int:
        """End dead PTYs and those detached for ``ttl``; the number ended."""
        moment = self._clock.monotonic() if now is None else now
        keys = [k for k, s in self._sessions.items() if self._expired(s, moment)]
        ended = 0
        for key in keys:
            session = self._sessions.pop(key, None)  # a concurrent reap may have taken it
            if session is not None:
                ended += 1
                await session.close()
        return ended

    async def run_reaper(self, interval: float = REAP_INTERVAL_S) -> None:
        """Reap every ``interval`` seconds until cancelled."""
        while True:
            await self._clock.sleep(interval)
            try:
                await self.reap()
            except Exception as exc:
                log.warning("pty reaper: %s", type(exc).__name__)

    def _evict_one(self) -> None:
        idle = [s for s in self._sessions.values() if not s.attached]
        if not idle:
            raise RegistryFull()
        oldest = min(idle, key=lambda s: s.detached_at if s.detached_at is not None else 0.0)
        del self._sessions[oldest.key]
        self._close_later(oldest)

    def _close_later(self, session: PtySession) -> None:
        task = asyncio.get_running_loop().create_task(session.close(), name="indus-pty-close")
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def close_all(self) -> None:
        """End every PTY (backend stop), concurrently."""
        doomed = list(self._sessions.values())
        self._sessions.clear()
        pending = [*(s.close() for s in doomed), *list(self._background)]
        await asyncio.gather(*pending, return_exceptions=True)
