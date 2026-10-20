"""Stdout belongs to the protocol: the guard that keeps it that way (P19-01, R09.14).

``claim()`` takes the process's standard streams for the ACP connection before anything that might
print is imported:

1. private, non-inheritable copies of descriptors 0 and 1 are made for the protocol;
2. descriptor 0 then reads the null device and descriptor 1 writes to stderr, so a child process
   started without explicit streams, or a library writing to fd 1 directly, can neither swallow
   request bytes nor corrupt the frame stream;
3. ``sys.stdout`` becomes a ``StrayWriter``: whatever Python code prints is dropped and logged as
   one error line naming how much was written (never the text — it may hold anything).

The returned ``Claimed`` holds the private descriptors; ``streams()`` opens asyncio streams on them
for the JSON-RPC connection.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
import sys
from dataclasses import dataclass
from typing import Any, TextIO

__all__ = ["Claimed", "StrayWriter", "claim"]

log = logging.getLogger(__name__)


class StrayWriter(io.TextIOBase):
    """A ``sys.stdout`` stand-in: every write is dropped and reported as a logged error."""

    def __init__(self, *, encoding: str = "utf-8") -> None:
        super().__init__()
        self._encoding = encoding
        self.dropped = 0
        self._reporting = False

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return self._encoding

    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return False

    def write(self, s: str) -> int:
        text = str(s)
        if not text:
            return 0
        self.dropped += len(text)
        if not self._reporting:  # a logging handler on stdout must not recurse
            self._reporting = True
            try:
                log.error(
                    "dropped %d characters written to stdout: stdout carries ACP frames only",
                    len(text),
                )
            finally:
                self._reporting = False
        return len(text)

    def flush(self) -> None:
        return None


@dataclass
class Claimed:
    """The private protocol descriptors (see the module notes)."""

    stdin_fd: int
    stdout_fd: int
    previous_stdout: TextIO | Any

    async def streams(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Asyncio streams over the private descriptors (POSIX pipes, files or sockets)."""
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader(limit=16 * 1024 * 1024)
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, os.fdopen(self.stdin_fd, "rb", buffering=0))
        transport, writer_protocol = await loop.connect_write_pipe(
            asyncio.streams.FlowControlMixin, os.fdopen(self.stdout_fd, "wb", buffering=0)
        )
        writer = asyncio.StreamWriter(transport, writer_protocol, None, loop)
        return reader, writer


def claim() -> Claimed:
    """Take descriptors 0 and 1 for the protocol and guard ``sys.stdout`` (module notes)."""
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
    previous = sys.stdout
    sys.stdout = StrayWriter()
    return Claimed(rpc_in, rpc_out, previous)
