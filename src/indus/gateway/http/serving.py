"""Serving the gateway's HTTP apps: binding, the uvicorn server, and the shared ASGI layers.

``bind_listener(host, port, clock)`` binds a TCP socket itself (so a port of 0 picks a free one
and the real port is known), retrying ``EADDRINUSE`` ``BIND_ATTEMPTS`` times ``BIND_DELAY_S``
apart on the injected clock while a restarting predecessor lets go of the port.
``HttpServer`` runs uvicorn on that socket inside the caller's event loop without installing
signal handlers (the daemon owns signals); ``stop()`` closes the listener and waits a few
seconds for open connections. WebSockets are off unless ``ws=`` names uvicorn's WebSocket
settings (the daemon's attach endpoint, P17-13: implementation, message size cap, no
compression).

The ASGI layers, outermost first, as ``api_server`` stacks them:

- ``SecurityHeaders``: fixed headers on every response, errors included;
- ``CorsGate``: no ``Origin`` header passes untouched; an origin on the allow list gets CORS
  headers (and a preflight answer); any other origin is refused with 403. With no allow list
  (the default) every browser origin is refused;
- ``BearerGate``: ``Authorization: Bearer <key>`` checked by the given verifier; a failure is 401
  with the OpenAI error envelope; the key id is put in ``scope["state"]["api_key_id"]``;
- ``LengthGate``: a ``Content-Length`` over the cap is 413 before the body is read.

``read_body(request, limit)`` reads a body while counting, so a chunked body without a length is
cut at the cap too (``BodyTooLarge`` → 413 by the app's handler).
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import logging
import socket
from collections.abc import Awaitable, Callable, Iterable, Mapping, MutableMapping
from typing import Any

from starlette.requests import Request

from indus.foundation.clock import Clock
from indus.foundation.digits import is_digits

__all__ = [
    "BIND_ATTEMPTS",
    "BIND_DELAY_S",
    "BearerGate",
    "BodyTooLarge",
    "CorsGate",
    "HttpServer",
    "LengthGate",
    "SecurityHeaders",
    "bind_listener",
    "is_loopback",
    "read_body",
]

log = logging.getLogger(__name__)

BIND_ATTEMPTS = 5
BIND_DELAY_S = 1.0
SHUTDOWN_GRACE_S = 3.0
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

type Scope_ = MutableMapping[str, Any]
type Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
type Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
type Asgi = Callable[[Scope_, Receive, Send], Awaitable[None]]

SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"),
    (b"cache-control", b"no-store"),
    (b"cross-origin-resource-policy", b"same-origin"),
)


def is_loopback(host: str | None) -> bool:
    return bool(host) and str(host).strip().lower() in LOOPBACK_HOSTS


# -- binding and serving ------------------------------------------------------------------------


def _open_socket(host: str, port: int) -> socket.socket:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)
    family, kind, proto, _, address = infos[0]
    sock = socket.socket(family, kind, proto)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(address)
        sock.listen(128)
        sock.setblocking(False)
    except BaseException:
        sock.close()
        raise
    return sock


async def bind_listener(
    host: str,
    port: int,
    clock: Clock,
    *,
    attempts: int = BIND_ATTEMPTS,
    delay: float = BIND_DELAY_S,
    opener: Callable[[str, int], socket.socket] = _open_socket,
) -> socket.socket:
    """A listening socket on ``host:port`` (see the module notes)."""
    for attempt in range(1, attempts + 1):
        try:
            return opener(host, port)
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE or attempt == attempts:
                raise
            log.info("port %s:%d is busy (attempt %d of %d)", host, port, attempt, attempts)
            await clock.sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


class HttpServer:
    """One ASGI app served by uvicorn on a socket we bound (see the module notes)."""

    def __init__(
        self,
        app: Asgi,
        sock: socket.socket,
        *,
        name: str,
        ws: Mapping[str, Any] | None = None,
    ) -> None:
        self.app = app
        self.sock = sock
        self.name = name
        self.ws: dict[str, Any] = dict(ws) if ws is not None else {"ws": "none"}
        self._server: Any = None
        self._task: asyncio.Task[None] | None = None

    @property
    def address(self) -> tuple[str, int]:
        host, port = self.sock.getsockname()[:2]
        return str(host), int(port)

    async def start(self) -> None:
        import uvicorn

        config = uvicorn.Config(
            self.app,
            lifespan="off",
            log_config=None,
            access_log=False,
            server_header=False,
            proxy_headers=False,
            timeout_graceful_shutdown=int(SHUTDOWN_GRACE_S),
            **self.ws,
        )
        config.load()
        server = uvicorn.Server(config)
        server.lifespan = config.lifespan_class(config)  # pyright: ignore[reportAttributeAccessIssue]
        await server.startup(sockets=[self.sock])
        if server.should_exit:
            raise OSError(f"the {self.name} server could not start")
        self._server = server
        self._task = asyncio.ensure_future(server.main_loop())
        host, port = self.address
        log.info("%s listening on %s:%d", self.name, host, port)

    async def stop(self) -> None:
        server, task = self._server, self._task
        self._server = self._task = None
        if server is None:
            self.sock.close()
            return
        server.should_exit = True
        if task is not None:
            with contextlib.suppress(Exception):
                await task
        with contextlib.suppress(Exception):
            await server.shutdown(sockets=[self.sock])
        self.sock.close()


# -- request bodies -----------------------------------------------------------------------------


class BodyTooLarge(Exception):
    def __init__(self, limit: int) -> None:
        super().__init__(f"the request body is larger than {limit} bytes")
        self.limit = limit


async def read_body(request: Request, limit: int) -> bytes:
    """The request body, refused with ``BodyTooLarge`` once more than ``limit`` bytes arrive."""
    declared = request.headers.get("content-length")
    if declared is not None and is_digits(declared.strip()) and int(declared) > limit:
        raise BodyTooLarge(limit)
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise BodyTooLarge(limit)
        chunks.append(chunk)
    return b"".join(chunks)


# -- ASGI layers --------------------------------------------------------------------------------


def _header(scope: Scope_, name: bytes) -> str | None:
    for key, value in scope.get("headers") or ():
        if key.lower() == name:
            return value.decode("latin-1")
    return None


async def send_json(
    send: Send, status: int, body: object, headers: Iterable[tuple[bytes, bytes]] = ()
) -> None:
    data = json.dumps(body).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(data)).encode()),
                *headers,
            ],
        }
    )
    await send({"type": "http.response.body", "body": data})


class SecurityHeaders:
    def __init__(self, app: Asgi) -> None:
        self.app = app

    async def __call__(self, scope: Scope_, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def wrapped(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                present = {k.lower() for k, _ in headers}
                headers.extend((k, v) for k, v in SECURITY_HEADERS if k not in present)
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, wrapped)


class CorsGate:
    """See the module notes. ``origins`` may contain ``*`` (every origin)."""

    def __init__(self, app: Asgi, origins: Callable[[], Iterable[str]]) -> None:
        self.app = app
        self.origins = origins

    def _allowed(self, origin: str) -> bool:
        allowed = {o.strip().rstrip("/") for o in self.origins() if o.strip()}
        return "*" in allowed or origin.rstrip("/") in allowed

    async def __call__(self, scope: Scope_, receive: Receive, send: Send) -> None:
        origin = _header(scope, b"origin") if scope["type"] == "http" else None
        if origin is None:
            await self.app(scope, receive, send)
            return
        if not self._allowed(origin):
            await send_json(
                send,
                403,
                {
                    "error": {
                        "message": "origin not allowed",
                        "type": "invalid_request_error",
                        "param": None,
                        "code": "origin_not_allowed",
                    }
                },
            )
            return
        cors = [
            (b"access-control-allow-origin", origin.encode("latin-1")),
            (b"vary", b"Origin"),
            (
                b"access-control-allow-headers",
                b"Authorization, Content-Type, Last-Event-ID, X-Indus-Session-Id",
            ),
            (b"access-control-allow-methods", b"GET, POST, PATCH, DELETE, OPTIONS"),
            (b"access-control-expose-headers", b"X-Indus-Session-Id"),
            (b"access-control-max-age", b"600"),
        ]
        if scope.get("method") == "OPTIONS":
            await send({"type": "http.response.start", "status": 204, "headers": cors})
            await send({"type": "http.response.body", "body": b""})
            return

        async def wrapped(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [*(message.get("headers") or []), *cors]
            await send(message)

        await self.app(scope, receive, wrapped)


AUTH_FAILED = {
    "error": {
        "message": "a valid API key is required (Authorization: Bearer <key>; "
        "make one with `indus gateway api-key create`)",
        "type": "invalid_request_error",
        "param": None,
        "code": "invalid_api_key",
    }
}


class BearerGate:
    """See the module notes. ``verify(token)`` returns the key id or None."""

    def __init__(self, app: Asgi, verify: Callable[[str], str | None]) -> None:
        self.app = app
        self.verify = verify

    async def __call__(self, scope: Scope_, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        header = _header(scope, b"authorization") or ""
        scheme, _, token = header.partition(" ")
        key_id: str | None = None
        if scheme.lower() == "bearer" and token.strip():
            try:
                key_id = self.verify(token.strip())
            except Exception:
                log.warning("the API key check failed", exc_info=True)
                key_id = None
        if key_id is None:
            client = scope.get("client")
            log.info(
                "API request refused: missing or wrong key (%s %s from %s)",
                scope.get("method"),
                scope.get("path"),
                client[0] if client else "?",
            )
            await send_json(send, 401, AUTH_FAILED, [(b"www-authenticate", b"Bearer")])
            return
        state = scope.setdefault("state", {})
        state["api_key_id"] = key_id
        await self.app(scope, receive, send)


class LengthGate:
    def __init__(self, app: Asgi, limit: int, body: Callable[[int], object]) -> None:
        self.app = app
        self.limit = limit
        self.body = body

    async def __call__(self, scope: Scope_, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            declared = _header(scope, b"content-length")
            if declared is not None:
                text = declared.strip()
                if not is_digits(text):
                    await send_json(send, 400, self.body(-1))
                    return
                if int(text) > self.limit:
                    await send_json(send, 413, self.body(self.limit))
                    return
        await self.app(scope, receive, send)


# -- JSON helpers the apps share ----------------------------------------------------------------


class BadRequest(Exception):
    """A request the app answers with ``status`` and the OpenAI error envelope."""

    def __init__(
        self, message: str, *, status: int = 400, code: str | None = None, param: str | None = None
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.param = param

    def body(self) -> dict[str, Any]:
        kind = "invalid_request_error" if self.status < 500 else "server_error"
        if self.status == 429:
            kind = "rate_limit_error"
        return {
            "error": {"message": self.message, "type": kind, "param": self.param, "code": self.code}
        }


async def read_json(request: Request, limit: int) -> Any:
    """The request's JSON body (``BodyTooLarge`` over ``limit``; ``BadRequest`` when not JSON)."""
    raw = await read_body(request, limit)
    if not raw.strip():
        raise BadRequest("the request body must be JSON", code="invalid_json")
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise BadRequest("the request body is not valid JSON", code="invalid_json") from None
