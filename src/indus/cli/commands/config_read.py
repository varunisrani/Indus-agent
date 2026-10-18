"""``indus config``: the group, and its read side (``show``, ``get``, ``path``, ``env-path``,
``edit``).

Printed values bypass log redaction and end up in agent transcripts when the agent runs these
commands, so ``show`` and ``get`` mask credentials unless ``--raw`` is given (R04.15): strings
under credential-shaped names become ``abcd…wxyz``, everything else goes through the redactor, and
``${NAME}`` references stay as written. ``edit`` creates a missing file through the P01 writer,
which writes the version stamp and the commented example, never default values (R04.4).
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import click

from indus.cli.commands._support import err, fresh_view, interactive, secret_store, state_of
from indus.cli.config_coerce import (
    check_key,
    is_secret_key,
    known_roots,
    mask_tree,
    mask_value,
    route_for,
    show_value,
)
from indus.cli.lazy import LazyGroup
from indus.foundation import yamlio
from indus.foundation.config import keys, loader, writer
from indus.foundation.config.loader import ConfigView
from indus.foundation.envfile import read_env_file
from indus.foundation.envload import env_file_path
from indus.foundation.errors import IndusError
from indus.foundation.home import is_managed
from indus.foundation.scope import Scope
from indus.foundation.secrets import SecretStoreError
from indus.platform.locate import which

__all__ = [
    "CONFIG_COMMANDS",
    "config_command",
    "edit_command",
    "editor_argv",
    "env_path_command",
    "get_command",
    "lookup_env_value",
    "path_command",
    "run_editor",
    "seed_config",
    "settings_file",
    "show_command",
]

_HERE = "indus.cli.commands.config_read"
_WRITE = "indus.cli.commands.config_write"
_CHECK = "indus.cli.commands.config_check"

CONFIG_COMMANDS: tuple[tuple[str, str, str], ...] = (
    ("show", f"{_HERE}:show_command", "Print the effective settings (credentials masked)."),
    ("get", f"{_HERE}:get_command", "Print one setting or stored key (masked unless --raw)."),
    ("set", f"{_WRITE}:set_command", "Change a setting, or store a key in the keychain or .env."),
    ("unset", f"{_WRITE}:unset_command", "Remove a setting or a stored key."),
    ("edit", f"{_HERE}:edit_command", "Open the settings file in your editor."),
    ("check", f"{_CHECK}:check_command", "Report problems in the settings file."),
    ("migrate", f"{_CHECK}:migrate_command", "Bring the settings file up to this version."),
    ("path", f"{_HERE}:path_command", "Print the path of the settings file."),
    ("env-path", f"{_HERE}:env_path_command", "Print the path of the profile's .env file."),
)

_FALLBACK_EDITORS = ("nano", "vim", "vi")
_WINDOWS_EDITORS = ("notepad",)


def settings_file(scope: Scope) -> Path:
    return loader.config_path(scope)


# -- group ----------------------------------------------------------------------------------------


@click.group(
    "config",
    cls=LazyGroup,
    registry=CONFIG_COMMANDS,
    invoke_without_command=True,
    help="Show and change settings. With no subcommand, the same as `indus config show`.",
)
@click.pass_context
def config_command(ctx: click.Context) -> int:
    if ctx.invoked_subcommand is not None:
        return 0
    return _show(state_of(ctx).scope)


# -- show -----------------------------------------------------------------------------------------


def _store_label(scope: Scope, view: ConfigView) -> str:
    try:
        store = secret_store(scope, view)
    except IndusError as exc:
        return f"not available ({exc.message})"
    if store.backend_name == "keychain":
        return "the OS keychain"
    return f"an owner-only file in {scope.home}"


def _show(scope: Scope) -> int:
    view = fresh_view(scope)
    where = settings_file(scope)
    if view.error is not None:
        served = "built-in settings" if view.failed else "the last settings that could be read"
        err(f"Note: {where} could not be read ({view.error}); showing {served}.")
    present = where.is_file()
    click.echo(f"Settings file: {where}" + ("" if present else " (not created yet)"))
    click.echo(f".env file:     {env_file_path(scope.home)}")
    click.echo(f"Keys stored in {_store_label(scope, view)}.")
    click.echo("")
    click.echo(yamlio.dump_plain(mask_tree(view.as_dict(), scope)).rstrip("\n"))
    return 0


@click.command("show", help="Print the effective settings with every credential masked.")
@click.pass_context
def show_command(ctx: click.Context) -> int:
    return _show(state_of(ctx).scope)


# -- get ------------------------------------------------------------------------------------------


def lookup_env_value(scope: Scope, view: ConfigView, name: str) -> str | None:
    """An env-shaped name's value: the secret store (credentials), then ``.env``, then the
    scope's environment."""
    if route_for(name) == "secret":
        try:
            stored = secret_store(scope, view).get(name.upper())
        except (SecretStoreError, IndusError) as exc:
            err(f"Note: the secret store could not be read ({exc}).")
            stored = None
        if stored is not None:
            return stored
        name = name.upper()
    from_file = read_env_file(env_file_path(scope.home)).get(name)
    if from_file is not None:
        return from_file
    return scope.env.get(name)


