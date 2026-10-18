"""One running surface per session: a non-blocking advisory lock file per session (D-16.69).

Until the durable turn lease of P17 exists, this lock is what keeps two surfaces — two REPLs, or a
REPL and a TUI, in one process or in two — from running turns on the same session. Each session
has a file under ``<store folder>/session-locks``; whoever holds an exclusive OS lock on it owns
the session. Asking never waits: a held session comes back as ``LockBusy`` at once (or
``SessionBusy``, code ``session.busy``, which the protocol maps to ``SESSION_BUSY``).

Crash recovery is mostly the kernel's job: it drops the OS lock when the holder dies, and the next
caller takes the file and reports the dead holder in ``recovered_from``. The file also carries the
holder's stamp (``state.holders``); when the lock is still held but that stamp names a process
that provably no longer runs (a forked child kept the descriptor), the file is replaced.

Locks taken by this process are also kept in an in-process table, so threads and objects in one
process see each other even where the file system cannot lock (then the table is all there is).
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import logging
import os
import re
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Self

from indus.foundation.errors import IndusError
from indus.state import holders

__all__ = [
    "LOCK_DIR_NAME",
    "LockBusy",
    "SessionBusy",
    "SessionLock",
    "lock_dir_for",
    "lock_file_name",
    "lock_session",
    "try_lock_session",
]

log = logging.getLogger(__name__)

LOCK_DIR_NAME = "session-locks"
_SUFFIX = ".lock"
_PLAIN_NAME = re.compile(r"[a-z0-9._-]{1,128}")
_HASH_CHARS = 40
_MAX_READ = 4096
_ATTEMPTS = 4
_CONTENDED = frozenset({errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK, errno.EDEADLK})
_UNSUPPORTED = frozenset({errno.ENOLCK, errno.EOPNOTSUPP, errno.ENOTSUP, errno.EINVAL})

_table: dict[str, SessionLock] = {}
_table_guard = threading.Lock()
_unsupported_warned = False


class SessionBusy(IndusError):
    """Another surface (in this process or another one) is running this session."""

    default_code = "session.busy"

    def __init__(
        self,
        code_or_message: str,
        message: str | None = None,
        *,
        session_id: str = "",
        holder: str | None = None,
        detail: object = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(code_or_message, message, detail=detail, hint=hint)
        self.session_id: str = session_id
        self.holder: str | None = holder


@dataclass(frozen=True, slots=True)
class LockBusy:
    """The answer when a session is held by someone else."""

    session_id: str
    holder: str | None
    """The holder's stamp (``pid=…:pidns=…:host``), when it could be read."""
    owner: str
    """The holder's own label (e.g. ``tui``); empty when it gave none."""
    in_process: bool
    """True when the holder is this very process."""

    def describe(self) -> str:
        """A one-line explanation for logs and error messages."""
        if self.in_process:
            who = "this process"
        else:
            info = holders.parse_holder(self.holder)
            who = f"process {info.pid}" if info is not None else "another process"
        label = f" ({self.owner})" if self.owner else ""
        return f"session {self.session_id} is in use by {who}{label}"

    def error(self) -> SessionBusy:
        """The typed error for callers that raise instead of branching."""
        return SessionBusy(
            self.describe(),
            session_id=self.session_id,
            holder=self.holder,
            hint="finish or close the other window, then try again",
        )


def lock_dir_for(store_path: Path) -> Path:
    """Where the session lock files of the store at ``store_path`` live."""
    return Path(store_path).parent / LOCK_DIR_NAME


def lock_file_name(session_id: str) -> str:
    """The lock file's name: the id when it is plain lowercase, else a hash of it.

    Hashing anything else keeps names safe on case-insensitive file systems and keeps path
    characters out of the folder.
    """
    if not session_id:
        raise ValueError("session_id must not be empty")
    if _PLAIN_NAME.fullmatch(session_id) and session_id not in {".", ".."}:
        return session_id + _SUFFIX
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:_HASH_CHARS]
    return f"h-{digest}{_SUFFIX}"


# -- OS lock primitives ------------------------------------------------------------------------

if sys.platform == "win32":  # pragma: no cover - sketched for Windows; not exercised in CI yet
    import msvcrt

    def _os_try_lock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _os_unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

    _CAN_UNLINK_OPEN = False

else:
    import fcntl

    def _os_try_lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _os_unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)

    _CAN_UNLINK_OPEN = True


def _open(path: Path) -> int:
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOINHERIT", 0)
    return os.open(path, flags, 0o600)


def _read(fd: int) -> tuple[str | None, str]:
    """The stamp and owner label recorded in an open lock file."""
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, _MAX_READ)
    except OSError:
        return None, ""
    lines = raw.decode("utf-8", "replace").splitlines()
    stamp = lines[0].strip() if lines and lines[0].strip() else None
    owner = lines[1].strip() if len(lines) > 1 else ""
    return stamp, owner


def _write(fd: int, owner: str) -> None:
    text = holders.holder_stamp() + "\n" + owner.replace("\n", " ").strip() + "\n"
    try:
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, text.encode("utf-8"))
    except OSError:
        log.debug("could not record the holder in a session lock file", exc_info=True)


