"""The request guard: every request a browser page makes passes the P05 URL check (SSRF).

Checking the address a tool sends the browser to is not enough. A page can make the browser ask
for any address on its own (images, scripts, styles, frames, ``fetch``/XHR, form posts,
WebSockets), and any answer can redirect anywhere, including this machine, the private network or
a cloud metadata service. So every session installs a ``RequestGuard`` as a Playwright route on
its requests (the whole browser context for a browser we launched; our own tab when attached to
the user's browser):

- An ``http(s)`` request is checked with ``url_policy`` for the session's scope (scheme, URL
  secrets, blocklist, metadata floor, private addresses unless ``security.allow_private_urls``).
  A refused request is aborted (``blockedbyclient``) and never sent.
- An allowed request is sent by Playwright with redirects *not* followed, so the target of every
  redirect hop is checked before anything is sent to it:

  - a sub-resource's chain is followed hop by hop here (303, and 301/302 after a POST, turn into
    GET) and the page gets the final answer; the browser never sees the redirect, so no hop
    escapes the check;
  - a navigation (the document of a frame) is answered with its first redirect after the whole
    chain was walked and every hop checked (``HEAD`` requests, ``GET`` when a server refuses
    ``HEAD``), so the browser follows a chain that passed and lands with the right address.
    Playwright sends the hops of a redirect the browser follows without routing them, so a server
    that answers the browser's walk differently from ours can still receive one ``GET`` at a
    refused address; the page tools refuse the address a frame lands on afterwards.

- ``data:``, ``blob:``, ``about:`` and ``javascript:`` never leave the browser and pass; any other
  scheme is aborted.
- A WebSocket's address is checked the same way (``ws`` as ``http``, ``wss`` as ``https``); a
  refused one is closed before it connects.

Any error inside the guard aborts the request (fail closed). The gate is a coroutine
``gate(url) -> reason | None`` the session manager binds to the session's scope.
"""

from __future__ import annotations

import collections
import contextlib
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit

__all__ = [
    "LOCAL_SCHEMES",
    "MAX_HOPS",
    "REDIRECTS",
    "RequestGate",
    "RequestGuard",
]

log = logging.getLogger(__name__)

type RequestGate = Callable[[str], Awaitable[str | None]]
"""``gate(url)``: why the URL is refused, or ``None`` when it may be fetched."""

LOCAL_SCHEMES = frozenset({"data", "blob", "about", "javascript"})
NETWORK_SCHEMES = frozenset({"http", "https"})
SOCKET_SCHEMES: Mapping[str, str] = {"ws": "http", "wss": "https"}
REDIRECTS = frozenset({301, 302, 303, 307, 308})
MAX_HOPS = 20
KEPT_REFUSALS = 50


class ResponseLike(Protocol):
    @property
    def status(self) -> int: ...

    @property
    def headers(self) -> dict[str, str]: ...


class RequestLike(Protocol):
    @property
    def url(self) -> str: ...

    @property
    def method(self) -> str: ...

    def is_navigation_request(self) -> bool: ...


class RouteLike(Protocol):
    async def fetch(
        self,
        *,
        url: str | None = None,
        method: str | None = None,
        post_data: Any = None,
        max_redirects: int | None = None,
    ) -> Any: ...

    async def fulfill(self, *, response: Any = None) -> None: ...

    async def abort(self, error_code: str | None = None) -> None: ...

    async def continue_(self) -> None: ...


class SocketRouteLike(Protocol):
    @property
    def url(self) -> str: ...

    async def close(self, *, code: int | None = None, reason: str | None = None) -> None: ...

    def connect_to_server(self) -> Any: ...


def _scheme(url: str) -> str:
    try:
        return urlsplit(url).scheme.lower()
    except ValueError:
        return ""


def _location(response: ResponseLike, base: str) -> str | None:
    if response.status not in REDIRECTS:
        return None
    headers = {k.lower(): v for k, v in response.headers.items()}
    target = headers.get("location", "").strip()
    return urljoin(base, target) if target else None


def _next_method(status: int, method: str) -> str:
    upper = method.upper()
    if status == 303 and upper != "HEAD":
        return "GET"
    if status in (301, 302) and upper == "POST":
        return "GET"
    return upper


class RequestGuard:
    """One session's route handlers (see the module notes)."""

    def __init__(self, gate: RequestGate) -> None:
        self.gate = gate
        self.refused: collections.deque[tuple[str, str]] = collections.deque(maxlen=KEPT_REFUSALS)

    async def _refusal(self, url: str) -> str | None:
        scheme = _scheme(url)
        if scheme not in NETWORK_SCHEMES:
            return f"the browser may not fetch {scheme or 'scheme-less'} addresses"
        reason = await self.gate(url)
        if reason is not None:
            self.refused.append((url, reason))
        return reason

    async def route(self, route: RouteLike, request: RequestLike) -> None:
        """The route handler for every request of the session."""
        try:
            await self._route(route, request)
        except Exception as exc:
            log.warning("browser request guard failed; request aborted: %s", exc)
            with contextlib.suppress(Exception):
                await route.abort("failed")

    async def _route(self, route: RouteLike, request: RequestLike) -> None:
        url = request.url
        if _scheme(url) in LOCAL_SCHEMES:
            await route.continue_()
            return
        if await self._refusal(url) is not None:
            await route.abort("blockedbyclient")
            return
        first = await route.fetch(max_redirects=0)
        if request.is_navigation_request():
            await self._navigation(route, request, first)
        else:
            await self._resource(route, request, first)

    async def _resource(self, route: RouteLike, request: RequestLike, response: Any) -> None:
        current, method, hops = request.url, request.method.upper(), 0
        while (target := _location(response, current)) is not None:
            hops += 1
            if hops > MAX_HOPS or await self._refusal(target) is not None:
                await route.abort("blockedbyclient" if hops <= MAX_HOPS else "failed")
                return
            next_method = _next_method(response.status, method)
            # a method turned into GET drops the body (None keeps the original request's body)
            body: Any = b"" if next_method != method and method not in ("GET", "HEAD") else None
            response = await route.fetch(
                url=target, method=next_method, post_data=body, max_redirects=0
            )
            current, method = target, next_method
        await route.fulfill(response=response)

    async def _navigation(self, route: RouteLike, request: RequestLike, first: Any) -> None:
        current, response, hops = request.url, first, 0
        while (target := _location(response, current)) is not None:
            hops += 1
            if hops > MAX_HOPS or await self._refusal(target) is not None:
                await route.abort("blockedbyclient" if hops <= MAX_HOPS else "failed")
                return
            response = await route.fetch(url=target, method="HEAD", max_redirects=0)
            if response.status in (405, 501):
                response = await route.fetch(url=target, method="GET", max_redirects=0)
            current = target
        await route.fulfill(response=first)

    async def socket(self, socket_route: SocketRouteLike) -> None:
        """The WebSocket route handler: check the address, then connect or close."""
        url = socket_route.url
        try:
            parts = urlsplit(url)
            mapped = SOCKET_SCHEMES.get(parts.scheme.lower())
            reason = (
                await self._refusal(parts._replace(scheme=mapped).geturl())
                if mapped is not None
                else f"the browser may not open {parts.scheme or 'scheme-less'} sockets"
            )
        except Exception as exc:
            reason = f"the address could not be checked: {type(exc).__name__}"
        if reason is None:
            socket_route.connect_to_server()
            return
        log.warning("browser WebSocket refused: %s", reason)
        with contextlib.suppress(Exception):
            await socket_route.close(code=1008, reason="blocked by the URL policy")
