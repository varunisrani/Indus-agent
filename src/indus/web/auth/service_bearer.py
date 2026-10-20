"""Service bearer providers and the bearer seam (P18-04).

A token provider opts exact route paths into ``Authorization: Bearer``; no other route ever
accepts its token, and those routes accept nothing else (no cookie, no session token). The seam is
the outermost auth layer: on an opted-in path a recognised token puts a ``TokenPrincipal`` into the
request state and later gates step aside; anything else is 401 (503 when a provider's store could
not be reached). A path whose provider is configured but refused (``ProviderRegistry.
disabled_routes``) answers 404, so the route stays switched off.

The bundled provider guards the daemon drain route (``DRAIN_ROUTE``, handler P18-15) with the
shared secret in ``INDUS_DASHBOARD_DRAIN_SECRET``. The secret must carry at least 256 bits
(``secret_bits``: length × log2 of the smallest alphabet among hex, base64url and printable ASCII
that holds every character); a weaker one keeps the route disabled. Tokens are compared through
SHA-256 digests with ``hmac.compare_digest``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import string
from collections.abc import Mapping
from typing import ClassVar, Final

from starlette.types import ASGIApp, Receive, Scope, Send

from indus.web.auth.audit import AuditEvent, AuditLog
from indus.web.auth.providers import Provider, ProviderRegistry, ProviderUnavailable, TokenPrincipal
from indus.web.auth.proxy import client_ip
from indus.web.auth.token import bearer_of

__all__ = [
    "DRAIN_ROUTE",
    "DRAIN_SECRET_ENV",
    "MIN_SECRET_BITS",
    "PRINCIPAL_KEY",
    "BearerSeam",
    "SharedSecretProvider",
    "register_drain",
    "secret_bits",
]

DRAIN_ROUTE: Final = "/api/daemon/drain"
DRAIN_SECRET_ENV: Final = "INDUS_DASHBOARD_DRAIN_SECRET"
MIN_SECRET_BITS: Final = 256
PRINCIPAL_KEY: Final = "indus.token_principal"
"""Scope ``state`` key holding the ``TokenPrincipal`` of an opted-in route."""

_HEX: Final = frozenset(string.hexdigits)
_B64URL: Final = frozenset(string.ascii_letters + string.digits + "-_")
_PRINTABLE: Final = frozenset(chr(code) for code in range(0x21, 0x7F))


def secret_bits(secret: str) -> float:
    """Estimated strength of ``secret`` in bits (see the module notes); 0 for anything else."""
    if not secret:
        return 0.0
    chars = set(secret)
    for alphabet, size in ((_HEX, 16), (_B64URL, 64), (_PRINTABLE, 95)):
        if chars <= alphabet:
            return len(secret) * math.log2(size)
    return 0.0


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).digest()


class SharedSecretProvider(Provider):
    """A bearer provider that knows one shared secret for a fixed set of routes."""

    kind = "token"
    name: ClassVar[str] = "drain"
    display_name: ClassVar[str] = "Daemon drain (service token)"

    def __init__(self, secret: str, routes: frozenset[str], *, principal: str = "drain") -> None:
        if secret_bits(secret) < MIN_SECRET_BITS:
            raise ValueError(f"the shared secret must carry at least {MIN_SECRET_BITS} bits")
        self._digest = _digest(secret)
        self._routes = routes
        self._principal = principal

    @property
    def token_routes(self) -> frozenset[str]:
        return self._routes

    def verify_bearer(self, token: str, path: str) -> TokenPrincipal | None:
        if path not in self._routes or not token:
            return None
        if hmac.compare_digest(_digest(token), self._digest):
            return TokenPrincipal(principal=self._principal, provider=self.name)
        return None


def register_drain(registry: ProviderRegistry, env: Mapping[str, str]) -> Provider | None:
    """The drain provider from ``INDUS_DASHBOARD_DRAIN_SECRET``; a weak secret disables its
    route."""
    secret = env.get(DRAIN_SECRET_ENV, "")
    if not secret:
        registry.unavailable("drain", f"{DRAIN_SECRET_ENV} is not set")
        return None
    try:
        provider = SharedSecretProvider(secret, frozenset({DRAIN_ROUTE}))
    except ValueError as exc:
        registry.unavailable("drain", str(exc))
        registry.disabled_routes.add(DRAIN_ROUTE)
        return None
    registry.register(provider)
    return provider


async def _answer(send: Send, status: int, payload: dict[str, str]) -> None:
    body = json.dumps(payload).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class BearerSeam:
    """Outermost auth layer for opted-in bearer routes (see the module notes)."""

    def __init__(self, app: ASGIApp, *, registry: ProviderRegistry, audit: AuditLog) -> None:
        self.app = app
        self.registry = registry
        self.audit = audit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")
        if path in self.registry.disabled_routes:
            await _answer(send, 404, {"error": "not_found", "detail": "This route is disabled."})
            return
        if path not in self.registry.token_routes():
            await self.app(scope, receive, send)
            return
        authorization = None
        for name, value in scope.get("headers") or ():
            if name.lower() == b"authorization":
                authorization = value.decode("latin-1")
        token = bearer_of(authorization)
        unreachable = ""
        for provider in self.registry.token_providers():
            try:
                principal = provider.verify_bearer(token, path)
            except ProviderUnavailable:
                unreachable = unreachable or provider.name
                continue
            if principal is not None:
                state = scope.setdefault("state", {})
                state[PRINCIPAL_KEY] = principal
                await self.app(scope, receive, send)
                return
        self.audit.record(
            AuditEvent.TOKEN_AUTH_FAILURE,
            reason="provider_unreachable" if unreachable else "no_provider_accepted",
            path=path,
            ip=client_ip(scope),
        )
        if unreachable:
            await _answer(send, 503, {"error": "unavailable", "detail": "Try again shortly."})
            return
        await _answer(
            send, 401, {"error": "unauthenticated", "detail": "A service token is required."}
        )
