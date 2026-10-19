"""Host wiring for scheduled jobs (P14-07, P14-08, P14-10, P14-11).

``indus.cron`` sits below the service layer, so what a cron run needs from above it is built
here and handed down:

- ``CronAgentRunner`` — the agent run of one job (``cron.runner.AgentRunner``): a fresh, hidden
  session (``source=cron``) over ``core.agent.Agent`` with the learning wiring (memory tools,
  skills index) but never the background review (``no_review``, R07.5); the run context ``cron``
  (approvals follow ``approvals.cron_mode``); the job's toolsets minus the denied tools; the job's
  workdir as the session's terminal cwd, project-context folder and project-skills folder
  (``project_trust.bind_project_cwd``, also around the whole run through ``RunEnv.around``);
  the chosen route, without the fallback chain when the job is pinned; the inactivity limit
  laid over ``agent.turn_liveness_s`` for this run only; every agent event a progress stamp.
  Whichever way the run ends, the turn is stopped, the agent closed within
  ``cron.cleanup_timeout_seconds`` and the session's terminal environment, kernels and browser
  released (their child processes die with them).
- ``skill_loader`` / ``skill_problem`` — P09's slash-invocation loading and the preflight skill
  check, through ``learning.skills``.
- ``CronHost`` — the one cron backend of this process (``ports.BACKEND_FACTORY``, set by
  ``install()`` when this module is imported): the job store, execution ledger, notepad and store
  health under the ticker, used by ``indus cron``, ``/cron`` and ``cronjob_manage``. Its ``env``
  is the ``RunEnv`` (``run_env``) a ticker runs on; ``tick_once`` / ``tick_main``, ``daemon`` /
  ``daemon_main`` and ``run_now`` are the entry points behind ``indus cron tick``, ``indus cron
  daemon`` and ``indus cron run``.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import signal
from collections.abc import Callable, Generator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from indus.core.agent import Agent, AgentOptions
from indus.core.events import EventSink
from indus.core.persist import StateStore, TranscriptStore
from indus.core.prompt.builder import BuildContext, PromptBuilder
from indus.core.prompt.lifecycle import Builder, PromptParts, SessionPrompts
from indus.core.turn_state import LoopOptions
from indus.cron import bookkeeping, delivery, incidents, monitor, ports, settings, targets
from indus.cron import ticker as ticker_mod
from indus.cron.classify import RunOutcome
from indus.cron.context import CronContext, script_rule
from indus.cron.due import collect_due, evaluate_all
from indus.cron.errors import AmbiguousJobRef, JobNotFound, NextRunError, SlotTaken
from indus.cron.grammar import OnceSchedule
from indus.cron.jobroute import RouteChoice
from indus.cron.ledger import ExecutionLedger, ProcessProbe
from indus.cron.models import CronJob
from indus.cron.models import JobSpec as CronJobSpec
from indus.cron.next_run import following_slot, next_after, plan_next
from indus.cron.output import bounded_async
from indus.cron.ports import (
    AttemptKind,
    CronPorts,
    DueSlot,
    JobSpec,
    MonitorCheck,
    NotepadPort,
    RunReport,
    StoreHealthView,
)
from indus.cron.preflight import McpState, PreflightProbes
from indus.cron.prompt import SKILLS_FRAME, SKILLS_INSTRUCTION, SkillLoader, SkillSection
from indus.cron.runner import AgentRunner, AgentRunRequest, AgentRunResult, RunEnv
from indus.cron.store import CronStore
from indus.cron.store_health import describe_error, probe_dir
from indus.cron.ticker import Ticker, TickReport
from indus.foundation import config as config_mod
from indus.foundation.config.schema import default_for
from indus.foundation.scope import ConfigSource, Scope, use_scope
from indus.learning import host as learning_host
from indus.learning.skills import project_trust
from indus.learning.skills.invoke import expand_skills
from indus.learning.skills.resolver import SkillError, load_catalog
from indus.protocol.events import EventBase, ExitReason
from indus.providers.auxiliary import AuxClient
from indus.providers.context_length import ListModels
from indus.providers.route import RuntimeRoute, resolve
from indus.service import code_host
from indus.state import registry as store_registry
from indus.state.session_lock import LockBusy, lock_dir_for, try_lock_session
from indus.tools import toolsets
from indus.tools.builtin import terminal
from indus.tools.registry import ToolRegistry

__all__ = [
    "CronAgentRunner",
    "CronHost",
    "daemon",
    "daemon_main",
    "install",
    "run_env",
    "run_now",
    "skill_loader",
    "skill_problem",
    "tick_main",
    "tick_once",
]

log = logging.getLogger(__name__)

PLATFORM = "cron"
SOURCE = "cron"
RUN_CONTEXT = "cron"
LIVENESS_KEY = "agent.turn_liveness_s"
TERMINAL_CWD_KEY = "terminal.cwd"
_DONE_REASONS = frozenset({ExitReason.COMPLETED, ExitReason.MAX_ITERATIONS})

type RouteResolver = Callable[[Scope, RouteChoice], RuntimeRoute]
type ClientFactory = Callable[[RuntimeRoute], Any]


# -- one run's config view ------------------------------------------------------------------------


class _Overlay:
    """A config view with a few dotted keys replaced (this run only)."""

    def __init__(self, base: ConfigSource | None, values: Mapping[str, object]) -> None:
        self._base = base
        self._values = dict(values)

    def get(self, path: str, default: Any = None, /) -> Any:
        if path in self._values:
            return self._values[path]
        if self._base is None:
            found = default_for(path)
        else:
            try:
                found = self._base.get(path)
            except Exception:
                found = None
        return default if found is None else found

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)


def _run_scope(scope: Scope, request: AgentRunRequest) -> Scope:
    base = scope.config if scope.config is not None else config_mod.load(scope)
    values: dict[str, object] = {LIVENESS_KEY: request.inactivity_s or 0}
    if request.workdir is not None:
        values[TERMINAL_CWD_KEY] = str(request.workdir)
    return scope.with_config(_Overlay(base, values))


# -- the prompt builder ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _CronBuilder:
    """The P04 builder with the cron surface rules: the persona always, project context files
    only from a job's workdir."""

    inner: Builder
    workdir: Path | None

    async def build(self, ctx: BuildContext) -> PromptParts:
        fixed = replace(
            ctx,
            context_files=self.workdir is not None,
            persona_without_context_files=True,
            cwd=self.workdir if self.workdir is not None else ctx.cwd,
        )
        return await self.inner.build(fixed)


