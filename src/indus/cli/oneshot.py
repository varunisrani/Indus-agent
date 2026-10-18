"""One-shot ``indus -z PROMPT`` and finite ``indus chat -q`` runs (R04.20).

Both run one turn through ``SessionService`` on a private event loop, with the run context
``single_query`` and no server-request kind answerable (nobody is there to answer). They differ in
what they print and in their exit codes, on purpose — scripts depend on both tables:

``-z`` (``oneshot_exit_code``)        ``chat -q`` (``finite_exit_code``)
0  completed with reply text           0  completed
1  completed, no text; never started    1  anything else that did not complete, never ran
2  failed, partial, budget, usage       130 interrupted
130 interrupted

``-z`` prints only the final reply on stdout (stdout and stderr are pointed at the null device
while the turn runs) and auto-approves risky commands for the run; the approval floors still
refuse hardline commands. ``--usage-file`` gets a JSON report even when the run fails; a report
that cannot be written never changes the exit code. When ``indus`` runs as a real process, the
entry point then flushes, shuts logging down and leaves through ``os._exit``
(``CliState.hard_exit``) so no late finaliser can change the code after the reply was printed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, TextIO

import click

from indus.cli.args import ChatArgs, CliState, split_csv
from indus.protocol.events import ExitReason

__all__ = [
    "INTERRUPTED",
    "RunOutcome",
    "finite_exit_code",
    "oneshot_exit_code",
    "run_finite",
    "run_from_cli",
    "usage_report",
    "write_usage_file",
]

log = logging.getLogger(__name__)

INTERRUPTED = 130
_INTERRUPT_REASONS = frozenset({ExitReason.INTERRUPTED_BY_USER, ExitReason.INTERRUPTED_BY_SYSTEM})
_PARTIAL_REASONS = frozenset(
    {ExitReason.BUDGET_EXHAUSTED, ExitReason.MAX_ITERATIONS, ExitReason.COMPRESSION_EXHAUSTED}
)
_PREFIX = "indus -z"


@dataclass
class RunOutcome:
    """What one finite turn came to (``started`` False: it never reached the model)."""

    started: bool = False
    exit_reason: ExitReason | None = None
    final_text: str | None = None
    api_calls: int = 0
    session_id: str | None = None
    model: str | None = None
    provider: str | None = None
    failure: str | None = None
    interrupted: bool = False
    usage: dict[str, Any] | None = None
    """``UsageSnapshot.as_dict()`` of the session's ledger (P08-08), once the turn ran."""

    @property
    def completed(self) -> bool:
        return self.started and self.exit_reason == ExitReason.COMPLETED and self.failure is None

    @property
    def partial(self) -> bool:
        if self.completed or self.interrupted:
            return False
        return self.exit_reason in _PARTIAL_REASONS or bool((self.final_text or "").strip())

    @property
    def failed(self) -> bool:
        return not self.completed and not self.interrupted


def oneshot_exit_code(outcome: RunOutcome) -> int:
    """The ``-z`` table (see the module notes)."""
    if outcome.interrupted:
        return INTERRUPTED
    if not outcome.started:
        return 1
    if not outcome.completed:
        return 2
    if not (outcome.final_text or "").strip():
        return 1
    return 0


def finite_exit_code(outcome: RunOutcome) -> int:
    """The ``chat -q`` table (see the module notes)."""
    if outcome.interrupted:
        return INTERRUPTED
    return 0 if outcome.completed else 1


# -- usage report ----------------------------------------------------------------------------------

_TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "total_tokens",
)


_COST_SOURCES = {"estimated": "price_table", "actual": "provider", "included": "local"}


def usage_report(outcome: RunOutcome, failure: str | None = None) -> dict[str, Any]:
    """The ``--usage-file`` JSON, from the session's usage ledger (P08-08). Counters stay null
    (``unknown``) when no model call was answered; the cost is null when no call could be
    priced, and ``cost_status`` never claims more than the ledger knows."""
    report: dict[str, Any] = {
        "estimated_cost_usd": None,
        "cost_status": "unknown",
        "cost_source": None,
    }
    for key in _TOKEN_KEYS:
        report[key] = None
    report["tokens_status"] = "unknown"
    ledger = outcome.usage or {}
    if ledger.get("api_calls"):
        status = str(ledger.get("cost_status") or "unknown")
        report["estimated_cost_usd"] = ledger.get("cost_usd")
        report["cost_status"] = status
        report["cost_source"] = _COST_SOURCES.get(status)
        for key in _TOKEN_KEYS:
            report[key] = ledger.get(key)
        report["tokens_status"] = "reported" if ledger.get("total_tokens") else "unknown"
    report.update(
        {
            "api_calls": outcome.api_calls,
            "model": outcome.model,
            "provider": outcome.provider,
            "session_id": outcome.session_id,
            "completed": outcome.completed,
            "failed": outcome.failed,
            "partial": outcome.partial,
            "interrupted": outcome.interrupted,
            "exit_reason": outcome.exit_reason.value if outcome.exit_reason else None,
        }
    )
    reason = failure or outcome.failure
    if reason:
        report["failure"] = reason
    return report


