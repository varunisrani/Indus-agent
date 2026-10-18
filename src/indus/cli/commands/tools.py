"""``indus tools``: which toolsets each platform gets, stored in ``platform_toolsets.<platform>``.

With no list saved, a platform uses its bundle (``indus-<platform>``). The rows people switch are
the plain toolsets (no ``includes``): the named sets of ``tools.toolsets`` plus toolsets that tools
registered themselves into. ``enable`` adds names to the saved list (starting from the bundle).
``disable`` first spells the saved list out as the rows it covers, then removes the name and every
row that only repeats its tools; disabling a bundle or composite removes only what it adds on top of
the shared core, the toolset resolver's own rule. Saves go through the P01 config writer.

Bare ``indus tools`` on a terminal is a checklist (``indus.cli.prompts``) followed, when web search
is on but no web provider is ready, by a provider choice and a masked key prompt; without a
terminal it lists.
"""

from __future__ import annotations

import difflib
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, cast

import click

from indus.cli.commands._support import err, fresh_view, scope_with_config, state_of
from indus.cli.lazy import LazyGroup
from indus.cli.prompts import (
    Cancelled,
    Interactive,
    ask_checklist,
    ask_choice,
    ask_secret,
    io_for,
)
from indus.foundation.config import writer
from indus.foundation.config.loader import ConfigView
from indus.foundation.errors import IndusError
from indus.foundation.scope import Scope
from indus.tools import toolsets
from indus.tools.registry import ToolEntry, ToolRegistry

__all__ = [
    "Row",
    "default_bundle",
    "disable_names",
    "enable_names",
    "list_rows",
    "platforms",
    "registry_factory",
    "saved_selection",
    "tools_command",
]

SECTION = "platform_toolsets"


def _build_registry(scope: Scope) -> ToolRegistry:
    from indus.tools.builtin import load_builtins

    registry = ToolRegistry(clock=scope.clock)
    load_builtins(registry)
    return registry


registry_factory: Callable[[Scope], ToolRegistry] = _build_registry
"""Builds the tool registry the command inspects (tests swap in their own)."""

# -- model ----------------------------------------------------------------------------------------


def platforms() -> list[str]:
    prefix = toolsets.BUNDLE_PREFIX
    return sorted(name[len(prefix) :] for name in toolsets.TOOLSETS if name.startswith(prefix))


def default_bundle(platform: str) -> str:
    return f"{toolsets.BUNDLE_PREFIX}{platform}"


def _saved_raw(view: ConfigView, platform: str) -> list[str] | None:
    section = view.user_raw().get(SECTION)
    if not isinstance(section, dict):
        return None
    value = cast(dict[str, Any], section).get(platform)
    if not isinstance(value, list):
        return None
    return [str(item) for item in cast(list[Any], value)]


def saved_selection(scope: Scope, platform: str = "cli") -> list[str] | None:
    """The toolsets saved for ``platform``, or None when the platform uses its bundle."""
    return _saved_raw(fresh_view(scope), platform)


def _selection(view: ConfigView, platform: str) -> list[str]:
    saved = _saved_raw(view, platform)
    return saved if saved is not None else [default_bundle(platform)]


def _plain(name: str, registry: ToolRegistry, scope: Scope) -> bool:
    toolset = toolsets.get_toolset(name, registry, scope)
    return (
        toolset is not None
        and not toolset.includes
        and not toolset.posture
        and not name.startswith(toolsets.BUNDLE_PREFIX)
        and name != toolsets.CORE
    )


def _row_names(registry: ToolRegistry, scope: Scope) -> list[str]:
    return [n for n in toolsets.toolset_names(registry, scope) if _plain(n, registry, scope)]


def _tools(
    names: Iterable[str], registry: ToolRegistry, scope: Scope, platform: str
) -> frozenset[str]:
    return toolsets.select(list(names), registry=registry, scope=scope, platform=platform).tools


