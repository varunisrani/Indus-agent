"""The gateway control socket's wire, paths and client (P15-27).

A running gateway answers local questions on a Unix socket in its profile home; this module is
the half every other process needs (the CLI's ``indus gateway status`` / ``stop``, the cron tick
guard, later the updater), so it lives below the surfaces (D-16.246). The server is
``indus.gateway.control_socket``.

Paths: ``<home>/gateway.sock``. A Unix socket path is short (about 104 bytes on macOS, 108 on
Linux), so when that path is longer than ``MAX_SOCKET_PATH`` bytes the server binds inside a fresh
private directory and writes the socket's path into ``<home>/gateway.sock.path``. A client
follows the pointer only when the pointer is a regular file we own with no group or other bits,
and the socket's directory is ours with mode 0700.

Wire: one exchange per connection. The client sends one JSON line
``{"protocol": 1, "verb": ..., "id": ..., "params": {...}}`` (``params`` optional, nothing else),
the server answers one line ``{"ok", "protocol", "id", "result" | "error", "verbs"?}`` and closes.
Requests are at most 64 KiB, answers at most 512 KiB.

Safety: a client refuses a home other people can write to — world-writable always, group-writable
unless the group is the user's own private group — because whoever can write there can swap the
socket or the pointer for their own.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import stat
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

__all__ = [
    "DEFAULT_TIMEOUT_S",
    "MAX_REQUEST_BYTES",
    "MAX_RESPONSE_BYTES",
    "MAX_SOCKET_PATH",
    "POINTER_FILE",
    "PROTOCOL",
    "REQUEST_KEYS",
    "SOCKET_FILE",
    "ControlReply",
    "fits",
    "home_is_safe",
    "identify",
    "pointer_path",
    "query",
    "socket_path_for",
]

PROTOCOL = 1
SOCKET_FILE = "gateway.sock"
POINTER_FILE = "gateway.sock.path"
MAX_SOCKET_PATH = 100
"""Bytes; below the smallest ``sun_path`` (104 on macOS) with room for the terminating NUL."""
MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 512 * 1024
DEFAULT_TIMEOUT_S = 2.0
REQUEST_KEYS = frozenset({"protocol", "verb", "id", "params"})


@dataclass(frozen=True)
class ControlReply:
    """What a control request came back with. ``error`` is a short code when ``ok`` is false:
    ``no_socket``, ``unsafe_home``, ``unreachable``, ``timeout``, ``bad_response``, or the
    server's own (``unknown_verb``, ``bad_request``, ``unsupported``, ``failed``, ...)."""

    ok: bool
    result: dict[str, Any] | None = None
    error: str | None = None
    detail: str = ""
    verbs: tuple[str, ...] = field(default=())

    @property
    def answered(self) -> bool:
        """A gateway answered (even with an error): it is alive."""
        return self.ok or self.error not in _LOCAL_ERRORS


_LOCAL_ERRORS = frozenset({"no_socket", "unsafe_home", "unreachable", "timeout", "bad_response"})


# -- paths ----------------------------------------------------------------------------------------


def pointer_path(home: Path) -> Path:
    return home / POINTER_FILE


def fits(path: Path) -> bool:
    return len(os.fsencode(str(path))) <= MAX_SOCKET_PATH


def _owned_private(info: os.stat_result, *, kind: int) -> bool:
    return (
        stat.S_IFMT(info.st_mode) == kind
        and info.st_uid == os.getuid()
        and not info.st_mode & 0o077
    )


def socket_path_for(home: Path) -> Path | None:
    """Where a client connects for ``home``: the direct socket, else a trusted pointer's target,
    else None (no gateway has bound here)."""
    direct = home / SOCKET_FILE
    with contextlib.suppress(OSError):
        if stat.S_ISSOCK(direct.lstat().st_mode):
            return direct
    pointer = pointer_path(home)
    try:
        if not _owned_private(pointer.lstat(), kind=stat.S_IFREG):
            return None
        target = Path(pointer.read_text(encoding="utf-8").strip())
        if not target.is_absolute() or not _owned_private(target.parent.lstat(), kind=stat.S_IFDIR):
            return None
        if not stat.S_ISSOCK(target.lstat().st_mode):
            return None
    except (OSError, ValueError):
        return None
    return target


