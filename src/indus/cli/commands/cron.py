"""``indus cron``: schedule jobs and look after them (P14-19).

Verbs: ``create <schedule> <prompt…>``, ``edit <job>``, ``list [--all]``, ``pause <job>``,
``resume <job>``, ``run <job> [--context …]``, ``remove <job> [-y]``, ``status``, ``tick``,
``daemon``, ``pause-all [--reason]``, ``resume-all``, ``runs <job>``, ``history``,
``incidents [--all] [--ack ID]``, ``notepad <job> list|get|set|unset``, ``doctor``,
``suggestions [list|accept N|dismiss N|refresh]`` (P24-46: suggested jobs, created only on
accept).

A job is named by its id or its name (any case; an exact id wins; a name shared by several jobs is
refused with their ids). ``create`` and ``edit`` check what they are given the same way the agent's
``cronjob_manage`` tool does (prompt safety scan, script folder, deliver targets, monitor, working
folder) and also take the user-only fields the tool refuses: ``--model``, ``--provider``,
``--base-url`` (same scheme, host and port as the provider's configured endpoint, R01.11) and
``--reasoning``. ``edit --skill`` replaces the skill list, ``--add-skill`` / ``--remove-skill``
change it, ``--clear-skills`` empties it, ``--workdir ""`` clears the folder.

``list`` labels a next run more than 15 minutes in the past on an active job as overdue (the same
rule as ``doctor`` and ``status``). ``status`` reads the ticker's marker files
(``<home>/cron/ticker_heartbeat``, ``ticker_last_success``) by their modification time.

Nothing is asked without a terminal: ``remove`` then needs ``-y`` and exits 1. Refusals exit 1
with the reason; ``doctor`` exits 1 while any finding stands. The line builders are shared with
``/cron``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Iterable, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

import click

from indus.cli.cmdkit import age_label, cli_state, err, group
from indus.cli.prompts import confirm, io_for
from indus.cron import doctor as doctor_mod
from indus.cron import monitor, ports, targets
from indus.cron import tool as checks
from indus.cron.incidents import IncidentLedger
from indus.cron.ports import CronBackend, JobAmbiguous, JobNotFound, StoreUnavailable
from indus.foundation.scope import Scope

__all__ = [
    "HEARTBEAT_MARKER",
    "LAST_SUCCESS_MARKER",
    "CronRefused",
    "create_spec",
    "cron_backend",
    "cron_group",
    "job_lines",
    "list_lines",
    "status_lines",
]

HEARTBEAT_MARKER = "ticker_heartbeat"
LAST_SUCCESS_MARKER = "ticker_last_success"
TICKER_ALIVE_S = 180.0


class CronRefused(Exception):
    """A user-facing refusal (exit 1 on the CLI, a line on ``/cron``)."""


def _fail(message: str, code: int = 1) -> NoReturn:
    err(f"cron: {message}")
    raise click.exceptions.Exit(code)


def cron_backend(scope: Scope) -> CronBackend:
    """This process's cron backend; importing the host module registers the real one."""
    from indus.service import cron_host

    cron_host.install()
    return ports.backend_for(scope)


def _backend(scope: Scope) -> CronBackend:
    try:
        return cron_backend(scope)
    except StoreUnavailable as exc:
        _fail(str(exc))


def find_job(backend: CronBackend, ref: str) -> Any:
    try:
        return backend.resolve(ref)
    except JobAmbiguous as exc:
        raise CronRefused(str(exc)) from None
    except JobNotFound:
        raise CronRefused(f"no job {ref!r}; `indus cron list` shows the jobs") from None


def _job(backend: CronBackend, ref: str) -> Any:
    try:
        return find_job(backend, ref)
    except CronRefused as exc:
        _fail(str(exc))


# -- line builders (shared with /cron) ------------------------------------------------------------


def _when(value: object, now: datetime) -> str:
    stamp = ports.parse_instant(value)
    if stamp is None:
        return "—"
    delta = (stamp - now).total_seconds()
    shown = stamp.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    if delta >= 0:
        return f"{shown} (in {doctor_mod.duration_label(delta)})"
    return f"{shown} ({doctor_mod.duration_label(-delta)} ago)"


