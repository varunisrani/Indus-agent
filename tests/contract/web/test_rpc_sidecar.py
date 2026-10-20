"""P18-09: ``/api/ws``, the JSON-RPC sidecar relayed to the profile's daemon.

The daemon side is a fake transport behind a fake dial (the real dial is P17's
``home_dialer``); the browser side is a real loopback socket into the real app (``chat_rig``)
through the real WebSocket helper.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from indus.foundation.clock import SystemClock
from indus.foundation.scope import Scope
from indus.service.remote.channel import Dial, Dialed, DialRefused
from indus.service.ws_auth import WEB_RPC
from indus.web.ws.chat_gate import PURPOSE_RPC, purpose_of
from indus.web.ws.chat_routes import ChatSeams
from indus.web.ws.rpc_sidecar import daemon_dial
from tests.contract.web.chat_rig import ChatServer, chat_server
from tests.fakes.pty.ws_wire import WireClient
from tests.unit.backends._aio import aio

READY = json.dumps({"jsonrpc": "2.0", "method": "gateway.ready", "params": {"heartbeat": 15}})


async def until(check: Any, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not check():
            await SystemClock().sleep(0.01)


class FakeTransport:
    """The daemon end: ``feed`` what the daemon says; ``sent`` is what reached it."""

    def __init__(self) -> None:
        self.subprotocol = "indus-gateway-v1"
        self.sent: list[str] = []
        self.closed: list[int] = []
        self._inbox: asyncio.Queue[str | None] = asyncio.Queue()

    def feed(self, text: str) -> None:
        self._inbox.put_nowait(text)

    def drop(self) -> None:
        self._inbox.put_nowait(None)

    async def send(self, text: str) -> None:
        self.sent.append(text)

    async def recv(self) -> str:
        item = await self._inbox.get()
        if item is None:
            raise ConnectionError("daemon gone")
        return item

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed.append(code)
        self._inbox.put_nowait(None)


class Daemon:
    def __init__(self, *, refuse: BaseException | None = None) -> None:
        self.refuse = refuse
        self.transports: list[FakeTransport] = []
        self.homes: list[Path] = []

    def dial_for(self, scope: Scope) -> Dial:
        async def dial() -> Dialed:
            self.homes.append(scope.home)
            if self.refuse is not None:
                raise self.refuse
            transport = FakeTransport()
            transport.feed(READY)
            self.transports.append(transport)
            return Dialed(transport, "p" * 16, "i" * 16)

        return dial


def _serve(scope: Scope, daemon: Daemon, **options: Any) -> Any:
    return chat_server(scope, seams=ChatSeams(dial_for=daemon.dial_for), **options)


def _url(server: ChatServer, query: str = "") -> str:
    return f"{server.base}/api/ws{query}"


@aio
async def test_frames_are_relayed_both_ways(scope: Scope) -> None:
    daemon = Daemon()
    async with _serve(scope, daemon) as server:
        client = await WireClient.connect(_url(server), server.offer("token"))
        assert json.loads(await client.recv())["method"] == "gateway.ready"
        request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "client.capabilities"})
        await client.send_text(request)
        upstream = daemon.transports[0]
        await until(lambda: upstream.sent == [request])
        upstream.feed('{"jsonrpc": "2.0", "id": 1, "result": {}}')
        assert json.loads(await client.recv()) == {"jsonrpc": "2.0", "id": 1, "result": {}}
        await client.close()
        await until(lambda: upstream.closed == [1000])  # the browser left: the daemon link goes
        assert purpose_of(server.chat.rpc.endpoint) == PURPOSE_RPC
    assert daemon.homes == [scope.home]


@aio
async def test_privileged_calls_need_a_person_s_credential(scope: Scope) -> None:
    daemon = Daemon()
    privileged = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "maintenance.run"})
    async with _serve(scope, daemon) as server:
        child = await WireClient.connect(_url(server), server.offer("internal"))
        await child.recv()  # ready
        await child.send_text(privileged)
        answer = json.loads(await child.recv())
        assert answer["id"] == 7 and answer["error"]["code"] == -32600
        await child.send_text(json.dumps({"jsonrpc": "2.0", "method": "maintenance.run"}))
        await child.send_text('{"jsonrpc": "2.0", "id": 8, "method": "session.list"}')
        await until(lambda: len(daemon.transports[0].sent) == 1)
        assert "maintenance.run" not in daemon.transports[0].sent[0]
        person = await WireClient.connect(_url(server), server.offer("token"))
        await person.recv()
        await person.send_text(privileged)
        await until(lambda: daemon.transports[1].sent == [privileged])
        await child.close()
        await person.close()


@aio
async def test_gated_mode_refuses_the_loopback_token(scope: Scope) -> None:
    daemon = Daemon()
    async with _serve(scope, daemon, gated=True) as server:
        refused = await WireClient.connect(_url(server), server.offer("token"))
        assert (await refused.closed_with()).code == 4401
        ticket = server.offer("ticket", purpose=WEB_RPC)
        allowed = await WireClient.connect(_url(server), ticket)
        assert json.loads(await allowed.recv())["method"] == "gateway.ready"
        reused = await WireClient.connect(_url(server), ticket)
        assert (await reused.closed_with()).code == 4401  # single use
        await allowed.close()
    assert len(daemon.transports) == 1


@aio
async def test_an_unreachable_daemon_closes_1011(scope: Scope) -> None:
    for refuse in (DialRefused("no_daemon", "nothing answers"), OSError("boom")):
        async with _serve(scope, Daemon(refuse=refuse)) as server:
            client = await WireClient.connect(_url(server), server.offer("token"))
            closed = await client.closed_with()
            assert closed.code == 1011 and closed.reason == "gateway unavailable"


@aio
async def test_a_lost_daemon_link_closes_the_browser_1011(scope: Scope) -> None:
    daemon = Daemon()
    async with _serve(scope, daemon) as server:
        client = await WireClient.connect(_url(server), server.offer("token"))
        await client.recv()
        daemon.transports[0].drop()
        closed = await client.closed_with()
        assert closed.code == 1011 and closed.reason == "gateway connection lost"


@aio
async def test_frame_limits_and_unknown_profile(scope: Scope) -> None:
    daemon = Daemon()
    async with _serve(scope, daemon) as server:
        binary = await WireClient.connect(_url(server), server.offer("token"))
        await binary.recv()
        await binary.send_bytes(b"\x00")
        assert (await binary.closed_with()).code == 1003
        big = await WireClient.connect(_url(server), server.offer("token"))
        await big.recv()
        await big.send_text("x" * (1024 * 1024 + 1))
        assert (await big.closed_with()).code == 1009
        nobody = await WireClient.connect(_url(server, "?profile=nope"), server.offer("token"))
        assert (await nobody.closed_with()).code == 1011
    assert all(t.sent == [] for t in daemon.transports)


@aio
async def test_the_profile_names_which_daemon(scope: Scope) -> None:
    other = scope.root / "profiles" / "other"
    other.mkdir(parents=True)
    (other / "profile.yaml").touch()
    daemon = Daemon()
    async with _serve(scope, daemon) as server:
        client = await WireClient.connect(_url(server, "?profile=other"), server.offer("token"))
        await client.recv()
        await client.close()
    assert daemon.homes == [other]


def test_the_default_dial_is_p17_s_home_dialer(scope: Scope) -> None:
    dial = daemon_dial(scope)
    assert callable(dial)  # built without dialing anything
