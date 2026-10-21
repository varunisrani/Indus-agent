"""P19-13: the ACP toolset, per-session MCP selection, the editor MCP policy and servers."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from indus.acp import toolset
from indus.foundation.scope import Scope
from indus.tools import toolsets
from indus.tools.registry import ToolRegistry
from tests.fakes.agent import DictConfig
from tests.fakes.clock import FakeClock
from tests.fakes.mcp.scripted import FakeFleet, FakeServer, drive, tool

VERBS = ("back", "cdp", "click", "console", "dialog", "exec", "get_images") + (
    "navigate",
    "press",
    "scroll",
    "snapshot",
    "type",
    "vault",
    "vision",
)  # fmt: skip  (exec, vault: P24-39 / P24-40, offered when set up)
GOLDEN = sorted(
    [
        *(f"browser_{verb}" for verb in VERBS),
        *("delegate_task", "execute_code", "memory", "patch", "process_manage", "read_file"),
        *("search_files", "session_search", "skill_manage", "skill_view", "skills_list"),
        *("terminal", "todo_list", "vision_analyze", "web_extract", "web_search", "write_file"),
    ]
)


def _with_config(scope: Scope, data: dict[str, Any]) -> Any:
    return DictConfig(data)


def test_the_default_bundle_resolves_to_the_golden_tool_list(scope: Scope) -> None:
    names = toolset.selection(scope, _with_config(scope, {}))
    assert names == ["indus-acp"]
    tools = sorted(toolsets.select(names, platform="acp").tools)
    assert tools == GOLDEN
    for absent in ("clarify", "cronjob_manage", "text_to_speech", "image_generate"):
        assert absent not in tools  # no questions tool, no scheduling, no messaging/audio


def test_a_saved_selection_wins_and_disabled_toolsets_go_last(scope: Scope) -> None:
    view = _with_config(scope, {"platform_toolsets": {"acp": ["file", "web", "no_mcp"]}})
    names = toolset.selection(scope, view)
    assert names == ["file", "web"]
    assert toolset.no_mcp(scope, view)
    chosen = toolsets.select(["indus-acp"], ["terminal"], platform="acp").tools
    assert "terminal" not in chosen and "process_manage" not in chosen
    assert "read_file" in chosen


def test_each_session_sees_only_its_own_editor_servers() -> None:
    chosen = toolset.session_selection(
        ["indus-acp", "mcp"], config_servers=["github", "editor-docs"], editor_servers=["docs"]
    )
    assert chosen == ["indus-acp", "mcp-github", "mcp-editor-docs"]
    other = toolset.session_selection(["indus-acp", "mcp"], config_servers=["github"])
    assert other == ["indus-acp", "mcp-github"]
    assert toolset.session_selection(None, editor_servers=["docs"]) is None


def test_no_mcp_drops_configured_servers_and_keeps_editor_ones() -> None:
    chosen = toolset.session_selection(
        ["indus-acp", "mcp", "mcp-github"],
        config_servers=["github"],
        editor_servers=["docs"],
        no_mcp=True,
    )
    assert chosen == ["indus-acp", "mcp-editor-docs"]


SPEC: dict[str, Any] = {
    "name": "docs",
    "command": "/usr/bin/docs-server",
    "args": ["--stdio"],
    "env": [{"name": "DOCS_TOKEN", "value": "tok" + "en-one"}],
}


def test_specs_become_p13_server_configs() -> None:
    stdio = toolset.server_config(SPEC)
    assert stdio.command == "/usr/bin/docs-server" and stdio.args == ["--stdio"]
    assert stdio.env == {"DOCS_TOKEN": "tok" + "en-one"}
    http = toolset.server_config(
        {
            "type": "http",
            "name": "api",
            "url": "https://mcp.example.com/mcp",
            "headers": [{"name": "X-Team", "value": "a"}],
        }
    )
    assert http.url == "https://mcp.example.com/mcp" and http.transport == "http"
    assert http.headers == {"X-Team": "a"}


def test_a_changed_spec_after_a_restart_is_refused() -> None:
    record = toolset.EditorToolPolicy.of([SPEC]).record()
    stored = repr(record)
    assert "en-one" not in stored and "DOCS_TOKEN" in stored or "fingerprints" in stored
    restarted = toolset.EditorToolPolicy.from_record(record)
    assert restarted.refused([SPEC]) == []
    changed = {**SPEC, "env": [{"name": "DOCS_TOKEN", "value": "tok" + "en-two"}]}
    assert restarted.refused([changed]) == ["docs"]
    assert restarted.refused([{**SPEC, "name": "fresh"}]) == []  # an unknown server is new


def test_schemas_stay_frozen_across_a_tools_changed_listing() -> None:
    policy = toolset.EditorToolPolicy()
    first = {"mcp__docs__search": {"parameters": {"type": "object"}}}
    assert policy.freeze("editor-docs", first) is False
    later = {"mcp__docs__search": {"parameters": {"type": "object", "required": ["q"]}}}
    assert policy.freeze("editor-docs", later) is True
    assert policy.schemas["editor-docs"] == first
    assert policy.freeze("editor-docs", first) is False


def test_editor_servers_connect_through_the_p13_client(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    fleet = FakeFleet(fake_clock, {"editor-docs": FakeServer(tools=[tool("search"), tool("open")])})
    registry = ToolRegistry(clock=fake_clock)
    servers = toolset.EditorServers(scope, registry, opener=fleet)

    async def go() -> None:
        connected, changed = await drive(fake_clock, servers.attach([SPEC]))
        assert connected == ["docs"] and changed == []
        names = registry.names_in_toolset("mcp-editor-docs", scope)
        assert len(names) == 2 and servers.tool_names("docs") == sorted(names)
        assert fleet.servers["editor-docs"].opens == 1
        await servers.aclose()

    asyncio.run(go())
