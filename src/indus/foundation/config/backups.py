"""Point-in-time copies of ``config.yaml`` under ``<home>/backups/config/``.

Names are ``config.yaml.<reason>.<YYYYMMDD-HHMMSS>``. A copy is skipped when the source is missing,
empty, or byte-identical to the newest copy for the same reason; the newest five per reason are
kept. A failed backup is logged and never blocks the write it precedes.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Protocol

from indus.foundation import fsio

log = logging.getLogger(__name__)

REASONS = frozenset({"good", "pre-write", "corrupt", "pre-migrate"})
DEFAULT_KEEP = 5
_SUBDIR = ("backups", "config")


class NowClock(Protocol):
    def now(self) -> datetime: ...


def backups_dir(config_path: Path) -> Path:
    return config_path.parent.joinpath(*_SUBDIR)


def list_backups(config_path: Path, reason: str | None = None) -> list[Path]:
    """Existing copies, newest first, optionally for one reason."""
    root = backups_dir(config_path)
    if not root.is_dir():
        return []
    prefix = f"{config_path.name}.{reason}." if reason else f"{config_path.name}."
    found = [
        p
        for p in root.iterdir()
        if p.name.startswith(prefix) and p.is_file() and not p.is_symlink()
    ]
    return sorted(found, key=lambda p: p.name, reverse=True)


def backup_config(
    config_path: Path, reason: str, *, clock: NowClock, keep: int = DEFAULT_KEEP
) -> Path | None:
    """Copy ``config_path`` for ``reason``; return the new copy, or None when skipped or failed."""
    if reason not in REASONS:
        raise ValueError(f"unknown config backup reason {reason!r}")
    try:
        if config_path.is_symlink() or not config_path.is_file():
            return None
        data = config_path.read_bytes()
        if not data:
            return None
        existing = list_backups(config_path, reason)
        if existing and existing[0].read_bytes() == data:
            return None
        root = backups_dir(config_path)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        stamp = clock.now().strftime("%Y%m%d-%H%M%S")
        target = root / f"{config_path.name}.{reason}.{stamp}"
        if target.is_symlink():
            log.warning("refusing to write config backup %s: it is a symlink", target)
            return None
        if target.exists():
            log.debug("config backup %s already exists; not overwriting it", target)
            return None
        fsio.atomic_write_bytes(target, data, mode=0o600, preserve_mode=False)
        for stale in [target, *existing][keep:]:
            stale.unlink(missing_ok=True)
        return target
    except OSError as exc:
        log.warning("could not back up %s (%s): %s", config_path, reason, exc)
        return None


def newest_good_bytes(config_path: Path) -> bytes | None:
    """Contents of the newest ``good`` copy (the file as it was at the last successful parse)."""
    newest = list_backups(config_path, "good")[:1]
    if not newest:
        return None
    try:
        return newest[0].read_bytes()
    except OSError as exc:
        log.warning("could not read config backup %s: %s", newest[0], exc)
        return None
