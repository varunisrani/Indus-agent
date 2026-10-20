"""``indus``: prepare the process in a fixed order, then run the click app.

Order (R04.2, R04.13, R04.14), each step before anything that depends on it:

0. a bare ``-V``/``--version`` (profile flags aside) prints the version block from stdlib-only
   code and returns; nothing else is imported;
1. the session flags are normalised (``app.rewrite_session_flags``) and the value-taking flag set
   is read from the live app (hand-kept fallback in ``args``);
2. provider key names are registered as managed ``.env`` names, so a key removed from the
   profile's ``.env`` is cleared from the view;
3. ``--yolo`` (found by a scan that skips flag values and stops at ``--``) is written as
   ``INDUS_YOLO_MODE=1`` into the environment the scope is built from — before any tool or
   approval module is imported;
4. ``bootstrap.run``: profile pre-parse and argv stripping, ``.env`` (the home's file wins over the
   shell), the ``security.redact_secrets`` bridge, then logging in ``cli`` mode (``gui`` for the
   dashboard/desktop commands);
5. the scope's env view is published to the process env (one profile per CLI process), ``--yolo``
   is laid over the view again so a ``.env`` line cannot cancel it, and the provider key names are
   declared blocked for child processes;
6. the click app runs; an int result is the exit code. A one-shot run then leaves through
   ``os._exit`` after flushing and shutting logging down.

A named profile that does not exist (or a saved one that was deleted) prints one line and exits 1;
any other failure inside profile selection is a warning and the default profile is used.
"""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Collection, MutableMapping, Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from indus.cli.args import HostOverrides

__all__ = ["GUI_COMMANDS", "RECOVERY_COMMANDS", "STDIO_COMMANDS", "find_flag", "main"]

GUI_COMMANDS = frozenset({"dashboard", "serve", "gui", "desktop"})
STDIO_COMMANDS = frozenset({"tui-bridge", "acp"})  # stdout carries protocol frames only
RECOVERY_COMMANDS = frozenset({"profile", "doctor", "config"})
_VERSION_FLAGS = ("-V", "--version")
_PROFILE_FLAGS = ("-p", "--profile")
_YOLO_ENV = "INDUS_YOLO_MODE"


def _without_profile(argv: Sequence[str]) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(argv):
        token = argv[i]
        if token in _PROFILE_FLAGS:
            i += 2
            continue
        if token.startswith("--profile="):
            i += 1
            continue
        out.append(token)
        i += 1
    return out


def _fast_version(argv: Sequence[str]) -> bool:
    rest = _without_profile(argv)
    if len(rest) != 1 or rest[0] not in _VERSION_FLAGS:
        return False
    from indus.cli.version import version_lines

    sys.stdout.write("\n".join(version_lines()) + "\n")
    sys.stdout.flush()
    return True


def find_flag(argv: Sequence[str], flag: str, value_flags: Collection[str]) -> bool:
    """Is ``flag`` itself in ``argv`` (not as another flag's value, not after ``--``)?"""
    i = 0
    while i < len(argv):
        token = argv[i]
        if token == "--":
            return False
        if token == flag:
            return True
        i += 2 if token in value_flags else 1
    return False


def _first_command(argv: Sequence[str], value_flags: Collection[str]) -> str | None:
    i = 0
    while i < len(argv):
        token = argv[i]
        if token == "--":
            return None
        if not token.startswith("-"):
            return token
        i += 2 if token in value_flags else 1
    return None


def _value_flags() -> frozenset[str]:
    from indus.cli.args import CHAT_VALUE_FLAGS, FALLBACK_VALUE_FLAGS

    try:
        from indus.cli.app import value_flag_sets

        required, _optional = value_flag_sets()
        return required | CHAT_VALUE_FLAGS | frozenset(_PROFILE_FLAGS)
    except Exception:
        return FALLBACK_VALUE_FLAGS | CHAT_VALUE_FLAGS


class _EarlyRecords:
    """Holds log records made before logging is set up (the keychain fallback note, a profile
    warning) and hands them to the real handlers afterwards. Without it Python prints them on
    stderr, which a one-shot run must keep clean."""

    def __init__(self) -> None:
        import logging

        self._logging = logging
        self.records: list[logging.LogRecord] = []
        self.handler = logging.Handler()
        self.handler.emit = self.records.append  # type: ignore[method-assign]

    def __enter__(self) -> _EarlyRecords:
        self._logging.getLogger().addHandler(self.handler)
        return self

    def __exit__(self, *exc: object) -> None:
        self._logging.getLogger().removeHandler(self.handler)

    def replay(self) -> None:
        for record in self.records:
            self._logging.getLogger(record.name).handle(record)
        self.records.clear()


def _hard_exit(code: int) -> None:
    from indus.foundation import log as logsetup

    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(OSError, ValueError):
            stream.flush()
    with contextlib.suppress(Exception):  # logging is already gone; nothing left to tell
        logsetup.shutdown()
    os._exit(code)


def main(
    argv: Sequence[str] | None = None,
    *,
    env: MutableMapping[str, str] | None = None,
    overrides: HostOverrides | None = None,
) -> int:
    """Run ``indus`` with ``argv`` (default ``sys.argv[1:]``) and return the exit code.

    ``env`` defaults to the process environment; tests pass a dict so nothing leaks. The real
    process is the call with neither (the console script): only then is the env view published
    to ``os.environ`` and a one-shot run hard-exits.
    """
    process = argv is None and env is None
    args = list(sys.argv[1:] if argv is None else argv)
    if _fast_version(args):
        return 0
    target: MutableMapping[str, str] = os.environ if env is None else env

    from indus.cli.app import rewrite_session_flags

    args = rewrite_session_flags(args)
    value_flags = _value_flags()

    from indus.providers import bundled

    bundled.register_managed_env_names()
    yolo = find_flag(args, "--yolo", value_flags)
    if yolo:
        target[_YOLO_ENV] = "1"

    from indus.foundation import bootstrap
    from indus.foundation.errors import IndusError

    command = _first_command(_without_profile(args), value_flags)
    mode = "gui" if command in GUI_COMMANDS else "stdio" if command in STDIO_COMMANDS else "cli"
    early = _EarlyRecords()
    try:
        with early:
            scope, args, selection = bootstrap.run_selected(
                args,
                target,
                mode=mode,
                value_flags=value_flags,
                recovery_commands=RECOVERY_COMMANDS,
            )
    except IndusError as exc:
        hint = f" ({exc.hint})" if exc.hint else ""
        sys.stderr.write(f"indus: {exc.message}{hint}\n")
        return 1
    early.replay()

    from indus.foundation.envload import publish_to_process

    if target is os.environ:
        publish_to_process(scope.env)
    else:
        publish_to_process(scope.env, target)
    if yolo:
        scope = scope.with_env(scope.env.with_overlay({_YOLO_ENV: "1"}))

    from indus.safety.child_env import register_blocked_names

    register_blocked_names("providers", bundled.env_var_names())

    from indus.backends.vendor_env import register_vendor_names

    register_vendor_names()  # sandbox vendor tokens never reach a child process (P21-11)

    from indus.cli.app import dispatch
    from indus.cli.args import CliState

    state = CliState(
        scope=scope,
        env=target,
        process=process,
        overrides=overrides,
        selected_by=selection.source,
    )
    code = dispatch(args, state)
    if state.hard_exit and process:
        _hard_exit(code)
    return code
