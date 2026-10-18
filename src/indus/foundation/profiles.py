"""Profile ids, live-profile markers, tombstones and the sticky active profile.

Layout: the root itself is the profile ``default``; a named profile lives at
``<root>/profiles/<id>/``. A named dir is a *live* profile only when it carries an identity marker
and has no tombstone. Tombstones sit beside the profile dirs at ``<root>/profiles/.deleted/<id>``,
so removing a profile dir (or a stray ``mkdir`` recreating it) never erases them.

Stdlib-only apart from ``foundation.home`` and ``foundation.errors``: ``foundation.preparse``
imports this module before any app code.
"""

from __future__ import annotations

import logging
import re
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from indus.foundation.errors import InvalidProfileId, ProfileNotFound, ProfileTombstoned
from indus.foundation.home import PROFILES_DIR, secure_dir

log = logging.getLogger(__name__)

DEFAULT_PROFILE = "default"
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
# Names that may not be used for a named profile dir. ``default`` is valid as an id (it means the
# root) but never as ``profiles/default``.
RESERVED_IDS = frozenset({"indus", "default", "test", "tmp", "root", "sudo"})
IDENTITY_MARKERS = ("config.yaml", ".env", "PERSONA.md", "profile.yaml", "auth.json", "state.db")
TOMBSTONE_DIR = ".deleted"
ACTIVE_FILE = "active_profile"
FALLBACK_SUGGESTION = "my-work"
LIST_HINT = "run `indus profile list` to see the profiles that exist"


class _HasNow(Protocol):
    def now(self) -> datetime: ...


# -- ids ----------------------------------------------------------------------------------------


def normalize_id(value: object) -> str:
    """Canonical profile id: trimmed, lowercase, ``Default`` → ``default``; validated."""
    if not isinstance(value, str):
        raise InvalidProfileId(
            f"a profile id must be text, got {type(value).__name__}",
            hint="pass the profile's name, not a number",
        )
    text = value.strip().lower()
    if text == DEFAULT_PROFILE:
        return text
    if not ID_PATTERN.fullmatch(text):
        suggestion = suggest_id(value)
        raise InvalidProfileId(
            f"{value!r} is not a valid profile id: use 1-64 lowercase letters, digits, '-' or '_', "
            "starting with a letter or digit",
            hint=f"for example: {suggestion}",
        )
    if text in RESERVED_IDS:
        raise InvalidProfileId(f"{text!r} is a reserved name and cannot be a profile id")
    return text


def is_valid_id(value: object) -> bool:
    try:
        normalize_id(value)
    except InvalidProfileId:
        return False
    return True


def suggest_id(text: str) -> str:
    """A valid id derived from free text (``My Work`` → ``my-work``), else ``my-work``."""
    candidate = "-".join(text.strip().lower().split())
    candidate = re.sub(r"[^a-z0-9_-]", "", candidate).lstrip("-_")[:64]
    if ID_PATTERN.fullmatch(candidate) and candidate not in RESERVED_IDS:
        return candidate
    return FALLBACK_SUGGESTION


# -- paths --------------------------------------------------------------------------------------


def profiles_dir(root: Path) -> Path:
    return root / PROFILES_DIR


def profile_home(root: Path, profile_id: str) -> Path:
    pid = normalize_id(profile_id)
    return root if pid == DEFAULT_PROFILE else profiles_dir(root) / pid


def named_profile_of(home: Path) -> tuple[Path, str] | None:
    """``(root, id)`` when ``home`` has the ``<root>/profiles/<id>`` shape, else None."""
    if home.parent.name == PROFILES_DIR and home.name and not home.name.startswith("."):
        return home.parent.parent, home.name
    return None


def profile_id_of(home: Path) -> str:
    named = named_profile_of(home)
    return named[1] if named is not None else DEFAULT_PROFILE


def tombstone_path(root: Path, profile_id: str) -> Path:
    return profiles_dir(root) / TOMBSTONE_DIR / profile_id


# -- markers and liveness -----------------------------------------------------------------------


def has_identity(home: Path) -> bool:
    """Any marker present; a dangling symlinked marker counts."""
    return any((home / m).is_file() or (home / m).is_symlink() for m in IDENTITY_MARKERS)


