"""The per-prompt worker of a batch run (P22-07).

A pool process runs whole batches, one prompt after another. For each prompt not done yet:

1. a row naming a container image, with ``terminal.backend: docker``, has its image checked
   locally (10 s) and pulled when absent (600 s) **before any model call**; a failed pull is a
   failure result with the first 500 characters of the error. The per-task override (task id
   ``task_<index>``) then goes through ``register_image``; per-task backend overrides (P21-14) do
   not exist yet, so the default hook refuses and image rows fail without a model call;
2. its toolsets are sampled from the distribution with the prompt's own RNG (P22-02);
3. a fresh in-process agent runs one conversation (``SessionAgentRunner``): a ``SessionService``
   over a ``MemoryStore`` (nothing lands in the profile's session store), built-in tools only (no
   memory or learning tools), the prompt builder with context files and the persona off, the
   ephemeral system prompt as the session's system message, ``unattended`` approvals, and the
   model caller wrapped so prefill messages and provider routing are added **to each request
   only** (never stored, so never in the sample). The session is always closed, its terminal
   environment and kernels released;
4. tool and reasoning stats come from the raw messages (P22-04), then the sample (P22-03);
5. no reasoning on any assistant turn → a ``no_reasoning`` tombstone; else the row. Either is
   appended durably (P22-06) before the index is reported complete. Exceptions, and turns that
   ended without a usable answer (provider failure, interruption), write nothing: the next run
   tries the prompt again.

``process_batch`` is the pool entry; ``init_worker`` (the pool initializer) rebuilds the profile
scope in the process from ``WorkerSettings`` — credentials are never pickled as callables, each
process resolves them from its scope.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import subprocess
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC
from pathlib import Path
from typing import Any, Literal, Protocol

from indus.core.agent import Agent, AgentOptions
from indus.core.events import EventSink
from indus.core.model_call import ModelCaller, OnEvent, OnWait
from indus.core.persist import MemoryStore
from indus.core.prompt.builder import BuildContext, PromptBuilder
from indus.core.prompt.lifecycle import Builder, PromptParts
from indus.core.turn_state import LoopOptions
from indus.foundation.blocking import run_blocking
from indus.foundation.scope import Scope, make_scope, use_scope
from indus.protocol.events import ExitReason
from indus.providers.route import RuntimeRoute, resolve
from indus.providers.streaming import CallOutcome
from indus.providers.types import ChatRequest
from indus.research import distributions
from indus.research.batch_io import (
    BatchRow,
    DiscardReason,
    Tombstone,
    append_durable,
    batch_path,
)
from indus.research.dataset import DatasetRow
from indus.research.stats import (
    Reasoning,
    Tally,
    normalise,
    reasoning_counts,
    tally_dict,
    tool_tallies,
)
from indus.research.trajectory import convert
from indus.service.broker import ServerRequestBroker
from indus.service.session_service import SessionService
from indus.tools.builtin import load_builtins, terminal
from indus.tools.registry import ToolRegistry

__all__ = [
    "PLATFORM",
    "BatchJob",
    "BatchOutcome",
    "Conversation",
    "ImagePrep",
    "SessionAgentRunner",
    "WorkerSettings",
    "init_worker",
    "process_batch",
    "registered_tools",
    "run_batch",
    "worker_scope",
]

log = logging.getLogger(__name__)

PLATFORM = "batch"
RUN_CONTEXT = "unattended"
PROBE_TIMEOUT_S = 10.0
PULL_TIMEOUT_S = 600.0
ERROR_PREVIEW = 500
DOCKER_BACKEND = "docker"

COMPLETE_EXITS = frozenset({ExitReason.COMPLETED})
PARTIAL_EXITS = frozenset(
    {
        ExitReason.MAX_ITERATIONS,
        ExitReason.BUDGET_EXHAUSTED,
        ExitReason.INVALID_TOOL_STRIKES,
        ExitReason.GUARDRAIL_HALT,
    }
)
NO_IMAGE_OVERRIDES = (
    "per-task container images need per-task backend overrides (P21-14), which this build does "
    "not have yet"
)

type Out = Callable[[str], None]


@dataclass(frozen=True)
class WorkerSettings:
    """Everything a pool process needs; plain values only (it is pickled)."""

    home: str
    env: Mapping[str, str]
    run_dir: str
    run_name: str
    model: str | None = None
    provider: str | None = None
    base_url: str | None = None
    distribution: str = distributions.DEFAULT_DISTRIBUTION
    seed: int = 0
    max_iterations: int = 10
    reasoning_effort: str | None = None
    prefill: tuple[Mapping[str, Any], ...] = ()
    ephemeral_system_prompt: str | None = None
    routing: Mapping[str, Any] | None = None
    registered: tuple[str, ...] = ()
    valid_toolsets: tuple[str, ...] = ()
    verbose: bool = False


@dataclass(frozen=True)
class BatchJob:
    batch: int
    items: tuple[tuple[int, DatasetRow], ...]
    skip: frozenset[int] = frozenset()


@dataclass
class BatchOutcome:
    batch: int
    processed: int = 0
    skipped: int = 0
    discarded: int = 0
    failed: int = 0
    completed: list[int] = field(default_factory=list[int])
    tool_stats: dict[str, dict[str, int]] = field(default_factory=dict[str, dict[str, int]])
    reasoning: dict[str, int] = field(default_factory=dict[str, int])
    bad_arguments: int = 0
    stopped: bool = False


@dataclass(frozen=True)
class Conversation:
    """What one agent run left: its messages, the tool array it sent, how it ended."""

    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    exit_reason: str
    api_calls: int
    model: str
    detail: str = ""


class AgentRunner(Protocol):
    async def __call__(
        self, prompt: str, toolsets: Sequence[str], task_id: str
    ) -> Conversation: ...


def registered_tools(clock: Any = None) -> tuple[str, ...]:
    """The tool names a batch agent's registry holds (the built-ins)."""
    registry = ToolRegistry(clock=clock)
    load_builtins(registry)
    return tuple(registry.names())


