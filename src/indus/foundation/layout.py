"""The profile home on disk: working folders, the seeded persona file and the scratch temp dir.

``ensure_home(scope)`` is cheap enough to call before any write into a home: it is remembered
per home, but the check that a named profile still exists runs first every time, so a stale
route can never re-create a profile that was deleted (its tombstone sits beside it at
``<root>/profiles/.deleted/<id>``).

The scratch dir ``<home>/cache/scratch`` is the temp dir of the process and of every child it
starts (``TMPDIR``/``TMP``/``TEMP``, unless the user set them). Entries nobody wrote to for a day
are pruned at most once an hour.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from indus.foundation.errors import ProfileError, ProfileNotFound, ProfileTombstoned
from indus.foundation.fsio import atomic_write_text
from indus.foundation.home import secure_dir
from indus.foundation.profiles import is_tombstoned, named_profile_of

if TYPE_CHECKING:
    from indus.foundation.clock import Clock
    from indus.foundation.scope import Scope

__all__ = [
    "DEFAULT_PERSONA",
    "PERSONA_FILE",
    "SKELETON_DIRS",
    "TEMP_VARS",
    "ensure_home",
    "profile_home_problem",
    "prune_scratch",
    "reset_memo",
    "scratch_dir",
    "scratch_env",
    "scratch_path",
    "seed_persona",
    "socket_dir",
]

log = logging.getLogger(__name__)

SKELETON_DIRS = (
    "logs",
    "sessions",
    "memories",
    "skills",
    "cron",
    "cache",
    "cache/scratch",
    "backups",
    "plugins",
    "hooks",
)

PERSONA_FILE = "PERSONA.md"

# Written once into a new home; the user owns the file from then on.
DEFAULT_PERSONA = """\
You are Indus Agent, an assistant that works alongside the user on their own machine.

- Get to the point: give the answer first, then only the detail that helps.
- When you are not sure, say so, and check with your tools rather than guess.
- Before anything destructive or hard to undo, stop and ask.
- Respect the user's time, their preferences and their privacy.
"""

TEMP_VARS = ("TMPDIR", "TMP", "TEMP")
SCRATCH_IDLE_SECONDS = 24 * 3600
PRUNE_INTERVAL_SECONDS = 3600
PRUNE_STAMP = ".last_prune"
# AF_UNIX paths are capped near 104-108 bytes; programs append long socket names to the temp
# root, so a longer root makes their bind fail.
SOCKET_PATH_BUDGET = 50

_memo: set[str] = set()
_memo_lock = threading.Lock()


def reset_memo() -> None:
    """Forget which homes were ensured (tests, and after a profile is deleted)."""
    with _memo_lock:
        _memo.clear()


def _secure_mkdir(path: Path, scope: Scope) -> None:
    # The home dir mode (INDUS_HOME_MODE, default 0700); managed installs keep their own modes.
    secure_dir(path, home=scope.home, env=scope.env)


def profile_home_problem(home: Path) -> str | None:
    """Why ``home`` must not be written to, as an error code, or None when it may be.

    Only a named profile (``<root>/profiles/<id>``) can be refused: when its tombstone exists
    (``profile.tombstoned``) or its dir is gone (``profile.not_found``). The default home may
    be created from nothing.
    """
    named = named_profile_of(home)
    if named is None:
        return None
    if is_tombstoned(*named):
        return "profile.tombstoned"
    if not home.is_dir():
        return "profile.not_found"
    return None


def _refuse(home: Path, code: str) -> ProfileError:
    name = home.name
    hint = "see `indus profile list`"
    if code == "profile.tombstoned":
        return ProfileTombstoned(f"profile '{name}' was deleted", detail=str(home), hint=hint)
    return ProfileNotFound(f"profile '{name}' does not exist", detail=str(home), hint=hint)


def ensure_home(scope: Scope) -> Path:
    """Create the home's working folders (0700) and seed ``PERSONA.md``; returns the home."""
    home = scope.home
    problem = profile_home_problem(home)
    if problem is not None:
        raise _refuse(home, problem)
    key = str(home)
    with _memo_lock:
        if key in _memo and home.is_dir():
            return home
    _secure_mkdir(home, scope)
    for rel in SKELETON_DIRS:
        _secure_mkdir(home / rel, scope)
    seed_persona(home)
    with _memo_lock:
        _memo.add(key)
    return home


