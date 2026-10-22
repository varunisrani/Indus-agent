"""What ``indus --version`` prints: the version, how this copy was installed, and where.

Standard library only, and nothing is read beyond a few file probes: the version path runs before
the profile, config and logging are set up and must finish within its time budget (D-04.4).

Install method, first match wins:

1. ``.install_method`` stamp in the install tree (written by the installers, P23);
2. ``nix`` when the package lives under ``/nix/store``;
3. ``docker`` when the process runs inside a container;
4. ``git`` for a source tree that is a git checkout, ``dev`` for a source tree without one;
5. ``package`` for an installed wheel.

The version on the first line is the package's own (``0.0.0`` in a source tree, the stamped one in
a release build), except in a git checkout whose HEAD is a release: then it is that release
(``checkout_release``, F-4). The release is found from file probes only, each checked against the
HEAD commit read from ``.git``: the checkout's release tags (``packed-refs`` with their peeled
commits, loose tag refs, a loose annotated tag object peeled with ``zlib``), the installer's
``.indus-bootstrap.json`` (``version`` + ``commit``) and the newest ``indus update`` receipts
(``to_version`` + ``to_commit``). A HEAD on no release keeps the package's version.

When ``indus update --check`` ran in the last 24 hours and found a newer release (and passive
notices are on, ``updates.check``), one more line names it. That is read from the small cache file
the check writes (``<root>/cache/update-check.json``); ``--version`` itself never goes to the
network (P23-19, I45).
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

__all__ = [
    "BOOTSTRAP_MARKER",
    "INSTALL_STAMP",
    "UPDATE_RECEIPTS",
    "checkout_release",
    "head_commit",
    "UPDATE_CACHE",
    "install_dir",
    "install_method",
    "update_line",
    "version_lines",
]

INSTALL_STAMP = ".install_method"
BOOTSTRAP_MARKER = ".indus-bootstrap.json"
UPDATE_RECEIPTS = ("logs", "update_receipts")
_RECEIPTS_READ = 20
_TAGS_READ = 4000
_OBJECT_CAP = 64 * 1024
_HEX = frozenset("0123456789abcdef")
UPDATE_CACHE = ("cache", "update-check.json")
_CHECK_FRESH = timedelta(hours=24)
_CONTAINER_MARKERS = ("/.dockerenv", "/run/.containerenv")


def _package_dir() -> Path:
    return Path(__file__).resolve().parents[1]


def install_dir(package_dir: Path | None = None) -> Path:
    """The source tree root for a checkout (``<root>/src/indus``), else the package's parent."""
    pkg = package_dir if package_dir is not None else _package_dir()
    if pkg.parent.name == "src" and (pkg.parent.parent / "pyproject.toml").is_file():
        return pkg.parent.parent
    return pkg.parent


def _read_stamp(root: Path) -> str | None:
    try:
        text = (root / INSTALL_STAMP).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None
    word = text.strip().lower()
    return word or None


def _in_container(exists: Callable[[str], bool]) -> bool:
    return any(exists(marker) for marker in _CONTAINER_MARKERS)


def install_method(
    package_dir: Path | None = None, *, exists: Callable[[str], bool] = os.path.exists
) -> str:
    """How this copy was installed (see the module notes)."""
    pkg = package_dir if package_dir is not None else _package_dir()
    root = install_dir(pkg)
    stamp = _read_stamp(root)
    if stamp:
        return stamp
    if str(pkg).startswith("/nix/store/"):
        return "nix"
    if _in_container(exists):
        return "docker"
    if root != pkg.parent:
        return "git" if (root / ".git").exists() else "dev"
    return "package"


def _default_root() -> Path | None:
    from indus.foundation.home import resolve_home, root_of

    try:
        return root_of(resolve_home())
    except Exception:
        return None


def update_line(root: Path | None = None, *, now: datetime | None = None) -> str | None:
    """ "Update available: ..." from a recent ``indus update --check``, else None."""
    base = root if root is not None else _default_root()
    if base is None:
        return None
    try:
        data = json.loads(base.joinpath(*UPDATE_CACHE).read_text(encoding="utf-8"))
        checked = datetime.fromisoformat(str(data["checked_at"]))
    except (OSError, ValueError, TypeError, KeyError):
        return None
    if not isinstance(data, dict) or data.get("schema") != 1:
        return None
    if data.get("available") is not True or data.get("notices") is False:
        return None
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=UTC)
    moment = now if now is not None else datetime.now(UTC)
    if not timedelta(0) <= moment - checked <= _CHECK_FRESH:
        return None
    target, channel = data.get("target"), data.get("channel")
    if not isinstance(target, str) or not isinstance(channel, str):
        return None
    return f"Update available: {target} on {channel} (run `indus update`)"


# -- the release a checkout is on (F-4) ------------------------------------------------------------


def _read(path: Path, limit: int = 1 << 20) -> str | None:
    try:
        with path.open("rb") as handle:
            return handle.read(limit).decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _object_id(text: str | None) -> str | None:
    word = (text or "").strip().lower()
    return word if len(word) in (40, 64) and set(word) <= _HEX else None


def _git_dirs(tree: Path) -> tuple[Path, Path] | None:
    """``(git_dir, common_dir)`` for a checkout (a linked worktree's ``.git`` is a file)."""
    dot = tree / ".git"
    if dot.is_dir():
        return dot, dot
    text = _read(dot, 4096) if dot.is_file() else None
    if text is None or not text.startswith("gitdir:"):
        return None
    git_dir = (tree / text[len("gitdir:") :].strip()).resolve()
    common_text = _read(git_dir / "commondir", 4096)
    common = (git_dir / common_text.strip()).resolve() if common_text else git_dir
    return (git_dir, common) if git_dir.is_dir() else None