# -- the request shaper (prefill and routing at request time) -------------------------------------


def shape_request(
    request: ChatRequest,
    prefill: Sequence[Mapping[str, Any]],
    routing: Mapping[str, Any] | None,
) -> ChatRequest:
    """``request`` with the prefill messages after its system message(s) and the provider
    routing in ``extra_body.provider``."""
    messages = list(request.messages)
    if prefill:
        at = 0
        while at < len(messages) and messages[at].get("role") == "system":
            at += 1
        messages[at:at] = [dict(m) for m in prefill]
    extra = dict(request.extra_body)
    if routing:
        current = extra.get("provider")
        merged = dict(current) if isinstance(current, Mapping) else {}
        merged.update(routing)
        extra["provider"] = merged
    return replace(request, messages=messages, extra_body=extra)


@dataclass
class RequestShaper:
    """A ``ModelCaller`` that shapes each request on its way out (nothing is stored)."""

    inner: ModelCaller
    prefill: tuple[Mapping[str, Any], ...] = ()
    routing: Mapping[str, Any] | None = None

    @property
    def model(self) -> str:
        return self.inner.model

    async def call(
        self, request: ChatRequest, *, on_event: OnEvent, on_wait: OnWait | None = None
    ) -> CallOutcome:
        shaped = shape_request(request, self.prefill, self.routing)
        return await self.inner.call(shaped, on_event=on_event, on_wait=on_wait)


# -- the prompt builder without context files ------------------------------------------------------


@dataclass(frozen=True)
class SampleBuilder:
    """The P04 builder with project context files and the persona off: neither may leak into
    training samples."""

    inner: Builder

    async def build(self, ctx: BuildContext) -> PromptParts:
        fixed = replace(ctx, context_files=False, persona_without_context_files=False)
        return await self.inner.build(fixed)


# -- one agent run ---------------------------------------------------------------------------------