# -- the agent run --------------------------------------------------------------------------------


def _default_route(scope: Scope, choice: RouteChoice) -> RuntimeRoute:
    return resolve(scope, model=choice.model, provider=choice.provider, base_url=choice.base_url)


class CronAgentRunner:
    """Runs one scheduled job's agent turn (see the module notes)."""

    def __init__(
        self,
        scope: Scope,
        *,
        route: RouteResolver | None = None,
        client_factory: ClientFactory | None = None,
        list_models: ListModels | None = None,
        store: TranscriptStore | None = None,
        register_tools: Sequence[Callable[[ToolRegistry], object]] = (),
        loop_values: Mapping[str, Any] | None = None,
    ) -> None:
        self.scope = scope
        self._register_tools = tuple(register_tools)
        self._loop_values = dict(loop_values or {})
        self._route = route or _default_route
        self._client_factory = client_factory
        self._list_models = list_models
        self._store = store
        self.last_agent: Agent | None = None

    async def __call__(self, request: AgentRunRequest) -> AgentRunResult:
        scope = _run_scope(self.scope, request)
        # Project skills (P13-28) are read from the job's workdir for this run only.
        with use_scope(scope), project_trust.bind_project_cwd(request.workdir):
            return await self._run(scope, request)

    async def _run(self, scope: Scope, request: AgentRunRequest) -> AgentRunResult:
        route = self._route(scope, request.route)
        handle = None
        store = self._store
        if store is None:
            handle = store_registry.acquire(scope)
            store = StateStore(handle)
        lock = None
        if isinstance(store, StateStore):
            taken = try_lock_session(
                lock_dir_for(store.handle.path), request.session_id, owner=SOURCE
            )
            if isinstance(taken, LockBusy):
                raise RuntimeError(f"session {request.session_id} is busy: {taken.describe()}")
            lock = taken
        agent: Agent | None = None
        try:
            agent = await self._start(scope, route, store, request)
            self.last_agent = agent
            return await self._turn(agent, request)
        finally:
            await self._end(scope, agent, request)
            if lock is not None:
                lock.release()
            if handle is not None:
                store_registry.release(handle)

    async def _start(
        self,
        scope: Scope,
        route: RuntimeRoute,
        store: TranscriptStore,
        request: AgentRunRequest,
    ) -> Agent:
        registry = learning_host.tool_registry(scope.clock)
        for register in self._register_tools:
            register(registry)  # more tools (the scheduling tool, C3; test tools)
        chosen = toolsets.select(
            request.toolsets, registry=registry, scope=scope, platform=PLATFORM
        )
        allowed = frozenset(chosen.tools - request.denied_tools)
        base = _CronBuilder(PromptBuilder(), request.workdir)
        prompt_store = SessionPrompts(store.handle) if isinstance(store, StateStore) else None
        learning = learning_host.wire_session(
            scope,
            allowed,
            session_id=request.session_id,
            prompt_store=prompt_store,
            registry=registry,
            inner=base,
        )
        loop_values: dict[str, Any] = {**self._loop_values, "platform": PLATFORM}
        if request.route.reasoning_effort:
            loop_values["reasoning_effort"] = request.route.reasoning_effort.strip().lower()
        fields: dict[str, Any] = {"model": route.model, "hidden": True}
        if request.workdir is not None:
            fields["cwd"] = str(request.workdir)
        options = AgentOptions(
            session_id=request.session_id,
            platform=PLATFORM,
            source=SOURCE,
            store=store,
            prompt_store=prompt_store,
            builder=learning.builder,
            subagent_builder=base,
            registry=registry,
            client_factory=self._client_factory,
            list_models=self._list_models,
            loop=LoopOptions.from_scope(scope, **loop_values),
            cwd=request.workdir,
            inline=learning.inline,
            note_sources=learning.note_sources,
            session_fields=fields,
            only_tools=allowed,
            aux=AuxClient(scope, route, client_factory=self._client_factory),
        )
        sink = EventSink(request.session_id)
        progress = request.on_progress
        if progress is not None:

            def stamp(_event: EventBase) -> None:
                try:
                    progress()
                except Exception:
                    log.debug("progress stamp failed", exc_info=True)

            sink.subscribe(stamp)
        agent = await Agent.start(scope, route, request.toolsets, options, sink)
        agent.runtime.environments = terminal.runtime().environments
        agent.runtime.run_context = RUN_CONTEXT
        learning.bind_aux(agent.aux)
        learning.attach_review(agent, no_review=True)
        if request.route.pinned:
            # A pinned job fails rather than switch model: no fallback chain (pool rotation stays).
            agent.runtime.fallback = None
            agent.fallback = None
            agent.caller.eager = None
        return agent

    async def _turn(self, agent: Agent, request: AgentRunRequest) -> AgentRunResult:
        stopper: asyncio.Task[None] | None = None
        cancel = request.cancel
        if cancel is not None:

            async def stop_when_cancelled() -> None:
                await cancel.wait()
                running = agent.current
                if running is not None:
                    running.state.control.interrupt(running.generation, by_system=True)

            stopper = asyncio.ensure_future(stop_when_cancelled())
        try:
            result = await agent.run_turn(request.prompt)
        finally:
            if stopper is not None:
                stopper.cancel()
        failure = result.failure
        completed = result.exit_reason in _DONE_REASONS and (
            result.exit_reason is ExitReason.COMPLETED or bool(result.final_text)
        )
        return AgentRunResult(
            final_text=result.final_text,
            completed=completed,
            exit_reason=str(result.exit_reason),
            failure_reason=failure.reason if failure is not None else None,
            detail=failure.detail if failure is not None else "",
            api_calls=result.api_calls,
            retry_after=failure.resets_at if failure is not None else None,
        )

    async def _end(self, scope: Scope, agent: Agent | None, request: AgentRunRequest) -> None:
        seconds = settings.cleanup_timeout(scope)
        if agent is not None:
            await bounded_async(
                agent.close(), seconds=seconds, clock=scope.clock, label="cron agent close"
            )
        await bounded_async(
            terminal.runtime().environments.release(scope, session_id=request.session_id),
            seconds=seconds,
            clock=scope.clock,
            label="cron terminal release",
        )
        await bounded_async(
            code_host.end_session(request.session_id),
            seconds=seconds,
            clock=scope.clock,
            label="cron kernels and browser",
        )


