"""Reconcile, ``/reload-mcp``, the config watcher and ``tools/list_changed`` (P13-08)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import mcp_types as types
import pytest

from indus.foundation.config import loader
from indus.mcp import reload as mcp_reload
from indus.mcp.discovery import McpHost, drop_host, register_host
from indus.mcp.lifecycle import RetryPolicy
from indus.mcp.reload import ConfigWatcher, ToolSurfaceChanged, reconcile, reload_mcp
from indus.tools.definitions import definitions
from indus.tools.registry import ToolRegistry
from tests.fakes.clock import FakeClock
from tests.fakes.mcp.scripted import FakeFleet, FakeServer, drive, run_for, tool
from tests.fakes.terminal import terminal_scope

STDIO: dict[str, Any] = {"command": "fake-server"}


def write_config(home: Path, data: dict[str, Any]) -> None:
    path = home / "config.yaml"
    path.write_text(json.dumps(data), encoding="utf-8")  # JSON is YAML
    loader.invalidate(path)


def make_host(
    tmp_path: Path, clock: FakeClock, fleet: FakeFleet, data: dict[str, Any]
) -> tuple[McpHost, list[Any]]:
    scope = terminal_scope(tmp_path, clock=clock)
    write_config(scope.home, data)
    scope = mcp_reload.fresh_scope(scope)
    host = McpHost(
        scope, ToolRegistry(clock=clock), clock=clock, opener=fleet, policy=RetryPolicy(jitter=0.0)
    )
    events: list[Any] = []
    host.listeners.append(events.append)
    return host, events


def names(host: McpHost) -> list[str]:
    return host.registry.names(host.scope)


async def pump(clock: FakeClock, done: Callable[[], bool], steps: int = 80) -> None:
    """Move the clock in half seconds, letting worker threads finish, until ``done()``."""
    loop = asyncio.get_running_loop()
    for _ in range(steps):
        if done():
            return
        await clock.advance_and_settle(0.5)
        idle = loop.create_future()
        await asyncio.wait({idle}, timeout=0.01)
        idle.cancel()
    assert done(), "the condition never held"


# -- reconcile ----------------------------------------------------------------------------------


def test_reconcile_adds_removes_restarts_and_keeps(tmp_path: Path, fake_clock: FakeClock) -> None:
    fleet = FakeFleet(
        fake_clock,
        {
            "keep": FakeServer(tools=[tool("k")]),
            "drop": FakeServer(tools=[tool("d")]),
            "edit": FakeServer(tools=[tool("e")]),
            "new": FakeServer(tools=[tool("n")]),
        },
    )
    first = {"mcp_servers": {"keep": dict(STDIO), "drop": dict(STDIO), "edit": dict(STDIO)}}
    host, events = make_host(tmp_path, fake_clock, fleet, first)

    async def go() -> None:
        await drive(fake_clock, host.discover())
        assert names(host) == ["mcp__drop__d", "mcp__edit__e", "mcp__keep__k"]
        write_config(
            host.scope.home,
            {
                "mcp_servers": {
                    "keep": dict(STDIO),
                    "edit": {**STDIO, "args": ["--v2"]},
                    "new": dict(STDIO),
                }
            },
        )
        report = await drive(fake_clock, reconcile(host, scope=mcp_reload.fresh_scope(host.scope)))
        assert (report.added, report.removed, report.restarted, report.kept) == (
            ["new"],
            ["drop"],
            ["edit"],
            ["keep"],
        )
        assert names(host) == ["mcp__edit__e", "mcp__keep__k", "mcp__new__n"]
        assert "drop" not in host.registry.toolset_names(host.scope)
        await host.close()

    asyncio.run(go())
    opens = {n: s.opens for n, s in fleet.servers.items()}
    assert opens == {"keep": 1, "drop": 1, "edit": 2, "new": 1}
    (event,) = [e for e in events if isinstance(e, ToolSurfaceChanged)]
    assert event.reason == "reload" and event.added == ("mcp__new__n",)
    assert event.removed == ("mcp__drop__d",)


def test_disabling_a_server_removes_its_tools(tmp_path: Path, fake_clock: FakeClock) -> None:
    fleet = FakeFleet(fake_clock, {"s": FakeServer()})
    host, _ = make_host(tmp_path, fake_clock, fleet, {"mcp_servers": {"s": dict(STDIO)}})

    async def go() -> None:
        await drive(fake_clock, host.discover())
        assert names(host) == ["mcp__s__read"]
        write_config(host.scope.home, {"mcp_servers": {"s": {**STDIO, "enabled": False}}})
        report = await drive(fake_clock, reconcile(host, scope=mcp_reload.fresh_scope(host.scope)))
        assert report.removed == ["s"] and names(host) == []
        await host.close()

    asyncio.run(go())


def test_reconcile_is_bounded_by_30_s(tmp_path: Path, fake_clock: FakeClock) -> None:
    fleet = FakeFleet(fake_clock, {"slow": FakeServer(hang=True)})
    host, _ = make_host(tmp_path, fake_clock, fleet, {"mcp_servers": {}})

    async def go() -> None:
        await drive(fake_clock, host.discover())
        write_config(host.scope.home, {"mcp_servers": {"slow": dict(STDIO)}})
        start = fake_clock.monotonic()
        report = await drive(fake_clock, reconcile(host, scope=mcp_reload.fresh_scope(host.scope)))
        assert fake_clock.monotonic() - start == pytest.approx(30.0, abs=1.0)
        assert report.timed_out and "Still connecting after 30 s" in report.summary()
        assert host.handles["slow"].launched
        await host.close()

    asyncio.run(go())


# -- /reload-mcp --------------------------------------------------------------------------------


def test_reload_keeps_the_stored_tool_array_and_announces_the_change(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    fleet = FakeFleet(fake_clock, {"a": FakeServer(tools=[tool("one")]), "b": FakeServer()})
    host, events = make_host(tmp_path, fake_clock, fleet, {"mcp_servers": {"a": dict(STDIO)}})

    async def go() -> None:
        await drive(fake_clock, host.discover())
        selected = names(host)
        stored = definitions(host.registry, selected, None, scope=host.scope)
        stored_bytes = json.dumps(stored, sort_keys=True)
        write_config(host.scope.home, {"mcp_servers": {"b": dict(STDIO)}})
        outcome = await drive(fake_clock, reload_mcp(host))
        assert outcome.reloaded and "added: b" in outcome.message
        # The running session's array is untouched; the next rebuild sees the new surface.
        assert json.dumps(stored, sort_keys=True) == stored_bytes
        assert definitions(host.registry, selected, None, scope=host.scope) == []
        rebuilt = definitions(host.registry, names(host), None, scope=host.scope)
        assert [d["function"]["name"] for d in rebuilt] == ["mcp__b__read"]
        await host.close()

    asyncio.run(go())
    (event,) = events
    assert event.added == ("mcp__b__read",) and event.removed == ("mcp__a__one",)
    assert event.generation > 0


@pytest.mark.parametrize("choice", ["cancel", "once", "always"])
def test_reload_asks_first(tmp_path: Path, fake_clock: FakeClock, choice: str) -> None:
    fleet = FakeFleet(fake_clock, {"a": FakeServer(), "b": FakeServer()})
    host, _ = make_host(tmp_path, fake_clock, fleet, {"mcp_servers": {"a": dict(STDIO)}})
    questions: list[str] = []

    async def ask(question: str) -> Any:
        questions.append(question)
        return choice

    async def go() -> None:
        await drive(fake_clock, host.discover())
        write_config(host.scope.home, {"mcp_servers": {"a": dict(STDIO), "b": dict(STDIO)}})
        outcome = await drive(fake_clock, reload_mcp(host, ask=ask))
        if choice == "cancel":
            assert not outcome.reloaded and names(host) == ["mcp__a__read"]
        else:
            assert outcome.reloaded and names(host) == ["mcp__a__read", "mcp__b__read"]
        await drive(fake_clock, reload_mcp(host, ask=ask))
        await host.close()

    asyncio.run(go())
    assert "prompt cache" in questions[0]
    saved = loader.load(host.scope).get("approvals.mcp_reload_confirm")
    if choice == "always":
        assert saved is False and len(questions) == 1  # the second reload did not ask
    else:
        assert saved is True and len(questions) == 2


def test_bridge_reload_entry(tmp_path: Path, fake_clock: FakeClock) -> None:
    fleet = FakeFleet(fake_clock, {"a": FakeServer()})
    host, _ = make_host(tmp_path, fake_clock, fleet, {"mcp_servers": {"a": dict(STDIO)}})

    async def go() -> None:
        missing = await mcp_reload.reload(host.scope)
        assert not missing.reloaded and "No MCP servers" in missing.message
        register_host(host)
        try:
            done = await drive(fake_clock, mcp_reload.reload(host.scope))
            assert done.reloaded and names(host) == ["mcp__a__read"]
        finally:
            drop_host(host.scope)
            await host.close()

    asyncio.run(go())


# -- the config watcher -------------------------------------------------------------------------


def test_watcher_reconciles_after_an_edit(tmp_path: Path, fake_clock: FakeClock) -> None:
    fleet = FakeFleet(fake_clock, {"a": FakeServer(), "b": FakeServer()})
    host, _ = make_host(tmp_path, fake_clock, fleet, {"mcp_servers": {"a": dict(STDIO)}})
    watcher = ConfigWatcher(host)

    async def go() -> None:
        await drive(fake_clock, host.discover())
        watcher.start()
        # An edit elsewhere in the file does not reconcile.
        write_config(
            host.scope.home, {"mcp_servers": {"a": dict(STDIO)}, "display": {"busy": "queue"}}
        )
        await pump(fake_clock, lambda: watcher.checks == 1)
        assert watcher.runs == 0 and names(host) == ["mcp__a__read"]
        write_config(host.scope.home, {"mcp_servers": {"a": dict(STDIO), "b": dict(STDIO)}})
        await pump(fake_clock, lambda: "mcp__b__read" in names(host))
        assert watcher.runs == 1 and watcher.checks == 2
        await watcher.close()
        await host.close()

    asyncio.run(go())


def test_watcher_respects_the_auto_reload_switch(tmp_path: Path, fake_clock: FakeClock) -> None:
    fleet = FakeFleet(fake_clock, {"a": FakeServer(), "b": FakeServer()})
    off = {"mcp": {"auto_reload_on_config_change": False}}
    host, _ = make_host(tmp_path, fake_clock, fleet, {"mcp_servers": {"a": dict(STDIO)}, **off})
    watcher = ConfigWatcher(host)

    async def go() -> None:
        await drive(fake_clock, host.discover())
        write_config(host.scope.home, {"mcp_servers": {"b": dict(STDIO)}, **off})
        assert await watcher.check() is None
        assert names(host) == ["mcp__a__read"] and watcher.runs == 0
        await host.close()

    asyncio.run(go())


# -- tools/list_changed -------------------------------------------------------------------------


def changed() -> types.ToolListChangedNotification:
    return types.ToolListChangedNotification(method="notifications/tools/list_changed")


def test_a_storm_of_list_changed_is_coalesced(tmp_path: Path, fake_clock: FakeClock) -> None:
    server = FakeServer(tools=[tool("a")])
    fleet = FakeFleet(fake_clock, {"s": server})
    host, events = make_host(tmp_path, fake_clock, fleet, {"mcp_servers": {"s": dict(STDIO)}})

    async def go() -> None:
        await drive(fake_clock, host.discover())
        assert server.listings == 1
        server.tools = [tool("a"), tool("b")]
        for _ in range(20):
            await fleet.notify("s", changed())
            await fake_clock.advance_and_settle(0.05)
        await run_for(fake_clock, 3)
        assert server.listings == 2  # one re-fetch for the whole storm
        assert names(host) == ["mcp__s__a", "mcp__s__b"]
        await host.close()

    asyncio.run(go())
    (event,) = events
    assert event.reason == "list_changed" and event.added == ("mcp__s__b",)


def test_notifications_during_a_refetch_cause_exactly_one_more(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    server = FakeServer(tools=[tool("a")])
    fleet = FakeFleet(fake_clock, {"s": server})
    host, _ = make_host(tmp_path, fake_clock, fleet, {"mcp_servers": {"s": dict(STDIO)}})
    gate = asyncio.Event()

    async def go() -> None:
        await drive(fake_clock, host.discover())
        live = fleet.servers["s"].live
        assert live is not None
        raw: Any = live.raw
        original = raw.list_tools

        async def slow_list(params: Any = None) -> Any:
            await gate.wait()
            return await original(params)

        raw.list_tools = slow_list
        await fleet.notify("s", changed())
        await run_for(fake_clock, 3)  # the window closed; the re-fetch is waiting on the gate
        for _ in range(5):
            await fleet.notify("s", changed())
        gate.set()
        await run_for(fake_clock, 6)
        refresher = host.handles["s"].refresher
        assert refresher is not None and refresher.fetches == 2
        await host.close()

    asyncio.run(go())
    assert server.listings == 3


def test_prompt_and_resource_changes_are_ignored(tmp_path: Path, fake_clock: FakeClock) -> None:
    server = FakeServer()
    fleet = FakeFleet(fake_clock, {"s": server})
    host, events = make_host(tmp_path, fake_clock, fleet, {"mcp_servers": {"s": dict(STDIO)}})

    async def go() -> None:
        await drive(fake_clock, host.discover())
        await fleet.notify(
            "s", types.PromptListChangedNotification(method="notifications/prompts/list_changed")
        )
        await fleet.notify(
            "s",
            types.ResourceListChangedNotification(method="notifications/resources/list_changed"),
        )
        await run_for(fake_clock, 5)
        assert host.handles["s"].refresher is None
        await host.close()

    asyncio.run(go())
    assert server.listings == 1 and events == []


def test_unchanged_refetch_announces_nothing(tmp_path: Path, fake_clock: FakeClock) -> None:
    server = FakeServer()
    fleet = FakeFleet(fake_clock, {"s": server})
    host, events = make_host(tmp_path, fake_clock, fleet, {"mcp_servers": {"s": dict(STDIO)}})

    async def go() -> None:
        await drive(fake_clock, host.discover())
        generation = host.registry.generation
        await fleet.notify("s", changed())
        await run_for(fake_clock, 3)
        assert server.listings == 2 and host.registry.generation == generation
        await host.close()

    asyncio.run(go())
    assert events == []