class SessionAgentRunner:
    """Runs one prompt through a fresh session (see the module notes)."""

    def __init__(
        self,
        scope: Scope,
        settings: WorkerSettings,
        *,
        route: RuntimeRoute | None = None,
        client_factory: Callable[[RuntimeRoute], Any] | None = None,
        list_models: Any = None,
    ) -> None:
        self.scope = scope
        self.settings = settings
        self._route = route
        self._client_factory = client_factory
        self._list_models = list_models
        self.agents: list[Agent] = []

    def route(self) -> RuntimeRoute:
        if self._route is None:
            s = self.settings
            self._route = resolve(
                self.scope, model=s.model, provider=s.provider, base_url=s.base_url
            )
        return self._route

    def _options(self, route: RuntimeRoute, store: MemoryStore) -> AgentOptions:
        s = self.settings
        loop: dict[str, Any] = {"platform": PLATFORM, "max_iterations": max(1, s.max_iterations)}
        if s.reasoning_effort is not None:
            loop["reasoning_effort"] = s.reasoning_effort
        registry = ToolRegistry(clock=self.scope.clock)
        load_builtins(registry)
        return AgentOptions(
            platform=PLATFORM,
            source=PLATFORM,
            store=store,
            builder=SampleBuilder(PromptBuilder()),
            registry=registry,
            client_factory=self._client_factory,
            list_models=self._list_models,
            loop=LoopOptions.from_scope(self.scope, **loop),
            system_message=s.ephemeral_system_prompt or None,
            session_fields={"model": route.model, "hidden": True},
        )

    def _factory(self, route: RuntimeRoute) -> Any:
        settings = self.settings

        async def build(
            session_id: str,
            sink: EventSink,
            broker: ServerRequestBroker,
            options: AgentOptions,
            selection: Sequence[str] | None,
        ) -> Agent:
            full = replace(options, session_id=session_id, broker=broker)
            agent = await Agent.start(self.scope, route, selection, full, sink)
            self.agents.append(agent)
            agent.runtime.environments = terminal.runtime().environments
            agent.runtime.run_context = RUN_CONTEXT
            if settings.prefill or settings.routing:
                agent.runtime.caller = RequestShaper(
                    agent.runtime.caller, tuple(settings.prefill), settings.routing
                )
            return agent

        return build

    async def __call__(self, prompt: str, toolsets: Sequence[str], task_id: str) -> Conversation:
        route = self.route()
        store = MemoryStore()
        service = SessionService(
            self.scope,
            route,
            toolsets=list(toolsets),
            options=self._options(route, store),
            store=store,
            owner=PLATFORM,
            agent_factory=self._factory(route),
        )
        session_id: str | None = None
        try:
            session_id = (await service.create()).session_id
            ticket = await service.submit(session_id, prompt)
            result = await service.wait(session_id, ticket.turn_id)
            current = service.current_id(session_id)
            agent = service.agent(current)
            rows = await store.load(current)
            failure = result.failure
            return Conversation(
                messages=[row.to_message() for row in rows if not row.scaffold],
                tools=[dict(t) for t in agent.runtime.tools],
                exit_reason=str(result.exit_reason),
                api_calls=result.api_calls,
                model=agent.binding.state.model,
                detail=(failure.detail or failure.reason) if failure is not None else "",
            )
        finally:
            await _quietly(service.aclose(), "closing the batch session")
            if session_id is not None:
                await _quietly(
                    terminal.runtime().environments.release(self.scope, session_id=session_id),
                    "releasing the terminal environment",
                )


async def _quietly(work: Awaitable[Any], what: str) -> None:
    try:
        await work
    except Exception:
        log.warning("%s failed", what, exc_info=True)


# -- container images ------------------------------------------------------------------------------

type Command = Callable[[Sequence[str], float], Awaitable[tuple[int, str]]]
type ImageRegistrar = Callable[[str, DatasetRow], Awaitable[str | None]]


async def refuse_images(task_id: str, row: DatasetRow) -> str | None:
    return NO_IMAGE_OVERRIDES


