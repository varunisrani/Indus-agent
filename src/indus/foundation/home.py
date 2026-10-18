"""Which directory is the home, which root holds it, and owner-only permissions.

Resolution order: (1) the context-local override (multi-profile hosts; never written to
``os.environ``), (2) ``INDUS_HOME`` from the given env with ``~``, ``$VAR`` and ``${VAR}`` expanded
from that same env, (3) the platform default: ``~/.indus`` on POSIX, ``%LOCALAPPDATA%\\indus`` on
Windows (``~/AppData/Local/indus`` without it), with ``INDUS_DATA_DIR_SUFFIX`` appended to the
directory name. Under ``sudo`` the default follows the invoking user, not root.

Nothing here reads the environment or the filesystem at import time; every function takes
``env`` and ``user_home`` (defaults: the process env and ``Path.home`` at call time). This module
is imported by ``foundation.preparse`` and must stay stdlib-only apart from ``foundation.errors``.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from pathlib import Path

from indus.foundation.errors import UnsafeHomeDir

log = logging.getLogger(__name__)

APP_DIR = "indus"
HOME_ENV = "INDUS_HOME"
SUFFIX_ENV = "INDUS_DATA_DIR_SUFFIX"
MODE_ENV = "INDUS_HOME_MODE"
MANAGED_ENV = "INDUS_MANAGED"
MANAGED_MARKER = ".managed"
PROFILES_DIR = "profiles"
DEFAULT_DIR_MODE = 0o700
DEFAULT_FILE_MODE = 0o600

_FALSE_WORDS = frozenset({"", "0", "false", "no", "off"})
_VAR = re.compile(r"\$(?:\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)\}|(?P<bare>[A-Za-z_][A-Za-z0-9_]*))")

_OVERRIDE: ContextVar[Path | None] = ContextVar("indus_home_override", default=None)


# -- context-local override ---------------------------------------------------------------------


def set_home_override(path: str | Path | None) -> Token[Path | None]:
    """Make ``path`` the home for this context (task/thread); returns the reset token."""
    return _OVERRIDE.set(None if path is None else Path(path))


def reset_home_override(token: Token[Path | None]) -> None:
    _OVERRIDE.reset(token)


def home_override() -> Path | None:
    return _OVERRIDE.get()


@contextmanager
def home_override_set(path: str | Path) -> Generator[Path]:
    token = set_home_override(path)
    try:
        yield Path(path)
    finally:
        reset_home_override(token)


# -- resolution ---------------------------------------------------------------------------------


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def _user_home(user_home: Callable[[], Path] | None) -> Callable[[], Path]:
    return Path.home if user_home is None else user_home


def expand_path_text(
    raw: str,
    env: Mapping[str, str] | None = None,
    user_home: Callable[[], Path] | None = None,
) -> Path:
    """Expand a leading ``~`` and ``$VAR``/``${VAR}`` from ``env``; unknown names stay literal."""
    values = _env(env)

    def sub(match: re.Match[str]) -> str:
        name = match.group("braced") or match.group("bare")
        return values.get(name, match.group(0))

    text = _VAR.sub(sub, raw.strip())
    if text == "~" or text.startswith(("~/", "~\\")):
        text = str(_user_home(user_home)()) + text[1:]
    return Path(text)


def _sudo_invoker_home(
    env: Mapping[str, str],
    euid: Callable[[], int] | None,
    home_of_user: Callable[[str], Path | None] | None,
) -> Path | None:
    get_euid = euid if euid is not None else getattr(os, "geteuid", None)
    if get_euid is None or get_euid() != 0:
        return None
    user = env.get("SUDO_USER", "").strip()
    if not user or user == "root":
        return None
    lookup = home_of_user if home_of_user is not None else _passwd_home
    return lookup(user)


def _passwd_home(user: str) -> Path | None:
    try:
        import pwd
    except ImportError:  # Windows
        return None
    try:
        return Path(pwd.getpwnam(user).pw_dir)
    except KeyError:
        return None


def _suffix(env: Mapping[str, str]) -> str:
    suffix = env.get(SUFFIX_ENV, "").strip()
    if any(sep in suffix for sep in ("/", "\\")) or suffix in (".", ".."):
        log.warning("ignoring %s=%r: it must not contain a path separator", SUFFIX_ENV, suffix)
        return ""
    return suffix


def default_home(
    env: Mapping[str, str] | None = None,
    user_home: Callable[[], Path] | None = None,
    *,
    windows: bool | None = None,
    euid: Callable[[], int] | None = None,
    home_of_user: Callable[[str], Path | None] | None = None,
) -> Path:
    """The platform default home (no override, no ``INDUS_HOME``)."""
    values = _env(env)
    is_windows = sys.platform == "win32" if windows is None else windows
    suffix = _suffix(values)
    if is_windows:
        local = values.get("LOCALAPPDATA", "").strip()
        base = Path(local) if local else _user_home(user_home)() / "AppData" / "Local"
        return base / (APP_DIR + suffix)
    invoker = _sudo_invoker_home(values, euid, home_of_user)
    base = invoker if invoker is not None else _user_home(user_home)()
    return base / f".{APP_DIR}{suffix}"


def resolve_home(
    env: Mapping[str, str] | None = None,
    user_home: Callable[[], Path] | None = None,
    *,
    windows: bool | None = None,
    euid: Callable[[], int] | None = None,
    home_of_user: Callable[[str], Path | None] | None = None,
) -> Path:
    """Override, then ``INDUS_HOME``, then the platform default."""
    override = _OVERRIDE.get()
    if override is not None:
        return override
    values = _env(env)
    raw = values.get(HOME_ENV, "").strip()
    if raw:
        return expand_path_text(raw, values, user_home)
    return default_home(values, user_home, windows=windows, euid=euid, home_of_user=home_of_user)


def root_of(home: Path) -> Path:
    """``<x>/profiles/<name>`` → ``<x>``; any other home is its own root."""
    if home.parent.name == PROFILES_DIR and home.name and not home.name.startswith("."):
        return home.parent.parent
    return home


# -- permissions --------------------------------------------------------------------------------


def dir_mode(env: Mapping[str, str] | None = None) -> int:
    """``INDUS_HOME_MODE`` parsed as octal (``0701``, ``701``, ``0o701``); invalid → 0700."""
    raw = _env(env).get(MODE_ENV, "").strip()
    if not raw:
        return DEFAULT_DIR_MODE
    digits = raw.lower().removeprefix("0o")
    try:
        mode = int(digits, 8)
    except ValueError:
        mode = -1
    if not 0 <= mode <= 0o777:
        log.warning("ignoring %s=%r: not an octal mode; using 0700", MODE_ENV, raw)
        return DEFAULT_DIR_MODE
    return mode


def is_managed(home: Path | None, env: Mapping[str, str] | None = None) -> bool:
    """Managed installs (``INDUS_MANAGED`` truthy or ``<home>/.managed``) keep their own modes."""
    flag = _env(env).get(MANAGED_ENV)
    if flag is not None and flag.strip().lower() not in _FALSE_WORDS:
        return True
    return home is not None and (home / MANAGED_MARKER).exists()


def _chmod_supported() -> bool:
    return os.name != "nt"


def _default_in_container() -> bool:
    from indus.platform.facts import is_container  # lazy: preparse must stay light

    return is_container()


def secure_dir(
    path: Path,
    *,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Create ``path`` (and parents) and give it the home dir mode unless the install is managed."""
    mode = dir_mode(env)
    path.mkdir(mode=mode, parents=True, exist_ok=True)
    if _chmod_supported() and not is_managed(home, env):
        os.chmod(path, mode)
    return path


