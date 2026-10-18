"""What ``indus --version`` prints: the version, how this copy was installed, and where.

Standard library only, and nothing is read beyond a few file probes: the version path runs before
the profile, config and logging are set up and must finish within its time budget (D-04.4).

Install method, first match wins:

1. ``.install_method`` stamp in the install tree (written by the installers, P23);
2. ``nix`` when the package lives under ``/nix/store``;
3. ``docker`` when the process runs inside a container;
4. ``git`` for a source tree that is a git checkout, ``dev`` for a source tree without one;
5. ``package`` for an installed wheel.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path

__all__ = ["INSTALL_STAMP", "install_dir", "install_method", "version_lines"]

INSTALL_STAMP = ".install_method"
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


def version_lines(package_dir: Path | None = None) -> list[str]:
    """The lines ``indus --version`` prints."""
    from indus import __version__

    return [
        f"Indus Agent {__version__}",
        f"Install method: {install_method(package_dir)}",
        f"Install directory: {install_dir(package_dir)}",
        f"Python: {sys.version.split()[0]}",
    ]
