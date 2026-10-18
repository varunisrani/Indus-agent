"""``indus checkpoints``: status / list, prune, clear (P07-24).

Works on any ``backends.checkpoint_api.CheckpointStore``. Destructive actions ask first; without
a terminal they need ``-f`` and exit 1 otherwise. ``prune`` binds orphan deletion to the projects
it listed: one whose folder disappears after the list was shown is not deleted in that run.
"""

from __future__ import annotations

import click

from indus.backends import checkpoint_api
from indus.backends.checkpoint_api import CheckpointProject, CheckpointStore
from indus.cli.cmdkit import age_label, cli_state, err, group, human_size
from indus.cli.prompts import confirm, io_for

__all__ = ["checkpoints_group"]

_NONE = "No checkpoints have been taken in this profile."
_WORKDIR_WIDTH = 56


def _store() -> tuple[CheckpointStore | None, float]:
    scope = cli_state().scope
    return checkpoint_api.store_for(scope), scope.clock.now().timestamp()


def _short(path: str | None) -> str:
    text = path or "(unknown)"
    if len(text) > _WORKDIR_WIDTH:
        return "…" + text[-(_WORKDIR_WIDTH - 1) :]
    return text


def _age(project: CheckpointProject, now: float) -> str:
    return age_label(None if project.last_touch is None else now - project.last_touch)


def _status(limit: int) -> int:
    store, now = _store()
    if store is None:
        click.echo(_NONE)
        return 0
    projects = sorted(store.list_projects(), key=lambda p: p.last_touch or 0.0, reverse=True)
    click.echo(f"Checkpoint store: {store.base}")
    click.echo(f"Total size:       {human_size(store.size())}")
    click.echo(f"Projects:         {len(projects)}")
    if not projects:
        return 0
    click.echo("")
    click.echo(
        f"  {'WORKDIR':<{_WORKDIR_WIDTH}} {'COMMITS':>7} {'SIZE':>9} {'LAST USED':>10}  STATE"
    )
    for p in projects[: max(limit, 0)]:
        state = "live" if p.exists else "orphan"
        click.echo(
            f"  {_short(p.workdir):<{_WORKDIR_WIDTH}} {p.commits:>7} "
            f"{human_size(p.size_bytes):>9} {_age(p, now):>10}  {state}"
        )
    if len(projects) > limit:
        click.echo(f"  … and {len(projects) - limit} more (use --limit)")
    return 0


@group(
    "checkpoints",
    "Inspect and trim the file snapshots taken before risky edits.",
    short_help="Inspect and trim file checkpoints.",
    invoke_without_command=True,
)
@click.pass_context
def checkpoints_group(ctx: click.Context) -> int:
    if ctx.invoked_subcommand is None:
        return _status(20)
    return 0


_limit = click.option("--limit", type=int, default=20, show_default=True, help="Rows to show.")


@checkpoints_group.command("status")
@_limit
def status_command(limit: int) -> int:
    """Total size, project count and one row per project."""
    return _status(limit)


@checkpoints_group.command("list")
@_limit
def list_command(limit: int) -> int:
    """The same as status."""
    return _status(limit)


@checkpoints_group.command("prune")
@click.option(
    "--retention-days",
    type=click.IntRange(min=0),
    default=7,
    show_default=True,
    help="Delete projects not used for this many days.",
)
@click.option(
    "--max-size-mb",
    type=click.IntRange(min=0),
    default=500,
    show_default=True,
    help="Then delete the oldest projects until the store is at most this size.",
)
@click.option("--keep-orphans", is_flag=True, help="Keep projects whose folder no longer exists.")
@click.option("-f", "--force", is_flag=True, help="Do not ask before deleting orphans.")
def prune_command(retention_days: int, max_size_mb: int, keep_orphans: bool, force: bool) -> int:
    """Delete orphaned, unused and oversized checkpoint history."""
    state = cli_state()
    store, now = _store()
    if store is None:
        click.echo("Nothing to prune: " + _NONE[0].lower() + _NONE[1:])
        return 0
    projects = store.list_projects()
    allow: set[str] | None = None
    if not keep_orphans:
        orphans = [p for p in projects if not p.exists]
        allow = {p.key for p in orphans}
        if orphans and not force:
            click.echo(f"{len(orphans)} project(s) point at a folder that is not there now:")
            for p in orphans:
                click.echo(f"  {_short(p.workdir)}  ({p.commits} checkpoint(s))")
            click.echo(
                "A folder can also look missing when a drive or network share is not mounted; "
                "--keep-orphans leaves these alone."
            )
            answer = confirm(
                io_for(
                    state, "Nothing was pruned: pass -f (or --keep-orphans) without a terminal."
                ),
                "Delete the checkpoints of these folders?",
                assume_yes=False,
            )
            if answer is None:
                return 1
            if not answer:
                click.echo("Nothing was pruned.")
                return 1
    plan = checkpoint_api.plan_prune(
        projects,
        now=now,
        retention_days=retention_days,
        max_size_bytes=max_size_mb * 1024 * 1024,
        delete_orphans=not keep_orphans,
        orphan_allowlist=allow,
    )
    if not plan.keys:
        click.echo("Nothing to prune.")
        return 0
    result = store.prune(plan)
    click.echo(f"Scanned:          {result.scanned or len(projects)} project(s)")
    click.echo(f"Deleted orphaned: {result.deleted_orphan}")
    click.echo(f"Deleted unused:   {result.deleted_stale}")
    click.echo(f"Deleted for size: {result.deleted_oversize}")
    click.echo(f"Space freed:      {human_size(result.bytes_freed)}")
    if result.errors:
        err(f"{result.errors} project(s) could not be deleted (see the log).")
        return 1
    return 0


@checkpoints_group.command("clear")
@click.option("-f", "--force", is_flag=True, help="Do not ask first.")
def clear_command(force: bool) -> int:
    """Delete every checkpoint of every folder (cannot be undone)."""
    state = cli_state()
    store, _now = _store()
    if store is None:
        click.echo("Nothing to clear: " + _NONE[0].lower() + _NONE[1:])
        return 0
    projects = store.list_projects()
    size = store.size()
    if not projects and size == 0:
        click.echo("Nothing to clear.")
        return 0
    click.echo(
        f"This deletes the whole checkpoint store at {store.base}: {len(projects)} project(s), "
        f"{human_size(size)}. Every /rollback point is lost."
    )
    answer = confirm(
        io_for(state, "Nothing was cleared: pass -f to clear without a terminal."),
        "Delete all checkpoints?",
        assume_yes=force,
    )
    if answer is None:
        return 1
    if not answer:
        click.echo("Nothing was cleared.")
        return 1
    result = store.clear()
    if not result.deleted:
        err("The checkpoint store could not be cleared:", *result.errors[:5])
        return 2
    click.echo(f"Cleared; {human_size(result.bytes_freed)} freed.")
    return 0
