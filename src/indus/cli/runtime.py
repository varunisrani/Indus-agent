"""Host wiring for the classic CLI: the route, the session service, the agent seams, shutdown.

The agent runs in this process (decision P1, inventory I55) behind ``SessionService``; the
interactive REPL drives it through ``InProcessHost``, finite runs await it directly. Seams wired
here (P05 wave 2 notes):

- every agent's ``SessionRuntime.environments`` is the process terminal runtime's registry, so
  the terminal, file and search tools share one set of backends and one cwd record;
- ``SessionRuntime.run_context`` tells the approval pipeline who is watching: ``interactive`` for
  the REPL, ``single_query`` for ``-z`` and finite ``chat -q`` runs;
- at exit the terminal runtime is shut down (background jobs, then every environment, which is
  the ``EnvironmentRegistry`` shutdown) on the loop that used it.
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
from indus.core.turn_state import LoopOptions
from indus.foundation.config import schema
from indus.foundation.config.loader import ConfigView, config_path
from indus.foundation.scope import Scope
from indus.providers.profile import REASONING_LEVELS
from indus.providers.route import RuntimeRoute, resolve
from indus.service.broker import ServerRequestBroker
from indus.service.host_inprocess import InProcessHost
from indus.service.session_service import AgentFactory, SessionService
from indus.state.registry import StoreHandle
from indus.tools.builtin import terminal

__all__ = [
    "DEFAULT_TOOLSETS",
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


def agent_factory(scope: Scope, route: RuntimeRoute, *, run_context: str) -> AgentFactory:
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
        agent = await Agent.start(scope, chosen, selection, full, sink)
        agent.runtime.environments = terminal.runtime().environments
        agent.runtime.run_context = run_context
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
    return SessionService(
        spec.scope,
        spec.route,
        toolsets=toolset_selection(spec.policy, spec.scope),
        options=replace(options, store=store),
        store=store,
        answerable_kinds=spec.answerable_kinds,
        owner=spec.source,
        agent_factory=agent_factory(spec.scope, spec.route, run_context=spec.run_context),
    )


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
