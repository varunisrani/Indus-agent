"""Store health: never fire a job whose run cannot be recorded (P14-05, R07.13).

``StoreHealth.gate()`` runs before any fire. It probes the cron folder the way a save would — a
temp file as large as ``jobs.json`` plus a page, written, fsynced and removed — and checks that
the job file, the ledger files and the locks are writable. Any failure (ENOSPC, EDQUOT, EROFS,
EACCES, EPERM, or anything else) closes the gate: no job fires, the ticker keeps running, and the
gate re-probes at most once a minute.

Notices: one ``unwritable`` event when the store goes bad and one ``recovered`` event after it has
stayed writable for an hour (a flapping disk inside that hour counts as the same outage). On
recovery jobs are simply still due; each fires once under the catch-up rules, and
``outage_since`` lets the due pass fire a one-shot that came due during the outage.

``probe_report(cron_dir)`` is the cross-process view for ``cron status``, ``list`` and doctor.
State is per process (the daemon); a separate ``indus cron tick`` process starts clean.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import os
import secrets
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from indus.foundation.clock import Clock

__all__ = [
    "PROBE_INTERVAL_S",
    "RECOVERY_NOTICE_AFTER_S",
    "UNWRITABLE_ERRNOS",
    "HealthCondition",
    "HealthEvent",
    "StoreHealth",
    "describe_error",
    "probe_dir",
    "probe_report",
]

log = logging.getLogger(__name__)

PROBE_INTERVAL_S = 60.0
RECOVERY_NOTICE_AFTER_S = 3600.0
UNWRITABLE_ERRNOS = frozenset({errno.ENOSPC, errno.EDQUOT, errno.EROFS, errno.EACCES, errno.EPERM})
FIX_HINT = "free disk space, remount it read-write, or fix the permissions of {store}"
_CHUNK = b"\0" * 65536
_WATCHED = (
    "jobs.json",
    "jobs.lock",
    ".tick.lock",
    "executions.db",
    "executions.db-journal",
    "executions.db-wal",
    "executions.db-shm",
)


def describe_error(exc: OSError) -> str:
    """``ENOSPC: No space left on device``; the message alone when the errno has no name."""
    message = exc.strerror or str(exc)
    name = errno.errorcode.get(exc.errno) if isinstance(exc.errno, int) else None
    return message if name is None else f"{name}: {message}"


def probe_dir(cron_dir: Path) -> OSError | None:
    """None when a save would land (or the folder does not exist yet), else the error it hits."""
    if not cron_dir.is_dir():
        return None
    for name in _WATCHED:
        target = os.path.realpath(cron_dir / name)
        if os.path.exists(target) and not os.access(target, os.W_OK):
            return PermissionError(errno.EACCES, os.strerror(errno.EACCES), target)
    try:
        size = (cron_dir / "jobs.json").stat().st_size + 4096
    except OSError:
        size = 4096
    tmp = cron_dir / f".probe-{os.getpid()}-{secrets.token_hex(4)}"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            written = 0
            while written < size:
                written += os.write(fd, _CHUNK[: size - written])
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        return exc
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
    return None


def probe_report(cron_dir: Path) -> dict[str, str] | None:
    """For status/doctor: None when writable, else store, error, since (last save) and fix."""
    error = probe_dir(cron_dir)
    if error is None:
        return None
    try:
        mtime = (cron_dir / "jobs.json").stat().st_mtime
        since = datetime.fromtimestamp(mtime, UTC).isoformat(timespec="seconds")
    except OSError:
        since = "unknown"
    store = str(cron_dir)
    return {
        "store": store,
        "error": describe_error(error),
        "since": since,
        "fix": FIX_HINT.format(store=store),
    }


@dataclass(frozen=True)
class HealthCondition:
    store: str
    error: str
    since: datetime

    @property
    def fix(self) -> str:
        return FIX_HINT.format(store=self.store)


@dataclass(frozen=True)
class HealthEvent:
    kind: Literal["unwritable", "recovered"]
    condition: HealthCondition
    at: datetime


class StoreHealth:
    """The fire gate for one cron folder (see the module notes)."""

    def __init__(
        self,
        cron_dir: Path,
        *,
        clock: Clock,
        notify: Callable[[HealthEvent], None] | None = None,
        probe: Callable[[Path], OSError | None] = probe_dir,
    ) -> None:
        self.cron_dir = cron_dir
        self.clock = clock
        self.notify = notify
        self.probe = probe
        self._condition: HealthCondition | None = None
        self._last_probe: float | None = None
        # The outage that ended, until its recovery notice is sent.
        self._ended: HealthCondition | None = None
        self._writable_since: datetime | None = None

    @property
    def condition(self) -> HealthCondition | None:
        """The current outage, or None while the store is writable."""
        return self._condition

    @property
    def writable(self) -> bool:
        return self._condition is None

    @property
    def outage_since(self) -> datetime | None:
        """Start of the current or just-ended outage (until its recovery notice)."""
        current = self._condition or self._ended
        return current.since if current is not None else None

    def _emit(self, kind: Literal["unwritable", "recovered"], condition: HealthCondition) -> None:
        if self.notify is None:
            return
        try:
            self.notify(HealthEvent(kind, condition, self.clock.now()))
        except Exception:  # a notice must never break the tick
            log.warning("cron store %s notice failed", kind, exc_info=True)

    def gate(self) -> bool:
        """True when jobs may fire now; probes (at most once a minute while unwritable)."""
        mono = self.clock.monotonic()
        if (
            self._condition is not None
            and self._last_probe is not None
            and mono - self._last_probe < PROBE_INTERVAL_S
        ):
            return False
        self._last_probe = mono
        error = self.probe(self.cron_dir)
        now = self.clock.now()
        if error is not None:
            self._enter(error, now)
            return False
        self._leave(now)
        return True

    def _enter(self, error: OSError, now: datetime) -> None:
        text = describe_error(error)
        if self._condition is not None:
            self._condition = replace(self._condition, error=text)
            return
        level = logging.WARNING if error.errno in UNWRITABLE_ERRNOS else logging.ERROR
        if self._ended is not None:
            # Bad again within the hour after recovering: the same outage, no second notice.
            self._condition = replace(self._ended, error=text)
            self._ended = None
            self._writable_since = None
            log.log(level, "cron store %s is unwritable again (%s)", self.cron_dir, text)
            return
        self._condition = HealthCondition(str(self.cron_dir), text, now)
        log.log(
            level,
            "cron store %s is unwritable (%s); no job fires until it accepts writes again. Fix: %s",
            self.cron_dir,
            text,
            self._condition.fix,
        )
        self._emit("unwritable", self._condition)

    def _leave(self, now: datetime) -> None:
        if self._condition is not None:
            log.warning("cron store %s is writable again; due jobs fire once each", self.cron_dir)
            self._ended = self._condition
            self._condition = None
            self._writable_since = now
        if (
            self._ended is not None
            and self._writable_since is not None
            and now - self._writable_since >= timedelta(seconds=RECOVERY_NOTICE_AFTER_S)
        ):
            ended = self._ended
            self._ended = None
            self._writable_since = None
            self._emit("recovered", ended)
