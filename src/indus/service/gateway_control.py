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
socket or the pointer for their own. Before sending, the client also checks who listens: a socket
the OS reports as served by another user is answered ``foreign_listener`` and gets nothing
(P17-24, ``remote.profile_guard.listener_is_ours``).

Attach verbs (P17-11): ``session_ticket`` asks for a single-use WebSocket ticket and checks the
answer (ticket shape, a credential-free loopback ``ws_url``, the profile and instance the caller
expects); ``describe`` returns the daemon's attach descriptor. A ``TicketReply`` never shows its
ticket in ``repr``.

Windows (docs/notes/P25/windows-gateway.md): there is no Unix socket; the daemon serves the same
wire on a named pipe whose name the pointer file holds (``indus.service.control_pipe``). Windows
has no uids or mode bits: the pointer only has to be a regular file naming a well-formed pipe, a
home only has to be a directory (the user profile's ACL protects it), and the listener check
compares the pipe server's user SID with ours.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import stat
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from indus.service import control_pipe
from indus.service.remote.profile_guard import FOREIGN_LISTENER, listener_is_ours
from indus.service.ws_auth import is_loopback_host, looks_like_ticket

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
    "TicketReply",
    "describe",
    "fits",
    "home_is_safe",
    "identify",
    "pipe_name_for",
    "pointer_path",
    "query",
    "session_ticket",
    "socket_path_for",
    "ws_url_is_safe",
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


def pipe_name_for(home: Path) -> str | None:
    """Windows: the pipe the pointer in ``home`` names, or None (no pointer, not a regular file,
    or not a well-formed pipe name). Whether anything serves it is the exchange's question."""
    pointer = pointer_path(home)
    try:
        if not stat.S_ISREG(pointer.lstat().st_mode):
            return None
        name = pointer.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return name if control_pipe.is_pipe_name(name) else None


def _target_for(home: Path) -> Path | str | None:
    """Where a request for ``home`` goes: the socket path, or the pipe name on Windows."""
    if sys.platform == "win32":
        return pipe_name_for(home)
    return socket_path_for(home)


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
    if sys.platform == "win32":
        return stat.S_ISDIR(info.st_mode)
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
            if not listener_is_ours(sock):
                # Another user's process listens on our profile's socket: say nothing to it.
                return ControlReply(
                    False, error=FOREIGN_LISTENER, detail="another user listens on the socket"
                )
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
    return _answer(b"".join(chunks))


def _pipe_exchange(name: str, request: bytes, timeout: float) -> ControlReply:
    """``_exchange`` over the Windows named pipe (same codes)."""
    try:
        raw = control_pipe.exchange(name, request, timeout, limit=MAX_RESPONSE_BYTES)
    except control_pipe.PipeMissing:
        return ControlReply(False, error="no_socket")
    except control_pipe.ForeignServer:
        return ControlReply(
            False, error=FOREIGN_LISTENER, detail="another user serves the control pipe"
        )
    except control_pipe.ResponseTooLong:
        return ControlReply(False, error="bad_response", detail="the answer is too long")
    except TimeoutError:
        return ControlReply(False, error="timeout")
    except OSError as exc:
        return ControlReply(False, error="unreachable", detail=type(exc).__name__)
    return _answer(raw)


def _answer(raw: bytes) -> ControlReply:
    line = raw.partition(b"\n")[0]
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
    target = _target_for(home)
    if target is None:
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
    if isinstance(target, str):
        return _pipe_exchange(target, request, timeout)
    return _exchange(target, request, timeout)


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


# -- attach verbs (P17-11) ------------------------------------------------------------------------


@dataclass(frozen=True)
class TicketReply:
    """A ``session-ticket`` answer. ``ticket`` is the secret (hidden from ``repr``); ``error`` is a
    short code when ``ok`` is false: the ``ControlReply`` codes, plus ``bad_response``,
    ``profile_mismatch`` and ``stale_instance`` for an answer the caller cannot trust."""

    ok: bool
    ticket: str = field(default="", repr=False)
    ws_url: str = ""
    profile_id: str = ""
    instance_id: str = ""
    expires_in: float = 0.0
    error: str | None = None
    detail: str = ""


def ws_url_is_safe(url: object) -> bool:
    """A ``ws://`` (or ``wss://``) URL on a literal loopback address with a port, no user info,
    no query and no fragment: nothing secret can ride in it."""
    if not isinstance(url, str) or not url:
        return False
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    return (
        parts.scheme in ("ws", "wss")
        and is_loopback_host(parts.hostname)
        and port is not None
        and not parts.username
        and not parts.password
        and not parts.query
        and not parts.fragment
        and "@" not in parts.netloc
    )


def session_ticket(
    home: Path,
    purpose: str,
    *,
    profile_id: str | None = None,
    instance_id: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> TicketReply:
    """Ask the daemon serving ``home`` for a ticket. ``profile_id`` / ``instance_id`` are the
    ones the caller expects (they are sent along, and the answer must carry the same). Never
    raises."""
    params: dict[str, Any] = {"purpose": purpose}
    if profile_id is not None:
        params["profile_id"] = profile_id
    if instance_id is not None:
        params["instance_id"] = instance_id
    reply = query(home, "session-ticket", params, timeout=timeout)
    if not reply.ok or reply.result is None:
        return TicketReply(False, error=reply.error or "failed", detail=reply.detail)
    result = reply.result
    ticket = result.get("ticket")
    ws_url = result.get("ws_url")
    got_profile = result.get("profile_id")
    got_instance = result.get("instance_id")
    expires_in = result.get("expires_in", 0)
    if not looks_like_ticket(ticket):
        return TicketReply(False, error="bad_response", detail="the answer holds no ticket")
    if not ws_url_is_safe(ws_url):
        return TicketReply(False, error="bad_response", detail="the ws_url is not a loopback URL")
    if not isinstance(got_profile, str) or not isinstance(got_instance, str):
        return TicketReply(False, error="bad_response", detail="the answer names no profile")
    if profile_id is not None and got_profile != profile_id:
        return TicketReply(False, error="profile_mismatch", detail="another profile answered")
    if instance_id is not None and got_instance != instance_id:
        return TicketReply(False, error="stale_instance", detail="the daemon restarted")
    if isinstance(expires_in, bool) or not isinstance(expires_in, int | float):
        expires_in = 0
    return TicketReply(
        True,
        ticket=cast(str, ticket),
        ws_url=cast(str, ws_url),
        profile_id=got_profile,
        instance_id=got_instance,
        expires_in=float(expires_in),
    )


def describe(home: Path, *, timeout: float = DEFAULT_TIMEOUT_S) -> dict[str, Any] | None:
    """The daemon's attach descriptor (``profile_id``, ``instance_id``, ``code_version``,
    ``ws_url``, ``creatable_sources``, ``capabilities``), or None when no daemon answers it
    well-formed."""
    reply = query(home, "describe", timeout=timeout)
    if not reply.ok or reply.result is None:
        return None
    result = reply.result
    if not isinstance(result.get("profile_id"), str) or not isinstance(
        result.get("instance_id"), str
    ):
        return None
    return result
