"""The first call of every entry point: make the process safe, pick the profile, build the scope.

``run(argv, env)`` must happen before the rest of the app is imported. In order it:

1. drops the current dir from ``sys.path`` when a package there would shadow ``indus``;
2. switches stdio to UTF-8 (``errors="replace"``) where the platform default is something else;
3. selects the profile from ``-p/--profile``, ``INDUS_HOME`` or the sticky file
   (``preparse.select_profile``) and writes ``INDUS_HOME`` into ``env``;
4. points ``TMPDIR``/``TMP``/``TEMP`` at the scratch dir unless the user set them;
5. builds the ``.env``-aware env view and the root ``Scope``;
6. creates the home skeleton and seeds the persona file;
7. attaches the profile's config and secret store to the scope (``attach_profile_state``);
8. sets up logging in the requested mode, then prunes idle scratch entries.

``env`` is the process environment by default; tests pass a plain dict so nothing leaks into
the test process. This module and ``preparse`` are the only ones that write process env.
"""

from __future__ import annotations

import codecs
import logging
import os
import sys
import tempfile
from collections.abc import Collection, MutableMapping, Sequence
from dataclasses import replace
from pathlib import Path

from indus.foundation import config, layout
from indus.foundation import log as logsetup
from indus.foundation.clock import SystemClock, system_clock_for
from indus.foundation.envload import build_env
from indus.foundation.home import root_of
from indus.foundation.preparse import Selection, select_profile
from indus.foundation.scope import Scope
from indus.foundation.secrets import open_secret_store

__all__ = [
    "attach_profile_state",
    "ensure_utf8_stdio",
    "harden_sys_path",
    "make_root_scope",
    "run",
    "run_selected",
]

log = logging.getLogger(__name__)

_STDIO = ("stdin", "stdout", "stderr")


def _package_parent() -> Path:
    """The directory that holds the ``indus`` package actually in use."""
    return Path(__file__).resolve().parents[2]


def harden_sys_path(cwd: Path | None = None) -> list[str]:
    """Remove the cwd entries (``''``, ``.``, the cwd itself) from ``sys.path`` when the cwd
    holds an ``indus`` package or module that is not ours. Returns the removed entries."""
    try:
        here = (cwd if cwd is not None else Path(os.getcwd())).resolve()
    except OSError:
        return []  # deleted cwd: nothing there can shadow us
    if not ((here / "indus").is_dir() or (here / "indus.py").is_file()):
        return []
    if here == _package_parent():
        return []
    kept: list[str] = []
    removed: list[str] = []
    for entry in sys.path:
        if entry in ("", "."):
            removed.append(entry)
            continue
        try:
            same = Path(entry).resolve() == here
        except (OSError, ValueError):
            same = False
        (removed if same else kept).append(entry)
    sys.path[:] = kept
    return removed


def ensure_utf8_stdio() -> list[str]:
    """Reconfigure stdin/stdout/stderr to UTF-8 with ``errors="replace"`` where they use another
    encoding. Streams without ``reconfigure`` (test captures, embedded hosts) are skipped.
    Returns the names of the streams changed."""
    changed: list[str] = []
    for name in _STDIO:
        stream = getattr(sys, name, None)
        encoding = getattr(stream, "encoding", None)
        reconfigure = getattr(stream, "reconfigure", None)
        if not isinstance(encoding, str) or reconfigure is None:
            continue
        try:
            if codecs.lookup(encoding).name == "utf-8":
                continue
        except LookupError:
            pass
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue
        changed.append(name)
    return changed


def make_root_scope(selection: Selection, env: MutableMapping[str, str]) -> Scope:
    """The process scope for the selected profile (config and secrets are attached later)."""
    home = Path(selection.home)
    draft = Scope(
        profile_id=selection.profile_id,
        home=home,
        root=root_of(home),
        env=build_env(home, env),
        clock=SystemClock(),
    )
    # The zone comes from the scope's own env view (INDUS_TIMEZONE, possibly set in .env).
    return replace(draft, clock=system_clock_for(draft))


def attach_profile_state(scope: Scope, env: MutableMapping[str, str]) -> Scope:
    """``scope`` with its config view and secret store attached.

    Config is read through the ``.env``-aware view; the view is then rebuilt with the config
    bridges (``terminal.*``, ``security.redact_secrets``), the clock re-resolved (the config may
    name the zone) and the secret store opened as ``security.secret_store`` says. Logging set up
    afterwards redacts the store's values. ``keychain`` with no usable keychain raises
    ``SecretStoreUnavailable``.
    """
    view = config.load(scope)
    with_config = scope.with_config(view).with_env(
        build_env(scope.home, env, config=view.as_dict())
    )
    with_config = replace(with_config, clock=system_clock_for(with_config))
    mode = view.get("security.secret_store", "auto")
    store = open_secret_store(scope.home, scope.profile_id, mode=str(mode), clock=with_config.clock)
    return with_config.with_secrets(store)


def run(
    argv: Sequence[str],
    env: MutableMapping[str, str] | None = None,
    *,
    mode: logsetup.Mode = "cli",
    verbose: bool = False,
    value_flags: Collection[str] = (),
    nested_tail_flags: Collection[str] | None = None,
    recovery_commands: Collection[str] = (),
) -> tuple[Scope, list[str]]:
    """Prepare the process for ``argv``; returns the root scope and argv without the profile
    selector. ``nested_tail_flags`` None keeps ``select_profile``'s own default (``--args``)."""
    scope, rest, _selection = run_selected(
        argv,
        env,
        mode=mode,
        verbose=verbose,
        value_flags=value_flags,
        nested_tail_flags=nested_tail_flags,
        recovery_commands=recovery_commands,
    )
    return scope, rest


def run_selected(
    argv: Sequence[str],
    env: MutableMapping[str, str] | None = None,
    *,
    mode: logsetup.Mode = "cli",
    verbose: bool = False,
    value_flags: Collection[str] = (),
    nested_tail_flags: Collection[str] | None = None,
    recovery_commands: Collection[str] = (),
) -> tuple[Scope, list[str], Selection]:
    """``run``, also returning the profile ``Selection`` (its ``source`` says how the profile was
    picked: flag, env, sticky or default)."""
    target: MutableMapping[str, str] = os.environ if env is None else env
    harden_sys_path()
    ensure_utf8_stdio()

    if nested_tail_flags is None:
        selection = select_profile(
            argv, target, value_flags=value_flags, recovery_commands=recovery_commands
        )
    else:
        selection = select_profile(
            argv,
            target,
            value_flags=value_flags,
            nested_tail_flags=nested_tail_flags,
            recovery_commands=recovery_commands,
        )
    home = Path(selection.home)
    target["INDUS_HOME"] = str(home)
    for name, value in layout.scratch_env(home, target).items():
        target[name] = value
    if sys.platform == "win32":
        target.setdefault("PYTHONIOENCODING", "utf-8")
    if target is os.environ:
        tempfile.tempdir = None  # pick up the new TMPDIR in this process too

    scope = make_root_scope(selection, target)
    layout.ensure_home(scope)  # refuses a deleted profile before anything is read or written
    scope = attach_profile_state(scope, target)
    logsetup.setup(scope, mode, verbose)
    if selection.warning:
        log.warning("%s", selection.warning)
    try:
        layout.prune_scratch(scope, scope.clock)
    except OSError as exc:
        log.warning("scratch prune skipped: %s", exc)
    return scope, list(selection.argv), selection
