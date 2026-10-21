"""``indus update``: move this source checkout to the newest release of its channel (P23-17..19).

- bare: fetch, pick the target (``cli.update.channels``), then apply (``cli.update.apply``) and
  hand off to the new code's ``--finish`` (``cli.update.finish``). Without a terminal it needs
  ``-y`` (checked before anything reaches the network);
- ``--check``: report only; exit 0 (up to date, ahead, nothing published) or 10 (available);
- ``--plan``: what an update would do, as JSON (install kind, from/to, this profile's gateway);
- ``--channel C`` for this run, ``--set-channel C`` saved for this install, ``--install-id``;
- ``--backup`` (full backup first), ``--no-backup``, ``-y``, ``--no-gateway-restart``;
- ``--finish REQUEST`` (hidden): the second half, run by the new code only.

While an update replaces the code and environment, ``<root>/.indus-update-in-progress`` names this
process (``cli.update.marker``, the contract the desktop app reads): claimed under the update lock
before the checkout moves, removed by the same pid when the run ends (in ``--finish`` after the
hand-off). A live marker of another process refuses the run.

Managed installs (Docker, Nix, the desktop's bundled runtime, packages) are refused with their own
instruction, exit 1. Everything the outside world provides is built by ``wiring`` so tests can
swap it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import click

from indus.cli.args import CliState
from indus.cli.commands._support import err, fresh_view, interactive, state_of
from indus.cli.update import (
    EXIT_AVAILABLE,
    REFUSALS,
    Console,
    Receipt,
    UpdatePaths,
    detect_kind,
    install_id,
    marker,
)
from indus.cli.update.apply import ApplyHooks, ApplyRequest, apply_update
from indus.cli.update.channels import (
    INSTALLS_KEY,
    Git,
    GitError,
    build_plan,
    choose_channel,
    current_version,
    decide,
    fetch,
    is_official,
    resolve_target,
    saved_channel,
    saved_extras,
    store_refusal,
    write_check_cache,
)
from indus.cli.update.finish import (
    DistBuilder,
    FinishContext,
    GatewayRestarter,
    ProfileGateway,
    default_builders,
    run_finish,
)
from indus.cli.version import install_dir
from indus.foundation.fsio import FileLock, LockTimeout
from indus.foundation.scope import Scope
from indus.foundation.versions import CHANNELS

__all__ = ["Wiring", "update_command", "wiring"]

_NOT_A_TERMINAL = (
    "indus update changes this install and may ask questions. Without a terminal run "
    "`indus update -y` (or `indus update --check` to only look)."
)


@dataclass
class Wiring:
    """The outside world of one ``indus update`` run."""

    package_dir: Path | None
    run: Callable[..., subprocess.CompletedProcess[str]]
    hooks: ApplyHooks
    gateway: GatewayRestarter
    builders: list[DistBuilder]
    pnpm: str | None
    now: Callable[[], datetime]
    git_run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run
    exists: Callable[[str], bool] = os.path.exists
    """File probe for the install-kind markers (``/.dockerenv``)."""


def _locate_uv(scope: Scope) -> str | None:
    from indus.platform.locate import which

    found = which("uv", path=scope.env.get("PATH"))
    if found:
        return found
    tools = sorted((scope.root / "tools").glob("uv-*/uv*"), reverse=True)
    return next((str(p) for p in tools if p.name in {"uv", "uv.exe"} and p.is_file()), None)


def _snapshot(scope: Scope, keep: int) -> Callable[[str], str]:
    def take(mode: str) -> str:
        from indus.cli.commands import backup

        if mode == "full":
            code = backup.run_full_backup(scope, None, keep)
        else:
            code = backup.run_quick_snapshot(scope, "pre-update")
        if code != 0:
            raise RuntimeError(f"the {mode} backup ended with exit {code}")
        return f"{mode} snapshot"

    return take


def _profile_gateway(scope: Scope) -> ProfileGateway:
    from indus.service import gateway_control
    from indus.service.gateway_manager import manager_for
    from indus.service.gateway_units import UnitSpec
    from indus.state import gateway_runtime

    view = fresh_view(scope)
    spec = UnitSpec.build(scope.home, env=scope.env, setting=lambda key, d: view.get(key, d))
    manager = manager_for(spec, scope.env, platform=sys.platform)

    def ask_stop(home: Path) -> tuple[bool, str]:
        reply = gateway_control.query(
            home, "stop", {"restart": True, "reason": "update"}, timeout=10.0
        )
        return reply.ok, reply.error or ""

    def start() -> str:
        argv = [sys.executable, "-m", "indus", "gateway", "start"]
        try:
            done = subprocess.run(
                argv,
                env={**scope.env.to_dict(), "INDUS_HOME": str(scope.home)},
                capture_output=True,
                text=True,
                timeout=90.0,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return str(exc)
        if done.returncode == 0:
            return ""
        lines = (done.stderr or done.stdout).strip().splitlines()
        return lines[-1] if lines else f"exit {done.returncode}"

    def record(home: Path, pid: int, start_time: int | None) -> None:
        gateway_runtime.record_planned_stop(
            home, pid, "restart", start_time=start_time, by="update"
        )

    return ProfileGateway(
        home=scope.home,
        manager=manager,
        identify=gateway_control.identify,
        ask_stop=ask_stop,
        alive=gateway_runtime.alive,
        start=start,
        record_stop=record,
        wait=lambda s: threading.Event().wait(s),
    )


def _default_wiring(scope: Scope, state: CliState) -> Wiring:
    from indus.platform.locate import which

    view = fresh_view(scope)
    keep = view.get("updates.backup_keep", 5)
    pnpm = which("pnpm", path=scope.env.get("PATH"))
    return Wiring(
        package_dir=None,
        run=subprocess.run,
        hooks=ApplyHooks(
            snapshot=_snapshot(scope, keep if isinstance(keep, int) else 5), uv=_locate_uv(scope)
        ),
        gateway=_profile_gateway(scope),
        builders=default_builders(pnpm),
        pnpm=pnpm,
        now=scope.clock.now,
    )


wiring: Callable[[Scope, CliState], Wiring] = _default_wiring
"""Builds the outside world of a run (tests replace it)."""


def _finish(
    state: CliState, wired: Wiring, request: Path, assume_yes: bool, no_restart: bool
) -> int:
    from indus.cli import prompts
    from indus.foundation.config import migrations

    scope = state.scope
    io = prompts.io_for(state, "")

    def confirm(question: str) -> bool | None:
        if assume_yes:
            return True
        if not io.tty:
            return None
        return prompts.confirm(io, question)

    data_root = scope.root
    console = Console(UpdatePaths(Path("."), data_root).log_file, now=wired.now)
    ctx = FinishContext(
        scope=scope,
        console=console,
        gateway=wired.gateway,
        builders=wired.builders,
        now=wired.now,
        confirm=confirm,
        migrate=lambda dry: migrations.migrate(scope, dry_run=dry),
        run=wired.run,
        pnpm=wired.pnpm,
    )
    try:
        return run_finish(request, ctx, no_gateway_restart=no_restart)
    finally:
        # The old process claimed the marker and exec'd this one (the pid is the same).
        marker.release(data_root)


def _save_channel(scope: Scope, iid: str, channel: str) -> str | None:
    from indus.foundation.config import writer
    from indus.foundation.errors import IndusError

    section, _sep, key = INSTALLS_KEY.partition(".")
    try:
        writer.save(scope, {section: {key: {iid: {"channel": channel}}}})
    except IndusError as exc:
        return exc.message
    return None


@click.command("update", short_help="Update this install to the newest release of its channel.")
@click.option(
    "--check",
    is_flag=True,
    help="Only look: exit 10 when an update is available, 0 otherwise. Installs nothing.",
)
@click.option("--plan", "show_plan", is_flag=True, help="Print what an update would do, as JSON.")
@click.option(
    "--channel", type=click.Choice(CHANNELS), default=None, help="Use this channel for this run."
)
@click.option(
    "--set-channel",
    type=click.Choice(CHANNELS),
    default=None,
    help="Save the channel this install follows, then update to it.",
)
@click.option("--install-id", "show_id", is_flag=True, help="Print this install's id and path.")
@click.option("--backup", is_flag=True, help="Take a full backup before updating.")
@click.option("--no-backup", is_flag=True, help="Skip the pre-update snapshot.")
@click.option("-y", "--yes", "assume_yes", is_flag=True, help="Answer yes to every question.")
@click.option(
    "--no-gateway-restart",
    is_flag=True,
    help="Leave the gateway running on the old code (restart it later yourself).",
)
@click.option("--finish", type=click.Path(path_type=Path), default=None, hidden=True)
@click.pass_context
def update_command(
    ctx: click.Context,
    check: bool,
    show_plan: bool,
    channel: str | None,
    set_channel: str | None,
    show_id: bool,
    backup: bool,
    no_backup: bool,
    assume_yes: bool,
    no_gateway_restart: bool,
    finish: Path | None,
) -> int:
    """Update this source checkout: fetch its channel, check the new code, rebuild its
    environment beside the old one and restart this profile's gateway."""
    state = state_of(ctx)
    scope = state.scope
    if backup and no_backup:
        err("--backup and --no-backup cannot be used together.")
        return 2
    if channel and set_channel:
        err("--channel and --set-channel cannot be used together.")
        return 2
    wired = wiring(scope, state)
    if finish is not None:
        return _finish(state, wired, finish, assume_yes, no_gateway_restart)
    checkout = install_dir(wired.package_dir)
    iid = install_id(checkout)
    if show_id:
        click.echo(f"{iid}  {checkout}")
        return 0
    kind = detect_kind(wired.package_dir, env=scope.env, exists=wired.exists)
    if kind != "source":
        if show_plan:
            plan = build_plan(
                kind=kind, checkout=checkout, iid=iid, choice=None, decision=None,
                current=None, services=[], refusal=REFUSALS[kind],
            )  # fmt: skip
            click.echo(json.dumps(plan, indent=2))
        else:
            err(REFUSALS[kind])
        return 1
    if not (check or show_plan or assume_yes or interactive(state)):
        err(_NOT_A_TERMINAL)
        return 1
    paths = UpdatePaths(checkout, scope.root)
    console = Console(None if (check or show_plan) else paths.log_file, now=wired.now)
    view = fresh_view(scope)
    installs = view.get(INSTALLS_KEY, {})
    saved = saved_channel(installs, iid)
    if set_channel is not None:
        problem = _save_channel(scope, iid, set_channel)
        if problem is not None:
            err(f"Could not save the channel: {problem}")
            return 1
        click.echo(f"This install now follows {set_channel}.")
        saved = set_channel
    git = Git(checkout, scope.env.to_dict(), runner=wired.git_run, log=console.child_output)
    choice = choose_channel(channel, saved, official=is_official(git.remote_url()))
    try:
        fetch(git)
        target = resolve_target(git, choice.channel)
        decision = decide(git, choice, target)
        current = current_version(git, decision.head)
        refusal = (
            store_refusal(git, scope.root, target)
            if decision.moves and target is not None
            else None
        )
    except GitError as exc:
        err(f"✗ Could not read the {choice.channel} channel: {exc}", f"  {exc.detail}".rstrip())
        return 1
    if show_plan:
        services = [wired.gateway.describe()]
        plan = build_plan(
            kind=kind, checkout=checkout, iid=iid, choice=choice, decision=decision,
            current=current, services=services, refusal=refusal,
        )  # fmt: skip
        click.echo(json.dumps(plan, indent=2))
        return 0
    if check:
        notices = view.get("updates.check", True) is not False
        write_check_cache(
            paths.cache_file, decision=decision, channel=choice.channel, notices=notices,
            now=wired.now(),
        )  # fmt: skip
        if refusal:
            click.echo(f"{choice.channel}: {refusal}")
            return 0
        if decision.action == "update" and decision.target is not None:
            click.echo(
                f"Update available on {choice.channel}: {decision.target.label} "
                "(run `indus update`)."
            )
            return EXIT_AVAILABLE
        click.echo(f"{choice.channel}: {decision.reason}.")
        return 0
    if not decision.moves or decision.target is None:
        hint = ""
        if decision.action == "none" and choice.channel != "main":
            hint = " (or follow every commit: indus update --channel main)"
        click.echo(f"Nothing to do: {decision.reason}{hint}.")
        return 0
    if refusal:
        err(f"✗ {refusal}")
        return 1
    return _apply(scope, wired, paths, console, git, choice, decision, iid, installs,
                  backup=backup, no_backup=no_backup, assume_yes=assume_yes,
                  no_gateway_restart=no_gateway_restart)  # fmt: skip