# -- skills ---------------------------------------------------------------------------------------


def skill_loader(scope: Scope) -> SkillLoader:
    """P09's slash-invocation loading (blocks, support files, config, usage) for cron prompts."""

    def load(names: Sequence[str], session_id: str | None) -> SkillSection | None:
        expansion = expand_skills(
            list(names),
            SKILLS_INSTRUCTION,
            scope=scope,
            session_id=session_id,
            frame=SKILLS_FRAME,
            instruction_first=True,
        )
        if expansion is None:
            return None
        return SkillSection(expansion.text, expansion.loaded, expansion.missing)

    return load


def _has_value(scope: Scope, name: str) -> bool:
    if scope.env.get(name):
        return True
    secrets = scope.secrets
    if secrets is None:
        return False
    try:
        return bool(secrets.get(name))
    except Exception:
        return False


def skill_problem(scope: Scope) -> Callable[[Sequence[str]], str | None]:
    """The preflight skill check: every attached skill resolves and its declared environment
    variables have values (scope env or secret store)."""

    def check(names: Sequence[str]) -> str | None:
        catalog = load_catalog(scope)
        for name in names:
            try:
                record = catalog.resolve(name)
            except SkillError as exc:
                return f"attached skill {name!r} cannot be used: {exc}"
            front = record.frontmatter
            declared = front.required_environment_variables if front is not None else []
            missing = [v.name for v in declared if not _has_value(scope, v.name)]
            if missing:
                return (
                    f"attached skill {name!r} needs {', '.join(missing)}, which is not set for "
                    f"profile {scope.profile_id!r} ({scope.home}); store it with "
                    f"`indus config set {missing[0]} <value>` or detach the skill"
                )
        return None

    return check


