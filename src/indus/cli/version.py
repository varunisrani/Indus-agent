"""What ``indus --version`` prints: the version, how this copy was installed, and where.

Standard library only, and nothing is read beyond a few file probes: the version path runs before
the profile, config and logging are set up and must finish within its time budget (D-04.4).

Install method, first match wins:

1. ``.install_method`` stamp in the install tree (written by the installers, P23);
2. ``nix`` when the package lives under ``/nix/store``;
3. ``docker`` when the process runs inside a container;
4. ``git`` for a source tree that is a git checkout, ``dev`` for a source tree without one;
5. ``package`` for an installed wheel.

When ``indus update --check`` ran in the last 24 hours and found a newer release (and passive
notices are on, ``updates.check``), one more line names it. That is read from the small cache file
the check writes (``<root>/cache/update-check.json``); ``--version`` itself never goes to the
network (P23-19, I45).
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

__all__ = [
    "INSTALL_STAMP",
    "UPDATE_CACHE",
    "install_dir",
    "install_method",
    "update_line",
    "version_lines",
]

INSTALL_STAMP = ".install_method"
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


def version_lines(package_dir: Path | None = None, *, root: Path | None = None) -> list[str]:
    """The lines ``indus --version`` prints."""
    from indus import __version__

    lines = [
        f"Indus Agent {__version__}",
        f"Install method: {install_method(package_dir)}",
        f"Install directory: {install_dir(package_dir)}",
        f"Python: {sys.version.split()[0]}",
    ]
    notice = update_line(root)
    if notice is not None:
        lines.append(notice)
    return lines
