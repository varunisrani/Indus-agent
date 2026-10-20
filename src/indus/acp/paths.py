"""Editor folders: translating a Windows-host cwd under WSL, and comparing two folders (P19-05/07).

An editor running on Windows may hand a WSL-hosted agent its workspace as a drive path
(``E:\\Projects\\x``) or as a UNC path into the distro (``\\\\wsl.localhost\\Ubuntu\\home\\me``,
also the older ``\\\\wsl$\\Ubuntu\\…``). Tools and stored sessions need the POSIX form, so
``translate_cwd`` maps both when this process runs under WSL and leaves every other path alone.

``comparable`` is what two folders are compared by (session listing filters, "did the editor open
this session from another folder"): ``~`` expanded, a drive path in its ``/mnt/<drive>/`` form (the
drive letter lowered, also when it was already a mount path), then ``realpath`` so symlinked
spellings agree — macOS reports ``/var/…`` and ``/tmp/…`` for folders stored as ``/private/var/…``
and ``/private/tmp/…`` (R09.16). A folder that does not exist compares by its normalised spelling.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from typing import Final

__all__ = ["comparable", "drive_to_mount", "same_folder", "translate_cwd", "unc_to_posix"]

_DRIVE: Final = re.compile(r"^([A-Za-z]):[\\/](.*)$")
_BARE_DRIVE: Final = re.compile(r"^([A-Za-z]):$")
_UNC: Final = re.compile(r"^\\\\wsl(?:\.localhost|\$)\\[^\\]+(?:\\(.*))?$", re.IGNORECASE)
_MOUNT: Final = re.compile(r"^/mnt/([A-Za-z])(/.*)?$")


def drive_to_mount(path: str) -> str | None:
    """``E:\\a\\b`` → ``/mnt/e/a/b`` (``E:`` alone → ``/mnt/e``); None for anything else."""
    text = path.strip()
    bare = _BARE_DRIVE.match(text)
    if bare is not None:
        return f"/mnt/{bare.group(1).lower()}"
    found = _DRIVE.match(text)
    if found is None:
        return None
    rest = found.group(2).replace("\\", "/").strip("/")
    base = f"/mnt/{found.group(1).lower()}"
    return f"{base}/{rest}" if rest else base


def unc_to_posix(path: str) -> str | None:
    """``\\\\wsl.localhost\\Distro\\home\\me`` → ``/home/me``; None for anything else."""
    found = _UNC.match(path.strip().replace("/", "\\"))
    if found is None:
        return None
    rest = (found.group(1) or "").replace("\\", "/").strip("/")
    return "/" + rest


def _running_in_wsl() -> bool:
    from indus.platform.facts import is_wsl

    try:
        return is_wsl()
    except Exception:
        return False


def translate_cwd(cwd: str, *, wsl: bool | Callable[[], bool] | None = None) -> str:
    """The cwd tools and the store should use: under WSL a Windows drive or ``wsl.localhost``
    path becomes POSIX; otherwise ``cwd`` unchanged. ``wsl`` overrides detection (tests)."""
    text = str(cwd)
    under_wsl = wsl() if callable(wsl) else wsl
    if under_wsl is None:
        under_wsl = _running_in_wsl()
    if not under_wsl:
        return text
    for translate in (unc_to_posix, drive_to_mount):
        found = translate(text)
        if found is not None:
            return found
    return text


def comparable(cwd: str | None) -> str:
    """The spelling two folders are compared by (see the module notes)."""
    text = os.path.expanduser(str(cwd or "").strip() or ".")
    mounted = drive_to_mount(text)
    if mounted is not None:
        text = mounted
    else:
        found = _MOUNT.match(text)
        if found is not None:
            text = f"/mnt/{found.group(1).lower()}{found.group(2) or ''}"
    try:
        return os.path.realpath(text)
    except (OSError, ValueError):
        return os.path.normpath(text)


def same_folder(a: str | None, b: str | None) -> bool:
    return comparable(a) == comparable(b)
