"""``indus curator``: look after the skills the agent wrote for itself (P09-20).

Verbs: ``status``, ``run [--background] [--dry-run] [--consolidate]``, ``pause`` / ``resume``,
``pin`` / ``unpin <name>``, ``adopt <name>``, ``list-unmanaged``, ``archive <name>``,
``restore <name>``, ``list-archived``, ``prune [--days N]``, ``purge [--days N]``, ``backup``,
``rollback [id] [--list]``, ``ledger [--skill S] [--rollback ID]``.

Nothing is asked without a terminal: ``prune``, ``purge``, ``rollback`` and
``ledger --rollback`` then need ``-y`` and exit 2 otherwise. Refusals exit 1 with the reason.
The ``status_lines`` / ``run_lines`` / ``set_paused_lines`` helpers are shared with ``/curator``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import click

from indus.cli.cmdkit import age_label, cli_state, err, group, human_size
from indus.cli.prompts import confirm, io_for
from indus.foundation.scope import Scope
from indus.learning.curator import ledger_port
from indus.learning.curator.archive import (
    Refused,
    archive_skill,
    list_archived,
    managed_skills,
    ownership,
    parse_time,
    purge_archived,
    restore_skill,
    unmanaged_skills,
)
from indus.learning.curator.settings import CuratorSettings
from indus.learning.curator.state import CuratorState
from indus.learning.skills.usage import usage_for

__all__ = [
    "curator_group",
    "default_step_factory",
    "run_lines",
    "set_paused_lines",
    "status_lines",
]


def default_step_factory(scope: Scope) -> Any:
    """The consolidation model step: the auxiliary client's ``curator`` task on the main route."""
    from indus.learning.curator.consolidate import AuxStep
    from indus.providers import route as route_mod
    from indus.providers.auxiliary import AuxClient

    return AuxStep(AuxClient(scope, lambda: route_mod.resolve(scope)))


STEP_FACTORY: Callable[[Scope], Any] = default_step_factory
"""Tests replace this with a fake model step."""


# -- shared line builders -------------------------------------------------------------------------


def _when(scope: Scope, value: object) -> str:
    stamp = parse_time(value)
    if stamp is None:
        return "never"
    seconds = (scope.clock.now() - stamp).total_seconds()
    return f"{stamp.strftime('%Y-%m-%d %H:%M')} ({age_label(seconds)})"


def _days(hours: float) -> str:
    return f"{int(hours // 24)} d" if hours % 24 == 0 and hours >= 24 else f"{hours:g} h"


def status_lines(scope: Scope) -> list[str]:
    config = CuratorSettings.from_scope(scope)
    data = CuratorState(scope).load()
    mode = "paused" if data.get("paused") else "on" if config.enabled else "off"
    lines = [
        f"curator: {mode}",
        f"  runs:          {data.get('run_count') or 0}",
        f"  last run:      {_when(scope, data.get('last_run_at'))}",
    ]
    summary = str(data.get("last_summary") or "(none)").splitlines() or ["(none)"]
    lines.append(f"  last summary:  {summary[0]}")
    lines += [f"                 {extra}" for extra in summary[1:]]
    if data.get("last_report"):
        lines.append(f"  last report:   {data['last_report']}")
    lines += [
        f"  runs every:    {_days(config.interval_hours)} (after {config.min_idle_hours:g} h idle)",
        f"  stale after:   {config.stale_after_days:g} d unused",
        f"  archive after: {config.archive_after_days:g} d unused",
        "  consolidation: "
        + ("on" if config.consolidate else "off (only the inactivity pass runs)"),
    ]
    managed = managed_skills(scope)
    by_state: dict[str, int] = {"active": 0, "stale": 0}
    for owner in managed:
        state = str(owner.record.get("state") or "active")
        by_state[state] = by_state.get(state, 0) + 1
    archived = list_archived(scope)
    lines.append("")
    lines.append(
        f"managed skills: {len(managed)} (active {by_state.get('active', 0)}, "
        f"stale {by_state.get('stale', 0)}); archived: {len(archived)}"
    )
    pinned = sorted(o.name for o in managed if o.record.get("pinned"))
    if pinned:
        lines.append(f"pinned ({len(pinned)}): {', '.join(pinned)}")
    if managed:

        def recency(owner: Any) -> str:
            rec = owner.record
            return str(rec.get("last_activity_at") or rec.get("created_at") or "")

        lines.append("least recently used:")
        for owner in sorted(managed, key=recency)[:5]:
            rec = owner.record
            lines.append(
                f"  {owner.name:<32} used {rec.get('use_count', 0):>3}  "
                f"last activity {_when(scope, rec.get('last_activity_at'))}"
            )
    unmanaged = unmanaged_skills(scope)
    if unmanaged:
        lines.append(f"unmanaged ({len(unmanaged)}; the curator leaves these alone):")
        for owner in unmanaged:
            lines.append(f"  {owner.name:<32} {owner.reason}")
        lines.append("  `indus curator adopt <name>` hands one to the curator")
    return lines


