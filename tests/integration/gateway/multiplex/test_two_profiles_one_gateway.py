"""DEMO (P24 "Done when", M8): two profiles served by one gateway, through ``gateway run``'s own
daemon (wave 42 wiring of P24-01..04).

The launch profile (``default``, at the root) and profile ``b`` each have their own Telegram bot
(a fake platform per bot token). Bot A's token and allowlist live in the launch process env; bot
B's token and allowlist live only in B's own secret store. One ``GatewayDaemon`` with
``gateway.multiplex_profiles: true`` claims the user's host lock, serves both profiles, answers
each chat through the bot that received it and keeps each conversation in its profile's own
store; a sender allowed only by B is refused by A and answered by B. A second ``gateway run`` for
``b`` attaches to the host instead of starting. After a restart each chat continues with its own
bot. The shared ingress serves B's API server under ``/p/b/`` with B's own keys.
"""

from __future__ import annotations

import asyncio
import os
import struct
from pathlib import Path

import httpx
import pytest

from indus.foundation.secrets import open_secret_store
from indus.gateway.daemon.process import GatewayDaemon
from indus.gateway.daemon.side_services import ControlPart
from indus.gateway.http import SurfaceSpec
from indus.gateway.http.part import HttpPart
from indus.gateway.multiplex.host import read_record
from indus.service import gateway_control
from indus.service.remote.profile_guard import check_ready, expected_profile_id
from indus.service.session_service import SessionService
from indus.service.ws_auth import (
    CLOSE_BAD_TICKET,
    SUBPROTOCOL,
    TICKET_PROTOCOL_PREFIX,
    WS_INTERACTIVE,
)
from indus.state.api_keys import ApiKeyRing
from indus.state.gateway_runtime import EXIT_OK, pid_path
from indus.state.registry import StoreHandle
from tests.contract.gateway._http_kit import make_service
from tests.fakes.model import text_turn
from tests.integration.gateway.daemon_kit import fake_service, fly, model_server, quick, rebind
from tests.integration.gateway.multiplex.host_kit import (
    ALLOW,
    API_ON,
    B_CONFIG,
    MULTIPLEX,
    ONLY_A,
    ONLY_B,
    TOKEN,
    World,
    dm_key,
    session_of,
)
from tests.integration.gateway.test_ws_listener import RawClient, needs_ws

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX locks and sockets")


def test_two_profiles_served_by_one_gateway_with_isolated_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = World(tmp_path, monkeypatch, config="")
    server = model_server(
        text_turn("A here"), text_turn("B here"), text_turn("B again"), text_turn("A again")
    )
    first_b_session: dict[str, str | None] = {}

    async def boot_one() -> None:
        host = world.host(server, tmp_path)
        flight = await fly(host)
        try:
            mux = host.mux
            assert mux is not None and host.status.state == "running"
            await flight.until(
                lambda: "b" in mux.daemons and "telegram" in mux.daemons["b"].adapters
            )
            record = read_record(tmp_path / "state" / "indus")
            assert record is not None and record.pid == os.getpid()
            assert record.served_profiles == ("default", "b")
            assert host.identity()["served_profiles"] == ["default", "b"]
            secondary = mux.daemons["b"]
            # B's view has none of the launch env's credentials or allowlist (R06.26).
            assert TOKEN not in secondary.scope.env and ALLOW not in secondary.scope.env
            assert sorted(world.built) == ["b:True", "default:False"]

            world.bot_a.dm(ONLY_A, "hello A")
            await flight.until(lambda: world.bot_a.texts(ONLY_A) == ["A here"])
            world.bot_a.dm(ONLY_B, "let me in, A")  # allowed only by B: A refuses
            world.bot_b.dm(ONLY_B, "hello B")
            await flight.until(lambda: world.bot_b.texts(ONLY_B) == ["B here"])
            assert "A here" not in world.bot_b.texts() and "B here" not in world.bot_a.texts()
            assert "B here" not in world.bot_a.texts(ONLY_B)
            # Each conversation lives in its own profile's store, keyed in its namespace.
            assert session_of(host, dm_key("default", ONLY_A)) is not None
            assert session_of(host, dm_key("b", ONLY_B)) is None
            assert session_of(host, dm_key("default", ONLY_B)) is None
            first_b_session["id"] = session_of(secondary, dm_key("b", ONLY_B))
            assert first_b_session["id"] is not None
            assert session_of(secondary, dm_key("default", ONLY_A)) is None

            # A second `gateway run` for b attaches to this host instead of starting.
            second = GatewayDaemon(
                world.scope(world.home_b),
                options=quick(),
                registry=world.registry(),
                service_factory=fake_service(server, tmp_path / "svc-2"),
                parts=[],
            )
            assert await second.run() == EXIT_OK
            assert second.attach_verdict is not None
            assert second.attach_verdict.outcome == "attach"
        finally:
            assert await flight.stop() == EXIT_OK
        assert read_record(tmp_path / "state" / "indus") is None
        assert host.mux is not None and host.mux.daemons == {"default": host}

    asyncio.run(boot_one())
    assert not pid_path(world.home_b).exists()

    async def boot_two() -> None:
        rebind(world.bot_a)
        rebind(world.bot_b)
        host = world.host(server, tmp_path)
        flight = await fly(host)
        try:
            mux = host.mux
            assert mux is not None
            await flight.until(
                lambda: "b" in mux.daemons and "telegram" in mux.daemons["b"].adapters
            )
            world.bot_b.dm(ONLY_B, "still there, B?")
            await flight.until(lambda: world.bot_b.texts(ONLY_B) == ["B here", "B again"])
            world.bot_a.dm(ONLY_A, "and you, A?")
            await flight.until(lambda: world.bot_a.texts(ONLY_A) == ["A here", "A again"])
            # The chat with B continues in B's own conversation, answered by B's own bot.
            assert session_of(mux.daemons["b"], dm_key("b", ONLY_B)) == first_b_session["id"]
            assert "B again" not in world.bot_a.texts() and "A again" not in world.bot_b.texts()
        finally:
            assert await flight.stop() == EXIT_OK

    asyncio.run(boot_two())