def job_lines(job: Any, now: datetime) -> list[str]:
    job_id = str(ports.field(job, "id", "?"))
    name = str(ports.field(job, "name", "") or "")
    state = str(ports.field(job, "state", "") or "")
    if not ports.field(job, "enabled", True) and state != "paused":
        state = "disabled"
    head = f"{job_id}  {name or '(unnamed)'}  [{state or 'scheduled'}]"
    schedule = ports.field(job, "schedule_display") or ports.schedule_kind(job) or "?"
    lines = [head, f"    schedule: {schedule}"]
    if doctor_mod.is_overdue(job, now):
        late = doctor_mod.overdue_seconds(job, now) or 0
        lines.append(
            f"    next run: OVERDUE by {doctor_mod.duration_label(late)} (has not fired; is the "
            "ticker running? `indus cron status`)"
        )
    elif doctor_mod.is_active(job):
        lines.append(f"    next run: {_when(ports.field(job, 'next_run_at'), now)}")
    times, done = ports.repeat_of(job)
    if times:
        lines.append(f"    runs: {done} of {times}")
    status = ports.field(job, "last_status")
    if status:
        lines.append(f"    last run: {status} at {_when(ports.field(job, 'last_run_at'), now)}")
    streak = int(ports.field(job, "failure_streak", 0) or 0)
    if streak:
        lines.append(f"    failed {streak} run(s) in a row")
    deliver = ports.field(job, "deliver", "local")
    lines.append(f"    deliver: {deliver}")
    delivery_error = ports.field(job, "last_delivery_error")
    if delivery_error:
        lines.append(f"    warning: last result not delivered: {str(delivery_error)[:160]}")
    unverified = ports.field(job, "last_delivery_unverified")
    if unverified:
        lines.append(f"    warning: delivery not confirmed by {unverified}")
    if ports.field(job, "paused_reason"):
        lines.append(f"    paused: {ports.field(job, 'paused_reason')}")
    return lines


def list_lines(backend: CronBackend, scope: Scope, *, show_all: bool = False) -> list[str]:
    health = backend.store_health()
    lines: list[str] = []
    if not health.writable:
        lines.append(f"warning: the cron store is not writable, so no job fires: {health.reason}")
    jobs = backend.list_jobs(include_disabled=True)
    shown = jobs if show_all else [j for j in jobs if ports.field(j, "state") != "completed"]
    if not shown:
        lines.append(
            'No scheduled jobs. Create one with `indus cron create "every 30m" "check the build"`.'
        )
        return lines
    now = scope.clock.now()
    for job in shown:
        lines.extend(job_lines(job, now))
    hidden = len(jobs) - len(shown)
    if hidden:
        lines.append(f"({hidden} completed job(s) hidden; --all shows them)")
    return lines


def _marker_age(path: Path, now: datetime) -> tuple[datetime | None, float | None]:
    try:
        stamp = datetime.fromtimestamp(os.stat(path).st_mtime, UTC)
    except OSError:
        return None, None
    return stamp, (now - stamp).total_seconds()


def status_lines(backend: CronBackend, scope: Scope) -> list[str]:
    now = scope.clock.now()
    folder = ports.cron_dir(scope)
    beat, beat_age = _marker_age(folder / HEARTBEAT_MARKER, now)
    if beat is None or beat_age is None:
        ticker = "never ran (start `indus cron daemon`, or run `indus cron tick` from a timer)"
    elif beat_age <= TICKER_ALIVE_S:
        ticker = f"alive (last heartbeat {age_label(beat_age)})"
    else:
        ticker = f"NOT RUNNING (last heartbeat {age_label(beat_age)})"
    success, success_age = _marker_age(folder / LAST_SUCCESS_MARKER, now)
    lines = [f"ticker: {ticker}"]
    lines.append("last good tick: " + (age_label(success_age) if success is not None else "never"))
    paused, reason = backend.global_pause()
    lines.append(
        "all jobs: PAUSED" + (f" ({reason})" if reason else "") if paused else "all jobs: on"
    )
    health = backend.store_health()
    lines.append(
        "store: writable"
        if health.writable
        else f"store: NOT WRITABLE, no job fires ({health.reason})"
    )
    jobs = backend.list_jobs(include_disabled=True)
    active = [j for j in jobs if doctor_mod.is_active(j)]
    paused_jobs = [j for j in jobs if ports.field(j, "state") == "paused"]
    lines.append(f"jobs: {len(active)} active, {len(paused_jobs)} paused, {len(jobs)} in all")
    upcoming = [
        (when, j)
        for j in active
        if (when := ports.parse_instant(ports.field(j, "next_run_at"))) is not None
    ]
    if upcoming:
        when, soonest = min(upcoming, key=lambda pair: pair[0])
        label = ports.field(soonest, "name") or ports.field(soonest, "id")
        lines.append(f"soonest run: {label} at {_when(when, now)}")
    overdue = sum(1 for j in active if doctor_mod.is_overdue(j, now))
    if overdue:
        lines.append(f"overdue: {overdue} job(s) past their run time by more than 15 min")
    return lines


