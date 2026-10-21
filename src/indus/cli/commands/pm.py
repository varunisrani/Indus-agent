"""``indus pm``: install and check the helper programs Indus uses (P24-24).

Subcommands: ``lock``, ``install``, ``env``, ``doctor``, ``repair``, ``gc``, ``bundle``,
``status``, ``update`` (operations in ``indus.pm.commands``). Nothing asks without a terminal:
``lock`` and ``update`` (which rewrite the lock and download) need ``--yes`` there and otherwise
exit 1 with guidance; the rest print and exit (R04.19). Exit 0 ok, 1 failure or guidance, 2 usage.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import click

from indus.cli import prompts
from indus.cli.cmdkit import cli_state, err, group
from indus.pm import api as pm_facade
from indus.pm import commands as ops

__all__ = ["pm_group"]


def _manager(local_dirs: Sequence[Path] = ()) -> pm_facade.Pm:
    scope = cli_state().scope
    if local_dirs:
        return pm_facade.Pm.for_scope(scope, local_dirs=tuple(local_dirs))
    return pm_facade.manager(scope)


def _fail(exc: pm_facade.PmError) -> None:
    if isinstance(exc, ops.UsageProblem):
        raise click.UsageError(exc.message)
    err(f"error: {exc.message}")
    if exc.hint:
        err(f"  {exc.hint}")
    raise click.exceptions.Exit(1)


def _steps(op: ops.Operation) -> None:
    for step in op.steps:
        word = "ok" if step.ok else "failed"
        click.echo(f"  {word:<6} {step.name}" + (f": {step.detail}" if step.detail else ""))


def _confirm(question: str, guidance: str, yes: bool) -> bool:
    io = prompts.io_for(cli_state(), guidance)
    answer = prompts.confirm(io, question, assume_yes=yes)
    if answer is None:
        raise click.exceptions.Exit(1)
    return answer


@group("pm", "Install and check the helper programs Indus uses.")
def pm_group() -> None:
    pass


@pm_group.command("lock")
@click.argument("names", nargs=-1)
@click.option("-y", "--yes", is_flag=True, help="Do not ask first.")
def lock_command(names: tuple[str, ...], yes: bool) -> None:
    """Pin every target of our package list (url and sha256)."""
    pm = _manager()
    try:
        chosen = [pm.package(n).name for n in names] if names else sorted(pm.definitions)
    except pm_facade.PmError as exc:
        _fail(exc)
        return
    if not _confirm(
        f"Download every archive of {', '.join(chosen)} for every target to pin its hash?",
        "`indus pm lock` downloads every archive to hash it; run it with --yes to go ahead.",
        yes,
    ):
        click.echo("Nothing was changed.")
        raise click.exceptions.Exit(1)
    try:
        op = ops.lock_packages(pm, names)
    except pm_facade.PmError as exc:
        _fail(exc)
        return
    _steps(op)
    click.echo(f"Lock written to {pm.paths.lock}.")
    raise click.exceptions.Exit(op.exit_code)


@pm_group.command("install")
@click.argument("names", nargs=-1)
@click.option(
    "--without",
    "without",
    multiple=True,
    metavar="PKG",
    help="Leave a default package out, now and later (name it again to opt back in).",
)
@click.option(
    "--from",
    "from_dir",
    type=click.Path(file_okay=False, path_type=Path),
    help="A folder written by `indus pm bundle`: install from it without the network.",
)
def install_command(
    names: tuple[str, ...], without: tuple[str, ...], from_dir: Path | None
) -> None:
    """Install packages (default: the required ones plus the default ones)."""
    if from_dir is not None and not from_dir.is_dir():
        raise click.BadParameter(f"{from_dir} is not a folder", param_hint="--from")
    pm = _manager((from_dir,) if from_dir is not None else ())
    try:
        if from_dir is not None and ops.adopt_bundle_lock(pm, from_dir):
            click.echo(f"Using the lock from {from_dir}.")
        op, warnings = ops.install(pm, names, without=without)
    except pm_facade.PmError as exc:
        _fail(exc)
        return
    finally:
        if from_dir is not None:
            pm.close()
    _steps(op)
    for line in warnings:
        err(f"warning: {line}")
    raise click.exceptions.Exit(op.exit_code)


@pm_group.command("env")
@click.argument("names", nargs=-1)
def env_command(names: tuple[str, ...]) -> None:
    """Print what installed packages add to the environment (JSON)."""
    try:
        exports = ops.env_exports(_manager(), names)
    except pm_facade.PmError as exc:
        _fail(exc)
        return
    click.echo(json.dumps(exports, indent=2, sort_keys=True))


@pm_group.command("doctor")
def doctor_command() -> None:
    """Check installed packages against the lock and the store."""
    try:
        rows = ops.doctor(_manager())
    except pm_facade.PmError as exc:
        _fail(exc)
        return
    if not rows:
        click.echo("pm has nothing locked or installed yet (`indus pm lock`, `indus pm install`).")
        return
    for row in rows:
        mark = "BAD" if row.bad else ("ok" if row.state == "ok" else "-")
        detail = f" ({row.detail})" if row.detail else ""
        click.echo(f"  {mark:<4} {row.name}: {row.state}{detail}")
    bad = [r for r in rows if r.bad]
    if bad:
        err("Run `indus pm repair` for damaged entries, `indus pm install` for missing ones.")
        raise click.exceptions.Exit(1)
    click.echo("The store is clean.")


@pm_group.command("repair")
def repair_command() -> None:
    """Rebuild damaged packages at their recorded versions (the lock is not changed)."""
    try:
        op = ops.repair(_manager())
    except pm_facade.PmError as exc:
        _fail(exc)
        return
    if not op.steps:
        click.echo("Nothing to repair.")
        return
    _steps(op)
    raise click.exceptions.Exit(op.exit_code)


@pm_group.command("gc")
def gc_command() -> None:
    """Remove store entries and downloads nothing installed refers to."""
    try:
        op = ops.gc(_manager())
    except pm_facade.PmError as exc:
        _fail(exc)
        return
    removed = [s for s in op.steps if s.name.startswith("remove ")]
    for step in removed:
        click.echo(f"  {step.name}")
    kept = next((s.detail for s in op.steps if s.name == "kept"), "")
    click.echo(f"gc: removed {len(removed)}, kept {kept}.")


@pm_group.command("bundle")
@click.option(
    "--out",
    required=True,
    type=click.Path(path_type=Path),
    help="A new or empty folder for the bundle.",
)
def bundle_command(out: Path) -> None:
    """Stage the store, facts and lock for an offline install elsewhere."""
    pm = _manager()
    try:
        op = ops.bundle(pm, out)
    except pm_facade.PmError as exc:
        _fail(exc)
        return
    _steps(op)
    click.echo(f"Bundle written to {out}; install it with `indus pm install --from {out}`.")


@pm_group.command("status")
@click.option("--json", "as_json", is_flag=True, help="Print the receipt as JSON.")
def status_command(as_json: bool) -> None:
    """Show the receipt of the last pm operation."""
    data = ops.status(_manager())
    if as_json:
        click.echo(json.dumps(data, indent=2, sort_keys=True))
        return
    receipt = data["receipt"]
    if receipt is None:
        click.echo("No pm operation has run yet.")
        return
    click.echo(
        f"Last: {receipt.get('operation')} {receipt.get('outcome')} "
        f"(exit {receipt.get('exit_code')}) at {receipt.get('finished_at')}"
    )
    for step in receipt.get("steps", []):
        word = "ok" if step.get("ok") else "failed"
        detail = f": {step.get('detail')}" if step.get("detail") else ""
        click.echo(f"  {word:<6} {step.get('name')}{detail}")


@pm_group.command("update")
@click.argument("names", nargs=-1)
@click.option("--check", is_flag=True, help="Only report; exit 1 when updates exist.")
@click.option("-y", "--yes", is_flag=True, help="Apply without asking.")
def update_command(names: tuple[str, ...], check: bool, yes: bool) -> None:
    """Move pins to the newest releases that are at least 14 days old."""
    pm = _manager()
    try:
        rows = ops.check_updates(pm, names)
    except pm_facade.PmError as exc:
        _fail(exc)
        return
    width = max((len(r.name) for r in rows), default=4)
    for row in rows:
        if row.changed:
            text = f"{row.locked or '-'} -> {row.newest}"
        else:
            text = row.reason or f"{row.locked} is the newest"
        click.echo(f"  {row.name:<{width}}  {text}")
    if any(r.reason.startswith("lookup failed") for r in rows):
        err("Some lookups failed; nothing was changed.")
        raise click.exceptions.Exit(1)
    changed = [r for r in rows if r.changed]
    if check or not changed:
        if not changed:
            click.echo("Everything is up to date.")
        raise click.exceptions.Exit(1 if changed else 0)
    if not _confirm(
        f"Re-pin {len(changed)} package(s) and reinstall the installed ones?",
        "Run `indus pm update --yes` to apply these updates.",
        yes,
    ):
        click.echo("Nothing was changed.")
        raise click.exceptions.Exit(1)
    try:
        op = ops.apply_updates(pm, changed)
    except pm_facade.PmError as exc:
        _fail(exc)
        return
    _steps(op)
    raise click.exceptions.Exit(op.exit_code)
