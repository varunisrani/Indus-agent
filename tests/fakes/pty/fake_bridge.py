"""TEST DOUBLE: a PTY bridge and a WebSocket viewer without any process or socket.

``FakeBridge`` is driven by the test: ``emit(b"…")`` is output the child "printed", ``exit()``
ends the stream, ``stall()`` makes writes fail as a child that stopped reading would. It records
what was written, resized and whether it was closed. ``FakeViewer`` stands in for an attached
WebSocket: it records the bytes and the close code it got and can be told to fail its sends.
"""

from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass, field

from indus.web.pty.resize import TermSize

_PIDS = itertools.count(40_000)


class FakeBridge:
    def __init__(self, pid: int | None = None) -> None:
        self.pid = pid if pid is not None else next(_PIDS)
        self.written: list[bytes] = []
        self.sizes: list[TermSize] = []
        self.closed = False
        self.close_calls = 0
        self.accepting = True
        self._alive = True
        self._out: asyncio.Queue[bytes | None] = asyncio.Queue()

    def emit(self, data: bytes) -> None:
        self._out.put_nowait(data)

    def exit(self) -> None:
        self._alive = False
        self._out.put_nowait(None)

    def stall(self) -> None:
        self.accepting = False

    def is_alive(self) -> bool:
        return self._alive and not self.closed

    async def read(self) -> bytes | None:
        if self.closed:
            return None
        return await self._out.get()

    async def write(self, data: bytes, *, timeout: float = 10.0) -> bool:
        if self.closed or not self.accepting:
            return False
        self.written.append(data)
        return True

    def resize(self, size: TermSize) -> None:
        self.sizes.append(size)

    async def close(self) -> None:
        self.close_calls += 1
        if self.closed:
            return
        self.closed = True
        self._alive = False
        self._out.put_nowait(None)


@dataclass
class FakeViewer:
    name: str = "viewer"
    received: list[bytes] = field(default_factory=list[bytes])
    closes: list[int] = field(default_factory=list[int])
    fail_sends: bool = False

    async def send_bytes(self, data: bytes) -> None:
        if self.fail_sends or self.closes:
            raise ConnectionError("viewer gone")
        self.received.append(data)

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closes.append(code)

    @property
    def text(self) -> bytes:
        return b"".join(self.received)
