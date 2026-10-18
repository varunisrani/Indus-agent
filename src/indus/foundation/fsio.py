"""Atomic, owner-only file writes and a cross-process file lock.

Every write to a user-owned file goes through this module: the data lands in a temp file beside
the target, is synced, and is renamed over the target, so a crash never leaves half a file. New
files are owner-only (0600); an existing file keeps its mode. Symlinks are refused unless the caller
opts in, so a planted link cannot redirect a write.
"""

from __future__ import annotations

import contextlib
import errno
import json
import logging
import os
import secrets
import stat
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol

log = logging.getLogger(__name__)

NEW_FILE_MODE = 0o600

# errno values that mean "this filesystem cannot lock", as opposed to "someone holds the lock".
_NO_LOCK_SUPPORT = frozenset({errno.ENOLCK, errno.EOPNOTSUPP, errno.ENOTSUP, errno.EINVAL})
_CONTENDED = frozenset({errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK, errno.EDEADLK})


class SymlinkRefused(OSError):
    """The target path is a symlink and the caller did not ask to follow it."""


class LockTimeout(TimeoutError):
    """A `FileLock` could not be taken before its deadline."""

    def __init__(self, message: str, *, lock_path: Path, holder_pid: int | None) -> None:
        super().__init__(message)
        self.lock_path = lock_path
        self.holder_pid = holder_pid


# -- atomic writes ------------------------------------------------------------------------------


def _target_for(path: Path, follow_symlink: bool) -> Path:
    """The path the rename should land on, after applying the symlink policy."""
    if not path.is_symlink():
        return path
    if not follow_symlink:
        raise SymlinkRefused(errno.ELOOP, "refusing to write through a symlink", str(path))
    resolved = path.resolve(strict=False)
    if resolved.exists():
        return resolved
    # A dangling link: replace the link itself, never create the file it points at.
    return path


def _final_mode(target: Path, mode: int | None, preserve_mode: bool) -> int:
    if mode is not None:
        return mode
    if preserve_mode:
        try:
            st = target.stat(follow_symlinks=False)
        except FileNotFoundError:
            return NEW_FILE_MODE
        if stat.S_ISREG(st.st_mode):
            return stat.S_IMODE(st.st_mode)
    return NEW_FILE_MODE


