# pyright: strict
"""Which paths the file tools may read and write (P05-16, P05-18).

Every check here is a pure function over path text plus the call's ``Scope`` (homes come from the
scope, never from the process). Callers pass *candidates*: the path as the model wrote it
(expanded and normalised) and the resolved real path, so neither a symlink pointing at a
protected file nor a protected name that is itself a symlink slips through.

Reads:
- ``nt_namespace_error``: Windows NT/device namespace prefixes, checked on the raw text before
  anything resolves it (resolving some of them already talks to the network on Windows).
- ``is_device_path`` / ``blocked_device``: device files that never end or block (``/dev/zero``,
  ``/dev/stdin``, ...) and ``/proc/<pid>/`` files that leak a process's secrets or memory,
  checked on the literal path, every symlink hop and the final real path.
- ``read_denial``: Indus's own secret stores and control files, and project secret env files.

Writes (in the order the tools apply them):
- ``write_denial``: system locations, credential directories in every home a write can reach,
  Indus secret stores and config — refused outright.
- ``write_needs_approval``: ``~/.ssh/config`` (can run commands through ``ProxyCommand``).
- ``binary_document_reason``: plain text over a binary document.
- ``protected_instruction_name``: files that steer future agent runs; every write asks.
"""

from __future__ import annotations

import fnmatch
import os
import posixpath
import re
from collections.abc import Iterable

from indus.foundation.home import expand_path_text
from indus.foundation.scope import Scope

__all__ = [
    "BINARY_EXTENSIONS",
    "OPAQUE_DOCUMENT_EXTENSIONS",
    "PROTECTED_INSTRUCTION_NAMES",
    "binary_document_reason",
    "blocked_device",
    "has_binary_extension",
    "homes_for",
    "indus_dirs",
    "is_device_path",
    "is_opaque_document",
    "is_sqlite_sidecar",
    "nt_namespace_error",
    "protected_instruction_name",
    "read_denial",
    "write_denial",
    "write_needs_approval",
]

# -- binary file names ---------------------------------------------------------------------------

_IMAGES = ".png .jpg .jpeg .gif .bmp .ico .webp .tif .tiff .heic .avif .psd"
_MEDIA = ".mp3 .wav .ogg .flac .aac .m4a .opus .wma .aiff .mp4 .mov .avi .mkv .webm .wmv .m4v .mpg"
_ARCHIVES = ".zip .tar .gz .tgz .bz2 .xz .7z .rar .zst .iso .dmg .lz4"
_PROGRAMS = ".exe .dll .so .dylib .bin .o .a .obj .lib .msi .deb .rpm .apk .pyc .pyo .class .jar"
_MORE = ".war .wasm .node .rlib .ttf .otf .woff .woff2 .eot .sqlite .sqlite3 .db .mdb .lockb .blend"
_OFFICE = ".doc .docx .docm .xls .xlsx .xlsm .xlsb .ppt .pptx .pptm .pps .ppsx .odt .ods .odp"

BINARY_EXTENSIONS = frozenset(
    " ".join((_IMAGES, _MEDIA, _ARCHIVES, _PROGRAMS, _MORE, _OFFICE)).split()
)
"""Extensions whose bytes are not text (PDF is left out: its syntax is text)."""

OPAQUE_DOCUMENT_EXTENSIONS = frozenset((_OFFICE + " .rtf .epub").split())
"""Containers a text write can never produce; ``read_file`` shows their extracted text."""

_SQLITE_EXTENSIONS = (".db", ".sqlite", ".sqlite3")
_SQLITE_SIDECARS = ("-wal", "-shm", "-journal")


def _suffix(path: str) -> str:
    name = posixpath.basename(path.replace("\\", "/"))
    dot = name.rfind(".")
    return name[dot:].lower() if dot > 0 or (dot == 0 and name.count(".") > 1) else ""


def is_sqlite_sidecar(path: str) -> bool:
    suffix = _suffix(path)
    return any(suffix == ext + side for ext in _SQLITE_EXTENSIONS for side in _SQLITE_SIDECARS)


def has_binary_extension(path: str) -> bool:
    return _suffix(path) in BINARY_EXTENSIONS or is_sqlite_sidecar(path)


def is_opaque_document(path: str) -> bool:
    return _suffix(path) in OPAQUE_DOCUMENT_EXTENSIONS


# -- raw text checks -----------------------------------------------------------------------------