# -- the job store, ledger and notepad as the ports' shapes ---------------------------------------


class _JobStore:
    """``JobStorePort`` over the job store (every change is one locked write)."""

    def __init__(self, store: CronStore) -> None:
        self.store = store

    def load(self) -> list[Any]:
        return list(self.store.load())

    def find(self, ref: str) -> Any | None:
        try:
            return self.store.resolve(ref)
        except (JobNotFound, AmbiguousJobRef):
            return None

    def edit[T](self, job_id: str, change: Callable[[Any], T]) -> T | None:
        with self.store.transaction() as jobs:
            for index, job in enumerate(jobs):
                if job.id != job_id:
                    continue
                work = job.model_copy(deep=True)
                result = change(work)
                jobs[index] = CronJob.model_validate(work.model_dump(mode="python"))
                return result
        return None


class _Ledger:
    """``LedgerPort`` over the execution ledger (its clock stamps every row)."""

    def __init__(self, scope: Scope, ledger: ExecutionLedger) -> None:
        self.scope = scope
        self.ledger = ledger

    def reap(self, now: datetime) -> int:
        inactivity = settings.inactivity_timeout(self.scope) or 0.0
        found = self.ledger.reap(
            inactivity_timeout_s=inactivity, script_timeout_s=settings.script_timeout(self.scope)
        )
        return len(found)

    def begin(
        self, job_id: str, scheduled_for: datetime | None, kind: AttemptKind, now: datetime
    ) -> str | None:
        try:
            return self.ledger.claim(job_id, scheduled_for=scheduled_for, kind=kind).attempt_id
        except SlotTaken:
            return None

    def running(self, attempt_id: str, now: datetime) -> None:
        self.ledger.start(attempt_id)

    def heartbeat(self, attempt_id: str, now: datetime) -> None:
        self.ledger.heartbeat(attempt_id)

    def finish(
        self,
        attempt_id: str,
        *,
        state: Literal["completed", "failed"],
        result_status: str,
        now: datetime,
        error: str | None = None,
    ) -> None:
        self.ledger.finish(
            attempt_id, ok=state == "completed", result_status=result_status, error=error
        )

    def note_delivery(self, attempt_id: str, status: str) -> None:
        self.ledger.note_delivery(attempt_id, status)


