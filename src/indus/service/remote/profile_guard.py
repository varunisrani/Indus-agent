"""Profile binding on attach (P17-24): a client talks only to its own profile's daemon.

A client knows its profile home, so it knows the profile id it expects: ``expected_profile_id``
(the same stable hash the daemon's ticket book uses, ``ws_auth.stable_profile_id``). Every answer
that names a profile is compared with that id, never with an id another answer reported:

- the control socket's ``describe`` (``check_described``), before any ticket is asked for, so a
  socket in our home that leads to another profile's daemon mints nothing for us;
- the WebSocket's first frame ``gateway.ready`` (``check_ready``), before the client sends any
  request; a client that walks away closes with ``CLOSE_PROFILE_MISMATCH`` (4005, the protocol
  table's ``PROFILE_MISMATCH``).

A missing id counts as a mismatch. ``ProfileMismatch`` carries the short code
``profile_mismatch`` and says where the mismatch was seen; it never names a path.

Before writing a byte to the control socket the client checks who listens on it
(``listener_is_ours``): a listener the OS reports as another user is refused
(``foreign_listener``); an OS that reports nothing is allowed (the home itself is owner-only).

On the daemon side every RPC runs under the daemon's one ``Scope`` — home, env, secrets and
terminal policy travel together in it (R05.14, R03.23). ``bind_daemon_scope`` is the check an
endpoint makes before it serves anyone: the scope's home must be the profile its ticket book
serves, its secret store (when it says where it lives) must belong to that home, and a routed
(multiplexed) scope is refused — one daemon serves one profile until P24.

Launch options of one run (``INDUS_YOLO_MODE``, ``INDUS_MODEL``, every ``INDUS_TUI_*`` …) belong
to a session, not to the daemon: ``launch_free_env`` is the one scrub the grant helper and
``ensure`` apply before anything that may start a daemon (``creation_policy.scrub_launch_env``).
"""

from __future__ import annotations

import os
import socket
import struct
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from indus.foundation.scope import Scope
from indus.protocol.errors import PROFILE_MISMATCH
from indus.service.creation_policy import scrub_launch_env
from indus.service.ws_auth import stable_profile_id

__all__ = [
    "CLOSE_PROFILE_MISMATCH",
    "FOREIGN_LISTENER",
    "MISMATCH_CODE",
    "ProfileMismatch",
    "ScopeNotBound",
    "bind_daemon_scope",
    "check_described",
    "check_profile",
    "check_ready",
    "expected_profile_id",
    "launch_free_env",
    "listener_is_ours",
    "socket_peer_uid",
]

MISMATCH_CODE: Final = "profile_mismatch"
"""The short code every surface reports for a daemon of another profile."""
CLOSE_PROFILE_MISMATCH: Final = PROFILE_MISMATCH.code
"""The WebSocket close code a client leaves with after a mismatching ``gateway.ready`` (4005)."""
FOREIGN_LISTENER: Final = "foreign_listener"
"""The control client's refusal when another user listens on the profile's socket."""

_LOCAL_PEERCRED: Final = 0x001  # macOS <sys/un.h>, at level SOL_LOCAL (0)
_XUCRED_SIZE: Final = 76


class ProfileMismatch(Exception):
    """The daemon that answered serves another profile (or named none)."""

    code: Final = MISMATCH_CODE

    def __init__(self, where: str) -> None:
        super().__init__(f"the daemon's {where} names another profile")
        self.where = where


class ScopeNotBound(Exception):
    """A daemon scope whose parts do not belong to one profile; nothing is served over it."""


def expected_profile_id(home: Path) -> str:
    """The profile id a client of ``home`` expects every daemon answer to carry."""
    return stable_profile_id(home)


def check_profile(expected: str, got: object, *, where: str) -> str:
    """``got`` when it is ``expected``; ``ProfileMismatch`` otherwise (a missing id included)."""
    if not isinstance(got, str) or not got or got != expected:
        raise ProfileMismatch(where)
    return got


def check_described(home: Path, described: Mapping[str, Any]) -> Mapping[str, Any]:
    """A ``describe`` answer from the socket in ``home``, once it names ``home``'s profile."""
    check_profile(expected_profile_id(home), described.get("profile_id"), where="describe")
    return described


def check_ready(expected: str | None, ready: Mapping[str, Any]) -> None:
    """Refuse a ``gateway.ready`` that names another profile than ``expected`` (None: the caller
    has nothing to compare with, e.g. an explicit URL without a grant)."""
    if expected is not None:
        check_profile(expected, ready.get("profile_id"), where="gateway.ready")


# -- who listens on the control socket ------------------------------------------------------------


def socket_peer_uid(sock: socket.socket) -> int | None:
    """The uid of the process at the other end of a connected Unix socket, or None when this OS
    does not say (``SO_PEERCRED`` on Linux, ``LOCAL_PEERCRED`` on macOS)."""
    try:
        if sys.platform.startswith("linux"):
            raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            _pid, uid, _gid = struct.unpack("3i", raw)
            return uid
        if sys.platform == "darwin":
            raw = sock.getsockopt(0, _LOCAL_PEERCRED, _XUCRED_SIZE)
            _version, uid = struct.unpack_from("=II", raw)
            return uid
    except (OSError, struct.error):
        return None
    return None


def _own_uid() -> int | None:
    if sys.platform == "win32":
        return None
    return os.getuid()


def listener_is_ours(sock: socket.socket) -> bool:
    """False only when the OS reports that another user listens at the other end."""
    theirs = socket_peer_uid(sock)
    ours = _own_uid()
    return theirs is None or ours is None or theirs == ours


# -- the daemon's side ----------------------------------------------------------------------------


def _inside(path: Path, home: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(home.resolve(strict=False))
    except ValueError:
        return False
    return True


def bind_daemon_scope(scope: Scope, profile_id: str) -> Scope:
    """``scope`` when it can serve the daemon of ``profile_id`` (see the module notes); raises
    ``ProfileMismatch`` for another home and ``ScopeNotBound`` for parts of another profile."""
    check_profile(expected_profile_id(scope.home), profile_id, where="ticket book")
    if scope.routed:
        raise ScopeNotBound("a routed scope cannot serve a daemon (one profile per daemon)")
    where = getattr(scope.secrets, "index_path", None)
    if isinstance(where, Path) and not _inside(where, scope.home):
        raise ScopeNotBound("the secret store belongs to another profile home")
    return scope


def launch_free_env(env: Mapping[str, str]) -> dict[str, str]:
    """``env`` without one run's launch options (the scrub ``ensure`` applies too)."""
    return scrub_launch_env(env)
