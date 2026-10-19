"""Contract suite for the MCP transports (P13-03) against fake servers built on the SDK."""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import mcp_types as types
import pytest
from mcp.shared.exceptions import MCPError

from indus.foundation.clock import SystemClock
from indus.foundation.scope import Scope
from indus.mcp.config import IdentityHeader, McpServer
from indus.mcp.connect import (
    Connection,
    ConnectOptions,
    LiveSession,
    McpConnectFailed,
    McpConnectTimeout,
    StderrLog,
)
from indus.mcp.lifecycle import ManagedServer, ServerState
from tests.fakes.clock import FakeClock
from tests.fakes.mcp import http_server
from tests.fakes.mcp.stdio_server import UNSET
from tests.fakes.terminal import forge_token, terminal_scope

FAKE = Path(__file__).resolve().parents[2] / "fakes" / "mcp" / "stdio_server.py"
REAL = SystemClock()


def stdio(*flags: str, **settings: Any) -> McpServer:
    settings.setdefault("connect_timeout", 30)
    return McpServer(command=sys.executable, args=[str(FAKE), *flags], **settings)


def text_of(result: types.CallToolResult) -> str:
    return "".join(part.text for part in result.content if isinstance(part, types.TextContent))


async def wait_for_file(path: Path, limit: float = 20.0) -> str:
    waited = 0.0
    while waited < limit:
        if path.exists() and path.read_text(encoding="utf-8").strip():
            return path.read_text(encoding="utf-8").strip()
        await REAL.sleep(0.05)
        waited += 0.05
    raise AssertionError(f"{path} never appeared")


def alive(pid: int) -> bool:
    status = Path(f"/proc/{pid}/status")
    if status.exists():
        return "State:\tZ" not in status.read_text(encoding="utf-8")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def connection(scope: Scope, server: McpServer, name: str = "files", **opts: Any) -> Connection:
    return Connection(name, server, scope, clock=REAL, options=ConnectOptions(**opts))


# -- stdio --------------------------------------------------------------------------------------


def test_stdio_initialize_list_call_close(tmp_path: Path) -> None:
    scope = terminal_scope(tmp_path)

    async def go() -> None:
        async with connection(scope, stdio("--name", "files")) as live:
            assert isinstance(live, LiveSession) and live.kind == "stdio"
            assert live.server_info is not None and live.server_info.name == "files"
            assert live.offers_tools
            tools = {t.name: t for t in await live.list_tools()}
            assert {"echo", "add", "getenv", "cwd", "crash"} <= set(tools)
            annotations = tools["echo"].annotations
            assert annotations is not None and annotations.read_only_hint is True
            assert text_of(await live.call_tool("add", {"a": 2, "b": 3})) == "5"
            await live.ping()
            assert not live.dropped.is_set()
        assert live.dropped.is_set()

    asyncio.run(go())


def test_stdio_env_is_filtered_and_configured_values_arrive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    leaked = forge_token()
    monkeypatch.setenv("OPENAI_API_KEY", leaked)
    scope = terminal_scope(tmp_path, secrets={"FILES_TOKEN": "from-the-store"})
    scope = scope.with_env(
        scope.env.with_overlay({"OPENAI_API_KEY": leaked, "XDG_DATA_HOME": "/x"})
    )
    server = stdio(env={"FILES_TOKEN": "${FILES_TOKEN}", "MODE": "ro"})

    async def go() -> None:
        async with connection(scope, server, workspace=tmp_path) as live:
            assert text_of(await live.call_tool("getenv", {"name": "OPENAI_API_KEY"})) == UNSET
            got = text_of(await live.call_tool("getenv", {"name": "FILES_TOKEN"}))
            assert got == "from-the-store"
            assert text_of(await live.call_tool("getenv", {"name": "MODE"})) == "ro"
            assert text_of(await live.call_tool("getenv", {"name": "XDG_DATA_HOME"})) == "/x"
            assert text_of(await live.call_tool("getenv", {"name": "PATH"})) != UNSET
            assert Path(text_of(await live.call_tool("cwd", {}))) == tmp_path

    asyncio.run(go())


def test_stdio_stderr_goes_to_the_log(tmp_path: Path) -> None:
    scope = terminal_scope(tmp_path)
    log_path = tmp_path / "logs" / "mcp-stderr.log"

    async def go() -> None:
        sink = StderrLog(log_path)
        async with connection(scope, stdio("--stderr", "hello from the fake"), stderr_log=sink):
            pass

    asyncio.run(go())
    text = log_path.read_text(encoding="utf-8")
    assert "--- starting MCP server files ---" in text
    assert "[files] hello from the fake" in text