# -- create / edit --------------------------------------------------------------------------------


def _check(problem: str | None) -> None:
    if problem:
        raise CronRefused(problem)


def _toolsets(value: str | None) -> list[str] | None:
    if value is None:
        return None
    return [t.strip() for t in value.split(",") if t.strip()]


def _shared(scope: Scope, opts: Mapping[str, Any]) -> dict[str, Any]:
    """Checked fields common to create and edit; only keys the person gave are present."""
    out: dict[str, Any] = {}
    if opts.get("prompt") is not None:
        _check(checks.prompt_problem(opts["prompt"]))
        out["prompt"] = opts["prompt"]
    for key in ("name", "schedule"):
        if opts.get(key) is not None:
            out[key] = str(opts[key]).strip()
    if opts.get("repeat") is not None:
        out["repeat"] = int(opts["repeat"])
    if opts.get("deliver") is not None:
        value = targets.join_tokens(targets.split_tokens(opts["deliver"])) or targets.LOCAL
        _check(checks.deliver_problem(value))
        out["deliver"] = value
    if opts.get("failure_deliver") is not None:
        lane = targets.join_tokens(targets.split_tokens(opts["failure_deliver"]))
        _check(checks.deliver_problem(lane) if lane else None)
        out["failure_deliver"] = lane or None
    if opts.get("script") is not None:
        _check(checks.script_problem(scope, opts["script"]))
        out["script"] = opts["script"].strip() or None
    if opts.get("no_agent") is not None:
        out["no_agent"] = bool(opts["no_agent"])
    if opts.get("workdir") is not None:
        _check(checks.workdir_problem(opts["workdir"]))
        out["workdir"] = opts["workdir"].strip() or None
    toolsets = _toolsets(opts.get("toolsets"))
    if toolsets is not None:
        out["enabled_toolsets"] = toolsets
    if opts.get("monitor") is not None:
        mon_script, mon_url = monitor.split_monitor(opts["monitor"])
        if mon_script:
            _check(checks.script_problem(scope, mon_script))
        out["monitor_script"], out["monitor_url"] = mon_script, mon_url
    for key, target in (
        ("model", "model"),
        ("provider", "provider"),
        ("reasoning", "reasoning_effort"),
    ):
        if opts.get(key) is not None:
            out[target] = opts[key].strip() or None
    if opts.get("base_url") is not None:
        out["base_url"] = opts["base_url"].strip().rstrip("/") or None
    if opts.get("context_from"):
        out["context_from"] = list(opts["context_from"])
    return out


def create_spec(scope: Scope, backend: CronBackend, opts: Mapping[str, Any]) -> dict[str, Any]:
    """The checked spec for a new job (CLI and ``/cron add``); raises ``CronRefused``."""
    if opts.get("reason") and not opts.get("paused"):
        raise CronRefused("--reason goes with --paused")
    spec = _shared(scope, opts)
    skills = [s.strip() for s in opts.get("skills") or () if s.strip()]
    if skills:
        spec["skills"] = skills
    spec.setdefault("deliver", targets.LOCAL)
    if spec.get("no_agent") and not spec.get("script"):
        raise CronRefused("--no-agent needs --script: the script's output is the whole job")
    if not spec.get("no_agent") and not (spec.get("prompt") or skills):
        raise CronRefused("give a prompt or at least one --skill (or --script with --no-agent)")
    _check(
        monitor.validate_monitor(
            spec.get("monitor_script"), spec.get("monitor_url"), no_agent=bool(spec.get("no_agent"))
        )
    )
    _check(checks.base_url_problem(scope, spec.get("provider"), spec.get("base_url")))
    _check_context(backend, spec.get("context_from"))
    if opts.get("paused"):
        spec["paused"] = True
        if opts.get("reason"):
            spec["paused_reason"] = opts["reason"]
    return spec


def _check_context(backend: CronBackend, refs: Sequence[str] | None) -> None:
    for ref in refs or ():
        if ref.lower() != checks.SELF_REF:
            find_job(backend, ref)


