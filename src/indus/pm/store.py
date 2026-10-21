"""The pm store: unpacked packages under ``<root>/pm/store/`` (P24-23).

Every entry is a folder named ``<package>-<version>-<sha256[:16]>``: the archive hash names it,
so two installs of the same pinned bytes share one entry and a new pin never reuses an old one.

An entry is born whole or not at all. Unpacking happens in a fresh ``.staging-<token>`` folder;
only after the archive is fully unpacked and the executable checked is that folder renamed to the
entry name (one ``os.replace`` on the same filesystem). A failure removes the staging folder; a
process killed halfway leaves only a ``.staging-*`` folder, which ``gc`` removes. An entry that
appeared meanwhile (another process won the race) is kept and the staging copy dropped.

Archives are unpacked defensively: absolute names, ``..``, links that leave the folder, device
files and FIFOs are refused (tar through the standard ``data`` filter, zip by our own checks).

``tree_digest`` is the content digest of a folder (relative path and bytes of every file, link
targets of every link); ``doctor`` compares it with the digest recorded at install time.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import secrets
import shutil
import stat
import tarfile
import zipfile
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

from indus.pm.lock import PmError

__all__ = [
    "FORMATS",
    "STAGING_PREFIX",
    "Store",
    "UnpackRefused",
    "entry_name",
    "file_sha256",
    "tree_digest",
    "unpack",
]

FORMATS = ("tar.gz", "tar.xz", "zip", "file")
STAGING_PREFIX = ".staging-"
_CHUNK = 1 << 20


class UnpackRefused(PmError):
    """The archive is broken or holds a member that may not be written."""


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def entry_name(package: str, version: str, sha256: str) -> str:
    return f"{package}-{version}-{sha256[:16]}"


def tree_digest(root: Path) -> str:
    """sha256 over every file's relative path and bytes and every link's target, in path order."""
    digest = hashlib.sha256()
    rows: list[tuple[str, Path]] = []
    for folder, dirs, files in os.walk(root, followlinks=False):
        base = Path(folder)
        for name in (*dirs, *files):
            path = base / name
            if path.is_symlink() or not path.is_dir():
                rows.append((path.relative_to(root).as_posix(), path))
    for rel, path in sorted(rows):
        if path.is_symlink():
            digest.update(f"L\0{rel}\0{os.readlink(path)}\0".encode("utf-8", "surrogateescape"))
        else:
            digest.update(f"F\0{rel}\0{file_sha256(path)}\0".encode("utf-8", "surrogateescape"))
    return digest.hexdigest()


# -- unpacking -------------------------------------------------------------------------------------


def _stripped(name: str, strip: int) -> str | None:
    """``name`` without its first ``strip`` folders (None when nothing is left). An absolute
    name is refused: an archive member never names a place outside the package folder."""
    text = name.replace("\\", "/")
    if text.startswith("/") or (len(text) > 1 and text[1] == ":"):
        raise UnpackRefused(f"member {name!r} has an absolute name")
    parts = [p for p in PurePosixPath(text).parts if p not in ("", ".")]
    rest = parts[strip:]
    return "/".join(rest) if rest else None


def _unpack_tar(archive: Path, dest: Path, strip: int) -> None:
    def keep(member: tarfile.TarInfo, path: str) -> tarfile.TarInfo | None:
        name = _stripped(member.name, strip)
        if name is None:
            return None
        link = member.linkname
        if member.islnk():
            stripped_link = _stripped(member.linkname, strip)
            if stripped_link is None:
                raise UnpackRefused(f"{archive.name}: hard link {member.name!r} leaves the folder")
            link = stripped_link
        try:
            return tarfile.data_filter(member.replace(name=name, linkname=link, deep=False), path)
        except tarfile.FilterError as exc:
            raise UnpackRefused(f"{archive.name}: member {member.name!r} refused ({exc})") from exc

    try:
        with tarfile.open(archive, "r:*") as tar:
            tar.extractall(dest, filter=keep)
    except (tarfile.TarError, EOFError) as exc:
        raise UnpackRefused(f"{archive.name} is not a readable tar archive ({exc})") from exc


def _inside(dest: Path, name: str) -> Path:
    target = (dest / name).resolve()
    if not target.is_relative_to(dest.resolve()):
        raise UnpackRefused(f"member {name!r} would land outside the package folder")
    return target


def _unpack_zip(archive: Path, dest: Path, strip: int) -> None:
    try:
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                name = _stripped(info.filename, strip)
                if name is None:
                    continue
                if ".." in PurePosixPath(name).parts:
                    raise UnpackRefused(f"{archive.name}: member {info.filename!r} refused")
                mode = (info.external_attr >> 16) & 0o177777
                target = _inside(dest, name)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                if stat.S_ISLNK(mode):
                    link = zf.read(info).decode("utf-8")
                    if link.startswith("/") or not (target.parent / link).resolve().is_relative_to(
                        dest.resolve()
                    ):
                        raise UnpackRefused(f"{archive.name}: link {info.filename!r} leaves")
                    os.symlink(link, target)
                    continue
                if mode and not stat.S_ISREG(mode):
                    raise UnpackRefused(f"{archive.name}: {info.filename!r} is not a plain file")
                with zf.open(info) as src, target.open("wb") as out:
                    shutil.copyfileobj(src, out, _CHUNK)
                if mode & 0o111:
                    target.chmod(0o755)
    except (zipfile.BadZipFile, UnicodeDecodeError) as exc:
        raise UnpackRefused(f"{archive.name} is not a readable zip archive ({exc})") from exc


def unpack(archive: Path, dest: Path, fmt: str, *, strip: int = 0, file_name: str = "") -> None:
    """Unpack ``archive`` (``fmt`` one of ``FORMATS``) into the existing folder ``dest``."""
    if fmt in ("tar.gz", "tar.xz"):
        _unpack_tar(archive, dest, strip)
    elif fmt == "zip":
        _unpack_zip(archive, dest, strip)
    elif fmt == "file":
        name = file_name or archive.name
        if "/" in name or "\\" in name or name in ("", ".", ".."):
            raise UnpackRefused(f"bad executable name {name!r}")
        target = dest / name
        shutil.copyfile(archive, target)
        target.chmod(0o755)
    else:
        raise UnpackRefused(f"unknown archive format {fmt!r}")


# -- the store -------------------------------------------------------------------------------------


class Store:
    def __init__(self, root: Path) -> None:
        self.root = root

    def entry(self, name: str) -> Path:
        return self.root / name

    def entries(self) -> list[Path]:
        """Published entries (no dot folders)."""
        if not self.root.is_dir():
            return []
        return sorted(p for p in self.root.iterdir() if p.is_dir() and not p.name.startswith("."))

    def staging_dirs(self) -> list[Path]:
        if not self.root.is_dir():
            return []
        return sorted(p for p in self.root.iterdir() if p.name.startswith(STAGING_PREFIX))

    @contextlib.contextmanager
    def staging(self) -> Iterator[Path]:
        """A fresh staging folder, removed on the way out unless it was published."""
        self.root.mkdir(parents=True, exist_ok=True)
        folder = self.root / f"{STAGING_PREFIX}{os.getpid()}-{secrets.token_hex(6)}"
        folder.mkdir()
        try:
            yield folder
        finally:
            if folder.exists():
                shutil.rmtree(folder, ignore_errors=True)

    def publish(self, staged: Path, name: str) -> Path:
        """Rename ``staged`` to the entry ``name``; an existing entry wins and is returned."""
        entry = self.entry(name)
        if entry.exists():
            return entry
        try:
            os.replace(staged, entry)
        except OSError:
            if entry.is_dir():
                return entry
            raise
        return entry

    def remove(self, path: Path) -> None:
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        else:
            shutil.rmtree(path, ignore_errors=True)
