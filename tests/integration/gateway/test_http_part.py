"""The HTTP surfaces inside a real in-process gateway daemon (P15-24 + P15-30..32 integration).

``HttpPart`` builds one session service per ``SurfaceSpec`` through the daemon's
``surface_service`` (the webhook surface is untrusted), reports each surface — or why it could
not start — in the runtime status, never fails the daemon over a surface, and closes the
surfaces and their services at shutdown.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import httpx
import pytest

from indus.gateway.daemon.process import GatewayDaemon
from indus.gateway.http import API_SURFACE, WEBHOOK_SURFACE, SurfaceSpec
from indus.gateway.http.part import HttpPart
from indus.service.session_service import SessionService
from indus.state.api_keys import ApiKeyRing
from indus.state.gateway_runtime import EXIT_OK, read_status
from indus.state.registry import StoreHandle
from tests.contract.gateway._http_kit import make_service
from tests.fakes.model import text_turn
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

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX daemon")

BOTH = (
    "gateway:\n"
    "  api_server: {enabled: true, port: 0}\n"
    "  webhook:\n"
    "    enabled: true\n"
    "    port: 0\n"
    "    routes: {local: {insecure: true, deliver_only: true, deliver: local}}\n"
)
REFUSED = (
    "gateway:\n"
    "  api_server: {enabled: true, port: 0}\n"
    "  webhook:\n"
    "    enabled: true\n"
    "    host: 0.0.0.0\n"
    "    port: 0\n"
    "    routes: {open: {insecure: true}}\n"
)


def _daemon(tmp_path: Path, config: str) -> tuple[GatewayDaemon, HttpPart, list[SurfaceSpec]]:
    scope = daemon_scope(tmp_path, config=config)
    server = model_server(text_turn("x"))
    built: list[SurfaceSpec] = []
    part = HttpPart()

    async def surfaces(daemon: GatewayDaemon, spec: SurfaceSpec) -> SessionService:
        built.append(spec)
        store: StoreHandle = daemon.store
        return make_service(
            daemon.scope,
            server,
            tmp_path / spec.name,
            store,
            toolsets=list(spec.toolsets),
            kinds=spec.answerable_kinds,
            platform=spec.platform,
        )

    daemon = GatewayDaemon(
        scope,
        options=quick(),
        registry=fake_registry(fake_entry(fake_platform())),
        service_factory=fake_service(server, tmp_path / "svc-locks"),
        surface_factory=surfaces,
        parts=[part],
    )
    return daemon, part, built


def test_the_daemon_serves_both_surfaces_and_closes_them(tmp_path: Path) -> None:
    daemon, part, built = _daemon(tmp_path, BOTH)
    secrets = daemon.scope.secrets
    assert secrets is not None
    ApiKeyRing(daemon.scope, secrets).create("daemon")  # type: ignore[arg-type]

    async def main() -> None:
        flight = await fly(daemon)
        try:
            surfaces = part.surfaces
            assert surfaces is not None and surfaces.problems == []
            assert surfaces.api.address[1] > 0 and surfaces.webhook.address[1] > 0
            port = surfaces.webhook.address[1]
            async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
                answer = await client.post(f"http://127.0.0.1:{port}/webhooks/local", json={})
                assert answer.status_code == 200
                health = await client.get(f"http://127.0.0.1:{surfaces.api.address[1]}/health")
                assert health.status_code == 401  # bearer auth on /health too
            assert "webhook" in daemon.status_snapshot()["surfaces"]
        finally:
            assert await flight.stop() == EXIT_OK
        assert part.surfaces is None and part.services == []

    asyncio.run(main())
    assert built == [API_SURFACE, WEBHOOK_SURFACE]
    assert WEBHOOK_SURFACE.untrusted and WEBHOOK_SURFACE.toolsets == ("indus-webhook",)


def test_a_surface_that_cannot_start_is_reported_and_the_daemon_runs(tmp_path: Path) -> None:
    daemon, part, _built = _daemon(tmp_path, REFUSED)

    async def main() -> None:
        flight = await fly(daemon)
        try:
            assert daemon.status.state == "running"
            surfaces = part.surfaces
            assert surfaces is not None
            assert surfaces.api is None and surfaces.webhook is None
            text = " | ".join(surfaces.problems)
            assert "API server" in text and "api-key create" in text
            assert "webhook receiver" in text and "loopback" in text
            status = read_status(daemon.scope.home)
            assert status is not None
            assert "api-key create" in status.surfaces["api_server"]["problem"]
            assert "loopback" in status.surfaces["webhook"]["problem"]
        finally:
            assert await flight.stop() == EXIT_OK

    asyncio.run(main())