def write_usage_file(path: str | None, outcome: RunOutcome, failure: str | None = None) -> bool:
    """Write the report to ``path``; any problem is logged and False returned, never raised."""
    if not path:
        return False
    try:
        from indus.foundation import fsio

        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        fsio.atomic_write_json(target, usage_report(outcome, failure))
        return True
    except Exception:
        log.warning("could not write the usage report to %s", path, exc_info=True)
        return False


# -- running one turn ------------------------------------------------------------------------------


def _scrub(text: str) -> str:
    """Lone surrogates cannot be encoded on a UTF-8 stdout; replace them."""
    return text.encode("utf-8", "replace").decode("utf-8") if text else text


async def _one_turn(spec: Any, text: str, session_id: str | None, outcome: RunOutcome) -> None:
    from indus.cli.runtime import build_service, shutdown_terminal
    from indus.protocol.methods import SessionCreateParams

    service = build_service(spec)
    loop = asyncio.get_running_loop()
    hooked = False
    try:
        if session_id is None:
            cwd = spec.policy.cwd
            created = await service.create(SessionCreateParams(cwd=cwd))
            session_id = created.session_id
        else:
            await service.resume(session_id)
        outcome.session_id = session_id
        ticket = await service.submit(session_id, text)
        outcome.started = True
        sid = session_id

        def stop() -> None:
            outcome.interrupted = True
            service.interrupt(sid, ticket.generation)

        try:
            loop.add_signal_handler(signal.SIGINT, stop)
            hooked = True
        except (NotImplementedError, RuntimeError, ValueError):
            hooked = False
        result = await service.wait(session_id, ticket.turn_id)
        outcome.usage = _ledger(service, session_id)
        outcome.session_id = result.session_id or session_id  # a rotating compaction moves it
        outcome.exit_reason = result.exit_reason
        outcome.final_text = result.final_text
        outcome.api_calls = result.api_calls
        if result.exit_reason in _INTERRUPT_REASONS:
            outcome.interrupted = True
        elif result.failure is not None:
            outcome.failure = result.failure.reason
    finally:
        if hooked:
            with contextlib.suppress(Exception):
                loop.remove_signal_handler(signal.SIGINT)
        try:
            await service.aclose()
        finally:
            await shutdown_terminal()


def _ledger(service: Any, session_id: str) -> dict[str, Any] | None:
    """The session's usage numbers (None when the agent is gone)."""
    try:
        return service.agent(session_id).usage.snapshot().as_dict()
    except Exception:
        log.debug("no usage snapshot for %s", session_id, exc_info=True)
        return None


def _run(spec: Any, text: str, session_id: str | None) -> tuple[RunOutcome, str | None]:
    """Run the turn; returns the outcome and a failure message for errors outside the turn."""
    from indus.foundation.errors import IndusError
    from indus.protocol.methods import ProtocolError

    outcome = RunOutcome(model=spec.route.model, provider=spec.route.provider)
    try:
        asyncio.run(_one_turn(spec, text, session_id, outcome))
    except KeyboardInterrupt:
        outcome.interrupted = True
    except ProtocolError as exc:
        return outcome, exc.message
    except IndusError as exc:
        return outcome, exc.message
    except Exception as exc:
        log.error("the run failed", exc_info=True)
        return outcome, f"{type(exc).__name__}: {exc}"
    return outcome, None


@contextlib.contextmanager
def _silenced() -> Iterator[tuple[TextIO, TextIO]]:
    """stdout and stderr pointed at the null device; yields the real ones."""
    real_out, real_err = sys.stdout, sys.stderr
    with (
        open(os.devnull, "w", encoding="utf-8") as sink,
        contextlib.redirect_stdout(sink),
        contextlib.redirect_stderr(sink),
    ):
        yield real_out, real_err


def run_finite(spec: Any, text: str, session_id: str | None, *, quiet: bool = False) -> int:
    """A finite ``chat -q`` run: the reply on stdout, the session id on stderr. ``quiet`` is
    accepted for symmetry: nothing but the reply and the session line is printed either way."""
    from indus.cli import resume

    del quiet
    outcome, failure = _run(spec, text, session_id)
    if outcome.session_id:
        resume.write_breadcrumb(spec.scope, outcome.session_id, spec.policy.cwd)
    reply = _scrub(outcome.final_text or "")
    if reply:
        click.echo(reply.rstrip("\n"))
    if failure or outcome.failure:
        click.echo(f"indus chat: {failure or outcome.failure}", err=True)
    if outcome.session_id:
        click.echo(f"session_id: {outcome.session_id}", err=True)
    return finite_exit_code(outcome)


# -- -z --------------------------------------------------------------------------------------------


def _usage(message: str) -> int:
    click.echo(f"{_PREFIX}: {message}", err=True)
    return 2


