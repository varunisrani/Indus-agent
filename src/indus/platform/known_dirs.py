"""The only tables of well-known directories (install locations, protected folders).

Entries are templates: ``~`` and ``$VAR`` / ``%VAR%`` are expanded at lookup time by
``expand()``, never at import. Every table takes the OS family (default: this host) and is empty
on an OS where that ecosystem does not install, so callers compose tables without OS branches.
A ratchet test fails when one of these literals, or ``shutil.which(``, appears elsewhere under
``src/``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from pathlib import Path

from indus.platform.facts import os_family

FAMILIES = ("linux", "darwin", "win32")


def _family(family: str | None) -> str:
    return family if family is not None else os_family()


def user_local_bin(family: str | None = None) -> tuple[str, ...]:
    return ("%USERPROFILE%/.local/bin",) if _family(family) == "win32" else ("~/.local/bin",)


def cargo_bin(family: str | None = None) -> tuple[str, ...]:
    return ("%USERPROFILE%/.cargo/bin",) if _family(family) == "win32" else ("~/.cargo/bin",)


def node_global_bins(family: str | None = None) -> tuple[str, ...]:
    if _family(family) == "win32":
        return ("%APPDATA%/npm", "%USERPROFILE%/.bun/bin", "%LOCALAPPDATA%/Volta/bin")
    return ("~/.npm-global/bin", "~/.bun/bin", "~/.volta/bin")


def homebrew_bins(family: str | None = None) -> tuple[str, ...]:
    return ("/opt/homebrew/bin", "/usr/local/bin") if _family(family) == "darwin" else ()


def snap_bins(family: str | None = None) -> tuple[str, ...]:
    return ("/snap/bin",) if _family(family) == "linux" else ()


def flatpak_bins(family: str | None = None) -> tuple[str, ...]:
    if _family(family) != "linux":
        return ()
    return ("/var/lib/flatpak/exports/bin", "~/.local/share/flatpak/exports/bin")


def windows_user_program_dirs(family: str | None = None) -> tuple[str, ...]:
    if _family(family) != "win32":
        return ()
    return (
        "%LOCALAPPDATA%/Programs",
        "%USERPROFILE%/scoop/shims",
        "%LOCALAPPDATA%/Microsoft/WinGet/Links",
    )


def mac_application_dirs(family: str | None = None) -> tuple[str, ...]:
    return ("/Applications", "~/Applications") if _family(family) == "darwin" else ()


def mac_protected_dirs(family: str | None = None) -> tuple[str, ...]:
    """Folders macOS guards with a privacy prompt; touching anything inside raises it."""
    if _family(family) != "darwin":
        return ()
    return ("~/Documents", "~/Desktop", "~/Downloads", "~/Library/Mobile Documents")


def vendored_bin(home: Path) -> Path:
    """Our own helper binaries live in ``<home>/bin`` (the home is passed in, never a literal)."""
    return home / "bin"


def command_dirs(family: str | None = None) -> tuple[str, ...]:
    """Where installers put commands outside PATH, in lookup order."""
    fam = _family(family)
    return (
        *user_local_bin(fam),
        *cargo_bin(fam),
        *node_global_bins(fam),
        *homebrew_bins(fam),
        *windows_user_program_dirs(fam),
        *snap_bins(fam),
        *flatpak_bins(fam),
    )


def all_literals() -> frozenset[str]:
    """Every directory literal in these tables for every OS (the ratchet's search list)."""
    tables = (
        user_local_bin,
        cargo_bin,
        node_global_bins,
        homebrew_bins,
        snap_bins,
        flatpak_bins,
        windows_user_program_dirs,
        mac_application_dirs,
        mac_protected_dirs,
    )
    return frozenset(entry for fam in FAMILIES for table in tables for entry in table(fam))


_VAR = re.compile(r"%(?P<win>[A-Za-z_][A-Za-z0-9_]*)%|\$(?:\{(?P<braced>\w+)\}|(?P<bare>\w+))")


def expand(entry: str, env: Mapping[str, str], user_home: Callable[[], Path]) -> Path | None:
    """Expand ``~`` and env references; None when a referenced variable is unset."""
    missing = False

    def sub(match: re.Match[str]) -> str:
        nonlocal missing
        name = match.group("win") or match.group("braced") or match.group("bare")
        value = env.get(name, "")
        if not value:
            missing = True
        return value

    text = _VAR.sub(sub, entry)
    if missing:
        return None
    if text == "~" or text.startswith("~/"):
        text = str(user_home()) + text[1:]
    return Path(text)