def hook(route: str) -> str:
    return (
        "  webhook:\n    enabled: true\n    port: 0\n"
        f"    routes: {{{route}: {{insecure: true, deliver_only: true, deliver: local}}}}\n"
    )


def test_a_served_profile_http_is_reached_under_its_prefix_with_its_own_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = World(tmp_path, monkeypatch, config=API_ON + hook("a-only"))
    b_config = B_CONFIG + MULTIPLEX + API_ON + hook("b-only")
    (world.home_b / "config.yaml").write_text(b_config, encoding="utf-8")
    server = model_server()
    store_a = open_secret_store(world.root, "default", mode="file")
    _info, key_a = ApiKeyRing(world.scope(), store_a).create("a")
    store_b = open_secret_store(world.home_b, "b", mode="file")
    _info, key_b = ApiKeyRing(world.scope(world.home_b), store_b).create("b")

    async def surfaces(daemon: GatewayDaemon, spec: SurfaceSpec) -> SessionService:
        store: StoreHandle = daemon.store
        return make_service(
            daemon.scope,
            server,
            tmp_path / f"{daemon.scope.profile_id}-{spec.name}",
            store,
            toolsets=list(spec.toolsets),
            kinds=spec.answerable_kinds,
            platform=spec.platform,
        )

    part = HttpPart()

    async def main() -> None:
        host = world.host(
            server,
            tmp_path,
            parts=[part],
            secondary_parts=lambda _d: [HttpPart()],
            surfaces=surfaces,
        )
        flight = await fly(host)
        try:
            mux = host.mux
            assert mux is not None
            await flight.until(lambda: "b" in mux.daemons and mux.ingress["webhook"].mounted("b"))
            assert mux.ingress["api_server"].mounted("b") == ["api_server"]
            surfaces_now = part.surfaces
            assert surfaces_now is not None and surfaces_now.api is not None
            port = surfaces_now.api.address[1]
            base = f"http://127.0.0.1:{port}"
            hooks = f"http://127.0.0.1:{surfaces_now.webhook.address[1]}"

            def auth(value: str) -> dict[str, str]:
                return {"Authorization": "Bearer " + value}

            async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
                assert (
                    await client.get(f"{base}/p/b/health", headers=auth(key_b))
                ).status_code == 200
                assert (
                    await client.get(f"{base}/p/b/health", headers=auth(key_a))
                ).status_code == 401
                assert (await client.get(f"{base}/health", headers=auth(key_b))).status_code == 401
                assert (await client.get(f"{base}/health", headers=auth(key_a))).status_code == 200
                assert (
                    await client.get(f"{base}/p/c/health", headers=auth(key_b))
                ).status_code == 404
                # B's webhook routes live under its prefix only; the launch profile's never there.
                assert (
                    await client.post(f"{hooks}/p/b/webhooks/b-only", json={})
                ).status_code == 200
                assert (await client.post(f"{hooks}/webhooks/b-only", json={})).status_code == 404
                assert (
                    await client.post(f"{hooks}/p/b/webhooks/a-only", json={})
                ).status_code == 404
                assert (await client.post(f"{hooks}/webhooks/a-only", json={})).status_code == 200
            rows = mux.daemons["b"].status_snapshot()["surfaces"]
            assert rows["api_server"] == {"mounted": "/p/b/"}
            assert rows["webhook"] == {"mounted": "/p/b/"}
        finally:
            assert await flight.stop() == EXIT_OK

    asyncio.run(main())


