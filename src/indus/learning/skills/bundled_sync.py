"""Seed the skills we ship into a profile and keep them current without touching user edits.

The repo's ``skills/<category>/<name>/`` packages are copied into ``<home>/skills/`` with the same
relative path. ``<home>/skills/.bundled_manifest`` remembers, one ``name:sha256`` line per skill,
the hash of each package as we copied it (its *origin*). Every later sync classifies a shipped
skill by that record alone:

- no row: new. Copied and recorded, unless a local skill already sits at that path; then it is
  kept, and recorded only when it is byte-identical to what we ship.
- row, no copy on disk: the user deleted it. It stays deleted.
- row, copy equals the origin, shipped package changed: refreshed (copy-new-then-swap).
- row, copy differs from the origin: the user edited it. Never touched again.

A row for a skill we no longer ship is kept while a live or archived copy exists (it is the only
record that the copy came from us) and dropped once neither does. Generated caches are neither
hashed nor copied. ``<home>/.no-bundled-skills`` stops every seeding path. Nothing here prompts:
confirmations belong to the CLI.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import shutil
import stat
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from indus.foundation.fsio import FileLock, atomic_write_text

__all__ = [
    "ARCHIVE_DIR",
    "CACHE_DIRS",
    "MANIFEST_NAME",
    "OPT_OUT_MARKER",
    "SOURCE_ENV",
    "BundledSkill",
    "PruneReport",
    "ResetResult",
    "SyncReport",
    "bundled_source",
    "discover",
    "is_opted_out",
    "list_modified",
    "manifest_path",
    "opt_in",
    "read_manifest",
    "remove_pristine",
    "reset",
    "set_opt_out",
    "sync_bundled",
    "tree_hash",
]

log = logging.getLogger(__name__)

SKILL_FILE = "SKILL.md"
SKILLS_DIR = "skills"
MANIFEST_NAME = ".bundled_manifest"
LOCK_NAME = ".bundled_manifest.lock"
OPT_OUT_MARKER = ".no-bundled-skills"
ARCHIVE_DIR = ".archive"
SOURCE_ENV = "INDUS_BUNDLED_SKILLS"
CACHE_DIRS = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"})
CACHE_SUFFIXES = frozenset({".pyc"})
_STAGED = ".sync-new"
_RETIRED = ".sync-old"

_OPT_OUT_TEXT = """\
Bundled skills are switched off for this profile: install, update and `indus skills sync` will
not copy the skills that ship with Indus Agent into it.
Run `indus skills opt-in` (or delete this file) to switch them back on.
"""


# -- where the shipped skills live ---------------------------------------------------------------


def bundled_source(env: Mapping[str, str] | None = None) -> Path:
    """The shipped skills tree: ``INDUS_BUNDLED_SKILLS``, the packaged copy, else the checkout."""
    override = (env or {}).get(SOURCE_ENV, "").strip()
    if override:
        return Path(override)
    here = Path(__file__).resolve()
    packaged = here.parents[2] / "_bundled_skills"
    if packaged.is_dir():
        return packaged
    return here.parents[4] / SKILLS_DIR


@dataclass(frozen=True)
class BundledSkill:
    name: str
    rel: Path
    """Path of the package relative to the source root, e.g. ``data/data-wrangling``."""
    path: Path


def _is_cache_name(name: str) -> bool:
    return name in CACHE_DIRS or Path(name).suffix in CACHE_SUFFIXES


def _skill_dirs(root: Path) -> Iterator[Path]:
    """Every package dir (holding ``SKILL.md``) under ``root``, skipping hidden and cache dirs.

    A package's own subfolders are not searched for more packages.
    """
    if not root.is_dir():
        return
    for current, dirs, files in os.walk(root):
        if SKILL_FILE in files and Path(current) != root:
            dirs[:] = []
            yield Path(current)
            continue
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and not _is_cache_name(d))


def discover(source: Path) -> list[BundledSkill]:
    """The shipped packages, sorted by path; a repeated name keeps its first package."""
    seen: dict[str, BundledSkill] = {}
    for found in sorted(_skill_dirs(source)):
        name = found.name
        if name in seen:
            log.warning("bundled skill %r shipped twice; keeping %s", name, seen[name].rel)
            continue
        seen[name] = BundledSkill(name, found.relative_to(source), found)
    return sorted(seen.values(), key=lambda s: s.rel.as_posix())


# -- hashing and copying -------------------------------------------------------------------------


def _package_files(directory: Path) -> list[tuple[str, Path]]:
    out: list[tuple[str, Path]] = []
    for current, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if d not in CACHE_DIRS]
        for name in files:
            if _is_cache_name(name):
                continue
            path = Path(current) / name
            out.append((path.relative_to(directory).as_posix(), path))
    out.sort()
    return out


def tree_hash(directory: Path) -> str:
    """SHA-256 over each file's relative path, size and bytes, in path order (caches skipped)."""
    digest = hashlib.sha256()
    for rel, path in _package_files(directory):
        data = path.read_bytes()
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(data)).encode("ascii"))
        digest.update(b"\0")
        digest.update(data)
    return digest.hexdigest()


