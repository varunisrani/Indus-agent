"""``indus approvals test -- CMD…``: what the approval rules would do with a command.

The real P05 pipeline (``safety.approvals.decide``) decides, for this profile's settings, with a
fresh approval memory (no session approval counts, none is recorded) and a stand-in for the person
that notes it was asked and does not answer. Nothing runs and nothing is saved. The backend's
flags come from the terminal backend registry, so an isolated backend gets the pipeline's isolated
path. Exit codes: 0 allow, 2 would ask, 3 deny, 1 usage or an unknown backend.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import click

from indus.cli.commands._support import err, scope_with_config, state_of
from indus.cli.lazy import LazyGroup
from indus.foundation.scope import Scope
from indus.protocol.events import CancelReason
from indus.protocol.requests_port import AnswerModel, RequestNotAnswered
from indus.safety.approvals import ApprovalContext, ApprovalDecision, ApprovalState, decide, detect
from indus.safety.approvals.detect import normalise

__all__ = [
    "EXIT_ALLOW",
    "EXIT_ASK",
    "EXIT_DENY",
    "EXIT_USAGE",
    "Verdict",
    "approvals_command",
    "evaluate",
    "approvals_test_command",
]

EXIT_ALLOW = 0
EXIT_USAGE = 1
EXIT_ASK = 2
EXIT_DENY = 3

type VerdictWord = Literal["allow", "ask", "deny"]
_EXIT: dict[VerdictWord, int] = {"allow": EXIT_ALLOW, "ask": EXIT_ASK, "deny": EXIT_DENY}

_EXPLAIN: dict[str, str] = {
    "empty": "an empty command runs nothing",
    "isolated": "the backend is isolated: only the hardline floor and your deny rules apply",
    "floor": "a safety floor blocks it whatever the approval settings",
    "auto": "approvals are bypassed here (--yolo, INDUS_YOLO_MODE or approvals.mode: off)",
    "allowlist": "it matches an entry of command_allowlist",
    "detect": "nothing in it is flagged as risky",
    "session": "this pattern was approved earlier in the session",
    "unattended": "nobody could answer here, so the unattended policy decided",
    "human": "it is flagged as risky, so a person would be asked first",
    "scope": "no profile was active",
}


class _NobodyAnswers:
    """Stands in for the person: records that it was asked and gives no answer."""

    def __init__(self) -> None:
        self.asked = False

    async def ask(
        self,
        kind: str,
        payload: Mapping[str, Any],
        *,
        timeout: float | None = None,
        turn_id: str | None = None,
    ) -> AnswerModel:
        self.asked = True
        raise RequestNotAnswered("dry run: nobody answers")

    def cancel_all(self, reason: CancelReason) -> int:
        return 0

    def human_wait_seconds(self) -> float:
        return 0.0


@dataclass(frozen=True)
class Verdict:
    command: str
    backend: str
    verdict: VerdictWord
    step: str
    outcome: str
    rule: str | None
    description: str | None
    trace: tuple[str, ...]

    @property
    def exit_code(self) -> int:
        return _EXIT[self.verdict]

    @property
    def explanation(self) -> str:
        return _EXPLAIN.get(self.step, self.step)

    def to_json(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "backend": self.backend,
            "verdict": self.verdict,
            "exit_code": self.exit_code,
            "step": self.step,
            "outcome": self.outcome,
            "rule": self.rule,
            "description": self.description,
            "trace": list(self.trace),
        }


def _isolated(backend: str) -> bool | None:
    """The backend's isolation flag, or None when no such backend is registered."""
    from indus.tools.builtin import terminal

    entry = terminal.runtime().environments.entry(backend)
    return None if entry is None else entry.flags.isolated


def known_backends() -> list[str]:
    from indus.tools.builtin import terminal

    return terminal.runtime().environments.names()


def _trace(command: str, scope: Scope) -> tuple[str, ...]:
    home = scope.env.get("HOME") or None
    detection = detect(command, user_home=home, indus_home=str(scope.home))
    lines = [normalise(command)]
    lines.extend(line for line in detection.trace if line not in lines)
    return tuple(lines)


def evaluate(
    command: str, scope: Scope, *, isolated: bool = False, backend: str = "local"
) -> Verdict:
    """The dry-run verdict for ``command`` (see the module notes)."""
    broker = _NobodyAnswers()
    ctx = ApprovalContext(
        scope=scope,
        session_id="approvals-test",
        run_context="interactive",
        broker=broker,
        isolated=isolated,
        state=ApprovalState(),
    )
    decision: ApprovalDecision = asyncio.run(decide(command, ctx))
    if broker.asked:
        word: VerdictWord = "ask"
    elif decision.approved:
        word = "allow"
    else:
        word = "deny"
    return Verdict(
        command=command,
        backend=backend,
        verdict=word,
        step=decision.step,
        outcome=decision.outcome,
        rule=decision.pattern_key,
        description=decision.description,
        trace=_trace(command, scope),
    )


def _render(verdict: Verdict) -> None:
    click.echo(f"Command: {verdict.command}")
    click.echo(f"Backend: {verdict.backend}")
    click.echo(f"Verdict: {verdict.verdict} (exit {verdict.exit_code})")
    click.echo(f"Why:     {verdict.explanation}")
    if verdict.rule or verdict.description:
        rule = verdict.rule or ""
        about = f" — {verdict.description}" if verdict.description else ""
        click.echo(f"Rule:    {rule}{about}")
    click.echo("Checked as:")
    for line in verdict.trace:
        click.echo(f"  {line}")


@click.group(
    "approvals",
    cls=LazyGroup,
    invoke_without_command=True,
    help="Check how the approval rules treat commands.",
)
@click.pass_context
def approvals_command(ctx: click.Context) -> int:
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())
    return 0


@approvals_command.command(
    "test",
    help="Show whether a command would run, need approval or be refused, without running it. "
    "Put -- before the command so its own flags are not read as ours. "
    "Exit codes: 0 allow, 2 would ask, 3 deny.",
    context_settings={"ignore_unknown_options": True, "allow_interspersed_args": False},
)
@click.option(
    "--backend", default="local", show_default=True, help="Terminal backend to judge for."
)
@click.option("--json", "as_json", is_flag=True, help="Print the verdict as JSON.")
@click.argument("words", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def approvals_test_command(
    ctx: click.Context, backend: str, as_json: bool, words: Sequence[str]
) -> int:
    parts = list(words)
    if parts and parts[0] == "--":
        parts = parts[1:]
    command = " ".join(parts)
    if not command.strip():
        err("Usage: indus approvals test [--backend NAME] [--json] -- COMMAND...")
        return EXIT_USAGE
    name = backend.strip().lower()
    isolated = _isolated(name)
    if isolated is None:
        err(f"Unknown backend '{backend}'. Known: {', '.join(known_backends())}.")
        return EXIT_USAGE
    scope = scope_with_config(state_of(ctx).scope)
    verdict = evaluate(command, scope, isolated=isolated, backend=name)
    if as_json:
        click.echo(json.dumps(verdict.to_json(), indent=2, ensure_ascii=False))
    else:
        _render(verdict)
    return verdict.exit_code
