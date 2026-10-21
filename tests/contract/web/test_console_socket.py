"""P24-36: ``/api/console`` over a real loopback server — the shared WebSocket gates, the frames,
one command at a time, exact confirmation, the 60 s limit, cancel, output cap and ``exit``."""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
from collections.abc import AsyncIterator
from typing import Any

from indus.foundation.clock import SystemClock
from indus.foundation.scope import Scope
from indus.gateway.http.serving import HttpServer, bind_listener
from indus.service.console import CancelToken, ConsoleCommand, ConsoleEngine
from indus.service.ws_auth import WEB_CONSOLE, dashboard_offer
from indus.web.app import AppOptions, create_app
from indus.web.console import CONSOLE_PATH, ConsoleRoute
from indus.web.state import DashboardState
from indus.web.ws import websocket_routes
from indus.web.ws.chat_gate import purpose_of
from tests.fakes.pty.ws_wire import WireClient, WireClosed
from tests.unit.backends._aio import aio


class Rig:
    def __init__(self, scope: Scope, *, enabled: bool = True) -> None:
        self.scope = scope
        self.enabled = enabled
        self.base = ""
        self.state: DashboardState | None = None
        self.release = threading.Event()
        self.started = threading.Event()
        self.unwound = threading.Event()
        self.sleeps: list[float] = []
        self.fire_timeout = asyncio.Event()

    def engine(self, scope: Scope) -> ConsoleEngine:
        engine = ConsoleEngine(scope)

        def slow(_e: ConsoleEngine, _a: list[str], token: CancelToken) -> str:
            token.on_cancel(self.release.set)
            self.started.set()
            self.release.wait(10)
            self.unwound.set()
            return "slow done"

        engine.register(ConsoleCommand(("slow",), "slow", "t", handler=slow))
        engine.register(
            ConsoleCommand(("big",), "big", "t", handler=lambda _e, _a, _t: "z" * 50_001)
        )
        engine.register(
            ConsoleCommand(
                ("config", "set"),
                "config set <k> <v>",
                "t",
                confirm="Change this setting?",
                handler=lambda _e, a, _t: f"set {' '.join(a)}",
            )
        )
        return engine

    async def fake_sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        await self.fire_timeout.wait()

    @contextlib.asynccontextmanager
    async def run(self) -> AsyncIterator[Rig]:
        sock = await bind_listener("127.0.0.1", 0, SystemClock())
        port = int(sock.getsockname()[1])
        app = create_app(
            self.scope,
            "serve",
            options=AppOptions(port=port, config={}, console_enabled=self.enabled),
        )
        state: DashboardState = app.state.indus
        self.state = state
        route: Any = state.extras["console"]
        assert isinstance(route, ConsoleRoute)
        route.engine_factory = self.engine
        route.sleep = self.fake_sleep
        self.base = f"ws://127.0.0.1:{port}"
        http = HttpServer(app, sock, name="console-test", ws={"ws": "wsproto"})
        async with app.router.lifespan_context(app):
            await http.start()
            try:
                yield self
            finally:
                self.release.set()
                self.fire_timeout.set()
                await http.stop()

    async def connect(self, offer: list[str] | None = None) -> WireClient:
        assert self.state is not None
        if offer is None:
            offer = dashboard_offer("token", self.state.auth.token.value)
        return await WireClient.connect(f"{self.base}{CONSOLE_PATH}", offer)


async def frame(client: WireClient) -> dict[str, Any]:
    message = await client.recv()
    assert isinstance(message, str)
    return json.loads(message)


async def until_type(client: WireClient, kind: str) -> dict[str, Any]:
    while True:
        got = await frame(client)
        if got["type"] == kind:
            return got


async def send(client: WireClient, **data: Any) -> None:
    await client.send_text(json.dumps(data))


def test_the_route_goes_through_the_shared_helper(scope: Scope) -> None:
    app = create_app(scope, "serve", options=AppOptions(config={}))
    routes = dict(websocket_routes(app))
    assert purpose_of(routes[CONSOLE_PATH]) == WEB_CONSOLE


@aio
async def test_socket_without_a_credential_is_refused(scope: Scope) -> None:
    async with Rig(scope).run() as rig:
        client = await rig.connect(["indus-dashboard-v1"])
        closed = await client.closed_with()
        assert closed.code == 4401
        wrong = await rig.connect(dashboard_offer("token", "x" * 43))
        assert (await wrong.closed_with()).code == 4401


@aio
async def test_console_off_is_refused_by_the_feature_gate(scope: Scope) -> None:
    async with Rig(scope, enabled=False).run() as rig:
        client = await rig.connect()
        assert (await client.closed_with()).code == 4404


@aio
async def test_ready_ping_and_a_command(scope: Scope) -> None:
    async with Rig(scope).run() as rig:
        client = await rig.connect()
        ready = await frame(client)
        assert ready["type"] == "ready" and ready["profile"] == "work"
        assert ready["prompt"] == "indus> "
        await send(client, type="ping")
        assert (await frame(client))["type"] == "pong"
        await send(client, type="input", line="version")
        out = await frame(client)
        assert out["type"] == "output" and out["stream"] == "stdout"
        assert out["data"].startswith("Indus Agent") and out["command"] == "version"
        done = await frame(client)
        assert done == {
            "type": "complete",
            "id": out["id"],
            "status": "ok",
            "command": "version",
            "prompt": "indus> ",
        }
        await client.close()