def _ignore_caches(_directory: str, names: list[str]) -> set[str]:
    return {n for n in names if _is_cache_name(n)}


def _copy_package(source: Path, target: Path) -> None:
    """Copy one package; every copied entry gets owner write so later syncs can replace it."""
    shutil.copytree(source, target, ignore=_ignore_caches)
    for current, _dirs, files in os.walk(target):
        for entry in (current, *(os.path.join(current, f) for f in files)):
            mode = os.stat(entry).st_mode
            if not mode & stat.S_IWUSR:
                os.chmod(entry, stat.S_IMODE(mode) | stat.S_IWUSR)


def _remove_tree(path: Path, skills_root: Path) -> None:
    """Delete ``path``, which must sit strictly inside ``skills_root``."""
    target = path.resolve() if not path.is_symlink() else path.parent.resolve() / path.name
    root = skills_root.resolve()
    if root not in target.parents:
        raise ValueError(f"refusing to delete {target}: it is not inside {root}")
    if path.is_symlink() or path.is_file():
        path.unlink()
        return

    def unlock(func: Callable[..., object], failed: str, _exc: BaseException) -> None:
        for entry in (os.path.dirname(failed), failed):
            with contextlib.suppress(OSError):
                os.chmod(entry, stat.S_IRWXU)
        func(failed)

    shutil.rmtree(path, onexc=unlock)


def _sibling(dest: Path, suffix: str) -> Path:
    return dest.parent / f".{dest.name}{suffix}"


def _clear(path: Path, skills_root: Path) -> None:
    if path.exists() or path.is_symlink():
        _remove_tree(path, skills_root)


def _install(source: Path, dest: Path, skills_root: Path) -> None:
    """Copy ``source`` to a hidden sibling first, then rename it into place."""
    staged = _sibling(dest, _STAGED)
    _clear(staged, skills_root)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        _copy_package(source, staged)
        os.rename(staged, dest)
    except BaseException:
        if staged.exists():
            _remove_tree(staged, skills_root)
        raise


def _replace(source: Path, dest: Path, skills_root: Path) -> None:
    """Swap ``dest`` for a fresh copy; the old copy waits in a hidden sibling until the swap."""
    staged = _sibling(dest, _STAGED)
    retired = _sibling(dest, _RETIRED)
    _clear(staged, skills_root)
    _clear(retired, skills_root)
    try:
        _copy_package(source, staged)
    except BaseException:
        if staged.exists():
            _remove_tree(staged, skills_root)
        raise
    os.rename(dest, retired)
    try:
        os.rename(staged, dest)
    except BaseException:
        os.rename(retired, dest)
        _remove_tree(staged, skills_root)
        raise
    try:
        _remove_tree(retired, skills_root)
    except OSError:
        log.debug("could not remove the old copy %s", retired, exc_info=True)


def _recover(dest: Path, skills_root: Path) -> None:
    """Undo what an interrupted install or swap left behind, before ``dest`` is classified."""
    retired = _sibling(dest, _RETIRED)
    if retired.is_dir():
        if dest.exists():
            _remove_tree(retired, skills_root)
        else:
            os.rename(retired, dest)
            log.info("recovered %s from an interrupted skill refresh", dest)
    _clear(_sibling(dest, _STAGED), skills_root)


# -- manifest and marker -------------------------------------------------------------------------


def manifest_path(home: Path) -> Path:
    return home / SKILLS_DIR / MANIFEST_NAME


def read_manifest(home: Path) -> dict[str, str]:
    """``{name: origin sha256}``; lines without a ``:`` are ignored."""
    try:
        text = manifest_path(home).read_text(encoding="utf-8-sig")
    except OSError:
        return {}
    rows: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        name, sep, digest = line.partition(":")
        if not sep or not name.strip() or not digest.strip():
            if line:
                log.debug("ignoring malformed bundled manifest line %r", line)
            continue
        rows[name.strip()] = digest.strip()
    return rows


def _write_manifest(home: Path, rows: Mapping[str, str]) -> None:
    text = "".join(f"{name}:{rows[name]}\n" for name in sorted(rows))
    atomic_write_text(manifest_path(home), text)


def _lock(home: Path) -> FileLock:
    return FileLock(home / SKILLS_DIR / LOCK_NAME)


def is_opted_out(home: Path) -> bool:
    return (home / OPT_OUT_MARKER).exists()


