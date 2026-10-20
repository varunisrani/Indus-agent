"""P18-09: ``/api/pub`` → ``/api/events`` fan-out over a real loopback server.

The publisher is the fake TUI child (``tests/fakes/pty/fake_tui.py``) started as a real process
with the publish URL and the internal credential in its environment, as ``/api/pty`` starts the
TUI. The app is the real one (``chat_rig``): every socket passes the real WebSocket helper.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.service.ws_auth import DASHBOARD_SUBPROTOCOL, WEB_EVENTS
from indus.web.ws.chat_gate import PURPOSE_EVENTS, PURPOSE_PUB
from indus.web.ws.events_fanout import (
    CLOSE_BAD_CHANNEL,
    CLOSE_NOT_PUBLISHER,
    EventsFanout,
    EventsRoutes,
)
from tests.contract.web.chat_rig import chat_server
from tests.fakes.clock import checkpoint
from tests.fakes.pty.ws_wire import WireClient
from tests.unit.backends._aio import aio

REPO = Path(__file__).resolve().parents[3]


async def until(check: Any, timeout: float = 5.0) -> None:
    from indus.foundation.clock import SystemClock

    async with asyncio.timeout(timeout):
        while not check():
            await SystemClock().sleep(0.01)


async def run_fake_tui(env: dict[str, str]) -> int:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tests.fakes.pty.fake_tui",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        env=env,
        cwd=str(REPO),
    )
    try:
        return await asyncio.wait_for(proc.wait(), 30)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


@aio
async def test_a_child_s_three_events_reach_both_subscribers(scope: Scope) -> None:
    async with chat_server(scope) as server:
        base, fanout = server.base, server.chat.fanout
        subs = [
            await WireClient.connect(f"{base}/api/events?channel=chat-1", server.offer("token"))
            for _ in range(2)
        ]
        other = await WireClient.connect(f"{base}/api/events?channel=chat-2", server.offer("token"))
        assert subs[0].subprotocol == DASHBOARD_SUBPROTOCOL  # only the protocol name is echoed
        await until(lambda: fanout.channels() == {"chat-1": 2, "chat-2": 1})
        link = server.chat.pty.link
        assert link is not None
        env = {
            "PATH": os.defpath,
            "PYTHONPATH": str(REPO),
            "FAKE_TUI_PUBLISH": "3",
            "INDUS_TUI_SIDECAR_URL": link.publish_url("chat-1") or "",
            "INDUS_TUI_SIDECAR_CREDENTIAL": link.credential(),
        }
        assert env["INDUS_TUI_SIDECAR_URL"] == f"{base}/api/pub?channel=chat-1"
        assert server.internal not in env["INDUS_TUI_SIDECAR_URL"]  # no credential in the URL
        assert await run_fake_tui(env) == 0
        for sub in subs:
            frames = [json.loads(await sub.recv()) for _ in range(3)]
            assert [f["params"]["n"] for f in frames] == [1, 2, 3]
        with pytest.raises(TimeoutError):
            await other.recv(timeout=0.3)  # another channel's feed stays quiet
        for client in (*subs, other):
            await client.close()
        await until(lambda: fanout.channels() == {})  # empty channels are dropped


@aio
async def test_subscribers_never_speak(scope: Scope) -> None:
    async with chat_server(scope) as server:
        base = server.base
        sub = await WireClient.connect(f"{base}/api/events?channel=c", server.offer("token"))
        pub = await WireClient.connect(f"{base}/api/pub?channel=c", server.offer("internal"))
        await until(lambda: server.chat.fanout.channels() == {"c": 1})
        await sub.send_text('{"method": "forged"}')
        await pub.send_text('{"n": 1}')
        assert await sub.recv() == '{"n": 1}'
        with pytest.raises(TimeoutError):
            await sub.recv(timeout=0.3)  # its own frame was not fanned out
        with pytest.raises(TimeoutError):
            await pub.recv(timeout=0.2)  # nothing goes back to a publisher
        await pub.close()
        await sub.close()


@aio
async def test_only_the_internal_credential_may_publish(scope: Scope) -> None:
    async with chat_server(scope) as server:
        base = server.base
        browser = await WireClient.connect(f"{base}/api/pub?channel=c", server.offer("token"))
        assert (await browser.closed_with()).code == CLOSE_NOT_PUBLISHER
        nothing = await WireClient.connect(f"{base}/api/pub?channel=c", [DASHBOARD_SUBPROTOCOL])
        assert (await nothing.closed_with()).code == 4401
        internal_events = await WireClient.connect(
            f"{base}/api/events?channel=c", server.offer("internal")
        )
        assert (await internal_events.closed_with()).code == 4401  # not a person's feed
        bad = await WireClient.connect(f"{base}/api/pub?channel=a%20b", server.offer("internal"))
        assert (await bad.closed_with()).code == CLOSE_BAD_CHANNEL
        sub_bad = await WireClient.connect(f"{base}/api/events", server.offer("token"))
        assert (await sub_bad.closed_with()).code == CLOSE_BAD_CHANNEL
        pub = await WireClient.connect(f"{base}/api/pub?channel=c", server.offer("internal"))
        await pub.send_bytes(b"\x00binary")
        assert (await pub.closed_with()).code == 1003
        big = await WireClient.connect(f"{base}/api/pub?channel=c", server.offer("internal"))
        await big.send_text("x" * (1024 * 1024 + 1))
        assert (await big.closed_with()).code == 1009


@aio
async def test_a_disabled_chat_and_a_gated_token_are_refused(scope: Scope) -> None:
    for enabled, gated, code in ((False, False, 4404), (True, True, 4401)):
        async with chat_server(scope, enabled=enabled, gated=gated) as server:
            client = await WireClient.connect(
                f"{server.base}/api/events?channel=c", server.offer("token")
            )
            assert (await client.closed_with()).code == code
    async with chat_server(scope, gated=True) as server:
        ticket = server.offer("ticket", purpose=WEB_EVENTS)
        client = await WireClient.connect(f"{server.base}/api/events?channel=c", ticket)
        await until(lambda: server.chat.fanout.channels() == {"c": 1})
        again = await WireClient.connect(f"{server.base}/api/events?channel=c", ticket)
        assert (await again.closed_with()).code == 4401  # a ticket opens one socket
        await client.close()


class FakeSocket:
    """A subscriber socket for the in-process fan-out: sends wait for ``release``; it never
    speaks until ``leave``."""

    def __init__(self, *, stuck: bool = False) -> None:
        self.sent: list[str] = []
        self.closes: list[int] = []
        self.release = asyncio.Event()
        if not stuck:
            self.release.set()
        self._gone = asyncio.Event()

    async def receive(self) -> dict[str, Any]:
        await self._gone.wait()
        return {"type": "websocket.disconnect"}

    def leave(self) -> None:
        self._gone.set()

    async def send_text(self, text: str) -> None:
        await self.release.wait()
        self.sent.append(text)

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closes.append(code)


@aio
async def test_a_subscriber_that_falls_behind_is_closed_1013_and_others_go_on() -> None:
    fanout = EventsFanout(queue_frames=2)
    stuck, quick = FakeSocket(stuck=True), FakeSocket()
    loop = asyncio.get_running_loop()
    tasks = [
        loop.create_task(fanout.serve_subscriber(sock, "c"))  # pyright: ignore[reportArgumentType]
        for sock in (stuck, quick)
    ]
    await until(lambda: fanout.channels() == {"c": 2})
    for number in range(5):
        assert fanout.publish("c", str(number)) == 2  # never waits for a socket
        for _ in range(3):
            await checkpoint()  # the quick writer keeps up; the stuck one cannot
    await until(lambda: len(quick.sent) == 5)
    stuck.release.set()
    await asyncio.wait_for(tasks[0], 5)
    assert stuck.closes == [1013]
    assert quick.sent == ["0", "1", "2", "3", "4"] and quick.closes == []
    assert fanout.channels() == {"c": 1}
    quick.leave()
    await asyncio.wait_for(tasks[1], 5)
    assert fanout.channels() == {}


def test_publish_to_an_empty_channel_reaches_nobody() -> None:
    assert EventsFanout().publish("nobody", "x") == 0


@aio
async def test_routes_carry_their_gate_purpose(scope: Scope) -> None:
    from indus.web.ws import websocket_routes
    from indus.web.ws.chat_gate import purpose_of

    routes = EventsRoutes()
    assert purpose_of(routes.publish_endpoint) == PURPOSE_PUB
    assert purpose_of(routes.events_endpoint) == PURPOSE_EVENTS
    async with chat_server(scope) as server:
        mounted = {path: purpose_of(endpoint) for path, endpoint in websocket_routes(server.app)}
    assert mounted["/api/pub"] == PURPOSE_PUB
    assert mounted["/api/events"] == PURPOSE_EVENTS
