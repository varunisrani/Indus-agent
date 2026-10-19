"""One gateway per profile home: the instance lock, the pid record and takeover (P15-24).

``InstanceLock`` is an exclusive ``flock`` on ``<home>/gateway.lock`` held for the whole life of
the daemon. The kernel drops it when the process dies, so a crash never leaves a lock that needs
cleaning, and two starts racing each other cannot both win. The lock file is never deleted (a
process could otherwise lock a new file while another still holds the old one); only the pid
record is removed at a clean exit. The descriptor is not inherited by child processes.

``claim_instance`` decides what a start does:
- the lock is free → it is ours; the pid record is (re)written;
- the lock is held → another gateway serves this home: without ``replace`` the caller reports it
  (exit 0 at a shell, 75 under a supervisor); with ``replace`` the holder named by the pid record
  gets SIGTERM, then, after ``wait`` seconds and only while its start time still matches (never a
  recycled pid), SIGKILL; the lock is retried while it goes away.
"""

from __future__ import annotations

import contextlib
import fcntl
import logging
import os
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from indus.foundation.clock import SystemClock
from indus.state.gateway_runtime import (
    PidRecord,
    ProcessIdentity,
    lock_path,
    pid_path,
    process_start_time,
    read_pid_record,
    write_pid_record,
)

_SYSTEM = SystemClock()

__all__ = ["InstanceClaim", "InstanceLock", "claim_instance", "release_instance"]

log = logging.getLogger(__name__)


class InstanceLock:
    """The profile's single-instance ``flock`` (see the module notes)."""

    def __init__(self, home: Path) -> None:
        self.path = lock_path(home)
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def try_acquire(self) -> bool:
        """Take the lock without waiting; True when it is ours."""
        if self._fd is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)  # not inheritable (PEP 446)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(fd)


@dataclass
class InstanceClaim:
    outcome: Literal["acquired", "running"]
    lock: InstanceLock | None = None
    holder: PidRecord | None = None
    replaced: bool = False


def _poll_lock(
    lock: InstanceLock, wait: float, *, sleep: Callable[[float], None], step: float = 0.1
) -> bool:
    end = time.monotonic() + max(0.0, wait)
    while True:
        if lock.try_acquire():
            return True
        if time.monotonic() >= end:
            return False
        sleep(step)


def _signal(
    holder: PidRecord,
    signum: int,
    *,
    kill: Callable[[int, int], None],
    start_time: Callable[[int], int | None],
) -> bool:
    """Signal the holder only while its start time still matches (never a recycled pid)."""
    recorded = holder.process.start_time
    current = start_time(holder.pid)
    if recorded is not None and current is not None and recorded != current:
        log.warning("not signalling pid %d: it is no longer the recorded gateway", holder.pid)
        return False
    if current is None and recorded is not None:
        return False  # gone (or unreadable): nothing safe to signal
    try:
        kill(holder.pid, signum)
    except ProcessLookupError:
        return False
    except OSError as exc:
        log.warning("could not signal gateway pid %d: %s", holder.pid, exc)
        return False
    return True


def claim_instance(
    home: Path,
    record: PidRecord,
    *,
    replace: bool = False,
    wait: float = 10.0,
    kill_wait: float = 5.0,
    settle: float = 1.0,
    sleep: Callable[[float], None] = _SYSTEM.sleep_sync,
    kill: Callable[[int, int], None] = os.kill,
    start_time: Callable[[int], int | None] = process_start_time,
    probes: dict[str, Any] | None = None,
) -> InstanceClaim:
    """Take the profile for ``record``'s process (see the module notes).

    ``settle``: how long a held lock is retried before a holder without a readable pid record (one
    still starting) is believed."""
    lock = InstanceLock(home)
    if lock.try_acquire():
        write_pid_record(home, record)
        return InstanceClaim("acquired", lock)
    holder = read_pid_record(home)
    if holder is None or holder.pid == record.pid:
        # Locked but no record yet: another start is between its lock and its write.
        if _poll_lock(lock, settle, sleep=sleep):
            write_pid_record(home, record)
            return InstanceClaim("acquired", lock)
        holder = read_pid_record(home)
    if not replace:
        return InstanceClaim("running", holder=holder)
    if holder is not None and holder.process.is_alive(**(probes or {})):
        log.info("replacing the running gateway (pid %d)", holder.pid)
        if _signal(holder, signal.SIGTERM, kill=kill, start_time=start_time) and not _poll_lock(
            lock, wait, sleep=sleep
        ):
            log.warning("gateway pid %d did not stop within %.0f s; killing it", holder.pid, wait)
            _signal(holder, signal.SIGKILL, kill=kill, start_time=start_time)
    if _poll_lock(lock, kill_wait if holder is not None else wait, sleep=sleep):
        write_pid_record(home, record)
        return InstanceClaim("acquired", lock, replaced=holder is not None)
    return InstanceClaim("running", holder=read_pid_record(home))


def release_instance(home: Path, claim: InstanceClaim, me: ProcessIdentity) -> None:
    """Remove our pid record (never someone else's), then drop the lock."""
    current = read_pid_record(home)
    if current is not None and current.pid == me.pid:
        with contextlib.suppress(OSError):
            pid_path(home).unlink(missing_ok=True)
    if claim.lock is not None:
        claim.lock.release()
