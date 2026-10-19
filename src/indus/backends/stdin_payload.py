# pyright: strict
"""A command's stdin for sandboxes without a stdin pipe, and streamed for those with one (P21-03).

Daytona and Vercel run commands without a stdin pipe. The payload is uploaded through the
backend's file API as a uniquely named file in the sandbox temp dir (never on a command line:
argv is capped near 128 KiB and readable by other users), its size is checked against what was
sent (R02.16), and the script starts with ``exec 0<file || exit 1; rm -f file`` so the shell owns
the data and the file is gone before the user's command runs. Modal has a stdin stream: the
payload goes in 64 KiB chunks, each drained, then EOF.

Text becomes bytes as on the local backend (UTF-8 with ``surrogateescape``, so bytes that came in
undecodable go back out unchanged). A payload that cannot be encoded never raises: the command
runs with empty stdin and the failure lands in ``ExecResult.stdin_error``.
"""

from __future__ import annotations

import contextlib
import logging
import secrets
import shlex
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from typing import Protocol

__all__ = [
    "STAGE_PREFIX",
    "STREAM_CHUNK",
    "StagedStdin",
    "StdinFailure",
    "StdinWriter",
    "encode_stdin",
    "stage_stdin",
    "staged_stdin_path",
    "stdin_chunks",
    "stream_stdin",
    "with_stdin",
]

log = logging.getLogger(__name__)

STREAM_CHUNK = 64 * 1024
STAGE_PREFIX = ".indus-stdin-"


@dataclass(frozen=True, slots=True)
class StdinFailure:
    """Why a payload could not be delivered (goes into ``ExecResult.stdin_error``)."""

    message: str


def encode_stdin(data: str) -> bytes | StdinFailure:
    """``data`` as bytes, or why it cannot be sent."""
    try:
        return data.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError as exc:
        return StdinFailure(f"stdin is not valid text ({exc.reason} at position {exc.start})")


def staged_stdin_path(temp_dir: str) -> str:
    """A fresh file name in the sandbox temp dir."""
    base = temp_dir.rstrip("/") or "/"
    sep = "" if base.endswith("/") else "/"
    return f"{base}{sep}{STAGE_PREFIX}{secrets.token_hex(16)}"


@dataclass(frozen=True, slots=True)
class StagedStdin:
    """The outcome of staging: a file to read stdin from, nothing, or an error."""

    path: str | None = None
    size: int = 0
    error: str | None = None

    def prefix(self) -> str:
        """The script lines that take stdin from the file and delete it ("" when none)."""
        if self.path is None:
            return ""
        quoted = shlex.quote(self.path)
        # bash keeps going after a failed ``exec`` redirection: stop explicitly, so the command
        # never runs with the sandbox's own stdin by mistake.
        return f"exec 0<{quoted} || exit 1; rm -f {quoted}\n"


def with_stdin(script: str, staged: StagedStdin) -> str:
    """``script`` reading its stdin from the staged file (unchanged when nothing was staged)."""
    return staged.prefix() + script


async def stage_stdin(
    data: str | None,
    *,
    temp_dir: str,
    write: Callable[[str, bytes], Awaitable[object]],
    size_of: Callable[[str], Awaitable[int]],
    remove: Callable[[str], Awaitable[object]] | None = None,
) -> StagedStdin:
    """Upload ``data`` for a pipe-less sandbox (see the module notes).

    ``write(path, raw)`` uploads through the backend's file API; ``size_of(path)`` reads the size
    the sandbox sees; ``remove(path)`` deletes it (best effort). ``None`` or empty ``data`` stages
    nothing. An encode failure or a size mismatch comes back as ``error``; failures of ``write`` or
    ``size_of`` themselves (the backend is unreachable) propagate.
    """
    if not data:
        return StagedStdin()
    raw = encode_stdin(data)
    if isinstance(raw, StdinFailure):
        return StagedStdin(error=raw.message)
    path = staged_stdin_path(temp_dir)
    await write(path, raw)
    seen = await size_of(path)
    if seen != len(raw):
        log.warning("staged stdin %s arrived with %d of %d bytes", path, seen, len(raw))
        if remove is not None:
            with contextlib.suppress(Exception):
                await remove(path)
        return StagedStdin(
            error=f"stdin upload was incomplete ({seen} of {len(raw)} bytes arrived)"
        )
    return StagedStdin(path=path, size=len(raw))


class StdinWriter(Protocol):
    """A vendor's blocking stdin stream (Modal's ``process.stdin``)."""

    def write(self, data: bytes, /) -> object: ...

    def drain(self) -> object: ...

    def write_eof(self) -> object: ...


def stdin_chunks(raw: bytes, size: int = STREAM_CHUNK) -> Iterator[bytes]:
    """``raw`` in ``size`` pieces (no empty trailing piece)."""
    if size <= 0:
        raise ValueError("chunk size must be positive")
    for offset in range(0, len(raw), size):
        yield raw[offset : offset + size]


def stream_stdin(writer: StdinWriter, raw: bytes, *, chunk: int = STREAM_CHUNK) -> None:
    """Write ``raw`` in chunks, draining after each, then close the stream (blocking; run it on
    the SDK call wrapper's worker)."""
    for piece in stdin_chunks(raw, chunk):
        writer.write(piece)
        writer.drain()
    writer.write_eof()
    writer.drain()
