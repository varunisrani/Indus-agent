"""The HTTP surfaces as a gateway daemon part (P15-30..32 in P15-24's daemon).

``HttpPart`` (``Stage.BACKGROUND``; ``PART_FACTORIES`` names ``create``, which skips it when neither
``gateway.api_server.enabled`` nor ``gateway.webhook.enabled`` is set) starts the surfaces once
the platforms are up and boot recovery is done: one session service per ``SurfaceSpec`` from the
daemon's ``surface_service`` (the webhook surface is untrusted: no MCP, no plugins). Each surface's
counters and every surface that could not start (``problems``) are rows of the runtime status
(``surfaces``); a surface that cannot start never stops the daemon (the platforms and cron keep
running). At shutdown the surfaces close first (their running turns and API-started cron runs are
cancelled and awaited), then their session services.
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, Any

from indus.gateway.config import gateway_section
from indus.gateway.daemon.parts import DaemonPart, Stage
from indus.gateway.http import HttpHost, HttpSurfaces, SurfaceSpec, start_http_surfaces

if TYPE_CHECKING:
    from indus.gateway.daemon.process import GatewayDaemon
    from indus.service.session_service import SessionService

__all__ = ["HttpPart", "create"]

log = logging.getLogger(__name__)


class HttpPart(DaemonPart):
    """See the module notes."""

    name = "http"
    stage = Stage.BACKGROUND

    def __init__(self) -> None:
        self.surfaces: HttpSurfaces | None = None
        self.services: list[SessionService] = []

    async def start(self, daemon: GatewayDaemon) -> None:
        async def build(spec: SurfaceSpec) -> SessionService:
            service = await daemon.surface_service(spec)
            self.services.append(service)
            return service

        def status(name: str, data: dict[str, Any]) -> None:
            daemon.status.set_surface(name, data)

        host = HttpHost(daemon.scope, services=build, store=daemon.store, on_status=status)
        self.surfaces = await start_http_surfaces(host)
        for problem in self.surfaces.problems:
            name, _, why = problem.partition(": ")
            key = "api_server" if name == "API server" else "webhook"
            daemon.status.set_surface(key, {"problem": why or problem})
        if self.surfaces.webhook is not None:
            daemon.status.set_surface("webhook", {"listening": True})

    async def stop(self, daemon: GatewayDaemon) -> None:
        surfaces, self.surfaces = self.surfaces, None
        if surfaces is not None:
            try:
                await surfaces.aclose()
            except Exception:
                log.warning("the HTTP surfaces did not close cleanly", exc_info=True)
        services, self.services = self.services, []
        for service in services:
            with contextlib.suppress(Exception):
                await service.aclose()


def create(daemon: GatewayDaemon) -> HttpPart | None:
    """``PART_FACTORIES`` entry (None when no HTTP surface is enabled)."""
    section = gateway_section(daemon.scope)
    if not (section.api_server.enabled or section.webhook.enabled):
        return None
    return HttpPart()
