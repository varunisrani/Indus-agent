"""``indus doctor``: check this profile's setup, and fix what is safe to fix (P07-19).

Runs the checks of ``indus.cli.doctor`` in order and prints one line per finding, then a summary:
"All checks passed" (exit 0), "Fixed N issue(s)" with ``--fix``, and the numbered problems that are
left with a ``--fix`` tip (exit 1). ``--live`` adds one bounded ``/models`` call per provider with
a key (``doctor.live_probe_timeout_s``, default 10 s). ``--ack ID`` records an advisory
acknowledgement in ``security.acked_advisories`` and stops there. ``--minimal`` prints only the
milestone-0 report of ``state.doctor_report`` (no package, network or backend checks), as JSON
with ``--json``.
"""

from __future__ import annotations

import json
import re

import click

from indus.cli.cmdkit import cli_state, err
from indus.cli.doctor import DoctorContext, Finding, run_checks, summarise
from indus.foundation import config as config_mod
from indus.foundation.errors import IndusError
from indus.foundation.profiles import DEFAULT_PROFILE, read_active
from indus.foundation.scope import Scope
from indus.state import doctor_report

__all__ = ["ACK_KEY", "doctor_command", "selection_source"]

ACK_KEY = "security.acked_advisories"
_ADVISORY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_MARK = {"ok": "ok  ", "info": "info", "warn": "warn", "error": "FAIL"}


def selection_source(scope: Scope, recorded: str | None = None) -> str:
    """How this profile was chosen: what bootstrap recorded, else the most likely answer."""
    if recorded:
        return recorded
    if scope.profile_id == DEFAULT_PROFILE:
        return "default"
    if read_active(scope.root) == scope.profile_id:
        return "sticky"
    return "flag or env"


def _ack(scope: Scope, advisory: str) -> int:
    if not _ADVISORY_ID.match(advisory):
        raise click.BadParameter(f"{advisory!r} is not an advisory id", param_hint="--ack")
    view = config_mod.load(scope)
    current = view.get(ACK_KEY) or []
    acked = [str(a) for a in current] if isinstance(current, list) else []
    if advisory in acked:
        click.echo(f"Advisory {advisory} was already acknowledged.")
        return 0
    try:
        config_mod.save(scope, {"security": {"acked_advisories": [*acked, advisory]}})
    except IndusError as exc:
        err(f"Could not save the acknowledgement: {exc.message}")
        return 1
    click.echo(f"Acknowledged advisory {advisory}; it will not be raised again.")
    return 0


def _print(title: str, found: list[Finding]) -> None:
    click.echo(title)
    for item in found:
        tag = "fixed" if item.fixed else _MARK[item.severity]
        click.echo(f"  [{tag}] {item.message}")


@click.command("doctor", short_help="Check the setup; --fix repairs.")
@click.option("--fix", "fix", is_flag=True, help="Repair what can be repaired safely.")
@click.option("--live", is_flag=True, help="Also call each provider with a key (bounded).")
@click.option("--ack", "ack", metavar="ID", default=None, help="Acknowledge a security advisory.")
@click.option("--minimal", is_flag=True, help="Only the basic report: home, config, keys, store.")
@click.option("--json", "as_json", is_flag=True, help="With --minimal: print JSON.")
def doctor_command(fix: bool, live: bool, ack: str | None, minimal: bool, as_json: bool) -> int:
    """Check this profile's setup and point at (or fix) what is wrong."""
    state = cli_state()
    scope = state.scope
    if ack is not None:
        return _ack(scope, ack)
    if minimal:
        report = doctor_report.minimal_report(
            scope, selected_by=selection_source(scope, state.selected_by)
        )
        if as_json:
            click.echo(json.dumps(report.as_dict(), indent=2, default=str))
        else:
            click.echo(report.text())
        return report.exit_code
    if as_json:
        raise click.UsageError("--json goes with --minimal")
    click.echo(f"Indus Agent doctor: profile {scope.profile_id} ({scope.home})")
    results = run_checks(DoctorContext(scope, fix=fix, live=live))
    for spec, found in results:
        _print(spec.title, found)
    summary = summarise(results)
    click.echo("")
    if fix and summary.fixed:
        click.echo(f"Fixed {summary.fixed} issue(s).")
    if not summary.remaining:
        click.echo("All checks passed.")
        return 0
    click.echo(f"{len(summary.remaining)} issue(s) left:")
    for number, item in enumerate(summary.remaining, 1):
        hint = f" — {item.fix}" if item.fix else ""
        click.echo(f"  {number}. {item.message}{hint}")
    if not fix and any(i.fix and "--fix" in i.fix for i in summary.remaining):
        click.echo("Tip: `indus doctor --fix` repairs the ones marked --fix.")
    return 1
