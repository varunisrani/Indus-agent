"""P17-13: the WebSocket endpoint on a real loopback listener (uvicorn + wsproto).

Runs only where a WebSocket implementation for uvicorn is installed (``wsproto`` or
``websockets``; see the endpoint's dependency note). The client is a few lines of RFC 6455 over a
raw loopback socket, so no client library is needed. The ticket comes from the real attach verb
(``session-ticket``) with the endpoint's ``ws_url``.
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
import struct
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.gateway.daemon.attach_verbs import AttachVerbs
from indus.gateway.ws import server as ws_server
from indus.gateway.ws.server import WsEndpoint, WsUnavailable
from indus.service.gateway_control import ws_url_is_safe
from indus.service.ws_auth import (
    CLOSE_BAD_TICKET,
    SUBPROTOCOL,
    TICKET_PROTOCOL_PREFIX,
    WS_INTERACTIVE,
    TicketBook,
)
from indus.state import registry
from indus.state.registry import StoreHandle
from tests.fakes.session_host import open_host
from tests.unit.backends._aio import aio

HAS_WS = any(importlib.util.find_spec(name) for name in ("wsproto", "websockets"))
needs_ws = pytest.mark.skipif(not HAS_WS, reason="no WebSocket implementation installed")


@pytest.fixture
def store(scope: Scope) -> Iterator[StoreHandle]:
    handle = registry.open_unshared(scope)
    yield handle
    handle.close()


class RawClient:
    """The client half of RFC 6455, just enough for these tests."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer

    @classmethod
    async def dial(cls, url: str, protocols: list[str]) -> tuple[RawClient, str]:
        rest = url.removeprefix("ws://")
        address, _, path = rest.partition("/")
        host, _, port = address.rpartition(":")
        reader, writer = await asyncio.open_connection(host, int(port))
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET /{path} HTTP/1.1\r\nHost: {address}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
            f"Sec-WebSocket-Protocol: {', '.join(protocols)}\r\n\r\n"
        )
        writer.write(request.encode())
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        return cls(reader, writer), head.decode("latin-1")

    async def frame(self) -> tuple[int, bytes]:
        first, second = await asyncio.wait_for(self.reader.readexactly(2), 5)
        size = second & 0x7F
        if size == 126:
            (size,) = struct.unpack("!H", await self.reader.readexactly(2))
        elif size == 127:
            (size,) = struct.unpack("!Q", await self.reader.readexactly(8))
        return first & 0x0F, await self.reader.readexactly(size)

    async def recv(self) -> dict[str, Any]:
        opcode, payload = await self.frame()
        assert opcode == 1, (opcode, payload)
        value = json.loads(payload)
        assert isinstance(value, dict)
        return value  # pyright: ignore[reportUnknownVariableType]

    async def send(self, value: object, opcode: int = 1) -> None:
        data = json.dumps(value).encode() if opcode == 1 else bytes(value)  # type: ignore[arg-type]
        mask = os.urandom(4)
        header = bytes([0x80 | opcode])
        if len(data) < 126:
            header += bytes([0x80 | len(data)])
        else:
            header += bytes([0x80 | 126]) + struct.pack("!H", len(data))
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.writer.write(header + mask + body)
        await self.writer.drain()

    async def close(self) -> None:
        with_code = struct.pack("!H", 1000)
        await self.send(list(with_code), opcode=8)
        self.writer.close()


async def listening(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> tuple[WsEndpoint, AttachVerbs, Any]:
    host = await open_host(scope, store, tmp_path)
    book = TicketBook.for_home(scope.home)
    endpoint = WsEndpoint(host, book, scope=scope, strict=True)
    verbs = AttachVerbs(scope.home, book, host=host, ws_url=lambda: endpoint.ws_url)
    return endpoint, verbs, host


@needs_ws
@aio
async def test_a_real_listener_on_loopback_serves_an_attach(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    endpoint, verbs, host = await listening(scope, store, tmp_path)
    url = await endpoint.start()
    try:
        assert ws_url_is_safe(url) and url.startswith("ws://127.0.0.1:") and url.endswith("/ws")
        row = endpoint.status_row()
        assert row["state"] == "listening" and row["url"] == url and row["port"] > 0
        described = await verbs.describe({})
        assert described["ws_url"] == url and described["capabilities"]
        minted = await verbs.session_ticket({"purpose": WS_INTERACTIVE})
        ticket = str(minted["ticket"])
        client, head = await RawClient.dial(url, [SUBPROTOCOL, TICKET_PROTOCOL_PREFIX + ticket])
        assert head.startswith("HTTP/1.1 101")
        assert f"sec-websocket-protocol: {SUBPROTOCOL}" in head.lower()
        assert ticket not in head
        ready = await client.recv()
        assert ready["method"] == "gateway.ready"
        assert ready["params"]["instance_id"] == minted["instance_id"]
        await client.send({"jsonrpc": "2.0", "id": 1, "method": "gateway.ping"})
        pong = await client.recv()
        assert pong["id"] == 1 and pong["result"]["pong"] is True
        await client.close()

        again, _head = await RawClient.dial(url, [SUBPROTOCOL, TICKET_PROTOCOL_PREFIX + ticket])
        opcode, payload = await again.frame()
        assert opcode == 8 and struct.unpack("!H", payload[:2])[0] == CLOSE_BAD_TICKET
        again.writer.close()
    finally:
        await endpoint.stop()
        await host.aclose()
    assert endpoint.ws_url is None and endpoint.status_row() == {"state": "stopped"}


@aio
async def test_the_endpoint_listens_on_loopback_only_and_needs_an_implementation(
    scope: Scope, store: StoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    endpoint, _verbs, host = await listening(scope, store, tmp_path)
    try:
        with pytest.raises(ValueError, match="loopback"):
            await endpoint.start(bind="0.0.0.0")
        monkeypatch.setattr(ws_server.importlib.util, "find_spec", lambda name: None)
        with pytest.raises(WsUnavailable, match="wsproto"):
            await endpoint.start()
        assert endpoint.ws_url is None
    finally:
        await host.aclose()