def run_lines(
    scope: Scope,
    *,
    dry_run: bool = False,
    consolidate: bool = False,
    background: bool = False,
    out: Callable[[str], object] | None = None,
) -> tuple[int, list[str]]:
    """Run a pass; returns ``(exit code, lines)``. ``out`` also receives lines as they come."""
    from indus.learning.curator.lifecycle import run_curator, run_in_background

    lines: list[str] = []

    def say(line: str) -> None:
        lines.append(line)
        if out is not None:
            out(line)

    config = CuratorSettings.from_scope(scope)
    if not config.enabled:
        say("curator: switched off (curator.enabled: false); nothing ran")
        return 1, lines
    merge = consolidate or None
    if not (consolidate or config.consolidate):
        say("curator: consolidation is off, so only the inactivity pass runs (--consolidate)")
    say("curator: previewing (nothing will change)..." if dry_run else "curator: running...")
    kwargs: dict[str, Any] = {
        "dry_run": dry_run,
        "consolidate": merge,
        "step_factory": STEP_FACTORY,
    }
    if background:
        run_in_background(scope, **kwargs)
        say("curator: running in the background; `indus curator status` shows the result")
        return 0, lines
    outcome = run_curator(scope, on_line=say, **kwargs)
    auto = outcome.transitions
    verb = "would change" if dry_run else "changed"
    say(
        f"curator: checked {auto.checked}, {verb}: stale {auto.marked_stale}, "
        f"archived {auto.archived}, active again {auto.reactivated}"
    )
    for failure in auto.failures:
        say(f"  could not archive {failure['name']}: {failure['error']}")
    if outcome.report:
        say(f"curator: report in {outcome.report}")
    return 0, lines


def set_paused_lines(scope: Scope, paused: bool) -> list[str]:
    CuratorState(scope).set_paused(paused)
    return ["curator: paused" if paused else "curator: resumed"]


# -- the group ----------------------------------------------------------------------------------


def _echo(lines: list[str]) -> None:
    for line in lines:
        click.echo(line)


def _fail(message: str, code: int = 1) -> None:
    err(f"curator: {message}")
    raise click.exceptions.Exit(code)


def _confirm(question: str, yes: bool, what: str) -> None:
    io = io_for(cli_state(), f"curator: {what} changes things for good; pass -y to go ahead.")
    answer = confirm(io, question, assume_yes=yes)
    if answer is None:
        raise click.exceptions.Exit(2)
    if not answer:
        raise click.exceptions.Exit(1)


@group("curator", "Look after the skills the agent wrote for itself.")
def curator_group() -> None:
    pass


@curator_group.command("status")
def status_command() -> None:
    """Show the curator's state and the skills it looks after."""
    _echo(status_lines(cli_state().scope))


@curator_group.command("run")
@click.option("--background", is_flag=True, help="Return at once; the pass runs on.")
@click.option("--dry-run", is_flag=True, help="Only report what would change.")
@click.option("--consolidate", is_flag=True, help="Also let a model merge overlapping skills.")
def run_command(background: bool, dry_run: bool, consolidate: bool) -> None:
    """Run a curator pass now."""
    code, _ = run_lines(
        cli_state().scope,
        dry_run=dry_run,
        consolidate=consolidate,
        background=background,
        out=click.echo,
    )
    if code:
        raise click.exceptions.Exit(code)


