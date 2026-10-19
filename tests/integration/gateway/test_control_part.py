"""The control socket part on a real in-process daemon (P15-24 + P15-27/29 integration).

``ControlPart`` is a required CONTROL part: the socket answers ``identify`` with the daemon's
identity, ``status`` with its status snapshot; a platform that fails to connect is queued for the
reconnect watcher through ``on_platform_down`` and comes back through the daemon's own
``connect_platform``; the ``drain``, ``pause-for-update`` and ``reload-plugins`` verbs act on the
daemon's lifecycle; ``gateway:startup`` fires once the daemon is ready.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, ClassVar

import pytest

from indus.gateway.adapters.base import AdapterConnectError
from indus.gateway.daemon.lifecycle import MAINTENANCE_NOTICE, RESTARTING_NOTICE
from indus.gateway.daemon.process import GatewayDaemon
from indus.gateway.daemon.side_services import ControlPart
from indus.service import gateway_control
from indus.state.gateway_runtime import EXIT_OK
from tests.integration.gateway.daemon_kit import (
    ALLOWED,
    HangingAdapter,
    bot_token,
    daemon_scope,
    fake_entry,
    fake_platform,
    fake_registry,
    fake_service,
    fly,
    model_server,
    quick,
)
from tests.unit.gateway.test_hooks import RECORDER, make_hook, seen

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Unix sockets")


class Flaky(HangingAdapter):
    """Refuses the first connect (retryable), connects after that."""

    tries: ClassVar[int] = 0

    async def _open(self, is_reconnect: bool) -> None:
        type(self).tries += 1
        if type(self).tries == 1:
            raise AdapterConnectError("not yet", retryable=True, code="unreachable")


async def ask(home: Path, verb: str, params: dict[str, Any] | None = None) -> Any:
    return await asyncio.to_thread(gateway_control.query, home, verb, params)


def test_the_control_part_serves_reconnects_and_drains(tmp_path: Path) -> None:
    scope = daemon_scope(tmp_path, env={"DISCORD_BOT_TOKEN": bot_token("discord")})
    recorder = make_hook(scope.home, "recorder", ["gateway:startup"], RECORDER)
    fake = fake_platform()
    part = ControlPart()
    daemon = GatewayDaemon(
        scope,
        options=quick(),
        registry=fake_registry(
            fake_entry(fake),
            fake_entry(fake_platform("discord"), name="discord", adapter=Flaky),
        ),
        service_factory=fake_service(model_server(), tmp_path / "svc-locks"),
        parts=[part],
    )

    async def main() -> None:
        flight = await fly(daemon)
        try:
            assert daemon.platforms["discord"].state == "retrying"
            services = part.services
            assert services is not None
            queued = services.watcher.get("discord")
            assert queued is not None and queued.error_code == "unreachable"
            assert daemon.pipeline.reconnect is services.watcher

            found = await asyncio.to_thread(gateway_control.identify, scope.home)
            assert found is not None and found["pid"] == os.getpid()
            assert found["epoch"] == daemon.boot.epoch and "supervisor" in found
            status = await ask(scope.home, "status")
            assert status.ok and status.result["platforms"]["discord"]["state"] == "retrying"

            # the watcher reconnects through the daemon (resume = due now)
            assert services.watcher.pause("discord") and services.watcher.resume("discord")
            await flight.until(lambda: daemon.platforms["discord"].state == "connected")
            await flight.until(lambda: services.watcher.get("discord") is None)
            assert sorted(daemon.adapters) == ["discord", "telegram"]

            # drain: new turns get the maintenance notice until it is lifted
            drained = await ask(scope.home, "drain")
            assert drained.result["draining"] is True and drained.result["reason"] == "operator"
            assert daemon.status.state == "draining"
            fake.dm(ALLOWED, "anyone there?")
            await flight.until(lambda: fake.texts(ALLOWED) == [MAINTENANCE_NOTICE])
            lifted = await ask(scope.home, "drain", {"on": False})
            assert lifted.result["draining"] is False and daemon.status.state == "running"

            paused = await ask(scope.home, "pause-for-update")
            assert paused.result["reason"] == "update"
            assert daemon.lifecycle.notice() == RESTARTING_NOTICE
            await ask(scope.home, "pause-for-update", {"resume": True})
            assert not daemon.lifecycle.draining

            reloaded = await ask(scope.home, "reload-plugins")
            assert reloaded.result["hooks"] == ["recorder"]
            assert reloaded.result["plugins_reloaded"] is False
        finally:
            assert await flight.stop() == EXIT_OK
        startup = [e for e in seen(recorder) if e["event"] == "gateway:startup"]
        assert len(startup) == 1 and startup[0]["context"]["platforms"] == ["telegram"]
        assert gateway_control.socket_path_for(scope.home) is None

    asyncio.run(main())
