"""Pick the profile for this run before any app module is imported.

Order: an explicit ``-p NAME`` / ``--profile NAME`` / ``--profile=NAME`` anywhere in argv; else an
``INDUS_HOME`` that already names ``<root>/profiles/<id>``; else the sticky ``active_profile`` file
(skipped for supervised gateway children); else the default profile. The scan skips the values of
value-taking flags and stops at ``--`` and at nested-tail flags (``mcp add --args …``), whose
remaining tokens belong to another program.

This module writes nothing: the caller exports ``INDUS_HOME`` and uses the stripped argv. It may
import only the standard library plus ``foundation.home``, ``foundation.profiles`` and the
stdlib-only ``foundation.errors`` (R04.2); a test checks that in a fresh interpreter.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from indus.foundation import home as home_mod
from indus.foundation import profiles
from indus.foundation.errors import ProfileError, ProfileNotFound, StickyProfileMissing

log = logging.getLogger(__name__)

Source = Literal["flag", "env", "sticky", "default"]
PROFILE_FLAGS = ("-p", "--profile")
SUPERVISED_ENV = "INDUS_GATEWAY_SUPERVISED"

_sticky_note_given = False


@dataclass(frozen=True)
class Selection:
    home: Path
    profile_id: str
    argv: list[str]
    source: Source
    warning: str | None = None


@dataclass(frozen=True)
class _Scan:
    name: str | None
    argv: list[str]


def _scan(
    argv: Sequence[str], value_flags: Collection[str], nested_tail_flags: Collection[str]
) -> _Scan:
    out: list[str] = []
    name: str | None = None
    i = 0
    while i < len(argv):
        token = argv[i]
        if token == "--" or token in nested_tail_flags:
            out.extend(argv[i:])
            break
        if name is None and token in PROFILE_FLAGS and i + 1 < len(argv):
            name = argv[i + 1]
            i += 2
            continue
        if name is None and token.startswith("--profile="):
            name = token.split("=", 1)[1]
            i += 1
            continue
        out.append(token)
        if token in value_flags and i + 1 < len(argv):
            out.append(argv[i + 1])
            i += 2
            continue
        i += 1
    return _Scan(name, out)


def _command_of(argv: Sequence[str], value_flags: Collection[str]) -> str | None:
    i = 0
    while i < len(argv):
        token = argv[i]
        if token == "--":
            return None
        if not token.startswith("-"):
            return token
        i += 2 if token in value_flags else 1
    return None


def _default_root(env: Mapping[str, str], user_home: Callable[[], Path] | None) -> Path:
    return home_mod.root_of(home_mod.resolve_home(env, user_home))


def _sudo_root(
    env: Mapping[str, str],
    euid: Callable[[], int] | None,
    home_of_user: Callable[[str], Path | None] | None,
    user_home: Callable[[], Path] | None,
) -> Path | None:
    if env.get(home_mod.HOME_ENV, "").strip():
        return None
    invoker = home_mod.default_home(env, user_home, euid=euid, home_of_user=home_of_user)
    plain = home_mod.default_home(env, user_home, euid=lambda: -1)
    return invoker if invoker != plain else None


def _named(root: Path, pid: str, sudo_root: Path | None) -> Path:
    if profiles.is_live(root, pid):
        return profiles.profile_home(root, pid)
    if sudo_root is not None and profiles.is_live(sudo_root, pid):
        return profiles.profile_home(sudo_root, pid)
    state = "was deleted" if profiles.is_tombstoned(root, pid) else "does not exist"
    raise ProfileNotFound(f"profile {pid!r} {state}", hint=profiles.LIST_HINT)


def _select(
    argv: Sequence[str],
    env: Mapping[str, str],
    value_flags: Collection[str],
    nested_tail_flags: Collection[str],
    recovery_commands: Collection[str],
    user_home: Callable[[], Path] | None,
    euid: Callable[[], int] | None,
    home_of_user: Callable[[str], Path | None] | None,
) -> Selection:
    global _sticky_note_given
    scan = _scan(argv, value_flags, nested_tail_flags)
    root = _default_root(env, user_home)
    raw_home = env.get(home_mod.HOME_ENV, "").strip()

    if scan.name is not None:
        pid = profiles.normalize_id(scan.name)
        if pid == profiles.DEFAULT_PROFILE:
            return Selection(root, pid, scan.argv, "flag")
        sudo_root = _sudo_root(env, euid, home_of_user, user_home)
        return Selection(_named(root, pid, sudo_root), pid, scan.argv, "flag")

    if raw_home:
        env_home = home_mod.expand_path_text(raw_home, env, user_home)
        if profiles.named_profile_of(env_home) is not None:
            return Selection(env_home, profiles.profile_id_of(env_home), scan.argv, "env")

    if env.get(SUPERVISED_ENV, "").strip() != "1":
        sticky = profiles.read_active(root)
        if sticky != profiles.DEFAULT_PROFILE:
            if profiles.is_live(root, sticky):
                warning = None
                if not raw_home and not _sticky_note_given:
                    _sticky_note_given = True
                    warning = (
                        f"using profile {sticky!r} from {root / profiles.ACTIVE_FILE}; "
                        "run `indus profile use default` to switch back"
                    )
                home = profiles.profile_home(root, sticky)
                return Selection(home, sticky, scan.argv, "sticky", warning)
            if _command_of(scan.argv, value_flags) in recovery_commands:
                return Selection(
                    root,
                    profiles.DEFAULT_PROFILE,
                    scan.argv,
                    "default",
                    f"saved profile {sticky!r} no longer exists; running in the default profile",
                )
            raise StickyProfileMissing(
                f"saved profile {sticky!r} no longer exists",
                hint="switch back with `indus profile use default`",
            )

    home = home_mod.resolve_home(env, user_home)
    return Selection(home, profiles.profile_id_of(home), scan.argv, "default")


def select_profile(
    argv: Sequence[str],
    env: Mapping[str, str],
    *,
    value_flags: Collection[str] = (),
    nested_tail_flags: Collection[str] = ("--args",),
    recovery_commands: Collection[str] = (),
    user_home: Callable[[], Path] | None = None,
    euid: Callable[[], int] | None = None,
    home_of_user: Callable[[str], Path | None] | None = None,
) -> Selection:
    """Decide the profile for ``argv`` (without the program name) and ``env``.

    Raises ``ProfileError`` subclasses for user mistakes (unknown, deleted or invalid profile,
    missing sticky profile). Any other failure falls back to the default profile with a warning:
    a bug in profile selection must never stop startup.
    """
    try:
        return _select(
            argv,
            env,
            value_flags,
            nested_tail_flags,
            recovery_commands,
            user_home,
            euid,
            home_of_user,
        )
    except ProfileError:
        raise
    except Exception as exc:
        stripped = _scan(argv, value_flags, nested_tail_flags).argv
        try:
            home = _default_root(env, user_home)
        except Exception:
            home = home_mod.default_home(env, user_home)
        log.debug("profile selection failed", exc_info=True)
        return Selection(
            home,
            profiles.DEFAULT_PROFILE,
            stripped,
            "default",
            f"profile selection failed ({type(exc).__name__}: {exc}); using the default profile",
        )


def reset_once_flags() -> None:
    """Forget the one-time sticky note (tests)."""
    global _sticky_note_given
    _sticky_note_given = False
