"""Host wiring for the classic CLI: the route, the session service, the agent seams, shutdown.

The agent runs in this process (decision P1, inventory I55) behind ``SessionService``; the
interactive REPL drives it through ``InProcessHost``, finite runs await it directly. Seams wired
here (P05 wave 2 notes):

- every agent's ``SessionRuntime.environments`` is the process terminal runtime's registry, so
  the terminal, file and search tools share one set of backends and one cwd record;
- ``SessionRuntime.run_context`` tells the approval pipeline who is watching: ``interactive`` for
  the REPL, ``single_query`` for ``-z`` and finite ``chat -q`` runs;
- at exit the terminal runtime is shut down (background jobs, then every environment, which is
  the ``EnvironmentRegistry`` shutdown) on the loop that used it;
- one auxiliary client (P08-17) per run goes to the service: it titles sessions after their
  first exchange and every agent binds it to its own route for compression summaries;
- the backends' questions about sessions (P11): process receipts follow the compression lineage
  of this run's store, and the Docker orphan sweep asks it whether a session is over
  (``service.terminal_host``);
- the learning loop (P09, ``learning.host``): each agent gets a registry with the learning tools,
  inline executors for the selected ``memory`` / ``session_search`` only, the prompt builder that
  adds the frozen memory block, the skills index and the ``skills.auto_load`` / ``-s`` preload
  block, and the skills-index change note; ``session_search`` gets the agent's bound auxiliary
  client once the agent is open; every agent gets its background review
  (``SessionLearning.attach_review``; one-shot runs never review);
- the learning upkeep (P09-18): an interactive run's service carries the maintenance timer with
  the curator check (``service.maintenance.learning_timer``), driven by the service's auxiliary
  client and the store's idle time;
- ``learning_startup`` before a chat run: the shipped skills are seeded into a profile that was
  never seeded (P09-21, honouring the opt-out marker), and an interactive session start runs the
  curator check on a daemon thread (it never blocks the chat and never raises);
- ``execute_code`` kernels (P10-10/11, ``service.code_host``): the service disposes a closed
  session's kernels (``/new``, ``/clear``, switching, exit); its ``KernelUpkeep`` shuts both
  kernel pools down at ``aclose`` (before the terminal runtime, so remote kernels still have
  their environments) and, in an interactive run, reaps idle kernels;
- subagents (P10-02): ``AgentOptions.subagent_builder`` is the builder under the learning
  wrappers, so a child's prompt has no frozen memory, skills index or preloads; children never
  get a background review (``attach_review`` forces ``no_review`` for them) and never reach the
  curator, which only this host's start and maintenance timer run;
- MCP servers (P13-05..12, ``service.mcp_host``): one tool registry per run (built-in and
  learning tools, plus the MCP tools of the profile's overlay) is shared by every agent of the
  service; ``McpUpkeep`` starts the profile's servers before the first agent (discovery once),
  adds the ``mcp`` toolset to the platform bundle, samples through the run's auxiliary client and
  closes the servers at exit; an interactive run also watches the config file for
  ``mcp_servers`` edits.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from indus.cli.args import HostOverrides
from indus.cli.session_select import LAUNCH_KEY, LaunchPolicy, workspace_key
from indus.core.agent import Agent, AgentOptions
from indus.core.events import EventSink
from indus.core.persist import StateStore
from indus.core.prompt.builder import PromptBuilder
from indus.core.prompt.lifecycle import Builder, SessionPrompts
from indus.core.turn_state import LoopOptions
from indus.foundation.config import schema
from indus.foundation.config.loader import ConfigView, config_path
from indus.foundation.scope import Scope
from indus.learning import host as learning_host
from indus.learning.review.counters import NO_REVIEW_CONTEXTS
from indus.providers.auxiliary import AuxClient
from indus.providers.profile import REASONING_LEVELS
from indus.providers.route import RuntimeRoute, resolve
from indus.service import terminal_host
from indus.service.broker import ServerRequestBroker
from indus.service.code_host import KernelUpkeep
from indus.service.host_inprocess import InProcessHost
from indus.service.maintenance import MaintenanceTimer, learning_timer, profile_idle_s
from indus.service.mcp_host import McpUpkeep
from indus.service.session_service import AgentFactory, SessionService
from indus.state.registry import StoreHandle
from indus.tools import toolsets
from indus.tools.builtin import terminal

__all__ = [
    "DEFAULT_TOOLSETS",
    "learning_startup",
    "SOURCE_CLI",
    "SOURCE_ONESHOT",
    "ServiceSpec",
    "agent_factory",
    "build_service",
    "defaults_only",
    "make_host",
    "reasoning_problem",
    "resolve_route",
    "scope_for",
    "shutdown_terminal",
    "toolset_selection",
    "with_learning",
]

log = logging.getLogger(__name__)

DEFAULT_TOOLSETS: tuple[str, ...] = ("indus-cli",)
"""The classic CLI's platform bundle, used when no ``-t`` was given and nothing is saved."""
SOURCE_CLI = "cli"
SOURCE_ONESHOT = "oneshot"
_YOLO_ENV = "INDUS_YOLO_MODE"