class _Notes:
    """``NotepadPort`` of one job; each call opens the profile's ledger database briefly."""

    def __init__(self, host: CronHost, job_id: str) -> None:
        self.host = host
        self.job_id = job_id

    def get(self, key: str) -> str | None:
        with self.host.opened() as ctx:
            return ctx.notepad.get(self.job_id, key)

    def set(self, key: str, value: str) -> None:
        with self.host.opened() as ctx:
            ctx.notepad.set(self.job_id, key, value)

    def unset(self, key: str) -> bool:
        with self.host.opened() as ctx:
            return ctx.notepad.unset(self.job_id, key)

    def items(self) -> list[tuple[str, str]]:
        with self.host.opened() as ctx:
            return [(entry.key, entry.value) for entry in ctx.notepad.list(self.job_id)]


def _changed(job: CronJob, changes: Mapping[str, Any]) -> CronJob:
    data = job.model_dump(mode="python")
    data.update(changes)
    return CronJob.model_validate(data)


def _mcp_probe(scope: Scope) -> Callable[[str], McpState | None]:
    """``PreflightProbes.mcp`` over the MCP host's status (P13): tools listed → ``ok``; still
    connecting or reconnecting → ``reconnecting`` (never blocks, R07.16); otherwise ``empty``.
    No MCP host in this process: the check is skipped."""

    def probe(name: str) -> McpState | None:
        from indus.mcp.discovery import host_for

        host = host_for(scope)
        if host is None:
            return None
        report = next((r for r in host.status() if r.name == name), None)
        if report is None:
            return None
        if report.tools > 0:
            return "ok"
        if report.state in _MCP_PENDING:
            return "reconnecting"
        return "empty"

    return probe


_MCP_PENDING = frozenset({"connecting", "reconnecting", "recycled", "lazy"})
_REPORT_STATUS = {"blocked_config": "blocked"}
_EXCERPT_CHARS = 2000


def _excerpt(text: str) -> str:
    text = (text or "").strip()
    return text if len(text) <= _EXCERPT_CHARS else text[:_EXCERPT_CHARS].rstrip() + " [...]"


