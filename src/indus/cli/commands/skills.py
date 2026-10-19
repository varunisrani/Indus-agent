"""``indus skills``: list, view, check and sync the skills of a profile (P09-24).

- ``list [--source S] [--json]``: every skill with its category, source (``local``, ``create_dir``,
  ``external``, or ``bundled`` / ``hub`` from their manifests), lifecycle state and description;
- ``view <name>``: ``SKILL.md`` and the support files;
- ``check``: validates every skill and runs the advisory linter; exit 1 when a skill is invalid or
  a name is ambiguous (lint findings alone never fail);
- ``list-modified``: shipped skills the user edited; ``diff <name>``: the edits as a unified diff;
- ``sync``: copy new and updated shipped skills (user edits are never touched);
- ``opt-out [--remove] [-y]`` / ``opt-in [--no-sync]``: stop or resume shipped skills;
- ``reset <name> [--restore] [-y]``: forget a shipped skill's origin, or put the shipped copy back;
- ``pending`` / ``approve <id…|all>`` / ``reject <id…|all>``: skill writes held by
  ``skills.write_approval``.

Nothing prompts without a terminal: a verb that needs a confirmation exits 2 unless ``-y``.
Hub verbs (``browse``, ``search``, ``inspect``, ``install``, ``check-updates``, ``update``,
``audit``, ``uninstall``, ``tap``, ``trust``/``untrust``) and ``trust-project`` come from
``skills_hub``.
"""

from __future__ import annotations

import difflib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click

from indus.cli.cmdkit import cli_state, err, group
from indus.cli.commands.memory import reject_ids, which_of
from indus.cli.commands.skills_hub import register as register_hub_verbs
from indus.cli.prompts import confirm, io_for
from indus.foundation.scope import Scope
from indus.learning import pending
from indus.learning.skills import bundled_sync
from indus.learning.skills.format import SkillFormatError, check_skill_text
from indus.learning.skills.lint import lint_skill
from indus.learning.skills.manage_tool import apply_pending_skill
from indus.learning.skills.resolver import (
    TIER_LOCAL,
    SkillError,
    SkillRecord,
    clear_scan_cache,
    load_catalog,
)
from indus.learning.skills.usage import hub_names, usage_for
from indus.learning.skills.view_tools import clear_list_cache, linked_files

__all__ = [
    "SOURCES",
    "CheckReport",
    "approve_skills",
    "check_skills",
    "skill_rows",
    "skills_group",
]

SOURCES = ("project", "local", "create_dir", "external", "bundled", "hub")
DESCRIPTION_WIDTH = 60


def source_of(rec: SkillRecord, shipped: set[str], hub: set[str]) -> str:
    if rec.tier == TIER_LOCAL and rec.name in hub:
        return "hub"
    if rec.tier == TIER_LOCAL and rec.name in shipped:
        return "bundled"
    return rec.tier_name


def skill_rows(scope: Scope) -> list[dict[str, Any]]:
    """One row per scanned skill that is not hidden by a higher tier."""
    catalog = load_catalog(scope)
    skills_root = scope.home / "skills"
    shipped = set(bundled_sync.read_manifest(scope.home))
    hub = hub_names(skills_root)
    usage = usage_for(scope).load()
    rows: list[dict[str, Any]] = []
    for rec in catalog.records:
        if rec.status == "shadowed" or rec.plugin is not None:
            continue
        status = "disabled" if rec.disabled else rec.status
        record = usage.get(rec.name, {})
        rows.append(
            {
                "name": rec.load_name or rec.name,
                "category": rec.category,
                "source": source_of(rec, shipped, hub),
                "state": record.get("state", "active"),
                "status": "ok" if status == "unique" else status,
                "description": rec.description,
                "path": str(rec.skill_dir),
            }
        )
    rows.sort(key=lambda r: (r["category"] or "", r["name"]))
    return rows


def _clip(text: str, width: int = DESCRIPTION_WIDTH) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def _resolve(scope: Scope, name: str) -> SkillRecord:
    try:
        return load_catalog(scope).resolve(name)
    except SkillError as exc:
        err(f"{exc}. `indus skills list` shows the skills of this profile.")
        raise click.exceptions.Exit(1) from None


def _confirmed(question: str, yes: bool, guidance: str) -> None:
    """Go on when confirmed; exit 2 without a terminal, 1 on "no"."""
    io = io_for(cli_state(), guidance)
    answer = confirm(io, question, assume_yes=yes)
    if answer is None:
        raise click.exceptions.Exit(2)
    if not answer:
        click.echo("Nothing changed.")
        raise click.exceptions.Exit(1)


@group("skills", "List, check and sync skills.")
def skills_group() -> None:
    pass


# -- reading --------------------------------------------------------------------------------------