_MISSING: Any = object()


def _masked(key: str, value: Any, scope: Scope) -> Any:
    if isinstance(value, str):
        if is_secret_key(key):
            return mask_value(value)
        return mask_tree(value, scope)
    return mask_tree(value, scope, key=key)


@click.command("get", help="Print one value. Credentials are masked unless --raw is given.")
@click.argument("key")
@click.option("--json", "as_json", is_flag=True, help="Print the value as JSON.")
@click.option("--raw", is_flag=True, help="Print credentials unmasked.")
@click.pass_context
def get_command(ctx: click.Context, key: str, as_json: bool, raw: bool) -> int:
    scope = state_of(ctx).scope
    view = fresh_view(scope)
    if route_for(key) != "config":
        found = lookup_env_value(scope, view, key)
        value: Any = _MISSING if found is None else found
    else:
        try:
            value = view.get(key, _MISSING)
        except keys.KeyPathError as exc:
            err(f"Invalid key: {exc}")
            return 1
    if value is _MISSING:
        err(f"Config key not set: {key}")
        return 1
    shown = value if raw else _masked(key, value, scope)
    click.echo(json.dumps(shown, ensure_ascii=False) if as_json else show_value(shown))
    if route_for(key) == "config":
        _phantom_note(key)
    return 0


def _phantom_note(key: str) -> None:
    parts = keys.split_key(key)
    if parts[0] not in known_roots():
        return
    check = check_key(key)
    if check.known:
        return
    err(
        f"Note: '{key}' is not a setting this version knows; the value above comes from your "
        "file and may be ignored."
    )
    if check.suggestion:
        err(f"Did you mean: {check.suggestion}?")


# -- path / env-path ------------------------------------------------------------------------------


@click.command("path", help="Print the path of the settings file.")
@click.pass_context
def path_command(ctx: click.Context) -> int:
    click.echo(str(settings_file(state_of(ctx).scope)))
    return 0


@click.command("env-path", help="Print the path of the profile's .env file.")
@click.pass_context
def env_path_command(ctx: click.Context) -> int:
    click.echo(str(env_file_path(state_of(ctx).scope.home)))
    return 0


# -- edit -----------------------------------------------------------------------------------------


def seed_config(scope: Scope) -> Path | None:
    """Create a missing settings file through the P01 writer (version stamp plus the commented
    example; no default values). Returns the path when it created one."""
    target = settings_file(scope)
    if target.exists() or target.is_symlink():
        return None
    writer.save(scope, {})
    return target


def editor_argv(env: Mapping[str, str]) -> list[str] | None:
    """``$VISUAL``, then ``$EDITOR`` (split shell-style), then the first fallback found."""
    for name in ("VISUAL", "EDITOR"):
        raw = env.get(name, "").strip()
        if raw:
            try:
                words = shlex.split(raw, posix=os.name != "nt")
            except ValueError:
                words = [raw]
            if words:
                return words
    fallbacks = _WINDOWS_EDITORS if sys.platform == "win32" else _FALLBACK_EDITORS
    for candidate in fallbacks:
        found = which(candidate, path=env.get("PATH"))
        if found:
            return [found]
    return None


def run_editor(argv: Sequence[str]) -> int:
    """Run the editor in the foreground and return its exit code (tests replace this)."""
    return subprocess.run(list(argv), check=False).returncode


@click.command("edit", help="Open the settings file in $VISUAL or $EDITOR (created if missing).")
@click.pass_context
def edit_command(ctx: click.Context) -> int:
    state = state_of(ctx)
    scope = state.scope
    target = settings_file(scope)
    if is_managed(scope.home, scope.env):
        err(
            "This installation is managed, so its settings cannot be edited here.",
            "Change them through the package manager or administrator that manages it.",
        )
        return 1
    try:
        if seed_config(scope) is not None:
            click.echo(f"Created {target}")
    except IndusError as exc:
        err(f"Could not create {target}: {exc.message}")
        return 1
    if not interactive(state):
        err(f"No terminal to run an editor in. Edit {target} with any text editor.")
        return 1
    argv = editor_argv(scope.env)
    if argv is None:
        err(f"No editor found; set $EDITOR, or edit {target} with any text editor.")
        return 1
    try:
        return run_editor([*argv, str(target)])
    except OSError as exc:
        err(f"Could not start {argv[0]}: {exc}")
        return 1
