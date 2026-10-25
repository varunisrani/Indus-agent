"""Win32 process facts for native Windows (docs/notes/P25/windows-gateway.md).

POSIX code answers these with ``os.kill(pid, 0)``, ``/proc``, ``ps`` and ``SO_PEERCRED``. On
Windows ``os.kill`` with any signal but the two console events *terminates* the process, so no
caller may fall back to it there. Every reader answers None (unknown) instead of raising:

- ``pid_alive``: the process exists and has not exited. A process we may not open (another user's,
  a protected one) exists, so it counts as alive.
- ``start_time``: the creation time in 100 ns ticks since 1601, a fingerprint against pid reuse.
- ``process_user_sid`` / ``own_user_sid``: the binary SID of a process's token user; two
  processes run as the same user when their SIDs are equal (``same_user``).
- ``pipe_client_pid`` / ``pipe_server_pid``: the pid at the other end of a named pipe handle.
- ``spawn_detached``: start a program that outlives its caller (its own process group, no console
  window, outside the caller's job object when the job allows that); its pid.

On other platforms every reader answers None / False and ``spawn_detached`` raises: callers branch
on ``sys.platform`` first.
"""

from __future__ import annotations

import functools
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from typing import IO

__all__ = [
    "CREATE_BREAKAWAY_FROM_JOB",
    "CREATE_NEW_PROCESS_GROUP",
    "CREATE_NO_WINDOW",
    "DETACHED_FLAGS",
    "own_user_sid",
    "pid_alive",
    "pipe_client_pid",
    "pipe_server_pid",
    "process_user_sid",
    "same_user",
    "spawn_detached",
    "start_time",
]

CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
DETACHED_FLAGS = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
"""A detached child: Ctrl+C in the caller's console does not reach it, and a console program gets
a hidden console of its own (its own children inherit that one instead of opening windows)."""

_STILL_ACTIVE = 259
_QUERY_LIMITED_INFORMATION = 0x1000
_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_ERROR_ACCESS_DENIED = 5

_detached: list[subprocess.Popen[bytes]] = []
"""Children ``spawn_detached`` started, kept so Python neither closes their handle early nor warns
about a running child; finished ones are dropped on the next spawn."""


if sys.platform == "win32":  # the Win32 calls; pyright and the tests on other OSes skip this
    import ctypes
    from ctypes import wintypes

    class _FileTime(ctypes.Structure):
        _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]  # noqa: RUF012 - layout

    class _Api:
        def __init__(self) -> None:
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            adv = ctypes.WinDLL("advapi32", use_last_error=True)
            self.open_process = k32.OpenProcess
            self.open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            self.open_process.restype = wintypes.HANDLE
            self.close = k32.CloseHandle
            self.close.argtypes = [wintypes.HANDLE]
            self.close.restype = wintypes.BOOL
            self.exit_code = k32.GetExitCodeProcess
            self.exit_code.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            self.exit_code.restype = wintypes.BOOL
            self.times = k32.GetProcessTimes
            self.times.argtypes = [wintypes.HANDLE, *[ctypes.POINTER(_FileTime)] * 4]
            self.times.restype = wintypes.BOOL
            self.client_pid = k32.GetNamedPipeClientProcessId
            self.client_pid.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)]
            self.client_pid.restype = wintypes.BOOL
            self.server_pid = k32.GetNamedPipeServerProcessId
            self.server_pid.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)]
            self.server_pid.restype = wintypes.BOOL
            self.open_token = adv.OpenProcessToken
            self.open_token.argtypes = [
                wintypes.HANDLE,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.HANDLE),
            ]
            self.open_token.restype = wintypes.BOOL
            self.token_info = adv.GetTokenInformation
            self.token_info.argtypes = [
                wintypes.HANDLE,
                ctypes.c_int,
                ctypes.c_void_p,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD),
            ]
            self.token_info.restype = wintypes.BOOL
            self.sid_valid = adv.IsValidSid
            self.sid_valid.argtypes = [ctypes.c_void_p]
            self.sid_valid.restype = wintypes.BOOL
            self.sid_length = adv.GetLengthSid
            self.sid_length.argtypes = [ctypes.c_void_p]
            self.sid_length.restype = wintypes.DWORD

    @functools.cache
    def _api() -> _Api:
        return _Api()

    def _open(pid: int) -> int | None:
        if pid < 1 or pid > 0xFFFFFFFF:
            return None
        return _api().open_process(_QUERY_LIMITED_INFORMATION, False, pid) or None

    def _alive(pid: int) -> bool:
        handle = _open(pid)
        if handle is None:
            return pid > 0 and ctypes.get_last_error() == _ERROR_ACCESS_DENIED
        try:
            code = wintypes.DWORD()
            if not _api().exit_code(handle, ctypes.byref(code)):
                return True
            return code.value == _STILL_ACTIVE
        finally:
            _api().close(handle)

    def _created(pid: int) -> int | None:
        handle = _open(pid)
        if handle is None:
            return None
        try:
            created, exited, kernel, user = _FileTime(), _FileTime(), _FileTime(), _FileTime()
            ok = _api().times(
                handle,
                ctypes.byref(created),
                ctypes.byref(exited),
                ctypes.byref(kernel),
                ctypes.byref(user),
            )
            return (created.high << 32) | created.low if ok else None
        finally:
            _api().close(handle)

    def _token_sid(process: int) -> bytes | None:
        api = _api()
        token = wintypes.HANDLE()
        if not api.open_token(process, _TOKEN_QUERY, ctypes.byref(token)):
            return None
        try:
            size = wintypes.DWORD(0)
            api.token_info(token, _TOKEN_USER, None, 0, ctypes.byref(size))
            if size.value == 0:
                return None
            buffer = ctypes.create_string_buffer(size.value)
            if not api.token_info(token, _TOKEN_USER, buffer, size, ctypes.byref(size)):
                return None
            sid = ctypes.c_void_p.from_buffer(buffer).value  # TOKEN_USER starts with the PSID
            if not sid or not api.sid_valid(sid):
                return None
            return ctypes.string_at(sid, api.sid_length(sid))
        finally:
            api.close(token)

    def _sid_of(pid: int) -> bytes | None:
        handle = _open(pid)
        if handle is None:
            return None
        try:
            return _token_sid(handle)
        finally:
            _api().close(handle)

    def _pipe_pid(handle: int, *, server: bool) -> int | None:
        pid = wintypes.ULONG(0)
        reader = _api().server_pid if server else _api().client_pid
        if not reader(handle, ctypes.byref(pid)) or pid.value == 0:
            return None
        return int(pid.value)


