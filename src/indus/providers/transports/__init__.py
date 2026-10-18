"""Wire transports: one per API mode, plus the registry and the helpers they share.

A transport only does format work for its API mode: build the HTTP request for a ``ChatRequest``,
turn decoded server-sent events into normalized stream events, parse a non-streaming JSON reply,
and recognise errors a provider hides inside a successful stream. Clients, timeouts, retries and
stale detection live elsewhere (``providers.http``, ``providers.streaming``, ``providers.retry``).

``get_transport(api_mode)`` returns the shared instance for a mode; more modes can be registered
with ``register_transport``. A transport whose replies are not server-sent events (Bedrock's binary
event stream) also offers ``read_frames(response)``; ``frames_of`` picks the right reader.
"""

from __future__ import annotations

import importlib
import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Iterable, Mapping
from contextlib import aclosing
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

import httpx

from indus.foundation.errors import ProviderError, ProviderHTTPError, ProviderStreamError
from indus.foundation.redact import redact_text
from indus.providers.retry import parse_retry_after
from indus.providers.sse import SSEEvent, SSEItem, aiter_sse
from indus.providers.types import ChatRequest, NormalizedResponse, StreamEvent

log = logging.getLogger(__name__)

ERROR_TEXT_LIMIT = 2000


class UnsupportedApiMode(ProviderError):
    """No transport is registered for the requested API mode (yet)."""


class RouteLike(Protocol):
    """What a transport reads from a resolved route (``providers.route.RuntimeRoute``)."""

    @property
    def model(self) -> str: ...

    @property
    def base_url(self) -> str: ...

    @property
    def api_key(self) -> object: ...


@runtime_checkable
class Transport(Protocol):
    api_mode: str

    def build_request(
        self, route: RouteLike, req: ChatRequest, *, profile: object = None
    ) -> httpx.Request: ...

    def parse_stream(self, events: AsyncIterator[SSEItem]) -> AsyncIterator[StreamEvent]: ...

    def parse_response(self, payload: Mapping[str, Any]) -> NormalizedResponse: ...

    def error_from_stream(self, event: SSEEvent) -> ProviderStreamError | None: ...

    def on_request_rejected(self, route: RouteLike, error: ProviderHTTPError) -> bool:
        """True when the transport changed its request shape for ``route`` and the same call
        should be sent once more (a compatibility retry that does not count as a retry)."""
        ...


type FrameReader = Callable[[httpx.Response], AsyncIterator[Any]]


# -- registry --------------------------------------------------------------------------------

_FACTORIES: dict[str, Callable[[], Transport]] = {}
_INSTANCES: dict[str, Transport] = {}
_BUILTIN_MODULES: dict[str, str] = {
    "chat_completions": "indus.providers.transports.chat_completions",
    "anthropic_messages": "indus.providers.transports.anthropic",
    "responses": "indus.providers.transports.responses",
    "bedrock": "indus.providers.transports.bedrock",
    "gemini": "indus.providers.transports.gemini",
}


def register_transport(api_mode: str, factory: Callable[[], Transport]) -> None:
    """Register (or replace) the transport for ``api_mode``."""
    _FACTORIES[api_mode] = factory
    _INSTANCES.pop(api_mode, None)


def get_transport(api_mode: str) -> Transport:
    """The shared transport instance for ``api_mode``; ``UnsupportedApiMode`` if there is none."""
    if api_mode in _INSTANCES:
        return _INSTANCES[api_mode]
    if api_mode not in _FACTORIES and api_mode in _BUILTIN_MODULES:
        importlib.import_module(_BUILTIN_MODULES[api_mode])
    factory = _FACTORIES.get(api_mode)
    if factory is None:
        raise UnsupportedApiMode(f"unknown API mode {api_mode!r}")
    instance = factory()
    _INSTANCES[api_mode] = instance
    return instance


def registered_api_modes() -> frozenset[str]:
    for module in _BUILTIN_MODULES.values():
        importlib.import_module(module)
    return frozenset(_FACTORIES)


# -- helpers shared by transports --------------------------------------------------------------


def profile_of(route: object, profile: object = None) -> object:
    """The provider profile for a request: explicit, ``route.profile``, or a profile object
    stored as ``route.provider``; ``None`` when there is none."""
    if profile is not None:
        return profile
    attached = getattr(route, "profile", None)
    if attached is not None:
        return attached
    provider = getattr(route, "provider", None)
    if provider is not None and not isinstance(provider, str):
        return provider
    return None


def secret_text(value: object) -> str | None:
    """The plain key text of a route's ``api_key`` (a str or a masking wrapper)."""
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    for method in ("reveal", "get_secret_value"):
        fn = getattr(value, method, None)
        if callable(fn):
            text = fn()
            return str(text) if text else None
    inner = getattr(value, "value", None)
    return str(inner) if inner else None


def route_headers(route: object) -> dict[str, str]:
    headers = getattr(route, "headers", None) or {}
    return {str(k): str(v) for k, v in dict(headers).items()}