def reasoning_problem(level: str | None) -> str | None:
    """A usage message when ``level`` is not on the ladder (None when fine)."""
    if level is None or level.strip().lower() in REASONING_LEVELS:
        return None
    return f"unknown reasoning level {level!r}; use one of: {', '.join(REASONING_LEVELS)}"


def defaults_only(scope: Scope) -> Scope:
    """``scope`` reading the built-in settings instead of the user's file (``--ignore-user-config``,
    ``--safe-mode``); ``.env`` and secrets stay."""
    view = ConfigView(config_path(scope), schema.defaults_tree(), {}, set())
    return scope.with_config(view)


def scope_for(scope: Scope, policy: LaunchPolicy, *, auto_approve: bool = False) -> Scope:
    """The scope a session runs under: built-in settings when the policy ignores the user's,
    and ``INDUS_YOLO_MODE`` laid over the env view when the run auto-approves."""
    out = scope
    if policy.ignore_user_config or policy.safe_mode:
        out = defaults_only(out)
    if auto_approve or policy.yolo:
        out = out.with_env(out.env.with_overlay({_YOLO_ENV: "1"}))
    return out


def resolve_route(
    scope: Scope, policy: LaunchPolicy, overrides: HostOverrides | None = None
) -> RuntimeRoute:
    """The route for the policy's model and provider (or the test override's route)."""
    if overrides is not None and overrides.route is not None:
        route: RuntimeRoute = overrides.route
        return replace(route, model=policy.model) if policy.model else route
    return resolve(scope, model=policy.model, provider=policy.provider)


def toolset_selection(policy: LaunchPolicy, scope: Scope | None = None) -> list[str]:
    """``-t`` wins; else the list ``indus tools`` saved for the ``cli`` platform (not when the run
    ignores the user's settings); else the platform bundle."""
    if policy.toolsets:
        return list(policy.toolsets)
    if scope is not None and not (policy.ignore_user_config or policy.safe_mode):
        from indus.cli.commands.tools import saved_selection

        saved = saved_selection(scope, "cli")
        if saved is not None:
            return saved
    return list(DEFAULT_TOOLSETS)


def with_learning(
    scope: Scope,
    options: AgentOptions,
    selection: Sequence[str] | None,
    *,
    session_id: str,
    preload: Sequence[str] = (),
) -> tuple[AgentOptions, learning_host.SessionLearning]:
    """``options`` with the learning loop wired in (see the module notes)."""
    registry = options.registry or learning_host.tool_registry(scope.clock)
    chosen = toolsets.select(
        selection,
        options.disabled_toolsets,
        registry=registry,
        scope=scope,
        platform=options.platform,
    )
    prompt_store = options.prompt_store
    if prompt_store is None and isinstance(options.store, StateStore):
        prompt_store = SessionPrompts(options.store.handle)
    learning = learning_host.wire_session(
        scope,
        chosen.tools,
        session_id=session_id,
        prompt_store=prompt_store,
        preload=preload,
        registry=registry,
        inner=options.builder,
    )
    wired = replace(
        options,
        registry=registry,
        prompt_store=prompt_store,
        builder=learning.builder,
        # Subagents get the base builder: no frozen memory, skills index or preloads (P10-02).
        subagent_builder=_base_builder(options),
        inline={**learning.inline, **options.inline},
        note_sources=(*options.note_sources, *learning.note_sources),
    )
    return wired, learning


def _base_builder(options: AgentOptions) -> Builder:
    if options.subagent_builder is not None:
        return options.subagent_builder
    return options.builder if options.builder is not None else PromptBuilder()


def learning_startup(scope: Scope, store: StoreHandle | None, *, interactive: bool) -> None:
    """First-run skill seeding, then (interactive runs) the curator check in the background."""
    from indus.learning.skills.bundled_sync import seed_first_run

    seed_first_run(scope.home, env=scope.env)
    if not interactive:
        return
    try:
        from indus.cli.commands import curator as curator_cmds
        from indus.foundation.scope import thread_with_scope
        from indus.learning.curator.lifecycle import maybe_run

        idle = profile_idle_s(store, scope.clock)
        thread_with_scope(
            lambda: maybe_run(scope, idle_s=idle, step_factory=curator_cmds.STEP_FACTORY),
            scope=scope,
            name="curator check",
            daemon=True,
        ).start()
    except Exception:
        log.warning("the curator check could not start", exc_info=True)


