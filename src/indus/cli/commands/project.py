"""``indus project``: named workspaces of this profile (P24-14).

Verbs: ``create NAME [FOLDER...]``, ``list`` (also the bare command), ``use [PROJECT]`` (no
argument clears the active project), ``add-path PROJECT FOLDER``, ``remove-path PROJECT FOLDER``
and ``archive PROJECT`` (``--restore`` brings it back). Projects are named by id, slug or name.
Nothing here asks a question, so the verbs behave the same without a terminal (R04.19).
``list --json`` prints the same rows as the ``projects.list`` method.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager

import click

from indus.cli.cmdkit import cli_state, err, group
from indus.service.projects_rpc import list_result
from indus.state.projects import FOLDER_KINDS, Project, ProjectsError, ProjectStore

__all__ = ["project_group"]


@contextmanager
def _store() -> Iterator[ProjectStore]:
    store = ProjectStore(cli_state().scope)
    try:
        yield store
    finally:
        store.close()


def _fail(exc: Exception) -> int:
    err(f"indus project: {exc}")
    return 1


def _describe(project: Project) -> None:
    click.echo(f"{project.slug}  [{project.id}]" + (" (archived)" if project.archived else ""))
    click.echo(f"  name:    {project.name}")
    for label, value in (
        ("about", project.description),
        ("board", project.board_slug),
        ("main", project.primary_path),
    ):
        if value:
            click.echo(f"  {label}:{' ' * (8 - len(label))}{value}")
    for folder in project.folders:
        mark = "*" if folder.primary else " "
        click.echo(f"   {mark} {folder.path} ({folder.kind})")


def _list(include_archived: bool, as_json: bool) -> int:
    try:
        with _store() as store:
            result = list_result(store, include_archived=include_archived)
    except ProjectsError as exc:
        return _fail(exc)
    if as_json:
        click.echo(json.dumps(result.to_wire(), indent=2, ensure_ascii=False))
        return 0
    if not result.projects:
        click.echo("No projects yet. Make one with `indus project create NAME [FOLDER...]`.")
        return 0
    for row in result.projects:
        mark = "*" if row.active else " "
        extra = " (archived)" if row.archived else ""
        click.echo(f"{mark} {row.slug:<24} {row.name}{extra}  [{row.folders} folder(s)]")
    return 0


@group(
    "project",
    "Projects group folders, repositories and sessions under one name (per profile).",
    short_help="Create, list and switch projects.",
    invoke_without_command=True,
)
@click.pass_context
def project_group(ctx: click.Context) -> int:
    if ctx.invoked_subcommand is None:
        return _list(False, False)
    return 0


@project_group.command("create")
@click.argument("name")
@click.argument("folders", nargs=-1)
@click.option("--slug", default=None, help="Short name (made from NAME when left out).")
@click.option("--primary", default=None, metavar="FOLDER", help="The main folder.")
@click.option("--description", default=None)
@click.option("--icon", default=None)
@click.option("--colour", "--color", "colour", default=None)
@click.option("--board", default=None, metavar="SLUG", help="Bind a task board.")
@click.option("--use", "use_it", is_flag=True, help="Also make it the active project.")
def create_command(
    name: str,
    folders: tuple[str, ...],
    slug: str | None,
    primary: str | None,
    description: str | None,
    icon: str | None,
    colour: str | None,
    board: str | None,
    use_it: bool,
) -> int:
    """Create a project owning FOLDERS (the first is the main folder)."""
    try:
        with _store() as store:
            made = store.create(
                name,
                folders=folders,
                primary=primary,
                slug=slug,
                description=description,
                icon=icon,
                colour=colour,
                board_slug=board,
            )
            if use_it:
                store.use(made.id)
    except ProjectsError as exc:
        return _fail(exc)
    click.echo(f"Created project {made.slug} ({made.id})")
    _describe(made)
    return 0


@project_group.command("list")
@click.option("--all", "include_archived", is_flag=True, help="Include archived projects.")
@click.option("--json", "as_json", is_flag=True, help="Print JSON (the projects.list rows).")
def list_command(include_archived: bool, as_json: bool) -> int:
    """List projects; * marks the active one."""
    return _list(include_archived, as_json)


@project_group.command("use")
@click.argument("project", required=False)
def use_command(project: str | None) -> int:
    """Make PROJECT the active project (no argument: none is active)."""
    try:
        with _store() as store:
            chosen = store.use(project)
    except ProjectsError as exc:
        return _fail(exc)
    click.echo(f"Active project: {chosen.slug}" if chosen else "No active project.")
    return 0


@project_group.command("add-path")
@click.argument("project")
@click.argument("folder")
@click.option("--kind", type=click.Choice(FOLDER_KINDS), default="folder", show_default=True)
@click.option("--primary", is_flag=True, help="Make it the main folder.")
@click.option("--label", default=None)
def add_path_command(project: str, folder: str, kind: str, primary: bool, label: str | None) -> int:
    """Add FOLDER to PROJECT."""
    try:
        with _store() as store:
            updated = store.add_path(project, folder, kind=kind, primary=primary, label=label)
    except ProjectsError as exc:
        return _fail(exc)
    click.echo(f"Added {store.norm(folder)} to {updated.slug}")
    return 0


@project_group.command("remove-path")
@click.argument("project")
@click.argument("folder")
def remove_path_command(project: str, folder: str) -> int:
    """Remove FOLDER from PROJECT (the main folder moves to the next one)."""
    try:
        with _store() as store:
            updated = store.remove_path(project, folder)
    except ProjectsError as exc:
        return _fail(exc)
    click.echo(f"Removed {folder} from {updated.slug}")
    return 0


@project_group.command("archive")
@click.argument("project")
@click.option("--restore", is_flag=True, help="Bring an archived project back.")
def archive_command(project: str, restore: bool) -> int:
    """Hide PROJECT from lists (its folders and sessions stay)."""
    try:
        with _store() as store:
            updated = store.archive(project, archived=not restore)
    except ProjectsError as exc:
        return _fail(exc)
    click.echo(f"{updated.slug} {'restored' if restore else 'archived'}")
    return 0