def _enabled_rows(
    names: Sequence[str], registry: ToolRegistry, scope: Scope, platform: str
) -> list[str]:
    offered = _tools(names, registry, scope, platform)
    rows: list[str] = []
    for row in _row_names(registry, scope):
        members = set(toolsets.resolve(row, registry, scope))
        if members and members <= offered:
            rows.append(row)
    return rows


def _spelled_out(
    names: Sequence[str], registry: ToolRegistry, scope: Scope, platform: str
) -> list[str]:
    """The saved list as rows; names the resolver does not know (later MCP servers) are kept."""
    passthrough = [n for n in names if not toolsets.is_known(n, registry, scope)]
    return sorted({*_enabled_rows(names, registry, scope, platform), *passthrough})


# -- reasons --------------------------------------------------------------------------------------


def _web_reason(scope: Scope) -> str | None:
    from indus.tools.builtin.web.providers import NoProvider, choose, default_registry

    try:
        choose(default_registry(), scope, "search")
    except NoProvider as exc:
        return str(exc)
    return None


TOOLSET_REASONS: dict[str, Callable[[Scope], str | None]] = {"web": _web_reason}
"""Toolsets whose probe has a better explanation than "not ready"."""


def _secret_present(scope: Scope, name: str) -> bool:
    if scope.secrets is not None and scope.secrets.get(name):
        return True
    return bool(scope.env.get(name))


def _entry_reason(entry: ToolEntry, scope: Scope) -> str:
    missing = [n for n in entry.requires_secrets if not _secret_present(scope, n)]
    if missing:
        return "needs " + ", ".join(missing)
    hook = TOOLSET_REASONS.get(entry.toolset)
    if hook is not None:
        try:
            found = hook(scope)
        except Exception as exc:
            found = f"its check failed ({type(exc).__name__})"
        if found:
            return found
    return "its readiness check says no here"


@dataclass(frozen=True)
class Row:
    name: str
    description: str
    enabled: bool
    ready: bool
    reason: str = ""


def list_rows(scope: Scope, platform: str, registry: ToolRegistry | None = None) -> list[Row]:
    reg = registry if registry is not None else registry_factory(scope)
    view = fresh_view(scope)
    enabled = set(_enabled_rows(_selection(view, platform), reg, scope, platform))
    rows: list[Row] = []
    for name in _row_names(reg, scope):
        toolset = toolsets.get_toolset(name, reg, scope)
        description = toolset.description if toolset is not None else ""
        reasons: list[str] = []
        for tool in toolsets.resolve(name, reg, scope):
            entry = reg.get(tool, scope)
            if entry is None:
                reasons.append(f"{tool}: not part of this build yet")
            elif not reg.available(entry, scope):
                reasons.append(f"{tool}: {_entry_reason(entry, scope)}")
        rows.append(Row(name, description, name in enabled, not reasons, "; ".join(reasons)))
    return rows


# -- changes --------------------------------------------------------------------------------------


class UnknownToolset(Exception):
    pass


def _check_known(names: Sequence[str], registry: ToolRegistry, scope: Scope) -> None:
    for name in names:
        if not toolsets.is_known(name, registry, scope):
            known = toolsets.toolset_names(registry, scope)
            close = difflib.get_close_matches(name, known, n=3, cutoff=0.6)
            hint = f" Did you mean: {', '.join(close)}?" if close else ""
            raise UnknownToolset(f"'{name}' is not a toolset.{hint} See `indus tools list`.")


def _save(scope: Scope, platform: str, names: Sequence[str]) -> None:
    writer.save(scope, {SECTION: {platform: list(names)}})


def enable_names(
    scope: Scope, platform: str, names: Sequence[str], registry: ToolRegistry | None = None
) -> list[str]:
    """Add ``names`` to the platform's list; returns the names that were not on before."""
    reg = registry if registry is not None else registry_factory(scope)
    _check_known(names, reg, scope)
    current = _selection(fresh_view(scope), platform)
    before = _tools(current, reg, scope, platform)
    added = [n for n in names if n not in current]
    updated = [*current, *added]
    after = _tools(updated, reg, scope, platform)
    if after == before:
        return []
    _save(scope, platform, updated)
    return added


