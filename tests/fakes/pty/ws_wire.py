"""TEST DOUBLE: a minimal WebSocket client for loopback test servers (wsproto over asyncio).

Unlike the product's attach client it allows a query string (the dashboard's chat sockets carry
``channel``, ``attach``, ``profile`` …) and binary frames. Used by the web tests and by the fake
TUI child (``fake_tui.py``) to publish its events. Loopback only; nothing here is product code.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from collections.abc import Sequence
from urllib.parse import urlsplit

from wsproto import ConnectionType, WSConnection
from wsproto.connection import ConnectionState
from wsproto.events import (
    AcceptConnection,
    BytesMessage,
    CloseConnection,
    Event,
    Message,
    Ping,
    RejectConnection,
    RejectData,
    Request,
    TextMessage,
)

LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


class WireClosed(Exception):
    def __init__(self, code: int, reason: str = "") -> None:
        super().__init__(f"closed {code}: {reason}")
        self.code = code
        self.reason = reason


class WireClient:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        conn: WSConnection,
        subprotocol: str | None,
        leftover: Sequence[Event],
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._conn = conn
        self.subprotocol = subprotocol
        self._events: deque[Event] = deque(leftover)
        self._parts: list[str | bytes] = []
        self.closed: WireClosed | None = None

    @classmethod
    async def connect(
        cls, url: str, subprotocols: Sequence[str] = (), *, timeout: float = 10.0
    ) -> WireClient:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if host not in LOOPBACK or parts.port is None:
            raise ValueError("test client dials loopback only")
        target = parts.path or "/"
        if parts.query:
            target += "?" + parts.query
        async with asyncio.timeout(timeout):
            reader, writer = await asyncio.open_connection(host, parts.port)
            conn = WSConnection(ConnectionType.CLIENT)
            writer.write(
                conn.send(
                    Request(
                        host=f"{host}:{parts.port}", target=target, subprotocols=list(subprotocols)
                    )
                )
            )
            await writer.drain()
            while True:
                data = await reader.read(65536)
                conn.receive_data(data or None)
                events = list(conn.events())
                for index, event in enumerate(events):
                    if isinstance(event, AcceptConnection):
                        return cls(reader, writer, conn, event.subprotocol, events[index + 1 :])
                    if isinstance(event, RejectConnection | RejectData):
                        writer.close()
                        raise WireClosed(1002, "upgrade refused")
                if not data:
                    writer.close()
                    raise WireClosed(1006, "closed during the upgrade")

    async def _write(self, data: bytes) -> None:
        self._writer.write(data)
        await self._writer.drain()

    async def send_text(self, text: str) -> None:
        await self._write(self._conn.send(TextMessage(data=text)))

    async def send_bytes(self, data: bytes) -> None:
        await self._write(self._conn.send(BytesMessage(data=data)))

    async def recv(self, timeout: float = 10.0) -> str | bytes:
        """The next whole message; ``WireClosed`` once the server closed."""
        async with asyncio.timeout(timeout):
            while True:
                while self._events:
                    event = self._events.popleft()
                    if isinstance(event, Message):
                        self._parts.append(event.data)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
                        if event.message_finished:
                            whole = self._parts
                            self._parts = []
                            if isinstance(whole[0], str):
                                return "".join(str(p) for p in whole)
                            return b"".join(p if isinstance(p, bytes) else b"" for p in whole)
                    elif isinstance(event, Ping):
                        await self._write(self._conn.send(event.response()))
                    elif isinstance(event, CloseConnection):
                        self.closed = WireClosed(event.code, event.reason or "")
                        with contextlib.suppress(Exception):
                            if self._conn.state is ConnectionState.REMOTE_CLOSING:
                                await self._write(self._conn.send(event.response()))
                        raise self.closed
                if self.closed is not None:
                    raise self.closed
                data = await self._reader.read(65536)
                if not data:
                    self.closed = WireClosed(1006, "connection lost")
                    raise self.closed
                self._conn.receive_data(data)
                self._events.extend(self._conn.events())

    async def closed_with(self, timeout: float = 10.0) -> WireClosed:
        """Read (dropping messages) until the server closes; the close."""
        try:
            while True:
                await self.recv(timeout)
        except WireClosed as closed:
            return closed

    async def close(self, code: int = 1000) -> None:
        with contextlib.suppress(Exception):
            if self._conn.state is ConnectionState.OPEN:
                await self._write(self._conn.send(CloseConnection(code=code)))
        with contextlib.suppress(Exception):
            self._writer.close()
            await self._writer.wait_closed()