@curator_group.command("pause")
def pause_command() -> None:
    """Stop automatic runs until `resume`."""
    _echo(set_paused_lines(cli_state().scope, True))


@curator_group.command("resume")
def resume_command() -> None:
    """Allow automatic runs again."""
    _echo(set_paused_lines(cli_state().scope, False))


def _pinnable(scope: Scope, name: str) -> None:
    owner = ownership(scope, name)
    if owner.owner in ("bundled", "hub"):
        source = "ships with Indus Agent" if owner.owner == "bundled" else "came from the hub"
        _fail(f"{name!r} {source}; the curator never manages it, so there is nothing to pin")
    if owner.owner in ("external", "missing"):
        _fail(f"no skill named {name!r} in this profile's own folders")
    if not owner.managed:
        _fail(
            f"{name!r} is not managed by the curator ({owner.reason}); "
            f"`indus curator adopt {name}` hands it over first"
        )


@curator_group.command("pin")
@click.argument("name")
def pin_command(name: str) -> None:
    """Keep a managed skill out of every curator change."""
    scope = cli_state().scope
    _pinnable(scope, name)
    if usage_for(scope).set_pinned(name, True) is None:
        _fail(f"could not record the pin for {name!r}")
    click.echo(f"curator: pinned {name}")


@curator_group.command("unpin")
@click.argument("name")
def unpin_command(name: str) -> None:
    """Let the curator manage a pinned skill again."""
    scope = cli_state().scope
    _pinnable(scope, name)
    if usage_for(scope).set_pinned(name, False) is None:
        _fail(f"could not record the change for {name!r}")
    click.echo(f"curator: unpinned {name}")


@curator_group.command("adopt")
@click.argument("name")
def adopt_command(name: str) -> None:
    """Hand a skill you or /learn made to the curator (its inactivity clock is kept)."""
    scope = cli_state().scope
    owner = ownership(scope, name)
    reasons = {
        "bundled": "ships with Indus Agent (curator.prune_builtins governs those)",
        "hub": "came from the skills hub; its publisher owns it",
        "external": "lives in an external skills folder",
        "missing": "is not a skill in this profile",
    }
    if owner.owner in reasons:
        _fail(f"cannot adopt {name!r}: it {reasons[owner.owner]}")
    if owner.managed:
        click.echo(f"curator: {name} is already managed")
        return
    if usage_for(scope).set_origin(name, "agent") is None:
        _fail(f"could not record the adoption of {name!r}")
    click.echo(f"curator: adopted {name}; it now ages like the skills the agent wrote")


@curator_group.command("list-unmanaged")
def list_unmanaged_command() -> None:
    """Skills in this profile the curator leaves alone, and why."""
    rows = unmanaged_skills(cli_state().scope)
    if not rows:
        click.echo("curator: every skill it could manage is managed")
        return
    for owner in rows:
        click.echo(f"{owner.name:<32} {owner.reason}")


@curator_group.command("archive")
@click.argument("name")
def archive_command(name: str) -> None:
    """Move a skill into the archive (restore brings it back)."""
    scope = cli_state().scope
    if usage_for(scope).is_pinned(name):
        _fail(f"{name!r} is pinned; `indus curator unpin {name}` first")
    try:
        dest = archive_skill(scope, name, actor="user")
    except (Refused, OSError) as exc:
        _fail(str(exc))
        return
    click.echo(f"curator: archived {name} to {dest}")


@curator_group.command("restore")
@click.argument("name")
def restore_command(name: str) -> None:
    """Bring an archived skill back."""
    scope = cli_state().scope
    config = CuratorSettings.from_scope(scope)
    try:
        dest = restore_skill(scope, name, actor="user", allow_bundled=config.prune_builtins)
    except (Refused, OSError) as exc:
        _fail(str(exc))
        return
    click.echo(f"curator: restored {name} to {dest}")


