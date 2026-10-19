"""Machine-local locks on platform credentials: two gateways never poll one bot token (P15-06).

On connect, every adapter that owns a unique credential takes a lock file in a directory shared by
every profile and install of the same OS user (R04.26). The file is named from the platform and
the first 16 hex characters of the credential's SHA-256, so the credential itself is never written
anywhere; it holds the owner's pid, process start time, profile and home.

Outcomes (R06.16):
- free, ours already, or held by a dead process → acquired (a dead holder is a pid that no longer
  exists, or a pid whose start time differs from the record: the number was reused);
- held by a live foreign process at startup → a fatal config conflict for that platform;
- the same conflict while running → retryable (the other holder may be shutting down).
A pid that is alive but whose start time cannot be read on either side counts as live (fail closed).

Every read-decide-write runs under one cross-process guard lock in the directory, so two racers
can never both reclaim one stale record; records land by atomic replace with mode 0600.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from indus.foundation import fsio

__all__ = [
    "GUARD_FILE",
    "LOCK_DIR_ENV",
    "LockHolder",
    "LockOutcome",
    "TokenLocks",
    "credential_hash",
    "default_lock_dir",
    "lock_file_name",
    "pid_alive",
    "process_start_time",
]

log = logging.getLogger(__name__)

LOCK_DIR_ENV = "INDUS_GATEWAY_LOCK_DIR"
GUARD_FILE = ".guard"
GUARD_TIMEOUT = 10.0
_DIR_NAME = "gateway-locks"
_APP = "indus"


# -- process identity -----------------------------------------------------------------------------


def pid_alive(pid: int) -> bool:
    """True when a process with ``pid`` exists (owned by anyone)."""
    if pid < 1:
        return False
    try:
        os.kill(pid, 0)  # signal 0 delivers nothing; it only asks whether the pid exists
    except OSError as exc:
        return exc.errno == errno.EPERM  # exists, but another user owns it
    return True


def _ps_start_time(pid: int) -> int | None:
    try:
        done = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
            env={"LC_ALL": "C", "PATH": "/bin:/usr/bin:/usr/sbin:/sbin"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = " ".join(done.stdout.split())
    if not text:
        return None
    try:
        return int(time.mktime(time.strptime(text, "%a %b %d %H:%M:%S %Y")))
    except ValueError:
        return None


def process_start_time(pid: int) -> int | None:
    """A per-process start fingerprint (None when unknown).

    Linux: field 22 of ``/proc/<pid>/stat`` (clock ticks since boot). Elsewhere: ``ps``'s start
    time in epoch seconds. Only compared with values read the same way on the same host.
    """
    if pid <= 0:
        return None
    if sys.platform.startswith("linux"):
        try:
            raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
        except OSError:
            return None
        fields = raw.rsplit(")", 1)[-1].split()
        try:
            return int(fields[19])
        except (IndexError, ValueError):
            return None
    if sys.platform == "win32":
        return None
    return _ps_start_time(pid)


# -- names and places ------------------------------------------------------------------------------


def credential_hash(credential: str) -> str:
    return hashlib.sha256(credential.encode("utf-8")).hexdigest()[:16]


def lock_file_name(platform: str, credential: str) -> str:
    return f"{platform}-{credential_hash(credential)}.lock"


def default_lock_dir(
    env: Mapping[str, str],
    *,
    family: str | None = None,
    user_home: Callable[[], Path] = Path.home,
) -> Path:
    """The per-OS-user directory every profile's gateway shares for credential locks.

    ``INDUS_GATEWAY_LOCK_DIR`` (absolute) overrides. Linux and other POSIX:
    ``$XDG_STATE_HOME/indus/gateway-locks`` (a relative ``XDG_STATE_HOME`` is invalid per the XDG
    spec and ignored) else ``~/.local/state/indus/gateway-locks``; macOS:
    ``~/Library/Application Support/indus/gateway-locks``; Windows:
    ``%LOCALAPPDATA%\\indus\\gateway-locks``.
    """
    override = (env.get(LOCK_DIR_ENV) or "").strip()
    if override and os.path.isabs(override):
        return Path(override)
    kind = family or ("darwin" if sys.platform == "darwin" else sys.platform)
    if kind == "darwin":
        return user_home() / "Library" / "Application Support" / _APP / _DIR_NAME
    if kind == "win32":
        local = (env.get("LOCALAPPDATA") or "").strip()
        base = Path(local) if local else user_home() / "AppData" / "Local"
        return base / _APP / _DIR_NAME
    state = (env.get("XDG_STATE_HOME") or "").strip()
    base = Path(state) if state and os.path.isabs(state) else user_home() / ".local" / "state"
    return base / _APP / _DIR_NAME


# -- records ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LockHolder:
    pid: int
    start_time: int | None
    platform: str
    profile: str = ""
    home: str = ""
    acquired_at: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "start_time": self.start_time,
            "platform": self.platform,
            "profile": self.profile,
            "home": self.home,
            "acquired_at": self.acquired_at,
        }

    @classmethod
    def from_json(cls, data: object) -> LockHolder | None:
        if not isinstance(data, dict):
            return None
        raw: dict[str, Any] = {str(k): v for k, v in data.items()}  # pyright: ignore[reportUnknownVariableType]
        pid = raw.get("pid")
        start = raw.get("start_time")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return None
        if start is not None and (not isinstance(start, int) or isinstance(start, bool)):
            start = None
        return cls(
            pid=pid,
            start_time=start,
            platform=str(raw.get("platform") or ""),
            profile=str(raw.get("profile") or ""),
            home=str(raw.get("home") or ""),
            acquired_at=str(raw.get("acquired_at") or ""),
        )

    def describe(self) -> str:
        who = f"profile {self.profile}" if self.profile else "another gateway"
        return f"{who} (pid {self.pid})"


@dataclass(frozen=True)
class LockOutcome:
    acquired: bool
    fatal: bool = False
    retryable: bool = False
    holder: LockHolder | None = None
    message: str = ""


def _read_record(path: Path) -> tuple[bool, LockHolder | None]:
    """``(exists, holder)``; an existing file that is not a valid record gives ``(True, None)``."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False, None
    except OSError:
        return True, None
    try:
        return True, LockHolder.from_json(json.loads(text))
    except ValueError:
        return True, None


