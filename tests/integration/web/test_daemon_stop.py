"""The daemon's owner-only ``stop`` control verb behind the dashboard's stop / restart buttons
(P18-15) on a real in-process daemon: the verb writes the planned-stop record ``indus gateway
stop`` writes and runs the normal shutdown, so a dashboard stop stays stopped (the next attach does
not start it again) while a restart's stop is labelled ``restart`` and the dashboard's own ensure
brings the daemon back.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

import pytest

from indus.gateway.control_socket import KNOWN_VERBS, OWNER_ONLY_VERBS
from indus.gateway.daemon.process import GatewayDaemon
from indus.gateway.daemon.side_services import ControlPart
from indus.service import gateway_control
from indus.service.gateway_ensure import planned_stop_reason
from indus.state.gateway_runtime import EXIT_OK, read_planned_stop, runtime_view
from tests.contract.web.rest_app import RestApp, build_rest_app, pause
from tests.integration.gateway.daemon_kit import (
    daemon_scope,
    fake_entry,
    fake_platform,
    fake_registry,
    fake_service,
    fly,
    model_server,
    quick,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Unix sockets")

WRITE = {"profile": "work"}


def _daemon(tmp_path: Path) -> GatewayDaemon:
    return GatewayDaemon(
        daemon_scope(tmp_path),
        options=quick(),
        registry=fake_registry(fake_entry(fake_platform())),
        service_factory=fake_service(model_server(), tmp_path / "svc-locks"),
        parts=[ControlPart()],
    )


def _finished(rest: RestApp, name: str, limit: float = 30.0) -> dict[str, Any]:
    deadline = time.monotonic() + limit
    while True:
        got = rest.client.get(f"/api/actions/{name}/status", params=WRITE).json()
        if got["state"] != "running" or time.monotonic() > deadline:
            return got
        pause(0.05)


def _exit_reason(home: Path) -> object:
    return (runtime_view(home).data or {}).get("exit_reason")


def test_stop_is_a_known_owner_only_verb() -> None:
    assert "stop" in KNOWN_VERBS and "stop" in OWNER_ONLY_VERBS


def test_the_dashboard_stop_ends_the_daemon_as_a_planned_stop(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    home = daemon.scope.home

    async def main() -> None:
        flight = await fly(daemon)
        rest = build_rest_app(daemon.scope)
        try:
            view = await asyncio.to_thread(rest.client.get, "/api/daemon", params=WRITE)
            assert view.json()["running"] is True
            started = await asyncio.to_thread(rest.client.post, "/api/daemon/stop", params=WRITE)
            assert started.status_code == 202, started.text
            assert await asyncio.wait_for(flight.task, 30) == EXIT_OK
            done = await asyncio.to_thread(_finished, rest, "daemon-stop")
            assert done["state"] == "done" and done["exit_code"] == 0, done
            assert any("stopped" in line for line in done["lines"])
        finally:
            rest.close()
            if not flight.task.done():
                await flight.stop()
        assert gateway_control.socket_path_for(home) is None
        assert _exit_reason(home) == "planned_stop:dashboard"
        assert read_planned_stop(home) is None  # the daemon consumed its record
        assert planned_stop_reason(home) == "dashboard"  # the next attach leaves it stopped

    asyncio.run(main())


def test_the_dashboard_restart_stops_then_ensures(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    home = daemon.scope.home
    ensured: list[str] = []

    class Ready:
        state = "ready"
        detail = ""

    def ensure(at: Path, _env: dict[str, str], *, mode: str) -> Ready:
        # what the real ensure decides from: the planned stop is a restart, so it would start
        assert planned_stop_reason(at) is None
        ensured.append(mode)
        return Ready()

    async def main() -> None:
        flight = await fly(daemon)
        rest = build_rest_app(daemon.scope, extras={"daemon.ensure": ensure})
        try:
            started = await asyncio.to_thread(rest.client.post, "/api/daemon/restart", params=WRITE)
            assert started.status_code == 202, started.text
            assert await asyncio.wait_for(flight.task, 30) == EXIT_OK
            done = await asyncio.to_thread(_finished, rest, "daemon-restart")
            assert done["state"] == "done" and done["exit_code"] == 0, done
        finally:
            rest.close()
            if not flight.task.done():
                await flight.stop()
        assert ensured == ["start"]
        assert _exit_reason(home) == "planned_stop:restart"

    asyncio.run(main())


def test_a_second_stop_while_stopping_changes_nothing(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    home = daemon.scope.home

    async def main() -> None:
        flight = await fly(daemon)
        try:
            first = await asyncio.to_thread(
                gateway_control.query, home, "stop", {"reason": "maint\nx", "restart": False}
            )
            assert first.ok and first.result == {
                "stopping": True,
                "restart": False,
                "reason": "maintx",
            }
            assert daemon.lifecycle.stopping
        finally:
            assert await asyncio.wait_for(flight.task, 30) == EXIT_OK
        assert _exit_reason(home) == "planned_stop:maintx"

    asyncio.run(main())