def _drop_for(name: str, rows: list[str], registry: ToolRegistry, scope: Scope) -> list[str]:
    toolset = toolsets.get_toolset(name, registry, scope)
    bundle_like = name.startswith(toolsets.BUNDLE_PREFIX) or (
        toolset is not None and (toolset.posture or bool(toolset.includes))
    )
    members = set(toolsets.resolve(name, registry, scope))
    if bundle_like:
        members -= set(toolsets.resolve(toolsets.CORE, registry, scope))
    kept: list[str] = []
    for row in rows:
        row_tools = set(toolsets.resolve(row, registry, scope))
        if row == name or (row_tools and members and row_tools <= members):
            continue
        kept.append(row)
    return kept


def disable_names(
    scope: Scope, platform: str, names: Sequence[str], registry: ToolRegistry | None = None
) -> list[str]:
    """Remove ``names`` (see the module notes); returns the rows that were switched off."""
    reg = registry if registry is not None else registry_factory(scope)
    _check_known(names, reg, scope)
    current = _selection(fresh_view(scope), platform)
    rows = _spelled_out(current, reg, scope, platform)
    updated = list(rows)
    for name in names:
        updated = _drop_for(name, updated, reg, scope)
    if _tools(updated, reg, scope, platform) == _tools(current, reg, scope, platform):
        return []
    _save(scope, platform, updated)
    return [r for r in rows if r not in updated]


# -- output ---------------------------------------------------------------------------------------


def _print_rows(platform: str, rows: Sequence[Row], source: str) -> None:
    click.echo(f"Toolsets for {platform} ({source}):")
    width = max((len(r.name) for r in rows), default=4)
    for row in rows:
        mark = "[x]" if row.enabled else "[ ]"
        state = "ready" if row.ready else f"not ready — {row.reason}"
        click.echo(f"  {mark} {row.name:<{width}}  {row.description}")
        if not row.ready:
            click.echo(f"      {' ' * width}{state}")


def _source(scope: Scope, platform: str) -> str:
    return (
        "saved list"
        if saved_selection(scope, platform) is not None
        else (f"the {default_bundle(platform)} bundle")
    )


def _check_platform(platform: str) -> str | None:
    if platform not in platforms():
        return f"Unknown platform '{platform}'. Known: {', '.join(platforms())}."
    return None


def _list(scope: Scope, platform: str) -> int:
    problem = _check_platform(platform)
    if problem:
        err(problem)
        return 1
    _print_rows(platform, list_rows(scope, platform), _source(scope, platform))
    return 0


def _summary(scope: Scope) -> int:
    view = fresh_view(scope)
    section = view.user_raw().get(SECTION)
    names = {"cli"}
    if isinstance(section, dict):
        names |= {str(k) for k in cast(dict[str, Any], section) if str(k) in platforms()}
    reg = registry_factory(scope)
    for platform in sorted(names):
        enabled = _enabled_rows(_selection(view, platform), reg, scope, platform)
        click.echo(f"{platform}: {', '.join(enabled) if enabled else '(none)'}")
    return 0


# -- interactive ----------------------------------------------------------------------------------


def _checklist(io: Interactive, scope: Scope, platform: str, reg: ToolRegistry) -> list[str] | None:
    rows = list_rows(scope, platform, reg)
    labels = [row.name if row.ready else f"{row.name}  (not ready)" for row in rows]
    checked = [i for i, row in enumerate(rows) if row.enabled]
    try:
        picked = ask_checklist(io, f"Toolsets for {platform}:", labels, checked)
    except Cancelled:
        return None
    chosen = {rows[i].name for i in picked}
    current = _selection(fresh_view(scope), platform)
    passthrough = [n for n in current if not toolsets.is_known(n, reg, scope)]
    return sorted({*chosen, *passthrough})


