"""Small helpers the slice-C management commands share: process state, stderr lines, fresh
settings and the profile's secret store.

Commands read settings with ``fresh_view`` (the loader's cache, re-read when the file changed)
rather than ``scope.config``, which was loaded when the process started and goes stale after a
write in the same process.
"""

from __future__ import annotations

import click

from indus.cli.args import CliState
from indus.foundation import config
from indus.foundation.config.loader import ConfigView
from indus.foundation.scope import Scope
from indus.foundation.secrets import SecretStore, open_secret_store

__all__ = ["err", "fresh_view", "interactive", "scope_with_config", "secret_store", "state_of"]


def state_of(ctx: click.Context) -> CliState:
    state = ctx.find_object(CliState)
    if state is None:
        raise click.UsageError(f"{ctx.command_path} was started without its process state")
    return state


def err(*lines: str) -> None:
    for line in lines:
        click.echo(line, err=True)


def interactive(state: CliState) -> bool:
    """Both stdin and stdout are terminals (a prompt can be shown and answered)."""
    return state.stdin_isatty() and state.stdout_isatty()


def fresh_view(scope: Scope) -> ConfigView:
    return config.load(scope)


def scope_with_config(scope: Scope) -> Scope:
    """``scope`` carrying the current settings (for code that reads ``scope.config``)."""
    return scope.with_config(fresh_view(scope))


def secret_store(scope: Scope, view: ConfigView | None = None) -> SecretStore:
    """The profile's secret store: the scope's own when it has one, else opened as
    ``security.secret_store`` says."""
    if isinstance(scope.secrets, SecretStore):
        return scope.secrets
    settings = view if view is not None else fresh_view(scope)
    mode = str(settings.get("security.secret_store", "auto") or "auto")
    return open_secret_store(scope.home, scope.profile_id, mode=mode, clock=scope.clock)
