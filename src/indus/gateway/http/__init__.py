"""The gateway daemon's HTTP surfaces: the OpenAI-compatible API server (P15-30, P15-31) and the
webhook receiver (P15-32).

The daemon (P15-24) calls one function on its own event loop::

    surfaces = await start_http_surfaces(HttpHost(scope, services=make_service, store=handle))
    ...
    await surfaces.aclose()          # at shutdown, before the session services close

``start_http_surfaces`` reads ``gateway.api_server`` and ``gateway.webhook``; each enabled surface
gets its own ``SessionService`` from ``HttpHost.services(spec)``, where ``spec`` says what to
build:

- ``API_SURFACE``: toolsets ``indus-api-server``, answerable requests ``approval`` and
  ``clarify`` (runs answer them over HTTP; chat completions run ``unattended`` and never ask),
  platform ``api_server``;
- ``WEBHOOK_SURFACE``: toolsets ``indus-webhook`` (the untrusted floor), no answerable requests,
  platform ``webhook``, ``untrusted=True``: the factory must not add MCP or plugin toolsets (the
  receiver also checks every session's tool list against the floor and refuses a wider one).

A surface that cannot start (no API key, a refused webhook route, the port taken after the bind
retries) is reported in ``HttpSurfaces.problems`` and left off; the other one still starts.

Under multiplexing (P24-04) the launch profile's host passes ``front``: each listener serves
``front(name, app)`` (a ``SharedIngress`` around the profile's own app). A served secondary's host
passes ``mount`` instead: nothing binds a port; each surface's app is mounted on the launch
listener of the same name at ``/p/<profile>/`` for the paths it serves (``API_PATHS``,
``WEBHOOK_PATHS``), and ``HttpSurfaces.mounted`` names the prefix.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Final

from indus.foundation.scope import Scope
from indus.foundation.secrets import SecretStore, open_secret_store
from indus.gateway.config import gateway_section
from indus.service.session_service import SessionService
from indus.state.api_keys import ApiKeyRing, KeyStore
from indus.state.registry import StoreHandle

__all__ = [
    "API_PATHS",
    "API_SURFACE",
    "WEBHOOK_PATHS",
    "WEBHOOK_SURFACE",
    "HttpHost",
    "HttpSurfaces",
    "ServiceFactory",
    "SurfaceSpec",
    "start_http_surfaces",
]

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SurfaceSpec:
    """What the session service of one HTTP surface must look like (see the module notes)."""

    name: str
    toolsets: tuple[str, ...]
    answerable_kinds: tuple[str, ...]
    platform: str
    untrusted: bool = False


API_SURFACE = SurfaceSpec(
    "api_server", ("indus-api-server",), ("approval", "clarify"), "api_server"
)
WEBHOOK_SURFACE = SurfaceSpec("webhook", ("indus-webhook",), (), "webhook", untrusted=True)

API_PATHS: Final = ("/health", "/v1/", "/api/")
"""What the API app serves (claimed by its mount on a shared listener)."""
WEBHOOK_PATHS: Final = ("/webhooks/",)
"""What the webhook receiver serves (claimed by its mount on a shared listener)."""

type ServiceFactory = Callable[[SurfaceSpec], SessionService | Awaitable[SessionService]]


@dataclass
class HttpHost:
    """What the daemon hands the HTTP surfaces."""

    scope: Scope
    services: ServiceFactory
    store: StoreHandle | None = None
    secrets: KeyStore | None = None  # default: the scope's store, else the profile's
    deliver: Any = None  # webhook.DirectDeliver; default: P14's deliverer registry
    on_status: Callable[[str, dict[str, Any]], object] | None = None
    tickets: Any = None  # service.ws_auth.TicketBook: the browser-control broker's (P24-47)
    front: Callable[[str, Any], Any] | None = None
    """Multiplex host: ``front(surface, app)`` is what that listener serves (P24-04)."""
    mount: Callable[[str, Any, tuple[str, ...]], str] | None = None
    """Served secondary: mount ``app`` for ``paths`` on the host's listener; the URL prefix.
    Raises ``LookupError`` when the host has no such listener."""


@dataclass
class HttpSurfaces:
    api: Any = None  # api_server.ApiServer
    webhook: Any = None  # webhook.WebhookReceiver
    problems: list[str] = field(default_factory=list[str])
    mounted: dict[str, str] = field(default_factory=dict[str, str])
    """Surface → its prefix on the host's shared listener (a served secondary)."""

    async def aclose(self) -> None:
        if self.api is not None:
            await self.api.aclose()
            self.api = None
        if self.webhook is not None:
            await self.webhook.aclose()
            self.webhook = None