def _valid_toolsets(scope: Any, names: list[str] | None) -> tuple[list[str] | None, str | None]:
    """``names`` without unknown entries (reported); ``all``/``*`` means every toolset."""
    if names is None:
        return None, None
    from indus.tools import toolsets
    from indus.tools.builtin import load_builtins
    from indus.tools.registry import ToolRegistry

    registry = ToolRegistry(clock=scope.clock)
    load_builtins(registry)
    if any(name in toolsets.EVERYTHING for name in names):
        return ["all"], None
    known = [n for n in names if toolsets.is_known(n, registry, scope)]
    unknown = [n for n in names if n not in known]
    if unknown:
        click.echo(f"{_PREFIX}: ignoring unknown toolsets: {', '.join(unknown)}", err=True)
    if not known:
        return None, "none of the --toolsets names is a known toolset"
    return known, None


class _OneShot:
    """One ``-z`` run: checks, session, turn, report."""

    def __init__(self, state: CliState) -> None:
        self.state = state
        self.args = state.globals
        self.outcome = RunOutcome(model=self.args.model, provider=self.args.provider)

    def fail(self, code: int, message: str) -> int:
        write_usage_file(self.args.usage_file, self.outcome, message)
        click.echo(f"{_PREFIX}: {message}", err=True)
        return code

    def run(self) -> int:
        from indus.cli.runtime import reasoning_problem

        args = self.args
        prompt = args.oneshot or ""
        if not prompt.strip():
            return self.fail(2, "the prompt is empty")
        model_env = self.state.scope.env.get("INDUS_INFERENCE_MODEL", "").strip()
        if args.provider and not (args.model or model_env):
            return self.fail(
                2,
                "--provider needs --model too (or INDUS_INFERENCE_MODEL); give both, or neither "
                "to use the configured model",
            )
        problem = reasoning_problem(args.reasoning)
        if problem is not None:
            return self.fail(2, problem)
        toolsets, problem = _valid_toolsets(self.state.scope, split_csv(args.toolsets))
        if problem is not None:
            return self.fail(2, problem)
        if toolsets is not None:
            self.args = replace(args, toolsets=",".join(toolsets))
        if self.args.in_dir is not None:
            from indus.cli.session_select import SelectionError, enter_in_dir

            try:
                enter_in_dir(self.args.in_dir)
            except SelectionError as exc:
                return self.fail(exc.code, exc.message)
        from indus.state import registry

        try:
            store = registry.acquire(self.state.scope)
        except Exception as exc:
            return self.fail(1, f"the session store could not be opened: {exc}")
        try:
            return self._with_store(store, prompt)
        finally:
            registry.release(store)

    def _with_store(self, store: Any, prompt: str) -> int:
        from indus.cli import resume, session_select
        from indus.cli.runtime import SOURCE_ONESHOT, ServiceSpec, resolve_route, scope_for
        from indus.foundation.errors import IndusError
        from indus.state import sessions

        args = self.args
        cwd = Path.cwd()
        try:
            session_id = session_select.select_session(store, args, cwd=cwd)
        except session_select.SelectionError as exc:
            return self.fail(exc.code, exc.message)
        policy, given = session_select.launch_from_args(args, ChatArgs(), cwd=cwd)
        if session_id is not None:
            row = sessions.get(store, session_id)
            stored = session_select.stored_launch(row)
            switched = resume.stored_reasoning(row)
            if stored is not None and switched is not None:
                stored = replace(stored, reasoning=switched)
            if stored is not None:
                conflicts = session_select.check_frozen(stored, given)
                if conflicts:
                    names = ", ".join(conflicts)
                    return self.fail(2, f"this session was started with other values for {names}")
                policy = session_select.replace_cwd(stored, cwd)
        run_scope = scope_for(self.state.scope, policy, auto_approve=True)
        try:
            route = resolve_route(run_scope, policy, self.state.overrides)
        except IndusError as exc:
            return self.fail(1, f"could not start: {exc.message}")
        self.outcome.model, self.outcome.provider = route.model, route.provider
        spec = ServiceSpec(
            scope=run_scope,
            route=route,
            policy=policy,
            store=store,
            source=SOURCE_ONESHOT,
            run_context="single_query",
            answerable_kinds=(),
            overrides=self.state.overrides,
            new_session=session_id is None,
        )
        with _silenced() as (real_out, real_err):
            result, failure = _run(spec, prompt, session_id)
        write_usage_file(args.usage_file, result, failure)
        if failure is not None and not result.started:
            real_err.write(f"{_PREFIX}: could not start: {failure}\n")
            real_err.flush()
            return 1
        reply = _scrub(result.final_text or "")
        if reply:
            # stdout carries only a model reply; when the turn failed, the text is the loop's
            # failure notice, so it belongs with the other diagnostics on stderr.
            if result.failure is not None:
                real_err.write(f"{_PREFIX}: {reply.rstrip()}\n")
            else:
                real_out.write(reply if reply.endswith("\n") else reply + "\n")
                real_out.flush()
        code = oneshot_exit_code(result)
        if code == 1:
            real_err.write(f"{_PREFIX}: the run finished without a reply.\n")
        elif code == 2 and (failure or result.failure):
            real_err.write(f"{_PREFIX}: {failure or result.failure}\n")
        real_err.flush()
        return code


def run_from_cli(state: CliState) -> int:
    """``indus -z PROMPT``; marks the process for a hard exit after cleanup."""
    state.hard_exit = True
    return _OneShot(state).run()
