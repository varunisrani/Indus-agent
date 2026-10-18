"""``indus config set`` and ``indus config unset``: route, coerce, guard, write (R04.7–R04.9,
R04.12).

Routing (first match wins): a managed install refuses; a malformed key refuses; a bare
credential-shaped name goes to the secret store (``.env`` when ``security.secret_store`` is
``file``); any other UPPER_SNAKE name goes to ``.env``; everything else to ``config.yaml``. Every
``.env`` write goes through P01's env writer, so its denylist applies on each path, and every
``config.yaml`` write through P01's config writer. Coercion and the section/container guards and
dotted-key navigation are P01's ``foundation.config.keys``. A refusal prints to stderr, says
nothing was written and returns 1.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import click

from indus.cli.commands._support import err, fresh_view, secret_store, state_of
from indus.cli.config_coerce import (
    admin_pinned,
    check_key,
    drop_stale_route,
    is_secret_key,
    mask_value,
    route_for,
    show_value,
)
from indus.foundation.config import keys, loader, schema, writer
from indus.foundation.config.loader import ConfigView
from indus.foundation.envfile import EnvNameError, check_writable_name, read_env_file, set_value
from indus.foundation.envload import env_file_path
from indus.foundation.errors import IndusError
from indus.foundation.home import is_managed
from indus.foundation.scope import Scope
from indus.foundation.secrets import SecretStoreError

__all__ = ["Refused", "set_command", "set_value_for", "unset_command", "unset_value_for"]

log = logging.getLogger(__name__)

NOTHING_WRITTEN = "Nothing was written."
_ALIASES = {"api_base": "model.base_url", "model.api_base": "model.base_url"}


class Refused(Exception):
    """A write that must not happen; the message says why."""


type Say = Callable[[str], None]


def _say(line: str) -> None:
    click.echo(line)


# -- shared checks --------------------------------------------------------------------------------


def _refuse_managed(scope: Scope) -> None:
    if is_managed(scope.home, scope.env):
        raise Refused(
            "This installation is managed, so its settings cannot be changed here. Change them "
            "through the package manager or administrator that manages it."
        )


def _check_key_text(key: str) -> None:
    if not key or key != key.strip():
        raise Refused(f"Invalid key {key!r}: it is empty or has spaces around it.")
    try:
        keys.split_key(key)
    except keys.KeyPathError as exc:
        raise Refused(
            f"Invalid key {key!r}: it has an empty part (two dots, or a dot at an end)."
        ) from exc
    if admin_pinned(key):
        raise Refused(f"'{key}' is set by your administrator and cannot be changed here.")


def _readable_view(scope: Scope) -> ConfigView:
    view = fresh_view(scope)
    if view.error is not None:
        raise Refused(
            f"{loader.config_path(scope)} cannot be read right now ({view.error}); fix the file "
            "first."
        )
    return view


def _secret_mode(view: ConfigView) -> str:
    return str(view.get("security.secret_store", "auto") or "auto")


# -- set ------------------------------------------------------------------------------------------


def _set_secret(scope: Scope, name: str, value: str, say: Say) -> None:
    name = name.upper()
    _guard_env_name(name)
    view = _readable_view(scope)
    env_path = env_file_path(scope.home)
    if _secret_mode(view) == "file":
        _write_env(env_path, name, value)
        say(f"Set {name} in {env_path}")
        return
    try:
        store = secret_store(scope, view)
        store.set(name, value)
    except (SecretStoreError, IndusError) as exc:
        raise Refused(f"The secret store could not save {name}: {exc}") from exc
    where = "the OS keychain" if store.backend_name == "keychain" else "the profile's secret file"
    say(f"Set {name} in {where}")
    if name in read_env_file(env_path):
        _write_env(env_path, name, None)
        say(f"Removed the older copy of {name} from {env_path}")


def _guard_env_name(name: str) -> None:
    try:
        check_writable_name(name)
    except EnvNameError as exc:
        raise Refused(str(exc)) from exc


def _write_env(path: Path, name: str, value: str | None) -> None:
    try:
        report = set_value(path, name, value)
    except EnvNameError as exc:
        raise Refused(str(exc)) from exc
    except OSError as exc:
        raise Refused(f"{path} could not be written: {exc}") from exc
    for warning in report.warnings:
        err(f"Warning: {warning}")


def _set_env(scope: Scope, name: str, value: str, say: Say) -> None:
    _guard_env_name(name)
    env_path = env_file_path(scope.home)
    _write_env(env_path, name, value)
    say(f"Set {name} in {env_path}")


def _coerced(key: str, raw_value: str) -> Any:
    try:
        return keys.coerce(raw_value, schema.schema_for(key), key=key)
    except keys.CoercionError as exc:
        raise Refused(str(exc)) from exc


def _model_section(raw: dict[str, Any]) -> dict[str, Any] | None:
    model = raw.get("model")
    return cast(dict[str, Any], model) if isinstance(model, dict) else None


def _set_config(scope: Scope, key: str, raw_value: str, force: bool, say: Say) -> None:
    if key in _ALIASES:
        say(f"Note: '{key}' is another name for {_ALIASES[key]}; saved there.")
        key = _ALIASES[key]
    check = check_key(key)
    if not check.known and check.wrong_prefix and not force:
        raise Refused(
            f"'{key}' is not a setting; did you mean {check.suggestion}? Use --force to write "
            "this path anyway."
        )
    view = _readable_view(scope)
    raw = view.user_raw()
    value = _coerced(key, raw_value)
    old_model = _model_section(raw)
    old_provider = str((old_model or {}).get("provider") or "")
    try:
        key, value = keys.check_write(raw, key, value, force=force)
        keys.set_path(raw, key, value)
    except (keys.GuardError, keys.KeyPathError) as exc:
        raise Refused(str(exc)) from exc
    notes: list[str] = []
    if key == "model.provider" and str(value).strip().lower() != old_provider.strip().lower():
        model = _model_section(raw)
        if model is not None:
            dropped, warning = drop_stale_route(model, str(value), scope.with_config(view))
            if dropped:
                parts = ", ".join(f"model.{k} ({v})" for k, v in dropped.items())
                was = old_provider or "the previous provider"
                notes.append(f"Cleared {parts}: that endpoint belonged to {was}, not {value}.")
            if warning:
                notes.append(f"Warning: {warning}")
    try:
        written = writer.save(scope, raw, replace=True)
    except IndusError as exc:
        raise Refused(exc.message + (f" ({exc.hint})" if exc.hint else "")) from exc
    shown: Any = value
    if isinstance(value, str) and is_secret_key(key):
        shown = mask_value(value)
    say(f"Set {key} = {show_value(shown)} in {written}")
    for line in notes:
        say(line)
    if not check.known and not force:
        say(f"Note: '{key}' is not a setting this version knows; it was saved, but may be ignored.")
        if check.suggestion:
            say(f"Did you mean: {check.suggestion}?")


def set_value_for(
    scope: Scope, key: str, value: str, *, force: bool = False, say: Say = _say
) -> None:
    """Route and write one value (see the module notes). Raises `Refused`."""
    _refuse_managed(scope)
    _check_key_text(key)
    route = route_for(key)
    if route == "secret":
        _set_secret(scope, key, value, say)
    elif route == "env":
        _set_env(scope, key, value, say)
    else:
        _set_config(scope, key, value, force, say)


@click.command(
    "set",
    help="Set KEY to VALUE: credentials go to the keychain, UPPER_SNAKE names "
    "to .env, everything else to the settings file.",
)
@click.argument("key")
@click.argument("value")
@click.option(
    "--force",
    is_flag=True,
    help="Write a key this version does not know, or replace a whole section with one value.",
)
@click.pass_context
def set_command(ctx: click.Context, key: str, value: str, force: bool) -> int:
    try:
        set_value_for(state_of(ctx).scope, key, value, force=force)
    except Refused as exc:
        err(str(exc), NOTHING_WRITTEN)
        return 1
    return 0


# -- unset ----------------------------------------------------------------------------------------


def _unset_secret(scope: Scope, name: str, say: Say) -> bool:
    name = name.upper()
    view = _readable_view(scope)
    removed_from: list[str] = []
    if _secret_mode(view) != "file":
        try:
            store = secret_store(scope, view)
            if store.delete(name):
                removed_from.append(
                    "the OS keychain" if store.backend_name == "keychain" else "the secret file"
                )
        except (SecretStoreError, IndusError) as exc:
            raise Refused(f"The secret store could not remove {name}: {exc}") from exc
    env_path = env_file_path(scope.home)
    if name in read_env_file(env_path):
        _write_env(env_path, name, None)
        removed_from.append(str(env_path))
    if removed_from:
        say(f"Removed {name} from {' and '.join(removed_from)}")
        _forget_pool_rows(scope, name)
    return bool(removed_from)


def _forget_pool_rows(scope: Scope, name: str) -> None:
    """Credential pools stop listing a removed key (``indus auth list``)."""
    from indus.foundation.errors import AuthError
    from indus.providers.auth.pool_model import prune_env_rows
    from indus.providers.auth.store import AuthStore

    try:
        prune_env_rows(AuthStore(scope.home), name)
    except AuthError as exc:
        log.debug("pool rows of %s not pruned: %s", name, exc)


def _unset_env(scope: Scope, name: str, say: Say) -> bool:
    env_path = env_file_path(scope.home)
    if name not in read_env_file(env_path):
        return False
    _write_env(env_path, name, None)
    say(f"Removed {name} from {env_path}")
    return True


def _unset_config(scope: Scope, key: str, say: Say) -> bool:
    key = _ALIASES.get(key, key)
    view = _readable_view(scope)
    raw = view.user_raw()
    try:
        removed = keys.unset_path(raw, key)
    except keys.KeyPathError as exc:
        raise Refused(str(exc)) from exc
    if not removed:
        return False
    try:
        written = writer.save(scope, raw, replace=True)
    except IndusError as exc:
        raise Refused(exc.message + (f" ({exc.hint})" if exc.hint else "")) from exc
    say(f"Removed {key} from {written}")
    return True


def unset_value_for(scope: Scope, key: str, *, say: Say = _say) -> bool:
    """Remove one value; False when nothing was set. Raises `Refused`."""
    _refuse_managed(scope)
    _check_key_text(key)
    route = route_for(key)
    if route == "secret":
        return _unset_secret(scope, key, say)
    if route == "env":
        return _unset_env(scope, key, say)
    return _unset_config(scope, key, say)


@click.command("unset", help="Remove KEY from wherever `config set` would have put it.")
@click.argument("key")
@click.pass_context
def unset_command(ctx: click.Context, key: str) -> int:
    try:
        removed = unset_value_for(state_of(ctx).scope, key)
    except Refused as exc:
        err(str(exc), NOTHING_WRITTEN)
        return 1
    if not removed:
        err(f"Config key not set: {key}")
        return 1
    return 0
