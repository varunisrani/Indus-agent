"""The one place that builds HTTP clients for model providers.

Every request identifies as this product only (``User-Agent: indus/<version>`` plus the profile's
own default headers; R01.12). Redirects are not followed, so a key never travels to a host the
route did not name. ``open_stream`` returns a handle whose ``aclose()`` aborts the response, so a
cancelled turn frees its socket at once instead of draining the body (R01.4).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, replace
from types import TracebackType
from typing import Any

import httpx

from indus.foundation.scope import Scope
from indus.providers.hosts import is_loopback
from indus.providers.profile import USER_AGENT
from indus.providers.route import RuntimeRoute
from indus.providers.settings import (
    as_mapping,
    config_of,
    model_entry,
    positive_number,
    provider_entry,
)

log = logging.getLogger(__name__)

API_TIMEOUT_ENV = "INDUS_API_TIMEOUT"
DEFAULT_CONNECT = 30.0
DEFAULT_READ = 120.0
DEFAULT_REQUEST = 1800.0
DEFAULT_POOL = 30.0
IPV4_ANY = "0.0.0.0"


@dataclass(frozen=True)
class Timeouts:
    connect: float = DEFAULT_CONNECT
    read: float = DEFAULT_READ
    write: float = DEFAULT_REQUEST
    pool: float = DEFAULT_POOL

    def to_httpx(self) -> httpx.Timeout:
        return httpx.Timeout(connect=self.connect, read=self.read, write=self.write, pool=self.pool)


def request_timeout(
    provider: str, model: str, *, config: Any, env: Mapping[str, str]
) -> tuple[float, str]:
    """``(seconds, source)``: per-model ``timeout_seconds`` > provider ``request_timeout_seconds``
    > ``INDUS_API_TIMEOUT`` > 1,800 s. Source is ``model``, ``provider``, ``env`` or ``default``."""
    entry = provider_entry(config, provider) if provider else {}
    per_model = positive_number(model_entry(entry, model).get("timeout_seconds"))
    if per_model is not None:
        return per_model, "model"
    per_provider = positive_number(entry.get("request_timeout_seconds"))
    if per_provider is not None:
        return per_provider, "provider"
    from_env = positive_number(env.get(API_TIMEOUT_ENV))
    if from_env is not None:
        return from_env, "env"
    return DEFAULT_REQUEST, "default"


def timeouts_for(
    route: RuntimeRoute, scope: Scope, *, stale_timeout: float | None = None
) -> Timeouts:
    """Socket timeouts for ``route``.

    Write is the request timeout. A configured request timeout (per model or per provider) is also
    the read timeout. Otherwise read is 120 s, raised to the request timeout for loopback
    endpoints (local servers prefill slowly) and to ``stale_timeout`` when that is larger
    (reasoning streams pause; the stale detector, not the socket, decides).
    """
    seconds, source = request_timeout(
        route.provider, route.model, config=config_of(scope), env=scope.env
    )
    timeouts = Timeouts(
        connect=min(DEFAULT_CONNECT, seconds), write=seconds, pool=min(DEFAULT_POOL, seconds)
    )
    if source in ("model", "provider"):
        return replace(timeouts, read=seconds)
    read = DEFAULT_READ
    if is_loopback(route.base_url):
        read = max(read, seconds)
    if stale_timeout is not None and stale_timeout > read:
        read = stale_timeout
    return replace(timeouts, read=read)


def force_ipv4_enabled(scope: Scope) -> bool:
    return as_mapping(config_of(scope).get("network")).get("force_ipv4") is True


def base_headers(route: RuntimeRoute) -> dict[str, str]:
    """Our user agent and the profile's own default headers; nothing that names another product."""
    headers = dict(route.headers)
    headers["User-Agent"] = USER_AGENT
    return headers


def make_transport(*, force_ipv4: bool) -> httpx.AsyncHTTPTransport:
    """The real network transport; ``force_ipv4`` binds the local address to ``0.0.0.0``."""
    if force_ipv4:
        return httpx.AsyncHTTPTransport(http1=True, http2=False, retries=0, local_address=IPV4_ANY)
    return httpx.AsyncHTTPTransport(http1=True, http2=False, retries=0)


def _loop_id() -> int:
    try:
        return id(asyncio.get_running_loop())
    except RuntimeError:
        return 0


class ClientPool:
    """``httpx.AsyncClient`` instances shared per (base URL, key fingerprint, settings)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._clients: dict[tuple[object, ...], httpx.AsyncClient] = {}

    def __len__(self) -> int:
        with self._lock:
            return len(self._clients)

    def client_for(
        self,
        route: RuntimeRoute,
        *,
        timeouts: Timeouts,
        transport: httpx.AsyncBaseTransport | None = None,
        force_ipv4: bool = False,
    ) -> httpx.AsyncClient:
        fingerprint = route.api_key.fingerprint() if route.api_key else ""
        key = (
            route.base_url,
            fingerprint,
            timeouts,
            force_ipv4,
            id(transport) if transport is not None else 0,
            tuple(sorted(route.headers.items())),
            _loop_id(),
        )
        with self._lock:
            client = self._clients.get(key)
            if client is not None and not client.is_closed:
                return client
            client = httpx.AsyncClient(
                headers=base_headers(route),
                timeout=timeouts.to_httpx(),
                transport=transport
                if transport is not None
                else make_transport(force_ipv4=force_ipv4),
                http1=True,
                http2=False,
                follow_redirects=False,
            )
            self._clients[key] = client
            return client

    async def close_all(self) -> int:
        """Close every cached client; returns how many were closed."""
        with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
        closed = 0
        for client in clients:
            if client.is_closed:
                continue
            try:
                await client.aclose()
            except Exception as exc:  # a client bound to a finished loop cannot close cleanly
                log.debug("closing a provider client failed: %s", exc)
            closed += 1
        return closed


_POOL = ClientPool()


def client_for(
    route: RuntimeRoute,
    *,
    timeouts: Timeouts,
    transport: httpx.AsyncBaseTransport | None = None,
    force_ipv4: bool = False,
) -> httpx.AsyncClient:
    """A cached client for ``route`` (one per base URL, key fingerprint and settings)."""
    return _POOL.client_for(route, timeouts=timeouts, transport=transport, force_ipv4=force_ipv4)


async def close_all() -> int:
    return await _POOL.close_all()


class StreamHandle:
    """An open streaming response. ``aclose()`` aborts it; safe to call more than once."""

    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def status_code(self) -> int:
        return self.response.status_code

    @property
    def headers(self) -> httpx.Headers:
        return self.response.headers

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        async for chunk in self.response.aiter_bytes():
            if chunk:
                yield chunk

    async def aread(self) -> bytes:
        return await self.response.aread()

    async def aclose(self) -> None:
        """Close the response without reading the rest; the connection is dropped, not reused."""
        if self._closed:
            return
        self._closed = True
        try:
            await self.response.aclose()
        except Exception as exc:
            log.debug("aborting a provider stream raised %s: %s", type(exc).__name__, exc)

    async def __aenter__(self) -> StreamHandle:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()


async def open_stream(client: httpx.AsyncClient, request: httpx.Request) -> StreamHandle:
    """Send ``request`` and return the response unread, wrapped in a ``StreamHandle``."""
    return StreamHandle(await client.send(request, stream=True))