def _secrets(host: HttpHost) -> KeyStore:
    if host.secrets is not None:
        return host.secrets
    if isinstance(host.scope.secrets, SecretStore):
        return host.scope.secrets
    mode = "auto"
    config = host.scope.config
    if config is not None:
        mode = str(config.get("security.secret_store") or "auto")
    return open_secret_store(
        host.scope.home, host.scope.profile_id, mode=mode, clock=host.scope.clock
    )


def _browser_broker(host: HttpHost) -> Any:
    """The browser-control broker over the daemon's ticket book when ``browser.control_enabled``
    is on (P24-47), else None (its routes are then not mounted at all)."""
    from indus.gateway.browser_broker import control_enabled, developer_mode

    scope = host.scope
    if host.tickets is None or not control_enabled(scope):
        return None
    from indus.gateway.browser_broker.artifacts import ArtifactStore
    from indus.gateway.browser_broker.broker import BrowserBroker

    return BrowserBroker(
        host.tickets,
        clock=scope.clock,
        developer_mode=lambda: developer_mode(scope),
        enabled=lambda: control_enabled(scope),
        artifacts=ArtifactStore(scope.home / "cache" / "browser-control"),
    )


async def _service(host: HttpHost, spec: SurfaceSpec) -> SessionService:
    made = host.services(spec)
    if inspect.isawaitable(made):
        return await made
    return made


async def start_http_surfaces(host: HttpHost) -> HttpSurfaces:
    """Start the enabled surfaces (see the module notes)."""
    from indus.gateway.http.admin_routes import default_cron
    from indus.gateway.http.api_server import ApiContext, ApiServer, ApiSettings, NoApiKey
    from indus.gateway.http.turns import FileCompletionLog, SessionDesk
    from indus.gateway.http.webhook import WebhookReceiver, WebhookSettings
    from indus.gateway.http.webhook_rules import RouteError

    scope = host.scope
    section = gateway_section(scope)
    surfaces = HttpSurfaces()
    secrets = _secrets(host)
    if section.api_server.enabled:
        settings = ApiSettings.from_scope(scope)
        completions = FileCompletionLog(scope)
        desk = SessionDesk(
            await _service(host, API_SURFACE),
            scope.clock,
            toolsets=API_SURFACE.toolsets,
            idle_close_s=settings.idle_close_seconds,
            completions=completions,
        )
        ctx = ApiContext(
            scope=scope,
            desk=desk,
            keys=ApiKeyRing(scope, secrets),
            settings=lambda: ApiSettings.from_scope(scope),
            clock=scope.clock,
            store=host.store,
            completions=completions,
        )
        ctx.cron = default_cron(ctx)
        ctx.browser_broker = _browser_broker(host)
        status = host.on_status
        front = host.front
        server = ApiServer(
            ctx,
            on_status=(lambda data: status("api_server", data)) if status else None,
            front=(lambda app: front("api_server", app)) if front is not None else None,
        )
        try:
            if host.mount is not None:
                if not ctx.keys.has_keys():
                    raise NoApiKey(
                        "the API server needs an API key: run `indus gateway api-key create` first"
                    )
                surfaces.mounted["api_server"] = host.mount(
                    "api_server", server.profile_app, API_PATHS
                )
            else:
                await server.start()
            surfaces.api = server
        except (NoApiKey, OSError, LookupError) as exc:
            surfaces.problems.append(f"API server: {exc}")
            log.error("the API server did not start: %s", exc)
            await server.aclose()
    if section.webhook.enabled:
        desk = SessionDesk(
            await _service(host, WEBHOOK_SURFACE), scope.clock, toolsets=WEBHOOK_SURFACE.toolsets
        )
        receiver = WebhookReceiver(
            scope,
            settings=lambda: WebhookSettings.from_scope(scope),
            secrets=secrets,
            desk=desk,
            deliver=host.deliver,
        )
        if host.front is not None:
            receiver.app = host.front("webhook", receiver.app)
        try:
            if host.mount is not None:
                receiver.load()
                surfaces.mounted["webhook"] = host.mount("webhook", receiver.app, WEBHOOK_PATHS)
            else:
                await receiver.start()
            surfaces.webhook = receiver
        except (RouteError, OSError, LookupError) as exc:
            surfaces.problems.append(f"webhook receiver: {exc}")
            log.error("the webhook receiver did not start: %s", exc)
            await receiver.aclose()
    return surfaces
