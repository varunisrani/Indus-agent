"""The control channel's Windows transport: a named pipe (docs/notes/P25/windows-gateway.md).

Python on Windows has no Unix sockets, so there the daemon serves its control wire (one JSON line
each way, ``indus.service.gateway_control``) on ``\\\\.\\pipe\\indus-gw-<32 hex>``: a random
name per start, published through the same pointer file the long-home fallback uses
(``<home>/gateway.sock.path``). A pointer that does not hold exactly such a name is ignored, so a
client never dials something else written into the home.

``exchange`` is the blocking client half: open the pipe (waiting while every instance is busy),
check who serves it, write the request, read up to the first newline, all within one deadline.
It raises ``PipeMissing`` (no such pipe: no daemon), ``ForeignServer`` (another user's process
serves the name), ``TimeoutError``, ``ResponseTooLong`` or ``OSError``; the caller maps them onto
its error codes. The server half lives in ``indus.gateway.control_socket``.
"""

from __future__ import annotations

import contextlib
import re
import secrets
import sys
import time
from typing import Final

__all__ = [
    "PIPE_PREFIX",
    "ForeignServer",
    "PipeMissing",
    "ResponseTooLong",
    "exchange",
    "is_pipe_name",
    "new_pipe_name",
    "pipe_exists",
]

PIPE_PREFIX: Final = "\\\\.\\pipe\\indus-gw-"
_NAME = re.compile(r"\\\\\.\\pipe\\indus-gw-[0-9a-f]{32}")
_CHUNK: Final = 65536
_ERROR_FILE_NOT_FOUND: Final = 2
_ERROR_PATH_NOT_FOUND: Final = 3
_ERROR_SEM_TIMEOUT: Final = 121


class PipeMissing(OSError):
    """No pipe of that name exists (nobody serves it)."""


class ForeignServer(OSError):
    """The process serving the pipe runs as another user."""


class ResponseTooLong(OSError):
    """The answer passed the caller's limit before its newline."""


def new_pipe_name() -> str:
    return PIPE_PREFIX + secrets.token_hex(16)


def is_pipe_name(text: str) -> bool:
    return _NAME.fullmatch(text) is not None


def _left_ms(deadline: float) -> int:
    return max(0, int((deadline - time.monotonic()) * 1000))


if sys.platform == "win32":  # the Win32 calls; pyright and the tests on other OSes skip this
    import _winapi

    def _missing(exc: OSError) -> bool:
        return getattr(exc, "winerror", None) in (_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND)

    def _open(name: str, deadline: float) -> int:
        while True:
            try:
                return _winapi.CreateFile(
                    name,
                    _winapi.GENERIC_READ | _winapi.GENERIC_WRITE,
                    0,
                    _winapi.NULL,
                    _winapi.OPEN_EXISTING,
                    _winapi.FILE_FLAG_OVERLAPPED,
                    _winapi.NULL,
                )
            except OSError as exc:
                if _missing(exc):
                    raise PipeMissing("no such pipe") from None
                if getattr(exc, "winerror", None) != _winapi.ERROR_PIPE_BUSY:
                    raise
            left = _left_ms(deadline)
            if left <= 0:
                raise TimeoutError("every pipe instance is busy")
            try:
                _winapi.WaitNamedPipe(name, left)
            except OSError as exc:
                if _missing(exc):
                    raise PipeMissing("no such pipe") from None
                if getattr(exc, "winerror", None) == _ERROR_SEM_TIMEOUT:
                    raise TimeoutError("every pipe instance is busy") from None
                raise

    def _settle(overlapped: _winapi.Overlapped, err: int, deadline: float) -> None:
        """Wait for a started overlapped read or write; cancel it at the deadline."""
        if err != _winapi.ERROR_IO_PENDING:
            return
        waited = _winapi.WaitForMultipleObjects([overlapped.event], False, _left_ms(deadline))
        if waited == _winapi.WAIT_OBJECT_0:
            return
        overlapped.cancel()
        with contextlib.suppress(OSError):
            overlapped.GetOverlappedResult(True)  # let the cancelled call finish
        raise TimeoutError("the pipe did not answer in time")

    def _write(handle: int, data: bytes, deadline: float) -> None:
        overlapped, err = _winapi.WriteFile(handle, data, overlapped=True)
        _settle(overlapped, err, deadline)
        written, err = overlapped.GetOverlappedResult(True)
        if err or written != len(data):
            raise OSError("the request was not written whole")

    def _read_line(handle: int, deadline: float, limit: int) -> bytes:
        got = bytearray()
        while b"\n" not in got:
            try:
                overlapped, err = _winapi.ReadFile(handle, _CHUNK, overlapped=True)
                _settle(overlapped, err, deadline)
                overlapped.GetOverlappedResult(True)
            except BrokenPipeError:
                break  # the server closed its end: what we have is the answer
            chunk = overlapped.getbuffer()
            if not chunk:
                break
            got += chunk
            if len(got.partition(b"\n")[0]) > limit:
                raise ResponseTooLong("the answer is too long")
        return bytes(got)

    def _server_is_ours(handle: int) -> bool:
        from indus.platform import winproc

        pid = winproc.pipe_server_pid(handle)
        return pid is None or winproc.same_user(pid) is not False


def exchange(name: str, request: bytes, timeout: float, *, limit: int) -> bytes:
    """Send ``request`` on the pipe ``name`` and return what came back up to and including the
    first newline (or everything before the server closed). See the module notes for errors."""
    if sys.platform != "win32":
        raise OSError("named pipes are the Windows transport")
    deadline = time.monotonic() + timeout
    handle = _open(name, deadline)
    try:
        if not _server_is_ours(handle):
            raise ForeignServer("another user serves the pipe")
        _write(handle, request, deadline)
        return _read_line(handle, deadline, limit)
    finally:
        _winapi.CloseHandle(handle)


def pipe_exists(name: str) -> bool:
    """Does a server listen on ``name``? (Busy or not ours still counts.)"""
    if sys.platform != "win32":
        return False
    try:
        _winapi.WaitNamedPipe(name, 1)
    except OSError as exc:
        return not _missing(exc)
    return True