def set_opt_out(home: Path, enabled: bool) -> bool:
    """Write or remove the opt-out marker; True when that changed anything."""
    marker = home / OPT_OUT_MARKER
    present = marker.exists()
    if enabled and not present:
        home.mkdir(parents=True, exist_ok=True)
        atomic_write_text(marker, _OPT_OUT_TEXT)
    elif not enabled and present:
        marker.unlink()
    return enabled != present


def _live_copies(skills_root: Path) -> dict[str, list[Path]]:
    """Live packages in the profile by directory name (hidden dirs such as the archive skipped)."""
    found: dict[str, list[Path]] = {}
    for path in _skill_dirs(skills_root):
        found.setdefault(path.name, []).append(path)
    return found


def _archived(skills_root: Path, name: str) -> bool:
    return (skills_root / ARCHIVE_DIR / name).is_dir()


# -- sync ----------------------------------------------------------------------------------------


@dataclass
class SyncReport:
    added: list[str] = field(default_factory=list[str])
    updated: list[str] = field(default_factory=list[str])
    unchanged: list[str] = field(default_factory=list[str])
    user_modified: list[str] = field(default_factory=list[str])
    """Edited by the user since we copied them: kept, never refreshed."""
    kept_local: list[str] = field(default_factory=list[str])
    """A different local skill already used the path: kept, not tracked."""
    left_deleted: list[str] = field(default_factory=list[str])
    cleaned: list[str] = field(default_factory=list[str])
    """Rows dropped for skills we no longer ship and the profile no longer holds."""
    failed: list[str] = field(default_factory=list[str])
    opted_out: bool = False
    total: int = 0

    def summary(self) -> str:
        if self.opted_out:
            return "Bundled skills are switched off for this profile; nothing was copied."
        parts = [
            f"{len(self.added)} added",
            f"{len(self.updated)} updated",
            f"{len(self.unchanged)} unchanged",
        ]
        if self.user_modified:
            parts.append(f"{len(self.user_modified)} kept with your edits")
        if self.kept_local:
            parts.append(f"{len(self.kept_local)} kept (a local skill uses the name)")
        if self.left_deleted:
            parts.append(f"{len(self.left_deleted)} left deleted")
        if self.failed:
            parts.append(f"{len(self.failed)} failed")
        return f"Bundled skills: {', '.join(parts)} ({self.total} shipped)."


def _sync_one(
    skill: BundledSkill, rows: dict[str, str], skills_root: Path, report: SyncReport
) -> None:
    dest = skills_root / skill.rel
    _recover(dest, skills_root)
    shipped = tree_hash(skill.path)
    origin = rows.get(skill.name)
    if origin is None:
        if dest.exists() or dest.is_symlink():
            if dest.is_dir() and tree_hash(dest) == shipped:
                rows[skill.name] = shipped
                report.unchanged.append(skill.name)
            else:
                report.kept_local.append(skill.name)
            return
        _install(skill.path, dest, skills_root)
        rows[skill.name] = shipped
        report.added.append(skill.name)
    elif not dest.exists():
        report.left_deleted.append(skill.name)
    elif origin == shipped:
        report.unchanged.append(skill.name)
    elif not dest.is_dir() or tree_hash(dest) != origin:
        report.user_modified.append(skill.name)
    else:
        _replace(skill.path, dest, skills_root)
        rows[skill.name] = shipped
        report.updated.append(skill.name)


def sync_bundled(
    home: Path, *, source: Path | None = None, env: Mapping[str, str] | None = None
) -> SyncReport:
    """Seed or refresh the shipped skills in ``home`` (see the module notes)."""
    if is_opted_out(home):
        return SyncReport(opted_out=True)
    root = source if source is not None else bundled_source(env)
    if not root.is_dir():
        # A broken install must not read as "every skill dropped upstream" and clean the rows
        # that keep user-deleted skills deleted.
        return SyncReport(failed=[f"the shipped skills folder {root} is missing"])
    shipped = discover(root)
    skills_root = home / SKILLS_DIR
    skills_root.mkdir(parents=True, exist_ok=True)
    report = SyncReport(total=len(shipped))
    with _lock(home):
        rows = read_manifest(home)
        for skill in shipped:
            try:
                _sync_one(skill, rows, skills_root, report)
            except (OSError, ValueError) as exc:
                log.warning("bundled skill %s not synced: %s", skill.name, exc)
                report.failed.append(f"{skill.name}: {exc}")
        dropped = sorted(set(rows) - {s.name for s in shipped})
        if dropped:
            live = _live_copies(skills_root)
            for name in dropped:
                if name not in live and not _archived(skills_root, name):
                    del rows[name]
                    report.cleaned.append(name)
        _write_manifest(home, rows)
    return report


