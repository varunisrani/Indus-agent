"""P15-33..35 wiring in the daemon: the three real adapters over their wire fakes.

- each platform's media downloads are registered with the intake while it is up and removed when
  it goes down (``ExtrasPart`` → ``adapter.register_fetchers``);
- a Telegram token revoked while polling parks the platform as ``fatal`` (status shows it, the
  reconnect watcher leaves it alone) instead of a retry loop.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from indus.gateway.adapters.telegram.bot_api import Unauthorized, Update
from indus.state.gateway_runtime import EXIT_OK, read_status
from tests.fakes.clock import FakeClock
from tests.fakes.model import text_turn
from tests.fakes.platform.telegram_wire import FakeBot
from tests.integration.gateway.three_platforms import (
    PLATFORMS,
    START,
    Brain,
    Wires,
    drive,
    extras_of,
    start_daemon,
    stop_daemon,
    three_daemon,
    three_scope,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX sockets")


def test_media_fetchers_follow_each_platform_up_and_down(tmp_path: Path) -> None:
    clock = FakeClock(START)
    wires = Wires.on(clock)
    brain = Brain((text_turn("unused"),))
    daemon = three_daemon(three_scope(tmp_path, clock), wires, brain, tmp_path)

    async def main() -> None:
        task = await start_daemon(daemon, clock, wires)
        try:
            assert {n: daemon.platforms[n].state for n in PLATFORMS} == dict.fromkeys(
                PLATFORMS, "connected"
            )
            extras = extras_of(daemon).extras
            assert extras is not None
            fetchers = extras.intake._fetchers  # pyright: ignore[reportPrivateUsage]
            assert sorted(fetchers) == sorted(PLATFORMS)
            for name in PLATFORMS:  # fatal: the reconnect watcher leaves them down
                await daemon.disconnect_platform(name, fatal=True, reason="test")
                assert name not in fetchers
            assert fetchers == {}
            await daemon.connect_platform("slack")
            assert sorted(fetchers) == ["slack"]
        finally:
            assert await stop_daemon(daemon, task, clock, wires) == EXIT_OK
        assert fetchers == {}

    asyncio.run(main())


def test_a_revoked_telegram_token_parks_the_platform_as_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(START)
    wires = Wires.on(clock)
    brain = Brain((text_turn("unused"),))
    scope = three_scope(tmp_path, clock)
    daemon = three_daemon(scope, wires, brain, tmp_path)
    revoked = {"now": False}
    original = FakeBot.get_updates

    async def get_updates(self: FakeBot, *args: object, **kw: object) -> list[Update]:
        if revoked["now"]:
            raise Unauthorized("Unauthorized")
        return await original(self, *args, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(FakeBot, "get_updates", get_updates)

    async def main() -> None:
        task = await start_daemon(daemon, clock, wires)
        try:
            assert daemon.platforms["telegram"].state == "connected"
            revoked["now"] = True
            await drive(
                clock,
                wires,
                lambda: daemon.platforms["telegram"].state == "fatal",
                what="telegram parked as fatal",
            )
            row = daemon.platforms["telegram"]
            assert row.error_code == "telegram_auth"
            assert "telegram" not in daemon.adapters
            assert daemon.status.state == "degraded"
            status = read_status(scope.home)
            assert status is not None and status.platforms["telegram"].state == "fatal"
            polls = len(wires.telegram.bot.calls)
            clock.advance(600)  # no retry loop: nothing polls again
            await drive(clock, wires, lambda: True, what="settle")
            assert len(wires.telegram.bot.calls) == polls
            assert daemon.platforms["telegram"].state == "fatal"
            assert {daemon.platforms[n].state for n in ("discord", "slack")} == {"connected"}
        finally:
            assert await stop_daemon(daemon, task, clock, wires) == EXIT_OK

    asyncio.run(main())
