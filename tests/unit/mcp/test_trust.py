"""Trust tier, circuit breaker and background OAuth parking (P13-07)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx2
import pytest

from indus.mcp import trust
from indus.mcp.connect import McpConnectFailed
from indus.mcp.discovery import McpHost
from indus.mcp.lifecycle import RetryPolicy, ServerState
from indus.mcp.trust import BreakerState, CircuitBreaker
from indus.safety.approvals.decision import ApprovalState
from indus.tools.calls import ToolCall, ToolContext, ToolResult
from indus.tools.dispatch import dispatch
from indus.tools.registry import ToolRegistry
from tests.fakes.broker import FakeBroker, Reply
from tests.fakes.clock import FakeClock
from tests.fakes.mcp.scripted import FakeFleet, FakeServer, drive, run_for, text_result, tool
from tests.fakes.terminal import terminal_scope

STDIO: dict[str, Any] = {"command": "fake-server"}
TOOLS = [
    tool("look", read_only=True),
    tool("change"),
    tool("sneaky", read_only="yes"),
    tool("flagged_false", read_only=False),
]


def host_with(
    tmp_path: Path,
    clock: FakeClock,
    fleet: FakeFleet,
    servers: dict[str, dict[str, Any]],
    config: dict[str, Any] | None = None,
    **kwargs: Any,
) -> McpHost:
    scope = terminal_scope(tmp_path, {"mcp_servers": servers, **(config or {})}, clock=clock)
    return McpHost(
        scope,
        ToolRegistry(clock=clock),
        clock=clock,
        opener=fleet,
        policy=RetryPolicy(jitter=0.0),
        approvals=ApprovalState(),
        **kwargs,
    )


async def run_tool(host: McpHost, name: str, **extras: Any) -> ToolResult:
    ctx = ToolContext(scope=host.scope, session_id="s1", extras=extras)
    return await dispatch(ToolCall(name, {"x": "1"}), ctx, registry=host.registry)


def error_of(result: ToolResult) -> dict[str, Any]:
    assert isinstance(result.content, str)
    return json.loads(result.content)


# -- the trust gate -----------------------------------------------------------------------------


def test_read_only_is_exactly_true() -> None:
    assert trust.read_only(True)
    for value in ("yes", "true", 1, 1.0, None, False, {"x": True}):
        assert not trust.read_only(value)
    assert trust.needs_approval("untrusted", False)
    assert not trust.needs_approval("untrusted", True)
    assert not trust.needs_approval("full", False)


@pytest.mark.parametrize("tier", ["untrusted", "untrsted", "UNTRUSTED", "maybe"])
def test_untrusted_write_tool_asks_and_a_denial_sends_nothing(
    tmp_path: Path, fake_clock: FakeClock, tier: str
) -> None:
    fleet = FakeFleet(fake_clock, {"ext": FakeServer(tools=list(TOOLS))})
    host = host_with(tmp_path, fake_clock, fleet, {"ext": {**STDIO, "trust": tier}})
    broker = FakeBroker(fake_clock, default=Reply(choice="deny", reason="not now"))

    async def go() -> None:
        await drive(fake_clock, host.discover())
        for name in ("change", "sneaky", "flagged_false"):
            result = await run_tool(host, f"mcp__ext__{name}", broker=broker)
            assert result.is_error and error_of(result)["error_type"] == "not_approved"
            assert "not now" in error_of(result)["error"]
        looked = await run_tool(host, "mcp__ext__look", broker=broker)
        assert not looked.is_error
        await host.close()

    asyncio.run(go())
    assert fleet.servers["ext"].calls == [("look", {"x": "1"})]
    assert len(broker.requests) == 3
    shown = broker.requests[0]
    assert "untrusted server ext" in getattr(shown, "command", "")


def test_full_trust_never_asks(tmp_path: Path, fake_clock: FakeClock) -> None:
    fleet = FakeFleet(fake_clock, {"ext": FakeServer(tools=list(TOOLS))})
    host = host_with(tmp_path, fake_clock, fleet, {"ext": dict(STDIO)})
    broker = FakeBroker(fake_clock, default=Reply(choice="deny"))

    async def go() -> None:
        await drive(fake_clock, host.discover())
        assert not (await run_tool(host, "mcp__ext__change", broker=broker)).is_error
        await host.close()

    asyncio.run(go())
    assert broker.requests == []


def test_approval_once_and_for_the_session(tmp_path: Path, fake_clock: FakeClock) -> None:
    fleet = FakeFleet(fake_clock, {"ext": FakeServer(tools=list(TOOLS))})
    host = host_with(tmp_path, fake_clock, fleet, {"ext": {**STDIO, "trust": "untrusted"}})
    broker = FakeBroker(fake_clock, replies=[Reply(choice="once"), Reply(choice="session")])

    async def go() -> None:
        await drive(fake_clock, host.discover())
        for _ in range(4):
            assert not (await run_tool(host, "mcp__ext__change", broker=broker)).is_error
        await host.close()

    asyncio.run(go())
    assert len(broker.requests) == 2  # once, then session: the last two calls did not ask
    assert len(fleet.servers["ext"].calls) == 4
    offered = [str(c) for c in getattr(broker.requests[0], "choices", [])]
    assert offered == ["once", "session", "deny"]


@pytest.mark.parametrize(
    ("run_context", "config", "runs"),
    [
        ("cron", {}, False),
        ("cron", {"approvals": {"cron_mode": "approve"}}, True),
        ("unattended", {}, False),
        (None, {}, False),  # interactive with no broker: nobody to ask
        (None, {"approvals": {"mode": "off"}}, True),
    ],
)
def test_nobody_to_ask_follows_the_unattended_policy(
    tmp_path: Path,
    fake_clock: FakeClock,
    run_context: str | None,
    config: dict[str, Any],
    runs: bool,
) -> None:
    fleet = FakeFleet(fake_clock, {"ext": FakeServer(tools=list(TOOLS))})
    host = host_with(
        tmp_path, fake_clock, fleet, {"ext": {**STDIO, "trust": "untrusted"}}, config=config
    )
    broker = FakeBroker(fake_clock)
    extras: dict[str, Any] = {"run_context": run_context} if run_context else {}
    if run_context:
        extras["broker"] = broker

    async def go() -> None:
        await drive(fake_clock, host.discover())
        result = await run_tool(host, "mcp__ext__change", **extras)
        assert result.is_error is not runs
        if not runs:
            assert "nobody here can give it" in error_of(result)["error"]
        await host.close()

    asyncio.run(go())
    assert broker.requests == []


def test_hints_stay_frozen_until_the_next_listing(tmp_path: Path, fake_clock: FakeClock) -> None:
    server = FakeServer(tools=[tool("flip", read_only=True)])
    fleet = FakeFleet(fake_clock, {"ext": server})
    host = host_with(tmp_path, fake_clock, fleet, {"ext": {**STDIO, "trust": "untrusted"}})
    broker = FakeBroker(fake_clock, default=Reply(choice="deny"))

    async def go() -> None:
        await drive(fake_clock, host.discover())
        server.tools = [tool("flip")]  # the server changes its mind without telling us
        assert not (await run_tool(host, "mcp__ext__flip", broker=broker)).is_error
        await host.handles["ext"].refetch()
        assert (await run_tool(host, "mcp__ext__flip", broker=broker)).is_error
        await host.close()

    asyncio.run(go())
    assert len(broker.requests) == 1


# -- the breaker --------------------------------------------------------------------------------


def test_breaker_states(fake_clock: FakeClock) -> None:
    breaker = CircuitBreaker("s", fake_clock)
    for _ in range(2):
        assert breaker.check() is None
        breaker.record(False)
    assert breaker.state is BreakerState.CLOSED
    assert breaker.check() is None
    breaker.record(False)
    assert breaker.state is BreakerState.OPEN
    refusal = breaker.check()
    assert refusal is not None and "unreachable" in refusal and "60 s" in refusal
    fake_clock.advance(59)
    assert breaker.check() is not None
    fake_clock.advance(1)
    assert breaker.state is BreakerState.HALF_OPEN
    assert breaker.check() is None  # the probe
    assert breaker.check() is not None  # only one probe at a time
    breaker.record(False)
    assert breaker.state is BreakerState.OPEN and breaker.retry_in() == 60
    fake_clock.advance(60)
    assert breaker.check() is None
    breaker.record(None)  # the probe was cancelled: the slot comes back
    assert breaker.check() is None
    breaker.record(True)
    assert breaker.state is BreakerState.CLOSED and breaker.failures == 0


def test_breaker_says_rejected_when_the_server_answered(fake_clock: FakeClock) -> None:
    breaker = CircuitBreaker("s", fake_clock)
    for _ in range(3):
        breaker.check()
        breaker.record(False, application=True)
    refusal = breaker.check()
    assert refusal is not None and "rejected the last 3 calls" in refusal


def test_breaker_opens_after_three_failures_then_probes(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    outcomes: list[Any] = [ConnectionResetError("gone")] * 4 + [text_result("back")]

    def answer(name: str, args: dict[str, Any]) -> Any:
        step = outcomes.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step

    fleet = FakeFleet(fake_clock, {"s": FakeServer(answer=answer)})
    host = host_with(tmp_path, fake_clock, fleet, {"s": dict(STDIO)})

    async def go() -> None:
        await drive(fake_clock, host.discover())
        for _ in range(3):
            assert error_of(await run_tool(host, "mcp__s__read"))["error_type"] == "transport"
        short = await run_tool(host, "mcp__s__read")
        assert error_of(short)["error_type"] == "breaker_open"
        assert "retry later" in error_of(short)["error"]
        assert len(fleet.servers["s"].calls) == 3
        await run_for(fake_clock, 60)
        probe = await run_tool(host, "mcp__s__read")  # half-open probe fails: open again
        assert error_of(probe)["error_type"] == "transport"
        assert error_of(await run_tool(host, "mcp__s__read"))["error_type"] == "breaker_open"
        await run_for(fake_clock, 60)
        assert not (await run_tool(host, "mcp__s__read")).is_error
        assert host.handles["s"].breaker.state is BreakerState.CLOSED
        await host.close()

    asyncio.run(go())
    assert len(fleet.servers["s"].calls) == 5


def test_breakers_are_per_profile_and_server(fake_clock: FakeClock) -> None:
    book = trust.BreakerBook(fake_clock)
    assert book.get("p1", "s") is book.get("p1", "s")
    assert book.get("p1", "s") is not book.get("p2", "s")
    assert book.get("p1", "s") is not book.get("p1", "t")


# -- OAuth in the background --------------------------------------------------------------------


def http_status(code: int) -> httpx2.HTTPStatusError:
    request = httpx2.Request("POST", "https://mcp.example/mcp")
    return httpx2.HTTPStatusError(
        str(code), request=request, response=httpx2.Response(code, request=request)
    )


def test_auth_failures_are_recognised_through_wrappers() -> None:
    wrapped = McpConnectFailed("could not connect", cause=http_status(401), permanent=True)
    assert trust.is_auth_failure(wrapped)
    assert trust.is_auth_failure(ExceptionGroup("g", [http_status(403)]))
    assert not trust.is_auth_failure(http_status(500))
    assert not trust.is_auth_failure(ConnectionError("x"))


def test_interactive_auth_is_off_unless_a_login_turns_it_on() -> None:
    assert not trust.interactive_auth_allowed()
    with trust.allow_interactive_auth():
        assert trust.interactive_auth_allowed()
        with trust.background_auth():
            assert not trust.interactive_auth_allowed()
        assert trust.interactive_auth_allowed()
    assert not trust.interactive_auth_allowed()


class FakeOAuth:
    """Stands in for the P13-09 OAuth layer: on a 401 it would open a browser, but only where
    interactive auth is allowed."""

    def __init__(self) -> None:
        self.browser_opens: list[str] = []
        self.revision = 1

    def refused(self, server: str) -> None:
        if trust.interactive_auth_allowed():
            self.browser_opens.append(server)

    def stamp(self, server: str) -> object:
        return self.revision


def test_background_401_parks_without_a_browser_and_new_sessions_leave_it(
    tmp_path: Path, fake_clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    oauth = FakeOAuth()
    cloud = FakeServer(open_errors=[http_status(401) for _ in range(20)])
    fleet = FakeFleet(fake_clock, {"cloud": cloud})
    real_open = fleet._open  # pyright: ignore[reportPrivateUsage]

    def opener(name: str, server: Any, scope: Any, *, clock: Any, options: Any = None) -> Any:
        if cloud.open_errors:
            oauth.refused(name)
        return real_open(name, server, options)

    host = host_with(
        tmp_path,
        fake_clock,
        fleet,
        {"cloud": {"url": "https://mcp.example/mcp", "auth": "oauth"}},
        credential_stamp=oauth.stamp,
    )
    host.opener = opener

    async def go() -> None:
        with (
            trust.allow_interactive_auth()
        ):  # even a caller allowed to log in starts runners in the background
            report = await drive(fake_clock, host.discover())
        handle = host.handles["cloud"]
        assert handle.managed.state is ServerState.PARKED and host.auth.parked("cloud")
        assert "cloud" in report.failed
        assert "indus mcp login cloud" in handle.report().detail
        assert cloud.opens == 1
        # New sessions run discovery again: the parked server is left alone.
        for _ in range(3):
            await host.discover()
            await run_for(fake_clock, 10)
        assert cloud.opens == 1
        # Only the timed self-probe retries the token.
        await run_for(fake_clock, 300)
        assert cloud.opens == 2
        # The user signs in again: the stamp changes and the next discovery tries at once.
        oauth.revision = 2
        await host.discover()
        await run_for(fake_clock, 2)
        assert cloud.opens == 3
        await host.close()

    asyncio.run(go())
    assert oauth.browser_opens == []
    warnings = [r for r in caplog.records if "refused its credentials" in r.getMessage()]
    assert len(warnings) == 2  # one per park stamp, not one per probe


def test_reauthorised_wakes_a_parked_server(tmp_path: Path, fake_clock: FakeClock) -> None:
    server = FakeServer(open_errors=[http_status(401)])
    fleet = FakeFleet(fake_clock, {"cloud": server})
    host = host_with(tmp_path, fake_clock, fleet, {"cloud": {"url": "https://mcp.example/mcp"}})

    async def go() -> None:
        await drive(fake_clock, host.discover())
        assert host.auth.parked("cloud")
        host.reauthorised("cloud")
        await run_for(fake_clock, 2)
        assert host.handles["cloud"].managed.state is ServerState.READY
        assert not host.auth.parked("cloud")
        await host.close()

    asyncio.run(go())


def test_a_background_sign_in_refusal_parks_like_a_401(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    from indus.mcp.oauth import McpAuthRequired

    needed = McpAuthRequired("MCP server 'cloud' needs a sign-in", server="cloud")
    assert trust.is_auth_failure(needed)
    assert trust.is_auth_failure(McpConnectFailed("connect failed", cause=needed))
    assert trust.is_auth_failure(ExceptionGroup("tasks", [needed]))
    server = FakeServer(open_errors=[needed])
    fleet = FakeFleet(fake_clock, {"cloud": server})
    host = host_with(tmp_path, fake_clock, fleet, {"cloud": {"url": "https://mcp.example/mcp"}})

    async def go() -> None:
        report = await drive(fake_clock, host.discover())
        assert host.auth.parked("cloud") and "cloud" in report.failed
        assert host.handles["cloud"].managed.state is ServerState.PARKED
        await host.close()

    asyncio.run(go())


def test_reload_restarts_a_parked_server_with_fresh_extras(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    from indus.mcp.reload import reconcile

    server = FakeServer(open_errors=[http_status(401)])
    fleet = FakeFleet(fake_clock, {"cloud": server})
    host = host_with(tmp_path, fake_clock, fleet, {"cloud": {"url": "https://mcp.example/mcp"}})
    built: list[str] = []

    class Extras:
        def __init__(self, name: str, base: Any) -> None:
            built.append(name)
            self.options = base
            self.requesters = None

    host.extras = lambda name, config, base: Extras(name, base)

    async def go() -> None:
        await drive(fake_clock, host.discover())
        assert host.auth.parked("cloud") and built == ["cloud"]
        # The config watcher's reconcile leaves a parked server alone ...
        quiet = await drive(fake_clock, reconcile(host, reason="config_change"))
        assert quiet.kept == ["cloud"] and built == ["cloud"]
        # ... an explicit /reload-mcp restarts it with a new handler (new extras).
        loud = await drive(fake_clock, reconcile(host, reason="reload"))
        assert loud.restarted == ["cloud"] and built == ["cloud", "cloud"]
        await run_for(fake_clock, 2)
        assert host.handles["cloud"].managed.state is ServerState.READY
        await host.close()

    asyncio.run(go())
