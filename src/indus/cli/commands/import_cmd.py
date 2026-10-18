"""``indus import ZIP [-f]``: restore a backup into the current home (P07-23, R04.22).

Order, and nothing is written before the last check passes:

1. the file exists and is a zip; it looks like one of ours (a ``config.yaml``, ``.env`` or
   ``state.db`` somewhere); a single wrapping folder such as ``.indus/`` is stripped;
2. every member must stay inside the home: ``..``, absolute paths and drive letters refuse the
   whole archive;
3. existing ``config.yaml`` / ``.env`` need a yes (or ``-f``, which a run without a terminal
   must pass);
4. every member is read to the end, so its CRC is checked, before the first write.

The target is the home this command runs under — with ``-p work`` that profile — and is never
mapped back to the root. Files are written through a temp file and a rename, owner-only. Files
that describe the source machine's running processes (pid, lock, process and gateway state files)
and SQLite sidecars are never written. A ``.db`` member is checked, then written page by page into
the existing database with the online backup API, so connections that have it open see the
restored data instead of a replaced file; another process holding it means the file is skipped
with a warning. Fewer sessions or messages after the import than before is reported. Exit 1 when
anything was skipped.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sqlite3
import tempfile
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import click

from indus.cli.backup_rules import DB_SUFFIXES, RETIRED_WAL_MARK, SIDECAR_SUFFIXES
from indus.cli.cmdkit import cli_state, err
from indus.cli.prompts import confirm, io_for
from indus.foundation.home import secure_dir
from indus.state import maintenance
from indus.state.readpool import read_only_uri

__all__ = ["MARKERS", "RUNTIME_NAMES", "import_command", "plan_members", "strip_prefix"]

MARKERS = frozenset({"config.yaml", ".env", "state.db"})
RUNTIME_NAMES = frozenset(
    {"gateway_state.json", "gateway.pid", "cron.pid", "gateway.lock", "processes.json"}
)
RUNTIME_SUFFIXES = (".pid", ".lock")
WRAPPER_NAMES = frozenset({".indus", "indus", "indus-home"})
_READ_ERRORS: tuple[type[BaseException], ...] = (zipfile.BadZipFile, zlib.error, EOFError, OSError)


class Refused(Exception):
    """The archive cannot be imported; nothing was written."""


@dataclass
class Plan:
    write: list[tuple[str, PurePosixPath]] = field(default_factory=lambda: [])
    runtime: list[str] = field(default_factory=lambda: [])


def strip_prefix(names: list[str]) -> str:
    """The one folder wrapping every member (``.indus/``), or ``""``."""
    files = [n for n in names if not n.endswith("/")]
    if not files:
        return ""
    firsts = {n.split("/", 1)[0] for n in files}
    if len(firsts) != 1 or any("/" not in n for n in files):
        return ""
    top = firsts.pop()
    if top == "profiles":
        return ""
    inside = {n.split("/", 2)[1] for n in files}
    if top in WRAPPER_NAMES or inside & MARKERS:
        return top + "/"
    return ""


def _safe_parts(rel: str) -> PurePosixPath | None:
    if not rel or "\\" in rel or rel.startswith("/") or (len(rel) > 1 and rel[1] == ":"):
        return None
    parts = PurePosixPath(rel).parts
    if any(p in ("..", "") for p in parts):
        return None
    return PurePosixPath(*parts) if parts else None


def _is_runtime(rel: PurePosixPath) -> bool:
    name = rel.name
    if name in RUNTIME_NAMES or name.endswith(RUNTIME_SUFFIXES) or RETIRED_WAL_MARK in name:
        return True
    for suffix in SIDECAR_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)].endswith(DB_SUFFIXES)
    return False


def plan_members(zf: zipfile.ZipFile) -> Plan:
    """What to write and what to keep from this machine; raises ``Refused`` for a bad archive."""
    names = zf.namelist()
    if not names:
        raise Refused("the archive is empty")
    if not any(PurePosixPath(n).name in MARKERS for n in names):
        raise Refused(
            "this zip does not look like an Indus Agent backup (no config.yaml, .env or state.db)"
        )
    prefix = strip_prefix(names)
    plan = Plan()
    bad: list[str] = []
    for member in names:
        if member.endswith("/"):
            continue
        rel_text = member[len(prefix) :] if prefix and member.startswith(prefix) else member
        rel = _safe_parts(rel_text)
        if rel is None:
            bad.append(member)
            continue
        if _is_runtime(rel):
            plan.runtime.append(rel.as_posix())
            continue
        plan.write.append((member, rel))
    if bad:
        raise Refused(
            f"{len(bad)} member(s) would land outside the home (for example {bad[0]!r}); "
            "the archive was refused"
        )
    return plan


def _damaged(zf: zipfile.ZipFile, members: list[str]) -> list[str]:
    found: list[str] = []
    for member in members:
        try:
            with zf.open(member) as src:
                while src.read(1 << 20):
                    pass
        except _READ_ERRORS as exc:
            found.append(f"{member}: {exc}")
    return found


def _extract_to_temp(zf: zipfile.ZipFile, member: str, folder: Path, name: str) -> Path:
    fd, temp = tempfile.mkstemp(dir=folder, prefix=f".{name[:60]}.", suffix=".importing")
    try:
        with os.fdopen(fd, "wb") as out, zf.open(member) as src:
            shutil.copyfileobj(src, out, 1 << 20)
            out.flush()
            os.fsync(out.fileno())
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise
    return Path(temp)


def _counts(path: Path) -> tuple[int, int] | None:
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(read_only_uri(path), uri=True, timeout=5.0)
        try:
            sessions = int(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])
            messages = int(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
            return sessions, messages
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def _check_db(path: Path) -> str | None:
    try:
        conn = sqlite3.connect(read_only_uri(path), uri=True, timeout=5.0)
        try:
            rows = [str(r[0]) for r in conn.execute("PRAGMA quick_check").fetchall()]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return str(exc)
    return None if rows == ["ok"] else "; ".join(rows[:3])


def _write_db(staged: Path, target: Path) -> None:
    """Pages of ``staged`` into ``target`` (same file, same inode) through the backup API."""
    problem = _check_db(staged)
    if problem is not None:
        raise OSError(f"the archived database is damaged ({problem}); the existing one was kept")
    if not target.exists():
        os.replace(staged, target)
        os.chmod(target, 0o600)
        return
    holders = maintenance.scan_holders(target)
    if holders:
        pids = ", ".join(str(h.pid) if h.pid >= 0 else "?" for h in holders)
        raise OSError(
            f"it is open in another process (pid {pids}); stop it and import again to restore "
            "this file"
        )
    src = sqlite3.connect(read_only_uri(staged), uri=True, timeout=5.0)
    try:
        dst = sqlite3.connect(str(target), timeout=10.0)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    os.chmod(target, 0o600)


@click.command("import", short_help="Restore a backup zip.")
@click.argument("zipfile_path", metavar="ZIP")
@click.option("-f", "--force", is_flag=True, help="Overwrite an existing setup without asking.")
def import_command(zipfile_path: str, force: bool) -> int:
    """Restore an `indus backup` zip into this profile's home."""
    state = cli_state()
    scope = state.scope
    source = Path(zipfile_path).expanduser()
    if not source.is_file():
        err(f"No such file: {source}")
        return 1
    if not zipfile.is_zipfile(source):
        err(f"Not a zip file: {source}")
        return 1
    home = scope.home
    with zipfile.ZipFile(source) as zf:
        try:
            plan = plan_members(zf)
        except Refused as exc:
            err(f"Not imported: {exc}.")
            return 1
        click.echo(f"Backup {source.name}: {len(plan.write)} file(s) to restore into {home}")
        click.echo("Stop the gateway and any other Indus Agent windows first, if any are running.")
        if (home / "config.yaml").exists() or (home / ".env").exists():
            answer = confirm(
                io_for(
                    state, "Not imported: this home already has settings; pass -f to overwrite."
                ),
                "This home already has settings; files from the backup will replace them. "
                "Continue?",
                assume_yes=force,
            )
            if answer is None:
                return 1
            if not answer:
                click.echo("Not imported.")
                return 0
        damaged = _damaged(zf, [m for m, _ in plan.write])
        if damaged:
            err(f"The archive is damaged ({len(damaged)} member(s)); nothing was restored:")
            for line in damaged[:10]:
                err(f"  {line}")
            return 1
        restored, skipped, shrunk = _restore(zf, plan, home, scope)
    click.echo(f"Restored {restored} file(s) into {home}.")
    if plan.runtime:
        click.echo(
            f"Kept this machine's own runtime files ({len(plan.runtime)}), e.g. {plan.runtime[0]}."
        )
    for rel, before, after in shrunk:
        click.echo(
            f"Note: {rel} now holds {after[0]} session(s) / {after[1]} message(s); it held "
            f"{before[0]} / {before[1]} before the import. Anything newer than the backup is gone."
        )
    if skipped:
        err(f"{len(skipped)} file(s) were not restored:")
        for line in skipped[:10]:
            err(f"  {line}")
        return 1
    return 0


