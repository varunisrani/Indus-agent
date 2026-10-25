# pyright: strict
"""Bringing files the agent changed in a cloud sandbox back to the profile (P21-10, R02.31).

Runs when a Modal, Daytona or Vercel sandbox is cleaned up, against the push-side ``FileSync`` of
that sandbox (P11-10):

1. Nothing was ever pushed in this session → nothing happens (no download).
2. Leftovers of crashed runs are swept: entries of ``<home>/cache/sync-back/`` named
   ``<pid>-…`` whose owner pid is dead and that are older than ``STALE_AFTER_S``.
3. The remote app dir comes down as one tar through the backend transport into a private
   ``<pid>-<hex>`` dir there. An archive larger than ``max_bytes``
   (``terminal.sync_back_max_bytes``, 2 GiB) is refused whole.
4. The apply, under a file lock in the profile home (one apply at a time across processes) and
   with Ctrl+C held back until it is over: each tar member is checked before anything is written.
   Only regular files; absolute names, ``..``, symlinks, hard links and devices are refused. The
   member must fall inside one of the mirrored roots (remote dir → host dir: the skills dir and
   the synced cache dirs); credential files are upload-only and never written (by remote path and
   by resolved host path). A member whose SHA-256 equals what this session pushed is unchanged;
   one equal to the host file is not rewritten either. A changed file is written through a temp
   file + rename in its target dir after checking that the target's real parent stays inside the
   real root (a symlinked dir on the host cannot redirect the write) and that the target is not a
   symlink. Missing dirs are made one level at a time and never through a symlink.
5. Download + apply get ``ATTEMPTS`` tries with ``BACKOFF_S`` waits in between; a refusal is not
   retried. Failures are logged and reported, never raised to the cleanup that called us.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import posixpath
import secrets
import shutil
import signal
import stat
import sys
import tarfile
import threading
import time
from collections.abc import Callable, Collection, Generator, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import IO, Any, Literal

from indus.backends.credential_files import CredentialFile
from indus.backends.remote_sync import FileSync
from indus.foundation.blocking import run_blocking
from indus.foundation.clock import Clock
from indus.foundation.fsio import FileLock

__all__ = [
    "ATTEMPTS",
    "BACKOFF_S",
    "DEFAULT_MAX_BYTES",
    "STALE_AFTER_S",
    "ApplyPlan",
    "SyncBackReport",
    "apply_archive",
    "credential_hosts",
    "defer_sigint",
    "sweep_stale",
    "sync_back",
    "temp_root",
]

log = logging.getLogger(__name__)

DEFAULT_MAX_BYTES = 2 * 1024 * 1024 * 1024
ATTEMPTS = 3
BACKOFF_S: tuple[float, ...] = (2.0, 4.0, 8.0)
STALE_AFTER_S = 30 * 60.0
LOCK_TIMEOUT_S = 120.0
CHUNK = 1 << 16
TAR_NAME = "remote.tar"

type Status = Literal["skipped", "refused", "applied", "failed"]


@dataclass
class SyncBackReport:
    status: Status
    written: list[str] = field(default_factory=list[str])
    refused: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ApplyPlan:
    """What the apply may do: ``roots`` are (remote dir, host dir) pairs, ``pushed`` the hash this
    session pushed per remote path, the two upload-only sets are never written."""

    roots: tuple[tuple[str, str], ...]
    pushed: dict[str, str]
    upload_only_remote: frozenset[str]
    upload_only_hosts: frozenset[str]


class _Refused(Exception):
    pass


# -- helpers ---------------------------------------------------------------------------------------


def _real(path: str) -> str:
    try:
        return os.path.realpath(path)
    except OSError:
        return os.path.abspath(path)


def credential_hosts(files: Iterable[CredentialFile]) -> set[str]:
    """Resolved host paths of the credential files (upload-only)."""
    return {_real(f.host_path) for f in files}


def temp_root(home: Path) -> Path:
    return home / "cache" / "sync-back"


def _pid_alive(pid: int) -> bool:
    """Dead only when the kernel says there is no such process (no permission = alive)."""
    if pid < 1:
        return False
    if sys.platform == "win32":  # os.kill(pid, 0) would terminate the process there
        from indus.platform import winproc

        return winproc.pid_alive(pid)
    outcome: OSError | None = None
    try:
        os.kill(pid, 0)
    except OSError as exc:
        outcome = exc
    return not isinstance(outcome, ProcessLookupError)


def sweep_stale(
    root: Path,
    *,
    now: float | None = None,
    pid_alive: Callable[[int], bool] = _pid_alive,
    stale_after_s: float = STALE_AFTER_S,
) -> int:
    """Remove ``<pid>-…`` entries whose owner is dead and that are older than the cutoff."""
    cutoff = (time.time() if now is None else now) - stale_after_s
    removed = 0
    try:
        entries = list(root.iterdir())
    except OSError:
        return 0
    for entry in entries:
        owner, _, rest = entry.name.partition("-")
        if not owner.isdigit() or not rest:
            continue
        try:
            info = entry.lstat()
            if info.st_mtime >= cutoff or pid_alive(int(owner)):
                continue
            if stat.S_ISDIR(info.st_mode):
                shutil.rmtree(entry)
            else:
                entry.unlink()
            removed += 1
        except OSError:
            log.debug("could not remove the stale sync-back entry %s", entry)
    return removed


@contextlib.contextmanager
def defer_sigint() -> Generator[None]:
    """Hold a Ctrl+C back until the block is over, then deliver it (main thread only)."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    held: list[int] = []

    def hold(signum: int, frame: Any) -> None:
        held.append(signum)
        log.info("Ctrl+C held until the sync-back finishes writing")

    previous = signal.signal(signal.SIGINT, hold)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)
        if held:
            signal.raise_signal(signal.SIGINT)


