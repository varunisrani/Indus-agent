"""Our minimal DingTalk Stream Mode client pieces (P16-16).

Stream Mode, as the vendor documents it:

1. ``POST /v1.0/gateway/connections/open`` with ``clientId``, ``clientSecret``, the
   ``subscriptions`` (here one ``CALLBACK`` for the robot topic), ``ua`` and ``localIp`` answers an
   ``endpoint`` (``wss://…``) and a one-time ``ticket``;
2. the client opens a WebSocket to ``endpoint?ticket=…``;
3. frames are JSON ``{specVersion, type, headers{topic, messageId, contentType, time, …},
   data}`` with ``type`` ``SYSTEM`` (topics ``ping``, ``disconnect``), ``EVENT`` or
   ``CALLBACK``; ``data`` is JSON text;
4. every frame is acknowledged with ``{code, headers{contentType, messageId}, message,
   data}`` (``data`` again JSON text); an un-acknowledged callback is pushed again. A ``ping`` is
   answered with its own ``data``; a ``disconnect`` asks the client to open a fresh connection.

Credentials: the secret travels in the open request's body only. The ticket (and the query of a
session webhook) are added by `SecretQuery` inside the transport, so httpx's request log line,
``response.request`` and error texts never show them. The WebSocket uses httpx-ws (the ``dingtalk``
extra), imported only when it opens.

Also here: `Breaker` (backoff 2, 5, 10, 30, 60 s; the fifth identical failure in a row trips a
300 s wait between attempts until the failure changes) and `LogCollapse` (identical log lines within
300 s are counted, not repeated).
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx

from indus.foundation.clock import Clock

__all__ = [
    "API_BASE",
    "BOT_TOPIC",
    "INSTALL_HINT",
    "OPEN_PATH",
    "QUERY_EXTENSION",
    "TOKEN_PATH",
    "Breaker",
    "LogCollapse",
    "SecretQuery",
    "SocketOpener",
    "StreamFrame",
    "StreamRefused",
    "WebSocketLike",
    "ack",
    "httpx_ws_opener",
    "open_connection",
    "socket_url",
]

API_BASE = "https://api.dingtalk.com"
OPEN_PATH = "/v1.0/gateway/connections/open"
TOKEN_PATH = "/v1.0/oauth2/accessToken"
BOT_TOPIC = "/v1.0/im/bot/messages/get"
INSTALL_HINT = "pip install 'indus-agent[dingtalk]'"
# Request extension read by `SecretQuery`: query parameters to add inside the transport.
QUERY_EXTENSION = "indus.secret_query"
ENDPOINT_SUFFIX = ".dingtalk.com"
BACKOFF_S: tuple[float, ...] = (2.0, 5.0, 10.0, 30.0, 60.0)
BREAKER_TRIPS = 5
BREAKER_WAIT_S = 300.0
LOG_WINDOW_S = 300.0


# -- secrets in URLs ------------------------------------------------------------------------------


class SecretQuery(httpx.AsyncBaseTransport):
    """Adds secret query parameters inside the transport (see the module notes).

    Per request: ``extensions={QUERY_EXTENSION: {...}}``. For requests we cannot decorate (the
    WebSocket upgrade made by httpx-ws), ``expect(url, params)`` registers parameters for the next
    request to that scheme, host and path."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner
        self._pending: dict[tuple[str, str, str], dict[str, str]] = {}

    @staticmethod
    def _key(url: httpx.URL) -> tuple[str, str, str]:
        return (url.scheme, url.host, url.path)

    def expect(self, url: str, params: Mapping[str, str]) -> None:
        self._pending[self._key(httpx.URL(url))] = dict(params)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        params: dict[str, str] = {}
        params.update(self._pending.pop(self._key(request.url), {}))
        extra = request.extensions.get(QUERY_EXTENSION)
        if isinstance(extra, Mapping):
            params.update({str(k): str(v) for k, v in extra.items()})  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
        if params:
            request = httpx.Request(
                request.method,
                request.url.copy_merge_params(params),
                headers=request.headers,
                stream=request.stream,
                extensions={k: v for k, v in request.extensions.items() if k != QUERY_EXTENSION},
            )
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


# -- opening a connection -------------------------------------------------------------------------


class StreamRefused(Exception):
    """``connections/open`` (or the token request) answered an error status."""

    def __init__(self, status: int, detail: str = "") -> None:
        super().__init__(
            f"DingTalk refused the request (HTTP {status}){': ' + detail if detail else ''}"
        )
        self.status = status


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except (ValueError, UnicodeDecodeError):
        return ""
    if isinstance(body, dict):
        code = str(body.get("code") or "")  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        message = str(body.get("message") or "")[:200]  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        return " ".join(p for p in (code, message) if p)
    return ""