def _command_runner(scope: Scope) -> Command:
    from indus.safety.child_env import child_env

    async def run(argv: Sequence[str], timeout: float) -> tuple[int, str]:
        def call() -> tuple[int, str]:
            try:
                done = subprocess.run(  # noqa: S603 - fixed argv, no shell
                    list(argv),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout,
                    env=child_env(scope),
                    check=False,
                )
            except FileNotFoundError:
                return 127, f"{argv[0]} not found"
            except subprocess.TimeoutExpired:
                return 124, f"{' '.join(argv[:2])} timed out after {timeout:.0f} s"
            return done.returncode, done.stderr or done.stdout or ""

        return await run_blocking(call, label="container image check")

    return run


@dataclass
class ImagePrep:
    """Gets a row's container image ready before any model call (see the module notes)."""

    scope: Scope
    command: Command | None = None
    register: ImageRegistrar = refuse_images

    def backend(self) -> str:
        config = self.scope.config
        value = config.get("terminal.backend") if config is not None else None
        return str(value or "local").strip().lower()

    async def __call__(self, row: DatasetRow, task_id: str) -> str | None:
        image = row.image
        if not image:
            return None
        if self.backend() == DOCKER_BACKEND:
            command = self.command or _command_runner(self.scope)
            code, _ = await command(["docker", "image", "inspect", image], PROBE_TIMEOUT_S)
            if code != 0:
                code, error = await command(["docker", "pull", image], PULL_TIMEOUT_S)
                if code != 0:
                    preview = error.strip()[:ERROR_PREVIEW]
                    return f"container image {image} is not available: {preview}"
        return await self.register(task_id, row)


# -- one prompt, one batch -------------------------------------------------------------------------

type PromptStatus = Literal["written", "discarded", "failed"]


@dataclass
class PromptResult:
    index: int
    status: PromptStatus
    error: str = ""
    tallies: dict[str, Tally] = field(default_factory=dict[str, Tally])
    reasoning: Reasoning = field(default_factory=Reasoning)
    bad_arguments: int = 0


async def _append(path: Path, record: Mapping[str, Any], fsync: Callable[[int], None]) -> None:
    await run_blocking(lambda: append_durable(path, record, fsync=fsync), label="batch row")


def _now_iso(scope: Scope) -> str:
    return scope.clock.now().astimezone(UTC).isoformat()


async def run_prompt(
    scope: Scope,
    settings: WorkerSettings,
    batch: int,
    index: int,
    row: DatasetRow,
    *,
    runner: AgentRunner,
    images: Callable[[DatasetRow, str], Awaitable[str | None]],
    sampler: distributions.Sampler,
    on_complete: Callable[[int], None],
    fsync: Callable[[int], None] = os.fsync,
) -> PromptResult:
    """One prompt end to end (see the module notes); never raises for a prompt's own failure."""
    task_id = f"task_{index}"
    problem = await images(row, task_id)
    if problem is not None:
        return PromptResult(index, "failed", problem)
    dist = distributions.get(settings.distribution)
    toolsets = sampler.sample(dist, distributions.prompt_rng(settings.seed, index))
    try:
        conversation = await runner(row.prompt, toolsets, task_id)
    except Exception as exc:
        log.debug("prompt %s failed", index, exc_info=True)
        return PromptResult(index, "failed", f"{type(exc).__name__}: {exc}")
    reason = conversation.exit_reason
    if reason not in COMPLETE_EXITS and reason not in PARTIAL_EXITS:
        detail = f" ({conversation.detail})" if conversation.detail else ""
        return PromptResult(index, "failed", f"the run ended {reason}{detail}")
    tallies = tool_tallies(conversation.messages)
    reasoning = reasoning_counts(conversation.messages)
    sample = convert(conversation.messages, prompt=row.prompt, tools=conversation.tools)
    path = batch_path(Path(settings.run_dir), batch)
    if not reasoning.any:
        tomb = Tombstone(index, DiscardReason.NO_REASONING, row.prompt)
        await _append(path, tomb.record(), fsync)
        on_complete(index)
        return PromptResult(index, "discarded", tallies=tallies, reasoning=reasoning)
    stats, errors = normalise(tallies, settings.registered)
    record = BatchRow(
        prompt_index=index,
        conversations=sample.turns,
        metadata={
            "batch": batch,
            "timestamp": _now_iso(scope),
            "model": conversation.model,
            "distribution": settings.distribution,
            "toolsets": list(toolsets),
        },
        completed=reason in COMPLETE_EXITS,
        partial=reason in PARTIAL_EXITS,
        api_calls=conversation.api_calls,
        tool_stats=stats,
        tool_error_counts=errors,
        reasoning=reasoning.as_dict(),
    )
    await _append(path, record.record(), fsync)
    on_complete(index)
    return PromptResult(
        index,
        "written",
        tallies=tallies,
        reasoning=reasoning,
        bad_arguments=sample.bad_arguments,
    )


