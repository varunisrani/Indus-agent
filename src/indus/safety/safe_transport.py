# pyright: strict
"""An httpx transport that applies the URL policy at connect time and dials the vetted address.

``indus.safety.url_policy.check_url`` answers for one moment; a host name may resolve to a public
address when checked and to an internal one when the connection opens (DNS rebinding).
``SafeTransport`` closes that gap: for **every** request it sends — the first one and each redirect
hop the client follows — it runs the full check again, then connects to one of the addresses that
check vetted instead of letting the connection layer resolve the name a second time. The request
keeps its ``Host`` header and the TLS server name (httpcore's ``sni_hostname`` extension), so
virtual hosting and certificate checks still see the real name.

When the scope's environment names a proxy, the proxy resolves the final target: the request goes
to the proxy unchanged after the host-name rules of the policy (scheme, credentials, blocklist,
metadata names, literal addresses) have passed.

A refusal raises ``ConnectBlocked``, an ``httpx.ConnectError``, so callers handle it like any other
failed connection. ``safe_client`` builds an ``httpx.AsyncClient`` around this transport that does
not read proxy settings from the process environment on its own (the transport decides).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import httpx

from indus.foundation.scope import Scope
from indus.safety.url_policy import Blocked, Resolver, check_url_async, proxy_for

__all__ = ["ConnectBlocked", "SafeTransport", "TransportFactory", "safe_client"]

log = logging.getLogger(__name__)

type TransportFactory = Callable[[str | None], httpx.AsyncBaseTransport]
"""Builds an inner transport; the argument is a proxy URL, or ``None`` for direct connections."""

DEFAULT_MAX_REDIRECTS = 5


class ConnectBlocked(httpx.ConnectError):
    """The URL policy refused this request (``verdict`` says why); nothing was sent."""

    def __init__(self, verdict: Blocked, request: httpx.Request) -> None:
        super().__init__(f"refused by the URL policy: {verdict.reason}", request=request)
        self.verdict = verdict


def _default_factory(proxy: str | None) -> httpx.AsyncBaseTransport:
    return httpx.AsyncHTTPTransport(proxy=proxy) if proxy else httpx.AsyncHTTPTransport()


class SafeTransport(httpx.AsyncBaseTransport):
    """See the module notes. One inner transport per host name (a pooled connection is never
    reused under another TLS name) plus one per proxy URL."""

    def __init__(
        self,
        scope: Scope | None,
        *,
        resolver: Resolver | None = None,
        inner_factory: TransportFactory | None = None,
    ) -> None:
        self._scope = scope
        self._resolver = resolver
        self._factory = inner_factory or _default_factory
        self._direct: dict[str, httpx.AsyncBaseTransport] = {}
        self._proxied: dict[str, httpx.AsyncBaseTransport] = {}

    def _inner(self, table: dict[str, httpx.AsyncBaseTransport], key: str, proxy: str | None):
        inner = table.get(key)
        if inner is None:
            inner = table[key] = self._factory(proxy)
        return inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        verdict = await check_url_async(str(request.url), self._scope, resolver=self._resolver)
        if isinstance(verdict, Blocked):
            raise ConnectBlocked(verdict, request)
        proxy = proxy_for(self._scope, request.url.scheme)
        if proxy is not None:
            inner = self._inner(self._proxied, proxy, proxy)
            return await inner.handle_async_request(request)
        if not verdict.addresses:  # cannot happen without a proxy; refuse rather than resolve
            raise ConnectBlocked(Blocked(verdict.url, "dns", "no vetted address to dial"), request)
        host = verdict.host
        inner = self._inner(self._direct, host, None)
        last: Exception | None = None
        for address in verdict.addresses:
            pinned = self._pin(request, address, host)
            try:
                return await inner.handle_async_request(pinned)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                log.debug("connect to %s (%s) failed: %s", host, address, exc)
                last = exc
        assert last is not None
        raise last

    @staticmethod
    def _pin(request: httpx.Request, address: str, host: str) -> httpx.Request:
        extensions: dict[str, Any] = dict(request.extensions)
        if request.url.scheme == "https":
            extensions["sni_hostname"] = host
        return httpx.Request(
            request.method,
            request.url.copy_with(host=address),
            headers=request.headers,  # carries the original Host header
            stream=request.stream,
            extensions=extensions,
        )

    async def aclose(self) -> None:
        for inner in (*self._direct.values(), *self._proxied.values()):
            try:
                await inner.aclose()
            except Exception:
                log.debug("closing an inner transport failed", exc_info=True)
        self._direct.clear()
        self._proxied.clear()


def safe_client(
    scope: Scope | None,
    *,
    resolver: Resolver | None = None,
    inner_factory: TransportFactory | None = None,
    follow_redirects: bool = True,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    **kwargs: Any,
) -> httpx.AsyncClient:
    """An ``AsyncClient`` whose every request (and redirect hop) passes ``SafeTransport``."""
    transport = SafeTransport(scope, resolver=resolver, inner_factory=inner_factory)
    return httpx.AsyncClient(
        transport=transport,
        trust_env=False,
        follow_redirects=follow_redirects,
        max_redirects=max_redirects,
        **kwargs,
    )
