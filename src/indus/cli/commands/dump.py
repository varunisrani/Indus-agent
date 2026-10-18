"""``indus dump [--show-keys]``: a plain-text summary to paste into a support request (P07-21).

Versions, OS, install method, profile, model / provider / backend, the signed-in providers (from
the auth store), the config keys you set (names only) and each provider key as ``set`` /
``not set``. With ``--show-keys`` a set key shows its first and last four characters; a short key
is never partly shown.
"""

from __future__ import annotations

import platform
import sys

import click

from indus.cli.cmdkit import cli_state, config_of, masked
from indus.cli.commands.status import (
    credentialed_providers,
    provider_state,
    signed_in,
    user_keys,
)
from indus.cli.version import install_method
from indus.providers import route as route_mod
from indus.providers.registry import Registry

__all__ = ["dump_command", "dump_lines"]


def _key_lines(show_keys: bool) -> list[str]:
    scope = cli_state().scope
    lines: list[str] = []
    seen: set[str] = set()
    for profile in Registry(scope).list_providers():
        for name in profile.env_vars:
            if name in seen:
                continue
            seen.add(name)
            found = route_mod.find_key(scope, (name,))
            shown = "not set"
            if found is not None:
                shown = masked(found[1]) if show_keys else "set"
            lines.append(f"  {name:<28} {shown}")
    return lines


def dump_lines(show_keys: bool) -> list[str]:
    from indus import __version__

    scope = cli_state().scope
    config = config_of(scope)
    state = provider_state(scope)
    providers = credentialed_providers(scope)
    keys = user_keys(scope)
    return [
        "--- indus dump ---",
        f"version:    Indus Agent {__version__} ({install_method()})",
        f"os:         {platform.system()} {platform.release()} {platform.machine()}",
        f"python:     {sys.version.split()[0]}",
        f"profile:    {scope.profile_id}",
        f"home:       {scope.home}",
        f"model:      {state.model}",
        f"provider:   {state.provider or 'none'}",
        f"terminal:   {config.get('terminal.backend') or 'local'}",
        f"providers:  {', '.join(p.name for p in providers) or 'none'}",
        f"signed in:  {', '.join(signed_in(scope)) or 'none'}",
        "",
        "keys:",
        *_key_lines(show_keys),
        "",
        f"settings you changed ({len(keys)}):",
        *(f"  {key}" for key in keys),
        "--- end dump ---",
    ]


@click.command("dump", short_help="Print a setup summary for support.")
@click.option(
    "--show-keys", is_flag=True, help="Show the first and last four characters of each key."
)
def dump_command(show_keys: bool) -> int:
    """Print a compact setup summary for support requests (no secret values)."""
    click.echo("\n".join(dump_lines(show_keys)))
    return 0