def edit_changes(
    scope: Scope, backend: CronBackend, job: Any, opts: Mapping[str, Any]
) -> dict[str, Any]:
    changes = _shared(scope, opts)
    skills = list(ports.field(job, "skills", []) or [])
    replaced = [s.strip() for s in opts.get("skills") or () if s.strip()]
    added = [s.strip() for s in opts.get("add_skills") or () if s.strip()]
    removed = {s.strip() for s in opts.get("remove_skills") or () if s.strip()}
    if opts.get("clear_skills"):
        if replaced or added:
            raise CronRefused("--clear-skills cannot be combined with --skill or --add-skill")
        changes["skills"] = []
    elif replaced or added or removed:
        base = replaced if replaced else skills
        merged = [*base, *[s for s in added if s not in base]]
        changes["skills"] = [s for s in merged if s not in removed]
    no_agent = changes.get("no_agent", bool(ports.field(job, "no_agent")))
    script = changes.get("script", ports.field(job, "script"))
    if no_agent and not script:
        raise CronRefused("a script-only job needs a script")
    if "monitor_script" in changes or "no_agent" in changes:
        _check(
            monitor.validate_monitor(
                changes.get("monitor_script", ports.field(job, "monitor_script")),
                changes.get("monitor_url", ports.field(job, "monitor_url")),
                no_agent=no_agent,
            )
        )
    if "base_url" in changes or "provider" in changes:
        _check(
            checks.base_url_problem(
                scope,
                changes.get("provider", ports.field(job, "provider")),
                changes.get("base_url", ports.field(job, "base_url")),
            )
        )
    _check_context(backend, changes.get("context_from"))
    if not changes:
        raise CronRefused("nothing to change; `indus cron edit --help` lists the options")
    return changes


# -- the click group ------------------------------------------------------------------------------


@group("cron", "Schedule jobs and look after them.")
def cron_group() -> None:
    pass


def _common_options(fn: Any) -> Any:
    options = [
        click.option("--name", default=None, help="A short name for the job."),
        click.option("--deliver", default=None, help="Where results go (default: local)."),
        click.option("--failure-deliver", default=None, help="Where failure notices go."),
        click.option("--script", default=None, help="A script file in <home>/scripts/."),
        click.option("--workdir", default=None, help="Absolute folder to work in ('' clears)."),
        click.option("--toolsets", default=None, help="Comma list of toolsets for the job."),
        click.option("--monitor", default=None, help="A URL or script that gates the run."),
        click.option("--model", default=None, help="Pin a model for this job."),
        click.option("--provider", default=None, help="Pin a provider for this job."),
        click.option("--base-url", default=None, help="Endpoint of the pinned provider."),
        click.option("--reasoning", default=None, help="Reasoning effort for this job."),
        click.option("--repeat", type=int, default=None, help="Runs before the job ends."),
        click.option(
            "--context-from", multiple=True, help="Another job whose last result is context."
        ),
    ]
    for option in reversed(options):
        fn = option(fn)
    return fn


def _say(lines: Iterable[str]) -> None:
    for line in lines:
        click.echo(line)


@cron_group.command("create")
@click.argument("schedule")
@click.argument("prompt", nargs=-1)
@_common_options
@click.option("--skill", "skills", multiple=True, help="A skill to load first (repeatable).")
@click.option("--no-agent", is_flag=True, help="Script-only: no model, the output is the job.")
@click.option("--paused", is_flag=True, help="Store the job paused.")
@click.option("--reason", default=None, help="With --paused: why.")
def create_command(schedule: str, prompt: tuple[str, ...], **opts: Any) -> None:
    """Create a job: SCHEDULE like "every 30m", "weekdays at 9am" or "in 2h"."""
    scope = cli_state().scope
    backend = _backend(scope)
    text = " ".join(prompt).strip()
    opts = {
        **opts,
        "schedule": schedule,
        "prompt": text or None,
        "no_agent": True if opts.get("no_agent") else None,
    }
    try:
        spec = create_spec(scope, backend, opts)
        job = backend.create(spec)
    except CronRefused as exc:
        _fail(str(exc))
    except ValueError as exc:
        _fail(str(exc))
    click.echo(f"cron: created {ports.field(job, 'id')}")
    _say(job_lines(job, scope.clock.now()))


