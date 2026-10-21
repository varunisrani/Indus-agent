"""``indus import-agent``: bring over a Claude Code or Codex setup (P24-18).

``indus import-agent [claude-code|codex] [--source DIR] [--project DIR] [--dry-run]
[--overwrite] [-y] [--sync]``. The preview (every mapping and its status) is always printed
first; nothing is written before the answer. Without a terminal the import needs ``--yes`` and
otherwise exits 1 after the preview (R04.19). ``--sync`` re-applies, without asking, the mappings
whose recorded source files changed since the last import (``<home>/import-sync.json``).
Credentials are never imported. Mapping rules live in ``indus.cli.importers.agents``.
"""

from __future__ import annotations

from pathlib import Path

import click

from indus.cli import prompts
from indus.cli.cmdkit import cli_state, err
from indus.cli.importers import agents as rules
from indus.foundation.scope import Scope

__all__ = ["import_agent_command"]

_ORDER = ("import", "conflict", "in place", "skipped", "error")
_WORD = {
    "import": "import",
    "conflict": "conflict",
    "in place": "in place",
    "skipped": "skip",
    "error": "error",
}


def _user_home(scope: Scope) -> Path | None:
    raw = scope.env.get("HOME") or scope.env.get("USERPROFILE")
    return Path(raw) if raw else None


def _print_items(items: list[rules.Item], *, done: bool = False) -> None:
    for status in _ORDER:
        for item in (i for i in items if i.status == status):
            word = _WORD[status]
            if done and status == "import":
                word = "imported"
            line = f"  {word:<9} {item.kind:<16} {item.source} -> {item.target}"
            reason = item.outcome if done and status == "error" and item.outcome else item.reason
            if reason:
                line += f"  ({reason})"
            click.echo(line)
            for note in item.notes:
                click.echo(f"{'':12}note: {note}")


def _summary(items: list[rules.Item]) -> str:
    counts = [(s, sum(1 for i in items if i.status == s)) for s in _ORDER]
    labels = {
        "import": "to import",
        "conflict": "conflicts",
        "in place": "read in place",
        "skipped": "skipped",
        "error": "errors",
    }
    return ", ".join(f"{n} {labels[s]}" for s, n in counts if n) or "nothing found"


def _header(plan: rules.Plan, scope: Scope, shown_source: str) -> None:
    click.echo(f"Import from {rules.LABELS[plan.agent]}")
    click.echo(f"  source:      {shown_source}")
    if plan.project is not None:
        click.echo(f"  project:     {plan.project}")
    click.echo(f"  into:        {scope.home}")
    click.echo("  credentials: never imported (set keys with `indus auth` / `indus config set`)")


def _stamp(scope: Scope) -> str:
    return scope.clock.now().isoformat(timespec="seconds")


def _run_sync(scope: Scope, agent: str | None, dry_run: bool, home: Path | None) -> int:
    manifest = rules.Manifest.read(scope.home)
    names: list[rules.Agent] = [
        a for a in rules.AGENTS if a in manifest.agents and agent in (None, a)
    ]
    if not names:
        click.echo("No imports are recorded yet; run `indus import-agent` once first.")
        return 0
    failed = False
    for name in names:
        entry = manifest.agents[name]
        source = Path(str(entry.get("source") or ""))
        project_raw = entry.get("project")
        project = Path(str(project_raw)) if project_raw else None
        label = rules.LABELS[name]
        if not source.is_dir():
            click.echo(f"{label}: {source} is gone; skipped.")
            continue
        plan = rules.build_plan(
            scope,
            name,
            source,
            project=project if project is not None and project.is_dir() else None,
            overwrite=bool(entry.get("overwrite")),
            manifest=manifest,
            user_home=home,
        )
        changed = rules.changed_items(plan, manifest)
        if not changed:
            click.echo(f"{label}: unchanged since the last import.")
            continue
        click.echo(f"{label}: {len(changed)} changed source(s)" + (" (dry run)" if dry_run else ""))
        if dry_run:
            _print_items(changed)
            continue
        rules.apply_plan(scope, changed)
        _print_items(changed, done=True)
        failed = failed or any(i.status == "error" for i in changed)
        plan.items = changed
        manifest = rules.record(
            scope.home,
            plan,
            overwrite=bool(entry.get("overwrite")),
            stamp=_stamp(scope),
            manifest=manifest,
        )
    return 1 if failed else 0