_NT_PREFIXES = ("\\??\\", "\\\\.\\", "\\\\?\\", "//./", "//?/", "/??/")


def nt_namespace_error(raw: str, *, verb: str = "Access") -> str | None:
    text = raw.strip()
    if not text.startswith(_NT_PREFIXES) and "globalroot" not in text.lower():
        return None
    return (
        f"{verb} refused: {raw!r} uses a Windows NT/device namespace prefix. Such paths skip "
        "normal path handling and can leak credentials just by being opened. Use an ordinary "
        "absolute or relative path."
    )


# -- devices -------------------------------------------------------------------------------------

_DEVICE_FILES = frozenset(
    {
        "/dev/zero",
        "/dev/random",
        "/dev/urandom",
        "/dev/full",
        "/dev/stdin",
        "/dev/stdout",
        "/dev/stderr",
        "/dev/tty",
        "/dev/console",
        "/dev/fd/0",
        "/dev/fd/1",
        "/dev/fd/2",
    }
)
_PROC_LEAKS = re.compile(
    r"^/proc/[^/]+(?:/task/[^/]+)?/"
    r"(?:environ|cmdline|maps|smaps|smaps_rollup|numa_maps|mem|auxv|pagemap|fd/[012])$"
)
_MAX_HOPS = 40


def is_device_path(path: str) -> bool:
    """True for a device or ``/proc`` file a read must never touch (path text only, no I/O)."""
    normal = posixpath.normpath(path) if path else path
    return normal in _DEVICE_FILES or bool(_PROC_LEAKS.match(normal))


def _link_target(link: str) -> str | None:
    """Where the symlink ``link`` points (absolute, normalised), or None when it is not one."""
    try:
        pointed = os.readlink(link)
    except OSError:
        return None
    return os.path.normpath(os.path.join(os.path.dirname(link), pointed))


def blocked_device(path: str, *, follow_links: bool = True) -> bool:
    """``is_device_path`` on ``path``, every symlink hop from it and its real path (host only
    when ``follow_links``)."""
    if is_device_path(path):
        return True
    if not follow_links:
        return False
    hops: list[str] = [os.path.normpath(path)]
    while len(hops) <= _MAX_HOPS:
        nxt = _link_target(hops[-1])
        if nxt is None or nxt in hops:
            break
        if is_device_path(nxt):
            return True
        hops.append(nxt)
    try:
        return is_device_path(os.path.realpath(path))
    except (OSError, ValueError):
        return False


# -- homes ---------------------------------------------------------------------------------------


def _real(path: str) -> str:
    try:
        return os.path.realpath(path)
    except (OSError, ValueError):
        return os.path.normpath(path)


def homes_for(scope: Scope | None) -> list[str]:
    """Every user home a write from this scope can land in (real paths, no duplicates)."""
    found: list[str] = []
    if scope is not None:
        env_home = scope.env.get("HOME")
        if env_home:
            found.append(_real(env_home))
    found.append(_real(str(expand_path_text("~", env={}))))
    return list(dict.fromkeys(found))


def indus_dirs(scope: Scope | None) -> list[str]:
    """The profile home and the Indus root (real paths)."""
    if scope is None:
        return []
    return list(dict.fromkeys(_real(str(p)) for p in (scope.home, scope.root)))


def _under(path: str, directory: str) -> bool:
    return path == directory or path.startswith(directory.rstrip("/") + "/")


# -- reads ---------------------------------------------------------------------------------------

INDUS_SECRET_FILES = ("secrets.json", "secrets.index.json", "secrets.json.lock", ".env")
INDUS_CONTROL_FILES = ("auth.json", "auth.lock", "config.yaml")
_ENV_FILE_OK_SUFFIXES = frozenset({"example", "sample", "template", "dist", "defaults", "schema"})


def _is_secret_env_name(name: str) -> bool:
    lower = name.lower()
    if lower == ".envrc" or lower == ".env":
        return True
    if lower.startswith(".env."):
        return lower.rsplit(".", 1)[-1] not in _ENV_FILE_OK_SUFFIXES
    return False


