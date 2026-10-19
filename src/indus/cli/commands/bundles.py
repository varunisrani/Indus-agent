"""``indus bundles``: list, show, create, delete and reload skill bundles (P09-10).

A bundle file lives in ``<home>/skill-bundles/<slug>.yaml``; ``/<slug>`` in chat loads all its
skills at once. ``create`` asks for the skills on a terminal, or takes ``--skill`` (repeatable);
``--force`` replaces an existing bundle. ``delete`` asks first; without a terminal it needs ``-y``
and exits 1 otherwise.
"""

from __future__ import annotations

import click

from indus.cli.cmdkit import cli_state, err, group
from indus.cli.prompts import Cancelled, ask_text, confirm, io_for
from indus.learning.skills.bundles import (
    BundleError,
    bundles_dir,
    delete_bundle,
    reload_bundles,
    save_bundle,
    scan_bundles,
    slugify,
)
from indus.learning.skills.resolver import load_catalog

__all__ = ["bundles_group"]

_NONE = "No skill bundles yet. Create one with `indus bundles create <name> --skill <skill>`."


@group("bundles", "Group skills into one slash command.")
def bundles_group() -> None:
    pass


@bundles_group.command("list")
def list_command() -> None:
    """List the bundles of this profile."""
    found = scan_bundles(cli_state().scope.home)
    if not found:
        click.echo(_NONE)
        return
    width = max(len(slug) for slug in found) + 1
    for slug in sorted(found):
        bundle = found[slug]
        line = f"/{slug:<{width}} {', '.join(bundle.skills)}"
        click.echo(line + (f"  — {bundle.description}" if bundle.description else ""))


@bundles_group.command("show")
@click.argument("name")
def show_command(name: str) -> None:
    """Show one bundle and whether its skills are installed."""
    scope = cli_state().scope
    bundle = scan_bundles(scope.home).get(slugify(name))
    if bundle is None:
        err(f"No bundle named {name!r}. `indus bundles list` shows them.")
        raise click.exceptions.Exit(1)
    catalog = load_catalog(scope)
    click.echo(f"/{bundle.slug}")
    if bundle.description:
        click.echo(f"  description: {bundle.description}")
    if bundle.instruction:
        click.echo(f"  instruction: {bundle.instruction}")
    click.echo(f"  file:        {bundle.path}")
    click.echo("  skills:")
    for skill in bundle.skills:
        state = "ok" if catalog.find(skill) is not None else "missing"
        click.echo(f"    - {skill} ({state})")


@bundles_group.command("create")
@click.argument("name")
@click.option("--skill", "-s", "skills", multiple=True, help="A skill to include (repeatable).")
@click.option("--description", default="", help="One line saying what the bundle is for.")
@click.option("--instruction", default="", help="Text sent before the skills when it runs.")
@click.option("--force", is_flag=True, help="Replace a bundle with the same name.")
def create_command(
    name: str, skills: tuple[str, ...], description: str, instruction: str, force: bool
) -> None:
    """Create a bundle (asks for the skills on a terminal)."""
    state = cli_state()
    chosen = [s.strip() for s in skills if s.strip()]
    if not chosen:
        io = io_for(state)
        if not io.tty:
            err("No terminal to ask on: pass the skills with --skill <name> (repeatable).")
            raise click.exceptions.Exit(2)
        try:
            raw = ask_text(io, "Skills to include (comma separated)")
            if not description:
                description = ask_text(io, "Description (optional)", "")
            if not instruction:
                instruction = ask_text(io, "Instruction sent first (optional)", "")
        except Cancelled:
            raise click.exceptions.Exit(1) from None
        chosen = [s.strip() for s in raw.split(",") if s.strip()]
    try:
        path = save_bundle(
            state.scope.home,
            name,
            chosen,
            description=description,
            instruction=instruction,
            overwrite=force,
        )
    except BundleError as exc:
        err(f"Bundle not saved: {exc}")
        raise click.exceptions.Exit(1) from None
    catalog = load_catalog(state.scope)
    missing = [s for s in chosen if catalog.find(s) is None]
    click.echo(f"Saved /{slugify(name)} to {path}.")
    if missing:
        click.echo(f"Not installed yet (skipped when the bundle runs): {', '.join(missing)}")


@bundles_group.command("delete")
@click.argument("name")
@click.option("--yes", "-y", is_flag=True, help="Do not ask first.")
def delete_command(name: str, yes: bool) -> None:
    """Delete a bundle file."""
    state = cli_state()
    if slugify(name) not in scan_bundles(state.scope.home):
        err(f"No bundle named {name!r}.")
        raise click.exceptions.Exit(1)
    io = io_for(state, "Deleting a bundle needs a terminal to confirm, or -y.")
    answer = confirm(io, f"Delete bundle /{slugify(name)}?", assume_yes=yes)
    if not answer:
        raise click.exceptions.Exit(1)
    try:
        path = delete_bundle(state.scope.home, name)
    except (BundleError, OSError) as exc:
        err(f"Bundle not deleted: {exc}")
        raise click.exceptions.Exit(1) from None
    click.echo(f"Deleted {path}.")


@bundles_group.command("reload")
def reload_command() -> None:
    """Re-read the bundle folder."""
    reload_bundles()
    home = cli_state().scope.home
    count = len(scan_bundles(home))
    click.echo(f"{count} bundle{'s' if count != 1 else ''} in {bundles_dir(home)}.")
