"""The relay connector inside a real in-process gateway daemon (wave 42 wiring of P24-08/09).

``RelayPart`` dials the fake connector (``tests/fakes/platform/relay_wire.py``) through the
transport's opener seam (no socket): one adapter per fronted platform joins the daemon like a
native one (intake, sender, status, the delivery router ``via_relay``); the connector's interrupt
frame stops the running turn; an auto-started daemon whose only platform is relayed idles out and
asks the connector to go dormant, and the next start gets the buffered events replayed. An
enabled native adapter keeps its platform.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from indus.cron.targets import DeliveryTarget
from indus.foundation.secrets import open_secret_store
from indus.gateway.daemon.idle_exit import IDLE_EXIT_REASON
from indus.gateway.daemon.process import GatewayDaemon
from indus.gateway.delivery.parts import RouterPart
from indus.gateway.registry import PlatformRegistry
from indus.gateway.relay.part import RelayPart, create
from indus.service.gateway_ensure import AUTOSTART_ENV
from indus.state.gateway_runtime import EXIT_OK, read_status
from tests.fakes.clock import FakeClock
from tests.fakes.model import FakeModelServer, Finish, Stall, Text, Turn, text_turn
from tests.fakes.platform.core import FakeChatPlatform
from tests.fakes.platform.relay_wire import GATEWAY_ID, URL, RelayWire, shared_secret
from tests.integration.gateway.daemon_kit import (
    ALLOWED,
    daemon_scope,
    fake_entry,
    fake_registry,
    fake_service,
    fly,
    model_server,
    quick,
    rebind,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX daemon")

RELAY = f"gateway:\n  relay: {{url: '{URL}', gateway_id: {GATEWAY_ID}}}\n"


def relayed_world(_tmp_path: Path) -> tuple[FakeChatPlatform, RelayWire]:
    platform = FakeChatPlatform("telegram", clock=FakeClock(datetime.now(UTC)))
    return platform, RelayWire(platform)


def relay_daemon(
    tmp_path: Path,
    wire: RelayWire,
    server: FakeModelServer,
    *,
    env: dict[str, str | None] | None = None,
    registry: PlatformRegistry | None = None,
    **options: Any,
) -> GatewayDaemon:
    scope = daemon_scope(tmp_path, config=RELAY, env={"TELEGRAM_BOT_TOKEN": None, **(env or {})})
    open_secret_store(scope.home, scope.profile_id, mode="file").set(
        "INDUS_RELAY_SECRET", shared_secret()
    )
    # The platform is known (its access policy comes from its entry) but has no credential of
    # its own here, so its native adapter is not enabled and the relay serves it.
    known = fake_registry(fake_entry(FakeChatPlatform("telegram")))
    return GatewayDaemon(
        scope,
        options=quick(**options),
        registry=registry or known,
        service_factory=fake_service(server, tmp_path / "svc-locks"),
        parts=[RouterPart(), RelayPart(opener=wire.open)],
    )


def test_the_part_is_built_only_with_a_relay_url(tmp_path: Path) -> None:
    plain = GatewayDaemon(daemon_scope(tmp_path / "a"), options=quick(), parts=[])
    assert create(plain) is None
    with_relay = GatewayDaemon(daemon_scope(tmp_path / "b", config=RELAY), options=quick())
    assert isinstance(create(with_relay), RelayPart)


def test_a_relayed_platform_chats_and_delivers_through_the_connector(tmp_path: Path) -> None:
    platform, wire = relayed_world(tmp_path)
    server = model_server(text_turn("hello through the relay"))
    daemon = relay_daemon(tmp_path, wire, server)

    async def main() -> None:
        flight = await fly(daemon)
        try:
            assert daemon.relayed == {"telegram"}
            assert daemon.platforms["telegram"].state == "connected"
            assert daemon.native_state("telegram") is None
            assert daemon.status_snapshot()["surfaces"]["relay"]["platforms"] == ["telegram"]
            platform.dm(ALLOWED, "hi")
            wire.push_inbound()
            await flight.until(lambda: platform.texts(ALLOWED) == ["hello through the relay"])
            router = next(p for p in daemon.parts if isinstance(p, RouterPart)).router
            assert router is not None and router.attached() == ["telegram"]
            sent = await router.send(
                DeliveryTarget("telegram", "chat-9", None, is_explicit=True),
                "a cron report",
                label="test",
            )
            assert sent.ok
            assert platform.texts("chat-9") == ["a cron report"]
            assert any(a.get("_platform") == "telegram" for a in wire.actions)
        finally:
            assert await flight.stop() == EXIT_OK
        assert not wire.live and daemon.relayed == set()

    asyncio.run(main())


def test_the_connectors_interrupt_stops_the_running_turn(tmp_path: Path) -> None:
    platform, wire = relayed_world(tmp_path)
    server = model_server(Turn((Text(("thinking",)), Stall(10_000.0), Finish("stop"))))
    daemon = relay_daemon(tmp_path, wire, server)

    async def main() -> None:
        flight = await fly(daemon)
        try:
            platform.dm(ALLOWED, "take your time")
            wire.push_inbound()
            await flight.until(lambda: len(daemon.pipeline.claims) == 1)
            await flight.until(lambda: len(server.model_requests) == 1)
            wire.interrupt(ALLOWED)
            await flight.until(lambda: len(daemon.pipeline.claims) == 0)
        finally:
            assert await flight.stop() == EXIT_OK

    asyncio.run(main())


def test_an_idle_daemon_leaves_the_connector_dormant_and_the_next_start_replays(
    tmp_path: Path,
) -> None:
    platform, wire = relayed_world(tmp_path)
    server = model_server(text_turn("you were buffered"))
    idle = relay_daemon(
        tmp_path, wire, server, env={AUTOSTART_ENV: "1"}, idle_exit_seconds=0.3, attach_ws=False
    )

    async def first() -> None:
        flight = await fly(idle)
        await flight.until(lambda: idle.relayed == {"telegram"})
        assert await asyncio.wait_for(flight.task, 20) == EXIT_OK

    asyncio.run(first())
    status = read_status(idle.scope.home)
    assert status is not None and status.exit_reason == IDLE_EXIT_REASON
    assert wire.dormant  # the connector keeps our events now
    platform.dm(ALLOWED, "are you there?")
    assert wire.push_inbound() == 1 and len(wire.buffer) == 1

    rebind(platform)
    again = relay_daemon(tmp_path, wire, server)

    async def second() -> None:
        flight = await fly(again)
        try:
            await flight.until(lambda: platform.texts(ALLOWED) == ["you were buffered"])
            await flight.until(lambda: wire.buffer == [])
        finally:
            assert await flight.stop() == EXIT_OK

    asyncio.run(second())


def test_an_enabled_native_adapter_keeps_its_platform(tmp_path: Path) -> None:
    platform, wire = relayed_world(tmp_path)
    native = FakeChatPlatform("telegram", clock=FakeClock(datetime.now(UTC)))
    server = model_server()
    daemon = relay_daemon(
        tmp_path,
        wire,
        server,
        env={"TELEGRAM_BOT_TOKEN": "fake" + "-native-" + "credential"},
        registry=fake_registry(fake_entry(native)),
    )

    async def main() -> None:
        flight = await fly(daemon)
        try:
            assert daemon.native_state("telegram") is True
            assert daemon.relayed == set()
            assert "telegram" in daemon.adapters and native.connected
            router = next(p for p in daemon.parts if isinstance(p, RouterPart)).router
            assert router is not None
            sender = router.sender_for("telegram")
            assert sender is not None and sender.adapter is daemon.adapters["telegram"]
        finally:
            assert await flight.stop() == EXIT_OK

    asyncio.run(main())
