"""``indus verify [PATH]``: find how to check a workspace and run it (P24-17).

The recipe comes from the workspace's files (``tools.verify.recipes``). Phases ``bootstrap``,
``build`` and ``test`` run in that order on the local terminal backend (its environment builder,
``terminal.*`` settings) and through the approval rules with nobody to ask, so a command that
would need a person's approval is refused rather than run. Then, unless ``--skip-start``, the
recipe's start command is booted and polled on 127.0.0.1 for up to ``verify.ready_timeout``.

``--phase`` (repeatable) picks phases (``bootstrap``, ``build``, ``test``, ``start``);
``--detect-only`` prints the recipe; ``--json`` prints one JSON object. Exit 0 when everything
passed, 1 on a failure or when no recipe was found, 2 when PATH is not a folder.

On native Windows (RL26) the phases run on the Git Bash local backend (``open_local_windows``, the
backend the registry opens there) and the start command boots under Git Bash in a job object
(``WindowsStarter``); with no Git Bash the command says so and exits 1.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import click

from indus.cli.cmdkit import cli_state, config_of, err
from indus.foundation.config.schema import default_for
from indus.foundation.scope import Scope
from indus.platform.facts import os_family

__all__ = ["report_lines", "run_for", "verify_command"]

PHASE_CHOICES = ("bootstrap", "build", "test", "start")


def _setting(scope: Scope, key: str) -> Any:
    value = config_of(scope).get(key)
    return default_for(key) if value is None else value


def _gate(scope: Scope) -> Any:
    from indus.safety import approvals

    async def gate(command: str, cwd: str) -> str | None:
        context = approvals.ApprovalContext(
            scope=scope, run_context="interactive", broker=None, isolated=False, cwd=cwd
        )
        try:
            decision = await approvals.decide(command, context)
        except Exception as exc:  # fail closed: nothing runs unchecked
            return f"the approval check failed ({type(exc).__name__})"
        if decision.approved:
            return None
        return decision.user_summary or decision.description or "the approval rules refused it"

    return gate


async def run_for(
    scope: Scope,
    root: Path,
    *,
    phases: tuple[str, ...],
    skip_start: bool,
    port: int | None,
    phase_timeout: float,
    ready_timeout: float,
) -> tuple[Any, Any]:
    """``(recipe, result)`` for ``root`` (``result`` None when no recipe was found)."""
    from indus.safety.child_env import child_env
    from indus.tools.verify.recipes import detect_recipe
    from indus.tools.verify.runner import LocalStarter, ServiceStarter, WindowsStarter, run_verify

    recipe = detect_recipe(root)
    if recipe is None:
        return None, None
    wants_start = not skip_start and (not phases or "start" in phases)
    starter: ServiceStarter | None = None
    if os_family() == "win32":  # Git Bash, best effort (RL26, D-16.950)
        from indus.backends.local_windows import open_local_windows

        windows_host = await open_local_windows(scope, cwd=str(root), timeout=phase_timeout)
        host: Any = windows_host
        if wants_start:
            starter = WindowsStarter(child_env(scope), windows_host.bash, clock=scope.clock)
    else:
        from indus.backends.local import open_local

        host = await open_local(scope, cwd=str(root), timeout=phase_timeout)
        if wants_start:
            starter = LocalStarter(child_env(scope), clock=scope.clock)
    try:
        selected = phases or None
        result = await run_verify(
            host,
            str(root),
            recipe,
            phases=selected,
            phase_timeout=phase_timeout,
            ready_timeout=ready_timeout,
            clock=scope.clock,
            gate=_gate(scope),
            starter=starter,
            port=port,
        )
    finally:
        await host.cleanup()
    return recipe, result


def report_lines(recipe: Any, result: Any) -> list[str]:
    lines = [f"Recipe: {recipe.name} ({recipe.kind})", ""]
    if result.refused:
        lines += [f"Refused: {result.refused}", "", "Result: FAILED"]
        return lines
    width = max([len(p.phase) for p in result.phases] + [5])
    for phase in result.phases:
        state = (
            "PASS"
            if phase.ok
            else "TIMEOUT"
            if phase.timed_out
            else "REFUSED"
            if (phase.refused)
            else "FAIL"
        )
        lines.append(
            f"  {phase.phase:<{width}}  {state:<7} {phase.duration:7.1f}s  {phase.command}"
        )
    if not result.phases:
        lines.append("  (no phase ran)")
    readiness = result.readiness
    if readiness is not None:
        state = "PASS" if readiness.ready else "FAIL"
        lines.append(f"  {'start':<{width}}  {state:<7} {readiness.duration:7.1f}s  {recipe.start}")
        answer = f"HTTP {readiness.status}" if readiness.ready else readiness.error or "no answer"
        lines.append(f"  ready check: {readiness.url} -> {answer}")
    lines += ["", f"Result: {'OK' if result.ok else 'FAILED'}"]
    for phase in result.phases:
        if phase.ok:
            continue
        if phase.refused:
            lines += ["", f"--- {phase.phase}: {phase.command} was not run: {phase.refused}"]
        elif phase.tail.strip():
            lines += [
                "",
                f"--- output (end) of {phase.phase}: {phase.command}",
                phase.tail.rstrip(),
            ]
    if readiness is not None and not readiness.ready and readiness.tail.strip():
        lines += ["", "--- output (end) of start", readiness.tail.rstrip()]
    return lines


@click.command("verify", short_help="Build and test this workspace.")
@click.argument("path", required=False, type=click.Path(file_okay=True, dir_okay=True))
@click.option(
    "--phase",
    "phases",
    multiple=True,
    type=click.Choice(PHASE_CHOICES),
    help="Run only this phase (repeatable).",
)
@click.option("--json", "as_json", is_flag=True, help="Print one JSON object.")
@click.option("--detect-only", is_flag=True, help="Only print the detected recipe.")
@click.option("--skip-start", is_flag=True, help="Do not boot the start command.")
@click.option("--port", type=int, default=None, help="Port the start command serves on.")
@click.option("--timeout", "phase_timeout", type=float, default=None, help="Seconds per command.")
@click.option("--ready-timeout", type=float, default=None, help="Seconds to wait for the service.")
def verify_command(
    path: str | None,
    phases: tuple[str, ...],
    as_json: bool,
    detect_only: bool,
    skip_start: bool,
    port: int | None,
    phase_timeout: float | None,
    ready_timeout: float | None,
) -> int:
    """Find how to check a workspace (lockfiles, scripts, Makefile, compose) and run its
    bootstrap, build and test commands, then boot and poll its start command."""
    from indus.tools.verify.recipes import detect_recipe

    scope = cli_state().scope
    scope = scope if scope.config is not None else scope.with_config(config_of(scope))
    root = Path(path or ".").expanduser().resolve()
    if not root.is_dir():
        err(f"Not a folder: {root}")
        return 2
    if detect_only:
        recipe = detect_recipe(root)
        payload: dict[str, Any] = {
            "root": str(root),
            "recipe": recipe.as_dict() if recipe else None,
        }
        click.echo(json.dumps(payload, indent=None if as_json else 2))
        return 0 if recipe is not None else 1
    from indus.backends.contract import BackendUnavailable

    try:
        recipe, result = asyncio.run(
            run_for(
                scope,
                root,
                phases=phases,
                skip_start=skip_start,
                port=port,
                phase_timeout=float(phase_timeout or _setting(scope, "verify.phase_timeout")),
                ready_timeout=float(ready_timeout or _setting(scope, "verify.ready_timeout")),
            )
        )
    except BackendUnavailable as exc:  # e.g. no Git Bash on native Windows
        hint = f" ({exc.hint})" if exc.hint else ""
        err(f"The local terminal backend is not available: {exc}{hint}")
        return 1
    if recipe is None:
        if as_json:
            click.echo(json.dumps({"ok": False, "error": "no_recipe", "root": str(root)}))
        else:
            err(
                f"Nothing here says how to build or test {root}: no package.json, pyproject.toml, "
                "go.mod, Cargo.toml, pom.xml, Gradle build, Makefile or compose file."
            )
        return 1
    if as_json:
        click.echo(json.dumps({**result.as_dict(), "root": str(root), "kind": recipe.kind}))
    else:
        for line in report_lines(recipe, result):
            click.echo(line)
    return 0 if result.ok else 1