def _stderr(line: str) -> None:
    sys.stderr.write(line + "\n")
    sys.stderr.flush()


async def run_batch(
    scope: Scope,
    settings: WorkerSettings,
    job: BatchJob,
    *,
    runner: AgentRunner | None = None,
    images: Callable[[DatasetRow, str], Awaitable[str | None]] | None = None,
    sampler: distributions.Sampler | None = None,
    keep_going: Callable[[], bool] = lambda: True,
    out: Out = _stderr,
    fsync: Callable[[int], None] = os.fsync,
) -> BatchOutcome:
    """The prompts of one batch, in order (see the module notes)."""
    runner = runner or SessionAgentRunner(scope, settings)
    images = images or ImagePrep(scope)
    sampler = sampler or distributions.Sampler(settings.valid_toolsets, warn=out)
    outcome = BatchOutcome(job.batch)
    totals: dict[str, Tally] = {}
    reasoning = Reasoning()
    for index, row in job.items:
        if index in job.skip:
            outcome.skipped += 1
            continue
        if not keep_going():
            outcome.stopped = True
            break
        outcome.processed += 1
        result = await run_prompt(
            scope,
            settings,
            job.batch,
            index,
            row,
            runner=runner,
            images=images,
            sampler=sampler,
            on_complete=outcome.completed.append,
            fsync=fsync,
        )
        for name, tally in result.tallies.items():
            totals.setdefault(name, Tally()).add(tally)
        reasoning.add(result.reasoning)
        outcome.bad_arguments += result.bad_arguments
        if result.status == "failed":
            outcome.failed += 1
            out(f"batch {job.batch}: prompt {index} failed (tried again next run): {result.error}")
        elif result.status == "discarded":
            outcome.discarded += 1
            if settings.verbose:
                out(f"batch {job.batch}: prompt {index} discarded (no reasoning)")
        elif settings.verbose:
            out(f"batch {job.batch}: prompt {index} done")
    outcome.tool_stats = tally_dict(totals)
    outcome.reasoning = reasoning.as_dict()
    return outcome


# -- the pool process ------------------------------------------------------------------------------


def worker_scope(settings: WorkerSettings) -> Scope:
    """The profile scope rebuilt in a pool process (config, ``.env`` and secret store)."""
    from indus.foundation.bootstrap import attach_profile_state
    from indus.foundation.envload import build_env

    home = Path(settings.home)
    env = dict(settings.env)
    draft = make_scope(home, env=build_env(home, env))
    return attach_profile_state(draft, env)


@dataclass
class _ProcessState:
    settings: WorkerSettings
    scope: Scope
    loop: asyncio.AbstractEventLoop
    parent: int


_PROCESS: _ProcessState | None = None


def init_worker(settings: WorkerSettings) -> None:
    """Pool initializer: Ctrl-C belongs to the parent; one scope and one loop per process."""
    global _PROCESS
    with contextlib.suppress(ValueError, OSError):
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    scope = worker_scope(settings)
    _PROCESS = _ProcessState(settings, scope, asyncio.new_event_loop(), os.getppid())


def process_batch(job: BatchJob) -> BatchOutcome:
    """Pool entry: one batch on this process's loop. Stops between prompts once the parent is
    gone (a killed run must not race the next one)."""
    state = _PROCESS
    if state is None:
        raise RuntimeError("the batch worker was started without init_worker")

    def parent_alive() -> bool:
        return os.getppid() == state.parent

    with use_scope(state.scope):
        return state.loop.run_until_complete(
            run_batch(state.scope, state.settings, job, keep_going=parent_alive)
        )