@click.command("import-agent")
@click.argument("agent", required=False, type=click.Choice(rules.AGENTS))
@click.option(
    "--source",
    type=click.Path(path_type=Path),
    help="The agent's folder (default ~/.claude or ~/.codex).",
)
@click.option(
    "--project",
    type=click.Path(file_okay=False, path_type=Path),
    help="Also read this project's own files (CLAUDE.md / AGENTS.md, settings, MCP servers).",
)
@click.option("--dry-run", is_flag=True, help="Show the preview and stop.")
@click.option("--overwrite", is_flag=True, help="Replace MCP servers and skills of the same name.")
@click.option(
    "-y", "--yes", is_flag=True, help="Import without asking (needed without a terminal)."
)
@click.option(
    "--sync", is_flag=True, help="Re-import recorded sources that changed, without asking."
)
def import_agent_command(
    agent: str | None,
    source: Path | None,
    project: Path | None,
    dry_run: bool,
    overwrite: bool,
    yes: bool,
    sync: bool,
) -> None:
    """Bring over another coding agent's setup (Claude Code, Codex)."""
    state = cli_state()
    scope = state.scope
    home = _user_home(scope)
    if sync:
        raise click.exceptions.Exit(_run_sync(scope, agent, dry_run, home))
    if agent is None:
        if source is not None:
            raise click.UsageError("name the agent the --source folder belongs to")
        found = [
            a for a in rules.AGENTS if home is not None and (home / rules.DEFAULT_DIRS[a]).is_dir()
        ]
        if not found:
            err(
                "No Claude Code (~/.claude) or Codex (~/.codex) setup was found.",
                "Point at one: `indus import-agent claude-code --source /path/to/.claude`.",
            )
            raise click.exceptions.Exit(1)
        if len(found) > 1:
            err(
                f"Found {' and '.join(rules.LABELS[a] for a in found)}; pick one:",
                *(f"  indus import-agent {a}" for a in found),
            )
            raise click.exceptions.Exit(1)
        agent = found[0]
    if source is None:
        if home is None:
            err("The home folder is not known here; pass --source.")
            raise click.exceptions.Exit(1)
        source = home / rules.DEFAULT_DIRS[agent]
    if not source.is_dir():
        err(f"{source} is not a folder.", "Point at the right one with `--source`.")
        raise click.exceptions.Exit(1)
    if project is not None and not project.is_dir():
        raise click.BadParameter(f"{project} is not a folder", param_hint="--project")
    name: rules.Agent = next(a for a in rules.AGENTS if a == agent)
    manifest = rules.Manifest.read(scope.home)
    plan = rules.build_plan(
        scope,
        name,
        source.resolve(),
        project=project.resolve() if project is not None else None,
        overwrite=overwrite,
        manifest=manifest,
        user_home=home,
    )
    _header(plan, scope, str(source))
    click.echo("")
    click.echo("Preview (nothing has been changed yet):")
    _print_items(plan.items)
    click.echo(f"Summary: {_summary(plan.items)}")
    todo = [i for i in plan.items if i.status == "import"]
    if dry_run:
        return
    if not todo:
        click.echo("Nothing to import.")
        raise click.exceptions.Exit(1 if plan.count("error") else 0)
    io = prompts.io_for(
        state, f"Run `indus import-agent {name} --yes` to import without a terminal."
    )
    answer = prompts.confirm(io, f"Import {len(todo)} item(s)?", assume_yes=yes)
    if answer is None:
        raise click.exceptions.Exit(1)
    if not answer:
        click.echo("Nothing was changed.")
        raise click.exceptions.Exit(1)
    rules.apply_plan(scope, todo)
    click.echo("")
    click.echo("Result:")
    _print_items(todo, done=True)
    rules.record(scope.home, plan, overwrite=overwrite, stamp=_stamp(scope), manifest=manifest)
    click.echo(
        f"Sources recorded in {scope.home / rules.MANIFEST_FILE}; `--sync` picks up changes."
    )
    click.echo(
        "Credentials were not imported: add keys with `indus auth add` or `indus config set`."
    )
    if any(i.status == "error" for i in plan.items):
        raise click.exceptions.Exit(1)