def _private_group(gid: int) -> bool:
    """The group is the user's own: their primary group, named like them, with nobody else."""
    import grp  # POSIX only, like the rest of this module's callers
    import pwd

    try:
        me = pwd.getpwuid(os.getuid())
        group = grp.getgrgid(gid)
    except KeyError:
        return False
    others = [m for m in group.gr_mem if m != me.pw_name]
    return gid == me.pw_gid and group.gr_name == me.pw_name and not others


def home_is_safe(home: Path) -> bool:
    """We own ``home`` and nobody else can write to it (see the module notes)."""
    try:
        info = home.stat()
    except OSError:
        return False
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        return False
    if info.st_mode & stat.S_IWOTH:
        return False
    return not (info.st_mode & stat.S_IWGRP) or _private_group(info.st_gid)


# -- client ---------------------------------------------------------------------------------------


def _decode(raw: bytes) -> ControlReply:
    try:
        loaded: object = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return ControlReply(False, error="bad_response", detail="the answer is not JSON")
    if not isinstance(loaded, dict):
        return ControlReply(False, error="bad_response", detail="the answer is not an object")
    answer = cast(dict[str, Any], loaded)
    verbs_raw: object = answer.get("verbs") or []
    verbs = (
        tuple(str(v) for v in cast(list[object], verbs_raw)) if isinstance(verbs_raw, list) else ()
    )
    if answer.get("ok") is True:
        result: object = answer.get("result")
        if not isinstance(result, dict):
            return ControlReply(False, error="bad_response", detail="the result is not an object")
        return ControlReply(True, result=cast(dict[str, Any], result), verbs=verbs)
    error = answer.get("error")
    detail = answer.get("detail")
    return ControlReply(
        False,
        error=error if isinstance(error, str) and error else "failed",
        detail=detail if isinstance(detail, str) else "",
        verbs=verbs,
    )


def _exchange(path: Path, request: bytes, timeout: float) -> ControlReply:
    deadline = time.monotonic() + timeout
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        try:
            sock.connect(str(path))
            sock.sendall(request)
            chunks: list[bytes] = []
            size = 0
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    return ControlReply(False, error="timeout")
                sock.settimeout(left)
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if b"\n" in chunk:
                    break
                if size > MAX_RESPONSE_BYTES:
                    return ControlReply(
                        False, error="bad_response", detail="the answer is too long"
                    )
        except TimeoutError:
            return ControlReply(False, error="timeout")
        except OSError as exc:
            return ControlReply(False, error="unreachable", detail=type(exc).__name__)
    line = b"".join(chunks).partition(b"\n")[0]
    if not line:
        return ControlReply(
            False, error="bad_response", detail="the gateway closed without answering"
        )
    if len(line) > MAX_RESPONSE_BYTES:
        return ControlReply(False, error="bad_response", detail="the answer is too long")
    return _decode(line)


def query(
    home: Path,
    verb: str,
    params: Mapping[str, Any] | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
    request_id: int | str = 1,
) -> ControlReply:
    """Ask the gateway serving ``home``. Never raises: a missing, unsafe or silent socket is an
    error code the caller falls back on (status file, unit state)."""
    if not home_is_safe(home):
        return ControlReply(False, error="unsafe_home")
    path = socket_path_for(home)
    if path is None:
        return ControlReply(False, error="no_socket")
    message: dict[str, Any] = {"protocol": PROTOCOL, "verb": verb, "id": request_id}
    if params is not None:
        message["params"] = dict(params)
    try:
        request = json.dumps(message).encode("utf-8") + b"\n"
    except (TypeError, ValueError):
        return ControlReply(False, error="bad_request", detail="params are not JSON")
    if len(request) > MAX_REQUEST_BYTES:
        return ControlReply(False, error="request_too_large")
    return _exchange(path, request, timeout)


def identify(home: Path, *, timeout: float = DEFAULT_TIMEOUT_S) -> dict[str, Any] | None:
    """The live gateway's identity (pid, start time, home, profile, ...), or None when no
    gateway answers well-formed. A well-formed answer IS liveness (R06.18)."""
    reply = query(home, "identify", timeout=timeout)
    if not reply.ok or reply.result is None:
        return None
    pid = reply.result.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid < 1:
        return None
    return reply.result
