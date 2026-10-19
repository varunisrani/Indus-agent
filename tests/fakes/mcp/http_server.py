"""An in-process Streamable HTTP MCP server for tests (P13-03).

``serving(server)`` runs the server's session manager; ``client_factory(app)`` gives the
``connect.HttpClientFactory`` that talks to the app through ``httpx2.ASGITransport`` (no socket),
recording every request's headers in ``HeaderLog``.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx2
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from indus.mcp.config import RemoteEndpoint
from tests.fakes.mcp.stdio_server import build_server

URL = "http://127.0.0.1:8000/mcp"


@dataclass
class HeaderLog:
    requests: list[dict[str, str]] = field(default_factory=list[dict[str, str]])
    clients: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])


class _Recorder:
    def __init__(self, app: Any, log: HeaderLog) -> None:
        self._app = app
        self._log = log

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http":
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            self._log.requests.append(headers)
        await self._app(scope, receive, send)


@asynccontextmanager
async def serving(server: MCPServer | None = None) -> AsyncGenerator[tuple[Any, HeaderLog]]:
    """``(asgi app, header log)`` while the server's session manager runs."""
    srv = server or build_server("remote")
    log = HeaderLog()
    # Any Host header: tests dial made-up host names through the in-process transport.
    open_hosts = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    app = _Recorder(srv.streamable_http_app(transport_security=open_hosts), log)
    async with srv.session_manager.run():
        yield app, log


def client_factory(app: Any, log: HeaderLog | None = None) -> Any:
    def make(
        endpoint: RemoteEndpoint,
        *,
        proxy: str | None,
        timeout: httpx2.Timeout,
        auth: httpx2.Auth | None,
    ) -> httpx2.AsyncClient:
        if log is not None:
            log.clients.append({"proxy": proxy, "cert": endpoint.cert, "headers": endpoint.headers})
        return httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            headers=dict(endpoint.headers),
            timeout=timeout,
            auth=auth,
        )

    return make
