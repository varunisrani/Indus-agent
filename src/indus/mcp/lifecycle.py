# pyright: strict
"""Keeping one MCP server usable: reconnects, cooldown, parking and stdio recycling (P13-04).

``ManagedServer`` runs one task per server for the life of the process; that task opens and
closes every transport of the server (P13-03 rule). States (``ServerState``)::

    connecting ──► ready ──► reconnecting ──► parked
        │            │  ▲         │             │ (probe every 300 s or on request)
        ▼            ▼  └─────────┘             ▼
    cooling_down   recycled (stdio idle/age; restarts on the next call)        closed

- First connects: one attempt plus up to 3 retries, backoff 1 s doubling to at most 60 s with
  ±20 % jitter. Still failing: ``cooling_down`` waits 30 s, then 60, 120, ... at most 600 s, one
  attempt per wait (``CooldownLedger``, shared with discovery).
- After a drop: up to 5 reconnects with the same backoff. A session that served a call, answered
  a keepalive or (stdio) lived a keepalive interval is proven and resets the budget; one that
  drops straight after the handshake does not, so a flapping server cannot respawn forever.
  Budget spent: ``parked``, probing itself every 300 s, at once on ``request_reconnect``.
- A permanent error (bad command, missing file, refused URL, rejected credentials) parks at once.
- Remote servers are pinged every 180 s (30 s per ping) while idle; a failed ping reconnects.
- Stdio servers recycle after ``idle_timeout_seconds`` without calls or ``max_lifetime_seconds``
  since the spawn (never during a call) and come back on the next call (≤ 15 s).
- A call that finds the server reconnecting (or its stdio child dead) waits up to 15 s for a new
  session, then fails with ``McpServerUnavailable``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterator, Mapping
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

import mcp_types as types
from mcp.shared.exceptions import MCPError

from indus.foundation.clock import Clock
from indus.foundation.scope import Scope
from indus.mcp.config import McpError, McpServer
from indus.mcp.connect import (
    ConnectOptions,
    LiveSession,
    clock_timeout,
    first_cause,
    is_permanent,
    open_session,
)

__all__ = [
    "CooldownLedger",
    "ManagedServer",
    "McpServerUnavailable",
    "Opener",
    "RetryPolicy",
    "ServerState",
    "ServerStatus",
    "backoff_delays",
    "jittered",
]

log = logging.getLogger(__name__)


class McpServerUnavailable(McpError):
    """The server has no live session to serve this call (and none came back in time)."""

    default_code = "mcp.unavailable"


class ServerState(StrEnum):
    CONNECTING = "connecting"
    READY = "ready"
    RECONNECTING = "reconnecting"
    COOLING_DOWN = "cooling_down"
    PARKED = "parked"
    RECYCLED = "recycled"
    CLOSED = "closed"


@dataclass(frozen=True)
class RetryPolicy:
    """Every timing of the ladder, in seconds (row P13-04)."""

    first_retries: int = 3
    reconnect_retries: int = 5
    backoff_start: float = 1.0
    backoff_cap: float = 60.0
    jitter: float = 0.2
    cooldown_start: float = 30.0
    cooldown_cap: float = 600.0
    park_probe: float = 300.0
    recycle_reconnect: float = 15.0
    respawn_wait: float = 15.0
    keepalive_interval: float = 180.0
    keepalive_timeout: float = 30.0


def jittered(seconds: float, fraction: float, rng: random.Random) -> float:
    """``seconds`` moved by a uniform ±``fraction``, never below zero."""
    return max(0.0, seconds * rng.uniform(1.0 - fraction, 1.0 + fraction))


def backoff_delays(policy: RetryPolicy, rng: random.Random) -> Iterator[float]:
    """1, 2, 4, ... capped at ``backoff_cap``, each jittered; endless."""
    base = policy.backoff_start
    while True:
        yield min(jittered(base, policy.jitter, rng), policy.backoff_cap)
        base = min(base * 2, policy.backoff_cap)


class CooldownLedger:
    """Consecutive failed connect cycles per server and when the next try may happen.

    The wait starts at ``cooldown_start`` and doubles per failure up to ``cooldown_cap``; a
    success clears the entry. Discovery reads it to skip a server that is still cooling down.
    """

    def __init__(self, policy: RetryPolicy | None = None) -> None:
        self.policy = policy or RetryPolicy()
        self._failures: dict[str, int] = {}
        self._retry_at: dict[str, float] = {}

    def delay_for(self, failures: int) -> float:
        steps = max(failures, 1) - 1
        return min(self.policy.cooldown_start * (2**steps), self.policy.cooldown_cap)

    def record_failure(self, name: str, now: float) -> float:
        count = self._failures.get(name, 0) + 1
        self._failures[name] = count
        delay = self.delay_for(count)
        self._retry_at[name] = now + delay
        return delay

    def clear(self, name: str) -> None:
        self._failures.pop(name, None)
        self._retry_at.pop(name, None)

    def failures(self, name: str) -> int:
        return self._failures.get(name, 0)

    def remaining(self, name: str, now: float) -> float | None:
        """Seconds until ``name`` may be tried again, or None when it may be tried now."""
        retry_at = self._retry_at.get(name)
        if retry_at is None or retry_at <= now:
            return None
        return retry_at - now


@dataclass(frozen=True)
class ServerStatus:
    name: str
    state: ServerState
    kind: str
    last_error: str | None = None
    failures: int = 0
    next_attempt_in: float | None = None
    calls_in_flight: int = 0


class Opener(Protocol):
    """``connect.open_session``'s shape (tests pass a scripted one)."""

    def __call__(
        self,
        name: str,
        server: McpServer,
        scope: Scope,
        *,
        clock: Clock,
        options: ConnectOptions | None = None,
    ) -> AbstractAsyncContextManager[LiveSession]: ...