@curator_group.command("list-archived")
def list_archived_command() -> None:
    """Archived skills."""
    scope = cli_state().scope
    rows = list_archived(scope)
    if not rows:
        click.echo("curator: the archive is empty")
        return
    for item in rows:
        stamp = item.archived_at.isoformat() if item.archived_at is not None else None
        click.echo(f"{item.name:<32} archived {_when(scope, stamp)}")


def _idle_days(scope: Scope, record: dict[str, Any] | Any) -> float | None:
    stamp = parse_time(record.get("last_activity_at")) or parse_time(record.get("created_at"))
    if stamp is None:
        return None
    return (scope.clock.now() - stamp).total_seconds() / 86400.0


@curator_group.command("prune")
@click.option("--days", type=int, default=None, help="Idle days (default: archive_after_days).")
@click.option("--dry-run", is_flag=True, help="Only list what would be archived.")
@click.option("--yes", "-y", is_flag=True, help="Do not ask first.")
def prune_command(days: int | None, dry_run: bool, yes: bool) -> None:
    """Archive managed skills idle for N days now (pinned ones are kept)."""
    scope = cli_state().scope
    limit = days if days is not None else CuratorSettings.from_scope(scope).archive_after_days
    if limit < 1:
        _fail(f"--days must be 1 or more (got {limit})", 2)
    picked: list[tuple[str, float]] = []
    for owner in managed_skills(scope):
        if owner.record.get("pinned"):
            continue
        idle = _idle_days(scope, owner.record)
        if idle is not None and idle >= limit:
            picked.append((owner.name, idle))
    if not picked:
        click.echo(f"curator: no unpinned managed skill has been idle for {limit:g} days")
        return
    click.echo(f"curator: idle for {limit:g} days or more:")
    for name, idle in sorted(picked, key=lambda p: -p[1]):
        click.echo(f"  {name:<32} {int(idle)} d")
    if dry_run:
        click.echo("(dry run: nothing changed)")
        return
    _confirm(f"Archive {len(picked)} skill(s)?", yes, "prune")
    failed = 0
    for name, _ in picked:
        try:
            archive_skill(scope, name, actor="user")
        except (Refused, OSError) as exc:
            failed += 1
            err(f"  {name}: {exc}")
    click.echo(f"curator: archived {len(picked) - failed} of {len(picked)}")
    if failed:
        raise click.exceptions.Exit(1)


@curator_group.command("purge")
@click.option("--days", type=int, default=None, help="Age in days (default: archive_ttl_days).")
@click.option("--dry-run", is_flag=True, help="Only list what would be deleted.")
@click.option("--yes", "-y", is_flag=True, help="Do not ask first.")
def purge_command(days: int | None, dry_run: bool, yes: bool) -> None:
    """Delete archived skills older than the archive TTL for good (ledgered)."""
    scope = cli_state().scope
    ttl = float(days) if days is not None else CuratorSettings.from_scope(scope).archive_ttl_days
    if ttl <= 0:
        click.echo(
            "curator: purge is off (curator.archive_ttl_days is 0); set it, or pass --days N, "
            "to delete archives older than N days"
        )
        return
    now = scope.clock.now()
    old = [
        a
        for a in list_archived(scope)
        if a.archived_at is not None and (now - a.archived_at).total_seconds() >= ttl * 86400
    ]
    if not old:
        click.echo(f"curator: no archived skill is older than {ttl:g} days")
        return
    click.echo(f"curator: archived more than {ttl:g} days ago:")
    for item in old:
        click.echo(f"  {item.name} ({item.path.name})")
    if dry_run:
        click.echo("(dry run: nothing deleted)")
        return
    _confirm(f"Delete {len(old)} archived skill(s) for good?", yes, "purge")
    purged, failed = purge_archived(scope, old, ttl_days=ttl)
    for name, why in failed:
        err(f"  {name}: {why}")
    click.echo(f"curator: deleted {len(purged)} archived skill(s)")
    if failed:
        raise click.exceptions.Exit(1)


