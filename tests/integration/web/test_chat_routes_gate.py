"""P18-01 + P18-06 + P18-09: every chat WebSocket route of the real app, dialled over a real
loopback server through the real helper — the credentials each one takes, the ones it refuses,
and that no credential is ever taken from the URL.

The machine-facing pieces (PTY spawn, daemon dial) are fakes; the app factory, the helper, the
ticket book and the routes are the product's.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from indus.foundation.scope import Scope
from indus.service.remote.channel import Dial, Dialed
from indus.service.ws_auth import (
    DASHBOARD_SUBPROTOCOL,
    WEB_EVENTS,
    WEB_PTY,
    WEB_RPC,
    CredentialKind,
)
from indus.web.ws import websocket_routes
from indus.web.ws.chat_gate import purpose_of
from indus.web.ws.chat_routes import ChatSeams
from indus.web.ws.pty_route import ChildRequest, ChildSpec
from tests.contract.web.chat_rig import ChatServer, chat_server
from tests.contract.web.test_rpc_sidecar import READY, FakeTransport
from tests.fakes.pty.fake_bridge import FakeBridge
from tests.fakes.pty.ws_wire import WireClient
from tests.unit.backends._aio import aio

ROUTES = {"/api/pty": WEB_PTY, "/api/events": WEB_EVENTS, "/api/pub": "web-pub", "/api/ws": WEB_RPC}
QUERY = {"/api/pty": "", "/api/events": "channel=c1", "/api/pub": "channel=c1", "/api/ws": ""}


class Machine:
    """Fake PTYs and a fake daemon behind the real routes."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.bridges: list[FakeBridge] = []
        self.dials = 0

    def child(self, request: ChildRequest) -> ChildSpec:
        return ChildSpec(("fake-tui",), self.tmp, {})

    def spawn(self, argv: tuple[str, ...], *, cwd: Path, env: dict[str, str]) -> FakeBridge:
        bridge = FakeBridge()
        self.bridges.append(bridge)
        return bridge

    def dial_for(self, scope: Scope) -> Dial:
        async def dial() -> Dialed:
            self.dials += 1
            transport = FakeTransport()
            transport.feed(READY)
            return Dialed(transport, "p" * 16, "i" * 16)

        return dial

    def seams(self) -> ChatSeams:
        return ChatSeams(
            child=self.child,
            spawn=self.spawn,
            unavailable=lambda: None,
            lease_holder=lambda _scope, _sid: None,
            dial_for=self.dial_for,
        )


def _url(server: ChatServer, path: str, extra: str = "") -> str:
    query = "&".join(part for part in (QUERY[path], extra) if part)
    return f"{server.base}{path}" + (f"?{query}" if query else "")


async def _opened(client: WireClient, path: str) -> None:
    """The socket is open and its route answered (the route's own first word)."""
    assert client.subprotocol == DASHBOARD_SUBPROTOCOL
    if path == "/api/ws":
        assert json.loads(await client.recv())["method"] == "gateway.ready"
    elif path == "/api/pty":
        await client.send_bytes(b"k")


async def _code(server: ChatServer, path: str, offer: list[str], extra: str = "") -> int:
    client = await WireClient.connect(_url(server, path, extra), offer)
    return (await client.closed_with()).code


@aio
async def test_every_chat_route_is_mounted_behind_the_helper(scope: Scope, tmp_path: Path) -> None:
    async with chat_server(scope, seams=Machine(tmp_path).seams()) as server:
        mounted = {path: purpose_of(endpoint) for path, endpoint in websocket_routes(server.app)}
        for path, purpose in ROUTES.items():
            assert mounted[path] == purpose, path
            assert await _code(server, path, []) == 4401, path  # nothing offered
            assert await _code(server, path, [DASHBOARD_SUBPROTOCOL]) == 4401, path


@aio
async def test_loopback_mode_credentials_per_route(scope: Scope, tmp_path: Path) -> None:
    machine = Machine(tmp_path)
    allowed: dict[str, tuple[CredentialKind, ...]] = {
        "/api/pty": ("token",),
        "/api/events": ("token",),
        "/api/pub": ("internal",),
        "/api/ws": ("token", "internal"),
    }
    async with chat_server(scope, seams=machine.seams()) as server:
        for path, kinds in allowed.items():
            for kind in ("token", "internal"):
                offer = server.offer(kind)
                if kind in kinds:
                    client = await WireClient.connect(_url(server, path), offer)
                    await _opened(client, path)
                    await client.close()
                else:
                    assert await _code(server, path, offer) == 4401, (path, kind)
            wrong = server.offer("token", "x" * 43)
            assert await _code(server, path, wrong) == 4401, path
            # a right credential in the URL is refused, with or without one in the subprotocols
            secret = server.internal if path == "/api/pub" else server.state.auth.token.value
            key = "internal" if path == "/api/pub" else "token"
            assert await _code(server, path, [DASHBOARD_SUBPROTOCOL], f"{key}={secret}") == 4401
            assert await _code(server, path, server.offer(kinds[0]), f"{key}={secret}") == 4401
        assert machine.dials == 2  # the token and the internal sidecar dials, nothing else
        assert len(machine.bridges) == 1


@aio
async def test_gated_mode_takes_one_ticket_per_socket(scope: Scope, tmp_path: Path) -> None:
    machine = Machine(tmp_path)
    async with chat_server(scope, gated=True, seams=machine.seams()) as server:
        for path, purpose in ROUTES.items():
            token = server.offer("token")
            assert await _code(server, path, token) == 4401, path  # never the token here
            if path == "/api/pub":
                ticket = server.offer("ticket", purpose=purpose)
                assert await _code(server, path, ticket) == 4401  # a person cannot publish
                client = await WireClient.connect(_url(server, path), server.offer("internal"))
                await _opened(client, path)
                await client.close()
                continue
            ticket = server.offer("ticket", purpose=purpose)
            client = await WireClient.connect(_url(server, path), ticket)
            await _opened(client, path)
            assert await _code(server, path, ticket) == 4401, path  # spent
            await client.close()
            other = WEB_RPC if purpose != WEB_RPC else WEB_PTY
            assert await _code(server, path, server.offer("ticket", purpose=other)) == 4401
            in_url = server.offer("ticket", purpose=purpose)[1].rpartition(".")[2]
            assert await _code(server, path, [DASHBOARD_SUBPROTOCOL], f"ticket={in_url}") == 4401
            assert await _code(server, path, server.offer("ticket", in_url)) == 4401  # spent too


@aio
async def test_the_internal_credential_reaches_the_child_but_not_the_url(
    scope: Scope, tmp_path: Path
) -> None:
    seen: list[ChildRequest] = []
    machine = Machine(tmp_path)

    def child(request: ChildRequest) -> ChildSpec:
        seen.append(request)
        return ChildSpec(("fake-tui",), tmp_path, {})

    seams = machine.seams()
    seams.child = child
    async with chat_server(scope, seams=seams) as server:
        client = await WireClient.connect(
            f"{server.base}/api/pty?channel=c1", server.offer("token")
        )
        await client.send_bytes(b"k")
        await client.close()
    request: Any = seen[0]
    assert request.sidecar_url == f"{server.base}/api/pub?channel=c1"
    assert request.sidecar_credential == server.internal
    assert server.internal not in request.sidecar_url
