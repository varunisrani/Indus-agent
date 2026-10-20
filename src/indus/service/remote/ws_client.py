"""A small WebSocket client for the daemon's loopback attach endpoint (P17-16).

``connect(url, subprotocols)`` opens ``ws://<loopback>:<port>/<path>`` over an asyncio stream and
speaks RFC 6455 through ``wsproto`` (a base dependency; it does the handshake key and the frame
masking). It refuses anything but ``ws://`` on a literal loopback address with a port and no
user info, query or fragment (``gateway_control.ws_url_is_safe``): the attach endpoint is never
remote, so nothing goes through a proxy and nothing secret can sit in the URL. The single-use
ticket travels only inside the offered subprotocols, and neither ``repr`` nor a log line shows
them.

The transport: ``subprotocol`` (what the server echoed), ``send(text)``, ``recv()`` (the next
whole text message; ``TransportClosed`` once the socket is gone), ``close(code)``. Pings are
answered as they arrive; binary messages close the socket (1003); a message over
``max_message`` bytes closes it (1009).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from collections.abc import Sequence
from typing import Final
from urllib.parse import urlsplit

from wsproto import ConnectionType, WSConnection
from wsproto.connection import ConnectionState
from wsproto.events import (
    AcceptConnection,
    BytesMessage,
    CloseConnection,
    Event,
    Ping,
    RejectConnection,
    RejectData,
    Request,
    TextMessage,
)

from indus.service.gateway_control import ws_url_is_safe

__all__ = [
    "DEFAULT_MAX_MESSAGE",
    "TransportClosed",
    "WsClientTransport",
    "connect",
]

log = logging.getLogger(__name__)

DEFAULT_MAX_MESSAGE: Final = 8 * 1024 * 1024
_READ_CHUNK: Final = 65536
_CONNECT_TIMEOUT_S: Final = 10.0


class TransportClosed(Exception):
    """The socket is closed (``code`` as the peer or we closed it; 1006 when it just vanished)."""

    def __init__(self, code: int = 1006, reason: str = "") -> None:
        super().__init__(f"closed {code}")
        self.code = code
        self.reason = reason


class WsClientTransport:
    """One open client WebSocket (see the module notes)."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        conn: WSConnection,
        subprotocol: str | None,
        *,
        max_message: int = DEFAULT_MAX_MESSAGE,
        leftover: Sequence[Event] = (),
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._conn = conn
        self.subprotocol = subprotocol
        self._max = max_message
        self._parts: list[str] = []
        self._size = 0
        self._messages: deque[str] = deque()
        self._closed: TransportClosed | None = None
        self._send_lock = asyncio.Lock()
        self._replies: list[bytes] = []
        for event in leftover:
            self._handle(event)

    def __repr__(self) -> str:
        return f"WsClientTransport(subprotocol={self.subprotocol!r})"

    async def send(self, text: str) -> None:
        if self._closed is not None:
            raise self._closed
        await self._write(self._conn.send(TextMessage(data=text)))

    async def recv(self) -> str:
        while True:
            if self._messages:
                return self._messages.popleft()
            if self._closed is not None:
                raise self._closed
            try:
                data = await self._reader.read(_READ_CHUNK)
            except (ConnectionError, OSError):
                data = b""
            if not data:
                self._closed = self._closed or TransportClosed(1006, "connection lost")
                self._writer.close()
                continue
            self._conn.receive_data(data)
            for event in self._conn.events():
                self._handle(event)
            await self._flush_replies()

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self._closed is None:
            self._closed = TransportClosed(code, reason)
            if self._conn.state is ConnectionState.OPEN:
                with contextlib.suppress(Exception):
                    await self._write(self._conn.send(CloseConnection(code=code, reason=reason)))
        self._writer.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._writer.wait_closed(), 2.0)

    # -- internals ------------------------------------------------------------------------------

    async def _write(self, data: bytes) -> None:
        async with self._send_lock:
            self._writer.write(data)
            await self._writer.drain()

    async def _flush_replies(self) -> None:
        replies, self._replies = self._replies, []
        for data in replies:
            with contextlib.suppress(Exception):
                await self._write(data)

    def _queue_reply(self, data: bytes) -> None:
        self._replies.append(data)

    def _handle(self, event: Event) -> None:
        if isinstance(event, TextMessage):
            self._size += len(event.data)
            if self._size > self._max:
                self._fail(1009, "message too big")
                return
            self._parts.append(event.data)
            if event.message_finished:
                self._messages.append("".join(self._parts))
                self._parts.clear()
                self._size = 0
        elif isinstance(event, BytesMessage):
            self._fail(1003, "binary messages are not expected")
        elif isinstance(event, Ping):
            self._queue_reply(self._conn.send(event.response()))
        elif isinstance(event, CloseConnection):
            if self._conn.state is ConnectionState.REMOTE_CLOSING:
                self._queue_reply(self._conn.send(event.response()))
            self._closed = self._closed or TransportClosed(event.code, event.reason or "")

    def _fail(self, code: int, reason: str) -> None:
        if self._closed is None:
            self._closed = TransportClosed(code, reason)
            if self._conn.state is ConnectionState.OPEN:
                self._queue_reply(self._conn.send(CloseConnection(code=code, reason=reason)))


async def connect(
    url: str,
    subprotocols: Sequence[str],
    *,
    timeout: float = _CONNECT_TIMEOUT_S,
    max_message: int = DEFAULT_MAX_MESSAGE,
) -> WsClientTransport:
    """Open the WebSocket at ``url`` offering ``subprotocols``; ``TransportClosed`` when the server
    refuses the upgrade or closes during it, ``ValueError`` for a URL that is not loopback."""
    if not ws_url_is_safe(url) or not url.startswith("ws://"):
        raise ValueError("the attach endpoint must be a ws:// URL on a loopback address")
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or 0
    path = parts.path or "/"
    shown = f"[{host}]" if ":" in host else host
    async with asyncio.timeout(timeout):
        reader, writer = await asyncio.open_connection(host, port)
        try:
            conn = WSConnection(ConnectionType.CLIENT)
            request = Request(host=f"{shown}:{port}", target=path, subprotocols=list(subprotocols))
            writer.write(conn.send(request))
            await writer.drain()
            accepted, leftover = await _handshake(reader, conn)
        except BaseException:
            writer.close()
            raise
    return WsClientTransport(
        reader, writer, conn, accepted.subprotocol, max_message=max_message, leftover=leftover
    )


async def _handshake(
    reader: asyncio.StreamReader, conn: WSConnection
) -> tuple[AcceptConnection, list[Event]]:
    while True:
        data = await reader.read(_READ_CHUNK)
        conn.receive_data(data or None)
        events = list(conn.events())
        for index, event in enumerate(events):
            if isinstance(event, AcceptConnection):
                return event, events[index + 1 :]
            if isinstance(event, RejectConnection | RejectData):
                status = getattr(event, "status_code", 0)
                raise TransportClosed(1002, f"upgrade refused (HTTP {status})")
        if not data:
            raise TransportClosed(1006, "closed during the upgrade")
