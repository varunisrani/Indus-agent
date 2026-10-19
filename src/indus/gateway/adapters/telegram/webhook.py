"""Telegram webhook mode: a tiny HTTP listener Telegram pushes updates to (P15-33).

Webhook mode runs only with a secret. Telegram sends the secret registered with ``setWebhook`` in
the ``X-Telegram-Bot-Api-Secret-Token`` header of every push; a request without the exact secret
(compared in constant time) is refused with 401 and never parsed, so nobody who merely finds the
URL can inject updates. Bodies over 1 MiB are refused with 413, bodies that are not an update
with 400. An accepted update is handed to ``on_update`` (the adapter's admission, which only
queues it) and answered 200 at once.
"""

from __future__ import annotations

import hmac
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Final, Protocol
from urllib.parse import urlparse

from indus.foundation.clock import Clock
from indus.gateway.adapters.telegram.bot_api import Update
from indus.gateway.http.serving import BodyTooLarge, HttpServer, bind_listener, read_body

__all__ = [
    "MAX_BODY_BYTES",
    "SECRET_HEADER",
    "UvicornServer",
    "WebhookOptions",
    "WebhookServer",
    "webhook_app",
]

log = logging.getLogger(__name__)

SECRET_HEADER: Final = "X-Telegram-Bot-Api-Secret-Token"
MAX_BODY_BYTES: Final = 1024 * 1024
DEFAULT_PORT: Final = 8443


@dataclass(frozen=True)
class WebhookOptions:
    url: str
    secret: str | None = field(default=None, repr=False)
    # Loopback by default; ``platforms.telegram.webhook_host`` opts in to a public bind
    # (behind a reverse proxy that terminates TLS, loopback is all it needs).
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT

    @property
    def path(self) -> str:
        return urlparse(self.url).path or "/telegram"


def webhook_app(secret: str, on_update: Callable[[Update], Awaitable[None]], path: str) -> Any:
    """The ASGI app serving ``path`` (FastAPI, imported only when webhook mode is on)."""
    from fastapi import FastAPI, Request, Response

    if not secret:
        raise ValueError("a Telegram webhook needs a secret")
    wanted = secret.encode("utf-8")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    async def receive(request: Request) -> Response:
        given = request.headers.get(SECRET_HEADER, "").encode("utf-8")
        if not hmac.compare_digest(given, wanted):
            log.warning("telegram webhook: refused a push without the right secret")
            return Response(status_code=401)
        try:
            body = await read_body(request, MAX_BODY_BYTES)
        except BodyTooLarge:
            return Response(status_code=413)
        try:
            data: object = json.loads(body)
        except ValueError:
            return Response(status_code=400)
        if not isinstance(data, dict) or not isinstance(data.get("update_id"), int):
            return Response(status_code=400)
        await on_update(Update.from_json(data))  # pyright: ignore[reportUnknownArgumentType]
        return Response(status_code=200)

    # A plain route: the handler reads the raw request (no parameter injection).
    app.add_route(path, receive, methods=["POST"])
    return app


class WebhookServer(Protocol):
    async def start(self, app: Any, host: str, port: int) -> None: ...

    async def stop(self) -> None: ...


class UvicornServer:
    """Serves the webhook app with the gateway's uvicorn helper (``gateway.http.serving``)."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self._server: HttpServer | None = None

    async def start(self, app: Any, host: str, port: int) -> None:
        sock = await bind_listener(host, port, self.clock)
        server = HttpServer(app, sock, name="Telegram webhook")
        try:
            await server.start()
        except BaseException:
            await server.stop()
            raise
        self._server = server

    @property
    def address(self) -> tuple[str, int] | None:
        return self._server.address if self._server is not None else None

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            await server.stop()
