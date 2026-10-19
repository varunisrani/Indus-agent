"""``indus hooks``: inspect, try and approve the shell hooks in ``config.yaml`` (P13-19).

- ``list``: configured hooks by event with their approval id and state, and a warning when the
  script changed since it was approved;
- ``test <event> [--payload JSON|@FILE] [--for-tool NAME] [--accept-hooks]``: runs the event's hooks
  once through the same path a session uses and prints the exit, output and the parsed answer. A
  hook without approval runs only after a yes on a terminal or with ``--accept-hooks`` (both record
  the approval);
- ``revoke <id|command>``: drops an approval; the hook is asked for again next time;
- ``doctor``: command found and executable, approval, and a dry run of approved hooks.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import click

from indus.cli import prompts
from indus.cli.cmdkit import cli_state, err, group
from indus.cli.commands._support import scope_with_config
from indus.foundation.scope import Scope
from indus.platform.locate import which
from indus.plugins import shell_hooks as sh

__all__ = ["DEFAULT_PAYLOADS", "hooks_group"]

DEFAULT_PAYLOADS: dict[str, dict[str, Any]] = {
    "pre_tool_call": {
        "session_id": "hooks-test",
        "tool_name": "terminal",
        "args": {"command": "echo hello"},
        "tool_call_id": "call-test",
    },
    "post_tool_call": {
        "session_id": "hooks-test",
        "tool_name": "terminal",
        "args": {"command": "echo hello"},
        "result": "hello",
        "is_error": False,
        "duration_ms": 12,
    },
    "pre_llm_call": {
        "session_id": "hooks-test",
        "user_message": "What changed in this repository today?",
        "is_first_turn": True,
        "model": "test-model",
        "platform": "cli",
    },
    "on_session_start": {"session_id": "hooks-test", "platform": "cli"},
    "on_session_end": {"session_id": "hooks-test", "platform": "cli", "completed": True},
}


def _scope() -> Scope:
    return scope_with_config(cli_state().scope)


def _clip(text: str, limit: int = 400) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


@group("hooks", "Inspect, test and approve shell hooks.")
def hooks_group() -> None:
    pass


@hooks_group.command("list")
def list_command() -> None:
    """Show the configured shell hooks and whether each is approved."""
    scope = _scope()
    specs, warnings = sh.configured_hooks(scope)
    err(*(f"warning: {w}" for w in warnings))
    if not specs:
        click.echo("No shell hooks are configured (add them under hooks: in config.yaml).")
        return
    store = sh.ConsentStore(scope.home)
    grants, readable = store.read()
    if not readable:
        err(f"warning: {store.path} cannot be read; nothing counts as approved")
    by_event: dict[str, list[sh.HookSpec]] = {}
    for spec in specs:
        by_event.setdefault(spec.event, []).append(spec)
    click.echo(f"Shell hooks ({len(specs)}):")
    for event in sh.EVENTS:
        if event not in by_event:
            continue
        click.echo(f"  {event}")
        for spec in by_event[event]:
            grant = grants.get(spec.digest)
            state = f"approved {grant.granted_at}" if grant else "not approved"
            extra = f", matcher {spec.matcher}" if spec.matcher else ""
            click.echo(f"    [{spec.grant_id}] {spec.command}")
            click.echo(f"        {state}; timeout {spec.timeout_s:g} s{extra}")
            if grant is not None and grant.script_mtime is not None:
                now = sh.script_mtime(spec.command)
                if now is not None and now != grant.script_mtime:
                    click.echo(
                        "        warning: the script changed since it was approved; check it, "
                        "then revoke and approve it again"
                    )


def _payload(event: str, raw: str | None, for_tool: str | None) -> dict[str, Any]:
    payload = dict(DEFAULT_PAYLOADS.get(event, {"session_id": "hooks-test"}))
    if raw:
        text = raw
        if raw.startswith("@"):
            try:
                text = Path(raw[1:]).expanduser().read_text(encoding="utf-8-sig")
            except OSError as exc:
                raise click.UsageError(f"cannot read {raw[1:]}: {exc}") from None
        try:
            given = json.loads(text)
        except ValueError as exc:
            raise click.UsageError(f"--payload is not JSON: {exc}") from None
        if not isinstance(given, Mapping):
            raise click.UsageError("--payload must be a JSON object")
        payload.update(cast(Mapping[str, Any], given))
    if for_tool:
        payload["tool_name"] = for_tool
    return payload


def _show_run(run: sh.HookRun, parsed: object) -> None:
    if run.error is not None:
        click.echo(f"      could not run: {run.error}")
    elif run.timed_out:
        click.echo(f"      timed out after {run.elapsed_s:.1f} s")
    else:
        click.echo(f"      exit {run.exit_code} after {run.elapsed_s:.2f} s")
        for name, text in (("stdout", run.stdout), ("stderr", run.stderr)):
            if text.strip():
                click.echo(f"      {name}: {_clip(text)}")
    if parsed is None:
        click.echo("      parsed: nothing (the hook adds no decision)")
    else:
        click.echo(f"      parsed: {json.dumps(parsed, ensure_ascii=False, sort_keys=True)}")


def _fire(scope: Scope, spec: sh.HookSpec, payload: Mapping[str, Any]) -> tuple[sh.HookRun, object]:
    body = sh.build_payload(spec.event, payload, str(Path.cwd()), scope.profile_id)
    session = str(payload.get("session_id") or "") or None
    run = asyncio.run(sh.run_hook(spec, body, scope=scope, cwd=str(Path.cwd()), session_id=session))
    return run, sh.interpret(spec, run)


@hooks_group.command("test")
@click.argument("event")
@click.option(
    "--payload", default=None, help="JSON object (or @FILE) merged into the test payload."
)
@click.option("--for-tool", default=None, help="Tool name for tool events (filters by matcher).")
@click.option(
    "--accept-hooks", is_flag=True, help="Run and approve hooks that are not approved yet."
)
def test_command(event: str, payload: str | None, for_tool: str | None, accept_hooks: bool) -> None:
    """Run the hooks of EVENT once and show what each answered."""
    if event not in sh.EVENTS:
        raise click.UsageError(f"unknown event {event!r}; one of {', '.join(sh.EVENTS)}")
    state = cli_state()
    scope = _scope()
    body = _payload(event, payload, for_tool)
    specs = [s for s in sh.configured_hooks(scope)[0] if s.event == event]
    if for_tool:
        specs = [s for s in specs if s.matches(for_tool)]
    if not specs:
        click.echo(f"No shell hooks for {event}.")
        return
    store = sh.ConsentStore(scope.home)
    accept = sh.accept_from(scope, accept_hooks)
    io = prompts.io_for(state)
    click.echo(f"Running {len(specs)} hook(s) for {event}:")
    for spec in specs:
        click.echo(f"  [{spec.grant_id}] {spec.command}")
        if store.granted(spec) is None:
            approved = accept
            if not approved and io.tty:
                click.echo("    " + sh.consent_text(spec).replace("\n", "\n    "))
                approved = prompts.confirm(io, "    Approve and run it?") is True
            if not approved:
                click.echo(
                    "      not approved: skipped (approve it with --accept-hooks or on a terminal)"
                )
                continue
            store.grant(spec)
        run, parsed = _fire(scope, spec, body)
        _show_run(run, parsed)


@hooks_group.command("revoke")
@click.argument("key")
def revoke_command(key: str) -> None:
    """Drop the approval with id KEY (or for the command KEY)."""
    scope = _scope()
    gone = sh.ConsentStore(scope.home).revoke(key)
    if not gone:
        err(f"No approval matches {key!r}; see `indus hooks list`.")
        raise click.exceptions.Exit(1)
    for grant in gone:
        click.echo(f"Approval {grant.id} removed: [{grant.event}] {grant.command}")
    click.echo("A running session keeps the hook until it restarts; new sessions ask again.")


def _runnable(command: str) -> str | None:
    """Why ``command`` cannot start, or None when it looks runnable."""
    path = sh.script_path(command)
    if path is not None:
        if not path.is_file():
            return f"{path} does not exist"
        if not path.stat().st_mode & 0o111:
            words = command.split()
            if len(words) == 1:
                return f"{path} is not executable (chmod +x it)"
        return None
    first = command.strip().split(maxsplit=1)[0]
    if which(first) is None:
        return f"{first} is not on PATH"
    return None


@hooks_group.command("doctor")
def doctor_command() -> None:
    """Check every configured hook: command, approval, and a dry run."""
    scope = _scope()
    specs, warnings = sh.configured_hooks(scope)
    problems = len(warnings)
    err(*(f"warning: {w}" for w in warnings))
    if not specs:
        click.echo("No shell hooks are configured; nothing to check.")
        if problems:
            raise click.exceptions.Exit(1)
        return
    store = sh.ConsentStore(scope.home)
    for spec in specs:
        click.echo(f"[{spec.grant_id}] {spec.event}: {spec.command}")
        why = _runnable(spec.command)
        if why is None:
            click.echo("    ok: the command can start")
        else:
            problems += 1
            click.echo(f"    problem: {why}")
        if store.granted(spec) is None:
            click.echo("    note: not approved yet (sessions skip it until it is)")
            continue
        click.echo("    ok: approved")
        run, parsed = _fire(scope, spec, _payload(spec.event, None, None))
        if run.error is not None or run.timed_out:
            problems += 1
        _show_run(run, parsed)
    if problems:
        click.echo(f"{problems} problem(s) found.")
        raise click.exceptions.Exit(1)
    click.echo("All shell hooks look healthy.")
