"""One cron run from gates to outcome, and the decisions an agent run needs (P14-11).

``run_job`` is the pipeline: structural preflight → (script-only jobs: the script is the job) →
monitor gate (C3) → pre-run script and wake gate → preflight → prompt assembly and threat scan →
the agent run → classification (``classify``) → output document (``output``). It returns a
``RunOutcome`` and never raises for a job's own problems; ``settle`` then closes the ledger
attempt and hands the outcome to delivery and bookkeeping (C3, through ``CronPorts.finish``).

The agent run itself is the ``AgentRunner`` callable the host passes in
(``service.cron_host.CronAgentRunner``): cron sits below the service layer. The decisions it
applies live here so they are tested without a model:

- ``session_id_for``: ``cron_<job>_<YYYYmmdd_HHMMSS>`` (UTC).
- ``toolset_selection``: job ``enabled_toolsets`` (an empty list = no tools) > ``cron.toolsets``
  > the ``indus-cron`` bundle (plus the ``cronjob`` toolset under ``cron.allow_agent_scheduling``);
  an unreadable ``cron.toolsets`` raises ``ToolsetConfigError`` so the
  run fails instead of getting every tool.
- ``denied_tools``: ``clarify`` (and messaging tools) always; ``cronjob_manage`` unless
  ``cron.allow_agent_scheduling`` (R07.15).
- ``jobroute.route_choice``: pin > ``cron.model``/``cron.provider`` > main; pinned routes get no
  fallback chain.
- inactivity: ``settings.inactivity_timeout``.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import logging
import socket
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx

from indus.cron import output, preflight, settings
from indus.cron.classify import RunOutcome, RunStatus, classify_reply, is_silent
from indus.cron.jobroute import RouteChoice, route_choice
from indus.cron.ports import AttemptKind, CronPorts, JobSpec, MonitorCheck
from indus.cron.preflight import AlertBook, PreflightProbes
from indus.cron.prompt import PromptBlocked, SkillLoader, assemble
from indus.cron.script import ScriptResult, run_script
from indus.foundation.scope import Scope

__all__ = [
    "DEFAULT_BUNDLE",
    "DENIED_ALWAYS",
    "SCHEDULING_TOOL",
    "SCHEDULING_TOOLSET",
    "AgentRunRequest",
    "AgentRunResult",
    "AgentRunner",
    "RunEnv",
    "ToolsetConfigError",
    "denied_tools",
    "run_job",
    "session_id_for",
    "settle",
    "toolset_selection",
]

log = logging.getLogger(__name__)

SOURCE = "cron"
DEFAULT_BUNDLE = "indus-cron"
SCHEDULING_TOOL = "cronjob_manage"
SCHEDULING_TOOLSET = "cronjob"
# Interactive or messaging tools a scheduled run never gets (P15 adds its send tool here).
DENIED_ALWAYS: frozenset[str] = frozenset({"clarify"})
NETWORK_REASONS = frozenset({"connection", "timeout", "dns", "network"})
_NETWORK_ERRNOS = frozenset(
    {
        errno.ECONNREFUSED,
        errno.ECONNRESET,
        errno.EHOSTUNREACH,
        errno.ENETUNREACH,
        errno.ENETDOWN,
        errno.ETIMEDOUT,
    }
)


class ToolsetConfigError(RuntimeError):
    """``cron.toolsets`` cannot be read as a list of names."""


# -- decisions ------------------------------------------------------------------------------------


def session_id_for(job_id: str, now: datetime) -> str:
    return f"cron_{job_id}_{now.astimezone(UTC).strftime('%Y%m%d_%H%M%S')}"


def toolset_selection(job: JobSpec, scope: Scope) -> list[str]:
    if job.enabled_toolsets is not None:
        return list(job.enabled_toolsets)
    raw = settings.config_value(scope, "cron.toolsets")
    if raw is None:
        # The bundle has no scheduling tool; the opt-in adds it (R07.15).
        if settings.allow_agent_scheduling(scope):
            return [DEFAULT_BUNDLE, SCHEDULING_TOOLSET]
        return [DEFAULT_BUNDLE]
    if not isinstance(raw, list):
        raise ToolsetConfigError(
            "cron.toolsets must be a list of toolset names; this run was refused rather than "
            f"given every tool (found {type(raw).__name__})"
        )
    items = cast(list[object], raw)
    if not all(isinstance(v, str) for v in items):
        raise ToolsetConfigError(
            "cron.toolsets must list toolset names only; this run was refused rather than given "
            "every tool"
        )
    return [str(v) for v in items]


def denied_tools(scope: Scope) -> frozenset[str]:
    if settings.allow_agent_scheduling(scope):
        return DENIED_ALWAYS
    return DENIED_ALWAYS | {SCHEDULING_TOOL}


# -- the agent seam -------------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentRunRequest:
    job: JobSpec
    prompt: str
    session_id: str
    toolsets: list[str]
    denied_tools: frozenset[str]
    route: RouteChoice
    workdir: Path | None
    inactivity_s: float | None
    on_progress: Callable[[], None] | None = None
    cancel: asyncio.Event | None = None


@dataclass(frozen=True)
class AgentRunResult:
    """What the agent run gave back. ``completed`` false: the turn did not finish normally."""

    final_text: str | None
    completed: bool
    exit_reason: str = "completed"
    failure_reason: str | None = None
    detail: str = ""
    api_calls: int = 0
    retry_after: datetime | None = None


AgentRunner = Callable[[AgentRunRequest], Awaitable[AgentRunResult]]


@dataclass
class RunEnv:
    """What one run needs from its host."""

    scope: Scope
    ports: CronPorts
    agent: AgentRunner | None = None
    skills: SkillLoader | None = None
    probes: PreflightProbes = field(default_factory=PreflightProbes)
    # Context the host wraps around one whole run (e.g. project-skill trust bound to the job's
    # workdir, so preflight, skill loading and the agent all read the same project).
    around: Callable[[JobSpec], AbstractContextManager[object]] | None = None

    @property
    def home(self) -> Path:
        return self.scope.home


# -- the pipeline ---------------------------------------------------------------------------------


@dataclass
class _Run:
    env: RunEnv
    job: JobSpec
    kind: AttemptKind
    scheduled_for: datetime | None
    session_id: str | None = None
    prompt: str = ""

    def outcome(self, status: RunStatus, **values: Any) -> RunOutcome:
        return RunOutcome(
            status=status,
            kind=self.kind,
            scheduled_for=self.scheduled_for,
            session_id=self.session_id,
            **values,
        )

    async def document(
        self,
        status: str,
        *,
        response: str = "",
        error: str | None = None,
        exc: BaseException | None = None,
        prompt: str | None = None,
    ) -> Path | None:
        scope = self.env.scope
        sections: dict[str, str] = {"prompt": self.prompt if prompt is None else prompt}
        sections["response"] = response
        if error:
            sections["error"] = error
        if exc is not None:
            sections["traceback"] = output.format_failure(exc, scope=scope)
        try:
            path = output.write_document(
                scope.home,
                job_id=self.job.id,
                label=self.job.label,
                schedule=self.job.schedule_display,
                status=status,
                when=scope.clock.now(),
                sections=sections,
            )
        except OSError as err:
            log.error("job %s: the output document could not be written: %s", self.job.id, err)
            return None
        keep = settings.output_retention(scope)
        await output.bounded(
            lambda: output.prune(scope.home, self.job.id, keep),
            seconds=settings.cleanup_timeout(scope),
            clock=scope.clock,
            label=f"output pruning for {self.job.id}",
        )
        return path

    async def failed(
        self, message: str, *, exc: BaseException | None = None, **values: Any
    ) -> RunOutcome:
        path = await self.document("failed", error=message, exc=exc)
        return self.outcome(
            "failed", message=message, deliver=True, error=message, document=path, **values
        )


async def run_job(
    env: RunEnv,
    job: JobSpec,
    *,
    kind: AttemptKind = "scheduled",
    scheduled_for: datetime | None = None,
    run_context: str | None = None,
    cancel: asyncio.Event | None = None,
    on_progress: Callable[[], None] | None = None,
) -> RunOutcome:
    """Run ``job`` once (see the module notes); never raises for the job's own problems."""
    run = _Run(env, job, kind, scheduled_for)
    around = env.around(job) if env.around is not None else contextlib.nullcontext()
    try:
        with around:
            outcome = await _pipeline(
                run, run_context=run_context, cancel=cancel, progress=on_progress
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.exception("job %s: the run failed", job.id)
        outcome = await run.failed(f"{type(exc).__name__}: {exc}", exc=exc)
    if _job_gone(env.ports, job.id):  # the run removed its own job: still delivered (P14-11)
        outcome = replace(outcome, job_removed=True)
    return outcome


def _job_gone(ports: CronPorts, job_id: str) -> bool:
    try:
        return ports.store.find(job_id) is None
    except Exception:
        log.debug("could not look the job up after its run", exc_info=True)
        return False


async def _pipeline(
    run: _Run,
    *,
    run_context: str | None,
    cancel: asyncio.Event | None,
    progress: Callable[[], None] | None,
) -> RunOutcome:
    env, job, scope = run.env, run.job, run.env.scope
    checks = settings.preflight_enabled(scope)
    if checks:
        reason = preflight.structural(scope, job)
        if reason:
            return await _blocked(run, reason)
    if job.no_agent:
        return await _script_only(run, checks=checks, cancel=cancel)

    monitor_block = ""
    if job.has_monitor and env.ports.monitor is not None:
        check: MonitorCheck = await env.ports.monitor(job)
        if check.state == "no_change":
            path = await run.document("no_change", prompt="")
            return run.outcome("no_change", document=path)
        if check.state == "error":
            return await run.failed(f"the monitor source failed: {check.error}")
        monitor_block = check.block

    script_output: str | None = None
    wake_context: Mapping[str, Any] | None = None
    if job.script:
        result = await run_script(scope, job, cancel=cancel)
        if not result.ok:
            return await run.failed(f"the pre-run script failed: {result.failure_text}")
        if not result.wake or result.nothing_to_report:
            note = "wake: false" if not result.wake else "the script printed nothing"
            path = await run.document("skipped", response=note, prompt="")
            return run.outcome("skipped", document=path)
        script_output, wake_context = result.stdout, result.context

    if checks:
        reason = preflight.check(scope, job, env.probes)
        if reason:
            return await _blocked(run, reason)
        AlertBook(scope.home).clear(job.id)

    if env.agent is None:
        return await run.failed("no agent runner is wired into this process")
    run.session_id = session_id_for(job.id, scope.clock.now())
    try:
        assembled = assemble(
            job,
            home=scope.home,
            skills=env.skills,
            session_id=run.session_id,
            notepad=env.ports.notepad(job.id),
            script_output=script_output,
            wake_context=wake_context,
            monitor_block=monitor_block,
            run_context=run_context,
        )
    except PromptBlocked as exc:
        return await run.failed(str(exc))
    run.prompt = assembled.text
    request = AgentRunRequest(
        job=job,
        prompt=assembled.text,
        session_id=run.session_id,
        toolsets=toolset_selection(job, scope),
        denied_tools=denied_tools(scope),
        route=route_choice(job, scope),
        workdir=Path(job.workdir) if job.workdir else None,
        inactivity_s=settings.inactivity_timeout(scope),
        on_progress=progress,
        cancel=cancel,
    )
    try:
        result = await env.agent(request)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        unreachable = _network_error(exc)
        return await run.failed(
            f"{type(exc).__name__}: {exc}",
            exc=exc,
            unreachable=unreachable,
            failure_reason="connection" if unreachable else None,
        )
    return await _agent_outcome(run, result)


async def _agent_outcome(run: _Run, result: AgentRunResult) -> RunOutcome:
    if not result.completed:
        why = _incomplete_text(run, result)
        unreachable = (
            (result.failure_reason or "") in NETWORK_REASONS
            and not result.final_text
            and result.api_calls == 0
        )
        return await run.failed(
            why,
            unreachable=unreachable,
            retry_after=result.retry_after,
            failure_reason=result.failure_reason,
            api_calls=result.api_calls,
        )
    text = (result.final_text or "").strip()
    verdict = classify_reply(text)
    calls = result.api_calls
    if verdict.status == "failed":
        path = await run.document("failed", response=text, error=verdict.evidence)
        return run.outcome(
            "failed",
            message=text,
            deliver=True,
            error=verdict.evidence,
            document=path,
            api_calls=calls,
        )
    if verdict.status == "silent":
        path = await run.document("silent", response=text)
        return run.outcome("silent", message=text, document=path, api_calls=calls)
    path = await run.document("ok", response=text)
    return run.outcome("ok", message=text, deliver=bool(text), document=path, api_calls=calls)


def _incomplete_text(run: _Run, result: AgentRunResult) -> str:
    detail = f": {result.detail}" if result.detail else ""
    if result.exit_reason == "interrupted_by_system" and result.failure_reason in (
        None,
        "liveness",
    ):
        limit = settings.inactivity_timeout(run.env.scope)
        if limit is not None:
            return f"the run was stopped after {limit:g} s without progress{detail}"
    reason = result.failure_reason or result.exit_reason
    return f"the agent run did not finish ({reason}){detail}"


async def _blocked(run: _Run, reason: str) -> RunOutcome:
    first = AlertBook(run.env.home).should_alert(run.job.id, reason)
    path = await run.document("blocked_config", error=reason, prompt="")
    log.warning("job %s blocked by preflight (no model call): %s", run.job.id, reason)
    return run.outcome("blocked_config", message=reason, deliver=first, error=reason, document=path)


async def _script_only(run: _Run, *, checks: bool, cancel: asyncio.Event | None) -> RunOutcome:
    env, job = run.env, run.job
    if checks:
        reason = preflight.check(env.scope, job, env.probes)
        if reason:
            return await _blocked(run, reason)
        AlertBook(env.home).clear(job.id)
    run.prompt = f"script: {job.script}"
    result: ScriptResult = await run_script(env.scope, job, cancel=cancel)
    if not result.ok:
        return await run.failed(result.failure_text)
    text = result.stdout.strip()
    if not result.wake or not text:
        path = await run.document("silent", response=text)
        return run.outcome("silent", message=text, document=path)
    if is_silent(text):  # the failure token means nothing here; silence does
        path = await run.document("silent", response=text)
        return run.outcome("silent", message=text, document=path)
    path = await run.document("ok", response=text)
    return run.outcome("ok", message=text, deliver=True, document=path)


def _network_error(exc: BaseException) -> bool:
    """True for a connection-level failure (DNS, refused, unreachable, timeout to connect)."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, httpx.ConnectError | httpx.ConnectTimeout | socket.gaierror):
            return True
        if isinstance(current, OSError) and current.errno in _NETWORK_ERRNOS:
            return True
        current = current.__cause__ or current.__context__
    return False


# -- after the run --------------------------------------------------------------------------------


async def settle(env: RunEnv, job: JobSpec, outcome: RunOutcome, attempt_id: str | None) -> None:
    """Close the ledger attempt, hand the outcome to delivery and bookkeeping, and drop the
    output folder of a job that removed itself (after its result went out)."""
    scope = env.scope
    if attempt_id is not None:
        try:
            env.ports.ledger.finish(
                attempt_id,
                state="failed" if outcome.failed else "completed",
                result_status=outcome.status,
                now=scope.clock.now(),
                error=outcome.error,
            )
        except Exception:
            log.exception("job %s: the ledger attempt could not be closed", job.id)
    if env.ports.finish is not None:
        delivered: str | None = None
        try:
            delivered = await env.ports.finish(job, outcome)
        except Exception:
            log.exception("job %s: delivery or bookkeeping failed", job.id)
        if attempt_id is not None and delivered:
            try:
                env.ports.ledger.note_delivery(attempt_id, delivered)
            except Exception:
                log.debug("job %s: delivery status not recorded", job.id, exc_info=True)
    if outcome.job_removed:
        await output.bounded(
            lambda: output.remove_job_output(scope.home, job.id),
            seconds=settings.cleanup_timeout(scope),
            clock=scope.clock,
            label=f"output removal for {job.id}",
        )