def _fsync_dir(directory: Path) -> None:
    if os.name != "posix":
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_bytes(
    path: Path | str,
    data: bytes,
    *,
    mode: int | None = None,
    preserve_mode: bool = True,
    follow_symlink: bool = False,
) -> Path:
    """Replace ``path`` with ``data`` atomically; return the path actually written.

    ``mode`` forces the final permission bits. Otherwise an existing regular file keeps its bits
    (``preserve_mode``) and a new file gets 0600. The parent directory must exist.
    """
    target = _target_for(Path(path), follow_symlink)
    final_mode = _final_mode(target, mode, preserve_mode)
    tmp = target.with_name(f".{target.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(tmp, flags, NEW_FILE_MODE)
    try:
        with os.fdopen(fd, "wb") as handle:
            if hasattr(os, "fchmod"):
                os.fchmod(handle.fileno(), final_mode)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    if not hasattr(os, "fchmod"):
        with contextlib.suppress(OSError):
            os.chmod(target, final_mode)
    _fsync_dir(target.parent)
    return target


def atomic_write_text(
    path: Path | str,
    text: str,
    *,
    encoding: str = "utf-8",
    mode: int | None = None,
    preserve_mode: bool = True,
    follow_symlink: bool = False,
) -> Path:
    """Text form of `atomic_write_bytes` (no newline translation)."""
    return atomic_write_bytes(
        path,
        text.encode(encoding),
        mode=mode,
        preserve_mode=preserve_mode,
        follow_symlink=follow_symlink,
    )


def atomic_write_json(
    path: Path | str,
    obj: Any,
    *,
    sort_keys: bool = False,
    indent: int | None = 2,
    mode: int | None = None,
    preserve_mode: bool = True,
    follow_symlink: bool = False,
) -> Path:
    """Write ``obj`` as UTF-8 JSON with a trailing newline, atomically."""
    text = json.dumps(obj, sort_keys=sort_keys, indent=indent, ensure_ascii=False)
    return atomic_write_text(
        path,
        text + "\n",
        mode=mode,
        preserve_mode=preserve_mode,
        follow_symlink=follow_symlink,
    )


# -- cross-process lock -------------------------------------------------------------------------


class MonotonicClock(Protocol):
    def monotonic(self) -> float: ...


def _event_wait(seconds: float) -> None:
    threading.Event().wait(seconds)


class _Holder(threading.local):
    depth: int = 0
    fd: int | None = None


_holders: dict[str, _Holder] = {}
_holders_guard = threading.Lock()
_degrade_warned = False


def _holder_for(path: Path) -> _Holder:
    key = os.path.abspath(path)
    with _holders_guard:
        holder = _holders.get(key)
        if holder is None:
            holder = _holders[key] = _Holder()
        return holder


if sys.platform == "win32":
    import msvcrt

    def _os_lock(fd: int, acquire: bool) -> None:
        """Lock or unlock the first byte; raises OSError when another holder has it."""
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK if acquire else msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _os_lock(fd: int, acquire: bool) -> None:
        """Whole-file advisory lock; raises OSError when another holder has it."""
        fcntl.flock(fd, (fcntl.LOCK_EX | fcntl.LOCK_NB) if acquire else fcntl.LOCK_UN)


def _read_holder_pid(path: Path) -> int | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


class FileLock:
    """Exclusive lock on ``path`` shared by threads and processes.

    Re-entrant per thread: a thread that holds the lock may take it again (also through another
    ``FileLock`` object for the same path). On a filesystem that cannot lock, the lock degrades to
    the in-process depth counter with one warning per process.
    """

    def __init__(
        self,
        path: Path | str,
        timeout: float = 15.0,
        poll: float = 0.05,
        clock: MonotonicClock | None = None,
        wait: Callable[[float], object] | None = None,
    ) -> None:
        self.path = Path(path)
        self.timeout = timeout
        self.poll = poll
        self._monotonic: Callable[[], float] = (
            clock.monotonic if clock is not None else time.monotonic
        )
        self._wait: Callable[[float], object] = wait if wait is not None else _event_wait
        self._holder = _holder_for(self.path)

    @property
    def held(self) -> bool:
        return self._holder.depth > 0

    def acquire(self) -> None:
        holder = self._holder
        if holder.depth > 0:
            holder.depth += 1
            return
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, NEW_FILE_MODE)
        try:
            locked = self._lock_fd(fd)
        except BaseException:
            os.close(fd)
            raise
        if locked:
            try:
                os.ftruncate(fd, 0)
                os.lseek(fd, 0, os.SEEK_SET)
                os.write(fd, str(os.getpid()).encode())
            except OSError:
                pass
            holder.fd = fd
        else:
            os.close(fd)
            holder.fd = None
        holder.depth = 1

    def _lock_fd(self, fd: int) -> bool:
        """Return True when the kernel lock is held, False when degraded to depth-only."""
        global _degrade_warned
        deadline = self._monotonic() + self.timeout
        while True:
            try:
                _os_lock(fd, True)
                return True
            except OSError as exc:
                if exc.errno in _NO_LOCK_SUPPORT and exc.errno not in _CONTENDED:
                    if not _degrade_warned:
                        _degrade_warned = True
                        log.warning(
                            "file locks are not supported for %s (%s); "
                            "falling back to in-process locking only",
                            self.path,
                            exc.strerror or exc,
                        )
                    return False
                if exc.errno not in _CONTENDED:
                    raise
            if self._monotonic() >= deadline:
                pid = _read_holder_pid(self.path)
                who = f"process {pid}" if pid is not None else "another process"
                raise LockTimeout(
                    f"timed out after {self.timeout:g}s waiting for {self.path} (held by {who})",
                    lock_path=self.path,
                    holder_pid=pid,
                )
            self._wait(self.poll)

    def release(self) -> None:
        holder = self._holder
        if holder.depth <= 0:
            raise RuntimeError(f"release of an unheld lock: {self.path}")
        holder.depth -= 1
        if holder.depth > 0:
            return
        fd = holder.fd
        holder.fd = None
        if fd is None:
            return
        with contextlib.suppress(OSError):
            os.ftruncate(fd, 0)
        try:
            _os_lock(fd, False)
        except OSError:
            pass
        finally:
            os.close(fd)

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()