def _web_setup(io: Interactive, scope: Scope) -> None:
    from indus.cli.commands.config_write import Refused, set_value_for
    from indus.foundation.config import keys
    from indus.tools.builtin.web.providers import default_registry

    providers = [p for p in default_registry().all() if p.can_search]
    rows: list[str] = []
    for provider in providers:
        missing = provider.missing(scope)
        state = "ready" if missing is None else missing
        rows.append(f"{provider.title or provider.name} ({state})")
    skipped = "Skipped; web search stays unavailable until a provider is set up."
    try:
        picked = ask_choice(
            io, "Web search has no working provider yet. Pick one:", [*rows, "Skip"], len(rows)
        )
        if picked >= len(providers):
            click.echo(skipped)
            return
        provider = providers[picked]
        set_value_for(scope, "web.backend", provider.name)
        missing = provider.missing(scope)
        first = missing.split()[0] if missing else ""
        if first and keys.is_env_shaped(first):
            secret = ask_secret(io, f"{first} (input hidden, Enter to skip)").strip()
            if secret:
                set_value_for(scope, first, secret)
    except Cancelled:
        click.echo(skipped)
    except Refused as exc:
        err(str(exc))


def _interactive(io: Interactive, scope: Scope, platform: str) -> int:
    reg = registry_factory(scope)
    chosen = _checklist(io, scope, platform, reg)
    if chosen is None:
        click.echo("No change.")
        return 0
    try:
        _save(scope, platform, chosen)
    except IndusError as exc:
        err(exc.message)
        return 1
    click.echo(f"Saved {len(chosen)} toolset(s) for {platform}.")
    tools_now = _tools(chosen, reg, scope, platform)
    if "web_search" in tools_now and _web_reason(scope_with_config(scope)) is not None:
        _web_setup(io, scope)
    return 0


# -- commands -------------------------------------------------------------------------------------

_PLATFORM_OPTION = click.option(
    "--platform", default="cli", show_default=True, help="Platform whose toolsets to use."
)


@click.group(
    "tools",
    cls=LazyGroup,
    invoke_without_command=True,
    help="Choose which toolsets each platform gets. With no subcommand: a checklist on a "
    "terminal, else the list.",
)
@click.option("--summary", is_flag=True, help="Print the enabled toolsets of every platform.")
@_PLATFORM_OPTION
@click.pass_context
def tools_command(ctx: click.Context, summary: bool, platform: str) -> int:
    if ctx.invoked_subcommand is not None:
        return 0
    state = state_of(ctx)
    scope = scope_with_config(state.scope)
    if summary:
        return _summary(scope)
    problem = _check_platform(platform)
    if problem:
        err(problem)
        return 1
    io = io_for(state)
    if not io.tty:
        return _list(scope, platform)
    return _interactive(io, scope, platform)


@tools_command.command("list", help="Show each toolset, whether it is on, and whether it is ready.")
@_PLATFORM_OPTION
@click.pass_context
def list_command(ctx: click.Context, platform: str) -> int:
    return _list(scope_with_config(state_of(ctx).scope), platform)


def _change(ctx: click.Context, platform: str, names: Sequence[str], enable: bool) -> int:
    scope = scope_with_config(state_of(ctx).scope)
    problem = _check_platform(platform)
    if problem:
        err(problem)
        return 1
    try:
        changed = (enable_names if enable else disable_names)(scope, platform, names)
    except UnknownToolset as exc:
        err(str(exc), "Nothing was written.")
        return 1
    except IndusError as exc:
        err(exc.message, "Nothing was written.")
        return 1
    if not changed:
        click.echo("Nothing changed.")
    else:
        verb = "Enabled" if enable else "Disabled"
        click.echo(f"{verb} for {platform}: {', '.join(changed)}")
    return 0


@tools_command.command("enable", help="Switch toolsets on for a platform.")
@click.argument("names", nargs=-1, required=True)
@_PLATFORM_OPTION
@click.pass_context
def enable_command(ctx: click.Context, names: tuple[str, ...], platform: str) -> int:
    return _change(ctx, platform, names, enable=True)


@tools_command.command("disable", help="Switch toolsets off for a platform.")
@click.argument("names", nargs=-1, required=True)
@_PLATFORM_OPTION
@click.pass_context
def disable_command(ctx: click.Context, names: tuple[str, ...], platform: str) -> int:
    return _change(ctx, platform, names, enable=False)
