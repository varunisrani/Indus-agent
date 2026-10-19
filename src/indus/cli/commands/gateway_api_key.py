"""``indus gateway api-key``: the bearer keys of the gateway's API server (P15-30).

``create [--label TEXT]`` makes a key and prints it once (it cannot be shown again);
``list [--json]`` shows each key's id, label and creation time, never a value; ``revoke ID``
removes one. Values live in the profile's secret store, labels in
``<home>/gateway/api_keys.json`` (``indus.state.api_keys``). A running gateway picks up a new or
revoked key on the next request.

Exit codes: 0 done, 1 no key with that id.

``api_key_group`` is mounted under ``indus gateway`` (P15-28).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from indus.state.api_keys import ApiKeyRing

__all__ = ["api_key_group"]

_SETTINGS = {"help_option_names": ["-h", "--help"], "max_content_width": 100}


def _ring() -> ApiKeyRing:
    from indus.cli.cmdkit import cli_state
    from indus.cli.commands._support import secret_store
    from indus.state.api_keys import ApiKeyRing

    scope = cli_state().scope
    return ApiKeyRing(scope, secret_store(scope))


@click.group("api-key", context_settings=_SETTINGS, invoke_without_command=True)
@click.pass_context
def api_key_group(ctx: click.Context) -> None:
    """Make, list and revoke the keys clients use with the gateway's HTTP API."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(list_keys)


@api_key_group.command("create", context_settings=_SETTINGS)
@click.option("--label", default="", help="A note to tell keys apart (for example the client).")
def create_key(label: str) -> None:
    """Make a new key and print it once."""
    info, value = _ring().create(label)
    click.echo(f"Created API key {info.key_id}" + (f" ({info.label})" if info.label else "") + ".")
    click.echo("")
    click.echo(f"  {value}")
    click.echo("")
    click.echo("Copy it now: it is not shown again. Clients send it as")
    click.echo("  Authorization: Bearer <key>")


@api_key_group.command("list", context_settings=_SETTINGS)
@click.option("--json", "as_json", is_flag=True, help="Print JSON.")
def list_keys(as_json: bool) -> None:
    """Show the keys (id, label, created); never their values."""
    keys = _ring().list()
    if as_json:
        rows = [{"id": k.key_id, "label": k.label, "created_at": k.created_at} for k in keys]
        click.echo(json.dumps({"keys": rows}, indent=2))
        return
    if not keys:
        click.echo("No API keys yet. Make one with `indus gateway api-key create`.")
        return
    click.echo(f"API keys ({len(keys)}):")
    for key in keys:
        label = f"  {key.label}" if key.label else ""
        click.echo(f"  {key.key_id}  {key.created_at}{label}")


@api_key_group.command("revoke", context_settings=_SETTINGS)
@click.argument("key_id")
def revoke_key(key_id: str) -> None:
    """Remove a key; clients using it are refused from the next request."""
    if not _ring().revoke(key_id):
        click.echo(f"api-key: no key with id {key_id!r}", err=True)
        raise click.exceptions.Exit(1)
    click.echo(f"Revoked API key {key_id.strip().upper()}.")
