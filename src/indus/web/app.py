"""The dashboard app factory (P18-01).

``create_app(scope, mode, options=...)`` builds one FastAPI app for the profile ``scope`` serves:

- ``mode="dashboard"`` (``indus dashboard``): the JSON/WS API plus the built SPA;
- ``mode="serve"`` (``indus serve``): the same API, headless — never any HTML (D-16.89).

At build time it reads ``dashboard.*`` from the profile's config and the ``INDUS_DASHBOARD_*``
variables from the scope's env, decides loopback or gated mode (``auth.gate``), sets up the login
providers, and refuses to build (``GateRefused``) when gated mode has no interactive provider —
so no caller can bind a gated server without one. A ``trusted_proxies`` entry that would trust
everyone raises ``ProxyConfigError`` the same way. OpenAPI pages are off (``/docs`` is HTML and
``/openapi.json`` would sit outside the API gate).

The middleware stack, outermost first: forwarded headers from trusted proxies → CORS (localhost
origins on 9119, 5173 and the chosen port) → service-bearer seam → login gate → plugin-route gate →
Host guard → auth body limit (``auth.gate`` explains each). Routers come from
``indus.web.routes.ROUTERS`` plus ``options.routers``, then the Chat tab's WebSocket routes
(``/api/pty``, ``/api/pub``, ``/api/events``, ``/api/ws``; ``ws.chat_routes``, in both modes); the
SPA catch-alls go last. The pages served are the ones ``web.bundle.locate`` finds (the checkout's
``apps/web/dist``, else the package's ``web/static``). The profile resolver (``web.scope``) is
installed over the backend's own scope. The lifespan starts the PTY reaper and, at stop, ends
every PTY and removes the channel files.

``DaemonLink`` attaches the dashboard to the profile daemon through P17's ensure (in a background
thread at start-up, never fatal: chat and live views report the verdict) and mints a single-use
attach ticket per dial (``service.remote.channel.grant_for``).
"""

from __future__ import annotations

import importlib
import logging
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from fastapi import APIRouter, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from indus.foundation import config as config_module
from indus.foundation.scope import Scope
from indus.service.ws_auth import (
    DASHBOARD_PURPOSES,
    WS_INTERACTIVE,
    InternalCredential,
    TicketBook,
)
from indus.web.auth.audit import AuditLog
from indus.web.auth.basic import register_basic
from indus.web.auth.gate import (
    AuthBodyLimit,
    AuthContext,
    GateRefused,
    LoginGate,
    PluginRouteGate,
    gate_required,
    refusal,
)
from indus.web.auth.host_guard import HostGuard, HostRules
from indus.web.auth.oidc import HttpClient, register_oidc
from indus.web.auth.providers import ProviderRegistry, SessionSigner
from indus.web.auth.proxy import (
    ForwardedHeaders,
    PublicOrigin,
    parse_trusted_proxies,
    resolve_public_url,
)
from indus.web.auth.service_bearer import BearerSeam, register_drain
from indus.web.auth.token import SessionToken
from indus.web.bundle import locate as locate_bundle
from indus.web.routes import ROUTERS
from indus.web.scope import install as install_scopes
from indus.web.spa import SpaBundle, mount_spa
from indus.web.state import DashboardState, Mode, dashboard_state
from indus.web.ws.chat_routes import ChatSeams, build_chat

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "AppOptions",
    "DaemonLink",
    "build_registry",
    "cors_origins",
    "create_app",
    "dashboard_settings",
]

log = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9119
DEV_SERVER_PORT = 5173


