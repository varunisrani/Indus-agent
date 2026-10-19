"""Discovery, registration, schema cache and lazy servers (P13-05), on a fake clock."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import mcp_types as types
import pytest

from indus.mcp.config import McpConfigInvalid, McpServer
from indus.mcp.discovery import McpHost, ServerHandle, drop_host, host_for, register_host
from indus.mcp.lifecycle import RetryPolicy
from indus.mcp.schema_cache import CachedTool, SchemaCache, fingerprint
from indus.tools import toolsets
from indus.tools.builtin import load_builtins
from indus.tools.calls import ToolCall, ToolContext
from indus.tools.dispatch import dispatch
from indus.tools.registry import ToolRegistry
from tests.fakes.clock import FakeClock
from tests.fakes.mcp.scripted import FakeFleet, FakeServer, drive, run_for, tool
from tests.fakes.terminal import terminal_scope

STDIO: dict[str, Any] = {"command": "fake-server"}


def make_host(
    tmp_path: Path,
    clock: FakeClock,
    fleet: FakeFleet,
    servers: dict[str, dict[str, Any]],
    *,
    mcp: dict[str, Any] | None = None,
    registry: ToolRegistry | None = None,
    name: str = "home",
) -> McpHost:
    config: dict[str, Any] = {"mcp_servers": servers}
    if mcp is not None:
        config["mcp"] = mcp
    scope = terminal_scope(tmp_path, config, clock=clock, name=name)
    return McpHost(
        scope,
        registry or ToolRegistry(clock=clock),
        clock=clock,
        opener=fleet,
        policy=RetryPolicy(jitter=0.0),
    )


async def call(host: McpHost, name: str, args: dict[str, Any] | None = None) -> Any:
    return await dispatch(
        ToolCall(name, args or {}), ToolContext(scope=host.scope), registry=host.registry
    )


# -- concurrency and budgets --------------------------------------------------------------------


def test_never_more_than_four_servers_connect_at_once(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    names = [f"s{i}" for i in range(10)]
    fleet = FakeFleet(fake_clock, {n: FakeServer(open_delay=5.0) for n in names})
    host = make_host(tmp_path, fake_clock, fleet, {n: dict(STDIO) for n in names})

    async def go() -> None:
        report = await drive(fake_clock, host.discover())
        assert sorted(report.connected) == sorted(names)
        assert report.tools == 10 and not report.failed
        assert "10 tool(s) from 10 server(s)" in report.summary()
        await host.close()

    asyncio.run(go())
    assert fleet.max_opening == 4
    assert all(s.opens == 1 for s in fleet.servers.values())


def test_concurrency_zero_means_all_at_once(tmp_path: Path, fake_clock: FakeClock) -> None:
    names = [f"s{i}" for i in range(6)]
    fleet = FakeFleet(fake_clock, {n: FakeServer(open_delay=5.0) for n in names})
    host = make_host(
        tmp_path,
        fake_clock,
        fleet,
        {n: dict(STDIO) for n in names},
        mcp={"discovery_concurrency": 0},
    )

    async def go() -> None:
        await drive(fake_clock, host.discover())
        await host.close()

    asyncio.run(go())
    assert fleet.max_opening == 6


def test_a_hanging_server_ends_its_wave_at_120_s(tmp_path: Path, fake_clock: FakeClock) -> None:
    fleet = FakeFleet(fake_clock, {"stuck": FakeServer(hang=True), "ok": FakeServer()})
    host = make_host(tmp_path, fake_clock, fleet, {"stuck": dict(STDIO), "ok": dict(STDIO)})

    async def go() -> None:
        start = fake_clock.monotonic()
        report = await drive(fake_clock, host.discover())
        assert fake_clock.monotonic() - start == pytest.approx(120.0, abs=1.0)
        assert report.connected == ["ok"]
        assert "still connecting after 120 s" in report.failed["stuck"]
        assert "stuck (still connecting" in report.summary()
        await host.close()

    asyncio.run(go())


def test_the_whole_pass_ends_at_300_s(tmp_path: Path, fake_clock: FakeClock) -> None:
    names = ["a", "b", "c"]
    fleet = FakeFleet(fake_clock, {n: FakeServer(hang=True) for n in names})
    host = make_host(
        tmp_path,
        fake_clock,
        fleet,
        {n: dict(STDIO) for n in names},
        mcp={"discovery_concurrency": 1},
    )

    async def go() -> None:
        start = fake_clock.monotonic()
        report = await drive(fake_clock, host.discover())
        assert fake_clock.monotonic() - start == pytest.approx(300.0, abs=1.0)
        assert set(report.failed) == {"a", "b", "c"}
        assert "after 120 s" in report.failed["a"] and "after 120 s" in report.failed["b"]
        assert "discovery ended after 300 s" in report.failed["c"]
        await host.close()

    asyncio.run(go())


def test_a_late_server_registers_itself_when_it_comes_up(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    fleet = FakeFleet(fake_clock, {"slow": FakeServer(open_delay=150.0)})
    host = make_host(tmp_path, fake_clock, fleet, {"slow": dict(STDIO)})

    async def go() -> None:
        report = await drive(fake_clock, host.discover())
        assert "slow" in report.failed and host.tool_count() == 0
        await run_for(fake_clock, 40)
        assert host.registry.names(host.scope) == ["mcp__slow__read"]
        await host.close()

    asyncio.run(go())


# -- registration -------------------------------------------------------------------------------


def test_tools_register_under_the_server_toolset_with_an_alias(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    fleet = FakeFleet(fake_clock, {"files": FakeServer(tools=[tool("read"), tool("write-it")])})
    host = make_host(tmp_path, fake_clock, fleet, {"files": dict(STDIO)})

    async def go() -> None:
        await drive(fake_clock, host.discover())
        reg, scope = host.registry, host.scope
        assert reg.names(scope) == ["mcp__files__read", "mcp__files__write_it"]
        entry = reg.get("mcp__files__read", scope)
        assert entry is not None and entry.toolset == "mcp-files" and entry.is_async
        assert entry.profile == host.profile
        assert toolsets.resolve("files", reg, scope) == ["mcp__files__read", "mcp__files__write_it"]
        assert toolsets.resolve("mcp-files", reg, scope) == toolsets.resolve("files", reg, scope)
        # Another profile sees nothing.
        other = terminal_scope(tmp_path, clock=fake_clock, name="other")
        assert reg.names(other) == []
        result = await call(host, "mcp__files__write_it", {"x": "1"})
        assert json.loads(str(result.content)) == {"result": "write-it ok"}
        assert fleet.servers["files"].calls == [("write-it", {"x": "1"})]
        await host.close()
        assert reg.names(scope) == [] and toolsets.resolve("files", reg, scope) == []

    asyncio.run(go())


def test_a_server_named_like_a_builtin_toolset_unions_with_it(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    fleet = FakeFleet(fake_clock, {"web": FakeServer()})
    registry = ToolRegistry(clock=fake_clock)
    load_builtins(registry)
    host = make_host(tmp_path, fake_clock, fleet, {"web": dict(STDIO)}, registry=registry)

    async def go() -> None:
        before = set(toolsets.resolve("web", registry, host.scope))
        await drive(fake_clock, host.discover())
        after = set(toolsets.resolve("web", registry, host.scope))
        assert after == before | {"mcp__web__read"}
        assert {"web_search", "web_extract"} <= after
        await host.close()

    asyncio.run(go())


def test_a_fully_filtered_server_creates_no_toolset(tmp_path: Path, fake_clock: FakeClock) -> None:
    fleet = FakeFleet(fake_clock, {"files": FakeServer(tools=[tool("read"), tool("write")])})
    host = make_host(
        tmp_path,
        fake_clock,
        fleet,
        {"files": {**STDIO, "tools": {"include": ["nothing*"], "exclude": ["read"]}}},
    )

    async def go() -> None:
        report = await drive(fake_clock, host.discover())
        assert report.connected == ["files"]
        reg = host.registry
        assert reg.names(host.scope) == []
        assert "mcp-files" not in reg.toolset_names(host.scope)
        assert "files" not in reg.toolset_names(host.scope)
        assert not toolsets.is_known("files", reg, host.scope)
        await host.close()

    asyncio.run(go())


def test_include_and_exclude_filters(tmp_path: Path, fake_clock: FakeClock) -> None:
    listed = [tool("read_a"), tool("read_b"), tool("write")]
    fleet = FakeFleet(
        fake_clock, {"inc": FakeServer(tools=listed), "exc": FakeServer(tools=listed)}
    )
    host = make_host(
        tmp_path,
        fake_clock,
        fleet,
        {
            "inc": {**STDIO, "tools": {"include": ["read_*"], "exclude": ["read_a"]}},
            "exc": {**STDIO, "tools": {"exclude": ["write"]}},
        },
    )

    async def go() -> None:
        await drive(fake_clock, host.discover())
        assert host.registry.names(host.scope) == [
            "mcp__exc__read_a",
            "mcp__exc__read_b",
            "mcp__inc__read_a",
            "mcp__inc__read_b",
        ]
        await host.close()

    asyncio.run(go())


def test_two_tools_that_normalise_to_one_name_are_both_refused(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    fleet = FakeFleet(
        fake_clock, {"x": FakeServer(tools=[tool("read-file"), tool("read_file"), tool("ok")])}
    )
    host = make_host(tmp_path, fake_clock, fleet, {"x": dict(STDIO)})

    async def go() -> None:
        await drive(fake_clock, host.discover())
        assert host.registry.names(host.scope) == ["mcp__x__ok"]
        await host.close()

    asyncio.run(go())


def test_read_only_hints_come_from_the_wire(tmp_path: Path, fake_clock: FakeClock) -> None:
    listed = [
        tool("t_true", read_only=True),
        tool("t_yes", read_only="yes"),
        tool("t_one", read_only=1),
        tool("t_false", read_only=False),
        tool("t_none"),
    ]
    fleet = FakeFleet(fake_clock, {"s": FakeServer(tools=listed)})
    host = make_host(tmp_path, fake_clock, fleet, {"s": dict(STDIO)})

    async def go() -> None:
        await drive(fake_clock, host.discover())
        specs = host.handles["s"].specs
        hints = {spec.server_tool: spec.read_only for spec in specs.values()}
        assert hints == {
            "t_true": True,
            "t_yes": False,
            "t_one": False,
            "t_false": False,
            "t_none": False,
        }
        entry = host.registry.get("mcp__s__t_true", host.scope)
        assert entry is not None and entry.traits.read_only
        await host.close()

    asyncio.run(go())


def test_utilities_only_when_allowed_and_advertised(tmp_path: Path, fake_clock: FakeClock) -> None:
    caps = types.ServerCapabilities(
        tools=types.ToolsCapability(),
        resources=types.ResourcesCapability(),
        prompts=types.PromptsCapability(),
    )
    fleet = FakeFleet(
        fake_clock,
        {
            "both": FakeServer(capabilities=caps),
            "no_prompts": FakeServer(capabilities=caps),
            "plain": FakeServer(),
            "own": FakeServer(capabilities=caps, tools=[tool("list_resources")]),
        },
    )
    host = make_host(
        tmp_path,
        fake_clock,
        fleet,
        {
            "both": dict(STDIO),
            "no_prompts": {**STDIO, "tools": {"prompts": False}},
            "plain": dict(STDIO),
            "own": dict(STDIO),
        },
    )

    async def go() -> None:
        await drive(fake_clock, host.discover())
        names = set(host.registry.names(host.scope))
        assert {
            "mcp__both__list_resources",
            "mcp__both__read_resource",
            "mcp__both__list_prompts",
            "mcp__both__get_prompt",
        } <= names
        assert "mcp__no_prompts__list_resources" in names
        assert "mcp__no_prompts__get_prompt" not in names
        assert not any(n.startswith("mcp__plain__") and n != "mcp__plain__read" for n in names)
        own = host.handles["own"].specs["mcp__own__list_resources"]
        assert own.kind == "tool"  # the server's own tool wins
        await host.close()

    asyncio.run(go())


def test_disabled_server_is_never_started(tmp_path: Path, fake_clock: FakeClock) -> None:
    fleet = FakeFleet(fake_clock)
    host = make_host(tmp_path, fake_clock, fleet, {"off": {**STDIO, "enabled": False}})

    async def go() -> None:
        report = await drive(fake_clock, host.discover())
        assert report.disabled == ["off"] and not report.connected
        assert [(r.name, r.state) for r in host.status()] == [("off", "disabled")]
        await host.close()

    asyncio.run(go())
    assert fleet.servers == {}


# -- failures and the summary -------------------------------------------------------------------


def test_startup_summary_names_failures_and_cooling_down_servers(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    bad = McpConfigInvalid("MCP server 'broken' has no usable command", server="broken")
    fleet = FakeFleet(
        fake_clock,
        {
            "broken": FakeServer(open_errors=[bad] * 5),
            "flaky": FakeServer(open_errors=[ConnectionError("refused")] * 10),
            "fine": FakeServer(),
        },
    )
    host = make_host(
        tmp_path,
        fake_clock,
        fleet,
        {"broken": dict(STDIO), "flaky": dict(STDIO), "fine": dict(STDIO), "bad": {"args": []}},
    )

    async def go() -> None:
        report = await drive(fake_clock, host.discover())
        assert report.connected == ["fine"]
        assert "no usable command" in report.failed["broken"]
        assert "refused" in report.failed["flaky"]
        assert "bad" in report.problems
        text = report.summary()
        assert "broken (" in text and "flaky (" in text and "not configured correctly: bad" in text
        # A new session's discovery skips the server still cooling down.
        again = await host.discover()
        assert again.connected == ["fine"]
        assert 0 < again.cooling_down["flaky"] <= 30
        assert "skipped while cooling down: flaky" in again.summary()
        states = {r.name: r.state for r in host.status()}
        assert states == {
            "broken": "parked",
            "flaky": "cooling_down",
            "fine": "ready",
            "bad": "failed",
        }
        await host.close()

    asyncio.run(go())


# -- the schema cache and lazy servers ----------------------------------------------------------


def write_cache(home: Path, name: str, config: dict[str, Any], tools: list[CachedTool]) -> None:
    SchemaCache(home).write(name, fingerprint(McpServer(**config)), tools, [], now=0.0)


def test_live_listing_writes_the_cache(tmp_path: Path, fake_clock: FakeClock) -> None:
    fleet = FakeFleet(fake_clock, {"s": FakeServer(tools=[tool("a", read_only=True), tool("b")])})
    host = make_host(tmp_path, fake_clock, fleet, {"s": dict(STDIO)})

    async def go() -> None:
        await drive(fake_clock, host.discover())
        await host.close()

    asyncio.run(go())
    path = host.scope.home / "cache" / "mcp" / "s.json"
    data = json.loads(path.read_text())
    assert data["fingerprint"] == fingerprint(McpServer(**STDIO))
    assert [(t["name"], t["readOnlyHint"]) for t in data["tools"]] == [("a", True), ("b", None)]
    assert path.stat().st_mode & 0o777 == 0o600


def test_lazy_server_registers_from_cache_without_starting(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    config = {**STDIO, "lazy": True}
    fleet = FakeFleet(fake_clock, {"s": FakeServer(tools=[tool("a", read_only=True)])})
    host = make_host(tmp_path, fake_clock, fleet, {"s": config})
    write_cache(
        host.scope.home,
        "s",
        config,
        [CachedTool("a", "cached a", {"type": "object"}, True), CachedTool("gone", None, {})],
    )

    async def go() -> None:
        report = await drive(fake_clock, host.discover())
        assert report.lazy == ["s"] and not report.connected
        assert fleet.servers["s"].opens == 0
        assert host.registry.names(host.scope) == ["mcp__s__a", "mcp__s__gone"]
        (status,) = host.status()
        assert (status.state, status.detail) == ("lazy", "lazy, starts on first use")
        assert "1 lazy, not started: s" in report.summary()
        # The first call starts it; the live listing replaces the cached one.
        result = await drive(fake_clock, call(host, "mcp__s__a"))
        assert not result.is_error
        assert fleet.servers["s"].opens == 1
        assert host.registry.names(host.scope) == ["mcp__s__a"]
        assert host.status()[0].state == "ready"
        await host.close()

    asyncio.run(go())


@pytest.mark.parametrize(
    "cached_config",
    [
        {"command": "old-server"},
        {"command": "fake-server", "args": ["--other"]},
        {"command": "fake-server", "tools": {"exclude": ["x"]}},
    ],
)
def test_an_edited_lazy_server_connects_eagerly_once(
    tmp_path: Path, fake_clock: FakeClock, cached_config: dict[str, Any]
) -> None:
    config = {**STDIO, "lazy": True}
    fleet = FakeFleet(fake_clock, {"s": FakeServer()})
    host = make_host(tmp_path, fake_clock, fleet, {"s": config})
    write_cache(host.scope.home, "s", cached_config, [CachedTool("old", None, {})])

    async def go() -> None:
        report = await drive(fake_clock, host.discover())
        assert report.connected == ["s"] and not report.lazy
        assert host.registry.names(host.scope) == ["mcp__s__read"]
        await host.close()

    asyncio.run(go())
    assert fleet.servers["s"].opens == 1
    entry = SchemaCache(host.scope.home).read("s", fingerprint(McpServer(**config)))
    assert entry is not None and [t.name for t in entry.tools] == ["read"]


def test_lazy_server_without_a_cache_connects_once(tmp_path: Path, fake_clock: FakeClock) -> None:
    fleet = FakeFleet(fake_clock, {"s": FakeServer()})
    host = make_host(tmp_path, fake_clock, fleet, {"s": {**STDIO, "lazy": True}})

    async def go() -> None:
        report = await drive(fake_clock, host.discover())
        assert report.connected == ["s"]
        await host.close()

    asyncio.run(go())
    assert (host.scope.home / "cache" / "mcp" / "s.json").is_file()


def test_unreadable_cache_is_ignored(tmp_path: Path, fake_clock: FakeClock) -> None:
    cache = SchemaCache(tmp_path)
    cache.folder.mkdir(parents=True)
    cache.path("s").write_text("{not json")
    assert cache.read("s", "abc") is None
    cache.path("s").write_text(json.dumps({"version": 1, "fingerprint": "abc", "tools": "no"}))
    assert cache.read("s", "abc") is None


# -- hosts --------------------------------------------------------------------------------------


def test_hosts_are_kept_per_profile(tmp_path: Path, fake_clock: FakeClock) -> None:
    fleet = FakeFleet(fake_clock)
    one = make_host(tmp_path, fake_clock, fleet, {}, name="one")
    two = make_host(tmp_path, fake_clock, fleet, {}, name="two")
    register_host(one)
    register_host(two)
    try:
        assert host_for(one.scope) is one and host_for(two.scope) is two
    finally:
        assert drop_host(one.scope) is one
        assert drop_host(two.scope) is two
    assert host_for(one.scope) is None


def test_a_handle_reports_its_kind(tmp_path: Path, fake_clock: FakeClock) -> None:
    host = make_host(tmp_path, fake_clock, FakeFleet(fake_clock), {})
    handle = ServerHandle(host, "r", McpServer(url="https://mcp.example/mcp"))
    assert handle.report().kind == "http" and handle.state == "connecting"


def test_registry_toolset_aliases(tmp_path: Path, fake_clock: FakeClock) -> None:
    registry = ToolRegistry(clock=fake_clock)
    one = terminal_scope(tmp_path, clock=fake_clock, name="one")
    two = terminal_scope(tmp_path, clock=fake_clock, name="two")
    schema = {"description": "t", "parameters": {"type": "object", "properties": {}}}
    registry.register("mcp__s__a", "mcp-s", schema, lambda args: "a", profile=str(one.home))
    generation = registry.generation
    registry.set_toolset_alias("s", "mcp-s", profile=str(one.home))
    assert registry.generation == generation + 1
    registry.set_toolset_alias("s", "mcp-s", profile=str(one.home))  # no change, no bump
    assert registry.generation == generation + 1
    assert registry.names_in_toolset("s", one) == ["mcp__s__a"]
    assert "s" in registry.toolset_names(one)
    assert registry.names_in_toolset("s", two) == [] and "s" not in registry.toolset_names(two)
    registry.set_toolset_alias("s", "mcp-s", profile=str(one.home), present=False)
    assert registry.names_in_toolset("s", one) == [] and "s" not in registry.toolset_names(one)