@cron_group.command("edit")
@click.argument("job_ref", metavar="JOB")
@_common_options
@click.option("--schedule", default=None, help="A new schedule.")
@click.option("--prompt", default=None, help="A new prompt.")
@click.option("--skill", "skills", multiple=True, help="Replace the skills (repeatable).")
@click.option("--add-skill", "add_skills", multiple=True, help="Add a skill (repeatable).")
@click.option("--remove-skill", "remove_skills", multiple=True, help="Remove a skill.")
@click.option("--clear-skills", is_flag=True, help="Remove every skill.")
@click.option("--no-agent/--agent", "no_agent", default=None, help="Script-only or not.")
def edit_command(job_ref: str, **opts: Any) -> None:
    """Change a job's fields."""
    scope = cli_state().scope
    backend = _backend(scope)
    job = _job(backend, job_ref)
    try:
        changes = edit_changes(scope, backend, job, opts)
        updated = backend.update(str(ports.field(job, "id")), changes)
    except CronRefused as exc:
        _fail(str(exc))
    except ValueError as exc:
        _fail(str(exc))
    click.echo(f"cron: updated {ports.field(updated, 'id')}")
    _say(job_lines(updated, scope.clock.now()))


@cron_group.command("list")
@click.option("--all", "show_all", is_flag=True, help="Also list completed jobs.")
def list_command(show_all: bool) -> None:
    """List the jobs with their next run, last result and warnings."""
    scope = cli_state().scope
    _say(list_lines(_backend(scope), scope, show_all=show_all))


@cron_group.command("pause")
@click.argument("job_ref", metavar="JOB")
@click.option("--reason", default=None, help="Why (shown in `list`).")
def pause_command(job_ref: str, reason: str | None) -> None:
    """Pause a job: it does not fire until resumed."""
    scope = cli_state().scope
    backend = _backend(scope)
    job = backend.pause(str(ports.field(_job(backend, job_ref), "id")), reason)
    click.echo(f"cron: paused {ports.field(job, 'id')}")


@cron_group.command("resume")
@click.argument("job_ref", metavar="JOB")
def resume_command(job_ref: str) -> None:
    """Resume a paused job."""
    scope = cli_state().scope
    backend = _backend(scope)
    job = backend.resume(str(ports.field(_job(backend, job_ref), "id")))
    click.echo(f"cron: resumed {ports.field(job, 'id')}")
    _say(job_lines(job, scope.clock.now()))


def run_lines(backend: CronBackend, job: Any, context: str | None) -> tuple[int, list[str]]:
    job_id = str(ports.field(job, "id"))
    if backend.is_running(job_id):
        return 1, [f"cron: {job_id} is already running"]
    report = asyncio.run(backend.run_now(job_id, context=context))
    lines = [f"cron: {job_id} finished: {report.status}"]
    if report.delivery:
        lines.append(f"  delivery: {report.delivery}")
    if report.error:
        lines.append(f"  error: {report.error}")
    if report.output_path is not None:
        lines.append(f"  output: {report.output_path}")
    if report.excerpt:
        lines.append("")
        lines.append(report.excerpt)
    return (0 if report.status not in ("failed", "blocked") else 1), lines


@cron_group.command("run")
@click.argument("job_ref", metavar="JOB")
@click.option("--context", default=None, help="Extra context for this run only (not saved).")
def run_command(job_ref: str, context: str | None) -> None:
    """Run a job now and wait for it (does not move its schedule)."""
    scope = cli_state().scope
    backend = _backend(scope)
    job = _job(backend, job_ref)
    if context:
        problem = checks.prompt_problem(context)
        if problem:
            _fail(problem)
    code, lines = run_lines(backend, job, context)
    _say(lines)
    if code:
        raise click.exceptions.Exit(code)


@cron_group.command("remove")
@click.argument("job_ref", metavar="JOB")
@click.option("--yes", "-y", is_flag=True, help="Do not ask first.")
def remove_command(job_ref: str, yes: bool) -> None:
    """Delete a job, its notepad and its incidents (output documents stay)."""
    state = cli_state()
    scope = state.scope
    backend = _backend(scope)
    job = _job(backend, job_ref)
    job_id = str(ports.field(job, "id"))
    io = io_for(state, "cron: removing a job cannot be undone; pass -y to go ahead.")
    answer = confirm(
        io, f"Remove job {job_id} ({ports.field(job, 'name') or 'unnamed'})?", assume_yes=yes
    )
    if not answer:
        raise click.exceptions.Exit(1)
    if not backend.remove(job_id):
        _fail(f"{job_id} could not be removed")
    with contextlib.suppress(Exception):
        IncidentLedger.for_scope(scope).forget_job(job_id)
    click.echo(f"cron: removed {job_id}")


