# pyright: strict
"""Host files a sandbox needs, and where they land in it (P11-11).

Sources: the config list ``terminal.credential_files`` (host paths; ``~`` and relative paths
resolve against the user's home in the scope env) and files a skill declares for a session
(``register``). Each usable entry becomes a ``CredentialFile``: the real host path, a path relative
to the sandbox's ``.indus`` dir under ``credentials/`` (home-relative when the file sits in the
user's home, else ``credentials/abs/<absolute path>``), and the mode it gets there.

Refused, with a warning: Indus's own secret stores and control files and secret ``.env`` files
(the rule ``read_file`` applies), anything that is not a regular file, and files another user
owns. A missing file is skipped with one warning per (profile, session). Docker mounts the entries
read-only (``mount_specs``); remote backends push them as upload-only sync items that a sync-back
must never pull (R02.31).
"""

from __future__ import annotations

import logging
import os
import posixpath
import stat
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, cast

from indus.foundation.config.schema import default_for
from indus.foundation.scope import Scope
from indus.safety.path_guards import read_denial

__all__ = [
    "CONFIG_KEY",
    "REMOTE_DIR",
    "CredentialFile",
    "clear",
    "mount_specs",
    "register",
    "resolve",
]

log = logging.getLogger(__name__)

CONFIG_KEY = "terminal.credential_files"
REMOTE_DIR = "credentials"
DEFAULT_MODE = 0o600

_lock = threading.Lock()
_declared: dict[tuple[str, str], dict[str, int]] = {}  # (home, session) -> {raw path: mode}
_warned: set[tuple[str, str, str]] = set()  # (home, session, path) already warned about


@dataclass(frozen=True, slots=True)
class CredentialFile:
    """One file to place in a sandbox: ``host_path`` (real path) → ``.indus/<remote_rel>``."""

    host_path: str
    remote_rel: str
    mode: int = DEFAULT_MODE


def _key(scope: Scope, session_id: str | None) -> tuple[str, str]:
    return (str(scope.home), session_id or "")


def register(
    scope: Scope, session_id: str | None, paths: Iterable[str], *, mode: int = DEFAULT_MODE
) -> None:
    """Files a skill declared for this session (checked when the sandbox asks for them)."""
    with _lock:
        table = _declared.setdefault(_key(scope, session_id), {})
        for raw in paths:
            text = str(raw).strip()
            if text:
                table[text] = mode


def clear(scope: Scope, session_id: str | None) -> None:
    """Forget the session's declared files and warnings (session end, ``/new``)."""
    key = _key(scope, session_id)
    with _lock:
        _declared.pop(key, None)
        for seen in [w for w in _warned if (w[0], w[1]) == key]:
            _warned.discard(seen)


def _configured(scope: Scope) -> list[str]:
    raw = scope.config.get(CONFIG_KEY) if scope.config is not None else None
    if raw is None:
        raw = default_for(CONFIG_KEY, [])
    if not isinstance(raw, list | tuple):
        return []
    return [str(v).strip() for v in cast(Iterable[Any], raw) if isinstance(v, str) and v.strip()]


def _user_home(env: Mapping[str, str]) -> str:
    home = env.get("HOME") or ""
    return home if os.path.isabs(home) else os.path.expanduser("~")


def _expand(raw: str, user_home: str) -> str:
    if raw == "~" or raw.startswith("~/"):
        raw = user_home + raw[1:]
    elif not os.path.isabs(raw):
        raw = os.path.join(user_home, raw)
    return os.path.normpath(raw)


def remote_rel_for(expanded: str, user_home: str) -> str:
    """Where ``expanded`` lands below the sandbox's ``.indus`` dir."""
    home = user_home.rstrip("/")
    if home and expanded.startswith(home + "/"):
        inner = expanded[len(home) + 1 :]
    else:
        inner = "abs/" + expanded.lstrip("/")
    return posixpath.join(REMOTE_DIR, *inner.split(os.sep))


def _warn_once(scope: Scope, session_id: str | None, path: str, message: str) -> None:
    home, session = _key(scope, session_id)
    with _lock:
        if (home, session, path) in _warned:
            return
        _warned.add((home, session, path))
    log.warning("credential file %s: %s", path, message)


def _problem(expanded: str, real: str, scope: Scope, uid: int | None) -> str | None:
    for candidate in dict.fromkeys((expanded, real)):
        denial = read_denial(candidate, scope)
        if denial:
            return f"refused: {denial}"
    try:
        info = os.stat(real)
    except FileNotFoundError:
        return "missing; skipped"
    except OSError as exc:
        return f"cannot be read ({exc.strerror}); skipped"
    if not stat.S_ISREG(info.st_mode):
        return "refused: not a regular file"
    owner = uid if uid is not None else (os.getuid() if hasattr(os, "getuid") else None)
    if owner is not None and info.st_uid != owner:
        return "refused: owned by another user"
    return None


def resolve(
    scope: Scope, session_id: str | None = None, *, uid: int | None = None
) -> list[CredentialFile]:
    """The usable credential files for this profile and session (see the module notes)."""
    user_home = _user_home(scope.env)
    wanted: dict[str, int] = dict.fromkeys(_configured(scope), DEFAULT_MODE)
    with _lock:
        wanted.update(_declared.get(_key(scope, session_id), {}))
    found: dict[str, CredentialFile] = {}
    for raw, mode in wanted.items():
        expanded = _expand(raw, user_home)
        try:
            real = os.path.realpath(expanded)
        except (OSError, ValueError):
            real = expanded
        problem = _problem(expanded, real, scope, uid)
        if problem:
            _warn_once(scope, session_id, raw, problem)
            continue
        if real in found:
            continue
        found[real] = CredentialFile(real, remote_rel_for(expanded, user_home), mode)
    return list(found.values())


def mount_specs(files: Iterable[CredentialFile], container_base: str) -> list[str]:
    """Docker ``-v`` values mounting each file read-only below ``container_base`` (the sandbox's
    ``.indus`` dir)."""
    base = container_base.rstrip("/")
    return [f"{f.host_path}:{base}/{f.remote_rel}:ro" for f in files]
