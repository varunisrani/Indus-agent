"""``indus worktree`` and ``/worktree``: list, make and prune session worktrees (P11-20).

Both work on the repository holding the current folder (the session's folder for the slash
command): ``list`` (also bare) shows each tree under ``.worktrees/`` with its branch, age and what
would keep it; ``new [NAME]`` makes one (``backends.worktrees.create``) and prints where it is;
``prune [--dry-run]`` removes old trees that lose nothing and lists the rest with reasons. A
worktree is never removed when its session ends; ``indus -w`` (``chat``) starts a session in a new
one. Outside a git repository every action says so and exits 1.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import click

from indus.backends import worktrees
from indus.cli.cmdkit import cli_state, config_of, err, group
from indus.cli.slash.context import SlashContext, SlashResult
from indus.foundation.scope import Scope

__all__ = ["HANDLERS", "list_lines", "new_lines", "prune_lines", "worktree_group", "worktree_slash"]

_NOT_A_REPO = "This folder is not inside a git repository; worktrees need one."
_USAGE = "Usage: /worktree [list] | new [name] | prune [--dry-run]"

type Lines = tuple[int, list[str]]


def _age(hours: float | None) -> str:
    if hours is None:
        return "?"
    if hours < 48:
        return f"{hours:.0f}h"
    return f"{hours / 24:.0f}d"


def _scoped(scope: Scope) -> Scope:
    return scope if scope.config is not None else scope.with_config(config_of(scope))


def list_lines(scope: Scope, folder: str | Path) -> Lines:
    try:
        trees = worktrees.statuses(_scoped(scope), folder)
    except worktrees.WorktreeError:
        return 1, [_NOT_A_REPO]
    if not trees:
        return 0, ["No worktrees under .worktrees/ here. `indus -w` starts a session in a new one."]
    lines = [f"  {'TREE':<28} {'BRANCH':<34} {'AGE':>5}  STATE"]
    for tree in trees:
        state = "clean" if not tree.blockers else "keep: " + "; ".join(tree.blockers)
        lines.append(
            f"  {Path(tree.path).name[:28]:<28} {(tree.branch or '(detached)')[:34]:<34} "
            f"{_age(tree.age_hours):>5}  {state}"
        )
    return 0, lines


def new_lines(scope: Scope, folder: str | Path, name: str | None) -> Lines:
    try:
        info = worktrees.create(_scoped(scope), folder, name or None)
    except worktrees.WorktreeError as exc:
        return 1, [str(exc)]
    return 0, [
        f"Worktree created: {info.path}",
        f"  Branch: {info.branch} (from {info.base})",
        f"  Start a session there with: indus --in {info.path}",
    ]


def prune_lines(scope: Scope, folder: str | Path, *, dry_run: bool) -> Lines:
    try:
        report = worktrees.prune(_scoped(scope), folder, dry_run=dry_run)
    except worktrees.WorktreeError:
        return 1, [_NOT_A_REPO]
    verb = "Would remove" if dry_run else "Removed"
    lines = [f"{verb}: {path}" for path in report.removed]
    lines += [f"Kept: {path} ({reason})" for path, reason in report.kept]
    if not lines:
        lines.append("No worktrees under .worktrees/ here.")
    elif not report.removed:
        lines.append("Nothing to prune: every tree is recent or holds work.")
    return 0, lines


def _echo(result: Lines) -> int:
    code, lines = result
    for line in lines:
        if code:
            err(line)
        else:
            click.echo(line)
    return code


@group(
    "worktree",
    "List, create and prune the session worktrees under .worktrees/ of this repository.",
    short_help="Create, list and prune session worktrees.",
    invoke_without_command=True,
)
@click.pass_context
def worktree_group(ctx: click.Context) -> int:
    if ctx.invoked_subcommand is None:
        return _echo(list_lines(cli_state().scope, Path.cwd()))
    return 0


@worktree_group.command("list")
def list_command() -> int:
    """Each tree with its branch, age and what keeps it."""
    return _echo(list_lines(cli_state().scope, Path.cwd()))


@worktree_group.command("new")
@click.argument("name", required=False)
def new_command(name: str | None) -> int:
    """Make a worktree on a new branch indus/NAME."""
    return _echo(new_lines(cli_state().scope, Path.cwd(), name))


@worktree_group.command("prune")
@click.option("--dry-run", is_flag=True, help="Only say what would be removed.")
def prune_command(dry_run: bool) -> int:
    """Remove old trees that lose nothing; list the rest with reasons."""
    return _echo(prune_lines(cli_state().scope, Path.cwd(), dry_run=dry_run))


# -- /worktree (classic CLI) -----------------------------------------------------------------------


def _session_folder(ctx: SlashContext) -> str:
    found = ctx.session_cwd() if ctx.session_cwd is not None else None
    return found or os.getcwd()


def worktree_slash(ctx: SlashContext, args: str) -> SlashResult:
    words = args.split()
    action = words[0].lower() if words else "list"
    folder = _session_folder(ctx)
    result: Lines
    if action == "list" and len(words) <= 1:
        result = list_lines(ctx.scope, folder)
    elif action == "new" and len(words) <= 2:
        result = new_lines(ctx.scope, folder, words[1] if len(words) == 2 else None)
    elif action == "prune" and words[1:] in ([], ["--dry-run"]):
        result = prune_lines(ctx.scope, folder, dry_run=words[1:] == ["--dry-run"])
    else:
        result = (2, [_USAGE])
    ctx.say(*result[1])
    return SlashResult()


HANDLERS: dict[str, Callable[[SlashContext, str], SlashResult]] = {"worktree": worktree_slash}
