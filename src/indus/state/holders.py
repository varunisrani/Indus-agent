"""Holder stamps for lock and lease rows, and the rule for when a holder may be replaced.

A lock row names its holder. When the holding process dies without releasing, the row would block
the session until it expires; when its process is *provably* gone we may take the row earlier.
"Provably" is strict, because two compressors on one conversation fork its lineage:

- the stamp must parse: ``pid=<n>:pidns=<inode of /proc/self/ns/pid>:<host>`` (callers may append
  ``:<more>``, e.g. a task nonce; only the first three fields are read);
- the PID is not our own (another thread of this process may hold it legitimately);
- the host is ours and the recorded PID namespace equals ours, and ours is known — a PID from a
  sibling container on a shared volume means nothing here (R03.20);
- ``os.kill(pid, 0)`` reports no such process. A permission error means "alive, not ours"; any
  other doubt keeps the row until its expiry. A wrongly kept row heals itself when it expires.
"""

from __future__ import annotations

import logging
import os
import re
import socket
import sys
from dataclasses import dataclass

__all__ = [
    "HolderInfo",
    "holder_is_stale",
    "holder_process_is_dead",
    "holder_stamp",
    "local_pid_namespace",
    "parse_holder",
]

log = logging.getLogger(__name__)

_UNKNOWN_NS = "-"
_NS_LINK = re.compile(r"\[(\d+)\]")
_PID_FIELD = re.compile(r"pid=(\d+)")
_NS_FIELD = re.compile(r"pidns=(\d+|-)")

_cached_namespace: str | None = None


@dataclass(frozen=True, slots=True)
class HolderInfo:
    pid: int
    pidns: str | None
    host: str


def local_pid_namespace() -> str | None:
    """The inode number of this process's PID namespace, or None where it cannot be read.

    A successful read is cached (a process never changes its own namespace); a failed one is not.
    """
    global _cached_namespace
    if _cached_namespace is not None:
        return _cached_namespace
    if not sys.platform.startswith("linux"):
        return None
    try:
        target = os.readlink("/proc/self/ns/pid")
    except OSError:
        return None
    found = _NS_LINK.search(target)
    if found is None:
        return None
    _cached_namespace = found.group(1)
    return _cached_namespace


def _host() -> str:
    try:
        return socket.gethostname() or "localhost"
    except OSError:
        return "localhost"


def holder_stamp() -> str:
    """``pid=<n>:pidns=<inode>:<host>`` for this process (``pidns=-`` when unknown)."""
    namespace = local_pid_namespace() or _UNKNOWN_NS
    return f"pid={os.getpid()}:pidns={namespace}:{_host()}"


def parse_holder(holder: str | None) -> HolderInfo | None:
    """The stamp fields of ``holder``; None for anything that is not one of our stamps."""
    if not holder:
        return None
    fields = holder.split(":")
    if len(fields) < 3:
        return None
    pid_match = _PID_FIELD.fullmatch(fields[0])
    ns_match = _NS_FIELD.fullmatch(fields[1])
    if pid_match is None or ns_match is None or not fields[2]:
        return None
    namespace = ns_match.group(1)
    return HolderInfo(
        pid=int(pid_match.group(1)),
        pidns=None if namespace == _UNKNOWN_NS else namespace,
        host=fields[2],
    )


def _pid_gone(pid: int) -> bool:
    """True only when the kernel says the PID does not exist (tests patch this)."""
    if os.name != "posix":
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except (OSError, OverflowError):
        return False  # permission denied: alive under another user
    return False


def holder_process_is_dead(holder: str | None) -> bool:
    """True only with proof: see the module docstring."""
    info = parse_holder(holder)
    if info is None or info.pid <= 0 or info.pid == os.getpid():
        return False
    ours = local_pid_namespace()
    if ours is None or info.pidns != ours or info.host != _host():
        return False
    try:
        return _pid_gone(info.pid)
    except Exception:  # any probe trouble is doubt, and doubt keeps the row
        log.debug("probing lock holder %s failed", holder, exc_info=True)
        return False


def holder_is_stale(holder: str | None, expires_at: float | None, now: float) -> bool:
    """A row may be replaced: it has expired, or its holder's process is provably gone."""
    if expires_at is not None and expires_at <= now:
        return True
    return holder_process_is_dead(holder)