class CronHost:
    """The ``CronBackend`` of one profile (``ports.BACKEND_FACTORY``): the job store, execution
    ledger, notepad and store health (``cron.context``) under the ticker and the runner.

    The ledger database is opened per use (``opened``) and closed after it, so a long-lived
    process holds no connection between calls. ``env(ctx)`` is the ``RunEnv`` a ticker runs on;
    its ``finish`` closes a run: monitor state is kept by the gate, then for a failure the incident
    decides whether to alert (``incidents.decide_failure_alert``) → delivery (``cron.delivery``)
    → bookkeeping inside one locked store write (``bookkeeping.settle_run`` with the store's
    next-run planner) → a success resolves the job's open incidents.
    """

    def __init__(
        self,
        scope: Scope,
        *,
        agent: AgentRunner | None = None,
        procs: ProcessProbe | None = None,
        probes: PreflightProbes | None = None,
    ) -> None:
        self.scope = scope
        self.store = CronStore(scope, script_check=script_rule)
        self._agent = agent
        self._procs = procs
        self._probes = probes

    # -- contexts and run environments ----------------------------------------------------------

    @contextlib.contextmanager
    def opened(self) -> Generator[CronContext]:
        ctx = CronContext.open(self.scope, procs=self._procs)
        try:
            yield ctx
        finally:
            ctx.close()

    def ports(self, ctx: CronContext) -> CronPorts:
        scope = self.scope

        def due(now: datetime) -> list[DueSlot]:
            jobs = ctx.store.load()
            decisions = evaluate_all(
                jobs,
                now=now,
                tz=ctx.store.tz,
                slots=ctx.ledger,
                catch_up_missed=settings.config_value(scope, "cron.catch_up_missed") is not False,
                outage_since=ctx.health.outage_since,
            )
            for decision in decisions:
                if decision.note:
                    log.info("cron job %s: %s", decision.job_id, decision.note)
                if decision.fire or not decision.updates:
                    continue
                try:
                    ctx.store.patch(decision.job_id, **decision.updates)
                except Exception:
                    log.warning("cron job %s: repair not saved", decision.job_id, exc_info=True)
            by_id = {job.id: job for job in jobs}
            return [
                DueSlot(by_id[d.job_id], d.scheduled_for, d.lateness or "on_time", d.updates)
                for d in collect_due(decisions)
            ]

        def next_run(record: Any, now: datetime) -> datetime | None:
            if not isinstance(record, CronJob) or not record.recurring:
                return None
            schedule = record.schedule
            if isinstance(schedule, OnceSchedule):
                return None
            try:
                return following_slot(schedule, record.next_run_at or now, now=now, tz=ctx.store.tz)
            except NextRunError:
                return None

        async def monitor_gate(job: JobSpec) -> MonitorCheck:
            record = ctx.store.get(job.id) or job
            found = await monitor.check_monitor(scope, record)
            if found.status == "no_change":
                return MonitorCheck("no_change")
            if found.status == "error":
                return MonitorCheck("error", error=found.error or "the monitor source failed")
            if found.state is not None:
                try:
                    ctx.store.patch(job.id, monitor_state=found.state)
                except Exception:
                    log.warning("cron job %s: monitor state not saved", job.id, exc_info=True)
            return MonitorCheck("changed", block=found.block or "")

        async def finish(job: JobSpec, outcome: RunOutcome) -> str | None:
            return await self._finish(ctx, job, outcome)

        return CronPorts(
            store=_JobStore(ctx.store),
            ledger=_Ledger(scope, ctx.ledger),
            store_writable=lambda _now: ctx.health.gate(),
            due=due,
            next_run=next_run,
            notepad=ctx.notepad.render_block,
            monitor=monitor_gate,
            finish=finish,
            deliverers=lambda: frozenset({targets.LOCAL, *delivery.registered_platforms()}),
        )

    def env(self, ctx: CronContext) -> RunEnv:
        return run_env(self.scope, self.ports(ctx), agent=self._agent, probes=self._probes)

    # -- after a run ------------------------------------------------------------------------------

    async def _finish(self, ctx: CronContext, job: JobSpec, outcome: RunOutcome) -> str | None:
        scope = self.scope
        now = scope.clock.now()
        record = None if outcome.job_removed else ctx.store.get(job.id)
        target: Any = record if record is not None else job
        book = self._book_outcome(outcome, now)
        rules = bookkeeping.Settings.from_scope(scope)
        preview = (
            self._settle(ctx, record, outcome, book, rules, now) if record is not None else None
        )
        text: str | None = None
        if outcome.failed:
            held = preview is not None and not preview.notify
            if outcome.deliver and not held:
                decision = incidents.decide_failure_alert(
                    scope,
                    job.id,
                    outcome.error or outcome.message or outcome.status,
                    output_path=str(outcome.document) if outcome.document else None,
                )
                if decision.alert:
                    extra = ""
                    if preview is not None:
                        extra = preview.nudge + bookkeeping.hold_notice(preview.held_until)
                    text = (outcome.message or outcome.error or "the run failed") + extra
        elif outcome.deliver:
            text = outcome.message
        result: delivery.DeliveryResult | None = None
        if text and text.strip():
            result = await delivery.deliver(
                scope, target, text, for_failure=outcome.failed, output_path=outcome.document
            )
        if record is not None:
            final = replace(book, delivery=result)
            with ctx.store.transaction() as jobs:
                for index, current in enumerate(jobs):
                    if current.id != job.id:
                        continue
                    settled = self._settle(ctx, current, outcome, final, rules, now)
                    changes = dict(settled.changes)
                    changes.pop("fire_claim", None)  # the ticker frees its own claim, no other
                    jobs[index] = _changed(current, changes)
                    break
        if not outcome.failed:
            incidents.resolve_after_success(scope, job.id)
        return result.status if result is not None else None

    def _settle(
        self,
        ctx: CronContext,
        record: CronJob,
        outcome: RunOutcome,
        book: bookkeeping.RunOutcome,
        rules: bookkeeping.Settings,
        now: datetime,
    ) -> bookkeeping.Settlement:
        tz = ctx.store.tz
        slot = outcome.scheduled_for if outcome.kind != "manual" else None

        def plan(changes: dict[str, Any]) -> datetime | None:
            # A manual run of a recurring job leaves its schedule where it was.
            if outcome.kind == "manual" and record.recurring and record.next_run_at is not None:
                return record.next_run_at
            # C1's planner, after last_run_at / repeat are set: cron steps from the slot.
            planned = plan_next(_changed(record, changes), now=now, tz=tz, slot=slot)
            return planned.next_run_at if planned.state == "scheduled" else None

        def step(schedule: Any, base: datetime) -> datetime | None:
            if isinstance(schedule, OnceSchedule):
                return None
            return next_after(schedule, base, tz)

        return bookkeeping.settle_run(
            record, book, now=now, settings=rules, next_run=step, plan=plan
        )

    def _book_outcome(self, outcome: RunOutcome, now: datetime) -> bookkeeping.RunOutcome:
        hold: float | None = None
        if (
            outcome.failed
            and outcome.retry_after is not None
            and outcome.failure_reason in ("rate_limit", "billing")
            and outcome.api_calls == 0
        ):
            seconds = (outcome.retry_after - now).total_seconds()
            hold = seconds if seconds > 0 else None
        return bookkeeping.RunOutcome(
            succeeded=not outcome.failed,
            status=outcome.status,
            error=outcome.error,
            model_calls=outcome.api_calls,
            model_unreachable=outcome.unreachable and outcome.api_calls == 0,
            quota_hold_s=hold,
            retry_run=outcome.kind == "retry",
            manual=outcome.kind == "manual",
        )

    # -- the surfaces' backend (ports.CronBackend) ------------------------------------------------

    def list_jobs(self, *, include_disabled: bool = True) -> list[Any]:
        jobs = self.store.load()
        return list(jobs) if include_disabled else [j for j in jobs if j.enabled]

    def resolve(self, ref: str) -> Any:
        try:
            return self.store.resolve(ref)
        except AmbiguousJobRef as exc:
            raise ports.JobAmbiguous(ref, exc.candidates) from None
        except JobNotFound:
            raise ports.JobNotFound(ref) from None

    def get(self, job_id: str) -> Any | None:
        return self.store.get(job_id)

    def create(self, spec: Mapping[str, Any]) -> Any:
        fields = {k: v for k, v in spec.items() if v is not None}
        try:
            request = CronJobSpec.model_validate(fields)
        except ValidationError as exc:
            raise ValueError(_invalid(exc)) from None
        return self.store.create(request)

    def update(self, job_id: str, changes: Mapping[str, Any]) -> Any:
        try:
            return self.store.update(job_id, **dict(changes))
        except ValidationError as exc:
            raise ValueError(_invalid(exc)) from None

    def pause(self, job_id: str, reason: str | None = None) -> Any:
        return self.store.pause(job_id, reason)

    def resume(self, job_id: str) -> Any:
        return self.store.resume(job_id)

    def remove(self, job_id: str) -> bool:
        with self.opened() as ctx:  # the notepad goes with the job
            try:
                ctx.store.remove(job_id)
            except JobNotFound:
                return False
        return True

    def is_running(self, job_id: str) -> bool:
        if ticker_mod.in_flight(self.scope.home, job_id):
            return True
        record = self.store.get(job_id)
        return record is not None and ticker_mod.claim_is_live(
            record.fire_claim, self.scope.clock.now()
        )

    async def run_now(self, job_id: str, *, context: str | None = None) -> RunReport:
        with self.opened() as ctx:
            try:
                outcome = await run_now(self.scope, job_id, run_context=context, env=self.env(ctx))
            except ticker_mod.JobNotFound as exc:
                return RunReport(job_id, "failed", error=str(exc))
            except ticker_mod.AlreadyRunning as exc:
                return RunReport(job_id, "skipped", error=str(exc))
            latest = ctx.ledger.history(job_id, 1)
            delivered = latest[0].delivery_status if latest else None
        return RunReport(
            job_id,
            _REPORT_STATUS.get(outcome.status, outcome.status),
            delivery=delivered,
            output_path=outcome.document,
            error=outcome.error,
            excerpt=_excerpt(outcome.message),
        )

    def runs(self, job_id: str | None = None, *, limit: int = 20) -> list[Mapping[str, Any]]:
        with self.opened() as ctx:
            found = (
                ctx.ledger.history(job_id, limit)
                if job_id is not None
                else ctx.ledger.recent(limit)
            )
        rows: list[Mapping[str, Any]] = []
        for attempt in found:
            row = dataclasses.asdict(attempt)
            rows.append(
                {k: (ports.iso(v) if isinstance(v, datetime) else v) for k, v in row.items()}
            )
        return rows

    def store_health(self) -> StoreHealthView:
        try:
            self.store.ensure_dirs()
        except OSError as exc:
            return StoreHealthView(False, describe_error(exc))
        problem = probe_dir(self.store.paths.cron_dir)
        if problem is None:
            return StoreHealthView(True)
        return StoreHealthView(False, describe_error(problem))

    def global_pause(self) -> tuple[bool, str | None]:
        state = ticker_mod.global_pause(self.scope.home)
        return (False, None) if state is None else (True, state.reason)

    def set_global_pause(self, paused: bool, reason: str | None = None) -> None:
        if paused:
            ticker_mod.pause_all(self.scope.home, reason or "", self.scope.clock.now())
        else:
            ticker_mod.resume_all(self.scope.home)

    def notepad(self, job_id: str) -> NotepadPort:
        return _Notes(self, job_id)

    def tick(self, out: Callable[[str], object] | None = None) -> int:
        with self.opened() as ctx:
            report = tick_main(self.scope, self.env(ctx))
        if out is not None:
            if report.message:
                out(report.message)
            for job_id, outcome in report.outcomes.items():
                out(f"{job_id} {outcome.status}")
        return report.exit_code

    def daemon(self) -> int:
        with self.opened() as ctx:
            return daemon_main(self.scope, self.env(ctx))