def _restore(
    zf: zipfile.ZipFile, plan: Plan, home: Path, scope: object
) -> tuple[int, list[str], list[tuple[str, tuple[int, int], tuple[int, int]]]]:
    env = getattr(scope, "env", None)
    secure_dir(home, home=home, env=env)
    restored = 0
    skipped: list[str] = []
    shrunk: list[tuple[str, tuple[int, int], tuple[int, int]]] = []
    for member, rel in plan.write:
        target = home.joinpath(*rel.parts)
        try:
            target.resolve().relative_to(home.resolve())
        except ValueError:
            skipped.append(f"{rel}: it would land outside the home through a link")
            continue
        try:
            secure_dir(target.parent, home=home, env=env)
            staged = _extract_to_temp(zf, member, target.parent, rel.name)
            try:
                if rel.name.endswith(DB_SUFFIXES):
                    before = _counts(target)
                    _write_db(staged, target)
                    after = _counts(target)
                    if before and after and (after[0] < before[0] or after[1] < before[1]):
                        shrunk.append((rel.as_posix(), before, after))
                else:
                    os.chmod(staged, 0o600)
                    os.replace(staged, target)
            finally:
                with contextlib.suppress(OSError):
                    staged.unlink(missing_ok=True)
            restored += 1
        except (OSError, sqlite3.Error, *_READ_ERRORS) as exc:
            skipped.append(f"{rel}: {exc}")
    return restored, skipped, shrunk
