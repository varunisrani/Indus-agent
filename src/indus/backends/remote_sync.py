# pyright: strict
"""Pushing host files into remote sandboxes before commands (P11-10).

What is mirrored: credential files (P11-11, upload-only), the profile's skills dir and a few cache
dirs, all below the sandbox's ``.indus`` dir (``SyncItem.remote_path`` is absolute there).

``FileSync.sync`` runs before each command, at most every ``SYNC_INTERVAL_S`` unless forced (first
call, or ``INDUS_FORCE_FILE_SYNC`` set in the scope env). One round: list the items, compare each
file's ``(mtime_ns, size)`` with what the last good round recorded, upload the changed ones and
delete remote copies of files that are gone, then record the new state. Any failure leaves the
recorded state untouched, so the next command retries everything (transactional). A file
modified within ``RACY_WINDOW_S`` of the scan may be rewritten again in the same timestamp tick
with the same size, so it is recorded with a key no stat can equal: the next round checks its
content against the hash that was pushed and uploads only when the bytes differ. ``pushed`` maps
remote path → SHA-256 of what was uploaded (the sync-back of P21 diffs against it).

Transports (``SyncTransport``) do the moving: one file (30 s), one tar archive for many (120 s),
download of a remote tree as a tar (120 s; for P21), delete (15 s). ``SshTransport`` uses ``scp``,
``tar`` over the ssh stdin and ``rm -f`` over ssh. Archives are written by ``tarfile`` while each
file is hashed, so the hash is of exactly the bytes sent.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import posixpath
import shlex
import stat
import tarfile
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Protocol

from indus.backends.contract import BackendError, EnvironmentConnectionError
from indus.backends.credential_files import CredentialFile
from indus.backends.ssh.client import SshTarget, scp_argv, ssh_argv
from indus.backends.ssh.runner import Runner, RunResult
from indus.foundation.clock import Clock
from indus.foundation.scope import Scope

__all__ = [
    "BULK_TIMEOUT_S",
    "CACHE_SUBDIRS",
    "DELETE_TIMEOUT_S",
    "FORCE_SYNC_ENV",
    "RACY_WINDOW_S",
    "SINGLE_TIMEOUT_S",
    "SYNC_INTERVAL_S",
    "FileSync",
    "SshTransport",
    "SyncItem",
    "SyncTransport",
    "collect_items",
    "credential_items",
]

log = logging.getLogger(__name__)

SYNC_INTERVAL_S = 5.0
RACY_WINDOW_S = 2.0
FORCE_SYNC_ENV = "INDUS_FORCE_FILE_SYNC"
SINGLE_TIMEOUT_S = 30.0
BULK_TIMEOUT_S = 120.0
DELETE_TIMEOUT_S = 15.0
MKDIR_TIMEOUT_S = 10.0
HASH_CHUNK = 1 << 16

CACHE_SUBDIRS: tuple[str, ...] = ("documents", "images", "audio", "screenshots", "attachments")
SKIPPED_SKILL_DIRS = frozenset({".git", "node_modules", "__pycache__", ".venv", ".hub", ".archive"})

type StatKey = tuple[int, int]
NEVER_EQUAL: StatKey = (-1, -1)


@dataclass(frozen=True, slots=True)
class SyncItem:
    """A host file mirrored at ``remote_path``. ``upload_only`` items are never pulled back."""

    host_path: str
    remote_path: str
    upload_only: bool = False
    mode: int | None = None


class SyncTransport(Protocol):
    """How bytes reach one remote sandbox (raises on failure)."""

    async def upload_one(self, path: str, dest: str) -> None: ...

    async def upload_bulk(self, tar_path: str) -> None:
        """Unpack ``tar_path`` (members relative to the sync base) into the base."""
        ...

    async def download_bulk(self, src: str, dest: str) -> None:
        """Write the remote tree ``src`` as a tar archive to the local file ``dest``."""
        ...

    async def delete(self, paths: Sequence[str]) -> None: ...


# -- what to mirror ------------------------------------------------------------------------------


def credential_items(files: Iterable[CredentialFile], remote_base: str) -> list[SyncItem]:
    base = remote_base.rstrip("/")
    return [
        SyncItem(f.host_path, f"{base}/{f.remote_rel}", upload_only=True, mode=f.mode)
        for f in files
    ]


def _tree_items(root: Path, remote_root: str, *, skip: frozenset[str]) -> list[SyncItem]:
    items: list[SyncItem] = []
    if not root.is_dir():
        return items
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in skip)
        here = Path(dirpath)
        for name in sorted(filenames):
            path = here / name
            try:
                info = path.lstat()
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue  # symlinks and specials stay on the host
            rel = path.relative_to(root).as_posix()
            items.append(SyncItem(str(path), f"{remote_root}/{rel}"))
    return items


def collect_items(
    scope: Scope, remote_base: str, credentials: Iterable[CredentialFile]
) -> list[SyncItem]:
    """Everything one sandbox should hold: credential files, skills, selected cache dirs."""
    base = remote_base.rstrip("/")
    items = credential_items(credentials, base)
    items += _tree_items(scope.home / "skills", f"{base}/skills", skip=SKIPPED_SKILL_DIRS)
    for sub in CACHE_SUBDIRS:
        items += _tree_items(scope.home / "cache" / sub, f"{base}/cache/{sub}", skip=frozenset())
    return items


# -- the manager ---------------------------------------------------------------------------------


def _stat_key(path: str) -> tuple[StatKey, int] | None:
    """((mtime_ns, size), mtime_ns) for a regular file, else None."""
    try:
        info = os.stat(path)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    return (info.st_mtime_ns, info.st_size), info.st_mtime_ns


def file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


class _HashingReader:
    """Reads ``size`` bytes of a file for ``tarfile`` and hashes them on the way."""

    def __init__(self, handle: IO[bytes]) -> None:
        self._handle = handle
        self.digest = hashlib.sha256()

    def read(self, size: int = -1) -> bytes:
        data = self._handle.read(size)
        self.digest.update(data)
        return data


def _member_name(remote_path: str, base: str) -> str:
    rel = posixpath.relpath(remote_path, base)
    if rel in (".", "") or rel.startswith("../") or rel == ".." or posixpath.isabs(rel):
        raise BackendError(f"sync path {remote_path} is outside the sync base {base}")
    return rel


def write_archive(items: Sequence[SyncItem], base: str, tar_path: str) -> dict[str, str]:
    """Write ``items`` into a tar at ``tar_path`` (names relative to ``base``); return the hash
    of what went in per remote path. A file that changes size while read fails the write."""
    hashes: dict[str, str] = {}
    with tarfile.open(tar_path, "w", format=tarfile.PAX_FORMAT) as archive:
        for item in items:
            name = _member_name(item.remote_path, base)
            with open(item.host_path, "rb") as handle:
                info = os.fstat(handle.fileno())
                member = tarfile.TarInfo(name)
                member.size = info.st_size
                member.mtime = int(info.st_mtime)
                member.mode = item.mode if item.mode is not None else stat.S_IMODE(info.st_mode)
                reader = _HashingReader(handle)
                archive.addfile(member, reader)  # pyright: ignore[reportArgumentType]
                hashes[item.remote_path] = reader.digest.hexdigest()
    return hashes


@dataclass
class SyncReport:
    uploaded: list[str] = field(default_factory=list[str])
    deleted: list[str] = field(default_factory=list[str])


class FileSync:
    """Push-side mirror of one sandbox (see the module notes)."""

    def __init__(
        self,
        source: Callable[[], Iterable[SyncItem]],
        transport: SyncTransport,
        *,
        base: str,
        clock: Clock,
        env: Mapping[str, str] | None = None,
        interval_s: float = SYNC_INTERVAL_S,
        racy_window_s: float = RACY_WINDOW_S,
        temp_dir: str | None = None,
    ) -> None:
        self.source = source
        self.transport = transport
        self.base = base.rstrip("/")
        self.clock = clock
        self.env: Mapping[str, str] = env or {}
        self.interval_s = interval_s
        self.racy_window_s = racy_window_s
        self.temp_dir = temp_dir
        self.synced: dict[str, StatKey] = {}
        self.pushed: dict[str, str] = {}
        self.upload_only: set[str] = set()
        self._last_ok: float | None = None

    def _forced_by_env(self) -> bool:
        value = self.env.get(FORCE_SYNC_ENV, "").strip().lower()
        return value not in ("", "0", "false", "no", "off")

    def due(self) -> bool:
        if self._last_ok is None or self._forced_by_env():
            return True
        return self.clock.monotonic() - self._last_ok >= self.interval_s

    async def sync(self, *, force: bool = False) -> SyncReport | None:
        """One round when due (or forced); None when skipped or failed (state kept as it was)."""
        if not force and not self.due():
            return None
        try:
            return await self._round()
        except Exception as exc:
            log.warning(
                "file sync to the sandbox failed; it is retried before the next command (%s)", exc
            )
            return None

    def _now_ns(self) -> int:
        return int(self.clock.now().timestamp() * 1_000_000_000)

    async def _round(self) -> SyncReport:
        items = list(self.source())
        now_ns = self._now_ns()
        racy_ns = int(self.racy_window_s * 1_000_000_000)
        next_state = dict(self.synced)
        changed: list[SyncItem] = []
        listed: set[str] = set()
        for item in items:
            listed.add(item.remote_path)
            if item.upload_only:
                self.upload_only.add(item.remote_path)
            found = _stat_key(item.host_path)
            if found is None:
                continue  # unreadable this round; keep what the sandbox has
            key, mtime_ns = found
            if self.synced.get(item.remote_path) == key:
                continue
            next_state[item.remote_path] = NEVER_EQUAL if now_ns - mtime_ns < racy_ns else key
            known = self.pushed.get(item.remote_path)
            if known is not None and file_sha256(item.host_path) == known:
                continue  # the sandbox already holds these bytes
            changed.append(item)
        gone = sorted(p for p in self.synced if p not in listed)
        report = SyncReport()
        new_hashes: dict[str, str] = {}
        if len(changed) == 1:
            only = changed[0]
            _member_name(only.remote_path, self.base)
            new_hashes[only.remote_path] = file_sha256(only.host_path)
            await self.transport.upload_one(only.host_path, only.remote_path)
        elif changed:
            fd, tar_path = tempfile.mkstemp(prefix="indus-sync-", suffix=".tar", dir=self.temp_dir)
            os.close(fd)
            try:
                new_hashes = write_archive(changed, self.base, tar_path)
                await self.transport.upload_bulk(tar_path)
            finally:
                with contextlib.suppress(OSError):
                    os.unlink(tar_path)
        if gone:
            await self.transport.delete(gone)
        # Every transport call worked: commit.
        for path in gone:
            next_state.pop(path, None)
            self.pushed.pop(path, None)
        self.pushed.update(new_hashes)
        self.synced = next_state
        self._last_ok = self.clock.monotonic()
        report.uploaded = [c.remote_path for c in changed]
        report.deleted = gone
        if changed or gone:
            log.debug("file sync: %d uploaded, %d deleted", len(changed), len(gone))
        return report


# -- SSH -----------------------------------------------------------------------------------------


class SshTransport:
    """``SyncTransport`` over the SSH backend's client (one master per target)."""

    def __init__(
        self,
        *,
        ssh: str,
        scp: str,
        target: SshTarget,
        control: str | None,
        env: Mapping[str, str],
        runner: Runner,
        base: str,
    ) -> None:
        self.ssh = ssh
        self.scp = scp
        self.target = target
        self.control = control
        self.env = dict(env)
        self.runner = runner
        self.base = base.rstrip("/")

    async def _remote(
        self,
        command: str,
        timeout: float,
        what: str,
        *,
        stdin_path: str | None = None,
        stdout_path: str | None = None,
    ) -> RunResult:
        argv = ssh_argv(self.ssh, self.target, command, control=self.control)
        return await self._run(argv, timeout, what, stdin_path=stdin_path, stdout_path=stdout_path)

    async def _run(
        self,
        argv: Sequence[str],
        timeout: float,
        what: str,
        *,
        stdin_path: str | None = None,
        stdout_path: str | None = None,
    ) -> RunResult:
        try:
            result = await self.runner(
                argv, env=self.env, timeout=timeout, stdin_path=stdin_path, stdout_path=stdout_path
            )
        except TimeoutError as exc:
            raise EnvironmentConnectionError(
                f"{what} on {self.target.label} took longer than {timeout:g}s"
            ) from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()
            raise BackendError(
                f"{what} on {self.target.label} failed (exit {result.returncode})"
                + (f": {detail[-1]}" if detail else "")
            )
        return result

    async def upload_one(self, path: str, dest: str) -> None:
        parent = posixpath.dirname(dest) or "/"
        await self._remote(f"mkdir -p -- {shlex.quote(parent)}", MKDIR_TIMEOUT_S, "creating a dir")
        argv = scp_argv(self.scp, self.target, path, dest, control=self.control)
        await self._run(argv, SINGLE_TIMEOUT_S, "copying a file")

    async def upload_bulk(self, tar_path: str) -> None:
        base = shlex.quote(self.base)
        await self._remote(
            f"mkdir -p -- {base} && tar -xf - -C {base}",
            BULK_TIMEOUT_S,
            "unpacking synced files",
            stdin_path=tar_path,
        )

    async def download_bulk(self, src: str, dest: str) -> None:
        await self._remote(
            f"tar -cf - -C / {shlex.quote(src.lstrip('/'))}",
            BULK_TIMEOUT_S,
            "packing remote files",
            stdout_path=dest,
        )

    async def delete(self, paths: Sequence[str]) -> None:
        if not paths:
            return
        quoted = " ".join(shlex.quote(p) for p in paths)
        await self._remote(f"rm -f -- {quoted}", DELETE_TIMEOUT_S, "removing synced files")
