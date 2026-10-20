"""Gate engagement and the request gates (P18-02, P18-03; R09.3, R09.4).

**Engagement.** Gated mode is on when the bind host is outside the loopback set (``127.0.0.1``,
``::1``, ``localhost``) or a declared public URL names a host outside it. Private LAN, CGNAT and
link-local addresses are public here: a hostile device on the same network is the threat. There
is no flag that turns the gate off (D-16.25). A gated start needs at least one interactive login
provider; ``refusal`` lists why each is off when there is none.

**Middleware order** (outermost first; ``install`` adds them in reverse because Starlette runs the
last-added first):

1. ``ForwardedHeaders`` (``auth.proxy``) — client address and scheme from trusted proxies only;
2. ``BearerSeam`` (``auth.service_bearer``) — opted-in bearer routes, nothing else;
3. ``LoginGate`` — loopback: the session token on every ``/api`` path outside the shared public
   list (``auth.public_paths``); gated: a verified session cookie (renewed through the provider's
   refresh token when it expired), the session token refused even when presented, and an
   unsafe-method ``Origin`` that is not ours refused (403);
4. ``PluginRouteGate`` — dashboard plugin API routes (``/api/ext/<name>/…``) only after auth, so
   an unauthenticated caller gets 401 and never a 404 that names installed plugins;
5. ``HostGuard`` (``auth.host_guard``) — DNS rebinding;
6. ``AuthBodyLimit`` — 64 KiB on the public auth POSTs (they carry credentials, not files).

Unauthenticated: an ``/api`` request gets 401 ``{error: unauthenticated | session_expired,
detail, login_url}`` (``login_url`` only in gated mode); an HTML load in gated mode is redirected
to the login page (or, with exactly one redirect provider and no recent attempt, straight to that
provider: the one-shot ``indus_sso_attempt`` cookie stops a loop).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import parse_qsl, quote, urlsplit

import anyio.to_thread
from starlette.exceptions import HTTPException
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from indus.web.auth import cookies
from indus.web.auth.audit import AuditEvent, AuditLog
from indus.web.auth.host_guard import LOOPBACK_HOSTS, HostRules, host_of_authority, scope_headers
from indus.web.auth.providers import (
    Provider,
    ProviderRegistry,
    ProviderUnavailable,
    RefreshRejected,
    Session,
    SessionSigner,
    first_session,
)
from indus.web.auth.proxy import PublicOrigin, client_ip, peer_is_trusted
from indus.web.auth.public_paths import (
    PUBLIC_API_PATHS,
    QUERY_TOKEN_PATHS,
    is_login_public,
)
from indus.web.auth.service_bearer import PRINCIPAL_KEY
from indus.web.auth.token import SESSION_HEADER, SessionToken, bearer_of
from indus.web.prefix import prefix_from_headers

__all__ = [
    "AUTH_BODY_LIMIT",
    "LOOPBACK_BIND_HOSTS",
    "PLUGIN_ROUTE_PREFIX",
    "SESSION_KEY",
    "AuthBodyLimit",
    "AuthContext",
    "GateRefused",
    "LoginGate",
    "PluginRouteGate",
    "gate_required",
    "is_api_path",
    "refusal",
    "request_prefix",
    "safe_next",
    "describe_mode",
    "session_of",
]

log = logging.getLogger(__name__)

LOOPBACK_BIND_HOSTS: Final[frozenset[str]] = LOOPBACK_HOSTS
AUTH_BODY_LIMIT: Final = 64 * 1024
PLUGIN_ROUTE_PREFIX: Final = "/api/ext/"
SESSION_KEY: Final = "indus.session"
"""Scope ``state`` key holding the verified ``Session`` (gated mode)."""
_UNSAFE: Final = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_NEXT_REFUSED: Final = ("/login", "/auth/", "/api/", "//")


class GateRefused(RuntimeError):
    """Gated mode without any interactive login provider: the server must not bind."""

    def __init__(self, lines: list[str]) -> None:
        super().__init__(
            "the dashboard needs a login provider to serve beyond this machine:\n  "
            + "\n  ".join(lines or ["no provider is configured"])
        )
        self.lines = lines


def _bare(host: str) -> str:
    return host.strip().strip("[]").lower()


def gate_required(bind_host: str, public: PublicOrigin | None) -> bool:
    """Gated mode: a bind outside the loopback set, or a public URL host outside it."""
    if _bare(bind_host) not in LOOPBACK_BIND_HOSTS:
        return True
    return public is not None and _bare(public.host) not in LOOPBACK_BIND_HOSTS


def same_origin(origin: str, host_header: str, rules: HostRules) -> bool:
    """CSRF check for unsafe methods: the ``Origin`` names the very authority the request was
    sent to (``Host``), or a declared public host; ``null`` and non-web origins never match."""
    try:
        parts = urlsplit(origin.strip())
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not parts.netloc or parts.username is not None:
        return False
    netloc = parts.netloc.lower()
    if host_of_authority(netloc) in rules.public_hosts:
        return True
    return bool(host_header) and netloc == host_header.strip().lower()


def refusal(registry: ProviderRegistry) -> list[str]:
    """Why a gated start cannot go ahead (empty when an interactive provider is active)."""
    if registry.interactive():
        return []
    return registry.refusal_lines() or ["no login provider is configured"]


@dataclass
class AuthContext:
    """What the gates need, built once per app."""

    gated: bool
    token: SessionToken
    registry: ProviderRegistry
    signer: SessionSigner
    audit: AuditLog
    rules: HostRules
    public: PublicOrigin | None = None
    plugin_allowed: Callable[[str], bool] = field(default=lambda name: False)
    public_paths: frozenset[str] = PUBLIC_API_PATHS
    api_only: bool = False
    """Headless (serve) mode: every unauthenticated answer is JSON, never a redirect."""


def is_api_path(path: str) -> bool:
    return path == "/api" or path.startswith("/api/")


def request_prefix(scope: Mapping[str, Any]) -> str:
    return prefix_from_headers(scope_headers(scope), trusted=peer_is_trusted(scope))


def session_of(connection: HTTPConnection) -> Session | None:
    state: object = connection.scope.get("state")
    if isinstance(state, dict):
        found = state.get(SESSION_KEY)  # pyright: ignore[reportUnknownMemberType]
        if isinstance(found, Session):
            return found
    return None


def safe_next(raw: str | None) -> str:
    """A same-origin path to land on after login, or ``""``."""
    if not raw or not raw.startswith("/") or raw.startswith("//") or "\\" in raw:
        return ""
    if any(ord(char) < 0x20 for char in raw):
        return ""
    if raw in ("/login", "/api") or raw.startswith(_NEXT_REFUSED):
        return ""
    return raw


def _login_url(scope: Mapping[str, Any], prefix: str) -> str:
    path = scope.get("path", "") or "/"
    query = scope.get("query_string", b"") or b""
    target = safe_next(path)
    if target and query:
        target = f"{target}?{query.decode('latin-1')}"
    if target and target != "/":
        return f"{prefix}/login?next={quote(prefix + target, safe='')}"
    return f"{prefix}/login"


def _state(scope: Scope) -> dict[str, Any]:
    return scope.setdefault("state", {})


async def _send_response(response: Response, scope: Scope, receive: Receive, send: Send) -> None:
    response.headers.setdefault("cache-control", "no-store")
    await response(scope, receive, send)


class LoginGate:
    """Loopback token or gated session (see the module notes)."""

    def __init__(self, app: ASGIApp, *, ctx: AuthContext) -> None:
        self.app = app
        self.ctx = ctx

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if PRINCIPAL_KEY in _state(scope):
            await self.app(scope, receive, send)
            return
        if self.ctx.gated:
            await self._gated(scope, receive, send)
        else:
            await self._loopback(scope, receive, send)

    # -- loopback --

    async def _loopback(self, scope: Scope, receive: Receive, send: Send) -> None:
        path: str = scope.get("path", "")
        if not is_api_path(path) or path in self.ctx.public_paths:
            await self.app(scope, receive, send)
            return
        headers = scope_headers(scope)
        if self.ctx.token.carried_by(headers) or self._query_token(scope, path):
            await self.app(scope, receive, send)
            return
        await _send_response(
            JSONResponse(
                {"error": "unauthenticated", "detail": "The dashboard session token is missing."},
                status_code=401,
            ),
            scope,
            receive,
            send,
        )

    def _query_token(self, scope: Scope, path: str) -> bool:
        if path not in QUERY_TOKEN_PATHS:
            return False
        raw: bytes = scope.get("query_string", b"") or b""
        for key, value in parse_qsl(raw.decode("latin-1")):
            if key == "token" and self.ctx.token.matches(value):
                return True
        return False

    # -- gated --

    def _unauthenticated(self, scope: Scope, *, reason: str, expired: bool = False) -> Response:
        prefix = request_prefix(scope)
        login_url = _login_url(scope, prefix)
        path: str = scope.get("path", "")
        if is_api_path(path) or self.ctx.api_only:
            self.ctx.audit.record(
                AuditEvent.SESSION_REJECTED, reason=reason, path=path, ip=client_ip(scope)
            )
            return JSONResponse(
                {
                    "error": "session_expired" if expired else "unauthenticated",
                    "detail": "Sign in to use the dashboard.",
                    "login_url": login_url,
                },
                status_code=401,
            )
        return RedirectResponse(login_url, status_code=302)

    def _presents_session_token(self, headers: Mapping[str, str]) -> bool:
        if SESSION_HEADER.lower() in headers:
            return True
        return self.ctx.token.matches(bearer_of(headers.get("authorization")))

    async def _gated(self, scope: Scope, receive: Receive, send: Send) -> None:
        headers = scope_headers(scope)
        path: str = scope.get("path", "")
        if self._presents_session_token(headers):
            await _send_response(
                self._unauthenticated(scope, reason="session_token_not_accepted"),
                scope,
                receive,
                send,
            )
            return
        origin = headers.get("origin")
        if (
            scope.get("method", "GET") in _UNSAFE
            and origin is not None
            and not same_origin(origin, headers.get("host", ""), self.ctx.rules)
        ):
            await _send_response(
                JSONResponse(
                    {"error": "forbidden_origin", "detail": "Cross-site request refused."},
                    status_code=403,
                ),
                scope,
                receive,
                send,
            )
            return
        if is_login_public(path):
            await self.app(scope, receive, send)
            return
        connection = HTTPConnection(scope)
        at = cookies.read(connection, cookies.AT_COOKIE)
        rt = cookies.read(connection, cookies.RT_COOKIE)
        hint = cookies.read(connection, cookies.PROVIDER_COOKIE)
        if not at and not rt:
            auto = self._auto_sign_in(scope, connection)
            response = auto or self._unauthenticated(scope, reason="no_session")
            await _send_response(response, scope, receive, send)
            return
        session: Session | None = None
        if at:
            session, unreachable = first_session(self.ctx.registry, at, hint)
            if session is None and unreachable:
                await _send_response(_unavailable(unreachable), scope, receive, send)
                return
        if session is not None:
            _state(scope)[SESSION_KEY] = session
            await self.app(scope, receive, send)
            return
        await self._renew(scope, receive, send, connection, at=at, rt=rt, hint=hint)

    async def _renew(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        connection: HTTPConnection,
        *,
        at: str | None,
        rt: str | None,
        hint: str | None,
    ) -> None:
        expired = self.ctx.signer.verify(at, allow_expired=True) if at else None
        provider: Provider | None = None
        if expired is not None:
            provider = self.ctx.registry.get(expired.provider)
        elif hint:
            provider = self.ctx.registry.get(hint)
        renewed: Session | None = None
        if rt and provider is not None:
            try:
                renewed = await anyio.to_thread.run_sync(provider.refresh, expired, rt)
            except ProviderUnavailable:
                await _send_response(_unavailable(provider.name), scope, receive, send)
                return
            except RefreshRejected:
                renewed = None
        prefix = request_prefix(scope)
        https = cookies.is_https(connection)
        if renewed is None:
            self.ctx.audit.record(
                AuditEvent.REFRESH_FAILURE if rt else AuditEvent.VERIFY_FAILURE,
                provider=provider.name if provider is not None else "",
                reason="session_expired",
                ip=client_ip(scope),
            )
            response = self._unauthenticated(scope, reason="session_expired", expired=True)
            cookies.clear_session(response, prefix=prefix)
            await _send_response(response, scope, receive, send)
            return
        self.ctx.audit.record(
            AuditEvent.REFRESH_SUCCESS,
            provider=renewed.provider,
            user_id=renewed.user_id,
            ip=client_ip(scope),
        )
        _state(scope)[SESSION_KEY] = renewed
        carrier = Response()
        cookies.set_session(
            carrier,
            session_token=renewed.token,
            max_age=renewed.expires_at - self.ctx.signer.now(),
            refresh_token=renewed.refresh_token,
            provider=renewed.provider,
            https=https,
            prefix=prefix,
        )
        extra = [(name, value) for name, value in carrier.raw_headers if name == b"set-cookie"]

        async def send_with_cookies(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = dict(message)
                message["headers"] = [*message.get("headers", []), *extra]
            await send(message)

        await self.app(scope, receive, send_with_cookies)

    def _auto_sign_in(self, scope: Scope, connection: HTTPConnection) -> Response | None:
        path: str = scope.get("path", "")
        if is_api_path(path) or self.ctx.api_only or scope.get("method", "GET") != "GET":
            return None
        prefix = request_prefix(scope)
        if cookies.read(connection, cookies.SSO_ATTEMPT_COOKIE):
            response = RedirectResponse(_login_url(scope, prefix), status_code=302)
            cookies.clear_sso_attempt(response, prefix=prefix)
            return response
        interactive = self.ctx.registry.interactive()
        if len(interactive) != 1 or interactive[0].kind != "redirect":
            return None
        provider = interactive[0]
        target = safe_next(path)
        url = f"{prefix}/auth/login?provider={quote(provider.name, safe='')}"
        if target and target != "/":
            url += f"&next={quote(prefix + target, safe='')}"
        response = RedirectResponse(url, status_code=302)
        cookies.set_sso_attempt(response, https=cookies.is_https(connection), prefix=prefix)
        self.ctx.audit.record(
            AuditEvent.LOGIN_START, provider=provider.name, reason="automatic", ip=client_ip(scope)
        )
        return response


def _unavailable(provider: str) -> Response:
    return JSONResponse(
        {
            "error": "provider_unavailable",
            "detail": f"The sign-in provider {provider!r} cannot be reached; try again shortly.",
        },
        status_code=503,
    )


class PluginRouteGate:
    """``/api/ext/<name>/…``: 404 for a plugin that is not enabled (runs after the login gate)."""

    def __init__(self, app: ASGIApp, *, allowed: Callable[[str], bool]) -> None:
        self.app = app
        self.allowed = allowed

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path: str = scope.get("path", "")
        if scope["type"] in ("http", "websocket") and path.startswith(PLUGIN_ROUTE_PREFIX):
            name = path[len(PLUGIN_ROUTE_PREFIX) :].split("/", 1)[0]
            if not name or not self.allowed(name):
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 4404, "reason": "not found"})
                    return
                await _send_response(
                    JSONResponse(
                        {"error": "not_found", "detail": "No such plugin route."}, status_code=404
                    ),
                    scope,
                    receive,
                    send,
                )
                return
        await self.app(scope, receive, send)


class _BodyTooLarge(HTTPException):
    """Raised from ``receive``; an HTTP exception so FastAPI's body parsing passes it on as 413."""

    def __init__(self) -> None:
        super().__init__(status_code=413, detail="The request body is too large.")