def pid_alive(pid: int) -> bool:
    """True when a process with ``pid`` exists and has not exited (owned by anyone)."""
    if sys.platform != "win32" or pid < 1:
        return False
    return _alive(pid)


def start_time(pid: int) -> int | None:
    """The process's creation time (100 ns ticks since 1601), or None when unknown."""
    if sys.platform != "win32" or pid < 1:
        return None
    return _created(pid)


def process_user_sid(pid: int) -> bytes | None:
    """The binary SID of the user ``pid`` runs as, or None when it cannot be read."""
    if sys.platform != "win32" or pid < 1:
        return None
    return _sid_of(pid)


@functools.cache
def own_user_sid() -> bytes | None:
    """The SID of the user this process runs as (None off Windows)."""
    return process_user_sid(os.getpid())


def same_user(pid: int) -> bool | None:
    """Does ``pid`` run as our user? None when either side cannot be read."""
    theirs = process_user_sid(pid)
    ours = own_user_sid()
    if theirs is None or ours is None:
        return None
    return theirs == ours


def pipe_client_pid(handle: int) -> int | None:
    """The pid of the client connected to the server end ``handle`` of a named pipe."""
    if sys.platform != "win32":
        return None
    return _pipe_pid(handle, server=False)


def pipe_server_pid(handle: int) -> int | None:
    """The pid of the process serving the named pipe whose client end is ``handle``."""
    if sys.platform != "win32":
        return None
    return _pipe_pid(handle, server=True)


def spawn_detached(argv: Sequence[str], *, cwd: str, env: Mapping[str, str], out: IO[bytes]) -> int:
    """Start ``argv`` so it outlives its caller (see the module notes), stdin from the null device
    and stdout/stderr into ``out``; its pid. Raises ``OSError`` when it cannot start."""
    if sys.platform != "win32":
        raise OSError("spawn_detached is the Windows start; POSIX double-forks")
    _detached[:] = [child for child in _detached if child.poll() is None]

    def start(flags: int) -> subprocess.Popen[bytes]:
        return subprocess.Popen(  # noqa: S603 - our own program, argv built by the caller
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=out,
            cwd=cwd,
            env=dict(env),
            creationflags=flags,
            close_fds=True,
        )

    try:
        child = start(DETACHED_FLAGS | CREATE_BREAKAWAY_FROM_JOB)
    except PermissionError:  # the caller's job does not allow breakaway: stay in it
        child = start(DETACHED_FLAGS)
    _detached.append(child)
    return child.pid