@skills_group.command("list")
@click.option("--source", type=click.Choice(SOURCES), help="Only skills from this source.")
@click.option("--json", "as_json", is_flag=True, help="Print the rows as JSON.")
def list_command(source: str | None, as_json: bool) -> None:
    """List the skills of this profile."""
    rows = skill_rows(cli_state().scope)
    if source:
        rows = [r for r in rows if r["source"] == source]
    if as_json:
        click.echo(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    if not rows:
        click.echo("No skills found." + (f" (source: {source})" if source else ""))
        return
    width = max(len(r["name"]) for r in rows)
    cat_width = max(len(r["category"] or "-") for r in rows)
    for row in rows:
        flags = "" if row["status"] == "ok" else f" [{row['status']}]"
        if row["state"] != "active":
            flags += f" [{row['state']}]"
        click.echo(
            f"{row['name']:<{width}}  {row['category'] or '-':<{cat_width}}  {row['source']:<10}  "
            f"{_clip(row['description'])}{flags}"
        )
    click.echo(f"{len(rows)} skill{'s' if len(rows) != 1 else ''}.")


@skills_group.command("view")
@click.argument("name")
def view_command(name: str) -> None:
    """Print a skill's SKILL.md and list its support files."""
    rec = _resolve(cli_state().scope, name)
    try:
        text = rec.skill_md.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        err(f"Could not read {rec.skill_md}: {exc}")
        raise click.exceptions.Exit(1) from None
    click.echo(f"# {rec.name}  ({rec.skill_dir})")
    click.echo(text.rstrip("\n"))
    files = [f for group_files in linked_files(rec.skill_dir).values() for f in group_files]
    if files:
        click.echo("")
        click.echo("Support files:")
        for path in files:
            click.echo(f"  {path}")


@dataclass
class CheckReport:
    checked: int = 0
    errors: list[str] | None = None
    warnings: list[str] | None = None

    @property
    def ok(self) -> bool:
        return not self.errors


def check_skills(scope: Scope) -> CheckReport:
    """Validate and lint every scanned skill (shadowed copies included: they are still files the
    user keeps)."""
    catalog = load_catalog(scope)
    errors: list[str] = []
    warnings: list[str] = []
    checked = 0
    for rec in catalog.records:
        if rec.plugin is not None:
            continue
        checked += 1
        where = f"{rec.name} ({rec.skill_dir})"
        try:
            text = rec.skill_md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(f"{where}: cannot read SKILL.md: {exc}")
            continue
        try:
            check_skill_text(text)
        except SkillFormatError as exc:
            errors.extend(f"{where}: {problem}" for problem in exc.problems)
            continue
        for finding in lint_skill(text, rec.skill_dir):
            warnings.append(f"{where}: {finding.rule}: {finding.message}")
    for name, recs in catalog.ambiguous_names().items():
        paths = ", ".join(str(r.skill_dir) for r in recs)
        errors.append(f"{name}: the name is used by several skills ({paths}); rename all but one")
    return CheckReport(checked, errors, warnings)


@skills_group.command("check")
def check_command() -> None:
    """Validate every skill and run the linter; exit 1 when one is invalid."""
    report = check_skills(cli_state().scope)
    for line in report.errors or []:
        click.echo(f"error    {line}")
    for line in report.warnings or []:
        click.echo(f"warning  {line}")
    errors, warnings = len(report.errors or []), len(report.warnings or [])
    click.echo(f"{report.checked} skills checked: {errors} errors, {warnings} warnings.")
    if not report.ok:
        raise click.exceptions.Exit(1)


# -- shipped skills -------------------------------------------------------------------------------


@skills_group.command("list-modified")
def list_modified_command() -> None:
    """List shipped skills you have edited (sync leaves them alone)."""
    names = bundled_sync.list_modified(cli_state().scope.home)
    if not names:
        click.echo("No shipped skill has been edited.")
        return
    for name in names:
        click.echo(name)


def _files(folder: Path) -> dict[str, Path]:
    out: dict[str, Path] = {}
    if not folder.is_dir():
        return out
    for path in sorted(folder.rglob("*")):
        rel = path.relative_to(folder)
        if not path.is_file() or any(
            part in bundled_sync.CACHE_DIRS or part.endswith(".pyc") for part in rel.parts
        ):
            continue
        out[rel.as_posix()] = path
    return out


def _text(path: Path | None) -> list[str]:
    if path is None:
        return []
    try:
        return path.read_text(encoding="utf-8").splitlines(keepends=True)
    except UnicodeDecodeError:
        return [f"(binary file, {path.stat().st_size} bytes)\n"]


def shipped_diff(scope: Scope, name: str) -> str | None:
    """The unified diff of the live copy against the shipped one; None when not shipped."""
    shipped = {s.name: s for s in bundled_sync.discover(bundled_sync.bundled_source(scope.env))}
    skill = shipped.get(name)
    if skill is None:
        return None
    live = _files(scope.home / "skills" / skill.rel)
    origin = _files(skill.path)
    chunks: list[str] = []
    for rel in sorted(set(live) | set(origin)):
        lines = difflib.unified_diff(
            _text(origin.get(rel)),
            _text(live.get(rel)),
            fromfile=f"shipped/{skill.rel.as_posix()}/{rel}",
            tofile=f"yours/{skill.rel.as_posix()}/{rel}",
        )
        chunks.extend(line if line.endswith("\n") else line + "\n" for line in lines)
    return "".join(chunks)


@skills_group.command("diff")
@click.argument("name")
def diff_command(name: str) -> None:
    """Show how your copy of a shipped skill differs from the shipped one."""
    text = shipped_diff(cli_state().scope, name)
    if text is None:
        err(f"{name!r} is not a skill that ships with Indus Agent.")
        raise click.exceptions.Exit(1)
    click.echo(text.rstrip("\n") if text else "No changes from the shipped version.")


@skills_group.command("sync")
def sync_command() -> None:
    """Copy new and updated shipped skills (your edits are kept)."""
    scope = cli_state().scope
    report = bundled_sync.sync_bundled(scope.home, env=scope.env)
    _after_change(scope)
    click.echo(report.summary())
    for problem in report.failed:
        err(f"  failed: {problem}")
    if report.failed:
        raise click.exceptions.Exit(1)


@skills_group.command("opt-out")
@click.option("--remove", is_flag=True, help="Also delete shipped skills you never edited.")
@click.option("--yes", "-y", is_flag=True, help="Do not ask first.")
def opt_out_command(remove: bool, yes: bool) -> None:
    """Stop copying shipped skills into this profile."""
    scope = cli_state().scope
    if remove:
        preview = bundled_sync.remove_pristine(scope.home, dry_run=True)
        if preview.removed:
            _confirmed(
                f"Delete {len(preview.removed)} shipped skills you never edited?",
                yes,
                "Deleting shipped skills needs a terminal to confirm, or -y.",
            )
    bundled_sync.set_opt_out(scope.home, True)
    click.echo("Shipped skills are off for this profile; sync will not copy them.")
    if remove:
        report = bundled_sync.remove_pristine(scope.home)
        _after_change(scope)
        click.echo(f"Deleted {len(report.removed)}: {', '.join(report.removed) or 'none'}.")
        for name, reason in report.kept:
            click.echo(f"  kept {name}: {reason}")


@skills_group.command("opt-in")
@click.option("--no-sync", is_flag=True, help="Only switch back on; copy at the next sync.")
def opt_in_command(no_sync: bool) -> None:
    """Copy shipped skills into this profile again."""
    scope = cli_state().scope
    report = bundled_sync.opt_in(scope.home, seed=not no_sync, env=scope.env)
    _after_change(scope)
    click.echo("Shipped skills are on for this profile.")
    if report is not None:
        click.echo(report.summary())


@skills_group.command("reset")
@click.argument("name")
@click.option("--restore", is_flag=True, help="Replace your copy with the shipped one.")
@click.option("--yes", "-y", is_flag=True, help="Do not ask first.")
def reset_command(name: str, restore: bool, yes: bool) -> None:
    """Forget where a shipped skill came from, or restore the shipped copy."""
    scope = cli_state().scope
    if restore:
        _confirmed(
            f"Replace your copy of {name!r} with the shipped one? Your edits will be lost.",
            yes,
            "Restoring a shipped skill needs a terminal to confirm, or -y.",
        )
    result = bundled_sync.reset(scope.home, name, restore=restore, env=scope.env)
    _after_change(scope)
    (click.echo if result.ok else err)(result.message)
    if not result.ok:
        raise click.exceptions.Exit(1)


def _after_change(scope: Scope) -> None:
    clear_scan_cache()
    clear_list_cache(scope)


# -- staged writes --------------------------------------------------------------------------------


def approve_skills(scope: Scope, which: str | Sequence[str]) -> list[pending.Approval]:
    store = pending.PendingStore(scope.home, scope.clock)
    return pending.approve(store, "skills", which, lambda r: apply_pending_skill(scope, r))


@skills_group.command("pending")
def pending_command() -> None:
    """List skill writes waiting for approval."""
    scope = cli_state().scope
    click.echo(pending.describe(pending.PendingStore(scope.home, scope.clock).list("skills")))


@skills_group.command("approve")
@click.argument("ids", nargs=-1, required=True)
def approve_command(ids: tuple[str, ...]) -> None:
    """Apply waiting skill writes (ids, or all) and show their diffs."""
    results = approve_skills(cli_state().scope, which_of(ids))
    if not results:
        click.echo("Nothing is waiting for approval.")
        return
    for approval in results:
        click.echo(pending.render_approval(approval, surface="cli"))
    if not any(a.ok for a in results):
        raise click.exceptions.Exit(1)


@skills_group.command("reject")
@click.argument("ids", nargs=-1, required=True)
def reject_command(ids: tuple[str, ...]) -> None:
    """Drop waiting skill writes (ids, or all)."""
    removed = reject_ids(cli_state().scope, "skills", which_of(ids))
    if not removed:
        err("No waiting write matched.")
        raise click.exceptions.Exit(1)
    click.echo(f"Rejected {', '.join(removed)}.")


# -- hub verbs (P13-27) and trust-project (P13-28) -------------------------------------------------

register_hub_verbs(skills_group)
