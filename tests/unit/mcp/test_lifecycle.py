"""Reconnect ladder, cooldown, parking and stdio recycling (P13-04), on a fake clock."""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import mcp_types as types
import pytest
from mcp.shared.exceptions import MCPError

from indus.foundation.scope import Scope
from indus.mcp.config import McpConfigInvalid, McpServer
from indus.mcp.connect import ConnectOptions, LiveSession
from indus.mcp.lifecycle import (
    CooldownLedger,
    ManagedServer,
    McpServerUnavailable,
    RetryPolicy,
    ServerState,
    backoff_delays,
    jittered,
)
from tests.fakes.clock import FakeClock
from tests.fakes.terminal import terminal_scope

EXACT = RetryPolicy(jitter=0.0)
STDIO = McpServer(command="fake-server")
REMOTE = McpServer(url="https://mcp.example/mcp")


class FakeRaw:
    """The part of the SDK ``ClientSession`` a ``LiveSession`` uses."""

    server_capabilities = None
    server_info = None
    protocol_version = "2025-11-25"
    instructions = None

    def __init__(self) -> None:
        self.ping_error: BaseException | None = None
        self.ping_hangs = False
        self.call_gate: asyncio.Event | None = None
        self.calls: list[str] = []
        self.listings = 0

    async def list_tools(self) -> types.ListToolsResult:
        self.listings += 1
        return types.ListToolsResult(tools=[])

    async def send_ping(self) -> None:
        if self.ping_hangs:
            await asyncio.Event().wait()
        if self.ping_error is not None:
            raise self.ping_error

    async def call_tool(self, name: str, arguments: dict[str, Any] | None) -> types.CallToolResult:
        self.calls.append(name)
        if self.call_gate is not None:
            await self.call_gate.wait()
        return types.CallToolResult(content=[types.TextContent(type="text", text=f"{name} ok")])


class ScriptedOpener:
    """Each open takes the next entry: an exception is raised, ``"hang"`` never finishes,
    anything else yields a fresh session."""

    def __init__(self, clock: FakeClock, script: list[object] | None = None) -> None:
        self.clock = clock
        self.script = list(script or [])
        self.opened: list[float] = []
        self.closed: list[float] = []
        self.sessions: list[LiveSession] = []
        self.raws: list[FakeRaw] = []

    def __call__(
        self,
        name: str,
        server: McpServer,
        scope: Scope,
        *,
        clock: Any,
        options: ConnectOptions | None = None,
    ) -> Any:
        return self._open(name, server)

    @asynccontextmanager
    async def _open(self, name: str, server: McpServer) -> AsyncGenerator[LiveSession]:
        self.opened.append(self.clock.monotonic())
        step = self.script.pop(0) if self.script else "ok"
        if isinstance(step, BaseException):
            raise step
        if step == "hang":
            await asyncio.Event().wait()
        raw = FakeRaw()
        live = LiveSession(name, server.kind, cast(Any, raw), asyncio.Event())
        self.raws.append(raw)
        self.sessions.append(live)
        try:
            yield live
        finally:
            self.closed.append(self.clock.monotonic())

    def gaps(self) -> list[float]:
        return [round(b - a, 3) for a, b in zip(self.opened, self.opened[1:], strict=False)]


def managed(
    tmp_path: Path,
    clock: FakeClock,
    opener: ScriptedOpener,
    server: McpServer = STDIO,
    **kwargs: Any,
) -> ManagedServer:
    scope = terminal_scope(tmp_path, clock=clock)
    kwargs.setdefault("policy", EXACT)
    return ManagedServer("files", server, scope, clock=clock, opener=opener, **kwargs)


async def run_for(clock: FakeClock, seconds: float, step: float = 1.0) -> None:
    await clock.run_until_idle()
    moved = 0.0
    while moved < seconds:
        await clock.advance_and_settle(step)
        moved += step


def flaky() -> ConnectionError:
    return ConnectionError("connection refused")


# -- the arithmetic -----------------------------------------------------------------------------


@pytest.mark.parametrize("seed", range(20))
def test_backoff_delays_stay_in_the_jitter_band_and_under_the_cap(seed: int) -> None:
    policy = RetryPolicy()
    delays = backoff_delays(policy, random.Random(seed))
    bases = [1, 2, 4, 8, 16, 32, 60, 60, 60, 60]
    for base in bases:
        delay = next(delays)
        assert base * 0.8 <= delay <= min(base * 1.2, 60.0)


def test_jitter_never_goes_negative() -> None:
    assert jittered(0.0, 0.2, random.Random(1)) == 0.0
    assert all(0.8 <= jittered(1.0, 0.2, random.Random(s)) <= 1.2 for s in range(50))


