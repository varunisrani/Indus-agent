"""``indus tui-bridge --stdio``: newline-delimited JSON-RPC 2.0 over the process's stdin/stdout.

Startup order (P12-28):

1. ``claim_stdio`` takes private copies of descriptors 0 and 1 for the protocol, then points
   descriptor 0 at the null device and descriptor 1 at stderr. A stray ``print``, a library banner
   or a child process started without explicit streams can then neither corrupt the frame stream
   nor swallow request bytes (R05.20). The private copies are not inherited by children.
2. MCP discovery starts in the background when the MCP package offers a starter (never awaited).
3. The first frame is ``gateway.ready {theme, heartbeat: false, protocol_version}``.
4. The read loop: one JSON object per line; blank lines are skipped; a line that is not JSON is
   answered ``-32700`` with id ``null`` and a JSON value that is not a frame ``-32600``; requests
   run as their own tasks (a slow handler never blocks reading); responses and notifications from
   the client are dropped (server requests are answered by method).
5. End of input (or SIGTERM) closes every session and returns 0. SIGINT is ignored: Ctrl+C in
   the terminal reaches every process of the foreground group, and the UI decides what it means.

``LineReader`` reads on a daemon thread. A read that ends with ``EAGAIN`` or an empty result while
the descriptor is non-blocking means a child flipped ``O_NONBLOCK`` on the shared open file
description: the flag is cleared and the read retried, at most ``MAX_RECOVERIES`` times per
``RECOVERY_WINDOW_S`` seconds; past that it counts as end of input. ``FrameWriter`` is the one
writer of protocol frames and serialises them under a lock.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import logging
import os
import signal
import socket
import struct
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from typing import Any, BinaryIO

from indus.foundation.scope import Scope
from indus.protocol.bridge import READY_METHOD, BridgeReady
from indus.protocol.envelope import FrameError, Request, parse_frame
from indus.protocol.errors import INTERNAL_ERROR, INVALID_REQUEST, PARSE_ERROR
from indus.protocol.methods import PROTOCOL_VERSION, ProtocolError
from indus.tui_bridge.bridge import Bridge, ContractViolation, ServiceFactory
from indus.tui_bridge.dispatch import Dispatcher, error_frame

__all__ = [
    "MAX_RECOVERIES",
    "RECOVERY_WINDOW_S",
    "FrameWriter",
    "LineReader",
    "StdioServer",
    "claim_stdio",
    "run",
]

log = logging.getLogger(__name__)

MAX_RECOVERIES = 10
RECOVERY_WINDOW_S = 60.0
READ_CHUNK = 65536
DRAIN_S = 2.0
STRICT_ENV = "INDUS_PROTOCOL_STRICT"
MCP_STARTER = "indus.mcp.startup:start_background_discovery"


# -- descriptors ----------------------------------------------------------------------------------


def claim_stdio() -> tuple[int, BinaryIO]:
    """Take the protocol channel: ``(private stdin descriptor, private stdout stream)``.

    Afterwards descriptor 0 reads the null device and descriptor 1 writes to stderr.
    """
    with contextlib.suppress(OSError, ValueError):
        sys.stdout.flush()
    rpc_in = os.dup(0)
    rpc_out = os.dup(1)
    for fd in (rpc_in, rpc_out):
        with contextlib.suppress(OSError):
            os.set_inheritable(fd, False)
    null = os.open(os.devnull, os.O_RDONLY)
    try:
        os.dup2(null, 0)
    finally:
        os.close(null)
    os.dup2(2, 1)
    return rpc_in, os.fdopen(rpc_out, "wb", buffering=0)


# -- reading --------------------------------------------------------------------------------------


def _non_blocking(fd: int) -> bool:
    try:
        return not os.get_blocking(fd)
    except OSError:
        return False


def _clear_receive_timeout(fd: int) -> None:
    """Zero a socket receive timeout on ``fd`` (no-op for pipes and files)."""
    copy = os.dup(fd)
    try:
        probe = socket.socket(fileno=copy)
    except OSError:
        os.close(copy)
        return
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_RCVTIMEO, struct.pack("ll", 0, 0))
    except OSError:
        pass
    finally:
        probe.close()


class LineReader:
    """Lines from a descriptor, read on a daemon thread (see the module notes)."""

    def __init__(
        self,
        fd: int,
        *,
        max_recoveries: int = MAX_RECOVERIES,
        window_s: float = RECOVERY_WINDOW_S,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.fd = fd
        self.max_recoveries = max_recoveries
        self.window_s = window_s
        self._monotonic = monotonic
        self._recoveries: deque[float] = deque()
        self.recovered = 0
        self._queue: asyncio.Queue[bytes | None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._queue = asyncio.Queue()
        self._thread = threading.Thread(target=self._run, name="bridge-stdin", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """End the line stream now (a termination signal); the reading thread is left behind."""
        if self._queue is not None:
            self._queue.put_nowait(None)

    async def readline(self) -> bytes | None:
        """The next line without its newline; None at the end of input."""
        if self._queue is None:
            raise RuntimeError("the reader was not started")
        return await self._queue.get()

    def _deliver(self, item: bytes | None) -> bool:
        loop, queue = self._loop, self._queue
        if loop is None or queue is None:
            return False
        try:
            loop.call_soon_threadsafe(queue.put_nowait, item)
        except RuntimeError:  # the loop is closed: nobody is reading any more
            return False
        return True

    def _recover(self, *, would_block: bool = False) -> bool:
        """Undo what a child did to the shared descriptor and read on; False for a real end of
        input or when the budget for this window is spent.

        An empty read is spurious only while the descriptor is non-blocking; a read that would
        block (``EAGAIN``) never means the end, whatever the flag says (a receive timeout a child
        set on a socket pair has the same effect)."""
        if not would_block and not _non_blocking(self.fd):
            return False
        now = self._monotonic()
        while self._recoveries and now - self._recoveries[0] > self.window_s:
            self._recoveries.popleft()
        if len(self._recoveries) >= self.max_recoveries:
            log.error(
                "stdin keeps failing to block (%d times in %.0f s); treating it as closed",
                len(self._recoveries),
                self.window_s,
            )
            return False
        self._recoveries.append(now)
        self.recovered += 1
        try:
            os.set_blocking(self.fd, True)
        except OSError:
            log.warning("could not make stdin blocking again", exc_info=True)
            return False
        _clear_receive_timeout(self.fd)
        log.warning("a child process changed how stdin blocks; reading on")
        return True

    def _read(self) -> bytes:
        while True:
            try:
                return os.read(self.fd, READ_CHUNK)
            except InterruptedError:
                continue
            except BlockingIOError:
                if self._recover(would_block=True):
                    continue
                return b""
            except OSError:
                log.debug("stdin read failed", exc_info=True)
                return b""

    def _run(self) -> None:
        pending = bytearray()
        while True:
            chunk = self._read()
            if not chunk:
                if self._recover():
                    continue
                break
            pending.extend(chunk)
            while True:
                cut = pending.find(b"\n")
                if cut < 0:
                    break
                line = bytes(pending[:cut])
                del pending[: cut + 1]
                if not self._deliver(line):
                    return
        if pending:
            self._deliver(bytes(pending))
        self._deliver(None)


# -- writing --------------------------------------------------------------------------------------


class FrameWriter:
    """The single writer of protocol frames (one JSON object per line)."""

    def __init__(self, stream: BinaryIO) -> None:
        self.stream = stream
        self._lock = threading.Lock()
        self.broken = False

    def write(self, frame: Mapping[str, Any]) -> bool:
        if self.broken:
            return False
        data = json.dumps(frame, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock:
            try:
                self.stream.write(data.encode("utf-8"))
                self.stream.flush()
            except (OSError, ValueError):
                self.broken = True
                log.info("the client's end of stdout is gone")
                return False
        return True

    def notify(self, method: str, params: Mapping[str, Any]) -> bool:
        return self.write({"jsonrpc": "2.0", "method": method, "params": dict(params)})


# -- the loop -------------------------------------------------------------------------------------


def _start_mcp_discovery(scope: Scope) -> None:
    """Kick off MCP discovery when the MCP package offers a starter; never waits for it."""
    module_name, _, attr = MCP_STARTER.partition(":")
    try:
        starter = getattr(importlib.import_module(module_name), attr)
    except (ImportError, AttributeError):
        return
    try:
        starter(scope)
    except Exception:
        log.warning("background MCP discovery did not start", exc_info=True)


class StdioServer:
    """Serves one client over a reader and a writer (see the module notes)."""

    def __init__(
        self,
        bridge: Bridge,
        reader: LineReader,
        writer: FrameWriter,
        *,
        dispatcher: Dispatcher | None = None,
        drain_s: float = DRAIN_S,
    ) -> None:
        self.bridge = bridge
        self.reader = reader
        self.writer = writer
        self.dispatcher = dispatcher if dispatcher is not None else Dispatcher(bridge)
        self.drain_s = drain_s
        self._tasks: set[asyncio.Task[None]] = set()
        self.violations: list[ContractViolation] = []

    def ready_frame(self) -> dict[str, Any]:
        from indus.tui_bridge.methods_config import tui_setting

        theme = str(tui_setting(self.bridge.scope, "theme") or "auto")
        ready = BridgeReady(theme=theme, heartbeat=False, protocol_version=PROTOCOL_VERSION)
        return {"jsonrpc": "2.0", "method": READY_METHOD, "params": ready.to_wire()}

    async def serve(self) -> int:
        """Run until the end of input; 0 on a clean end, 1 when stdout broke first."""
        loop = asyncio.get_running_loop()
        self.reader.start(loop)
        stop_signal = getattr(signal, "SIGTERM", None)
        if stop_signal is not None:
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                loop.add_signal_handler(stop_signal, self.reader.stop)
        _start_mcp_discovery(self.bridge.scope)
        if not self.writer.write(self.ready_frame()):
            await self._shutdown()
            return 1
        while True:
            line = await self.reader.readline()
            if line is None or self.writer.broken:
                break
            self._take(line)
        await self._shutdown()
        return 1 if self.writer.broken else 0

    def _take(self, line: bytes) -> None:
        text = line.strip()
        if not text:
            return
        try:
            value = json.loads(text)
        except ValueError:
            self.writer.write(error_frame(None, ProtocolError(PARSE_ERROR, "parse error")))
            return
        try:
            frame = parse_frame(value)
        except FrameError as exc:
            request_id = value.get("id") if isinstance(value, dict) else None
            if not isinstance(request_id, str | int) or isinstance(request_id, bool):
                request_id = None
            self.writer.write(error_frame(request_id, ProtocolError(INVALID_REQUEST, str(exc))))
            return
        if not isinstance(frame, Request):
            log.debug("dropped a %s frame from the client", type(frame).__name__)
            return
        task = asyncio.get_running_loop().create_task(
            self._answer(frame), name=f"rpc {frame.method}"
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _answer(self, request: Request) -> None:
        try:
            response = await self.dispatcher.handle(request)
        except ContractViolation as exc:
            self.violations.append(exc)
            log.error("contract violation in %s: %s", request.method, exc)
            response = error_frame(request.id, ProtocolError(INTERNAL_ERROR, str(exc)))
        self.writer.write(response)

    async def _shutdown(self) -> None:
        if self._tasks:
            _done, pending = await asyncio.wait(set(self._tasks), timeout=self.drain_s)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.wait(pending, timeout=self.drain_s)
        await self.bridge.aclose()


def strict_from(scope: Scope) -> bool:
    """Contract checks raise when ``INDUS_PROTOCOL_STRICT`` is set (the test flag)."""
    return scope.env.get(STRICT_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def run(scope: Scope, *, make_service: ServiceFactory | None = None) -> int:
    """Serve the process's stdio until the client closes it; returns the exit code."""
    # Ctrl+C in the terminal reaches the whole foreground group; the UI decides what it means.
    with contextlib.suppress(ValueError, OSError):
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    rpc_in, rpc_out = claim_stdio()
    writer = FrameWriter(rpc_out)
    reader = LineReader(rpc_in)
    if make_service is None:
        from indus.tui_bridge import host

        make_service = host.make_service
        store = host.store_source(scope)
    else:
        store = None

    async def main() -> int:
        bridge = Bridge(
            scope, writer, make_service=make_service, store=store, strict=strict_from(scope)
        )
        return await StdioServer(bridge, reader, writer).serve()

    try:
        return asyncio.run(main())
    finally:
        with contextlib.suppress(OSError):
            rpc_out.close()