def _is_current(fd: int, path: Path) -> bool:
    """True when ``fd`` is still the file at ``path`` (nobody unlinked or replaced it)."""
    try:
        held, named = os.fstat(fd), os.stat(path)
    except OSError:
        return False
    return (held.st_dev, held.st_ino) == (named.st_dev, named.st_ino)


def _warn_unsupported(path: Path, exc: OSError) -> None:
    global _unsupported_warned
    if not _unsupported_warned:
        _unsupported_warned = True
        log.warning(
            "file locks do not work for %s (%s); sessions are guarded within this process only",
            path,
            exc.strerror or exc,
        )


# -- the lock ----------------------------------------------------------------------------------


class SessionLock:
    """A held session. Release it with ``release()`` or by leaving its ``with`` block."""

    def __init__(
        self,
        session_id: str,
        path: Path,
        fd: int | None,
        *,
        owner: str,
        recovered_from: str | None,
    ) -> None:
        self.session_id = session_id
        self.path = path
        self.owner = owner
        self.recovered_from = recovered_from
        """Stamp of a previous holder that died without releasing, if there was one."""
        self._fd = fd
        self._held = True
        self._guard = threading.Lock()

    @property
    def held(self) -> bool:
        return self._held

    @property
    def os_locked(self) -> bool:
        """False when the file system could not lock and only the in-process table guards it."""
        return self._fd is not None

    def release(self) -> None:
        """Give the session up. Calling it again does nothing."""
        with self._guard:
            if not self._held:
                return
            self._held = False
            fd, self._fd = self._fd, None
        try:
            if fd is not None:
                if _CAN_UNLINK_OPEN and _is_current(fd, self.path):
                    with contextlib.suppress(OSError):
                        self.path.unlink()
                with contextlib.suppress(OSError):
                    _os_unlock(fd)
                with contextlib.suppress(OSError):
                    os.close(fd)
        finally:
            key = str(self.path)
            with _table_guard:
                if _table.get(key) is self:
                    del _table[key]

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()

    def __repr__(self) -> str:
        state = "held" if self._held else "released"
        return f"SessionLock({self.session_id!r}, {state})"


def try_lock_session(
    directory: Path, session_id: str, *, owner: str = ""
) -> SessionLock | LockBusy:
    """Take ``session_id``'s lock under ``directory`` without waiting.

    Returns the held ``SessionLock``, or ``LockBusy`` naming the current holder. ``owner`` is a
    short label for the holder (``repl``, ``tui``…) shown to whoever finds the session busy.
    File-system faults other than contention (an unwritable folder) raise ``OSError``.
    """
    path = Path(directory) / lock_file_name(session_id)
    key = str(path)
    with _table_guard:
        mine = _table.get(key)
        if mine is not None:
            return LockBusy(session_id, holders.holder_stamp(), mine.owner, in_process=True)
        # Reserve the slot so another thread of ours cannot race through the file step.
        placeholder = SessionLock(session_id, path, None, owner=owner, recovered_from=None)
        _table[key] = placeholder
    try:
        result = _lock_file(path, session_id, owner)
    except BaseException:
        with _table_guard:
            if _table.get(key) is placeholder:
                del _table[key]
        raise
    with _table_guard:
        if isinstance(result, SessionLock):
            _table[key] = result
        elif _table.get(key) is placeholder:
            del _table[key]
    return result


def lock_session(directory: Path, session_id: str, *, owner: str = "") -> SessionLock:
    """Like ``try_lock_session`` but raises ``SessionBusy`` when the session is held."""
    result = try_lock_session(directory, session_id, owner=owner)
    if isinstance(result, LockBusy):
        raise result.error()
    return result


def _lock_file(path: Path, session_id: str, owner: str) -> SessionLock | LockBusy:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    broke_once = False
    for _ in range(_ATTEMPTS):
        fd = _open(path)
        try:
            _os_try_lock(fd)
        except OSError as exc:
            if exc.errno in _UNSUPPORTED and exc.errno not in _CONTENDED:
                _warn_unsupported(path, exc)
                os.close(fd)
                return SessionLock(session_id, path, None, owner=owner, recovered_from=None)
            if exc.errno not in _CONTENDED:
                os.close(fd)
                raise
            stamp, holder_owner = _read(fd)
            os.close(fd)
            if not broke_once and holders.holder_process_is_dead(stamp):
                broke_once = True
                log.warning(
                    "session %s: lock holder %s no longer runs; taking the session over",
                    session_id,
                    stamp,
                )
                with contextlib.suppress(OSError):
                    path.unlink()
                continue
            return LockBusy(session_id, stamp, holder_owner, in_process=False)
        if not _is_current(fd, path):
            # The file was unlinked (released or broken) between our open and our lock.
            with contextlib.suppress(OSError):
                _os_unlock(fd)
            os.close(fd)
            continue
        previous, _ = _read(fd)
        recovered = previous if holders.holder_process_is_dead(previous) else None
        if recovered is not None:
            log.info("session %s: previous holder %s ended without releasing", session_id, previous)
        _write(fd, owner)
        return SessionLock(session_id, path, fd, owner=owner, recovered_from=recovered)
    raise OSError(errno.EAGAIN, f"session lock file kept changing: {path}")
