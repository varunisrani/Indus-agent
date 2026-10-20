"""TEST DOUBLE: a WebSocket client that drives an ASGI app in the test's own event loop.

The daemon's endpoint (P17-13) is an ASGI app over the daemon session host, and the host must run
on the loop that built it; Starlette's ``TestClient`` runs the app on a loop of its own thread, so
the tests talk ASGI directly instead. ``AsgiWsClient`` builds the upgrade scope (peer address,
headers, offered subprotocols, query string), feeds ``websocket.connect`` / ``receive`` /
``disconnect`` messages and collects what the app sends. ``pause()`` makes the app's sends wait,
as a client that stopped reading would (the kernel buffer is full).

No network is involved; nothing here is product code.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable, Iterable, MutableMapping
from typing import Any

type Asgi = Callable[
    [
        MutableMapping[str, Any],
        Callable[[], Awaitable[MutableMapping[str, Any]]],
        Callable[[MutableMapping[str, Any]], Awaitable[None]],
    ],
    Awaitable[None],
]


class WsClosed(Exception):
    """The app closed the socket."""

    def __init__(self, code: int, reason: str) -> None:
        super().__init__(f"closed {code}: {reason}")
        self.code = code
        self.reason = reason


class AsgiWsClient:
    """One WebSocket connection to ``app`` (see the module notes)."""

    def __init__(
        self,
        app: Asgi,
        *,
        subprotocols: Iterable[str] = (),
        path: str = "/ws",
        query: bytes = b"",
        headers: Iterable[tuple[str, str]] = (),
        peer: tuple[str, int] = ("127.0.0.1", 50123),
    ) -> None:
        self.app = app
        self.scope: dict[str, Any] = {
            "type": "websocket",
            "asgi": {"version": "3.0"},
            "scheme": "ws",
            "http_version": "1.1",
            "path": path,
            "raw_path": path.encode(),
            "root_path": "",
            "query_string": query,
            "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers],
            "client": peer,
            "server": ("127.0.0.1", 8765),
            "subprotocols": list(subprotocols),
            "state": {},
        }
        self._to_app: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._from_app: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._reading = asyncio.Event()
        self._reading.set()
        self._task: asyncio.Task[None] | None = None
        self.accepted: str | None = None
        self.closed: WsClosed | None = None
        self.notes: list[dict[str, Any]] = []
        self.sent_frames = 0
        self._next_id = 0

    # -- the ASGI side --------------------------------------------------------------------------

    async def _receive(self) -> MutableMapping[str, Any]:
        return await self._to_app.get()

    async def _send(self, message: MutableMapping[str, Any]) -> None:
        if message["type"] == "websocket.send":
            await self._reading.wait()
            self.sent_frames += 1
        await self._from_app.put(dict(message))
        if message["type"] == "websocket.close":
            # The socket is gone for the app: anything it reads next is the disconnect.
            await self._to_app.put({"type": "websocket.disconnect", "code": message.get("code")})

    # -- client calls ---------------------------------------------------------------------------

    async def open(self, timeout: float = 5.0) -> str | None:
        """Connect; the subprotocol the app echoed. ``WsClosed`` when it closed at once after
        accepting (a refused upgrade) or before accepting."""
        self._task = asyncio.ensure_future(self.app(self.scope, self._receive, self._send))
        await self._to_app.put({"type": "websocket.connect"})
        message = await asyncio.wait_for(self._from_app.get(), timeout)
        if message["type"] == "websocket.close":
            self.closed = WsClosed(int(message.get("code", 1000)), str(message.get("reason", "")))
            raise self.closed
        assert message["type"] == "websocket.accept", message
        self.accepted = message.get("subprotocol")
        return self.accepted

    async def send_text(self, text: str) -> None:
        await self._to_app.put({"type": "websocket.receive", "text": text})

    async def send_bytes(self, data: bytes) -> None:
        await self._to_app.put({"type": "websocket.receive", "bytes": data})

    async def send_json(self, value: object) -> None:
        await self.send_text(json.dumps(value))

    async def recv(self, timeout: float = 5.0) -> dict[str, Any]:
        """The next frame the app sent (parsed); ``WsClosed`` once it closed."""
        if self.closed is not None and self._from_app.empty():
            raise self.closed
        message = await asyncio.wait_for(self._from_app.get(), timeout)
        if message["type"] == "websocket.close":
            self.closed = WsClosed(int(message.get("code", 1000)), str(message.get("reason", "")))
            raise self.closed
        assert message["type"] == "websocket.send", message
        value = json.loads(message["text"])
        assert isinstance(value, dict)
        return value  # pyright: ignore[reportUnknownVariableType]

    async def request(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 5.0
    ) -> dict[str, Any]:
        """Send a request and read until its response; notifications on the way go to
        ``notes``."""
        self._next_id += 1
        rid = self._next_id
        frame: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            frame["params"] = params
        await self.send_json(frame)
        return await self.response(rid, timeout=timeout)

    async def response(self, rid: int, *, timeout: float = 5.0) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while True:
            frame = await self.recv(max(0.01, end - loop.time()))
            if frame.get("id") == rid and "method" not in frame:
                return frame
            self.notes.append(frame)

    async def until(
        self, check: Callable[[dict[str, Any]], bool], *, timeout: float = 5.0
    ) -> dict[str, Any]:
        """Read (keeping notifications in ``notes``) until a frame passes ``check``."""
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while True:
            frame = await self.recv(max(0.01, end - loop.time()))
            if "method" in frame:
                self.notes.append(frame)
            if check(frame):
                return frame

    async def closed_with(self, timeout: float = 5.0) -> WsClosed:
        """Read until the app closes; the close."""
        try:
            while True:
                frame = await self.recv(timeout)
                if "method" in frame:
                    self.notes.append(frame)
        except WsClosed as closed:
            return closed

    def pause(self) -> None:
        """Stop reading: the app's next send waits."""
        self._reading.clear()

    def resume(self) -> None:
        self._reading.set()

    async def close(self, code: int = 1000) -> None:
        """The client goes away; waits for the app to finish its teardown."""
        self._reading.set()
        await self._to_app.put({"type": "websocket.disconnect", "code": code})
        await self.wait_done()

    async def wait_done(self, timeout: float = 10.0) -> None:
        if self._task is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(self._task), timeout)