@cron_group.command("status")
def status_command() -> None:
    """Is the ticker alive, when does the next job run, is the store healthy?"""
    scope = cli_state().scope
    _say(status_lines(_backend(scope), scope))


@contextlib.contextmanager
def _self_chat() -> Iterator[None]:
    """The ``self-chat`` target (P24-07: a job's output into this profile's own bot chat) works
    from a CLI scheduler pass as it does inside the gateway."""
    from indus.service.self_chat import install_self_chat

    undo = install_self_chat()
    try:
        yield
    finally:
        undo()


@cron_group.command("tick")
def tick_command() -> None:
    """Run one scheduler pass now (for a user timer) and wait for its jobs."""
    scope = cli_state().scope
    with _self_chat():
        code = _backend(scope).tick(click.echo)
    if code:
        raise click.exceptions.Exit(code)


@cron_group.command("daemon")
def daemon_command() -> None:
    """Run the scheduler every minute until stopped (Ctrl+C / SIGTERM)."""
    scope = cli_state().scope
    with _self_chat():
        code = _backend(scope).daemon()
    if code:
        raise click.exceptions.Exit(code)


@cron_group.command("pause-all")
@click.option("--reason", default=None, help="Why (shown in `status`).")
def pause_all_command(reason: str | None) -> None:
    """Emergency stop: no scheduled run starts until resume-all (manual runs still work)."""
    scope = cli_state().scope
    _backend(scope).set_global_pause(True, reason)
    click.echo("cron: all scheduled runs paused; `indus cron resume-all` turns them back on")


@cron_group.command("resume-all")
def resume_all_command() -> None:
    """Undo pause-all."""
    scope = cli_state().scope
    _backend(scope).set_global_pause(False, None)
    click.echo("cron: scheduled runs are on again")


_RUN_KEYS = (
    "attempt_id",
    "job_id",
    "kind",
    "state",
    "scheduled_for",
    "started_at",
    "finished_at",
    "result_status",
    "delivery_status",
)


