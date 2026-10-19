"""``indus journey`` (alias ``learning``): what this profile has learned, on a timeline (P09-23).

Without a verb it prints the timeline, newest first; ``--animate`` plays it in from the oldest
node (``--fps``, default 8), ``--width`` sets the line width and ``--no-color`` drops the ANSI
colours (they are also off when stdout is not a terminal). ``list [--json]`` prints the node ids,
``delete <id> [-y]`` archives a skill or removes a memory entry, ``edit <id>`` opens it in
``$VISUAL`` / ``$EDITOR`` (default ``vi``). ``timeline_lines`` and ``delete_lines`` are shared with
``/journey``.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

import click

from indus.cli.cmdkit import cli_state, err, group
from indus.cli.prompts import confirm, io_for
from indus.foundation.clock import SystemClock
from indus.foundation.scope import Scope
from indus.learning.journey import JourneyError, Node, delete_node, edit_node, node_text, nodes

__all__ = ["delete_lines", "journey_group", "list_lines", "timeline_lines"]

EMPTY = "Nothing learned yet: skills the agent writes or uses, and its memories, will show here."
_GLYPH = {"skill": "●", "memory": "◆"}
_COLOR = {"skill": "36", "memory": "35"}
DEFAULT_WIDTH = 90
FRAMES = 24

SLEEP: Callable[[float], None] = SystemClock().sleep_sync
"""Tests replace this so ``--animate`` does not wait."""


def _paint(text: str, code: str, color: bool) -> str:
    return f"\x1b[{code}m{text}\x1b[0m" if color else text


def _fit(text: str, width: int) -> str:
    return text if len(text) <= width else text[: max(1, width - 1)] + "…"


def _row(node: Node, width: int, color: bool) -> str:
    date = (node.when or "unknown date")[:10]
    detail = node.category if node.kind == "skill" else f"{node.store} memory"
    if node.kind == "skill":
        extras = [f"used {node.use_count}"]
        if node.state != "active":
            extras.append(node.state)
        if node.pinned:
            extras.append("pinned")
        detail = f"{detail}, {', '.join(extras)}"
    glyph = _paint(_GLYPH.get(node.kind, "·"), _COLOR.get(node.kind, "0"), color)
    body = _fit(f"{node.label}  ({detail})", max(20, width - 14))
    return f"{date}  {glyph} {body}"


def timeline_lines(
    found: Sequence[Node], *, width: int = DEFAULT_WIDTH, color: bool = False
) -> list[str]:
    if not found:
        return [EMPTY]
    skills = sum(1 for n in found if n.kind == "skill")
    head = f"Journey: {skills} skill(s), {len(found) - skills} memory entr(ies), newest first"
    return [_paint(head, "1", color), *(_row(n, width, color) for n in found)]


def list_lines(found: Sequence[Node]) -> list[str]:
    if not found:
        return [EMPTY]
    return [f"{n.id}  {_GLYPH.get(n.kind, '·')} {n.label}  {(n.when or '')[:10]}" for n in found]


def delete_lines(scope: Scope, node_id: str) -> tuple[bool, list[str]]:
    try:
        return True, [delete_node(scope, node_id)]
    except JourneyError as exc:
        return False, [str(exc)]


def payload(found: Sequence[Node]) -> dict[str, object]:
    return {
        "nodes": [n.as_dict() for n in found],
        "counts": {
            "skills": sum(1 for n in found if n.kind == "skill"),
            "memories": sum(1 for n in found if n.kind == "memory"),
        },
    }


def _animate(found: Sequence[Node], *, width: int, color: bool, fps: int) -> None:
    oldest_first = list(reversed(found))
    steps = min(FRAMES, len(oldest_first))
    delay = 1.0 / max(1, min(fps, 60))
    for frame in range(1, steps + 1):
        shown = oldest_first[: round(len(oldest_first) * frame / steps)]
        if color:
            click.echo("\x1b[2J\x1b[H", nl=False)
        else:
            click.echo(f"-- {len(shown)}/{len(oldest_first)} --")
        for line in timeline_lines(list(reversed(shown)), width=width, color=color):
            click.echo(line)
        if frame < steps:
            SLEEP(delay)


@group("journey", "What the agent has learned, on a timeline.", invoke_without_command=True)
@click.option("--animate", is_flag=True, help="Play the timeline in from the oldest node.")
@click.option("--fps", type=int, default=8, show_default=True, help="Frames per second.")
@click.option("--width", type=int, default=None, help="Line width (default: the terminal's).")
@click.option("--no-color", is_flag=True, help="Plain text, no colours.")
@click.option("--json", "as_json", is_flag=True, help="Print the nodes as JSON.")
@click.pass_context
def journey_group(
    ctx: click.Context, animate: bool, fps: int, width: int | None, no_color: bool, as_json: bool
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    state = cli_state()
    found = nodes(state.scope)
    if as_json:
        click.echo(json.dumps(payload(found), indent=2, ensure_ascii=False))
        return
    cols = width or shutil.get_terminal_size((DEFAULT_WIDTH, 24)).columns
    color = not no_color and state.stdout_isatty()
    if animate and found:
        _animate(found, width=cols, color=color, fps=fps)
        return
    for line in timeline_lines(found, width=cols, color=color):
        click.echo(line)


@journey_group.command("list")
@click.option("--json", "as_json", is_flag=True, help="Print the nodes as JSON.")
def list_command(as_json: bool) -> None:
    """Node ids for delete and edit."""
    found = nodes(cli_state().scope)
    if as_json:
        click.echo(json.dumps(payload(found), indent=2, ensure_ascii=False))
        return
    for line in list_lines(found):
        click.echo(line)


@journey_group.command("delete")
@click.argument("node_id")
@click.option("--yes", "-y", is_flag=True, help="Do not ask first.")
def delete_command(node_id: str, yes: bool) -> None:
    """Archive a learned skill or remove a memory entry."""
    state = cli_state()
    try:
        kind, text = node_text(state.scope, node_id)
    except JourneyError as exc:
        err(str(exc))
        raise click.exceptions.Exit(1) from None
    label = node_id if kind == "skill" else text.splitlines()[0][:60]
    io = io_for(state, "Deleting needs a terminal to confirm, or -y.")
    answer = confirm(io, f"Delete {label!r}?", assume_yes=yes)
    if answer is None:
        raise click.exceptions.Exit(2)
    if not answer:
        raise click.exceptions.Exit(1)
    ok, lines = delete_lines(state.scope, node_id)
    for line in lines:
        (click.echo if ok else err)(line)
    if not ok:
        raise click.exceptions.Exit(1)


def _editor_runner(scope: Scope) -> Callable[[Path], bool]:
    env = dict(scope.env)
    command = shlex.split(env.get("VISUAL") or env.get("EDITOR") or "vi")

    def run(path: Path) -> bool:
        try:
            code = subprocess.call([*command, str(path)], env=env)
        except OSError as exc:
            err(f"could not start the editor: {exc}")
            return False
        return code == 0

    return run


@journey_group.command("edit")
@click.argument("node_id")
def edit_command(node_id: str) -> None:
    """Edit a learned skill or a memory entry in your editor."""
    scope = cli_state().scope
    try:
        message = edit_node(scope, node_id, _editor_runner(scope))
    except (JourneyError, OSError) as exc:
        err(str(exc))
        raise click.exceptions.Exit(1) from None
    click.echo(message)