def secure_file(
    path: Path,
    *,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
    in_container: Callable[[], bool] | None = None,
) -> None:
    """chmod 0600, skipped for managed installs and inside containers (shared mounts)."""
    if not _chmod_supported() or is_managed(home, env):
        return
    if (in_container or _default_in_container)():
        return
    os.chmod(path, DEFAULT_FILE_MODE)


def _too_shallow(directory: Path) -> bool:
    # "/" has one part, "/home" and "C:\\Users" two: a filesystem root or a top-level dir.
    return len(directory.parts) < 3


def secure_parent_dir(
    path: Path,
    *,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Restrict ``path``'s parent to the home dir mode; refuse roots and top-level dirs.

    A credential file whose parent is ``/``, a drive root, ``/home`` or ``C:\\Users`` means the
    home was resolved wrongly; chmodding there would lock users out, so this raises instead.
    """
    parent = path.parent.resolve() if path.parent != path else path.resolve()
    if _too_shallow(parent):
        raise UnsafeHomeDir(
            f"refusing to restrict permissions on {parent}: it is a filesystem root or a "
            "top-level directory",
            hint="check INDUS_HOME; it probably points at the wrong place",
        )
    if _chmod_supported() and not is_managed(home, env):
        os.chmod(parent, dir_mode(env))
    return parent