def join_url(base_url: str, path: str) -> str:
    return base_url.rstrip("/") + "/" + path.lstrip("/")


def clip(text: str, limit: int = ERROR_TEXT_LIMIT) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def stream_error(
    message: str,
    *,
    error_type: str | None = None,
    code: str | None = None,
    status: int | None = None,
    raw_text: str = "",
) -> ProviderStreamError:
    """A ``ProviderStreamError`` with the OpenAI-like body the classifier reads."""
    safe = clip(redact_text(message))
    error: dict[str, object] = {"message": safe}
    if error_type:
        error["type"] = error_type
    if code:
        error["code"] = code
    if status is not None:
        error["status"] = status
    text = f"provider stream error: {safe}"
    return ProviderStreamError(
        text, body={"error": error}, status=status, raw_text=clip(redact_text(raw_text))
    )


def http_error_from_response(
    status: int,
    headers: Mapping[str, str],
    body: bytes,
    *,
    url: str = "",
    secrets: Iterable[str] = (),
    now: datetime | None = None,
) -> ProviderHTTPError:
    """Build the error for a non-2xx answer; neither message nor body carries a credential
    (shape patterns plus the exact ``secrets`` given, normally the route's key).

    ``Retry-After`` is read by ``providers.retry.parse_retry_after``; an HTTP-date form is
    measured from ``now`` (an injected clock's time; the real time when omitted).
    """
    text = redact_text(body.decode("utf-8", errors="replace"), [s for s in secrets if s])
    parsed: object = None
    message = ""
    try:
        parsed = json.loads(text) if text.strip() else None
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        err = parsed.get("error")
        if isinstance(err, dict):
            message = str(err.get("message") or err.get("type") or "")
        elif isinstance(err, str):
            message = err
        if not message and parsed.get("message"):
            message = str(parsed["message"])
    if not message:
        message = text.strip()
    host = httpx.URL(url).host if url else ""
    where = f" from {host}" if host else ""
    summary = clip(message, 500) or "no details"
    return ProviderHTTPError(
        f"HTTP {status}{where}: {summary}",
        status=status,
        retry_after=parse_retry_after(headers, now=now),
        headers={k.lower(): v for k, v in headers.items()},
        body=parsed if parsed is not None else clip(text),
    )


type Sender = Callable[[httpx.AsyncClient, httpx.Request], Awaitable[httpx.Response]]


async def _send_streaming(client: httpx.AsyncClient, request: httpx.Request) -> httpx.Response:
    return await client.send(request, stream=True)


async def open_call(
    client: httpx.AsyncClient,
    transport: Transport,
    route: RouteLike,
    req: ChatRequest,
    *,
    profile: object = None,
    send: Sender | None = None,
) -> httpx.Response:
    """Send ``req`` and return the open response (status < 400), or raise ``ProviderHTTPError``.

    When the transport answers a rejection by changing its request shape
    (``on_request_rejected``), the call is sent once more; that compatibility retry does not count
    against any retry budget. The caller closes the returned response.
    """
    sender = send or _send_streaming
    compat_retry_used = False
    while True:
        request = transport.build_request(route, req, profile=profile)
        response = await sender(client, request)
        if response.status_code < 400:
            return response
        try:
            body = await response.aread()
        finally:
            await response.aclose()
        key = secret_text(route.api_key)
        error = http_error_from_response(
            response.status_code,
            dict(response.headers),
            body,
            url=str(request.url),
            secrets=[key] if key else (),
        )
        if not compat_retry_used and transport.on_request_rejected(route, error):
            compat_retry_used = True
            log.info("retrying once with a compatible request shape (HTTP %s)", error.status)
            continue
        raise error


async def _aclose(iterator: object) -> None:
    close: Callable[[], Awaitable[object]] | None = getattr(iterator, "aclose", None)
    if close is not None:
        await close()


def frames_of(transport: object, response: httpx.Response) -> AsyncIterator[Any]:
    """The decoded frames of an open streaming response: the transport's own reader when it has
    one (``read_frames``), else server-sent events."""
    reader: FrameReader | None = getattr(transport, "read_frames", None)
    if callable(reader):
        return reader(response)
    return aiter_sse(response)


async def stream_events(
    transport: Transport, response: httpx.Response
) -> AsyncGenerator[StreamEvent]:
    """Normalized events from an open streaming response."""
    items = frames_of(transport, response)
    events = transport.parse_stream(items)
    try:
        async for event in events:
            yield event
    finally:
        await _aclose(events)
        await _aclose(items)


async def iter_call(
    client: httpx.AsyncClient,
    transport: Transport,
    route: RouteLike,
    req: ChatRequest,
    *,
    profile: object = None,
    send: Sender | None = None,
) -> AsyncGenerator[StreamEvent]:
    """Open the call and yield its events; the response is closed however the loop ends
    (finished, failed, or the consuming task cancelled)."""
    response = await open_call(client, transport, route, req, profile=profile, send=send)
    try:
        async with aclosing(stream_events(transport, response)) as events:
            async for event in events:
                yield event
    finally:
        await response.aclose()
