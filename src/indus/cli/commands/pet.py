"""``indus pet``: make, show and manage the desktop pet (P24-19).

``generate IDEA`` draws up to four looks with the profile's image service and prints a token and
the draft files; ``hatch TOKEN --pick N --name NAME`` draws the pet's moods after one draft and
installs it (not shown yet); ``use`` shows a pet on every surface, ``off`` hides it; ``list``,
``show``, ``rename`` and ``remove`` manage the pets. ``remove`` asks first; without a terminal it
needs ``-y``. Nothing here needs a terminal otherwise (R04.19).
"""

from __future__ import annotations

import asyncio

import click

from indus.cli.cmdkit import cli_state, err, group
from indus.cli.prompts import confirm, io_for
from indus.service import pets
from indus.service.pets import PetError, PetStore

__all__ = ["pet_group"]

_NONE = 'No pets yet. Draw one with `indus pet generate "a small sleepy otter"`.'


def _store() -> PetStore:
    return PetStore(cli_state().scope)


def _fail(exc: PetError) -> click.exceptions.Exit:
    err(str(exc))
    return click.exceptions.Exit(1)


@group("pet", "Make and show the desktop pet.")
def pet_group() -> None:
    pass


@pet_group.command("generate")
@click.argument("idea", nargs=-1, required=True)
@click.option(
    "--count", "-n", default=pets.MAX_DRAFTS, show_default=True, help="Looks to draw (1-4)."
)
def generate_command(idea: tuple[str, ...], count: int) -> None:
    """Draw draft looks for a new pet with the profile's image service."""
    scope = cli_state().scope
    try:
        made = asyncio.run(pets.generate_drafts(scope, " ".join(idea), count=count))
    except PetError as exc:
        raise _fail(exc) from None
    click.echo(f"Drafts for “{made.idea}” (token {made.token}, kept for an hour):")
    for draft in made.drafts:
        click.echo(f"  {draft.index}: {draft.path}")
    click.echo(f"Hatch one with `indus pet hatch {made.token} --pick N --name NAME`.")


@pet_group.command("hatch")
@click.argument("token")
@click.option("--pick", "-p", type=int, default=0, show_default=True, help="Which draft.")
@click.option("--name", required=True, help="The pet's name.")
@click.option("--use", "use_now", is_flag=True, help="Show it at once.")
def hatch_command(token: str, pick: int, name: str, use_now: bool) -> None:
    """Draw the pet's moods after one draft and install it."""
    scope = cli_state().scope
    try:
        pet = asyncio.run(pets.hatch(scope, token.strip().lower(), pick, name))
        if use_now:
            PetStore(scope).use(pet.slug)
    except PetError as exc:
        raise _fail(exc) from None
    click.echo(f"Hatched {pet.name} ({pet.slug}) with {len(pet.moods)} moods in {pet.directory}.")
    if not use_now:
        click.echo(f"Show it with `indus pet use {pet.slug}`.")


@pet_group.command("list")
def list_command() -> None:
    """List the pets of this profile."""
    store = _store()
    found = store.list()
    if not found:
        click.echo(_NONE)
        return
    settings = store.settings()
    width = max(len(p.slug) for p in found) + 1
    for pet in found:
        mark = "*" if settings.enabled and settings.active == pet.slug else " "
        click.echo(f"{mark} {pet.slug:<{width}} {pet.name}")


@pet_group.command("show")
@click.argument("slug", required=False)
def show_command(slug: str | None) -> None:
    """Show one pet (the shown one by default)."""
    store = _store()
    try:
        pet = store.get(slug) if slug else store.shown()
    except PetError as exc:
        raise _fail(exc) from None
    if pet is None:
        err("No pet is shown. `indus pet list` lists them; `indus pet use SLUG` shows one.")
        raise click.exceptions.Exit(1)
    settings = store.settings()
    shown = settings.enabled and settings.active == pet.slug
    click.echo(f"{pet.name} ({pet.slug}){'  — shown' if shown else ''}")
    if pet.idea:
        click.echo(f"  idea:   {pet.idea}")
    click.echo(f"  scale:  {settings.scale:g}")
    click.echo(f"  folder: {pet.directory}")
    for mood in pets.MOODS:
        picture = pet.picture(mood)
        click.echo(f"  {mood:<6}  {picture.name if picture else '(missing)'}")


@pet_group.command("use")
@click.argument("slug")
def use_command(slug: str) -> None:
    """Show this pet on the desktop and in the TUI."""
    try:
        pet = _store().use(slug)
    except PetError as exc:
        raise _fail(exc) from None
    click.echo(f"{pet.name} is shown now.")


@pet_group.command("off")
def off_command() -> None:
    """Hide the pet everywhere."""
    _store().off()
    click.echo("The pet is hidden.")


@pet_group.command("rename")
@click.argument("slug")
@click.argument("name", nargs=-1, required=True)
def rename_command(slug: str, name: tuple[str, ...]) -> None:
    """Give a pet a new name."""
    try:
        pet = _store().rename(slug, " ".join(name))
    except PetError as exc:
        raise _fail(exc) from None
    click.echo(f"Renamed to {pet.name} ({pet.slug}).")


@pet_group.command("remove")
@click.argument("slug")
@click.option("--yes", "-y", is_flag=True, help="Do not ask first.")
def remove_command(slug: str, yes: bool) -> None:
    """Delete a pet and its pictures."""
    state = cli_state()
    store = PetStore(state.scope)
    try:
        pet = store.get(slug)
    except PetError as exc:
        raise _fail(exc) from None
    io = io_for(state, "Removing a pet needs a terminal to confirm, or -y.")
    if not confirm(io, f"Delete {pet.name} and its pictures?", assume_yes=yes):
        raise click.exceptions.Exit(1)
    try:
        store.remove(pet.slug)
    except (PetError, OSError) as exc:
        err(f"Pet not removed: {exc}")
        raise click.exceptions.Exit(1) from None
    click.echo(f"Removed {pet.name}.")