def test_stdio_server_dying_at_start_reports_its_stderr(tmp_path: Path) -> None:
    scope = terminal_scope(tmp_path)
    server = stdio("--stderr", "missing module frobnicate", "--crash-on-start")

    async def go() -> None:
        sink = StderrLog(tmp_path / "err.log")
        with pytest.raises(McpConnectFailed) as caught:
            await connection(scope, server, stderr_log=sink).start()
        assert "files" in str(caught.value)
        assert "frobnicate" in caught.value.stderr_tail

    asyncio.run(go())


def test_missing_command_is_a_permanent_failure(tmp_path: Path) -> None:
    scope = terminal_scope(tmp_path)
    server = McpServer(command=str(tmp_path / "no-such-server"))

    async def go() -> None:
        with pytest.raises(McpConnectFailed) as caught:
            await connection(scope, server, stderr_log=StderrLog(tmp_path / "e.log")).start()
        assert caught.value.permanent

    asyncio.run(go())


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="the process sweep reads /proc")
def test_process_tree_is_gone_after_close(tmp_path: Path) -> None:
    scope = terminal_scope(tmp_path)
    marker = tmp_path / "grandchild.pid"

    async def go() -> int:
        sink = StderrLog(tmp_path / "e.log")
        async with connection(scope, stdio("--grandchild", str(marker)), stderr_log=sink):
            pid = int(await wait_for_file(marker))
            assert alive(pid)
        return pid

    pid = asyncio.run(go())
    assert not alive(pid)


def test_connect_timeout_with_a_silent_server(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
    scope = terminal_scope(tmp_path, clock=clock)
    marker = tmp_path / "server.pid"
    server = stdio("--hang", "--pidfile", str(marker), connect_timeout=60)

    async def go() -> None:
        conn = Connection(
            "silent",
            server,
            scope,
            clock=clock,
            options=ConnectOptions(stderr_log=StderrLog(tmp_path / "e.log")),
        )
        starting = asyncio.ensure_future(conn.start())
        await wait_for_file(marker)
        clock.advance(59)
        await REAL.sleep(0.2)
        assert not starting.done()
        clock.advance(1)
        with pytest.raises(McpConnectTimeout) as caught:
            await starting
        assert "silent" in str(caught.value) and "60" in str(caught.value)
        await conn.close()

    asyncio.run(go())


# -- Streamable HTTP (in process) ---------------------------------------------------------------


def test_http_initialize_list_call_close(tmp_path: Path) -> None:
    token = forge_token()
    scope = terminal_scope(tmp_path, secrets={"REMOTE_TOKEN": token}, name="work")
    server = McpServer(
        url=http_server.URL,
        headers={"Authorization": "Bearer ${REMOTE_TOKEN}"},
        identity_header=IdentityHeader(name="X-Indus-Profile", value_from="profile"),
    )

    async def go() -> None:
        async with http_server.serving() as (app, log):
            factory = http_server.client_factory(app, log)
            async with connection(scope, server, name="remote", http_client=factory) as live:
                assert live.kind == "http"
                names = {t.name for t in await live.list_tools()}
                assert "echo" in names
                assert text_of(await live.call_tool("echo", {"text": "over http"})) == "over http"
            assert log.requests, "no request reached the app"
            first = log.requests[0]
            assert first["authorization"] == f"Bearer {token}"
            assert first["x-indus-profile"] == scope.profile_id

    asyncio.run(go())


def test_http_proxy_from_the_scope_env(tmp_path: Path) -> None:
    scope = terminal_scope(tmp_path)
    scope = scope.with_env(
        scope.env.with_overlay(
            {"HTTP_PROXY": "socks://proxy.example:1080", "NO_PROXY": "", "no_proxy": None}
        )
    )
    url = "http://mcp.corp.example:8000/mcp"

    async def go() -> None:
        async with http_server.serving() as (app, log):
            factory = http_server.client_factory(app, log)
            opts: dict[str, Any] = {"http_client": factory, "resolver": lambda h, p: ["10.1.2.3"]}
            async with connection(scope, McpServer(url=url), **opts) as live:
                await live.ping()
            assert log.clients[0]["proxy"] == "socks5://proxy.example:1080"

    asyncio.run(go())


# -- a managed stdio server (P13-04 over the real transport) ------------------------------------


def test_managed_stdio_server_respawns_after_a_crash(tmp_path: Path) -> None:
    scope = terminal_scope(tmp_path)
    options = ConnectOptions(stderr_log=StderrLog(tmp_path / "e.log"))
    server = ManagedServer("files", stdio(), scope, clock=REAL, options=options)

    async def go() -> None:
        first = await server.start()
        assert text_of(await server.call_tool("echo", {"text": "before"})) == "before"
        with pytest.raises(MCPError):
            await server.call_tool("crash")
        assert text_of(await server.call_tool("echo", {"text": "after"})) == "after"
        assert server.live is not first and server.state is ServerState.READY
        await server.close()
        assert server.state is ServerState.CLOSED

    asyncio.run(go())