def seed_persona(home: Path) -> bool:
    """Write the default persona when there is none; True when it was written.

    An existing file (even an empty one) or a link to an existing file is the user's and is
    left alone. A dangling link is replaced by a real file; its target is never created.
    """
    path = home / PERSONA_FILE
    dangling = path.is_symlink() and not path.exists()
    if not dangling and os.path.lexists(path):
        return False
    atomic_write_text(path, DEFAULT_PERSONA, mode=0o600, follow_symlink=dangling)
    return True


# -- scratch ------------------------------------------------------------------------------------


def scratch_path(home: Path) -> Path:
    return home / "cache" / "scratch"


def scratch_dir(scope: Scope) -> Path:
    """``<home>/cache/scratch``, created owner-only when missing."""
    path = scratch_path(scope.home)
    _secure_mkdir(path, scope)
    return path


def scratch_env(home: Path, env: Mapping[str, str]) -> dict[str, str]:
    """The temp-dir variables to point at the scratch dir: only those the user left unset."""
    target = str(scratch_path(home))
    return {name: target for name in TEMP_VARS if not env.get(name)}


def _idle(entry: Path, cutoff: float) -> bool:
    """True when nothing in ``entry``'s tree was written at or after ``cutoff``.

    Links are not followed. A file with more than one hard link does not count as a write:
    its mtime belongs to the shared inode (``git clone --local`` links packs that the source
    repo keeps touching). Anything that cannot be inspected counts as busy (kept).
    """
    try:
        if entry.lstat().st_mtime >= cutoff:
            return False
        if entry.is_symlink() or not entry.is_dir():
            return True
        pending = [entry]
        while pending:
            with os.scandir(pending.pop()) as listing:
                for child in listing:
                    info = child.stat(follow_symlinks=False)
                    shared = info.st_nlink > 1 and child.is_file(follow_symlinks=False)
                    if info.st_mtime >= cutoff and not shared:
                        return False
                    if child.is_dir(follow_symlinks=False):
                        pending.append(Path(child.path))
    except OSError:
        return False
    return True


def _remove(entry: Path) -> bool:
    try:
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    except OSError as exc:
        log.warning("scratch prune: could not remove %s: %s", entry, exc)
        return False
    return True


def prune_scratch(scope: Scope, clock: Clock) -> int:
    """Delete scratch entries idle for over 24 h; runs at most once an hour per home.

    Returns how many entries were removed (0 when the hourly stamp says it ran recently).
    """
    scratch = scratch_path(scope.home)
    if not scratch.is_dir():
        return 0
    now = clock.now().timestamp()
    stamp = scratch / PRUNE_STAMP
    try:
        last = stamp.stat().st_mtime
    except OSError:
        last = None
    if last is not None and 0 <= now - last < PRUNE_INTERVAL_SECONDS:
        return 0
    try:
        stamp.touch(mode=0o600)
        os.utime(stamp, (now, now))
    except OSError as exc:
        log.warning("scratch prune: cannot stamp %s: %s", stamp, exc)
        return 0
    cutoff = now - SCRATCH_IDLE_SECONDS
    removed = 0
    try:
        entries = [e for e in scratch.iterdir() if e.name != PRUNE_STAMP]
    except OSError:
        return 0
    for entry in entries:
        if _idle(entry, cutoff) and _remove(entry):
            removed += 1
    if removed:
        log.info("scratch prune: removed %d idle entr%s", removed, "y" if removed == 1 else "ies")
    return removed


def _os_temp_root(fallback: Path) -> Path:
    if sys.platform == "win32":
        return fallback
    root = Path("/tmp")  # the OS temp root; short enough for socket paths
    return root if root.is_dir() else fallback


def socket_dir(scope: Scope, *, temp_root: Path | None = None) -> Path:
    """Where to put AF_UNIX sockets: the scratch dir, or the OS temp root when that is too long."""
    scratch = scratch_path(scope.home)
    if len(str(scratch)) <= SOCKET_PATH_BUDGET:
        return scratch
    return temp_root if temp_root is not None else _os_temp_root(scratch)