async def open_connection(
    client: httpx.AsyncClient, client_id: str, client_secret: str, *, ua: str
) -> tuple[str, str]:
    """``(endpoint, ticket)`` for a new stream connection; `StreamRefused` on an error status."""
    body = {
        "clientId": client_id,
        "clientSecret": client_secret,
        "subscriptions": [{"type": "CALLBACK", "topic": BOT_TOPIC}],
        "ua": ua,
        "localIp": "",
    }
    response = await client.post(OPEN_PATH, json=body, timeout=10.0)
    if response.status_code >= 400:
        raise StreamRefused(response.status_code, _detail(response))
    data = response.json()
    endpoint = str(data.get("endpoint") or "") if isinstance(data, dict) else ""  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    ticket = str(data.get("ticket") or "") if isinstance(data, dict) else ""  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    if not endpoint or not ticket:
        raise StreamRefused(response.status_code, "no endpoint or ticket in the answer")
    return endpoint, ticket


def socket_url(endpoint: str) -> str:
    """The endpoint as the https URL httpx-ws upgrades; only ``wss://`` on ``*.dingtalk.com``."""
    parts = urlsplit(endpoint.strip())
    host = (parts.hostname or "").lower()
    if parts.scheme != "wss" or not host.endswith(ENDPOINT_SUFFIX) or parts.username:
        raise ValueError("the stream endpoint is not a wss:// address on dingtalk.com")
    return urlunsplit(("https", parts.netloc, parts.path or "/", "", ""))


class WebSocketLike(Protocol):
    async def receive_text(self) -> str: ...

    async def send_text(self, data: str) -> None: ...


type SocketOpener = Callable[
    [httpx.AsyncClient, str], contextlib.AbstractAsyncContextManager[WebSocketLike]
]


def httpx_ws_opener(client: httpx.AsyncClient, url: str) -> Any:
    """The production opener: ``httpx_ws.aconnect_ws`` on our client (its transport adds the
    ticket)."""
    try:
        import httpx_ws  # pyright: ignore[reportMissingImports]
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            f"DingTalk Stream Mode needs httpx-ws: {INSTALL_HINT}", name=exc.name
        ) from None
    return httpx_ws.aconnect_ws(  # pyright: ignore[reportUnknownMemberType]
        url, client, max_message_size_bytes=4 * 1024 * 1024
    )


# -- frames ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class StreamFrame:
    type: str
    topic: str
    message_id: str
    data: str
    headers: Mapping[str, Any] = field(default_factory=dict[str, Any])

    @classmethod
    def parse(cls, raw: str) -> StreamFrame | None:
        try:
            body = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(body, dict):
            return None
        frame: dict[str, Any] = dict(body)  # pyright: ignore[reportUnknownArgumentType]
        headers = frame.get("headers")
        heads: dict[str, Any] = dict(headers) if isinstance(headers, dict) else {}  # pyright: ignore[reportUnknownArgumentType]
        data = frame.get("data")
        return cls(
            type=str(frame.get("type") or ""),
            topic=str(heads.get("topic") or ""),
            message_id=str(heads.get("messageId") or ""),
            data=data if isinstance(data, str) else json.dumps(data) if data is not None else "",
            headers=heads,
        )

    def payload(self) -> dict[str, Any]:
        """``data`` decoded as a JSON object (empty when it is not one)."""
        try:
            decoded = json.loads(self.data) if self.data else {}
        except ValueError:
            return {}
        return dict(decoded) if isinstance(decoded, dict) else {}  # pyright: ignore[reportUnknownArgumentType]


def ack(
    frame: StreamFrame, *, code: int = 200, message: str = "OK", data: str | None = None
) -> str:
    """The acknowledgement of ``frame`` (``data`` defaults to ``{"response": null}``)."""
    return json.dumps(
        {
            "code": code,
            "headers": {"contentType": "application/json", "messageId": frame.message_id},
            "message": message,
            "data": data if data is not None else json.dumps({"response": None}),
        }
    )


# -- backoff, breaker, log collapse ---------------------------------------------------------------


class Breaker:
    """Reconnect delays: the backoff steps, or ``BREAKER_WAIT_S`` once the same failure has been
    seen ``BREAKER_TRIPS`` times in a row (until a different failure or a success)."""

    def __init__(self) -> None:
        self.attempt = 0
        self.same = 0
        self._last: str | None = None

    @property
    def tripped(self) -> bool:
        return self.same >= BREAKER_TRIPS

    def failed(self, key: str) -> float:
        self.same = self.same + 1 if key == self._last else 1
        self._last = key
        step = BACKOFF_S[min(self.attempt, len(BACKOFF_S) - 1)]
        self.attempt += 1
        return BREAKER_WAIT_S if self.tripped else step

    def succeeded(self) -> None:
        self.attempt = 0
        self.same = 0
        self._last = None


class LogCollapse:
    """Say each distinct line once per window; ``note`` answers None (stay quiet) or how many
    identical lines were held back since it last spoke."""

    def __init__(self, clock: Clock, window_s: float = LOG_WINDOW_S, max_keys: int = 256) -> None:
        self.clock = clock
        self.window_s = window_s
        self.max_keys = max_keys
        self._seen: dict[str, list[float]] = {}

    def note(self, key: str) -> int | None:
        now = self.clock.monotonic()
        entry = self._seen.get(key)
        if entry is not None and now - entry[0] < self.window_s:
            entry[1] += 1
            return None
        held = int(entry[1]) if entry is not None else 0
        if entry is None and len(self._seen) >= self.max_keys:
            self._seen.clear()
        self._seen[key] = [now, 0.0]
        return held
