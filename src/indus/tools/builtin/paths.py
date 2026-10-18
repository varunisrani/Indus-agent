# pyright: strict
"""Turning the path a model typed into the one absolute path a file tool works on (P05-15).

Relative paths anchor on the task's terminal working directory: the environment's cwd when one is
live, else the configured ``terminal.cwd``, and the process cwd only when neither names a real
directory. Values that only look like a cwd (``""``, ``.``, ``./``, ``auto``, ``cwd``) count as
unset, and a relative anchor is no anchor: it would silently land edits wherever the process
happens to run.

``~`` means the home the scope gives child processes (its ``HOME``), so a cron or gateway run
resolves ``~/notes.md`` to the same file an interactive run does. ``~user`` is left alone.

On the host the result is ``realpath``-ed, so symlinks are seen through before any guard looks at
it. Paths inside a sandbox (container paths, remote shells) are only normalised: following a host
symlink of the same name would point somewhere else entirely.
"""

from __future__ import annotations

import os
import posixpath

from indus.foundation.home import expand_path_text
from indus.foundation.scope import Scope

__all__ = [
    "PLACEHOLDER_CWDS",
    "base_dir",
    "clean_cwd",
    "configured_cwd",
    "expand_tilde",
    "resolve",
    "user_home",
]

PLACEHOLDER_CWDS = frozenset({"", ".", "./", "auto", "cwd"})


def user_home(scope: Scope | None) -> str:
    """The home children of this scope see (``HOME`` in its env view), else the process user's."""
    if scope is not None:
        home = scope.env.get("HOME")
        if home:
            return home
    return str(expand_path_text("~", env={}))


def expand_tilde(raw: str, scope: Scope | None) -> str:
    if raw == "~":
        return user_home(scope)
    if raw.startswith("~/"):
        return user_home(scope).rstrip("/") + raw[1:]
    return raw


def clean_cwd(raw: object, scope: Scope | None = None) -> str | None:
    """``raw`` as an absolute directory text, or None when it is a placeholder or relative."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.lower() in PLACEHOLDER_CWDS:
        return None
    text = expand_tilde(text, scope)
    return text if posixpath.isabs(text) else None


def configured_cwd(scope: Scope | None) -> str | None:
    if scope is None or scope.config is None:
        return None
    return clean_cwd(scope.config.get("terminal.cwd"), scope)


def base_dir(scope: Scope | None, env_cwd: str | None = None, *, host: bool = True) -> str:
    """The absolute directory relative paths anchor on (see the module notes)."""
    chosen = clean_cwd(env_cwd, scope) or configured_cwd(scope)
    if chosen is None:
        chosen = os.getcwd()
    return os.path.normpath(chosen) if host else posixpath.normpath(chosen)


def resolve(raw: str, *, base: str, scope: Scope | None, host: bool = True) -> str:
    """``raw`` made absolute against ``base``; host paths have their symlinks resolved."""
    text = expand_tilde(raw.strip(), scope)
    if host:
        joined = text if os.path.isabs(text) else os.path.join(base, text)
        return os.path.realpath(joined)
    joined = text if posixpath.isabs(text) else posixpath.join(base, text)
    return posixpath.normpath(joined)
