"""``indus vault``: the local encrypted vault behind ``browser_vault`` (P24-40).

``add`` asks for a login (site origin, label, identifier, then the password and an optional
authenticator key, both hidden), a payment card or an address, and stores it encrypted in
``<home>/vault/local.vault`` with its key in the profile's secret store. Secret values are only
ever read from hidden prompts, never from arguments, so ``add`` needs a terminal (exit 1 without
one). ``list`` (the default) shows handles and metadata, never values. ``remove HANDLE`` deletes an
item. ``lock`` stops the agent from using the local vault until ``unlock`` (or until the user
approves an unlock card in a chat).

Exit codes: 0 done, 1 refused (no terminal for ``add``, unknown handle, a vault error), 2 usage.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from indus.foundation.scope import Scope

__all__ = ["vault_group"]

_SETTINGS = {"help_option_names": ["-h", "--help"], "max_content_width": 100}
_ALIASES = {"ls": "list", "rm": "remove"}
_KINDS = ("login", "payment", "address")


class _Group(click.Group):
    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        return super().get_command(ctx, _ALIASES.get(cmd_name, cmd_name))


def _scope() -> Scope:
    from indus.cli.cmdkit import cli_state

    return cli_state().scope


def _store_scope() -> Scope:
    """The scope with a writable secret store (the vault key lives there)."""
    from indus.cli.commands._support import secret_store

    scope = _scope()
    return scope.with_secrets(secret_store(scope))


def _fail(message: str) -> None:
    click.echo(f"vault: {message}", err=True)
    raise click.exceptions.Exit(1)


def _ask(text: str, *, hidden: bool, required: bool = True) -> str:
    """One prompt (hidden for secrets); empty answers repeat while ``required``."""
    while True:
        value = click.prompt(text, default="", show_default=False, hide_input=hidden)
        value = str(value).strip()
        if value or not required:
            return value
        click.echo("A value is needed.", err=True)


@click.group("vault", cls=_Group, context_settings=_SETTINGS, invoke_without_command=True)
@click.pass_context
def vault_group(ctx: click.Context) -> None:
    """Saved logins, cards and addresses the agent can fill without seeing them."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(list_items)


@vault_group.command("add", context_settings=_SETTINGS)
@click.option("--kind", type=click.Choice(_KINDS), default=None, help="What to save.")
@click.option("--origin", default="", help="The site it may be filled on, e.g. https://x.com.")
@click.option("--label", default="", help="A short name shown by `list`.")
def add_item(kind: str | None, origin: str, label: str) -> None:
    """Save an item (values are asked for in hidden prompts)."""
    from indus.cli.cmdkit import cli_state
    from indus.cli.commands._support import interactive
    from indus.tools.builtin.browser.vault import (
        ADDRESS_FIELDS,
        PAYMENT_FIELDS,
        REQUIRED_FIELDS,
        LocalVault,
        VaultError,
    )

    if not interactive(cli_state()):
        _fail("values are read from hidden prompts only; run `indus vault add` in a terminal.")
    scope = _store_scope()
    chosen = kind or click.prompt(
        "Kind", type=click.Choice(_KINDS), default="login", show_choices=True
    )
    site = origin.strip() or _ask("Site origin (e.g. https://github.com)", hidden=False)
    name = label.strip() or _ask("Label", hidden=False, required=False)
    identifier = ""
    otp_seed = ""
    secret: dict[str, str] = {}
    if chosen == "login":
        identifier = _ask("Username or email", hidden=False)
        secret["password"] = _ask("Password (hidden)", hidden=True)
        otp_seed = _ask("Authenticator key (optional, hidden)", hidden=True, required=False)
    else:
        fields = PAYMENT_FIELDS if chosen == "payment" else ADDRESS_FIELDS
        needed = REQUIRED_FIELDS[chosen]
        for field_name in fields:
            text = field_name.replace("_", " ").replace("cc ", "card ")
            optional = "" if field_name in needed else " (optional)"
            secret[field_name] = _ask(
                f"{text}{optional}", hidden=chosen == "payment", required=field_name in needed
            )
    try:
        meta = LocalVault().add(
            scope,
            kind=chosen,
            label=name,
            origin=site,
            secret=secret,
            identifier=identifier,
            otp_seed=otp_seed,
        )
    except VaultError as exc:
        _fail(str(exc))
        return
    finally:
        secret.clear()
    click.echo(f"Saved {meta.kind} {meta.handle} for {meta.origin}.")


@vault_group.command("list", context_settings=_SETTINGS)
def list_items() -> None:
    """Show saved items (never their values)."""
    from indus.tools.builtin.browser.vault import LocalVault, VaultError

    scope = _store_scope()
    vault = LocalVault()
    try:
        items = vault.list_items(scope)
    except VaultError as exc:
        _fail(str(exc))
        return
    if vault.locked(scope):
        click.echo("The local vault is locked: the agent cannot use it (indus vault unlock).")
    if not items:
        click.echo("The vault is empty. Save a login with `indus vault add`.")
        return
    for meta in items:
        who = f" {meta.identifier}" if meta.identifier else ""
        click.echo(f"{meta.handle}  {meta.kind:<8} {meta.label}{who}  {meta.origin}")
    click.echo("Values are never shown; the agent fills them from the handle.")


@vault_group.command("remove", context_settings=_SETTINGS)
@click.argument("handle")
def remove_item(handle: str) -> None:
    """Delete an item by its handle."""
    from indus.tools.builtin.browser.vault import LocalVault, VaultError

    try:
        removed = LocalVault().remove(_store_scope(), handle)
    except VaultError as exc:
        _fail(str(exc))
        return
    if not removed:
        _fail(f"no vault item has the handle {handle!r}")
    click.echo(f"Removed {handle}.")


@vault_group.command("lock", context_settings=_SETTINGS)
def lock_vault() -> None:
    """Stop the agent from using the local vault."""
    from indus.tools.builtin.browser.vault import LocalVault

    LocalVault().lock(_scope())
    click.echo("The local vault is locked.")


@vault_group.command("unlock", context_settings=_SETTINGS)
def unlock_vault() -> None:
    """Let the agent use the local vault again."""
    from indus.tools.builtin.browser.vault import LocalVault

    was = LocalVault().unlock_marker(_scope())
    click.echo("The local vault is unlocked." if was else "The local vault was not locked.")