def _packed_refs(common: Path) -> dict[str, tuple[str, str | None]]:
    """``ref -> (object, peeled commit or None)`` from ``packed-refs``."""
    refs: dict[str, tuple[str, str | None]] = {}
    last: str | None = None
    for line in (_read(common / "packed-refs", 16 << 20) or "").splitlines():
        if line.startswith("^") and last is not None:
            refs[last] = (refs[last][0], _object_id(line[1:]))
            continue
        sha, _sep, name = line.partition(" ")
        if _object_id(sha) and name:
            refs[name] = (sha.lower(), None)
            last = name
        else:
            last = None
    return refs


def head_commit(tree: Path) -> str | None:
    """The commit a checkout's HEAD names, from ``.git`` files only (None when unreadable)."""
    dirs = _git_dirs(tree)
    if dirs is None:
        return None
    git_dir, common = dirs
    head = (_read(git_dir / "HEAD", 4096) or "").strip()
    if not head.startswith("ref:"):
        return _object_id(head)
    ref = head[len("ref:") :].strip()
    if not ref.startswith("refs/") or ".." in ref:
        return None
    loose = _object_id(_read(common / ref, 4096))
    if loose is not None:
        return loose
    packed = _packed_refs(common).get(ref)
    return None if packed is None else packed[0]


def _peel_loose(common: Path, sha: str) -> str | None:
    """The object a loose annotated tag points at (None when not a loose tag object)."""
    import zlib

    try:
        with (common / "objects" / sha[:2] / sha[2:]).open("rb") as handle:
            data = zlib.decompressobj().decompress(handle.read(_OBJECT_CAP), _OBJECT_CAP)
    except (OSError, zlib.error):
        return None
    header, _nul, body = data.partition(b"\0")
    if not header.startswith(b"tag "):
        return None
    first = body.split(b"\n", 1)[0].decode("ascii", "replace")
    return _object_id(first[len("object ") :]) if first.startswith("object ") else None


def _tag_releases(tree: Path, head: str) -> list[str]:
    from indus.foundation.versions import parse_tag

    dirs = _git_dirs(tree)
    if dirs is None:
        return []
    common = dirs[1]
    refs = {
        name[len("refs/tags/") :]: pair
        for name, pair in _packed_refs(common).items()
        if name.startswith("refs/tags/v")
    }
    loose_dir = common / "refs" / "tags"
    try:
        loose = sorted(loose_dir.glob("v*"))[:_TAGS_READ]
    except OSError:
        loose = []
    for path in loose:
        sha = _object_id(_read(path, 4096)) if path.is_file() else None
        if sha is not None:
            refs[path.name] = (sha, None)
    found: list[str] = []
    for name, (sha, peeled) in refs.items():
        if parse_tag(name) is None:
            continue
        if sha == head or peeled == head or (peeled is None and _peel_loose(common, sha) == head):
            found.append(name[1:])
    return found


def _recorded_releases(tree: Path, head: str, root: Path | None) -> list[str]:
    """Releases the installer's marker and the newest update receipts record for ``head``."""
    found: list[str] = []
    rows: list[tuple[object, object]] = []
    marker = _read(tree / BOOTSTRAP_MARKER, 1 << 16)
    if marker is not None:
        with contextlib.suppress(OSError, ValueError, TypeError):
            data = json.loads(marker)
            if isinstance(data, dict):
                rows.append((data.get("commit"), data.get("version")))  # type: ignore[union-attr]
    base = root if root is not None else _default_root()
    if base is not None:
        with contextlib.suppress(OSError, ValueError, TypeError):
            receipts = sorted(base.joinpath(*UPDATE_RECEIPTS).glob("*.json"), reverse=True)
            for path in receipts[:_RECEIPTS_READ]:
                text = _read(path, 1 << 20)
                with contextlib.suppress(OSError, ValueError, TypeError):
                    data = json.loads(text or "")
                    if isinstance(data, dict):
                        rows.append((data.get("to_commit"), data.get("to_version")))  # type: ignore[union-attr]
    for commit, version in rows:
        if isinstance(commit, str) and isinstance(version, str) and commit.lower() == head:
            found.append(version)
    return found


def checkout_release(tree: Path, *, root: Path | None = None) -> str | None:
    """The release a git checkout's HEAD is on (the newest when several name it), or None."""
    from indus.foundation.versions import parse_version

    head = head_commit(tree)
    if head is None:
        return None
    names = _tag_releases(tree, head) or _recorded_releases(tree, head, root)
    versions = [v for v in (parse_version(name) for name in names) if v is not None]
    return str(max(versions)) if versions else None


def version_lines(package_dir: Path | None = None, *, root: Path | None = None) -> list[str]:
    """The lines ``indus --version`` prints."""
    from indus import __version__

    tree = install_dir(package_dir)
    shown = __version__
    if tree != (package_dir if package_dir is not None else _package_dir()).parent:
        shown = checkout_release(tree, root=root) or __version__
    lines = [
        f"Indus Agent {shown}",
        f"Install method: {install_method(package_dir)}",
        f"Install directory: {install_dir(package_dir)}",
        f"Python: {sys.version.split()[0]}",
    ]
    notice = update_line(root)
    if notice is not None:
        lines.append(notice)
    return lines
