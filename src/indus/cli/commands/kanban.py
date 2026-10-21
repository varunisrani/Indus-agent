"""``indus kanban``: the task board from a terminal or a script (P24-13).

Verbs: ``board [SLUG]`` (list boards, or make SLUG the current one), ``create``, ``list`` (also the
bare command), ``show``, ``move`` (``ready``/``todo`` unblock or send back from review,
``blocked``, ``done``, ``archived``), ``comment``, ``attach``, ``assign``, ``review`` (``--approve``
or ``--changes REASON``), ``dispatch`` (one dispatcher pass, for hosts without the gateway daemon)
and ``archive``. ``--board SLUG`` before the verb picks another board for one command.

Nothing here asks a question, so every verb behaves the same without a terminal (R04.19): missing
arguments are usage errors (exit 2), refusals print the reason on stderr and exit 1. ``--json``
prints machine-readable output on the verbs that show data.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import click

from indus.board.dispatcher import Dispatcher, DispatchSettings
from indus.cli.cmdkit import cli_state, config_of, err, group
from indus.foundation.scope import Scope
from indus.state.board import (
    BoardError,
    BoardStore,
    Task,
    current_board,
    list_boards,
    normalize_slug,
    set_current_board,
)

__all__ = ["kanban_group", "task_line"]

MOVE_TARGETS = ("ready", "todo", "blocked", "done", "archived")
AUTHOR = "user"


def task_line(task: Task) -> str:
    who = task.assignee or "-"
    flags = ""
    if task.escalated:
        flags = " (needs a person)"
    elif task.scheduled:
        flags = " (scheduled)"
    return f"  {task.id}  {task.status:<8} {who:<14} {task.title}{flags}"


def _slug(ctx: click.Context, scope: Scope) -> str:
    chosen = ctx.find_root().meta.get("indus.kanban.board")
    return normalize_slug(chosen) if chosen else current_board(scope.root)


@contextmanager
def _store(ctx: click.Context | None = None) -> Iterator[BoardStore]:
    scope = cli_state().scope
    store = BoardStore(scope, _slug(ctx or click.get_current_context(), scope))
    try:
        yield store
    finally:
        store.close()


def _fail(exc: Exception) -> int:
    err(f"indus kanban: {exc}")
    return 1


def _print_json(value: Any) -> int:
    click.echo(json.dumps(value, indent=2, ensure_ascii=False, default=str))
    return 0


def _list(status: str | None, assignee: str | None, archived: bool, as_json: bool) -> int:
    try:
        with _store() as store:
            store.recompute_ready()
            tasks = store.list(status=status, assignee=assignee, include_archived=archived)
            slug = store.slug
    except BoardError as exc:
        return _fail(exc)
    if as_json:
        return _print_json([t.view() for t in tasks])
    click.echo(f"Board: {slug}")
    if not tasks:
        click.echo("  (no tasks)")
    for task in tasks:
        click.echo(task_line(task))
    return 0


@group(
    "kanban",
    "The task board profiles hand work to each other on. Bare `indus kanban` lists the tasks.",
    short_help="The task board: create, list and move tasks.",
    invoke_without_command=True,
)
@click.option("--board", "board", metavar="SLUG", default=None, help="Use this board.")
@click.pass_context
def kanban_group(ctx: click.Context, board: str | None) -> int:
    if board:
        try:
            ctx.find_root().meta["indus.kanban.board"] = normalize_slug(board)
        except BoardError as exc:
            raise click.BadParameter(str(exc), param_hint="--board") from exc
    if ctx.invoked_subcommand is None:
        return _list(None, None, False, False)
    return 0


@kanban_group.command("board")
@click.argument("slug", required=False)
def board_command(slug: str | None) -> int:
    """List boards, or make SLUG the current board (created when new)."""
    scope = cli_state().scope
    if slug is None:
        current = current_board(scope.root)
        for name in list_boards(scope.root):
            click.echo(f"{'*' if name == current else ' '} {name}")
        return 0
    try:
        chosen = set_current_board(scope.root, slug)
        BoardStore(scope, chosen).close()
    except BoardError as exc:
        return _fail(exc)
    click.echo(f"Current board: {chosen}")
    return 0


def _workspace(value: str | None) -> tuple[str, str | None]:
    if not value or value == "scratch":
        return "scratch", None
    if value == "worktree":
        return "worktree", None
    for prefix, kind in (("dir:", "dir"), ("worktree:", "worktree")):
        if value.startswith(prefix):
            path = value[len(prefix) :].strip()
            if not path:
                raise click.BadParameter(f"{prefix} needs a path", param_hint="--workspace")
            return kind, str(Path(path).expanduser().resolve())
    raise click.BadParameter(
        "use scratch, worktree, worktree:PATH or dir:PATH", param_hint="--workspace"
    )


@kanban_group.command("create")
@click.argument("title")
@click.option("--body", default=None, help="What to do, in full.")
@click.option("--assignee", default=None, metavar="PROFILE", help="Profile that does it.")
@click.option("--parent", "parents", multiple=True, metavar="ID", help="Must finish first.")
@click.option("--priority", type=int, default=0, help="Higher runs first.")
@click.option("--workspace", default=None, help="scratch (default), worktree[:REPO], dir:PATH.")
@click.option("--max-runtime", type=int, default=None, metavar="SECONDS")
@click.option("--json", "as_json", is_flag=True, help="Print the task as JSON.")
def create_command(
    title: str,
    body: str | None,
    assignee: str | None,
    parents: tuple[str, ...],
    priority: int,
    workspace: str | None,
    max_runtime: int | None,
    as_json: bool,
) -> int:
    """Create a task (ready, or waiting until its parents are done)."""
    kind, path = _workspace(workspace)
    try:
        with _store() as store:
            task = store.create(
                title,
                body=body,
                assignee=assignee,
                created_by=AUTHOR,
                parents=list(parents),
                priority=priority,
                workspace_kind=kind,
                workspace_path=path,
                max_runtime_s=max_runtime,
            )
    except BoardError as exc:
        return _fail(exc)
    if as_json:
        return _print_json(task.view())
    click.echo(f"Created {task.id} ({task.status}, assignee {task.assignee or '-'})")
    return 0


@kanban_group.command("list")
@click.option("--status", default=None, help="Only tasks with this status.")
@click.option("--assignee", default=None, metavar="PROFILE", help="Only this profile's tasks.")
@click.option("--archived", is_flag=True, help="Include archived tasks.")
@click.option("--json", "as_json", is_flag=True, help="Print JSON.")
def list_command(status: str | None, assignee: str | None, archived: bool, as_json: bool) -> int:
    """List tasks, highest priority first."""
    return _list(status, assignee, archived, as_json)


@kanban_group.command("show")
@click.argument("task_id")
@click.option("--json", "as_json", is_flag=True, help="Print JSON.")
def show_command(task_id: str, as_json: bool) -> int:
    """A task with its links, comments, runs and recent events."""
    try:
        with _store() as store:
            task = store.require(task_id)
            data = {
                "task": task.view(),
                "parents": store.parents(task_id),
                "children": store.children(task_id),
                "comments": [c.view() for c in store.comments(task_id)],
                "attachments": [a.view() for a in store.attachments(task_id)],
                "runs": [r.view() for r in store.runs(task_id)],
                "events": [e.view() for e in store.events(task_id, limit=20)],
            }
    except BoardError as exc:
        return _fail(exc)
    if as_json:
        return _print_json(data)
    click.echo(f"{task.id}  {task.title}")
    click.echo(f"  status:    {task.status}" + (" (needs a person)" if task.escalated else ""))
    click.echo(f"  assignee:  {task.assignee or '-'}")
    click.echo(
        f"  workspace: {task.workspace_kind}"
        + (f" {task.workspace_path}" if task.workspace_path else "")
    )
    if task.block_kind:
        click.echo(f"  blocked:   {task.block_kind} (x{task.block_recurrences})")
    if data["parents"]:
        click.echo("  parents:   " + ", ".join(data["parents"]))
    if data["children"]:
        click.echo("  children:  " + ", ".join(data["children"]))
    if task.body:
        click.echo("")
        click.echo(task.body)
    for comment in data["comments"]:
        click.echo(f"  [{comment['author']}] {comment['body']}")
    for run in data["runs"]:
        note = run["summary"] or run["error"] or ""
        click.echo(
            f"  run {run['id']} {run['outcome'] or 'running'} {run['profile'] or '-'} {note}"
        )
    return 0


@kanban_group.command("move")
@click.argument("task_id")
@click.argument("status", type=click.Choice(MOVE_TARGETS))
@click.option("--reason", default=None, help="Why (shown on the board).")
def move_command(task_id: str, status: str, reason: str | None) -> int:
    """Move a task by hand: ready/todo, blocked, done or archived."""
    try:
        with _store() as store:
            task = store.require(task_id)
            if status in ("ready", "todo"):
                if task.status == "review":
                    moved = store.reopen_review(task_id, reason=reason, author=AUTHOR)
                elif task.status == "blocked":
                    moved = store.unblock(task_id)
                else:
                    raise BoardError(
                        f"{task_id} is {task.status}; only blocked or review tasks go back"
                    )
            elif status == "blocked":
                moved = store.block(
                    task_id, reason=reason or "Stopped by hand.", kind="needs_input", force=True
                )
            elif status == "done":
                moved = store.complete(
                    task_id, summary=reason or "Marked done by hand.", force=True
                )
            else:
                store.archive(task_id)
                moved = store.require(task_id)
    except BoardError as exc:
        return _fail(exc)
    click.echo(f"{moved.id} is now {moved.status}")
    return 0


@kanban_group.command("comment")
@click.argument("task_id")
@click.argument("text")
def comment_command(task_id: str, text: str) -> int:
    """Add a comment to a task."""
    try:
        with _store() as store:
            number = store.comment(task_id, AUTHOR, text)
    except BoardError as exc:
        return _fail(exc)
    click.echo(f"Comment {number} added to {task_id}")
    return 0


@kanban_group.command("attach")
@click.argument("task_id")
@click.argument("file", type=click.Path(dir_okay=False, path_type=Path))
def attach_command(task_id: str, file: Path) -> int:
    """Attach a file (at most 25 MiB)."""
    try:
        data = file.read_bytes()
    except OSError as exc:
        err(f"indus kanban: cannot read {file}: {exc.strerror or exc}")
        return 1
    try:
        with _store() as store:
            saved = store.attach_bytes(task_id, file.name, data, uploaded_by=AUTHOR)
    except BoardError as exc:
        return _fail(exc)
    click.echo(f"Attached {saved.filename} ({saved.size} bytes) to {task_id}")
    return 0


@kanban_group.command("assign")
@click.argument("task_id")
@click.argument("profile")
def assign_command(task_id: str, profile: str) -> int:
    """Give a task to a profile ('none' takes it away)."""
    target = None if profile.lower() in ("none", "-") else profile
    try:
        with _store() as store:
            task = store.assign(task_id, target)
    except BoardError as exc:
        return _fail(exc)
    click.echo(f"{task.id} assigned to {task.assignee or 'nobody'}")
    return 0


@kanban_group.command("review")
@click.argument("task_id")
@click.option("--approve", is_flag=True, help="Accept the work: the task is done.")
@click.option("--changes", default=None, metavar="REASON", help="Send it back with the changes.")
def review_command(task_id: str, approve: bool, changes: str | None) -> int:
    """Decide on a task waiting in review."""
    if approve == (changes is not None):
        raise click.UsageError("give exactly one of --approve or --changes REASON")
    try:
        with _store() as store:
            task = store.require(task_id)
            if task.status != "review":
                raise BoardError(f"{task_id} is {task.status}, not in review")
            if approve:
                moved = store.complete(task_id, force=True)
            else:
                moved = store.reopen_review(task_id, reason=changes, author=AUTHOR)
    except BoardError as exc:
        return _fail(exc)
    if approve:
        click.echo(f"{moved.id} approved: done")
    else:
        click.echo(f"{moved.id} sent back to {moved.assignee or '-'} ({moved.status})")
    return 0


@kanban_group.command("dispatch")
@click.option("--dry-run", is_flag=True, help="Only say what would start.")
@click.option("--json", "as_json", is_flag=True, help="Print the pass report as JSON.")
def dispatch_command(dry_run: bool, as_json: bool) -> int:
    """Run one dispatcher pass: book finished workers and start ready tasks."""
    scope = cli_state().scope
    try:
        with _store() as store:
            settings = DispatchSettings.from_config(config_of(scope))
            report = Dispatcher(store, scope=scope, settings=settings).tick(dry_run=dry_run)
    except BoardError as exc:
        return _fail(exc)
    if as_json:
        return _print_json(report.as_dict())
    verb = "Would start" if dry_run else "Started"
    for task_id, profile in report.spawned:
        click.echo(f"{verb} {task_id} for {profile}")
    for label, ids in (
        ("Rate limited", report.rate_limited),
        ("Crashed", report.crashed),
        ("Ended without a board call", report.violations),
        ("Blocked", report.blocked),
        ("Reclaimed", report.reclaimed),
        ("Unassigned", report.skipped_unassigned),
        ("Unknown profile", report.skipped_unknown_profile),
        ("Lane cooling down", report.skipped_cooled),
    ):
        if ids:
            click.echo(f"{label}: {', '.join(ids)}")
    for problem in report.errors:
        err(problem)
    if not report.spawned:
        click.echo("Nothing to start.")
    return 0


@kanban_group.command("archive")
@click.argument("task_id")
def archive_command(task_id: str) -> int:
    """Archive a task (a running worker's run is closed)."""
    try:
        with _store() as store:
            store.archive(task_id)
    except BoardError as exc:
        return _fail(exc)
    click.echo(f"{task_id} archived")
    return 0
