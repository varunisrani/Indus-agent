"""Whole-tree snapshots of the skills folder, taken before a consolidation pass (P09-19).

A snapshot is ``<home>/skills/.curator_backups/<id>/skills.tar.gz`` plus ``manifest.json``
``{id, reason, created_at, size, skill_files}``. The id is the UTC time
``YYYY-MM-DDTHH-MM-SSZ`` (``-NN`` added when that second is taken). Left out of every snapshot:
the backups themselves, the archive (rolling it back would lose archived skills), the skills hub
state, lock files, the ledger and its blobs, ``.git`` folders and regenerable caches. The usage
sidecar, the curator state and the bundled manifest are kept, so a rollback also brings back the
last-run time and provenance records.

Only the newest ``curator.backup.keep`` (2) snapshots stay. With ``curator.backup.enabled: false``
nothing is snapshotted and rollback is refused (it would have no safety snapshot).

Rollback: snapshot the current tree first ("before rollback to <id>", with the target protected
from that snapshot's prune), move the current entries aside into a staging folder, extract, and
on any failure put the staged entries back. Members with absolute paths or ``..`` are refused.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import tarfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from indus.foundation.fsio import atomic_write_json
from indus.foundation.scope import Scope
from indus.learning.curator.settings import CuratorSettings

__all__ = [
    "BACKUPS_DIR",
    "BackupInfo",
    "BackupsDisabled",
    "RollbackFailed",
    "list_backups",
    "prune_backups",
    "rollback",
    "snapshot",
]

log = logging.getLogger(__name__)

BACKUPS_DIR = ".curator_backups"
ARCHIVE_NAME = "skills.tar.gz"
MANIFEST = "manifest.json"
STAGING_PREFIX = ".staging-"
EXCLUDED = frozenset(
    {
        BACKUPS_DIR,
        ".archive",
        ".hub",
        ".locks",
        ".git",
        ".ledger.jsonl",
        ".ledger-blobs",
        ".usage.lock",
        ".curator_state.lock",
        ".bundled_manifest.lock",
    }
)
CACHE_DIRS = frozenset(
    {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules", "venv", ".venv"}
)


class BackupsDisabled(Exception):
    pass


class RollbackFailed(Exception):
    pass


@dataclass(frozen=True)
class BackupInfo:
    id: str
    path: Path
    reason: str
    created_at: str
    size: int
    skill_files: int


def _skills(scope: Scope) -> Path:
    return scope.home / "skills"


def _backups(scope: Scope) -> Path:
    return _skills(scope) / BACKUPS_DIR


def _settings(scope: Scope, settings: CuratorSettings | None) -> CuratorSettings:
    return settings if settings is not None else CuratorSettings.from_scope(scope)


def _base_id(now: datetime) -> str:
    return now.astimezone(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")


def _is_id(name: str) -> bool:
    try:
        datetime.strptime(name[:20], "%Y-%m-%dT%H-%M-%SZ")
    except ValueError:
        return False
    rest = name[20:]
    return rest == "" or (len(rest) == 3 and rest[0] == "-" and rest[1:].isdigit())


def _excluded(member: tarfile.TarInfo) -> bool:
    parts = Path(member.name).parts
    folders = parts if member.isdir() else parts[:-1]
    return any(p in EXCLUDED for p in parts) or any(p in CACHE_DIRS for p in folders)


def _count_skills(root: Path) -> int:
    count = 0
    for path in root.rglob("SKILL.md"):
        rel = path.relative_to(root).parts
        if not any(p in EXCLUDED or p in CACHE_DIRS for p in rel):
            count += 1
    return count


def _read_manifest(folder: Path) -> dict[str, Any]:
    try:
        data = json.loads((folder / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return cast(dict[str, Any], data) if isinstance(data, dict) else {}


def _restorable(folder: Path) -> bool:
    return folder.is_dir() and _is_id(folder.name) and (folder / ARCHIVE_NAME).is_file()


def list_backups(scope: Scope) -> list[BackupInfo]:
    """Restorable snapshots, newest first."""
    root = _backups(scope)
    if not root.is_dir():
        return []
    out: list[BackupInfo] = []
    for folder in sorted(root.iterdir(), key=lambda p: p.name, reverse=True):
        if not _restorable(folder):
            continue
        manifest = _read_manifest(folder)
        size = manifest.get("size")
        if not isinstance(size, int):
            size = (folder / ARCHIVE_NAME).stat().st_size
        out.append(
            BackupInfo(
                id=folder.name,
                path=folder,
                reason=str(manifest.get("reason") or "?"),
                created_at=str(manifest.get("created_at") or ""),
                size=size,
                skill_files=int(manifest.get("skill_files") or 0),
            )
        )
    return out


def prune_backups(
    scope: Scope, *, keep: int | None = None, protect: frozenset[str] = frozenset()
) -> list[str]:
    """Remove snapshots beyond the newest ``keep`` (and leftover staging folders)."""
    root = _backups(scope)
    if not root.is_dir():
        return []
    count = keep if keep is not None else CuratorSettings.from_scope(scope).backup_keep
    ordered = sorted(
        (p for p in root.iterdir() if _is_id(p.name)), key=lambda p: p.name, reverse=True
    )
    removed: list[str] = []
    for folder in ordered[max(count, 1) :]:
        if folder.name in protect:
            continue
        try:
            shutil.rmtree(folder)
            removed.append(folder.name)
        except OSError as exc:
            log.warning("old skills snapshot %s not removed: %s", folder, exc)
    for folder in root.iterdir():
        if folder.name.startswith(STAGING_PREFIX) and folder.is_dir():
            with contextlib.suppress(OSError):
                shutil.rmtree(folder)
    return removed


def snapshot(
    scope: Scope,
    reason: str = "manual",
    *,
    settings: CuratorSettings | None = None,
    protect: frozenset[str] = frozenset(),
) -> BackupInfo | None:
    """Take a snapshot; None when backups are off or the skills folder is missing.

    IO errors are raised to the caller (the curator logs them and goes on; the CLI reports them).
    """
    config = _settings(scope, settings)
    if not config.backup_enabled:
        log.debug("skill snapshots are switched off (curator.backup.enabled)")
        return None
    skills = _skills(scope)
    if not skills.is_dir():
        return None
    root = _backups(scope)
    root.mkdir(parents=True, exist_ok=True)
    base = _base_id(scope.clock.now())
    snap_id, n = base, 0
    while (root / snap_id).exists():
        n += 1
        snap_id = f"{base}-{n:02d}"
    folder = root / snap_id
    folder.mkdir()
    archive = folder / ARCHIVE_NAME
    try:
        with tarfile.open(archive, "w:gz", compresslevel=6) as tar:
            for entry in sorted(skills.iterdir()):
                if entry.name in EXCLUDED:
                    continue
                tar.add(
                    str(entry),
                    arcname=entry.name,
                    filter=lambda m: None if _excluded(m) else m,
                )
        manifest = {
            "id": snap_id,
            "reason": reason,
            "created_at": scope.clock.now().astimezone(UTC).isoformat(),
            "size": archive.stat().st_size,
            "skill_files": _count_skills(skills),
        }
        atomic_write_json(folder / MANIFEST, manifest, sort_keys=True)
    except (OSError, tarfile.TarError):
        shutil.rmtree(folder, ignore_errors=True)
        raise
    prune_backups(scope, keep=config.backup_keep, protect=protect | {snap_id})
    log.info("skills snapshot %s taken (%s)", snap_id, reason)
    return BackupInfo(
        snap_id, folder, reason, manifest["created_at"], manifest["size"], manifest["skill_files"]
    )


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def _carry_over(staged: Path, skills: Path) -> list[str]:
    """Move what snapshots leave out (a skill's ``.git``, caches, virtualenvs) from the folder
    before back into the restored tree, where its skill still exists; returns what failed."""
    failed: list[str] = []
    for current, dirs, files in os.walk(staged):
        here = Path(current)
        if here == staged:
            dirs[:] = [d for d in dirs if d not in EXCLUDED]
            continue
        carried = [here / f for f in files if f in EXCLUDED]
        carried += [here / d for d in dirs if d in EXCLUDED or d in CACHE_DIRS]
        for source in carried:
            dest = skills / source.relative_to(staged)
            if dest.parent.is_dir() and not dest.exists():
                try:
                    shutil.move(str(source), str(dest))
                except OSError:
                    failed.append(source.relative_to(staged).as_posix())
        dirs[:] = [d for d in dirs if d not in EXCLUDED and d not in CACHE_DIRS]
    return failed


def _check_members(tar: tarfile.TarFile) -> None:
    for member in tar.getmembers():
        parts = Path(member.name).parts
        if member.name.startswith(("/", "\\")) or ".." in parts:
            raise RollbackFailed(f"the snapshot holds an unsafe path {member.name!r}; not restored")


def rollback(
    scope: Scope, backup_id: str | None = None, *, settings: CuratorSettings | None = None
) -> tuple[BackupInfo, BackupInfo]:
    """Restore the skills folder from ``backup_id`` (default: the newest snapshot).

    Returns ``(restored, safety)``: the snapshot restored and the one taken of the tree before.
    Raises ``BackupsDisabled`` or ``RollbackFailed`` (the tree is then as it was).
    """
    config = _settings(scope, settings)
    if not config.backup_enabled:
        raise BackupsDisabled(
            "skill snapshots are switched off (curator.backup.enabled: false), so a rollback "
            "could not be undone; switch them on first"
        )
    available = list_backups(scope)
    if backup_id:
        target = next((b for b in available if b.id == backup_id), None)
    else:
        target = available[0] if available else None
    if target is None:
        named = f" named {backup_id!r}" if backup_id else ""
        raise RollbackFailed(f"no skills snapshot{named} (`indus curator rollback --list`)")
    archive = target.path / ARCHIVE_NAME
    try:
        with tarfile.open(archive, "r:gz") as tar:
            _check_members(tar)
    except tarfile.TarError as exc:
        raise RollbackFailed(f"snapshot {target.id} cannot be read: {exc}") from exc
    try:
        safety = snapshot(
            scope,
            f"before rollback to {target.id}",
            settings=config,
            protect=frozenset({target.id}),
        )
    except (OSError, tarfile.TarError) as exc:
        raise RollbackFailed(f"the safety snapshot failed ({exc}); nothing was changed") from exc
    if safety is None:
        raise RollbackFailed("the safety snapshot could not be taken; nothing was changed")
    skills = _skills(scope)
    staging = _backups(scope) / f"{STAGING_PREFIX}{_base_id(scope.clock.now())}"
    n = 0
    while staging.exists():
        n += 1
        staging = staging.with_name(f"{STAGING_PREFIX}{_base_id(scope.clock.now())}-{n}")
    staging.mkdir(parents=True)
    moved: list[tuple[Path, Path]] = []

    def put_back() -> list[str]:
        failed: list[str] = []
        for original, staged in moved:
            try:
                _remove(original)
                shutil.move(str(staged), str(original))
            except OSError:
                failed.append(original.name)
        return failed

    try:
        for entry in list(skills.iterdir()):
            if entry.name in EXCLUDED:
                continue
            dest = staging / entry.name
            shutil.move(str(entry), str(dest))
            moved.append((entry, dest))
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(str(skills), filter="data")
    except (OSError, tarfile.TarError) as exc:
        keep = EXCLUDED | {original.name for original, _ in moved}
        for entry in list(skills.iterdir()):
            if entry.name not in keep:
                with contextlib.suppress(OSError):
                    _remove(entry)
        failed = put_back()
        if failed:
            raise RollbackFailed(
                f"rollback failed ({exc}) and {', '.join(failed)} could not be put back; "
                f"the copies are in {staging}"
            ) from exc
        shutil.rmtree(staging, ignore_errors=True)
        raise RollbackFailed(f"rollback failed ({exc}); the skills folder is as it was") from exc
    left = _carry_over(staging, skills)
    if left:
        raise RollbackFailed(
            f"restored from {target.id}, but {', '.join(left)} could not be carried over from the "
            f"folder before; the copies are in {staging}"
        )
    shutil.rmtree(staging, ignore_errors=True)
    with contextlib.suppress(Exception):
        from indus.learning.skills.resolver import clear_scan_cache
        from indus.learning.skills.view_tools import clear_list_cache

        clear_scan_cache()
        clear_list_cache(scope)
    log.info("skills folder restored from snapshot %s", target.id)
    return target, safety