_SETTLED = frozenset(
    {ServerState.READY, ServerState.COOLING_DOWN, ServerState.PARKED, ServerState.CLOSED}
)


@dataclass
class _Signals:
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    reconnect: asyncio.Event = field(default_factory=asyncio.Event)
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    settled: asyncio.Event = field(default_factory=asyncio.Event)
    idle: asyncio.Event = field(default_factory=asyncio.Event)


class ManagedServer:
    """One configured MCP server kept alive by its own runner task (see the module notes)."""

    def __init__(
        self,
        name: str,
        server: McpServer,
        scope: Scope,
        *,
        clock: Clock,
        options: ConnectOptions | None = None,
        policy: RetryPolicy | None = None,
        ledger: CooldownLedger | None = None,
        rng: random.Random | None = None,
        opener: Opener = open_session,
        on_ready: Callable[[LiveSession], Awaitable[None]] | None = None,
        on_state: Callable[[ServerStatus], None] | None = None,
    ) -> None:
        self.name = name
        self.server = server
        self.scope = scope
        self.clock = clock
        self.options = options or ConnectOptions()
        self.policy = policy or RetryPolicy()
        self.ledger = ledger or CooldownLedger(self.policy)
        self._rng = rng or random.Random()
        self._opener = opener
        self._on_ready = on_ready
        self._on_state = on_state
        self._signals = _Signals()
        self._signals.idle.set()
        self._state = ServerState.CONNECTING
        self._task: asyncio.Task[None] | None = None
        self._live: LiveSession | None = None
        self._last_error: str | None = None
        self._next_attempt_at: float | None = None
        self._ever_ready = False
        self._proven = False
        self._in_flight = 0
        self._last_activity = 0.0
        self._started_at = 0.0
        self._opening = False
        self._ping_unsupported = False

    # -- reading ----------------------------------------------------------------------------

    @property
    def state(self) -> ServerState:
        return self._state

    @property
    def live(self) -> LiveSession | None:
        """The current session when ready (None otherwise)."""
        return self._live if self._state is ServerState.READY else None

    def status(self) -> ServerStatus:
        wait = None
        if self._next_attempt_at is not None:
            wait = max(0.0, self._next_attempt_at - self.clock.monotonic())
        failures = self.ledger.failures(self.name)
        return ServerStatus(
            name=self.name,
            state=self._state,
            kind=self.server.kind,
            last_error=self._last_error,
            failures=failures,
            next_attempt_in=wait,
            calls_in_flight=self._in_flight,
        )

    # -- control ----------------------------------------------------------------------------

    def _set_state(self, state: ServerState, *, next_attempt_in: float | None = None) -> None:
        self._state = state
        self._next_attempt_at = (
            self.clock.monotonic() + next_attempt_in if next_attempt_in is not None else None
        )
        if state is ServerState.READY:
            self._signals.ready.set()
        else:
            self._signals.ready.clear()
        if state in _SETTLED:
            self._signals.settled.set()
        log.debug("MCP server %r: %s", self.name, state.value)
        if self._on_state is not None:
            try:
                self._on_state(self.status())
            except Exception:
                log.exception("MCP server %r: state listener failed", self.name)

    def launch(self) -> None:
        """Start the runner task (idempotent)."""
        if self._task is None:
            self._task = asyncio.ensure_future(self._run())

    async def start(self) -> LiveSession:
        """Start and wait until the first connect settles: the session when ready, else the
        error that left the server cooling down or parked."""
        self.launch()
        await self._signals.settled.wait()
        if self._state is ServerState.READY and self._live is not None:
            return self._live
        raise McpServerUnavailable(
            self._last_error or f"MCP server {self.name!r} is {self._state.value}", server=self.name
        )

    def request_reconnect(self) -> None:
        """Rebuild the transport now: from ready (no budget charge) or out of a park/cooldown."""
        self._signals.reconnect.set()
        self._signals.wake.set()

    async def close(self) -> None:
        """Stop the runner; a connect still in progress is abandoned (cancelled in its task)."""
        self._signals.stop.set()
        self._signals.wake.set()
        task = self._task
        if task is not None and not task.done():
            if self._opening:
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._state is not ServerState.CLOSED:
            self._set_state(ServerState.CLOSED)

    # -- calls ------------------------------------------------------------------------------

    async def session(self) -> LiveSession:
        """A ready session, waiting (bounded) for a respawn or a recycled restart."""
        state = self._state
        if state is ServerState.CLOSED or self._signals.stop.is_set():
            raise McpServerUnavailable(f"MCP server {self.name!r} is closed", server=self.name)
        live = self._live
        if state is ServerState.READY and live is not None and not live.dropped.is_set():
            return live
        if state in (ServerState.COOLING_DOWN, ServerState.PARKED):
            reason = f": {self._last_error}" if self._last_error else ""
            raise McpServerUnavailable(
                f"MCP server {self.name!r} is {state.value}{reason}", server=self.name
            )
        if state is ServerState.RECYCLED:
            wait = self.policy.recycle_reconnect
            self._signals.wake.set()
        else:
            wait = self.policy.respawn_wait
        if live is not None and live.dropped.is_set():
            self._signals.ready.clear()
        try:
            async with clock_timeout(self.clock, wait):
                while True:
                    await self._signals.ready.wait()
                    live = self._live
                    if live is not None and not live.dropped.is_set():
                        return live
                    self._signals.ready.clear()
        except TimeoutError:
            raise McpServerUnavailable(
                f"MCP server {self.name!r} did not come back within {wait:g} s"
                + (f" ({self._last_error})" if self._last_error else ""),
                server=self.name,
            ) from None

    @asynccontextmanager
    async def use(self) -> AsyncGenerator[LiveSession]:
        """A ready session for one request; counts as activity and holds off recycling."""
        live = await self.session()
        self._in_flight += 1
        self._signals.idle.clear()
        ok = False
        try:
            yield live
            ok = True
        finally:
            self._in_flight -= 1
            self._last_activity = self.clock.monotonic()
            if self._in_flight == 0:
                self._signals.idle.set()
            if ok:
                self._proven = True

    async def call_tool(
        self, tool: str, arguments: Mapping[str, Any] | None = None, *, timeout: float | None = None
    ) -> types.CallToolResult:
        """``tools/call`` with the server's own tool name, bounded by the server's call timeout."""
        async with self.use() as live, clock_timeout(self.clock, timeout or self.server.timeout):
            return await live.call_tool(tool, arguments)

    # -- the runner -------------------------------------------------------------------------

    async def _pause(self, seconds: float | None) -> bool:
        """Wait ``seconds`` (None = until woken); True when a stop or wake ended it early."""
        stop, wake = self._signals.stop, self._signals.wake
        waiters: list[asyncio.Future[Any]] = [
            asyncio.ensure_future(stop.wait()),
            asyncio.ensure_future(wake.wait()),
        ]
        if seconds is not None:
            waiters.append(asyncio.ensure_future(self.clock.sleep(seconds)))
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()
        woken = stop.is_set() or wake.is_set()
        wake.clear()
        return woken

    async def _run(self) -> None:
        delays = backoff_delays(self.policy, self._rng)
        first_failures = 0
        budget = 0
        try:
            while not self._signals.stop.is_set():
                if self._state not in (ServerState.CONNECTING, ServerState.RECONNECTING):
                    self._set_state(
                        ServerState.RECONNECTING if self._ever_ready else ServerState.CONNECTING
                    )
                self._signals.reconnect.clear()
                outcome: str
                try:
                    outcome = await self._connect_and_serve()
                except Exception as exc:
                    if self._signals.stop.is_set():
                        break
                    root = first_cause(exc)
                    self._last_error = str(root) or type(root).__name__
                    if is_permanent(exc):
                        log.warning("MCP server %r: %s; parked", self.name, self._last_error)
                        await self._park()
                        continue
                    if not self._ever_ready:
                        first_failures += 1
                        if first_failures <= self.policy.first_retries:
                            delay = next(delays)
                            self._set_state(ServerState.CONNECTING, next_attempt_in=delay)
                            await self._pause(delay)
                            continue
                        delay = self.ledger.record_failure(self.name, self.clock.monotonic())
                        log.warning(
                            "MCP server %r: could not connect (%s); next try in %.0f s",
                            self.name,
                            self._last_error,
                            delay,
                        )
                        self._set_state(ServerState.COOLING_DOWN, next_attempt_in=delay)
                        await self._pause(delay)
                        continue
                    budget += 1
                    if budget <= self.policy.reconnect_retries:
                        delay = next(delays)
                        self._set_state(ServerState.RECONNECTING, next_attempt_in=delay)
                        await self._pause(delay)
                        continue
                    log.warning(
                        "MCP server %r: %d reconnects failed (%s); parked",
                        self.name,
                        self.policy.reconnect_retries,
                        self._last_error,
                    )
                    await self._park()
                    continue
                # A session was open and has ended.
                first_failures = 0
                delays = backoff_delays(self.policy, self._rng)
                if outcome == "stop":
                    break
                if outcome == "recycle":
                    self._set_state(ServerState.RECYCLED)
                    if await self._pause(None) and self._signals.stop.is_set():
                        break
                    budget = 0
                    continue
                if outcome == "reconnect":
                    continue
                # dropped (or a failed keepalive)
                if self._proven:
                    budget = 0
                else:
                    budget += 1
                if budget > self.policy.reconnect_retries:
                    log.warning(
                        "MCP server %r keeps dropping right after connecting; parked", self.name
                    )
                    await self._park()
                    continue
                if budget:
                    delay = next(delays)
                    self._set_state(ServerState.RECONNECTING, next_attempt_in=delay)
                    await self._pause(delay)
                else:
                    self._set_state(ServerState.RECONNECTING)
        finally:
            self._live = None
            self._set_state(ServerState.CLOSED)

    async def _park(self) -> None:
        self._set_state(ServerState.PARKED, next_attempt_in=self.policy.park_probe)
        await self._pause(self.policy.park_probe)
        self._signals.reconnect.clear()

    async def _connect_and_serve(self) -> str:
        """Open, publish and hold one session in this task; the outcome says why it ended.
        Errors before the session is published are connect failures; later ones (a transport
        that fails while closing) only end the session."""
        outcome = "dropped"
        published = False
        try:
            async with AsyncExitStack() as stack:
                self._opening = True
                try:
                    live = await stack.enter_async_context(
                        self._opener(
                            self.name,
                            self.server,
                            self.scope,
                            clock=self.clock,
                            options=self.options,
                        )
                    )
                    if self._on_ready is not None:
                        await self._on_ready(live)
                finally:
                    self._opening = False
                self._publish(live)
                published = True
                try:
                    outcome = await self._serve(live)
                finally:
                    self._live = None
                    self._signals.ready.clear()
        except Exception as exc:
            if not published:
                raise
            log.debug("MCP server %r: session ended with %s", self.name, first_cause(exc))
        return outcome

    def _publish(self, live: LiveSession) -> None:
        now = self.clock.monotonic()
        self._live = live
        self._ever_ready = True
        self._proven = False
        self._started_at = now
        self._last_activity = now
        self._last_error = None
        self._ping_unsupported = False
        self.ledger.clear(self.name)
        self._signals.wake.clear()
        self._set_state(ServerState.READY)

    def _recycle_due(self, now: float) -> float | None:
        """Monotonic time the stdio session should be recycled, or None (no limits)."""
        if self.server.kind != "stdio":
            return None
        due: list[float] = []
        if self.server.idle_timeout_seconds > 0:
            due.append(self._last_activity + self.server.idle_timeout_seconds)
        if self.server.max_lifetime_seconds > 0:
            due.append(self._started_at + self.server.max_lifetime_seconds)
        return min(due) if due else None

    async def _serve(self, live: LiveSession) -> str:
        """Hold the session until something ends it: ``stop``, ``reconnect``, ``recycle`` or
        ``dropped``."""
        signals = self._signals
        remote = self.server.kind != "stdio"
        next_check = self._started_at + self.policy.keepalive_interval
        while True:
            now = self.clock.monotonic()
            recycle_at = self._recycle_due(now)
            if recycle_at is not None and now >= recycle_at and self._in_flight == 0:
                log.info("MCP server %r: recycling the idle stdio server", self.name)
                return "recycle"
            if now >= next_check:
                next_check = now + self.policy.keepalive_interval
                if remote and self._in_flight == 0:
                    if not await self._keepalive(live):
                        return "dropped"
                elif not remote and not live.dropped.is_set():
                    self._proven = True
            deadline = next_check
            if recycle_at is not None and (self._in_flight == 0 or recycle_at > now):
                deadline = min(deadline, recycle_at)
            waiters: list[asyncio.Future[Any]] = [
                asyncio.ensure_future(signals.stop.wait()),
                asyncio.ensure_future(signals.reconnect.wait()),
                asyncio.ensure_future(live.dropped.wait()),
                asyncio.ensure_future(self.clock.sleep(max(0.0, deadline - now))),
            ]
            if recycle_at is not None and self._in_flight > 0:
                waiters.append(asyncio.ensure_future(signals.idle.wait()))
            try:
                await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for waiter in waiters:
                    waiter.cancel()
            if signals.stop.is_set():
                return "stop"
            if signals.reconnect.is_set():
                signals.reconnect.clear()
                return "reconnect"
            if live.dropped.is_set():
                log.info("MCP server %r: connection lost", self.name)
                return "dropped"

    async def _keepalive(self, live: LiveSession) -> bool:
        """One liveness probe: ``ping``, or ``tools/list`` for a server without ``ping``."""
        try:
            async with clock_timeout(self.clock, self.policy.keepalive_timeout):
                if self._ping_unsupported:
                    await live.raw.list_tools()
                else:
                    try:
                        await live.ping()
                    except MCPError as exc:
                        if exc.code != types.METHOD_NOT_FOUND:
                            raise
                        self._ping_unsupported = True
                        await live.raw.list_tools()
        except Exception as exc:
            root = first_cause(exc)
            self._last_error = f"keepalive failed: {str(root) or type(root).__name__}"
            log.warning("MCP server %r: %s; reconnecting", self.name, self._last_error)
            return False
        self._proven = True
        return True