@dataclass
class DaemonLink:
    """The dashboard's tie to the profile daemon (P17): ensure once at start, a ticket per dial."""

    home: Path
    env: Mapping[str, str]
    ensure: Callable[..., Any] | None = None
    verdict: Any = None
    _thread: threading.Thread | None = field(default=None, repr=False)

    def start(self) -> None:
        """Ensure the daemon in the background (never raises)."""

        def run() -> None:
            try:
                ensure = self.ensure
                if ensure is None:
                    from indus.service.gateway_ensure import ensure as real_ensure

                    ensure = real_ensure
                self.verdict = ensure(self.home, dict(self.env), mode="start")
            except Exception as exc:
                log.warning("the dashboard could not reach the profile daemon: %s", exc)

        self._thread = threading.Thread(target=run, name="indus-dashboard-ensure", daemon=True)
        self._thread.start()

    def wait(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def grant(self, purpose: str = WS_INTERACTIVE) -> Any:
        """A fresh attach grant (single-use ticket + endpoint) from the daemon's control socket."""
        from indus.service.remote.channel import grant_for

        return grant_for(self.home, purpose=purpose)


@dataclass
class AppOptions:
    """How ``create_app`` should build the app (CLI flags and test seams)."""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    session_token: SessionToken | None = None
    initial_profile: str = ""
    bundle_dir: Path | None = None
    embedded_chat: bool = True
    console_enabled: bool = False
    daemon: DaemonLink | None = None
    routers: Iterable[APIRouter] = ()
    plugin_allowed: Callable[[str], bool] = field(default=lambda name: False)
    http: HttpClient | None = None
    now: Callable[[], float] = time.time
    monotonic: Callable[[], float] = time.monotonic
    config: Mapping[str, Any] | None = None
    """Settings to use instead of reading the profile's config file (tests)."""
    chat: ChatSeams | None = None
    """Replacements for the chat routes' machine-facing pieces (tests)."""


def dashboard_settings(scope: Scope, override: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The ``dashboard`` mapping of the profile's effective config (``{}`` when absent)."""
    if override is not None:
        section: object = override.get("dashboard", {})
    else:
        view = config_module.load(scope)
        section = view.get("dashboard", {})
    return dict(cast(Mapping[str, Any], section)) if isinstance(section, Mapping) else {}


def _mapping(value: object) -> Mapping[str, Any]:
    return cast(Mapping[str, Any], value) if isinstance(value, Mapping) else {}


def build_registry(
    settings: Mapping[str, Any],
    env: Mapping[str, str],
    signer: SessionSigner,
    *,
    http: HttpClient | None = None,
) -> ProviderRegistry:
    """Every bundled provider that is configured, in login-page order; reasons for the rest."""
    registry = ProviderRegistry()
    register_basic(registry, _mapping(settings.get("basic_auth")), env, signer)
    register_oidc(registry, _mapping(settings.get("oidc")), env, signer, http=http)
    register_drain(registry, env)
    return registry


def cors_origins(port: int) -> list[str]:
    ports = sorted({DEFAULT_PORT, DEV_SERVER_PORT, port})
    return [f"http://{host}:{p}" for p in ports for host in ("localhost", "127.0.0.1")]


def _import_router(spec: str) -> APIRouter:
    module_name, _, attr = spec.partition(":")
    module = importlib.import_module(module_name)
    router = getattr(module, attr)
    if not isinstance(router, APIRouter):
        raise TypeError(f"{spec} is not an APIRouter")
    return router


def _health_router() -> APIRouter:
    """``GET /api/health`` (public): liveness plus who answers, for ``serve discover`` and the
    desktop's boot handshake. Counts and ids only, nothing secret."""
    router = APIRouter()

    @router.get("/api/health")
    async def health(request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        state = dashboard_state(request.app)
        return JSONResponse(
            {
                "ok": True,
                "mode": state.mode,
                "profile_id": state.stable_id,
                "profile": state.profile_name,
                "code_version": state.code_version,
                "auth_required": state.gated,
            },
            headers={"cache-control": "no-store"},
        )

    return router


def create_app(scope: Scope, mode: Mode, *, options: AppOptions | None = None) -> FastAPI:
    """Build the dashboard app (see the module notes)."""
    import indus

    opts = options or AppOptions()
    env = scope.env
    settings = dashboard_settings(scope, opts.config)
    public: PublicOrigin | None = resolve_public_url(env, settings.get("public_url"))
    proxies = parse_trusted_proxies(settings.get("trusted_proxies"))
    gated = gate_required(opts.host, public)
    token = opts.session_token or SessionToken.from_env(env)
    signer = SessionSigner.from_env(env, now=opts.now)
    internal = InternalCredential()
    audit = AuditLog.for_home(scope.home, secrets=[token.value, internal.value])
    registry = build_registry(settings, env, signer, http=opts.http)
    if gated:
        lines = refusal(registry)
        if lines:
            raise GateRefused(lines)
    rules = HostRules(opts.host, frozenset({public.host}) if public is not None else frozenset())
    ctx = AuthContext(
        gated=gated,
        token=token,
        registry=registry,
        signer=signer,
        audit=audit,
        rules=rules,
        public=public,
        plugin_allowed=opts.plugin_allowed,
        api_only=mode == "serve",
    )
    tickets = TicketBook.for_home(scope.home, purposes=DASHBOARD_PURPOSES, monotonic=opts.monotonic)
    bundle = None
    if mode == "dashboard":
        bundle = SpaBundle(opts.bundle_dir or locate_bundle().directory)
    theme = str(settings.get("theme") or "system")
    state = DashboardState(
        mode=mode,
        auth=ctx,
        tickets=tickets,
        internal=internal,
        home=scope.home,
        root=scope.root,
        profile_name=scope.profile_id,
        bound_host=opts.host,
        port=opts.port,
        public=public,
        proxies=proxies,
        embedded_chat=opts.embedded_chat,
        console_enabled=opts.console_enabled,
        initial_profile=opts.initial_profile,
        theme=theme,
        bundle=bundle,
        daemon=opts.daemon,
        code_version=indus.__version__,
    )
    chat = build_chat(state, scope, opts.chat)
    state.extras["chat"] = chat

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if state.daemon is not None:
            state.daemon.start()
        await chat.start()
        try:
            yield
        finally:
            tickets.revoke_all()
            await chat.stop()

    app = FastAPI(
        title="Indus Agent dashboard",
        version=indus.__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.indus = state
    install_scopes(app, chat.scopes)
    app.add_middleware(AuthBodyLimit)
    app.add_middleware(HostGuard, rules=rules)
    app.add_middleware(PluginRouteGate, allowed=opts.plugin_allowed)
    app.add_middleware(LoginGate, ctx=ctx)
    app.add_middleware(BearerSeam, registry=registry, audit=audit)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins(opts.port),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(ForwardedHeaders, proxies=proxies)
    app.include_router(_health_router())
    for spec in ROUTERS:
        app.include_router(_import_router(spec))
    for router in opts.routers:
        app.include_router(router)
    app.include_router(chat.router())
    mount_spa(app, state)
    log.info(
        "dashboard app built (%s, %s mode, %d login providers)",
        mode,
        "gated" if gated else "loopback",
        len(registry.interactive()),
    )
    return app
