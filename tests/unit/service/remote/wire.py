"""TEST DOUBLES for the attach channel and the remote host (P17-16/17).

- ``AsgiTransport``: a channel ``Transport`` over the daemon's real WebSocket endpoint, driven
  in-loop through the ASGI double (``tests.fakes.asgi_ws``). ``kill()`` makes the socket vanish
  (the app sees a disconnect, the client a dead transport); ``cut_reply_to(method)`` kills it at
  the moment the answer to the next ``method`` request arrives, so the daemon did the work but the
  client never hears it.
- ``Rig``: a ``DaemonSessionHost`` over the fake model, a ticket book and the endpoint, with a
  ``dial`` that mints a fresh ticket for every dial (and remembers them).
- ``ScriptedTransport``: a transport whose inbound frames the test pushes (fake-clock tests).

No network; nothing here is product code.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from indus.foundation.scope import Scope
from indus.gateway.ws.server import WsEndpoint
from indus.service.daemon_host import DaemonSessionHost
from indus.service.remote.channel import Dialed
from indus.service.remote.ws_client import TransportClosed
from indus.service.ws_auth import SUBPROTOCOL, TICKET_PROTOCOL_PREFIX, WS_INTERACTIVE, TicketBook
from indus.state.registry import StoreHandle
from tests.fakes.asgi_ws import AsgiWsClient
from tests.fakes.model import Turn
from tests.fakes.session_host import open_host


class AsgiTransport:
    """A channel transport over the endpoint's ASGI app (see the module notes)."""

    def __init__(self, client: AsgiWsClient) -> None:
        self.client = client
        self.subprotocol: str | None = client.accepted
        self.dead = False
        self.sent: list[dict[str, Any]] = []
        self._cut_method: str | None = None
        self._cut_ids: set[object] = set()
        self.cut_fired = asyncio.Event()

    @classmethod
    async def open(cls, app: Any, protocols: list[str]) -> AsgiTransport:
        client = AsgiWsClient(app, subprotocols=protocols)
        await client.open()
        return cls(client)

    def cut_reply_to(self, method: str) -> None:
        self._cut_method = method

    async def send(self, text: str) -> None:
        if self.dead:
            raise TransportClosed(1006, "gone")
        frame = json.loads(text)
        self.sent.append(frame)
        if self._cut_method is not None and frame.get("method") == self._cut_method:
            self._cut_ids.add(frame.get("id"))
            self._cut_method = None
        await self.client.send_text(text)

    async def recv(self) -> str:
        while True:
            if self.dead:
                raise TransportClosed(1006, "gone")
            message = await self.client._from_app.get()  # pyright: ignore[reportPrivateUsage]
            if self.dead:
                raise TransportClosed(1006, "gone")
            if message["type"] == "websocket.close":
                self.dead = True
                raise TransportClosed(int(message.get("code", 1000)), str(message.get("reason")))
            text = str(message["text"])
            if self._cut_ids:
                frame = json.loads(text)
                if "method" not in frame and frame.get("id") in self._cut_ids:
                    self.cut_fired.set()
                    await self.kill()
                    raise TransportClosed(1006, "cut")
            return text

    async def kill(self) -> None:
        """The socket vanishes: the app gets a disconnect, this side a dead transport."""
        if self.dead:
            return
        self.dead = True
        await self.client._from_app.put(  # pyright: ignore[reportPrivateUsage]
            {"type": "websocket.close", "code": 1006}
        )
        await self.client.close(1006)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if not self.dead:
            self.dead = True
            await self.client.close(code)


class Rig:
    """A daemon host, its ticket book and endpoint, and a ticket-per-dial dialer."""

    def __init__(self, host: DaemonSessionHost, endpoint: WsEndpoint, book: TicketBook) -> None:
        self.host = host
        self.endpoint = endpoint
        self.book = book
        self.tickets: list[str] = []
        self.transports: list[AsgiTransport] = []
        self.reuse_first_ticket = False
        self.dials = 0
        self.dial_gate: asyncio.Event | None = None
        """When set, dials wait until it is (the test holds the reconnect back)."""

    async def dial(self) -> Dialed:
        if self.dial_gate is not None:
            await self.dial_gate.wait()
        self.dials += 1
        if self.reuse_first_ticket and self.tickets:
            ticket = self.tickets[0]
        else:
            ticket = self.book.mint(WS_INTERACTIVE).ticket
            self.tickets.append(ticket)
        transport = await AsgiTransport.open(
            self.endpoint.app, [SUBPROTOCOL, TICKET_PROTOCOL_PREFIX + ticket]
        )
        self.transports.append(transport)
        return Dialed(transport, self.book.profile_id, self.book.instance_id)

    @property
    def last(self) -> AsgiTransport:
        return self.transports[-1]

    async def close(self) -> None:
        await self.endpoint.stop()
        await self.host.aclose()


async def rig(
    scope: Scope,
    store: StoreHandle,
    tmp_path: Path,
    *turns: Turn,
    tools: dict[str, Any] | None = None,
) -> Rig:
    host = await open_host(scope, store, tmp_path, *turns, tools=tools)
    book = TicketBook.for_home(scope.home)
    endpoint = WsEndpoint(host, book, scope=scope, strict=True)
    return Rig(host, endpoint, book)


class ScriptedTransport:
    """Inbound frames come from ``push``; outbound ones land in ``sent``."""

    def __init__(self, subprotocol: str | None = SUBPROTOCOL) -> None:
        self.subprotocol = subprotocol
        self.sent: list[dict[str, Any]] = []
        self.closed: int | None = None
        self._inbound: asyncio.Queue[str | None] = asyncio.Queue()

    def push(self, frame: dict[str, Any]) -> None:
        self._inbound.put_nowait(json.dumps(frame))

    def ready(self, *, heartbeat: bool = True, profile_id: str = "p1") -> None:
        params = {
            "theme": "auto",
            "heartbeat": heartbeat,
            "protocol_version": 1,
            "replay_epoch": "e1",
            "profile_id": profile_id,
            "instance_id": "i1",
            "capabilities": [],
        }
        self.push({"jsonrpc": "2.0", "method": "gateway.ready", "params": params})

    async def send(self, text: str) -> None:
        if self.closed is not None:
            raise TransportClosed(self.closed)
        frame = json.loads(text)
        self.sent.append(frame)
        if frame.get("method") == "client.capabilities":
            self.push({"jsonrpc": "2.0", "id": frame["id"], "result": {"features": []}})

    async def recv(self) -> str:
        text = await self._inbound.get()
        if text is None:
            raise TransportClosed(self.closed or 1006)
        return text

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self.closed is None:
            self.closed = code
            self._inbound.put_nowait(None)

    def pings(self) -> list[dict[str, Any]]:
        return [f for f in self.sent if f.get("method") == "gateway.ping"]