@aio
async def test_second_command_while_one_runs_is_refused(scope: Scope) -> None:
    async with Rig(scope).run() as rig:
        client = await rig.connect()
        await frame(client)
        await send(client, type="input", line="slow")
        await asyncio.to_thread(rig.started.wait, 5)
        await send(client, type="input", line="version")
        error = await frame(client)
        assert error["type"] == "error" and "already running" in error["message"]
        assert error["command"] == "slow"
        rig.release.set()
        assert (await until_type(client, "output"))["data"] == "slow done"
        assert (await frame(client))["status"] == "ok"
        await client.close()


@aio
async def test_confirm_needs_the_exact_command(scope: Scope) -> None:
    async with Rig(scope).run() as rig:
        client = await rig.connect()
        await frame(client)
        await send(client, type="input", line="config set agent.max_turns 3")
        asked = await frame(client)
        assert asked["type"] == "confirm_required"
        assert asked["command"] == "config set agent.max_turns 3"
        assert asked["message"] == "Change this setting?"
        assert (await frame(client))["status"] == "confirm_required"
        # anything else first is refused, the confirmation stays pending
        await send(client, type="input", line="version")
        assert "Confirm or cancel" in (await frame(client))["message"]
        await send(client, type="confirm", command="config set agent.max_turns 4")
        refused = await frame(client)
        assert refused["type"] == "error" and "same command" in refused["message"]
        await send(client, type="confirm", command="config set agent.max_turns 3")
        out = await frame(client)
        assert out["data"] == "set agent.max_turns 3" and out["id"] == asked["id"]
        assert (await frame(client))["status"] == "ok"
        # cancel of a pending one
        await send(client, type="input", line="config set a 1")
        await until_type(client, "complete")
        await send(client, type="cancel")
        cancelled = await frame(client)
        assert cancelled["status"] == "cancelled" and cancelled["command"] == "config set a 1"
        await send(client, type="cancel")
        assert (await frame(client))["status"] == "idle"
        await client.close()


@aio
async def test_timeout_after_60_seconds(scope: Scope) -> None:
    async with Rig(scope).run() as rig:
        client = await rig.connect()
        await frame(client)
        await send(client, type="input", line="slow")
        await asyncio.to_thread(rig.started.wait, 5)
        assert rig.sleeps == [60.0]
        rig.fire_timeout.set()  # the fake clock reaches 60 s
        error = await frame(client)
        assert error["type"] == "error" and "longer than 60 s" in error["message"]
        done = await frame(client)
        assert done["status"] == "timeout" and done["command"] == "slow"
        assert rig.unwound.is_set()  # the token released the worker, which was waited for
        await client.close()


@aio
async def test_cancel_interrupts_and_waits_for_the_worker(scope: Scope) -> None:
    async with Rig(scope).run() as rig:
        client = await rig.connect()
        await frame(client)
        await send(client, type="input", line="slow")
        await asyncio.to_thread(rig.started.wait, 5)
        await send(client, type="cancel")
        error = await frame(client)
        assert error["message"] == "The command was cancelled."
        done = await frame(client)
        assert done["status"] == "cancelled"
        assert rig.unwound.is_set()
        await client.close()


@aio
async def test_output_truncated_with_marker(scope: Scope) -> None:
    async with Rig(scope).run() as rig:
        client = await rig.connect()
        await frame(client)
        await send(client, type="input", line="big")
        out = await frame(client)
        assert out["data"].startswith("z" * 50_000)
        assert out["data"].endswith("1 more characters were left out")
        await client.close()


@aio
async def test_bad_frames_keep_the_socket_open(scope: Scope) -> None:
    async with Rig(scope).run() as rig:
        client = await rig.connect()
        await frame(client)
        for raw in ("not json", "[1, 2]", json.dumps({"type": "dance"})):
            await client.send_text(raw)
            assert (await frame(client))["type"] == "error"
        await send(client, type="input", line=5)
        assert (await frame(client))["type"] == "error"
        await client.send_bytes(b"\x00")
        assert (await frame(client))["type"] == "error"
        await send(client, type="input", line="status | cat")
        refused = await until_type(client, "error")
        assert "shell syntax" in refused["message"]
        assert (await frame(client))["status"] == "error"
        await client.close()


@aio
async def test_exit_closes_1000_and_clear_sends_clear(scope: Scope) -> None:
    async with Rig(scope).run() as rig:
        client = await rig.connect()
        await frame(client)
        await send(client, type="input", line="clear")
        assert (await frame(client))["type"] == "clear"
        assert (await frame(client))["status"] == "clear"
        await send(client, type="command", line="exit")
        assert (await frame(client))["status"] == "exit"
        try:
            await client.recv()
        except WireClosed as closed:
            assert closed.code == 1000
        else:
            raise AssertionError("the socket stayed open after exit")


@aio
async def test_unknown_profile_closes_4400(scope: Scope) -> None:
    async with Rig(scope).run() as rig:
        assert rig.state is not None
        offer = dashboard_offer("token", rig.state.auth.token.value)
        client = await WireClient.connect(f"{rig.base}{CONSOLE_PATH}?profile=nope", offer)
        assert (await client.closed_with()).code == 4400