class AuthBodyLimit:
    """Refuse a public auth POST whose body passes ``AUTH_BODY_LIMIT`` (413 JSON)."""

    def __init__(self, app: ASGIApp, *, limit: int = AUTH_BODY_LIMIT) -> None:
        self.app = app
        self.limit = limit

    def _applies(self, scope: Scope) -> bool:
        path: str = scope.get("path", "")
        return (
            scope["type"] == "http"
            and scope.get("method") == "POST"
            and path.startswith("/auth/")
            and is_login_public(path)
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self._applies(scope):
            await self.app(scope, receive, send)
            return
        too_large = JSONResponse(
            {"error": "too_large", "detail": "The request body is too large."}, status_code=413
        )
        length = scope_headers(scope).get("content-length", "")
        if length.isdigit() and int(length) > self.limit:
            await too_large(scope, receive, send)
            return
        seen = 0
        started = False

        async def counted() -> Message:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.limit:
                    raise _BodyTooLarge()
            return message

        async def tracked(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counted, tracked)
        except _BodyTooLarge:
            if started:
                raise
            await too_large(scope, receive, send)


def describe_mode(ctx: AuthContext) -> dict[str, object]:
    """Plain facts for status pages: gated flag and the active interactive providers."""
    return {
        "auth_required": ctx.gated,
        "auth_providers": [provider.name for provider in ctx.registry.interactive()],
    }
