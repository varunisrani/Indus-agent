"""``indus backup``: a zip of the whole root home, or a quick snapshot of one profile (P07-22).

Full backup: the files ``backup_rules.iter_backup_files`` keeps, databases copied through SQLite's
online backup API, written to a hidden ``.partial`` file beside the output and renamed when it is
complete. A file that could not be read leaves the archive as ``<name>.incomplete.zip`` (kept, so
the rest can still be restored) and the exit code 1. One backup at a time: the root's
``.backup.lock`` is waited for 0.25 s, then exit 2. ``--keep N`` (default 3, 0 = keep all) removes
older ``indus-backup-*.zip`` files in the output folder after a complete run; other names are never
touched.

Secrets in the OS keychain are not exported (D-16.79): the run names them so the person can
re-enter them after a restore. File-held secrets (``.env``, ``secrets.json``) are archived like
any other file and come back owner-only.

``--quick [--label]`` copies this profile's critical files into
``<home>/state-snapshots/<UTC stamp>[-label]/`` (the newest 20 are kept; files over 1 GiB are
skipped with a note).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import zipfile
from collections.abc import Sequence
from datetime import UTC
from pathlib import Path, PurePosixPath
from typing import Any

import click

from indus.cli.backup_rules import copy_database, is_sqlite_file, iter_backup_files
from indus.cli.cmdkit import cli_state, err, human_size
from indus.cli.version import install_dir
from indus.foundation.fsio import FileLock, LockTimeout
from indus.foundation.home import PROFILES_DIR, secure_dir
from indus.foundation.scope import Scope
from indus.tools.builtin.paths import expand_tilde, user_home

__all__ = [
    "INCOMPLETE_SUFFIX",
    "LOCK_NAME",
    "LOCK_WAIT_SECONDS",
    "QUICK_DIR",
    "QUICK_KEEP",
    "QUICK_MAX_BYTES",
    "QUICK_PATHS",
    "RUN_PREFIX",
    "backup_command",
    "keychain_only_names",
]

RUN_PREFIX = "indus-backup-"
INCOMPLETE_SUFFIX = ".incomplete.zip"
LOCK_NAME = ".backup.lock"
LOCK_WAIT_SECONDS = 0.25
QUICK_DIR = "state-snapshots"
QUICK_KEEP = 20
QUICK_MAX_BYTES = 1 << 30
QUICK_PATHS = (
    "state.db",
    "config.yaml",
    ".env",
    "auth.json",
    "secrets.json",
    "secrets.index.json",
    "PERSONA.md",
    "cron/jobs.json",
    "memories",
)
_LABEL = re.compile(r"[^A-Za-z0-9._-]+")


class BackupBusy(Exception):
    pass


@contextlib.contextmanager
def _backup_slot(root: Path) -> Any:
    lock = FileLock(root / LOCK_NAME, timeout=LOCK_WAIT_SECONDS)
    try:
        lock.acquire()
    except LockTimeout as exc:
        who = f" (process {exc.holder_pid})" if getattr(exc, "holder_pid", None) else ""
        raise BackupBusy(
            f"Another backup is running{who}; try again when it has finished."
        ) from exc
    try:
        yield
    finally:
        lock.release()


# -- keychain names -------------------------------------------------------------------------------


def _json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _profile_homes(root: Path) -> list[tuple[str, Path]]:
    homes = [("default", root)]
    folder = root / PROFILES_DIR
    if folder.is_dir():
        homes += [(p.name, p) for p in sorted(folder.iterdir()) if p.is_dir()]
    return homes


def keychain_only_names(root: Path) -> list[tuple[str, list[str]]]:
    """``(profile, names)`` of secrets the index lists but no file in the home holds."""
    out: list[tuple[str, list[str]]] = []
    for profile, home in _profile_homes(root):
        listed = _json(home / "secrets.index.json").get("names")
        if not isinstance(listed, list):
            continue
        held = _json(home / "secrets.json").get("secrets")
        in_file = set(held) if isinstance(held, dict) else set()
        names = sorted(str(n) for n in listed if str(n) not in in_file)
        if names:
            out.append((profile, names))
    return out


def _warn_keychain(root: Path) -> None:
    found = keychain_only_names(root)
    if not found:
        return
    click.echo("")
    click.echo("Not in the backup (kept in the OS keychain, which is never exported):")
    for profile, names in found:
        click.echo(f"  profile {profile}: {', '.join(names)}")
    click.echo(
        "  After restoring, enter them again with `indus auth add` or `indus config set NAME "
        "VALUE`, or copy them out of the keychain yourself."
    )


# -- full backup ----------------------------------------------------------------------------------


def _output_path(scope: Scope, output: str | None) -> Path:
    stamp = scope.clock.now().strftime("%Y-%m-%d-%H%M%S")
    default_name = f"{RUN_PREFIX}{stamp}.zip"
    if output:
        path = Path(expand_tilde(output, scope))
        if path.is_dir() or output.endswith(("/", os.sep)):
            path = path / default_name
    else:
        path = Path(user_home(scope)) / default_name  # the scope's HOME, never the process's
    if path.suffix.lower() != ".zip":
        path = path.with_name(path.name + ".zip")
    path = Path(os.path.abspath(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _add_file(zf: zipfile.ZipFile, source: Path, rel: PurePosixPath, staging: Path) -> int:
    """Add one member; databases go through a backup-API snapshot first. Returns its size."""
    if is_sqlite_file(source):
        fd, name = tempfile.mkstemp(dir=staging, prefix=".db-copy-", suffix=".partial")
        os.close(fd)
        copy = Path(name)
        try:
            copy_database(source, copy)
            zf.write(copy, rel.as_posix())
            return copy.stat().st_size
        finally:
            copy.unlink(missing_ok=True)
    with source.open("rb") as handle:  # open first: a read error must not leave half a member
        info = zipfile.ZipInfo.from_file(source, rel.as_posix())
        info.compress_type = zipfile.ZIP_DEFLATED
        with zf.open(info, "w") as member:
            shutil.copyfileobj(handle, member, 1 << 20)
    return source.stat().st_size


def _prune_old(folder: Path, keep: int, current: Path) -> int:
    if keep <= 0:
        return 0
    found = sorted(
        (
            p
            for p in folder.iterdir()
            if p.is_file()
            and p.name.startswith(RUN_PREFIX)
            and p.name.endswith(".zip")
            and not p.name.endswith(INCOMPLETE_SUFFIX)
        ),
        key=lambda p: p.name,
        reverse=True,
    )
    removed = 0
    for old in found[keep:]:
        if old == current:
            continue
        with contextlib.suppress(OSError):
            old.unlink()
            removed += 1
    return removed


def _capped(lines: Sequence[str], indent: str = "    ", limit: int = 10) -> None:
    for line in lines[:limit]:
        click.echo(f"{indent}{line}")
    if len(lines) > limit:
        click.echo(f"{indent}… and {len(lines) - limit} more")


def run_full_backup(scope: Scope, output: str | None, keep: int) -> int:
    root = scope.root
    out_path = _output_path(scope, output)
    skipped: set[str] = set()
    files = list(
        iter_backup_files(root, output=out_path, install_dir=install_dir(), skipped=skipped)
    )
    if not files:
        click.echo("No files to back up.")
        return 0
    click.echo(f"Backing up {len(files)} file(s) from {root} ...")
    partial = out_path.with_name(f".{out_path.name}.{os.getpid()}.partial")
    errors: list[str] = []
    total = 0
    try:
        fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with (
            os.fdopen(fd, "wb") as raw,
            zipfile.ZipFile(raw, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf,
        ):
            for source, rel in files:
                try:
                    total += _add_file(zf, source, rel, out_path.parent)
                except (OSError, sqlite3.Error, ValueError) as exc:
                    errors.append(f"{rel}: {exc}")
        final = out_path
        if errors:
            final = out_path.with_name(out_path.name[: -len(".zip")] + INCOMPLETE_SUFFIX)
        os.replace(partial, final)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    size = final.stat().st_size
    word = "incomplete" if errors else "complete"
    click.echo(f"Backup {word}: {final}")
    click.echo(f"  Files:      {len(files) - len(errors)} of {len(files)}")
    click.echo(f"  Original:   {human_size(total)}")
    click.echo(f"  Compressed: {human_size(size)}")
    if skipped:
        click.echo("  Left out (rebuildable or private):")
        _capped(sorted(f"{d}/" for d in skipped))
    _warn_keychain(root)
    if errors:
        err(f"{len(errors)} file(s) could not be added; the archive was kept as {final.name}:")
        for line in errors[:10]:
            err(f"  {line}")
        return 1
    click.echo(f"Restore with: indus import {final}")
    if out_path.name.startswith(RUN_PREFIX):
        removed = _prune_old(out_path.parent, keep, final)
        if removed:
            click.echo(f"Removed {removed} older backup(s) (keeping {keep}).")
    return 0


# -- quick snapshot -------------------------------------------------------------------------------


def _copy_one(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if is_sqlite_file(source):
        copy_database(source, target)
    else:
        shutil.copyfile(source, target)
        os.chmod(target, 0o600)


def _quick_sources(home: Path) -> list[tuple[Path, str]]:
    found: list[tuple[Path, str]] = []
    for rel in QUICK_PATHS:
        path = home / rel
        if path.is_symlink():
            continue
        if path.is_file():
            found.append((path, rel))
        elif path.is_dir():
            for child in sorted(path.rglob("*")):
                if child.is_file() and not child.is_symlink():
                    found.append((child, child.relative_to(home).as_posix()))
    return found


def run_quick_snapshot(scope: Scope, label: str | None) -> int:
    home = scope.home
    folder = secure_dir(home / QUICK_DIR, home=home, env=scope.env)
    stamp = scope.clock.now().astimezone(UTC).strftime("%Y%m%d-%H%M%S")
    tag = _LABEL.sub("-", label or "").strip("-.")[:40]
    base = f"{stamp}-{tag}" if tag else stamp
    name = base
    counter = 2
    while (folder / name).exists():
        name = f"{base}-{counter}"
        counter += 1
    staging = folder / f".{name}.partial"
    shutil.rmtree(staging, ignore_errors=True)
    secure_dir(staging, home=home, env=scope.env)
    copied = 0
    notes: list[str] = []
    try:
        for source, rel in _quick_sources(home):
            size = source.stat().st_size
            if size > QUICK_MAX_BYTES:
                notes.append(f"skipped {rel} ({human_size(size)} is over the 1 GB limit)")
                continue
            _copy_one(source, staging / rel)
            copied += 1
        if not copied:
            shutil.rmtree(staging, ignore_errors=True)
            click.echo("No state files to snapshot yet.")
            return 0
        os.replace(staging, folder / name)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    olds = sorted(
        (p for p in folder.iterdir() if p.is_dir() and not p.name.startswith(".")),
        key=lambda p: p.name,
        reverse=True,
    )
    for old in olds[QUICK_KEEP:]:
        shutil.rmtree(old, ignore_errors=True)
    click.echo(f"State snapshot: {folder / name} ({copied} file(s))")
    for note in notes:
        click.echo(f"  {note}")
    return 0


@click.command("backup", short_help="Back up the home into a zip file.")
@click.option("-o", "--output", metavar="PATH", default=None, help="Zip file or folder to write.")
@click.option("-q", "--quick", is_flag=True, help="Snapshot this profile's key files instead.")
@click.option("-l", "--label", default=None, help="With --quick: a word added to the name.")
@click.option(
    "-k",
    "--keep",
    type=click.IntRange(min=0),
    default=3,
    show_default=True,
    help="Keep this many indus-backup-*.zip files in the output folder (0 = all).",
)
def backup_command(output: str | None, quick: bool, label: str | None, keep: int) -> int:
    """Back up the whole Indus Agent home (every profile) into one zip file."""
    scope = cli_state().scope
    root = scope.root
    if not root.is_dir():
        err(f"There is no Indus Agent home at {root}.")
        return 1
    try:
        with _backup_slot(root):
            if quick:
                return run_quick_snapshot(scope, label)
            return run_full_backup(scope, output, keep)
    except BackupBusy as exc:
        err(str(exc))
        return 2
    except OSError as exc:
        err(f"Backup failed: {exc}")
        return 1
