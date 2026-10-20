"""P16-12 wiring: a Microsoft Graph receiver's replies reach ``platforms.msgraph.deliver_to``
through the daemon's delivery router (``RouterPart``); the receiver itself is never a target."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from indus.cron import delivery as cron_delivery
from indus.foundation.scope import Scope
from indus.gateway.adapters.base import Adapter
from indus.gateway.adapters.msgraph.adapter import GraphOptions
from indus.gateway.core.send import ErrorKind, SendResult
from indus.gateway.delivery.parts import RouterPart, graph_forward
from indus.gateway.delivery.router import DeliveryRouter
from indus.gateway.outbound.sender import OutboundSender
from indus.state import registry as store_registry
from indus.state.registry import StoreHandle
from tests.unit.gateway.adapters.msgraph.test_adapter import Rig
from tests.unit.gateway.outbound.recording import RecordingAdapter, recording_adapter


@pytest.fixture
def store(scope: Scope) -> Iterator[StoreHandle]:
    handle = store_registry.open_unshared(scope)
    yield handle
    handle.close()


class _Daemon:
    """What ``RouterPart`` reads from the gateway daemon."""

    def __init__(self, scope: Scope, store: StoreHandle) -> None:
        self.scope = scope
        self.store = store
        self.service = None
        self.senders: dict[str, OutboundSender] = {}
        self.settings: Any = type("Settings", (), {"get": staticmethod(lambda _name: None)})()
        self.ups: list[Callable[[str, Adapter], None]] = []

    def on_platform_up(self, listener: Callable[[str, Adapter], None]) -> Callable[[], None]:
        self.ups.append(listener)
        return lambda: None

    def on_platform_down(self, listener: object) -> Callable[[], None]:
        return lambda: None

    def up(self, name: str, adapter: Adapter) -> None:
        for listener in self.ups:
            listener(name, adapter)


def _forward(router: DeliveryRouter, target: str) -> SendResult:
    async def main() -> SendResult:
        return await graph_forward(router)(target, "text")

    return asyncio.run(main())


def _telegram(scope: Scope) -> tuple[RecordingAdapter, OutboundSender]:
    adapter = recording_adapter(name="telegram", markup="plain")
    return adapter, OutboundSender(adapter, scope=scope, platform="telegram", rng=lambda: 0.0)


def test_the_router_part_forwards_graph_replies_and_never_targets_the_receiver(
    scope: Scope, store: StoreHandle
) -> None:
    async def main() -> None:
        daemon: Any = _Daemon(scope, store)
        part = RouterPart()
        await part.prepare(daemon)
        telegram, sender = _telegram(scope)
        daemon.senders["telegram"] = sender
        rig = Rig(options=GraphOptions(deliver_to="telegram:4242"))
        try:
            daemon.up("telegram", telegram)
            daemon.up("msgraph", rig.adapter)
            assert cron_delivery.deliverer_for("telegram") is not None
            assert cron_delivery.deliverer_for("msgraph") is None
            await rig.adapter.connect()
            result = await rig.adapter.send("msgraph:sub-1", "A file changed in the drive.")
            assert result.ok and result.message_id
            sent = [op for op in telegram.ops if op.op == "send"]
            assert [(op.chat_id, op.text) for op in sent] == [
                ("4242", "A file changed in the drive.")
            ]
            assert rig.adapter.counts["undelivered"] == 0
            await rig.adapter.disconnect()
        finally:
            await part.stop(daemon)

    asyncio.run(main())


@pytest.mark.parametrize(
    "target", ["all", "telegram", "msgraph:sub-9", "local", "telegram:1,slack:2"]
)
def test_a_target_that_is_not_one_chat_elsewhere_is_a_final_failure(
    scope: Scope, target: str
) -> None:
    router = DeliveryRouter(scope, homes=lambda _name: None)
    _adapter, sender = _telegram(scope)
    router.attach("telegram", sender)
    try:
        result = _forward(router, target)
    finally:
        router.close()
    assert not result.ok and not result.retryable and result.kind is ErrorKind.BAD_FORMAT


def test_a_platform_that_is_not_up_fails_without_a_retry(scope: Scope) -> None:
    router = DeliveryRouter(scope, homes=lambda _name: None)
    try:
        result = _forward(router, "slack:C1")
    finally:
        router.close()
    assert not result.ok and not result.retryable and not result.unsupported
    assert result.error is not None and "slack" in result.error


def test_deliver_to_may_not_name_the_receiver_itself() -> None:
    with pytest.raises(ValueError, match="another platform"):
        GraphOptions.read({"deliver_to": "msgraph:sub-1"})
