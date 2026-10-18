"""``indus completion [bash|zsh|fish]``: a shell script that asks ``indus`` itself for candidates.

The script is click's shell completion for the live command tree, so commands added later complete
without regenerating anything. Three options complete values (hooked up in ``cli.app``):
``-p/--profile`` live profile names, ``-c/--continue`` and ``-r/--resume`` titles of recent
sessions, ``-t/--toolsets`` toolset names (the last item of a comma list). Session titles come from
the store opened read-only, in a worker thread the caller waits on for at most ``TITLE_BUDGET_S``;
a slow or missing store gives no titles rather than a hung shell.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path

import click
from click.shell_completion import CompletionItem, get_completion_class

from indus.cli.args import CliState
from indus.foundation.home import resolve_home, root_of
from indus.foundation.profiles import list_profiles

__all__ = [
    "COMPLETE_VAR",
    "SHELLS",
    "TITLE_BUDGET_S",
    "complete_profiles",
    "complete_session_titles",
    "complete_toolsets",
    "completion_command",
    "completion_script",
    "session_titles",
]

SHELLS = ("bash", "zsh", "fish")
COMPLETE_VAR = "_INDUS_COMPLETE"
PROG = "indus"
TITLE_BUDGET_S = 1.0
TITLE_LIMIT = 200
_HIDDEN_SOURCES = ("subagent", "tool", "oneshot", "kanban")

type Opener = Callable[[Path], sqlite3.Connection]


def completion_script(shell: str) -> str:
    from indus.cli.app import cli

    complete_class = get_completion_class(shell)
    if complete_class is None:
        raise click.BadParameter(f"unsupported shell {shell!r}")
    return complete_class(cli, {}, PROG, COMPLETE_VAR).source()


def _home(ctx: click.Context | None) -> Path:
    state = ctx.find_object(CliState) if ctx is not None else None
    return state.scope.home if state is not None else resolve_home()


def complete_profiles(
    ctx: click.Context | None, param: click.Parameter | None, incomplete: str
) -> list[CompletionItem]:
    try:
        names = list_profiles(root_of(_home(ctx)))
    except OSError:
        return []
    return [CompletionItem(n) for n in names if n.startswith(incomplete.lower())]


def _open_titles(path: Path) -> sqlite3.Connection:
    from indus.state.readpool import open_read_only

    return open_read_only(path, timeout=TITLE_BUDGET_S)


def _read_titles(path: Path, opener: Opener) -> list[str]:
    conn = opener(path)
    try:
        marks = ",".join("?" for _ in _HIDDEN_SOURCES)
        rows = conn.execute(
            "SELECT title FROM sessions WHERE title IS NOT NULL AND title != '' "
            f"AND (source IS NULL OR source NOT IN ({marks})) "
            "ORDER BY started_at DESC LIMIT ?",
            (*_HIDDEN_SOURCES, TITLE_LIMIT),
        ).fetchall()
    finally:
        conn.close()
    return [str(row[0]) for row in rows]


def session_titles(
    home: Path, *, budget: float = TITLE_BUDGET_S, opener: Opener | None = None
) -> list[str]:
    """Recent session titles from ``home``'s store, or [] when it is missing, broken or slow."""
    from indus.state.connection import DB_NAME

    path = home / DB_NAME
    if not path.is_file():
        return []
    box: dict[str, list[str]] = {}

    def work() -> None:
        try:
            box["titles"] = _read_titles(path, opener or _open_titles)
        except Exception:
            box["titles"] = []

    worker = threading.Thread(target=work, name="indus-complete-titles", daemon=True)
    worker.start()
    worker.join(budget)
    return box.get("titles", [])


def complete_session_titles(
    ctx: click.Context | None, param: click.Parameter | None, incomplete: str
) -> list[CompletionItem]:
    needle = incomplete.lower()
    titles = session_titles(_home(ctx))
    return [CompletionItem(t) for t in titles if t.lower().startswith(needle)]


def complete_toolsets(
    ctx: click.Context | None, param: click.Parameter | None, incomplete: str
) -> list[CompletionItem]:
    from indus.tools import toolsets

    head, sep, last = incomplete.rpartition(",")
    prefix = f"{head}{sep}"
    taken = {w.strip() for w in head.split(",") if w.strip()}
    names = [*sorted(toolsets.TOOLSETS), "all"]
    return [CompletionItem(f"{prefix}{n}") for n in names if n.startswith(last) and n not in taken]


@click.command(
    "completion",
    help="Print a completion script. Load it with, for example, "
    '`eval "$(indus completion bash)"` in ~/.bashrc, `eval "$(indus completion zsh)"` in '
    "~/.zshrc, or `indus completion fish | source` in fish.",
)
@click.argument("shell", required=False, default="bash", type=click.Choice(SHELLS))
def completion_command(shell: str) -> int:
    click.echo(completion_script(shell), nl=False)
    return 0
