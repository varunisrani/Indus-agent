"""The MCP host end to end against the SDK fake servers (P13-05..07, ACC-G14-1).

A real stdio server and the in-process Streamable HTTP server: discovery registers prefixed
tools, the dispatch path calls them, the raw ``readOnlyHint`` arrives through the wire tap and
gates an untrusted server's write tool.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from indus.foundation.clock import SystemClock
from indus.mcp.connect import ConnectOptions
from indus.mcp.discovery import McpHost
from indus.safety.approvals.decision import ApprovalState
from indus.tools import toolsets
from indus.tools.calls import ToolCall, ToolContext, ToolResult
from indus.tools.dispatch import dispatch
from indus.tools.registry import ToolRegistry
from tests.fakes.broker import FakeBroker, Reply
from tests.fakes.clock import FakeClock
from tests.fakes.mcp import http_server
from tests.fakes.terminal import terminal_scope

FAKE = Path(__file__).resolve().parents[2] / "fakes" / "mcp" / "stdio_server.py"
REAL = SystemClock()


def text(result: ToolResult) -> Any:
    assert isinstance(result.content, str)
    return json.loads(result.content)


def test_stdio_server_tools_are_prefixed_and_callable(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    servers = {
        "files": {
            "command": sys.executable,
            "args": [str(FAKE), "--name", "files"],
            "trust": "untrusted",
            "connect_timeout": 30,
        }
    }
    scope = terminal_scope(tmp_path, {"mcp_servers": servers})
    host = McpHost(scope, ToolRegistry(), clock=REAL, approvals=ApprovalState())
    broker = FakeBroker(fake_clock, replies=[Reply(choice="deny"), Reply(choice="once")])

    async def call(name: str, args: dict[str, Any]) -> ToolResult:
        ctx = ToolContext(scope=scope, session_id="s1", extras={"broker": broker})
        return await dispatch(ToolCall(name, args), ctx, registry=host.registry)

    async def go() -> None:
        report = await host.discover()
        assert report.connected == ["files"], report.summary()
        names = set(toolsets.resolve("files", host.registry, scope))
        tools = {f"mcp__files__{t}" for t in ("add", "crash", "cwd", "echo", "getenv")}
        assert tools <= names
        # The SDK server advertises resources and prompts: their utility tools come along.
        assert names - tools == {
            f"mcp__files__{u}"
            for u in ("list_resources", "read_resource", "list_prompts", "get_prompt")
        }
        specs = host.handles["files"].specs
        assert specs["mcp__files__echo"].read_only and not specs["mcp__files__add"].read_only
        assert text(await call("mcp__files__echo", {"text": "hi"}))["result"] == "hi"
        assert broker.requests == []  # read-only: no question
        denied = await call("mcp__files__add", {"a": 2, "b": 3})
        assert denied.is_error and text(denied)["error_type"] == "not_approved"
        added = await call("mcp__files__add", {"a": 2, "b": 3})
        assert text(added)["result"] == "5"
        assert len(broker.requests) == 2
        await host.close()

    asyncio.run(go())
    cached = json.loads((scope.home / "cache" / "mcp" / "files.json").read_text())
    hints = {t["name"]: t["readOnlyHint"] for t in cached["tools"]}
    assert hints["echo"] is True and hints["add"] is None


def test_http_server_tools_through_the_host(tmp_path: Path) -> None:
    async def go() -> None:
        async with http_server.serving() as (app, _log):
            scope = terminal_scope(tmp_path, {"mcp_servers": {"remote": {"url": http_server.URL}}})
            options = ConnectOptions(http_client=http_server.client_factory(app))
            host = McpHost(scope, ToolRegistry(), clock=REAL, options=options)
            report = await host.discover()
            assert report.connected == ["remote"], report.summary()
            ctx = ToolContext(scope=scope)
            result = await dispatch(
                ToolCall("mcp__remote__add", {"a": 40, "b": 2}), ctx, registry=host.registry
            )
            assert text(result)["result"] == "42"
            await host.close()

    asyncio.run(go())
