"""A click group whose subcommands are imported on first use, in a fixed order.

``LazyGroup(name, ..., registry=[("chat", "indus.cli.chat:chat_command", "Chat ..."), ...])``
lists the commands in registration order (that order is the ``--help`` order and never changes by
sorting) and imports ``module:attr`` only when the command is actually run or its own help is
asked for; the group's help shows the one-line text from the registry, so ``indus --help``
imports none of them. A first word that names no command prints the three-line
answer from ``indus.cli.unknown`` and exits 2; nested ``LazyGroup``s behave the same way.
"""

from __future__ import annotations

import importlib
from collections.abc import Sequence
from typing import Any

import click

from indus.cli.unknown import unknown_command_lines

__all__ = ["LazyGroup", "load_target"]


def load_target(target: str) -> click.Command:
    """Import ``module:attr`` and return the click command it names."""
    module_name, _, attr = target.partition(":")
    if not module_name or not attr:
        raise ValueError(f"lazy command target must be 'module:attr', got {target!r}")
    found = getattr(importlib.import_module(module_name), attr)
    if not isinstance(found, click.Command):
        raise TypeError(f"{target} is not a click command")
    return found


class LazyGroup(click.Group):
    """See the module notes."""

    def __init__(
        self,
        *args: Any,
        registry: Sequence[tuple[str, str, str]] = (),
        hidden: Sequence[tuple[str, str]] = (),
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._lazy: dict[str, str] = {}
        self._short: dict[str, str] = {}
        for name, target, short in registry:
            if name in self._lazy:
                raise ValueError(f"command {name!r} registered twice")
            self._lazy[name] = target
            self._short[name] = short
        # Hidden commands load lazily like the others but are never listed (help, completion).
        for name, target in hidden:
            if name in self._lazy:
                raise ValueError(f"command {name!r} registered twice")
            self._lazy[name] = target
        self._order: list[str] = [name for name, _, _ in registry]

    @property
    def registered(self) -> list[str]:
        """Lazy command names in registration order."""
        return list(self._order)

    def list_commands(self, ctx: click.Context) -> list[str]:
        eager = [name for name in self.commands if name not in self._lazy]
        return [*self._order, *eager]

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        found = self.commands.get(cmd_name)
        if found is not None:
            return found
        target = self._lazy.get(cmd_name)
        if target is None:
            return None
        command = load_target(target)
        self.commands[cmd_name] = command
        return command

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        rows: list[tuple[str, str]] = []
        for name in self.list_commands(ctx):
            loaded = self.commands.get(name)
            if loaded is not None and loaded.hidden:
                continue
            if name in self._short or loaded is None:
                # The registry's one-liner wins, so the listing is the same whether or not the
                # command was imported earlier in this process.
                rows.append((name, self._short.get(name, "")))
            else:
                rows.append((name, loaded.get_short_help_str(limit=formatter.width)))
        if rows:
            with formatter.section("Commands"):
                formatter.write_dl(rows)

    def resolve_command(
        self, ctx: click.Context, args: list[str]
    ) -> tuple[str | None, click.Command | None, list[str]]:
        word = args[0]
        command = self.get_command(ctx, word)
        if command is None and not ctx.resilient_parsing and not word.startswith("-"):
            for line in unknown_command_lines(ctx.command_path, word, self.list_commands(ctx)):
                click.echo(line, err=True)
            ctx.exit(2)
        return super().resolve_command(ctx, args)