ROUTED = "300"  # allowed by B; routed to B although it writes to bot A
ROUTE = (
    f"  profile_routes:\n    - {{name: to-b, platform: telegram, profile: b, user: '{ROUTED}'}}\n"
)


def test_a_routed_message_runs_in_its_profile_and_is_answered_by_the_bot_that_received_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = World(tmp_path, monkeypatch, config=ROUTE)
    open_secret_store(world.home_b, "b", mode="file").set(ALLOW, f"{ONLY_B},{ROUTED}")
    server = model_server(text_turn("B answers through A"))

    async def main() -> None:
        host = world.host(server, tmp_path)
        flight = await fly(host)
        try:
            mux = host.mux
            assert mux is not None
            await flight.until(
                lambda: "b" in mux.daemons and "telegram" in mux.daemons["b"].adapters
            )
            world.bot_a.dm(ROUTED, "who answers me?")
            await flight.until(lambda: world.bot_a.texts(ROUTED) == ["B answers through A"])
            assert world.bot_b.texts() == []  # never another bot by guess
            secondary = mux.daemons["b"]
            assert session_of(secondary, dm_key("b", ROUTED)) is not None
            assert session_of(host, dm_key("default", ROUTED)) is None
            assert mux.router.bots is not None
            assert mux.router.bots.get(dm_key("b", ROUTED)) == "default"  # the receiving bot
        finally:
            assert await flight.stop() == EXIT_OK

    asyncio.run(main())


@needs_ws
def test_a_served_profile_is_attached_over_its_own_websocket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D-16.542 as amended in wave 42: a served profile's daemon serves its own attach endpoint,
    bound to that profile's (routed) scope, so `indus` / the TUI attach to a multiplexed profile
    the normal way: B's control socket grants a ticket for B's endpoint, whose ready frame names
    B; a ticket of the launch profile's book does not open it."""
    world = World(tmp_path, monkeypatch, config="")
    server = model_server()

    async def main() -> None:
        host = world.host(server, tmp_path, secondary_parts=lambda _d: [ControlPart()])
        flight = await fly(host)
        try:
            mux = host.mux
            assert mux is not None
            await flight.until(lambda: "b" in mux.daemons and mux.daemons["b"].ws_url() is not None)
            served = mux.daemons["b"]
            assert served.scope.routed and served.ws is not None
            assert host.ws_url() is not None and served.ws_url() != host.ws_url()
            expected = expected_profile_id(world.home_b)
            granted = await asyncio.to_thread(
                gateway_control.session_ticket, world.home_b, WS_INTERACTIVE, profile_id=expected
            )
            assert granted.ok and granted.ws_url == served.ws_url()
            client, head = await RawClient.dial(
                granted.ws_url, [SUBPROTOCOL, TICKET_PROTOCOL_PREFIX + granted.ticket]
            )
            assert head.startswith("HTTP/1.1 101")
            ready = await client.recv()
            assert ready["method"] == "gateway.ready"
            check_ready(expected, ready["params"])
            assert ready["params"]["instance_id"] == served.instance_id
            await client.send({"jsonrpc": "2.0", "id": 1, "method": "gateway.ping"})
            pong = await client.recv()
            assert pong["id"] == 1 and pong["result"]["pong"] is True
            await client.close()

            launch_ticket = host.tickets.mint(WS_INTERACTIVE).ticket
            refused, _head = await RawClient.dial(
                granted.ws_url, [SUBPROTOCOL, TICKET_PROTOCOL_PREFIX + launch_ticket]
            )
            opcode, payload = await refused.frame()
            assert opcode == 8 and struct.unpack("!H", payload[:2])[0] == CLOSE_BAD_TICKET
            refused.writer.close()
        finally:
            assert await flight.stop() == EXIT_OK

    asyncio.run(main())