@curator_group.command("backup")
@click.option("--reason", default="manual", help="A note stored with the snapshot.")
def backup_command(reason: str) -> None:
    """Snapshot the skills folder now."""
    from indus.learning.curator.backup import snapshot

    scope = cli_state().scope
    if not CuratorSettings.from_scope(scope).backup_enabled:
        _fail("snapshots are switched off (curator.backup.enabled: false)")
    try:
        info = snapshot(scope, reason)
    except OSError as exc:
        _fail(f"snapshot failed: {exc}")
        return
    if info is None:
        _fail("there is no skills folder to snapshot yet")
        return
    click.echo(f"curator: snapshot {info.id} ({human_size(info.size)}, {info.skill_files} skills)")


@curator_group.command("rollback")
@click.argument("backup_id", required=False)
@click.option("--list", "show", is_flag=True, help="List the snapshots.")
@click.option("--yes", "-y", is_flag=True, help="Do not ask first.")
def rollback_command(backup_id: str | None, show: bool, yes: bool) -> None:
    """Restore the whole skills folder from a snapshot (the newest by default)."""
    from indus.learning.curator.backup import (
        BackupsDisabled,
        RollbackFailed,
        list_backups,
        rollback,
    )

    scope = cli_state().scope
    available = list_backups(scope)
    if show:
        if not available:
            click.echo("curator: no snapshots yet")
            return
        for info in available:
            click.echo(
                f"{info.id:<24} {info.reason[:40]:<40} {info.skill_files:>4} skills "
                f"{human_size(info.size):>9}"
            )
        return
    if not CuratorSettings.from_scope(scope).backup_enabled:
        _fail(
            "snapshots are switched off (curator.backup.enabled: false), so a rollback could "
            "not be undone; switch them on first"
        )
    target = (
        next((b for b in available if b.id == backup_id), None)
        if backup_id
        else (available[0] if available else None)
    )
    if target is None:
        if not available:
            _fail("no snapshots yet; `indus curator backup` takes one")
        _fail(f"no snapshot named {backup_id!r}; `indus curator rollback --list` shows them")
        return
    click.echo(f"Restore the skills folder from {target.id} ({target.reason})?")
    click.echo("A snapshot of the current folder is taken first, so this can be undone.")
    _confirm("Go ahead?", yes, "rollback")
    try:
        restored, safety = rollback(scope, target.id)
    except (BackupsDisabled, RollbackFailed) as exc:
        _fail(str(exc))
        return
    click.echo(f"curator: restored from {restored.id}; the folder before is snapshot {safety.id}")


@curator_group.command("ledger")
@click.option("--skill", default=None, help="Only entries for this skill.")
@click.option("--limit", type=int, default=20, help="How many entries to show.")
@click.option("--rollback", "entry_id", default=None, help="Undo one entry by id.")
@click.option("--yes", "-y", is_flag=True, help="Do not ask first.")
def ledger_command(skill: str | None, limit: int, entry_id: str | None, yes: bool) -> None:
    """The record of every skill change; --rollback undoes one."""
    port = ledger_port.resolve(cli_state().scope)
    if port is None:
        click.echo("curator: the skill change ledger is not available in this build")
        return
    if entry_id:
        entry = port.get(entry_id)
        if entry is None:
            _fail(f"no ledger entry {entry_id!r}; `indus curator ledger` lists them")
            return
        click.echo(
            f"Undo {entry.get('action', '?')} of {entry.get('skill', '?')} by "
            f"{entry.get('actor', '?')} at {entry.get('ts', '?')}?"
        )
        _confirm("Go ahead?", yes, "a ledger rollback")
        ok, message = port.rollback(entry_id)
        if not ok:
            _fail(f"rollback failed: {message}")
        click.echo(f"curator: {message}")
        return
    rows = list(port.entries(skill=skill, limit=limit))
    if not rows:
        click.echo("curator: the ledger is empty")
        return
    for row in rows:
        extra = ""
        evidence = row.get("evidence") or {}
        if isinstance(evidence, dict) and evidence.get("absorbed_into"):
            extra = f"  → {evidence['absorbed_into']}"
        click.echo(
            f"{row.get('id', '?'):<14} {str(row.get('ts', '?'))[:19]:<19} "
            f"{row.get('actor', '?'):<8} {row.get('action', '?'):<12} "
            f"{row.get('skill', '?')}{extra}"
        )