def _apply(
    scope: Scope,
    wired: Wiring,
    paths: UpdatePaths,
    console: Console,
    git: Git,
    choice: Any,
    decision: Any,
    iid: str,
    installs: object,
    *,
    backup: bool,
    no_backup: bool,
    assume_yes: bool,
    no_gateway_restart: bool,
) -> int:
    view = fresh_view(scope)
    if no_backup:
        mode, reason = "off", "--no-backup"
    elif backup:
        mode, reason = "full", ""
    else:
        configured = str(view.get("updates.pre_update_backup", "quick") or "quick")
        mode = configured if configured in {"quick", "full", "off"} else "quick"
        reason = "updates.pre_update_backup is off" if mode == "off" else ""
    target = decision.target
    if decision.action == "rollback":
        console.say(f"→ {decision.reason} (an explicit {choice.channel} choice).")
    receipt = Receipt(started_at=wired.now().isoformat(), install_id=iid, channel=choice.channel)
    finish_argv = [
        *(["-y"] if assume_yes else []),
        *(["--no-gateway-restart"] if no_gateway_restart else []),
    ]
    request = ApplyRequest(
        paths=paths,
        profile_home=scope.home,
        choice=choice,
        target=target,
        env=scope.env.to_dict(),
        now=wired.now,
        backup_mode=mode,
        backup_reason=reason,
        extras=saved_extras(installs, iid),
        assume_yes=assume_yes,
        no_gateway_restart=no_gateway_restart,
        finish_argv=finish_argv,
    )
    paths.root.mkdir(parents=True, exist_ok=True)
    lock = FileLock(paths.lock_file, timeout=0.5)
    try:
        lock.acquire()
    except LockTimeout:
        err("✗ Another `indus update` is running for this install; try again when it ends.")
        return 1
    try:
        try:
            marker.claim(paths.root)
        except marker.MarkerBusy as exc:
            err(f"✗ {exc}; try again when it ends.")
            return 1
        except OSError as exc:
            console.warn(
                f"⚠ Could not write the update marker ({exc}); the desktop app may not wait."
            )
        console.say(f"Updating {paths.checkout} on {choice.channel} to {target.label}...")
        # On success apply_update execs the new code's --finish (same pid), which removes the
        # marker; every other way out of it returns here.
        return apply_update(git, request, wired.hooks, console, receipt)
    finally:
        marker.release(paths.root)
        lock.release()