def _invalid(exc: ValidationError) -> str:
    parts = [
        f"{'.'.join(str(p) for p in error.get('loc', ())) or 'job'}: {error.get('msg', 'invalid')}"
        for error in exc.errors(include_url=False, include_input=False)
    ]
    return "invalid job: " + "; ".join(parts)


# -- entry points ---------------------------------------------------------------------------------


def run_env(
    scope: Scope,
    ports_: CronPorts,
    *,
    agent: AgentRunner | None = None,
    probes: PreflightProbes | None = None,
) -> RunEnv:
    """The ``RunEnv`` of one ticker over ``ports_`` (``CronHost.env`` builds the real one)."""
    return RunEnv(
        scope=scope,
        around=_project_of,
        ports=ports_,
        agent=agent if agent is not None else CronAgentRunner(scope),
        skills=skill_loader(scope),
        probes=probes
        if probes is not None
        else PreflightProbes(
            skills=skill_problem(scope), mcp=_mcp_probe(scope), deliverers=ports_.deliverers
        ),
    )


def _project_of(job: JobSpec) -> contextlib.AbstractContextManager[object]:
    """Project skills (P13-28) for a whole run come from the job's workdir (none without one)."""
    return project_trust.bind_project_cwd(job.workdir)


async def _shutdown_terminal() -> None:
    try:
        await terminal.runtime().shutdown()
    except Exception:
        log.warning("terminal shutdown failed", exc_info=True)