def _sha256_of(handle: IO[bytes]) -> str:
    digest = hashlib.sha256()
    while chunk := handle.read(CHUNK):
        digest.update(chunk)
    return digest.hexdigest()


def _file_sha256(path: str) -> str:
    with open(path, "rb") as handle:
        return _sha256_of(handle)


def _member_remote(member: tarfile.TarInfo) -> str:
    """The absolute remote path of a member, or ``_Refused`` with the reason."""
    name = member.name
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        raise _Refused("absolute path")
    parts = PurePosixPath(name).parts
    if ".." in parts:
        raise _Refused("path leaves the archive root")
    if member.issym() or member.islnk():
        raise _Refused("link")
    if not member.isfile():
        raise _Refused("not a regular file")
    clean = posixpath.normpath(name)
    if clean in (".", ""):
        raise _Refused("empty name")
    return "/" + clean.lstrip("/")


def _host_target(remote: str, plan: ApplyPlan) -> tuple[str, list[str]]:
    """(host root, path parts below it) for ``remote``, or ``_Refused``."""
    if remote in plan.upload_only_remote:
        raise _Refused("credential file (upload-only)")
    for remote_root, host_root in plan.roots:
        prefix = remote_root.rstrip("/") + "/"
        if remote.startswith(prefix):
            parts = [p for p in remote[len(prefix) :].split("/") if p]
            if not parts or any(p in (".", "..") for p in parts):
                raise _Refused("path leaves the mirrored dir")
            return host_root, parts
    raise _Refused("outside the mirrored dirs")


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _make_parents(host_root: str, parts: Sequence[str]) -> str:
    """Create the dirs of ``parts[:-1]`` under the root one level at a time, never through a
    symlink; return the parent path."""
    os.makedirs(host_root, mode=0o700, exist_ok=True)
    current = host_root
    for part in parts[:-1]:
        current = os.path.join(current, part)
        if os.path.islink(current):
            raise _Refused("a host dir on the way is a symlink")
        if not os.path.exists(current):
            os.mkdir(current, 0o700)
        elif not os.path.isdir(current):
            raise _Refused("a host path on the way is not a dir")
    return current


