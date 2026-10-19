"""P15-21..23 wiring: one call plugs cron, chat prompts and media intake into a daemon."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from indus.cron import ports
from indus.cron.ticker import ticker_owner
from indus.foundation.scope import Scope
from indus.gateway.core.events import Attachment, InboundEvent, MessageKind
from indus.gateway.extras import ExtrasSeat, install_extras
from indus.service.cron_host import CronHost
from tests.fakes.clock import FakeClock
from tests.fakes.model import text_turn
from tests.unit.gateway.core._kit import build_kit, dm, model_inputs
from tests.unit.gateway.cron_world import cron_scope


def test_install_extras_wires_everything(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = cron_scope(scope)
    monkeypatch.setattr(ports, "BACKEND_FACTORY", lambda s: CronHost(s))

    async def main() -> None:
        kit = await build_kit(profile, fake_clock, tmp_path, text_turn("nice file"))
        daemon = ExtrasSeat(profile, kit.pipeline)
        extras = install_extras(daemon)
        try:
            pipeline = kit.pipeline
            assert pipeline.enrich == extras.intake.enrich
            assert pipeline.on_request == extras.prompts.deliver
            assert pipeline.front_doors == [extras.albums.front_door, extras.prompts.front_door]
            assert extras.cron is not None and not extras.cron.running
            await daemon.start()
            assert extras.cron.running
            assert ticker_owner(profile.home, fake_clock.now()) is not None
            # a document reaches the agent through the intake
            saved = await extras.intake.cache.save("documents", b"hello from a file", name="a.txt")
            note = InboundEvent(
                source=dm("x").source,
                text="what does it say?",
                kind=MessageKind.DOCUMENT,
                media=(Attachment(ref=str(saved)),),
                message_id="d1",
            )
            await pipeline.accept(note)
            await kit.until(lambda: "nice file" in kit.wire())
            sent = model_inputs(kit.server)[-1]
            assert "hello from a file" in sent and sent.endswith("what does it say?")
        finally:
            await daemon.stop()
            await kit.close()
        assert not extras.cron.running
        assert ticker_owner(profile.home, fake_clock.now()) is None
        assert kit.pipeline.front_doors == []  # both doors undone

    asyncio.run(main())