async def tick_once(scope: Scope, env: RunEnv) -> TickReport:
    """One pass that waits for the runs it started (``indus cron tick``)."""
    with use_scope(scope):
        return await Ticker(env).tick(wait=True)


def tick_main(scope: Scope, env: RunEnv) -> TickReport:
    """``indus cron tick`` in a fresh event loop; the terminal runtime is shut down after."""

    async def main() -> TickReport:
        try:
            return await tick_once(scope, env)
        finally:
            await _shutdown_terminal()

    return asyncio.run(main())


async def daemon(scope: Scope, stop: asyncio.Event, env: RunEnv) -> None:
    """The 60 s loop until ``stop`` (``indus cron daemon``), then the 30 s drain."""
    with use_scope(scope):
        await Ticker(env).run_daemon(stop)


def daemon_main(scope: Scope, env: RunEnv) -> int:
    """``indus cron daemon``: runs until SIGINT/SIGTERM."""

    async def main() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(sig, stop.set)
        try:
            await daemon(scope, stop, env)
        finally:
            await _shutdown_terminal()

    asyncio.run(main())
    return 0


async def run_now(
    scope: Scope, ref: str, *, run_context: str | None = None, env: RunEnv
) -> RunOutcome:
    """``indus cron run <job>``: run it now, whatever its pause state (raises
    ``ticker.JobNotFound`` / ``ticker.AlreadyRunning``)."""
    with use_scope(scope):
        return await Ticker(env).run_now(ref, run_context=run_context)


def install() -> None:
    """Register ``CronHost`` as this process's cron backend (``ports.backend_for``); once."""
    if ports.BACKEND_FACTORY is None:
        ports.BACKEND_FACTORY = CronHost


install()