def test_cooldown_doubles_from_30_to_600() -> None:
    ledger = CooldownLedger()
    assert [ledger.record_failure("a", 0.0) for _ in range(8)] == [
        30.0,
        60.0,
        120.0,
        240.0,
        480.0,
        600.0,
        600.0,
        600.0,
    ]
    assert ledger.remaining("a", 0.0) == 600.0
    assert ledger.remaining("a", 600.0) is None
    ledger.clear("a")
    assert ledger.failures("a") == 0 and ledger.remaining("a", 0.0) is None


# -- first connects -----------------------------------------------------------------------------


def test_first_connect_retries_three_times_then_cools_down(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    opener = ScriptedOpener(fake_clock, [flaky() for _ in range(40)])
    ledger = CooldownLedger()
    server = managed(tmp_path, fake_clock, opener, ledger=ledger)

    async def go() -> None:
        server.launch()
        await run_for(fake_clock, 7)
        assert len(opener.opened) == 4
        assert server.state is ServerState.COOLING_DOWN
        status = server.status()
        assert status.next_attempt_in == 30.0 and "refused" in (status.last_error or "")
        assert ledger.remaining("files", fake_clock.monotonic()) == 30.0
        with pytest.raises(McpServerUnavailable, match="cooling_down"):
            await server.session()
        await run_for(fake_clock, 2200, step=5.0)
        await server.close()

    asyncio.run(go())
    assert opener.gaps()[:10] == [1, 2, 4, 30, 60, 120, 240, 480, 600, 600]
    assert server.state is ServerState.CLOSED


def test_start_returns_the_session_and_a_success_clears_the_ledger(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    opener = ScriptedOpener(fake_clock, [flaky(), flaky()])
    ledger = CooldownLedger()
    ledger.record_failure("files", 0.0)
    server = managed(tmp_path, fake_clock, opener, ledger=ledger)

    async def go() -> None:
        starting = asyncio.ensure_future(server.start())
        await run_for(fake_clock, 3)
        live = await starting
        assert live is opener.sessions[0]
        assert server.state is ServerState.READY and server.live is live
        assert ledger.failures("files") == 0
        await server.close()

    asyncio.run(go())


def test_start_raises_when_the_first_connects_fail(tmp_path: Path, fake_clock: FakeClock) -> None:
    opener = ScriptedOpener(fake_clock, [flaky() for _ in range(5)])
    server = managed(tmp_path, fake_clock, opener)

    async def go() -> None:
        starting = asyncio.ensure_future(server.start())
        await run_for(fake_clock, 7)
        with pytest.raises(McpServerUnavailable, match="refused"):
            await starting
        await server.close()

    asyncio.run(go())


def test_permanent_error_parks_without_retries_and_probes_every_300_s(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    bad = McpConfigInvalid("MCP server 'files' has no command", server="files")
    opener = ScriptedOpener(fake_clock, [bad, bad, bad])
    server = managed(tmp_path, fake_clock, opener)

    async def go() -> None:
        server.launch()
        await run_for(fake_clock, 299, step=1.0)
        assert len(opener.opened) == 1 and server.state is ServerState.PARKED
        await run_for(fake_clock, 1)
        assert len(opener.opened) == 2
        await run_for(fake_clock, 300, step=10.0)
        assert opener.gaps() == [300, 300]
        server.request_reconnect()
        await run_for(fake_clock, 0)
        assert len(opener.opened) == 4 and server.state is ServerState.READY
        await server.close()

    asyncio.run(go())


# -- after a drop -------------------------------------------------------------------------------


def test_unproven_drops_spend_the_budget_then_park(tmp_path: Path, fake_clock: FakeClock) -> None:
    opener = ScriptedOpener(fake_clock, ["ok", *[flaky() for _ in range(10)]])
    server = managed(tmp_path, fake_clock, opener)

    async def go() -> None:
        await server.start()
        opener.sessions[0].dropped.set()
        await run_for(fake_clock, 31)
        assert server.state is ServerState.PARKED
        await run_for(fake_clock, 600, step=10.0)
        await server.close()

    asyncio.run(go())
    assert opener.gaps() == [1, 2, 4, 8, 16, 300, 300]


def test_a_proven_session_reconnects_at_once(tmp_path: Path, fake_clock: FakeClock) -> None:
    opener = ScriptedOpener(fake_clock)
    server = managed(tmp_path, fake_clock, opener)

    async def go() -> None:
        await server.start()
        result = await server.call_tool("echo", {"text": "x"})
        assert result.content[0].text == "echo ok"  # type: ignore[union-attr]
        opener.sessions[0].dropped.set()
        await run_for(fake_clock, 0)
        assert len(opener.opened) == 2 and server.state is ServerState.READY
        await server.close()

    asyncio.run(go())
    assert opener.gaps() == [0]


def test_respawn_wait_is_15_s_after_a_crash(tmp_path: Path, fake_clock: FakeClock) -> None:
    opener = ScriptedOpener(fake_clock, ["ok", "hang"])
    server = managed(tmp_path, fake_clock, opener)

    async def go() -> None:
        await server.start()
        opener.sessions[0].dropped.set()
        waiting = asyncio.ensure_future(server.call_tool("echo"))
        await run_for(fake_clock, 14)
        assert not waiting.done()
        await run_for(fake_clock, 1)
        with pytest.raises(McpServerUnavailable, match="15 s"):
            await waiting
        await server.close()

    asyncio.run(go())


def test_a_quick_respawn_serves_the_waiting_call(tmp_path: Path, fake_clock: FakeClock) -> None:
    opener = ScriptedOpener(fake_clock)
    server = managed(tmp_path, fake_clock, opener)

    async def go() -> None:
        await server.start()
        opener.sessions[0].dropped.set()
        waiting = asyncio.ensure_future(server.call_tool("echo"))
        await run_for(fake_clock, 2)
        result = await waiting
        assert result.content[0].text == "echo ok"  # type: ignore[union-attr]
        assert opener.raws[1].calls == ["echo"]
        await server.close()

    asyncio.run(go())


def test_reconnect_request_rebuilds_a_ready_session(tmp_path: Path, fake_clock: FakeClock) -> None:
    opener = ScriptedOpener(fake_clock)
    server = managed(tmp_path, fake_clock, opener)

    async def go() -> None:
        await server.start()
        server.request_reconnect()
        await run_for(fake_clock, 0)
        assert len(opener.opened) == 2 and len(opener.closed) == 1
        assert server.state is ServerState.READY
        await server.close()

    asyncio.run(go())


# -- keepalive ----------------------------------------------------------------------------------


def test_remote_keepalive_failure_reconnects(tmp_path: Path, fake_clock: FakeClock) -> None:
    opener = ScriptedOpener(fake_clock)
    server = managed(tmp_path, fake_clock, opener, REMOTE)

    async def go() -> None:
        await server.start()
        await run_for(fake_clock, 180)
        assert len(opener.opened) == 1, "a good ping keeps the session"
        opener.raws[0].ping_error = MCPError(-32000, "gone")
        await run_for(fake_clock, 180)
        assert len(opener.opened) == 2 and server.state is ServerState.READY
        await server.close()

    asyncio.run(go())


def test_a_server_without_ping_is_probed_with_a_tool_listing(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    opener = ScriptedOpener(fake_clock)
    server = managed(tmp_path, fake_clock, opener, REMOTE)

    async def go() -> None:
        await server.start()
        opener.raws[0].ping_error = MCPError(types.METHOD_NOT_FOUND, "no ping here")
        await run_for(fake_clock, 360)
        assert len(opener.opened) == 1 and server.state is ServerState.READY
        assert opener.raws[0].listings == 2
        await server.close()

    asyncio.run(go())


def test_a_hung_keepalive_gives_up_after_30_s(tmp_path: Path, fake_clock: FakeClock) -> None:
    opener = ScriptedOpener(fake_clock)
    server = managed(tmp_path, fake_clock, opener, REMOTE)

    async def go() -> None:
        await server.start()
        opener.raws[0].ping_hangs = True
        await run_for(fake_clock, 209)
        assert len(opener.opened) == 1
        await run_for(fake_clock, 1)
        assert opener.closed == [210.0], "the session ends when the ping runs out of time"
        assert server.state is ServerState.RECONNECTING
        assert "keepalive" in (server.status().last_error or "")
        await run_for(fake_clock, 1)
        assert len(opener.opened) == 2 and server.state is ServerState.READY
        await server.close()

    asyncio.run(go())


# -- stdio recycling ----------------------------------------------------------------------------


def test_idle_stdio_server_recycles_and_restarts_on_the_next_call(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    opener = ScriptedOpener(fake_clock)
    server = managed(tmp_path, fake_clock, opener, McpServer(command="x", idle_timeout_seconds=60))

    async def go() -> None:
        await server.start()
        await run_for(fake_clock, 59)
        assert server.state is ServerState.READY
        await run_for(fake_clock, 1)
        assert server.state is ServerState.RECYCLED and opener.closed == [60.0]
        await run_for(fake_clock, 1000, step=50.0)
        assert len(opener.opened) == 1, "a recycled server waits for a call"
        result = await server.call_tool("echo")
        assert result.content[0].text == "echo ok"  # type: ignore[union-attr]
        assert len(opener.opened) == 2 and server.state is ServerState.READY
        await server.close()

    asyncio.run(go())


def test_lifetime_limit_recycles_even_a_busy_looking_server(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    opener = ScriptedOpener(fake_clock)
    server = managed(tmp_path, fake_clock, opener, McpServer(command="x", max_lifetime_seconds=100))

    async def go() -> None:
        await server.start()
        for _ in range(9):
            await server.call_tool("echo")
            await run_for(fake_clock, 10)
        assert server.state is ServerState.READY
        await run_for(fake_clock, 10)
        assert server.state is ServerState.RECYCLED
        await server.close()

    asyncio.run(go())


def test_recycling_waits_for_the_call_in_flight(tmp_path: Path, fake_clock: FakeClock) -> None:
    opener = ScriptedOpener(fake_clock)
    server = managed(tmp_path, fake_clock, opener, McpServer(command="x", idle_timeout_seconds=5))

    async def go() -> None:
        await server.start()
        gate = asyncio.Event()
        opener.raws[0].call_gate = gate
        call = asyncio.ensure_future(server.call_tool("echo", timeout=1000))
        await run_for(fake_clock, 30)
        assert server.state is ServerState.READY and not call.done()
        gate.set()
        await run_for(fake_clock, 0)
        await call
        await run_for(fake_clock, 5)
        assert server.state is ServerState.RECYCLED
        await server.close()

    asyncio.run(go())


def test_remote_servers_never_recycle(tmp_path: Path, fake_clock: FakeClock) -> None:
    opener = ScriptedOpener(fake_clock)
    remote = McpServer(url="https://mcp.example/mcp", idle_timeout_seconds=5)
    server = managed(tmp_path, fake_clock, opener, remote)

    async def go() -> None:
        await server.start()
        await run_for(fake_clock, 100, step=5.0)
        assert server.state is ServerState.READY
        await server.close()

    asyncio.run(go())


# -- closing ------------------------------------------------------------------------------------


@pytest.mark.parametrize("script", [["ok"], [McpConfigInvalid("bad")], ["hang"]])
def test_close_ends_every_state_at_once(
    tmp_path: Path, fake_clock: FakeClock, script: list[object]
) -> None:
    opener = ScriptedOpener(fake_clock, list(script) + [flaky() for _ in range(5)])
    server = managed(tmp_path, fake_clock, opener)

    async def go() -> None:
        server.launch()
        await run_for(fake_clock, 2)
        await asyncio.wait_for(server.close(), timeout=5)
        assert server.state is ServerState.CLOSED
        with pytest.raises(McpServerUnavailable, match="closed"):
            await server.session()

    asyncio.run(go())


def test_close_during_cooldown(tmp_path: Path, fake_clock: FakeClock) -> None:
    opener = ScriptedOpener(fake_clock, [flaky() for _ in range(5)])
    server = managed(tmp_path, fake_clock, opener)

    async def go() -> None:
        server.launch()
        await run_for(fake_clock, 8)
        assert server.state is ServerState.COOLING_DOWN
        await asyncio.wait_for(server.close(), timeout=5)
        assert server.state is ServerState.CLOSED

    asyncio.run(go())


def test_state_listener_sees_each_transition(tmp_path: Path, fake_clock: FakeClock) -> None:
    opener = ScriptedOpener(fake_clock, [flaky(), "ok"])
    seen: list[str] = []
    server = managed(tmp_path, fake_clock, opener, on_state=lambda s: seen.append(s.state.value))

    async def go() -> None:
        server.launch()
        await run_for(fake_clock, 2)
        await server.close()

    asyncio.run(go())
    assert seen == ["connecting", "ready", "closed"]


def test_on_ready_failure_counts_as_a_failed_connect(tmp_path: Path, fake_clock: FakeClock) -> None:
    opener = ScriptedOpener(fake_clock)
    attempts: list[int] = []

    async def list_tools(live: LiveSession) -> None:
        attempts.append(1)
        if len(attempts) == 1:
            raise MCPError(-32603, "tools/list failed")

    server = managed(tmp_path, fake_clock, opener, on_ready=list_tools)

    async def go() -> None:
        server.launch()
        await run_for(fake_clock, 2)
        assert server.state is ServerState.READY
        assert len(opener.opened) == 2 and len(opener.closed) == 1
        await server.close()

    asyncio.run(go())
