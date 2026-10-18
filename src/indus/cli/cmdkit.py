"""Small helpers the maintenance commands share: the process state, stderr lines and the size /
age / mask wording used in their tables. Questions (yes/no included) live in ``indus.cli.prompts``.
"""

from __future__ import annotations

from typing import Any

import click

from indus.cli.args import CliState
from indus.cli.lazy import LazyGroup
from indus.foundation import config as config_mod
from indus.foundation.scope import Scope

__all__ = [
    "age_label",
    "cli_state",
    "config_of",
    "err",
    "group",
    "human_size",
    "masked",
]

MASK_KEEP = 4
MASK_MIN = 12


def cli_state() -> CliState:
    """The ``CliState`` of the running click command."""
    ctx = click.get_current_context()
    state = ctx.find_object(CliState)
    if state is None:
        raise click.UsageError(f"{ctx.command_path} was started without its process state")
    return state


def config_of(scope: Scope) -> Any:
    """The scope's config view (loaded on demand when the scope carries none)."""
    if scope.config is not None:
        return scope.config
    return config_mod.load(scope)


def err(*lines: str) -> None:
    for line in lines:
        click.echo(line, err=True)


def human_size(size: int | None) -> str:
    if size is None:
        return "?"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def age_label(seconds: float | None) -> str:
    """``42s ago`` / ``5m ago`` / ``3h ago`` / ``2d ago``; ``—`` unknown, ``now`` in the future."""
    if seconds is None:
        return "—"
    if seconds < 0:
        return "now"
    for limit, size, unit in ((60, 1, "s"), (3600, 60, "m"), (86400, 3600, "h")):
        if seconds < limit:
            return f"{int(seconds // size)}{unit} ago"
    return f"{int(seconds // 86400)}d ago"


def masked(value: str) -> str:
    """First and last four characters of a long value; short values are never partly shown."""
    if len(value) < MASK_MIN:
        return "[set]"
    return f"{value[:MASK_KEEP]}…{value[-MASK_KEEP:]}"


def group(name: str, help_text: str, **kwargs: Any) -> Any:
    """A command group that answers an unknown subcommand like the top level does."""
    return click.group(name=name, cls=LazyGroup, registry=(), help=help_text, **kwargs)