def _write_replacing(source: str, target: str, mode: int) -> None:
    parent = os.path.dirname(target)
    tmp = os.path.join(parent, f".{os.path.basename(target)}.sync-{secrets.token_hex(4)}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as out, open(source, "rb") as src:
            shutil.copyfileobj(src, out, CHUNK)
            out.flush()
            os.fsync(out.fileno())
            if hasattr(os, "fchmod"):
                os.fchmod(out.fileno(), mode)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def apply_archive(
    tar_path: str,
    plan: ApplyPlan,
    *,
    staging: str,
    on_write: Callable[[str], None] | None = None,
) -> tuple[list[str], list[tuple[str, str]]]:
    """Apply one downloaded archive (see the module notes, step 4): (written host paths,
    refused (member, reason) pairs). ``on_write`` is called after each written file (tests)."""
    written: list[str] = []
    refused: list[tuple[str, str]] = []
    with tarfile.open(tar_path, "r:") as archive:
        for member in archive:
            if member.isdir():
                continue
            try:
                remote = _member_remote(member)
                host_root, parts = _host_target(remote, plan)
            except _Refused as why:
                refused.append((member.name, str(why)))
                continue
            source = archive.extractfile(member)
            if source is None:
                refused.append((member.name, "unreadable member"))
                continue
            staged = os.path.join(staging, secrets.token_hex(8))
            digest = hashlib.sha256()
            with source, open(staged, "wb") as out:
                while chunk := source.read(CHUNK):
                    digest.update(chunk)
                    out.write(chunk)
            new_hash = digest.hexdigest()
            try:
                pushed = plan.pushed.get(remote)
                if pushed is not None and pushed == new_hash:
                    continue  # unchanged since this session pushed it
                target = _apply_one(staged, new_hash, host_root, parts, plan, pushed)
            except _Refused as why:
                refused.append((member.name, str(why)))
                continue
            finally:
                with contextlib.suppress(OSError):
                    os.unlink(staged)
            if target is not None:
                written.append(target)
                if on_write is not None:
                    on_write(target)
    return written, refused


def _apply_one(
    staged: str,
    new_hash: str,
    host_root: str,
    parts: Sequence[str],
    plan: ApplyPlan,
    pushed: str | None,
) -> str | None:
    target = os.path.join(host_root, *parts)
    if _real(target) in plan.upload_only_hosts:
        raise _Refused("credential file (upload-only)")
    parent = _make_parents(host_root, parts)
    if not _inside(_real(parent), _real(host_root)):
        raise _Refused("target leaves the mirrored dir")
    if os.path.islink(target):
        raise _Refused("target is a symlink")
    mode = 0o600
    if os.path.lexists(target):
        if not os.path.isfile(target):
            raise _Refused("target is not a regular file")
        current = _file_sha256(target)
        if current == new_hash:
            return None  # the host already has these bytes
        if pushed is not None and current != pushed:
            log.warning(
                "sync-back: %s changed on the host and in the sandbox; the sandbox copy wins",
                target,
            )
        mode = stat.S_IMODE(os.stat(target).st_mode)
    _write_replacing(staged, target, mode)
    return target


# -- the whole round -------------------------------------------------------------------------------


def _locked_apply(
    lock_path: Path,
    tar_path: str,
    plan: ApplyPlan,
    staging: str,
    on_write: Callable[[str], None] | None,
) -> tuple[list[str], list[tuple[str, str]]]:
    with FileLock(lock_path, timeout=LOCK_TIMEOUT_S):
        return apply_archive(tar_path, plan, staging=staging, on_write=on_write)


async def sync_back(
    sync: FileSync,
    *,
    home: Path,
    remote_dir: str,
    roots: Sequence[tuple[str, str]],
    upload_only_hosts: Collection[str] = (),
    max_bytes: int = DEFAULT_MAX_BYTES,
    clock: Clock,
    attempts: int = ATTEMPTS,
    backoff_s: Sequence[float] = BACKOFF_S,
    on_write: Callable[[str], None] | None = None,
) -> SyncBackReport:
    """One sync-back of the sandbox behind ``sync`` (see the module notes)."""
    if not sync.pushed and not sync.synced:
        log.debug("sync-back skipped: nothing was pushed to this sandbox")
        return SyncBackReport("skipped", detail="nothing was pushed")
    root = temp_root(home)
    root.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(root, 0o700)
    swept = sweep_stale(root)
    if swept:
        log.info("removed %d stale sync-back temp entr%s", swept, "y" if swept == 1 else "ies")
    plan = ApplyPlan(
        roots=tuple((r.rstrip("/"), h) for r, h in roots),
        pushed=dict(sync.pushed),
        upload_only_remote=frozenset(sync.upload_only),
        upload_only_hosts=frozenset(_real(p) for p in upload_only_hosts),
    )
    last_error = ""
    for attempt in range(max(1, attempts)):
        if attempt:
            delay = backoff_s[min(attempt - 1, len(backoff_s) - 1)] if backoff_s else 0.0
            log.warning(
                "sync-back attempt %d failed (%s); retrying in %gs", attempt, last_error, delay
            )
            await clock.sleep(delay)
        work = root / f"{os.getpid()}-{secrets.token_hex(6)}"
        try:
            work.mkdir(mode=0o700)
            tar_path = str(work / TAR_NAME)
            await sync.transport.download_bulk(remote_dir, tar_path)
            size = os.path.getsize(tar_path)
            if size > max_bytes:
                log.warning(
                    "sync-back refused: the sandbox archive is %d bytes, over the %d byte limit "
                    "(terminal.sync_back_max_bytes)",
                    size,
                    max_bytes,
                )
                return SyncBackReport("refused", detail=f"archive of {size} bytes over the limit")
            with defer_sigint():
                written, refused = await run_blocking(
                    _locked_apply,
                    home / "cache" / "sync-back.lock",
                    tar_path,
                    plan,
                    str(work),
                    on_write,
                    label="sync-back",
                )
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            continue
        finally:
            shutil.rmtree(work, ignore_errors=True)
        for name, why in refused:
            log.debug("sync-back skipped %s: %s", name, why)
        if written:
            log.info("sync-back wrote %d changed file(s)", len(written))
        return SyncBackReport("applied", written=written, refused=refused)
    log.warning("sync-back failed after %d attempts: %s", attempts, last_error)
    return SyncBackReport("failed", detail=last_error)
