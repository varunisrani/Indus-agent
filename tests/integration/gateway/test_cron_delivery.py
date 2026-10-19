"""P15-21: a cron job fired by the daemon's ticker reaches a Telegram-named fake chat (H3, H10),
through P15-20's platform deliverer (the delivery router over the platform's outbound sender)."""

from __future__ import annotations

import asyncio

import pytest

from indus.cron import ports
from indus.foundation.scope import Scope
from indus.gateway.config import HomeChannel
from indus.gateway.delivery.router import DeliveryRouter
from indus.gateway.outbound.sender import OutboundSender
from indus.service.cron_host import CronHost
from tests.fakes.clock import FakeClock
from tests.fakes.platform.core import FakeChatPlatform
from tests.unit.gateway.core._kit import FakeChatAdapter
from tests.unit.gateway.cron_world import CronWorld, cron_scope

HOME_CHAT = "555"


@pytest.fixture
def world(scope: Scope, fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> CronWorld:
    # log redaction off: cron deliveries are masked anyway
    found = CronWorld(cron_scope(scope, {"security": {"redact_secrets": False}}), fake_clock)
    monkeypatch.setattr(ports, "BACKEND_FACTORY", lambda s: CronHost(s, agent=found.agent))
    return found


def test_a_cron_job_reaches_the_telegram_home_channel_masked(world: CronWorld) -> None:
    token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0" + "K1l2M3n4O5p6Q7r8"  # built at run time
    fake = FakeChatPlatform("telegram", clock=world.clock)
    adapter = FakeChatAdapter(fake, clock=world.clock)

    async def main() -> None:
        await adapter.connect()
        router = DeliveryRouter(
            world.scope,
            homes=lambda name: HomeChannel(name, HOME_CHAT) if name == "telegram" else None,
        )
        router.attach("telegram", OutboundSender(adapter, scope=world.scope, platform="telegram"))
        host = world.gateway_host()
        try:
            job = world.create(schedule="every 30m", prompt="check the build", deliver="telegram")
            world.replies[job.id] = [f"Build 812 is green. Deploy key used: {token}"]
            await host.start()
            await world.advance(31 * 60, step=10)
            await world.until(lambda: any("Build 812" in t for t in fake.texts(HOME_CHAT)))
            await world.until(lambda: world.attempts(job.id)[-1].delivery_status is not None)
            attempt = world.attempts(job.id)[-1]
            assert attempt.result_status == "ok" and attempt.delivery_status == "delivered"
            texts = fake.texts(HOME_CHAT)
            assert len(texts) == 1
            assert job.id in texts[0]  # P14's wrapper names the job
            assert token not in texts[0] and "ghp_" in texts[0]
            # the next half hour fires it again, once
            await world.advance(30 * 60, step=10)
            await world.until(lambda: len(fake.texts(HOME_CHAT)) == 2)
            await world.advance(120, step=10)
            assert len(fake.texts(HOME_CHAT)) == 2
        finally:
            await host.stop()
            router.close()
            await adapter.disconnect()

    asyncio.run(main())