class TokenLocks:
    """The credential locks one gateway process holds."""

    def __init__(
        self,
        directory: Path,
        *,
        profile: str = "",
        home: str = "",
        pid: int | None = None,
        start_time: Callable[[int], int | None] = process_start_time,
        alive: Callable[[int], bool] = pid_alive,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.directory = Path(directory)
        self.profile = profile
        self.home = home
        self.pid = os.getpid() if pid is None else pid
        self._start_time = start_time
        self._alive = alive
        self._now = now if now is not None else (lambda: datetime.now(UTC))
        self._held: dict[str, Path] = {}
        self._lock = threading.Lock()

    # -- helpers ------------------------------------------------------------------------------

    def _path(self, platform: str, credential: str) -> Path:
        return self.directory / lock_file_name(platform, credential)

    def _ensure_dir(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self.directory, 0o700)

    def _guard(self) -> fsio.FileLock:
        return fsio.FileLock(self.directory / GUARD_FILE, timeout=GUARD_TIMEOUT)

    def _is_stale(self, holder: LockHolder | None) -> bool:
        """A record no longer naming a live process (or not a record at all)."""
        if holder is None:
            return True
        if not self._alive(holder.pid):
            return True
        current = self._start_time(holder.pid)
        if holder.start_time is not None and current is not None:
            return holder.start_time != current
        return False

    def _is_ours(self, holder: LockHolder | None) -> bool:
        if holder is None or holder.pid != self.pid:
            return False
        mine = self._start_time(self.pid)
        return holder.start_time is None or mine is None or holder.start_time == mine

    def _record(self, platform: str) -> LockHolder:
        return LockHolder(
            pid=self.pid,
            start_time=self._start_time(self.pid),
            platform=platform,
            profile=self.profile,
            home=self.home,
            acquired_at=self._now().astimezone(UTC).isoformat(timespec="seconds"),
        )

    # -- public ---------------------------------------------------------------------------------

    def acquire(self, platform: str, credential: str, *, startup: bool) -> LockOutcome:
        """Take the lock for ``platform``'s ``credential``.

        A live foreign holder is fatal at ``startup`` and retryable later (R06.16).
        """
        if not credential:
            raise ValueError("a token lock needs a non-empty credential")
        path = self._path(platform, credential)
        self._ensure_dir()
        with self._guard():
            exists, holder = _read_record(path)
            if exists and not self._is_ours(holder) and not self._is_stale(holder):
                assert holder is not None
                return self._conflict(platform, holder, startup=startup)
            if exists and not self._is_ours(holder):
                log.info(
                    "platform %s: reclaiming a stale credential lock (%s)",
                    platform,
                    holder.describe() if holder is not None else "unreadable record",
                )
            fsio.atomic_write_json(path, self._record(platform).to_json(), mode=0o600)
        with self._lock:
            self._held[f"{platform}\0{credential_hash(credential)}"] = path
        return LockOutcome(acquired=True, holder=holder if exists else None)

    def _conflict(self, platform: str, holder: LockHolder, *, startup: bool) -> LockOutcome:
        if startup:
            message = (
                f"{platform}: this bot credential is already in use by {holder.describe()}; "
                "one credential can serve only one gateway. Stop the other gateway or give this "
                "profile its own bot."
            )
            return LockOutcome(acquired=False, fatal=True, holder=holder, message=message)
        message = (
            f"{platform}: the bot credential is held by {holder.describe()}; "
            "retrying once it is released"
        )
        return LockOutcome(acquired=False, retryable=True, holder=holder, message=message)

    def release(self, platform: str, credential: str) -> bool:
        """Drop our lock (a record that is no longer ours is left alone)."""
        key = f"{platform}\0{credential_hash(credential)}"
        with self._lock:
            path = self._held.pop(key, None)
        if path is None:
            path = self._path(platform, credential)
        return self._release_path(path)

    def _release_path(self, path: Path) -> bool:
        if not self.directory.is_dir():
            return False
        with self._guard():
            _exists, holder = _read_record(path)
            if holder is None or holder.pid != self.pid:
                return False
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
            return True

    def release_all(self) -> int:
        with self._lock:
            paths = list(self._held.values())
            self._held.clear()
        return sum(1 for path in paths if self._release_path(path))

    def held(self) -> list[Path]:
        with self._lock:
            return list(self._held.values())