def read_denial(resolved: str, scope: Scope | None) -> str | None:
    """Why a read of ``resolved`` is refused, or None."""
    for base in indus_dirs(scope):
        for name in INDUS_SECRET_FILES:
            if resolved == posixpath.join(base, name):
                return (
                    f"{resolved} is an Indus secret store; secrets reach the tools that need "
                    "them through the secret store, never through file reads."
                )
        for name in INDUS_CONTROL_FILES:
            if resolved == posixpath.join(base, name):
                return (
                    f"{resolved} is an Indus control file and cannot be read with file tools; "
                    "use `indus config` / `indus auth` to inspect it."
                )
    if _is_secret_env_name(posixpath.basename(resolved)):
        return (
            f"{resolved} is a secret-bearing environment file. Read a committed example such as "
            ".env.example to learn its shape."
        )
    return None


# -- writes --------------------------------------------------------------------------------------

_SYSTEM_PREFIXES = (
    "/etc/",
    "/boot/",
    "/usr/lib/systemd/",
    "/lib/systemd/",
    "/private/etc/",
    "/private/var/db/",
    "/private/var/root/",
)
_SYSTEM_FILES = frozenset({"/var/run/docker.sock", "/run/docker.sock", "/etc/sudoers"})
_HOME_DIRS = (".ssh", ".aws", ".kube", ".config/systemd")
_HOME_FILES = (".netrc",)
_SSH_CONFIG = ".ssh/config"


def write_denial(candidates: Iterable[str], scope: Scope | None) -> str | None:
    """Why a write to any of ``candidates`` is refused outright, or None."""
    homes = homes_for(scope)
    indus = indus_dirs(scope)
    for path in candidates:
        if path.startswith(_SYSTEM_PREFIXES) or path in _SYSTEM_FILES:
            return (
                f"Refusing to write to the system path {path}. Change system files yourself, "
                "or through the terminal tool where the command is approved."
            )
        for home in homes:
            if path == posixpath.join(home, _SSH_CONFIG):
                continue
            for name in _HOME_DIRS:
                if _under(path, posixpath.join(home, name)):
                    return (
                        f"Refusing to write to {path}: it is inside the credential folder ~/{name}."
                    )
            for name in _HOME_FILES:
                if path == posixpath.join(home, name):
                    return f"Refusing to write to {path}: it holds login credentials."
        for base in indus:
            for name in (*INDUS_SECRET_FILES, *INDUS_CONTROL_FILES):
                if path == posixpath.join(base, name):
                    return (
                        f"Refusing to write to {path}: Indus's own secret and control files "
                        "change only through `indus config`, `indus auth` and the secret store."
                    )
    return None


def write_needs_approval(candidates: Iterable[str], scope: Scope | None) -> str | None:
    """A reason when a write may happen only after the person approves it, else None."""
    homes = homes_for(scope)
    for path in candidates:
        if any(path == posixpath.join(home, _SSH_CONFIG) for home in homes):
            return (
                f"{path} is the SSH client config; it can run commands (ProxyCommand, Match "
                "exec), so writing it needs your approval."
            )
    return None


def binary_document_reason(path: str, *, exists: bool) -> str | None:
    """Why plain text must not be written to ``path``, or None."""
    suffix = _suffix(path)
    if is_opaque_document(path):
        return (
            f"Refusing to write plain text to the document {path} ({suffix}): read_file showed "
            "extracted text, not the file's bytes, and text cannot make a valid document. Use a "
            "document library through the terminal instead."
        )
    if is_sqlite_sidecar(path):
        return (
            f"Refusing to write plain text to {path}: it holds raw SQLite pages. Use sqlite3 "
            "through the terminal."
        )
    if exists and (suffix == ".pdf" or has_binary_extension(path)):
        return (
            f"Refusing to overwrite the existing binary file {path} ({suffix}) with text: what "
            "read_file showed is not its bytes. Use a tool that understands the format through "
            "the terminal (a new file with this name is allowed)."
        )
    return None


PROTECTED_INSTRUCTION_NAMES = frozenset({"agents.md", "claude.md", "persona.md", ".cursorrules"})


def protected_instruction_name(
    candidates: Iterable[str], extra_globs: Iterable[str] = ()
) -> str | None:
    """The protected file name a write would touch (any directory, any case), or None."""
    globs = [g.lower() for g in extra_globs if g]
    for path in candidates:
        name = posixpath.basename(path.rstrip("/"))
        lower = name.lower()
        if lower in PROTECTED_INSTRUCTION_NAMES or any(
            fnmatch.fnmatchcase(lower, g) for g in globs
        ):
            return name
    return None