def agent_factory(
    scope: Scope, route: RuntimeRoute, *, run_context: str, preload: Sequence[str] = ()
) -> AgentFactory:
    """Builds each session's agent and wires the host seams (see the module notes)."""

    async def build(
        session_id: str,
        sink: EventSink,
        broker: ServerRequestBroker,
        options: AgentOptions,
        selection: Sequence[str] | None,
    ) -> Agent:
        chosen = route
        model = options.session_fields.get("model")
        if isinstance(model, str) and model and model != route.model:
            chosen = replace(route, model=model)
        full = replace(options, session_id=session_id, broker=broker)
        full, learning = with_learning(
            scope, full, selection, session_id=session_id, preload=preload
        )
        agent = await Agent.start(scope, chosen, selection, full, sink)
        agent.runtime.environments = terminal.runtime().environments
        agent.runtime.run_context = run_context
        learning.bind_aux(agent.aux)
        learning.attach_review(agent, no_review=run_context in NO_REVIEW_CONTEXTS)
        return agent

    return build


@dataclass(frozen=True)
class ServiceSpec:
    """Everything ``build_service`` needs for one CLI run."""

    scope: Scope
    route: RuntimeRoute
    policy: LaunchPolicy
    store: StoreHandle
    source: str = SOURCE_CLI
    run_context: str = "interactive"
    answerable_kinds: Iterable[str] = ()
    overrides: HostOverrides | None = None
    new_session: bool = True


def _session_fields(spec: ServiceSpec, cwd: Path) -> dict[str, Any]:
    fields: dict[str, Any] = {"cwd": str(cwd), "model": spec.route.model}
    root = workspace_key(cwd)
    if root != cwd or (root / ".git").exists():
        fields["git_root"] = str(root)
    if spec.new_session:
        record = replace(spec.policy, model=spec.route.model, provider=spec.route.provider)
        fields["model_config"] = {LAUNCH_KEY: record.record()}
    return fields


def _loop_options(spec: ServiceSpec) -> LoopOptions:
    values: dict[str, Any] = {"platform": "cli"}
    if spec.policy.max_turns is not None:
        values["max_iterations"] = max(0, spec.policy.max_turns)
    if spec.policy.reasoning:
        values["reasoning_effort"] = spec.policy.reasoning.strip().lower()
    return LoopOptions.from_scope(spec.scope, **values)


def build_service(spec: ServiceSpec) -> SessionService:
    """The session service for one CLI run (call it on the loop that will run it)."""
    cwd = Path(spec.policy.cwd) if spec.policy.cwd else Path.cwd()
    overrides = spec.overrides
    options = AgentOptions(
        platform="cli",
        source=spec.source,
        cwd=cwd,
        loop=_loop_options(spec),
        session_fields=_session_fields(spec, cwd),
        client_factory=overrides.client_factory if overrides is not None else None,
        list_models=overrides.list_models if overrides is not None else None,
    )
    store = StateStore(spec.store)
    terminal_host.install(spec.store, terminal.runtime().processes)
    aux = AuxClient(
        spec.scope,
        spec.route,
        client_factory=overrides.client_factory if overrides is not None else None,
    )
    # One registry for the run: every agent sees the MCP tools registered into it (P13-05).
    registry = learning_host.tool_registry(spec.scope.clock)
    options = replace(options, registry=registry)
    return SessionService(
        spec.scope,
        spec.route,
        toolsets=toolset_selection(spec.policy, spec.scope),
        options=replace(options, store=store),
        store=store,
        answerable_kinds=spec.answerable_kinds,
        owner=spec.source,
        agent_factory=agent_factory(
            spec.scope,
            spec.route,
            run_context=spec.run_context,
            preload=spec.policy.skills,
        ),
        aux=aux,
        maintenance=_maintenance(spec, aux),
        # The CLI owns its process: kernels end at exit; an interactive run reaps idle ones.
        kernels=KernelUpkeep(spec.scope, reaper=spec.run_context == "interactive"),
        mcp=McpUpkeep(spec.scope, registry, aux=aux, watch=spec.run_context == "interactive"),
    )


def _maintenance(spec: ServiceSpec, aux: AuxClient) -> MaintenanceTimer | None:
    """An interactive run keeps the profile's learning upkeep going (P09-18); finite runs end
    long before the first poll."""
    if spec.run_context != "interactive":
        return None
    scope, handle = spec.scope, spec.store
    return learning_timer(scope, aux=aux, idle_s=lambda: profile_idle_s(handle, scope.clock))


def make_host(spec: ServiceSpec, *, call_timeout: float | None = None) -> InProcessHost:
    """An in-process host (not started) whose service is built from ``spec`` on its loop."""
    return InProcessHost(
        lambda: build_service(spec), scope=spec.scope, name="indus-cli", call_timeout=call_timeout
    )


async def shutdown_terminal() -> None:
    """Stop background jobs and clean every terminal environment (process exit)."""
    try:
        await terminal.runtime().shutdown()
    except Exception:
        log.warning("terminal shutdown failed", exc_info=True)
