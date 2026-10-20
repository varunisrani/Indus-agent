"""The base for wire fakes of HTTP-callback platforms (P16-01).

A callback platform talks to the gateway two ways: it calls the adapter's listener with signed
callbacks (inbound) and answers the adapter's REST calls (outbound). ``HttpCallbackWire`` fakes
both over one `FakeChatPlatform`, with no socket:

- outbound: ``transport()`` / ``client()`` give the adapter an ``httpx.MockTransport`` routed to
  the handlers a subclass registers with ``@api("POST", "/v1/chats/{chat}/messages")``. A handler
  turns the request into a platform call (``self.platform.post(...)``) and returns an
  ``httpx.Response``; every request is kept in ``calls``. A `FakePlatformError` raised by the
  platform (armed failures, the length cap) becomes what the vendor would do: a connect timeout
  raises ``httpx.ConnectTimeout``, a read timeout ``httpx.ReadTimeout``, anything else becomes
  ``error_response(exc)`` (status by kind, ``Retry-After`` on 429, a JSON error body; override for
  the vendor's own error shape).
- inbound: ``attach(app)`` takes the adapter's listener app (``CallbackListener.app``, built with
  ``serve=False``); ``render(item)`` (subclass) turns a platform-neutral `FakeInbound` into a
  vendor `Callback` (path, body, headers, signed), or None when the platform has no such event;
  ``pump()`` renders and delivers everything queued on the platform, through httpx's ASGI
  transport, and keeps each answer in ``responses``. ``deliver(callback)`` sends one by hand
  (forged signatures, replays).

A contract case wires it as ``AdapterCase(pump=lambda env, adapter: env.wire.pump())``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

from indus.gateway.http.serving import Asgi
from tests.fakes.platform.core import (
    FailureKind,
    FakeChatPlatform,
    FakeInbound,
    FakePlatformError,
)

ApiHandler = Callable[..., Awaitable[httpx.Response]]

# Status a vendor answers with for each failure kind (timeouts never get an answer).
ERROR_STATUS: dict[FailureKind, int] = {
    FailureKind.RATE_LIMIT: 429,
    FailureKind.CHAT_NOT_FOUND: 404,
    FailureKind.THREAD_NOT_FOUND: 404,
    FailureKind.FORBIDDEN: 403,
    FailureKind.PARSE_ERROR: 400,
    FailureKind.TOO_LONG: 400,
}


@dataclass(frozen=True)
class Callback:
    """One vendor callback, ready to send to the adapter's listener."""

    path: str
    body: bytes
    headers: dict[str, str] = field(default_factory=dict[str, str])
    method: str = "POST"
    query: str = ""


@dataclass(frozen=True)
class ApiCall:
    """One outbound request the adapter made."""

    method: str
    path: str
    params: dict[str, str]
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body) if self.body else None


@dataclass(frozen=True)
class _Api:
    method: str
    pattern: re.Pattern[str]
    handler: ApiHandler


def _compile(template: str) -> re.Pattern[str]:
    parts = re.split(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", template)
    out = ""
    for n, part in enumerate(parts):
        out += f"(?P<{part}>[^/]+)" if n % 2 else re.escape(part)
    return re.compile(out + "$")


def api(method: str, template: str) -> Callable[[ApiHandler], ApiHandler]:
    """Mark a wire method as the handler of ``method template`` (``{name}`` = one path segment,
    passed to the handler as a keyword)."""

    def mark(fn: ApiHandler) -> ApiHandler:
        setattr(fn, "__wire_api__", (method.upper(), template))  # noqa: B010
        return fn

    return mark


class HttpCallbackWire:
    """See the module notes. Subclasses add ``@api`` handlers and ``render``."""

    base_url: str = "https://api.fake.test"
    # Where the vendor's callbacks appear to come from.
    client_address: tuple[str, int] = ("203.0.113.7", 44321)

    def __init__(self, platform: FakeChatPlatform) -> None:
        self.platform = platform
        self.calls: list[ApiCall] = []
        self.responses: list[httpx.Response] = []
        self.app: Asgi | None = None
        self._apis: list[_Api] = []
        for name in dir(type(self)):
            fn = getattr(type(self), name)
            marked = getattr(fn, "__wire_api__", None)
            if marked is not None:
                method, template = marked
                self._apis.append(_Api(method, _compile(template), getattr(self, name)))

    # -- outbound: the vendor API ---------------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def client(self, **kw: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self.transport(), base_url=self.base_url, **kw)

    def calls_to(self, method: str, path_prefix: str = "") -> list[ApiCall]:
        return [
            c for c in self.calls if c.method == method.upper() and c.path.startswith(path_prefix)
        ]

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        call = ApiCall(
            request.method,
            request.url.path,
            dict(request.url.params),
            {k.lower(): v for k, v in request.headers.items()},
            body,
        )
        self.calls.append(call)
        for entry in self._apis:
            if entry.method != request.method:
                continue
            match = entry.pattern.match(request.url.path)
            if match is None:
                continue
            try:
                return await entry.handler(call, **match.groupdict())
            except FakePlatformError as exc:
                if exc.kind is FailureKind.CONNECT_TIMEOUT:
                    raise httpx.ConnectTimeout(exc.text, request=request) from None
                if exc.kind is FailureKind.READ_TIMEOUT:
                    raise httpx.ReadTimeout(exc.text, request=request) from None
                return self.error_response(exc)
        return httpx.Response(404, json={"error": {"message": "no such endpoint"}})

    def error_response(self, exc: FakePlatformError) -> httpx.Response:
        """The vendor's answer for an armed failure (override for the vendor's error shape)."""
        headers: dict[str, str] = {}
        if exc.kind is FailureKind.RATE_LIMIT and exc.retry_after is not None:
            headers["Retry-After"] = str(int(exc.retry_after))
        status = ERROR_STATUS.get(exc.kind, 500)
        return httpx.Response(status, json={"error": {"message": exc.text}}, headers=headers)

    # -- inbound: callbacks into the adapter ----------------------------------------------------

    def attach(self, app: Asgi) -> None:
        self.app = app

    def render(self, item: FakeInbound) -> Callback | None:
        """The vendor callback for ``item`` (signed), or None when the platform has none."""
        raise NotImplementedError

    async def deliver(self, callback: Callback) -> httpx.Response:
        if self.app is None:
            raise RuntimeError("attach the adapter's listener app first")
        transport = httpx.ASGITransport(app=self.app, client=self.client_address)  # pyright: ignore[reportArgumentType]
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            url = callback.path + (f"?{callback.query}" if callback.query else "")
            response = await client.request(
                callback.method, url, content=callback.body, headers=callback.headers
            )
        self.responses.append(response)
        return response

    async def pump(self) -> list[httpx.Response]:
        """Deliver every inbound item waiting on the platform; the answers in order."""
        answers: list[httpx.Response] = []
        for item in self.platform.drain_inbound():
            callback = self.render(item)
            if callback is not None:
                answers.append(await self.deliver(callback))
        return answers


def json_body(data: Mapping[str, Any] | list[Any]) -> bytes:
    """Compact JSON bytes, as vendors send them (signatures cover these exact bytes)."""
    return json.dumps(data, separators=(",", ":")).encode()


__all__ = [
    "ERROR_STATUS",
    "ApiCall",
    "ApiHandler",
    "Callback",
    "HttpCallbackWire",
    "api",
    "json_body",
]