def _run_rows(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    if not rows:
        return ["cron: no runs recorded yet"]
    return ["  ".join(f"{k}={row[k]}" for k in _RUN_KEYS if row.get(k) is not None) for row in rows]


@cron_group.command("runs")
@click.argument("job_ref", metavar="JOB")
@click.option("--limit", type=int, default=20, help="How many runs to show.")
def runs_command(job_ref: str, limit: int) -> None:
    """The recorded runs of one job, newest first."""
    scope = cli_state().scope
    backend = _backend(scope)
    job_id = str(ports.field(_job(backend, job_ref), "id"))
    _say(_run_rows(backend.runs(job_id, limit=limit)))


@cron_group.command("history")
@click.option("--limit", type=int, default=20, help="How many runs to show.")
def history_command(limit: int) -> None:
    """Recent runs of every job, newest first."""
    scope = cli_state().scope
    _say(_run_rows(_backend(scope).runs(None, limit=limit)))


@cron_group.command("incidents")
@click.option("--all", "show_all", is_flag=True, help="Also resolved and acknowledged ones.")
@click.option("--ack", "ack_id", default=None, help="Acknowledge one: it stays silent.")
def incidents_command(show_all: bool, ack_id: str | None) -> None:
    """Failures grouped by job and error; acknowledge one to silence it."""
    scope = cli_state().scope
    ledger = IncidentLedger.for_scope(scope)
    if ack_id:
        if not ledger.ack(ack_id):
            _fail(f"no open incident {ack_id!r}; `indus cron incidents --all` lists them")
        click.echo(f"cron: incident {ack_id} acknowledged; that failure no longer alerts")
        return
    rows = [i for i in ledger.list() if show_all or i.state in ("detected", "alerted")]
    if not rows:
        click.echo("cron: no open incidents" if not show_all else "cron: no incidents")
        return
    for row in rows:
        click.echo(
            f"{row.id}  job {row.job_id}  {row.state:<8}  seen {row.occurrences}x, last "
            f"{row.last_seen_at[:19]}"
        )
        click.echo(f"    {row.error[:160]}")


@cron_group.command("notepad")
@click.argument("job_ref", metavar="JOB")
@click.argument("action", type=click.Choice(["list", "get", "set", "unset"]))
@click.argument("key", required=False)
@click.argument("value", required=False)
def notepad_command(job_ref: str, action: str, key: str | None, value: str | None) -> None:
    """A job's own notes kept between runs (cursors, last-seen ids)."""
    scope = cli_state().scope
    backend = _backend(scope)
    pad = backend.notepad(str(ports.field(_job(backend, job_ref), "id")))
    if action == "list":
        items = pad.items()
        _say([f"{k}={v}" for k, v in items] or ["cron: the notepad is empty"])
        return
    if not key:
        _fail(f"notepad {action} needs a KEY", code=2)
    if action == "get":
        found = pad.get(key)
        if found is None:
            _fail(f"no note {key!r}")
        click.echo(found)
    elif action == "set":
        if value is None:
            _fail("notepad set needs KEY and VALUE", code=2)
        try:
            pad.set(key, value)
        except ValueError as exc:
            _fail(str(exc))
        click.echo(f"cron: noted {key}")
    elif not pad.unset(key):
        _fail(f"no note {key!r}")
    else:
        click.echo(f"cron: removed note {key}")


@cron_group.command("doctor")
def doctor_command() -> None:
    """Check every active job (read-only); exit 1 while anything needs attention."""
    scope = cli_state().scope
    report = doctor_mod.run_doctor(_backend(scope), scope)
    if report.ok:
        click.echo(f"cron doctor: no problems ({report.checked} active job(s) checked)")
        return
    if report.store_problem:
        click.echo(f"store: {report.store_problem}")
    by_job: dict[str, list[doctor_mod.Finding]] = {}
    for finding in report.findings:
        by_job.setdefault(finding.job_id, []).append(finding)
    for job_id, findings in by_job.items():
        click.echo(f"{job_id}  {findings[0].name or '(unnamed)'}")
        for finding in findings:
            click.echo(f"    - {finding.message}")
    total = len(report.findings) + (1 if report.store_problem else 0)
    click.echo(f"cron doctor: {total} problem(s); fix them and run `indus cron doctor` again")
    raise click.exceptions.Exit(1)


# -- suggested jobs (P24-46) ----------------------------------------------------------------------


@cron_group.group("suggestions", invoke_without_command=True)
@click.pass_context
def suggestions_group(ctx: click.Context) -> None:
    """Suggested jobs: nothing is scheduled until you accept one."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(suggestions_list_command)


def _suggestions(verb: str, ref: str = "") -> None:
    from indus.cron import suggestions
    from indus.cron.errors import CronStoreBusy

    scope = cli_state().scope
    store = suggestions.SuggestionStore(scope)
    try:
        if verb == "list":
            _say(suggestions.pending_lines(store.pending()))
        elif verb == "refresh":
            added = store.refresh()
            click.echo(f"cron: {len(added)} new suggestion(s)")
            _say(suggestions.pending_lines(store.pending()) if added else [])
        elif verb == "dismiss":
            record = store.dismiss(ref)
            click.echo(f"cron: dismissed {record.id} ({record.title}); it will not come back")
        else:
            record, job = store.accept(ref, _backend(scope))
            click.echo(f"cron: created {record.job_id} from suggestion {record.id}")
            _say(job_lines(job, scope.clock.now()))
    except suggestions.SuggestionNotFound:
        _fail(f"no waiting suggestion {ref!r}; `indus cron suggestions` lists them")
    except suggestions.SuggestionRefused as exc:
        _fail(f"the job was not created ({exc}); the suggestion is still waiting")
    except CronStoreBusy as exc:
        _fail(str(exc))


@suggestions_group.command("list")
def suggestions_list_command() -> None:
    """List the suggestions waiting for a yes or no."""
    _suggestions("list")


@suggestions_group.command("accept")
@click.argument("ref", metavar="N|ID")
def suggestions_accept_command(ref: str) -> None:
    """Create the suggested job (list number or id)."""
    _suggestions("accept", ref)


@suggestions_group.command("dismiss")
@click.argument("ref", metavar="N|ID")
def suggestions_dismiss_command(ref: str) -> None:
    """Never suggest this one again."""
    _suggestions("dismiss", ref)


@suggestions_group.command("refresh")
def suggestions_refresh_command() -> None:
    """Offer the starter jobs and the jobs your skills suggest."""
    _suggestions("refresh")