def servable(home: Path) -> bool:
    """Stricter than ``has_identity``: an empty ``.env`` alone (a crashed create) does not count."""

    def claims(marker: str) -> bool:
        candidate = home / marker
        if candidate.is_symlink():
            return True
        try:
            info = candidate.stat()
        except OSError:
            return False
        if not stat.S_ISREG(info.st_mode):
            return False
        return marker != ".env" or info.st_size > 0

    return any(claims(marker) for marker in IDENTITY_MARKERS)


def is_tombstoned(root: Path, profile_id: str) -> bool:
    return tombstone_path(root, profile_id).exists()


def is_live(root: Path, profile_id: str) -> bool:
    pid = normalize_id(profile_id)
    if pid == DEFAULT_PROFILE:
        return True
    home = profiles_dir(root) / pid
    return home.is_dir() and has_identity(home) and not is_tombstoned(root, pid)


def list_profiles(root: Path) -> list[str]:
    """Live profile ids, ``default`` first, the rest sorted."""
    named: list[str] = []
    try:
        entries = list(profiles_dir(root).iterdir())
    except OSError:
        entries = []
    for entry in entries:
        name = entry.name
        if name.startswith(".") or not ID_PATTERN.fullmatch(name) or name in RESERVED_IDS:
            continue
        if is_live(root, name):
            named.append(name)
    return [DEFAULT_PROFILE, *sorted(named)]


def tombstone(root: Path, profile_id: str, *, clock: _HasNow | None = None) -> Path:
    """Record that ``profile_id`` was deleted (the delete command itself is P07)."""
    pid = normalize_id(profile_id)
    if pid == DEFAULT_PROFILE:
        raise InvalidProfileId("the default profile cannot be deleted")
    stamp = (clock.now() if clock is not None else datetime.now(UTC)).isoformat()
    path = tombstone_path(root, pid)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_text_atomic(path, stamp + "\n")
    return path


def clear_tombstone(root: Path, profile_id: str) -> None:
    tombstone_path(root, normalize_id(profile_id)).unlink(missing_ok=True)


def assert_profile_home_live(home: Path) -> None:
    """Refuse a named profile home that is tombstoned or missing; other homes pass."""
    named = named_profile_of(home)
    if named is None:
        return
    root, pid = named
    if is_tombstoned(root, pid):
        raise ProfileTombstoned(
            f"profile {pid!r} was deleted; it will not be recreated", hint=LIST_HINT
        )
    if not home.is_dir():
        raise ProfileNotFound(f"profile {pid!r} does not exist", hint=LIST_HINT)


def mkdir_in_profile(home: Path, rel: str | Path) -> Path:
    """Create ``home/rel``, but never inside a deleted or missing named profile."""
    assert_profile_home_live(home)
    target = home / rel
    secure_dir(target, home=home)
    return target


# -- sticky active profile ----------------------------------------------------------------------


def read_active(root: Path) -> str:
    """The sticky profile from ``<root>/active_profile``; missing, empty or invalid → default."""
    try:
        text = (root / ACTIVE_FILE).read_bytes().decode("utf-8-sig").strip()
    except (OSError, UnicodeDecodeError):
        return DEFAULT_PROFILE
    if not text:
        return DEFAULT_PROFILE
    try:
        return normalize_id(text)
    except InvalidProfileId:
        log.warning("ignoring %s: %r is not a valid profile id", root / ACTIVE_FILE, text)
        return DEFAULT_PROFILE


def write_active(root: Path, profile_id: str) -> None:
    """Make ``profile_id`` sticky; ``default`` removes the file."""
    pid = normalize_id(profile_id)
    path = root / ACTIVE_FILE
    if pid == DEFAULT_PROFILE:
        path.unlink(missing_ok=True)
        return
    if not is_live(root, pid):
        raise ProfileNotFound(f"profile {pid!r} does not exist", hint=LIST_HINT)
    _write_text_atomic(path, pid + "\n")


def _write_text_atomic(path: Path, text: str) -> None:
    """Owner-only atomic write through ``fsio``."""
    # Imported here, not at the top: ``preparse`` imports this module and must stay light.
    from indus.foundation.fsio import atomic_write_text

    atomic_write_text(path, text, mode=0o600)
