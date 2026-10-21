"""``indus blueprint``: job templates with a fixed recurrence and a few blanks (P24-46).

Verbs: ``list`` (the starter catalogue and the skills' blueprints), ``show <name>`` (slots,
defaults, the ``/cron add`` line and, with ``--json``, the form fields), ``use <name>
[slot=value …] [--dry-run]`` (fills the blueprint and creates the job through the normal job
creation; ``--dry-run`` prints the job spec and creates nothing). A name is matched the way
``/blueprint`` matches it. Refusals (no match, several matches, a value outside its slot) exit 1
with the reason; nothing asks a question, so every verb behaves the same without a terminal.
"""

from __future__ import annotations

import json
from typing import NoReturn

import click

from indus.cli.cmdkit import cli_state, err, group
from indus.cron import blueprints as bps
from indus.cron import ports
from indus.cron.errors import CronStoreBusy
from indus.foundation.scope import Scope

__all__ = ["blueprint_group"]


def _fail(message: str) -> NoReturn:
    err(f"blueprint: {message}")
    raise click.exceptions.Exit(1)


def _pick(scope: Scope, name: str) -> bps.Blueprint:
    found, several = bps.match_blueprint(bps.list_blueprints(scope), name)
    if found is not None:
        return found
    if several:
        _fail(f"{name!r} matches several: " + ", ".join(b.key for b in several))
    _fail(f"no blueprint matches {name!r}; `indus blueprint list` shows them")


def _backend(scope: Scope) -> ports.CronBackend:
    from indus.service import cron_host

    cron_host.install()
    return ports.backend_for(scope)


@group("blueprint", "Job templates: a fixed schedule with a few blanks to fill.")
def blueprint_group() -> None:
    pass


@blueprint_group.command("list")
def list_command() -> None:
    """List the blueprints."""
    for bp in bps.list_blueprints(cli_state().scope):
        where = f"  (skill {bp.skill})" if bp.skill else ""
        click.echo(f"{bp.key}: {bp.title}{where}")
        if bp.description:
            click.echo(f"  {bp.description}")


@blueprint_group.command("show")
@click.argument("name")
@click.option("--json", "as_json", is_flag=True, help="Print the form fields as JSON.")
def show_command(name: str, as_json: bool) -> None:
    """Show a blueprint's blanks and the /cron line it fills."""
    bp = _pick(cli_state().scope, name)
    if as_json:
        click.echo(json.dumps(bps.form(bp), indent=2))
        return
    click.echo(f"{bp.key}: {bp.title}")
    if bp.description:
        click.echo(f"  {bp.description}")
    click.echo(f"  schedule: {bp.schedule}  ({bps.describe_schedule(bp)})")
    for slot in bp.slots:
        default = f" [default {slot.default}]" if slot.default not in (None, "") else ""
        optional = " (optional)" if slot.optional else ""
        click.echo(f"  {slot.name}: {slot.label}, {slot.kind}{default}{optional}")
    if bp.fills_from_defaults():
        click.echo(f"  {bps.cron_line(bp)}")


@blueprint_group.command("use")
@click.argument("name")
@click.argument("values", nargs=-1)
@click.option("--dry-run", is_flag=True, help="Print the job it would create; create nothing.")
def use_command(name: str, values: tuple[str, ...], dry_run: bool) -> None:
    """Fill a blueprint (slot=value …) and create the job."""
    scope = cli_state().scope
    bp = _pick(scope, name)
    given, rest = bps.split_values(values)
    if rest:
        _fail(f"give values as slot=value; not understood: {' '.join(rest)}")
    try:
        spec = bps.fill(bp, given)
    except bps.BlueprintError as exc:
        _fail(str(exc))
    if dry_run:
        click.echo(json.dumps(spec, indent=2))
        return
    try:
        job = _backend(scope).create(spec)
    except (ValueError, CronStoreBusy, ports.StoreUnavailable) as exc:
        _fail(f"the job was not created: {exc}")
    click.echo(f"blueprint: created job {ports.field(job, 'id')} ({spec['schedule']})")
