"""Connection pieces without spawning processes (P13-03): timeouts, URL gate, proxies, mTLS
errors, the stderr log, handshake fallback and paging."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

import httpx2
import mcp_types as types
import pytest
from mcp.shared.exceptions import MCPError

from indus.foundation.scope import Scope
from indus.mcp import connect as mc
from indus.mcp.config import McpClientCertMissing, McpServer, McpUnresolvedRef, RemoteEndpoint
from indus.mcp.connect import (
    Connection,
    ConnectOptions,
    LiveSession,
    McpConnectFailed,
    McpConnectTimeout,
    McpUrlRefused,
    StderrLog,
    check_endpoint,
    clock_timeout,
    first_cause,
    is_permanent,
    open_session,
    proxy_for_url,
)
from tests.fakes.clock import FakeClock, checkpoint
from tests.fakes.terminal import forge_token, terminal_scope


def resolver_for(answers: Mapping[str, Sequence[str]]) -> Any:
    def resolve(host: str, port: int | None) -> Sequence[str]:
        if host not in answers:
            raise socket.gaierror(-2, "Name or service not known")
        return answers[host]

    return resolve


class FactoryLog:
    def __init__(self, app: Any) -> None:
        self.app = app
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        endpoint: RemoteEndpoint,
        *,
        proxy: str | None,
        timeout: httpx2.Timeout,
        auth: httpx2.Auth | None,
    ) -> httpx2.AsyncClient:
        self.calls.append(
            {"headers": dict(endpoint.headers), "proxy": proxy, "cert": endpoint.cert}
        )
        return httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=self.app), headers=dict(endpoint.headers)
        )


async def silent_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    await asyncio.Event().wait()


async def not_found_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    if scope["type"] != "http":
        return
    await send({"type": "http.response.start", "status": 404, "headers": []})
    await send({"type": "http.response.body", "body": b"nothing here"})


# -- clock_timeout ------------------------------------------------------------------------------


def test_clock_timeout_fires_on_the_injected_clock(fake_clock: FakeClock) -> None:
    async def go() -> None:
        async def slow() -> None:
            async with clock_timeout(fake_clock, 10):
                await asyncio.Event().wait()

        task = asyncio.ensure_future(slow())
        await fake_clock.run_until_idle()
        await fake_clock.advance_and_settle(9)
        assert not task.done()
        await fake_clock.advance_and_settle(1)
        with pytest.raises(TimeoutError):
            await task

        async with clock_timeout(fake_clock, 10):
            await checkpoint()
        await checkpoint()
        assert fake_clock.pending == 0

    asyncio.run(go())


# -- open_session failures before any network ---------------------------------------------------


def test_connect_timeout_on_a_server_that_never_answers(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    scope = terminal_scope(tmp_path, clock=fake_clock)
    factory = FactoryLog(silent_app)
    server = McpServer(url="http://127.0.0.1:9/mcp", connect_timeout=60)

    async def go() -> None:
        conn = Connection(
            "silent", server, scope, clock=fake_clock, options=ConnectOptions(http_client=factory)
        )
        starting = asyncio.ensure_future(conn.start())
        await fake_clock.run_until_idle()
        await fake_clock.advance_and_settle(59)
        assert not starting.done()
        await fake_clock.advance_and_settle(1)
        with pytest.raises(McpConnectTimeout, match="'silent' did not finish connecting within 60"):
            await starting
        await conn.close()

    asyncio.run(go())
    assert len(factory.calls) == 1


def test_missing_client_certificate_names_the_server(tmp_path: Path, fake_clock: FakeClock) -> None:
    scope = terminal_scope(tmp_path, clock=fake_clock)
    factory = FactoryLog(not_found_app)
    server = McpServer(url="https://mcp.example/mcp", client_cert=str(tmp_path / "missing.pem"))

    async def go() -> None:
        with pytest.raises(McpClientCertMissing, match="'corp'.*missing.pem"):
            async with open_session(
                "corp", server, scope, clock=fake_clock, options=ConnectOptions(http_client=factory)
            ):
                pass

    asyncio.run(go())
    assert factory.calls == []


def test_unset_header_variable_refuses_before_any_request(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    scope = terminal_scope(tmp_path, clock=fake_clock)
    scope = scope.with_env(scope.env.with_overlay({"MISSING_TOKEN": None}))
    factory = FactoryLog(not_found_app)
    server = McpServer(url="https://mcp.example/mcp", headers={"Authorization": "${MISSING_TOKEN}"})

    async def go() -> None:
        async with open_session(
            "remote", server, scope, clock=fake_clock, options=ConnectOptions(http_client=factory)
        ):
            pass

    with pytest.raises(McpUnresolvedRef):
        asyncio.run(go())
    assert factory.calls == []


def test_http_error_answer_is_a_connect_failure(tmp_path: Path, fake_clock: FakeClock) -> None:
    scope = terminal_scope(tmp_path, clock=fake_clock)
    factory = FactoryLog(not_found_app)

    async def go() -> None:
        async with open_session(
            "remote",
            McpServer(url="http://127.0.0.1:9/mcp"),
            scope,
            clock=fake_clock,
            options=ConnectOptions(http_client=factory),
        ):
            pass

    with pytest.raises(McpConnectFailed, match="'remote' could not connect"):
        asyncio.run(go())


def test_sse_transport_builds_its_client_through_the_factory(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    scope = terminal_scope(tmp_path, clock=fake_clock, secrets={"TOK": "t"})
    factory = FactoryLog(not_found_app)
    server = McpServer(
        url="http://127.0.0.1:9/sse", transport="sse", headers={"Authorization": "Bearer ${TOK}"}
    )

    async def go() -> None:
        async with open_session(
            "legacy", server, scope, clock=fake_clock, options=ConnectOptions(http_client=factory)
        ):
            pass

    with pytest.raises(McpConnectFailed):
        asyncio.run(go())
    assert factory.calls and factory.calls[0]["headers"]["Authorization"] == "Bearer t"


# -- the URL gate -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "answers", "needle"),
    [
        ("ftp://mcp.example/x", {}, "http or https"),
        ("http:///mcp", {}, "no host"),
        ("http://metadata.google.internal/mcp", {}, "metadata host"),
        ("http://169.254.169.254/latest", {}, "metadata address"),
        ("http://[fd00:ec2::254]/mcp", {}, "metadata address"),
        (
            "http://sneaky.example/mcp",
            {"sneaky.example": ["10.0.0.1", "169.254.169.254"]},
            "metadata",
        ),
        ("http://mcp.example:99999/mcp", {}, "does not parse"),
    ],
)
def test_url_gate_refuses(
    tmp_path: Path, url: str, answers: dict[str, list[str]], needle: str
) -> None:
    scope = terminal_scope(tmp_path)
    with pytest.raises(McpUrlRefused, match=needle):
        asyncio.run(check_endpoint("srv", url, scope, resolver=resolver_for(answers)))


@pytest.mark.parametrize(
    ("url", "answers"),
    [
        ("http://localhost:3000/mcp", {"localhost": ["127.0.0.1"]}),
        ("http://127.0.0.1:3000/mcp", {}),
        ("https://mcp.lan/mcp", {"mcp.lan": ["192.168.1.20"]}),
        ("https://unresolvable.example/mcp", {}),
        ("https://mcp.example/mcp?api_key=SEKRET", {"mcp.example": ["93.184.216.34"]}),
    ],
)
def test_url_gate_allows_operator_endpoints(
    tmp_path: Path, url: str, answers: dict[str, list[str]]
) -> None:
    scope = terminal_scope(tmp_path)
    asyncio.run(check_endpoint("srv", url, scope, resolver=resolver_for(answers)))


def test_url_gate_honours_the_website_blocklist(tmp_path: Path) -> None:
    config = {"security": {"website_blocklist": {"enabled": True, "domains": ["*.blocked.test"]}}}
    scope = terminal_scope(tmp_path, config)
    with pytest.raises(McpUrlRefused, match="blocked.test"):
        asyncio.run(check_endpoint("srv", "https://mcp.blocked.test/mcp", scope))


# -- proxies ------------------------------------------------------------------------------------

VIA = {"HTTPS_PROXY": "http://p"}


@pytest.mark.parametrize(
    ("url", "env", "expected"),
    [
        ("https://mcp.example/x", {}, None),
        ("https://mcp.example/x", {"HTTPS_PROXY": "http://p:3128"}, "http://p:3128"),
        ("http://mcp.example/x", {"HTTPS_PROXY": "http://p:3128"}, None),
        ("http://mcp.example/x", {"http_proxy": "p:3128"}, "http://p:3128"),
        ("https://mcp.example/x", {"ALL_PROXY": "socks://s:1080"}, "socks5://s:1080"),
        ("https://localhost/x", VIA, None),
        ("https://127.0.0.5/x", VIA, None),
        ("https://[::1]/x", VIA, None),
        (
            "https://a.corp.example/x",
            {**VIA, "NO_PROXY": ".corp.example"},
            None,
        ),
        (
            "https://a.corp.example/x",
            {**VIA, "NO_PROXY": "*.corp.example"},
            None,
        ),
        ("https://corp.example/x", {**VIA, "no_proxy": "corp.example"}, None),
        (
            "https://notcorp.example/x",
            {**VIA, "NO_PROXY": "corp.example"},
            "http://p",
        ),
        ("https://10.2.3.4/x", {**VIA, "NO_PROXY": "10.0.0.0/8"}, None),
        ("https://11.2.3.4/x", {**VIA, "NO_PROXY": "10.0.0.0/8"}, "http://p"),
        (
            "https://h.example:8443/x",
            {**VIA, "NO_PROXY": "h.example:8443"},
            None,
        ),
        (
            "https://h.example/x",
            {**VIA, "NO_PROXY": "h.example:8443"},
            "http://p",
        ),
        ("https://any.example/x", {**VIA, "NO_PROXY": "*"}, None),
        (
            "https://b.example/x",
            {**VIA, "NO_PROXY": "a.example, b.example"},
            None,
        ),
    ],
)
def test_proxy_choice(url: str, env: dict[str, str], expected: str | None) -> None:
    assert proxy_for_url(url, env) == expected


# -- stderr log ---------------------------------------------------------------------------------


def test_stderr_log_stamps_redacts_and_rotates(tmp_path: Path) -> None:
    path = tmp_path / "logs" / "mcp-stderr.log"
    sink = StderrLog(path, max_bytes=400, backups=2)
    secret = forge_token()
    sink.write("files", [f"token={secret}"])
    text = path.read_text(encoding="utf-8")
    assert "[files] token=" in text and secret not in text
    for i in range(30):
        sink.write("files", [f"line {i:02d} " + "x" * 30])
    rotated = sorted(p.name for p in path.parent.iterdir())
    assert rotated == ["mcp-stderr.log", "mcp-stderr.log.1", "mcp-stderr.log.2"]
    assert all(p.stat().st_size <= 400 for p in path.parent.iterdir())
    assert "line 29" in path.read_text(encoding="utf-8")


def test_stderr_tap_pumps_lines_and_keeps_a_tail(tmp_path: Path) -> None:
    sink = StderrLog(tmp_path / "e.log")
    tap = sink.tap("srv")
    tap.sink.write(b"first line\nsecond ")
    tap.sink.write(b"half\nno newline at the end")
    tail = tap.close(wait=5)
    text = (tmp_path / "e.log").read_text(encoding="utf-8")
    assert "--- starting MCP server srv ---" in text
    assert "[srv] first line" in text and "[srv] second half" in text
    assert "[srv] no newline at the end" in text
    assert tail.endswith("no newline at the end")


def test_stderr_log_is_shared_per_home(tmp_path: Path) -> None:
    one = StderrLog.for_home(tmp_path / "a")
    assert StderrLog.for_home(tmp_path / "a") is one
    assert one.path == tmp_path / "a" / "logs" / "mcp-stderr.log"
    assert StderrLog.for_home(tmp_path / "b") is not one


# -- errors -------------------------------------------------------------------------------------


def test_first_cause_unwraps_nested_groups() -> None:
    inner = ConnectionResetError("reset")
    group = ExceptionGroup("outer", [ExceptionGroup("inner", [inner])])
    assert first_cause(group) is inner
    mixed = BaseExceptionGroup("mixed", [asyncio.CancelledError(), ValueError("v")])
    assert isinstance(first_cause(mixed), ValueError)


def test_permanent_errors() -> None:
    request = httpx2.Request("POST", "https://a/mcp")
    unauthorized = httpx2.HTTPStatusError(
        "401", request=request, response=httpx2.Response(401, request=request)
    )
    busy = httpx2.HTTPStatusError(
        "503", request=request, response=httpx2.Response(503, request=request)
    )
    assert is_permanent(FileNotFoundError("no such file"))
    assert is_permanent(McpUrlRefused("refused"))
    assert is_permanent(ExceptionGroup("g", [unauthorized]))
    assert not is_permanent(busy)
    assert not is_permanent(ConnectionRefusedError())
    assert not is_permanent(McpConnectTimeout("slow"))
    assert is_permanent(McpConnectFailed("x", permanent=True))


# -- session pieces -----------------------------------------------------------------------------


class HandshakeRaw:
    def __init__(self, error: MCPError | None) -> None:
        self.error = error
        self.discovered = False

    async def initialize(self) -> None:
        if self.error is not None:
            raise self.error

    async def discover(self) -> None:
        self.discovered = True


@pytest.mark.parametrize(
    ("code", "discovers"),
    [(types.METHOD_NOT_FOUND, True), (types.UNSUPPORTED_PROTOCOL_VERSION, True), (-32603, None)],
)
def test_handshake_falls_back_to_discover_only_when_initialize_is_refused(
    code: int, discovers: bool | None
) -> None:
    raw = HandshakeRaw(MCPError(code, "no"))
    if discovers is None:
        with pytest.raises(MCPError):
            asyncio.run(mc._handshake(cast(Any, raw)))  # pyright: ignore[reportPrivateUsage]
        assert not raw.discovered
    else:
        asyncio.run(mc._handshake(cast(Any, raw)))  # pyright: ignore[reportPrivateUsage]
        assert raw.discovered


class PagingRaw:
    def __init__(self, pages: int | None) -> None:
        self.pages = pages
        self.requests = 0

    async def list_tools(
        self, *, params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        self.requests += 1
        more = self.pages is None or self.requests < self.pages
        tool = types.Tool(name=f"t{self.requests}", input_schema={"type": "object"})
        return types.ListToolsResult(
            tools=[tool], next_cursor=f"c{self.requests}" if more else None
        )


def test_tool_listing_follows_cursors_and_stops_at_50_pages(
    caplog: pytest.LogCaptureFixture,
) -> None:
    three = PagingRaw(3)
    live = LiveSession("s", "stdio", cast(Any, three))
    assert [t.name for t in asyncio.run(live.list_tools())] == ["t1", "t2", "t3"]
    endless = PagingRaw(None)
    live = LiveSession("s", "stdio", cast(Any, endless))
    tools = asyncio.run(live.list_tools())
    assert len(tools) == 50 and endless.requests == 50
    assert any("50 pages" in r.getMessage() for r in caplog.records)


def test_notifications_reach_the_handler_and_stream_errors_do_not() -> None:
    seen: list[tuple[str, str]] = []

    async def handler(name: str, note: Any) -> None:
        seen.append((name, type(note).__name__))

    deliver = mc._message_handler("srv", handler)  # pyright: ignore[reportPrivateUsage]
    note = types.ToolListChangedNotification()

    async def go() -> None:
        await deliver(note)
        await deliver(RuntimeError("bad frame"))

    asyncio.run(go())
    assert seen == [("srv", "ToolListChangedNotification")]


def test_a_connection_starts_once(tmp_path: Path, fake_clock: FakeClock) -> None:
    scope: Scope = terminal_scope(tmp_path, clock=fake_clock)
    conn = Connection("s", McpServer(url="ftp://x/y"), scope, clock=fake_clock)

    async def go() -> None:
        with pytest.raises(McpUrlRefused):
            await conn.start()
        with pytest.raises(RuntimeError, match="starts once"):
            await conn.start()
        await conn.close()

    asyncio.run(go())


def test_module_exports_are_real() -> None:
    assert all(hasattr(mc, name) for name in mc.__all__)
