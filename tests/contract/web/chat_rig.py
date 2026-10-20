"""The real dashboard app with its chat routes, served on 127.0.0.1 for the chat contract tests.

``chat_server(scope, ...)`` builds the app with ``indus.web.app.create_app`` (serve mode, so no SPA
bundle is needed), runs its lifespan (the PTY reaper; at the end every PTY ended and the channel
files removed) and serves it on a free loopback port. Every socket passes the real WebSocket helper
(``indus.web.ws.guarded_ws``) with real credentials, offered as subprotocols the way the SPA and the
TUI child offer them: ``offer("token")`` is the loopback session token, ``offer("ticket",
purpose=...)`` a single-use ticket from the app's own book (gated mode), ``offer("internal")`` the
process-lifetime credential of a server-spawned child.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI

from indus.foundation.clock import SystemClock
from indus.foundation.scope import Scope
from indus.gateway.http.serving import HttpServer, bind_listener
from indus.service.ws_auth import WEB_PTY, CredentialKind, dashboard_offer
from indus.web.app import AppOptions, create_app
from indus.web.state import DashboardState
from indus.web.ws.chat_routes import ChatRoutes, ChatSeams
from tests.unit.web.conftest import basic_config

__all__ = ["ChatServer", "chat_server"]


@dataclass
class ChatServer:
    app: FastAPI
    state: DashboardState
    base: str = ""

    @property
    def chat(self) -> ChatRoutes:
        found: Any = self.state.extras["chat"]
        assert isinstance(found, ChatRoutes)
        return found

    @property
    def internal(self) -> str:
        return self.state.internal.value

    def offer(
        self, kind: CredentialKind, value: str | None = None, *, purpose: str = WEB_PTY
    ) -> list[str]:
        if value is None:
            if kind == "token":
                value = self.state.auth.token.value
            elif kind == "internal":
                value = self.internal
            else:
                value = self.state.tickets.mint(purpose).ticket
        return dashboard_offer(kind, value)


@contextlib.asynccontextmanager
async def chat_server(
    scope: Scope,
    *,
    gated: bool = False,
    enabled: bool = True,
    seams: ChatSeams | None = None,
) -> AsyncIterator[ChatServer]:
    """Serve the real app for ``scope`` on 127.0.0.1; ``gated`` builds it as an all-interfaces
    server with a password login (so tickets, not the token, open sockets)."""
    sock = await bind_listener("127.0.0.1", 0, SystemClock())
    port = int(sock.getsockname()[1])
    app = create_app(
        scope,
        "serve",
        options=AppOptions(
            host="0.0.0.0" if gated else "127.0.0.1",
            port=port,
            config=basic_config() if gated else {},
            embedded_chat=enabled,
            chat=seams,
        ),
    )
    server = ChatServer(app, app.state.indus, f"ws://127.0.0.1:{port}")
    http = HttpServer(app, sock, name="web-test", ws={"ws": "wsproto", "ws_max_size": 4 << 20})
    async with app.router.lifespan_context(app):
        await http.start()
        try:
            yield server
        finally:
            await http.stop()