# -- maintenance ---------------------------------------------------------------------------------


def _single_copy(live: Mapping[str, list[Path]], name: str) -> Path | None:
    copies = live.get(name, [])
    return copies[0] if len(copies) == 1 else None


def list_modified(home: Path) -> list[str]:
    """Tracked skills whose copy no longer matches what we copied (the user edited them)."""
    rows = read_manifest(home)
    live = _live_copies(home / SKILLS_DIR)
    out: list[str] = []
    for name in sorted(rows):
        copy = _single_copy(live, name)
        if copy is not None and tree_hash(copy) != rows[name]:
            out.append(name)
    return out


@dataclass
class PruneReport:
    removed: list[str] = field(default_factory=list[str])
    kept: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    """``(name, reason)`` for every tracked skill that was not removed."""
    dry_run: bool = False


def remove_pristine(home: Path, *, dry_run: bool = False) -> PruneReport:
    """Delete tracked skills whose copy is still exactly what we copied, and their rows.

    Edited copies, ambiguous names and failures are kept and listed with a reason. Used by
    ``indus skills opt-out --remove`` after the CLI has confirmed.
    """
    skills_root = home / SKILLS_DIR
    report = PruneReport(dry_run=dry_run)
    if not skills_root.is_dir():
        return report
    with _lock(home):
        rows = read_manifest(home)
        live = _live_copies(skills_root)
        for name in sorted(rows):
            copies = live.get(name, [])
            if not copies:
                if not dry_run:
                    del rows[name]
                continue
            if len(copies) > 1:
                report.kept.append((name, "more than one skill uses this name"))
                continue
            if tree_hash(copies[0]) != rows[name]:
                report.kept.append((name, "edited since it was copied"))
                continue
            if not dry_run:
                try:
                    _remove_tree(copies[0], skills_root)
                except (OSError, ValueError) as exc:
                    report.kept.append((name, f"could not delete: {exc}"))
                    continue
                del rows[name]
            report.removed.append(name)
        if not dry_run:
            _write_manifest(home, rows)
    return report


def opt_in(
    home: Path,
    *,
    seed: bool = False,
    source: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> SyncReport | None:
    """Remove the opt-out marker; with ``seed``, sync straight away and return the report."""
    set_opt_out(home, False)
    return sync_bundled(home, source=source, env=env) if seed else None


@dataclass(frozen=True)
class ResetResult:
    ok: bool
    action: str
    """``untracked``, ``restored``, ``not_tracked``, ``not_shipped`` or ``failed``."""
    message: str


def reset(
    home: Path,
    name: str,
    *,
    restore: bool = False,
    source: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> ResetResult:
    """Forget one skill's origin row; with ``restore``, replace the copy with the shipped one.

    Without ``restore`` the copy stays: the next sync re-tracks it when it is identical to what
    we ship and otherwise treats it as the user's own skill. ``restore`` removes the copy first
    (nothing changes if that fails), then copies the shipped package and records it, even when the
    profile opted out, because the user asked for this one skill.
    """
    skills_root = home / SKILLS_DIR
    shipped = {s.name: s for s in discover(source if source is not None else bundled_source(env))}
    skills_root.mkdir(parents=True, exist_ok=True)
    with _lock(home):
        rows = read_manifest(home)
        if name not in rows and name not in shipped:
            return ResetResult(False, "not_tracked", f"'{name}' is not a bundled skill.")
        if not restore:
            if name not in rows:
                return ResetResult(
                    True, "untracked", f"'{name}' was not tracked; nothing to reset."
                )
            del rows[name]
            _write_manifest(home, rows)
            return ResetResult(
                True,
                "untracked",
                f"'{name}' is no longer tracked as a bundled skill. The next sync tracks it again "
                "if it matches the shipped version; otherwise it counts as your own skill. Use "
                "--restore to get the shipped version back.",
            )
        skill = shipped.get(name)
        if skill is None:
            return ResetResult(
                False,
                "not_shipped",
                f"'{name}' is no longer shipped, so it cannot be restored; its row was kept.",
            )
        dest = skills_root / skill.rel
        try:
            _recover(dest, skills_root)
            _clear(dest, skills_root)
        except (OSError, ValueError) as exc:
            return ResetResult(False, "failed", f"Could not remove {dest}: {exc}. Nothing changed.")
        rows.pop(name, None)
        try:
            _install(skill.path, dest, skills_root)
        except OSError as exc:
            _write_manifest(home, rows)
            return ResetResult(
                False, "failed", f"Removed {dest} but could not copy the shipped version: {exc}"
            )
        rows[name] = tree_hash(skill.path)
        _write_manifest(home, rows)
    return ResetResult(True, "restored", f"Restored the shipped version of '{name}'.")
