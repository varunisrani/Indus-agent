"""The agent: one session's wiring around the turn loop (P04-20).

A host holds one ``Agent`` per session. ``Agent.start(...)`` (or ``Agent(...)`` then
``await agent.open()``) builds its parts in this fixed order — each phase uses only what the
earlier ones made:

1. ``route`` — the ``RouteBinding`` (window resolved through P03, client, reasoning, extra body);
2. ``sink`` — the session's ``EventSink`` (the session id is settled here, events carry it);
3. ``client`` — ``BoundCaller``, the model caller over the binding, with ``agent.api_max_retries``;
4. ``tools`` — registry, toolset selection, the provider-neutral tool array;
5. ``session`` — transcript store, prompt lifecycle, the ``SessionRuntime`` kept across turns;
6. ``config`` — loop options from the config (``agent.max_turns``) and the bound route;
7. ``context_engine`` — the session's context engine (P08-05; None when compression is off).

Usage (P08-08): the loop's caller is wrapped so every answered call lands in ``Agent.usage`` (the
session's ``UsageLedger``, also written to the store); the context engine listens to that ledger
— the one path a response's usage reaches the usage anchor. Side tasks go through ``Agent.aux``,
the host's auxiliary client (P08-17) bound to this session's route and ledger; without one, the
compression summary asks the main route, recorded under the ``compression`` task tag.

Fast mode (P08-15): ``Agent.fast`` holds the session's ``/fast`` choice; the caller layers the
route's priority tier onto a request next to ``finish_request`` while it is active, and each
started turn opens the ``auto`` window. The smart approval judge (P08-14) is the session's
auxiliary client, offered to tools as ``approval_judge``.

A rotating compaction (``compression.in_place: false``) continues the conversation in a child
session: the agent's sink, ledger and engine move to the child id and ``on_session_moved``
listeners (the host) are told ``(old id, new id)``.

Edits and stop gates (P24-16/17): the tool hook seam is always wrapped in
``tools.lsp.diagnostics.LspToolHooks`` (it binds who is editing for each call, so the turn's
edited files are known even with no language server, and hands late LSP notes to the next tool
result), and the runtime asks ``AgentOptions.stop_gates`` — by default
``core.stop_gates_lsp.standard_gates``: the LSP gate, verify-on-stop in the session's terminal
environment, and ``AgentOptions.pre_verify`` (the plugin hook) when the host gave one — before a
text answer is final.

``run_turn`` runs one turn and returns its ``TurnResult``; ``chat`` returns just the final text.
``start_turn`` first awaits the runtime's ``before_turn`` hooks (P09-15: a live turn cancels the
session's background review); a failing hook is logged and the turn goes ahead. While a turn
runs, ``interrupt`` / ``steer`` / ``redirect`` reach it (loop thread only). Every task
the agent starts goes through ``spawn``, which runs it with the agent's own ``Scope`` in a copied
context, whatever scope the caller has (R01.16). ``close`` stops a running turn, runs the agent's
closers and the task cleanups tool backends registered, and closes clients the agent owns; every
step is guarded and the whole thing runs once.

Credentials and fallback (P08-12, P08-13): each request asks the provider's live credential
pool (``Agent.pools``; subagents get the parent's through ``AgentOptions.pools``) for its key and
rotates on key failures; what the pool cannot fix goes to the session's ``FallbackChain``, which
the loop tries and the prologue rolls back at the next turn.

R01.5: the caller reads ``binding.state`` once per request. A request built for another model is
not sent, and a retry is never sent once the binding's generation moved: the call ends
``RouteChanged`` and the loop rebuilds the request for the new route.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import secrets
from collections.abc import Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

import httpx

from indus.core import fast_mode
from indus.core.cleanup import TASK_CLEANUPS, CleanupRegistry
from indus.core.compression.engine import (
    BuiltinEngine,
    CompressRequest,
    Outcome,
    build_engine,
    caller_summarizer,
    compress_now,
)
from indus.core.compression.summarize import AuxSummarizer, Summarizer
from indus.core.events import EventListener, EventSink, as_sink
from indus.core.fallback import FallbackChain
from indus.core.fast_mode import FastMode
from indus.core.loop import run_turn
from indus.core.model_call import OnEvent, OnWait, RouteCaller
from indus.core.persist import MemoryStore, StateStore, TranscriptStore
from indus.core.prompt.builder import BuildContext, PromptBuilder
from indus.core.prompt.lifecycle import Builder, PromptLifecycle, PromptStore, SessionPrompts
from indus.core.route import ClientFactory, RouteBinding, RouteState, pooled_client, prompt_identity
from indus.core.stop_gates import PreVerifyHook, StopContext, StopGates
from indus.core.stop_gates_lsp import standard_gates
from indus.core.transcript import TranscriptRow
from indus.core.turn_state import (
    Hydrator,
    InlineExecutor,
    LlmContextSource,
    LoopOptions,
    NoteSource,
    OriginLookup,
    SessionRuntime,
    TurnResult,
    TurnState,
    new_turn_state,
)
from indus.core.usage import RecordingCaller, UsageLedger
from indus.core.wire import finish_request
from indus.foundation.blocking import run_blocking
from indus.foundation.clock import Clock
from indus.foundation.errors import IndusError
from indus.foundation.ids import new_session_id
from indus.foundation.scope import Scope, spawn_with_scope
from indus.protocol.requests_port import ServerRequestPort
from indus.providers.auxiliary import AuxClient
from indus.providers.classify import ClassifiedError, classify
from indus.providers.context_length import ListModels
from indus.providers.pool import Adjust, PoolSet
from indus.providers.retry import DEFAULT_MAX_RETRIES, clamp_max_retries
from indus.providers.route import RuntimeRoute
from indus.providers.streaming import CallOutcome, RouteChanged, StaleStreak, StreamFlags
from indus.providers.timeouts import TimeoutPolicy
from indus.providers.types import ChatRequest
from indus.safety.approvals.smart import judge_from_aux
from indus.state import usage as usage_rows
from indus.state.usage import UsageDelta
from indus.tools import toolsets
from indus.tools.builtin import load_builtins
from indus.tools.builtin.delegate import TOOL_NAME as DELEGATE_TOOL
from indus.tools.builtin.exec_host import ExecHost
from indus.tools.builtin.todo import TOOL_NAME as TODO_TOOL
from indus.tools.builtin.todo import TodoWiring, todo_wiring
from indus.tools.definitions import definitions
from indus.tools.hooks import NO_HOOKS, ToolHooks
from indus.tools.lsp.diagnostics import LspToolHooks
from indus.tools.registry import ToolRegistry

__all__ = [
    "PHASES",
    "Agent",
    "AgentBusy",
    "AgentClosed",
    "AgentOptions",
    "BoundCaller",
    "RedirectOutcome",
    "RunningTurn",
]

log = logging.getLogger(__name__)

PHASES = ("route", "sink", "client", "tools", "session", "config", "context_engine")
"""The construction phases, in the order ``open`` runs them."""

CLOSE_WAIT_S = 10.0
"""How long ``close`` waits for an interrupted turn to finish its exit funnel."""

type RedirectOutcome = Literal["redirected", "steered", "rejected"]


class AgentBusy(IndusError):
    """A turn is already running on this agent."""

    default_code = "session.busy"


class AgentClosed(IndusError):
    """The agent was closed; it runs no more turns."""

    default_code = "session.busy"


@dataclass(frozen=True)
class AgentOptions:
    """Everything about an agent that is not its scope, route, toolsets or sink."""

    session_id: str | None = None
    platform: str = "cli"
    source: str = ""
    store: TranscriptStore | None = None
    prompt_store: PromptStore | None = None
    builder: Builder | None = None
    registry: ToolRegistry | None = None
    disabled_toolsets: Sequence[str] = ()
    client_factory: ClientFactory | None = None
    list_models: ListModels | None = None
    loop: LoopOptions | None = None
    max_retries: int | None = None
    policy: TimeoutPolicy = field(default_factory=TimeoutPolicy)
    cwd: Path | None = None
    system_message: str | None = None
    broker: ServerRequestPort | None = None
    hooks: ToolHooks = NO_HOOKS
    hydrators: Mapping[str, Hydrator] = field(default_factory=dict[str, Hydrator])
    inline: Mapping[str, InlineExecutor] = field(default_factory=dict[str, InlineExecutor])
    session_fields: Mapping[str, Any] = field(default_factory=dict[str, Any])
    cleanups: CleanupRegistry | None = None
    # The compression summariser (P08-04); default: the auxiliary client, else the main route.
    summarizer: Summarizer | None = None
    # The host's auxiliary client (P08-17); each agent binds it to its own route and ledger.
    aux: AuxClient | None = None
    # Live credential pools (P08-12); a subagent gets its parent's. Default: the scope's.
    pools: PoolSet | None = None
    # Host note sources the prologue runs before the user message (P09-07 skills delta).
    note_sources: Sequence[NoteSource] = ()
    # Context for each turn's user message from the plugin hooks bus (P13-17 ``pre_llm_call``).
    llm_context: LlmContextSource | None = None
    # Subagents (P10-02): the exact tools a child may have (None: the selection decides) and the
    # child's delegation depth (0 for a top-level agent).
    only_tools: frozenset[str] | None = None
    delegation_depth: int = 0
    # The prompt builder this agent's subagents start from (P10-02). None: ``builder``. A host that
    # wraps ``builder`` with session-level blocks (P09: frozen memory, skills index, preloads)
    # passes the unwrapped one here, so children never carry them.
    subagent_builder: Builder | None = None
    # Stop gates (A28): None builds the standard ones (``core.stop_gates_lsp.standard_gates``).
    stop_gates: StopGates | None = None
    # The ``pre_verify`` plugin hook for the standard gates (P24-17); None: no such gate.
    pre_verify: PreVerifyHook | None = None
    # The chat a gateway session answers in, offered to tools as ``delivery_origin`` (P24-13).
    delivery_origin: OriginLookup | None = None


@dataclass
class RunningTurn:
    """A turn the agent started: its task and its state (the control lives on the state)."""

    turn_id: str
    generation: int
    state: TurnState
    task: asyncio.Task[TurnResult]

    @property
    def done(self) -> bool:
        return self.task.done()


# -- the model caller over the binding ------------------------------------------------------------


@dataclass
class BoundCaller:
    """The ``ModelCaller`` the loop uses: one ``RouteCaller`` per request, built from the binding
    state read once for that request (see the module notes on R01.5)."""

    binding: RouteBinding
    clock: Clock
    max_retries: int = DEFAULT_MAX_RETRIES
    policy: TimeoutPolicy = field(default_factory=TimeoutPolicy)
    # Who drives the session (``auto`` cache tier) and the prompt's static prefix, if known.
    source: str = ""
    static_prefix: Callable[[], str | None] = lambda: None
    # Live credential pools (P08-12) and the fallback chain's eager rule (P08-13).
    pools: PoolSet | None = None
    eager: Callable[[ClassifiedError], bool] | None = None
    # The session's fast mode (P08-15): the priority tier layered onto a request, if any.
    fast: FastMode | None = None
    _flags: dict[tuple[str, str, str], StreamFlags] = field(
        default_factory=dict[tuple[str, str, str], StreamFlags]
    )
    _streaks: dict[tuple[str, str, str], StaleStreak] = field(
        default_factory=dict[tuple[str, str, str], StaleStreak]
    )

    @property
    def model(self) -> str:
        return self.binding.state.model

    def caller_for(
        self,
        state: RouteState,
        generation: int,
        *,
        route: RuntimeRoute | None = None,
        adjust: Adjust | None = None,
    ) -> RouteCaller:
        """``route``: the bound route carrying a pool credential (default: the bound route)."""
        key = (state.provider, state.base_url, state.model)
        binding = self.binding
        target = route if route is not None else state.route
        return RouteCaller(
            client=state.client,
            route=target,
            clock=self.clock,
            policy=self.policy,
            max_retries=self.max_retries,
            flags=self._flags.setdefault(key, StreamFlags()),
            streak=self._streaks.setdefault(key, StaleStreak()),
            epoch=lambda: binding.generation,
            still_current=lambda: binding.generation == generation,
            classify=self._classifier(target, adjust),
        )

    def _classifier(
        self, route: RuntimeRoute, adjust: Adjust | None
    ) -> Callable[[BaseException], ClassifiedError]:
        """P03's classifier with the route, its profile and the clock (so ``reset_at`` is known),
        then the pool's per-attempt rule and the chain's eager rule."""
        clock, eager = self.clock, self.eager

        def run(error: BaseException) -> ClassifiedError:
            verdict = classify(error, route=route, profile=route.profile, clock=clock)
            if adjust is not None:
                verdict = adjust(verdict)
            if eager is not None and verdict.retryable and eager(verdict):
                verdict = replace(verdict, retryable=False)
            return verdict

        return run

    def reset_streak(self, state: RouteState) -> None:
        """A route switch: the stale streak of the route now bound starts at zero."""
        self._streaks.pop((state.provider, state.base_url, state.model), None)

    async def call(
        self, request: ChatRequest, *, on_event: OnEvent, on_wait: OnWait | None = None
    ) -> CallOutcome:
        state = self.binding.state
        generation = self.binding.generation
        if request.model != state.model:
            log.info(
                "request was built for %s but the route is now %s; rebuilding it",
                request.model,
                state.model,
            )
            return RouteChanged(None, 0, 0)
        if state.extra_body and not request.extra_body:
            request = replace(request, extra_body=dict(state.extra_body))
        # The cache plan for the route this request really goes to, made last (P08-07).
        request = finish_request(
            request,
            markers=state.cache.markers,
            ttl_setting=state.cache.ttl,
            source=self.source,
            native=state.api_mode == "anthropic_messages",
            static_prefix=self.static_prefix() if state.cache.markers else None,
        )
        # The priority tier (P08-15) goes on next, so every pooled key below carries it.
        tier = None
        if self.fast is not None:
            request, fast_route, tier = self.fast.layer(request, state.route)
            if fast_route is not state.route:
                state = replace(state, route=fast_route)
        pool = self.pools.for_route(state.route) if self.pools is not None else None
        if pool is None:
            caller = self.caller_for(state, generation)
            outcome = await caller.call(request, on_event=on_event, on_wait=on_wait)
            return fast_mode.served(outcome, tier)

        async def send(route: RuntimeRoute, adjust: Adjust) -> CallOutcome:
            caller = self.caller_for(state, generation, route=route, adjust=adjust)
            return await caller.call(request, on_event=on_event, on_wait=on_wait)

        binding = self.binding
        outcome = await pool.call(
            state.route,
            send,
            still_current=lambda: binding.generation == generation,
            answered=lambda outcome: not isinstance(outcome, RouteChanged),
        )
        return fast_mode.served(outcome, tier)


def _stable_tier(lifecycle: PromptLifecycle) -> str | None:
    """The stable tier of a prompt built in this process (a restored prompt has no known split)."""
    parts = lifecycle.last_parts
    if parts is None or lifecycle.prompt is None:
        return None
    stable = parts.stable.strip()
    return stable if stable and lifecycle.prompt.startswith(stable) else None


def _edit_seam(hooks: ToolHooks) -> ToolHooks:
    """The session's tool hook seam inside ``LspToolHooks`` (once; see the module notes)."""
    return hooks if isinstance(hooks, LspToolHooks) else LspToolHooks(hooks)


# -- the agent ------------------------------------------------------------------------------------


class Agent:
    """One session's agent (see the module notes)."""

    def __init__(
        self,
        scope: Scope,
        route: RuntimeRoute | RouteBinding,
        toolset_selection: Sequence[str] | None = None,
        options: AgentOptions | None = None,
        sink: EventSink | EventListener | None = None,
    ) -> None:
        self.scope = scope
        self.options = options or AgentOptions()
        self.toolset_selection = list(toolset_selection) if toolset_selection is not None else None
        self.phases: list[str] = []
        self.context_engine: BuiltinEngine | None = None
        self._route_in = route
        self._sink_in = sink
        self._binding: RouteBinding | None = route if isinstance(route, RouteBinding) else None
        self._sink: EventSink | None = None
        self._caller: BoundCaller | None = None
        self._runtime: SessionRuntime | None = None
        self._registry: ToolRegistry | None = None
        self._selected: list[str] = []
        self._tools_stale = False
        self._owned_clients: list[httpx.AsyncClient] = []
        self._closers: list[tuple[str, Any]] = []
        self._tasks: set[asyncio.Task[Any]] = set()
        self._current: RunningTurn | None = None
        self._generation = 0
        self._opened = False
        self._open_lock: asyncio.Lock | None = None
        self._closed = False
        self.close_errors: list[str] = []
        self.todo: TodoWiring | None = None
        self.usage: UsageLedger = UsageLedger(clock=scope.clock)
        self.aux: AuxClient | None = None
        self.pools: PoolSet | None = self.options.pools
        self.fallback: FallbackChain | None = None
        self.fast: FastMode | None = None
        self._former_ids: list[str] = []
        self._moved: list[Callable[[str, str], object]] = []

    @classmethod
    async def start(
        cls,
        scope: Scope,
        route: RuntimeRoute | RouteBinding,
        toolset_selection: Sequence[str] | None = None,
        options: AgentOptions | None = None,
        sink: EventSink | EventListener | None = None,
    ) -> Agent:
        """Build an agent and run every construction phase."""
        agent = cls(scope, route, toolset_selection, options, sink)
        await agent.open()
        return agent

    # -- views ------------------------------------------------------------------------------------

    @property
    def binding(self) -> RouteBinding:
        if self._binding is None:
            raise RuntimeError("the agent is not open yet")
        return self._binding

    @property
    def sink(self) -> EventSink:
        if self._sink is None:
            raise RuntimeError("the agent is not open yet")
        return self._sink

    @property
    def runtime(self) -> SessionRuntime:
        if self._runtime is None:
            raise RuntimeError("the agent is not open yet")
        return self._runtime

    @property
    def caller(self) -> BoundCaller:
        if self._caller is None:
            raise RuntimeError("the agent is not open yet")
        return self._caller

    @property
    def session_id(self) -> str:
        return self.sink.session_id

    @property
    def broker(self) -> ServerRequestPort | None:
        return self.options.broker

    @property
    def current(self) -> RunningTurn | None:
        """The running turn, if any."""
        current = self._current
        return current if current is not None and not current.done else None

    @property
    def generation(self) -> int:
        """The generation of the newest turn started (0 before the first)."""
        return self._generation

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def tool_names(self) -> list[str]:
        return list(self._selected)

    # -- construction -----------------------------------------------------------------------------

    async def open(self) -> None:
        """Run the construction phases in order (once; later calls return at once)."""
        if self._opened:
            return
        if self._open_lock is None:
            self._open_lock = asyncio.Lock()
        async with self._open_lock:
            if self._opened:
                return
            await self._open_route()
            self.phases.append("route")
            self._open_sink()
            self.phases.append("sink")
            self._open_client()
            self.phases.append("client")
            self._open_tools()
            self.phases.append("tools")
            self._open_session()
            self.phases.append("session")
            self._read_config()
            self.phases.append("config")
            self._open_context_engine()
            self.phases.append("context_engine")
            self._opened = True
            log.debug("agent for session %s open (%s)", self.session_id, ", ".join(self.phases))

    async def _open_route(self) -> None:
        if self._binding is not None:
            return
        route = self._route_in
        assert isinstance(route, RuntimeRoute)
        given = self.options.client_factory
        factory: ClientFactory = pooled_client
        if given is not None:

            def owned(target: RuntimeRoute) -> httpx.AsyncClient:
                client = given(target)
                self._owned_clients.append(client)
                return client

            factory = owned
        self._binding = await RouteBinding.bind(
            self.scope, route, client_factory=factory, list_models=self.options.list_models
        )

    def _open_sink(self) -> None:
        wanted = self.options.session_id
        if wanted is None and isinstance(self._sink_in, EventSink):
            wanted = self._sink_in.session_id
        session_id = wanted or new_session_id(self.scope.clock)
        self._sink = as_sink(session_id, self._sink_in)

    def _open_client(self) -> None:
        if self.pools is None:
            self.pools = PoolSet.from_scope(self.scope)
        self.fast = FastMode(self.scope, lambda: self.sink.session_id)
        self._caller = BoundCaller(
            self.binding,
            self.scope.clock,
            max_retries=self._max_retries(),
            policy=self.options.policy,
            source=self.options.source or self.options.platform,
            pools=self.pools,
            fast=self.fast,
        )

    def _max_retries(self) -> int:
        if self.options.max_retries is not None:
            return clamp_max_retries(self.options.max_retries)
        config = self.scope.config
        if config is None:
            return DEFAULT_MAX_RETRIES
        try:
            return clamp_max_retries(config.get("agent.api_max_retries"))
        except Exception:
            log.debug("agent.api_max_retries unreadable; using the default", exc_info=True)
            return DEFAULT_MAX_RETRIES

    def _open_tools(self) -> None:
        registry = self.options.registry
        if registry is None:
            registry = ToolRegistry(clock=self.scope.clock)
            load_builtins(registry)
        self._registry = registry
        chosen = toolsets.select(
            self.toolset_selection,
            self.options.disabled_toolsets,
            registry=registry,
            scope=self.scope,
            platform=self.options.platform,
        )
        tools = chosen.tools
        if self.options.only_tools is not None:
            tools = tools & self.options.only_tools
        self._selected = sorted(tools)

    def _tool_array(self, state: RouteState) -> list[dict[str, Any]]:
        assert self._registry is not None
        return definitions(self._registry, self._selected, state.route, scope=self.scope)

    def tools_changed(self, selection: Sequence[str] | None = None) -> None:
        """The registry's tool surface changed (P13-08: MCP reload, ``list_changed``). The
        stored tool array stays as it is until the next compaction rebuilds it (R01.1); a new
        ``selection`` (when given) is used from then on."""
        if selection is not None:
            self.toolset_selection = list(selection)
        self._tools_stale = True

    def _refresh_tools(self) -> None:
        """Compaction hook: rebuild the tool array if the tool surface changed meanwhile."""
        runtime = self._runtime
        if not self._tools_stale or runtime is None:
            return
        self._tools_stale = False
        self._open_tools()
        runtime.tools = self._tool_array(self.binding.state)
        runtime.prompt_context = replace(
            runtime.prompt_context,
            tool_names=frozenset(self._selected) | frozenset(self.options.inline),
        )

    def _open_session(self) -> None:
        options = self.options
        state = self.binding.state
        store = options.store if options.store is not None else MemoryStore()
        prompt_store = options.prompt_store
        if prompt_store is None and isinstance(store, StateStore):
            prompt_store = SessionPrompts(store.handle)
        lifecycle = PromptLifecycle(options.builder or PromptBuilder(), store=prompt_store)
        self.caller.static_prefix = lambda: _stable_tier(lifecycle)
        tools = self._tool_array(state)
        inline, hydrators = dict(options.inline), dict(options.hydrators)
        if TODO_TOOL in self._selected and TODO_TOOL not in inline:
            # The session's task list (P05-24): run by the loop, rebuilt from history on resume.
            self.todo = todo_wiring()
            inline[TODO_TOOL] = self.todo.execute
            hydrators.setdefault("todo", self.todo.hydrate)
        if DELEGATE_TOOL in self._selected and DELEGATE_TOOL not in inline:
            # Subagents (P10-01..07): the loop runs delegate_task through the session's
            # delegation; imported here because the subagents package builds agents itself.
            from indus.core.subagents import attach_delegation

            inline[DELEGATE_TOOL] = attach_delegation(self).execute
        context = BuildContext(
            scope=self.scope,
            model=state.model,
            provider=state.provider,
            platform=options.platform,
            session_id=self.session_id,
            tool_names=frozenset(self._selected) | frozenset(options.inline),
            system_message=options.system_message,
            cwd=options.cwd,
            context_length=state.context_length,
        )
        self.usage = UsageLedger(
            clock=self.scope.clock, session_id=self.session_id, store=self._usage_sink(store)
        )
        binding = self.binding
        if options.aux is not None:
            ledger = self.usage

            async def record(task: str, route: RuntimeRoute, usage: Any, latency: float) -> None:
                await ledger.record(usage, route=route, latency_s=latency, task=task)

            self.aux = options.aux.bound(lambda: binding.state.route, record=record)
        self._runtime = SessionRuntime(
            session_id=self.session_id,
            caller=RecordingCaller(self.caller, self.usage, lambda: binding.state.route),
            clock=self.scope.clock,
            prompts=lifecycle,
            prompt_context=context,
            store=store,
            registry=self._registry or ToolRegistry(clock=self.scope.clock),
            tools=tools,
            scope=self.scope,
            route=state.route,
            hooks=_edit_seam(options.hooks),
            inline=inline,
            hydrators=hydrators,
            source=options.source or options.platform,
            session_fields=options.session_fields,
            on_continued=self._continued,
            # The smart approval judge asks the session's auxiliary model (P08-14).
            approval_judge=judge_from_aux(self.aux) if self.aux is not None else None,
            note_sources=tuple(options.note_sources),
            llm_context=options.llm_context,
            aux=self.aux,
            subagent=options.delegation_depth > 0,
            delivery_origin=options.delivery_origin,
        )
        gates = options.stop_gates
        if gates is None:
            gates = standard_gates(
                self.scope, host_for=self._verify_host, pre_verify=options.pre_verify
            )
        self._runtime.stop_gates = gates
        caller = self.caller
        self.fallback = FallbackChain(
            self.scope,
            binding,
            clock=self.scope.clock,
            pools=self.pools,
            on_switch=caller.reset_streak,
        )
        self._runtime.fallback = self.fallback
        self._runtime.on_compaction.append(self._refresh_tools)
        caller.eager = self.fallback.wants_eager
        self.binding.add_dependent(prompt_identity(lifecycle))
        self.binding.add_dependent(self._follow_route)

    async def _verify_host(self, ctx: StopContext) -> ExecHost | None:
        """Where verify-on-stop runs: the session's terminal environment (None: none here)."""
        runtime = self._runtime
        environments = runtime.environments if runtime is not None else None
        if environments is None:
            return None
        _key, env = await environments.acquire(
            self.scope, task_id=ctx.owner, session_id=ctx.session_id
        )
        return env

    def _usage_sink(self, store: TranscriptStore) -> Any:
        """Where the usage ledger writes (P08-08): the session's store rows, when there is one."""
        if not isinstance(store, StateStore):
            return None
        handle = store.handle

        async def write(delta: UsageDelta, task: str) -> None:
            session_id = self.usage.session_id  # follows a rotating compaction

            def run() -> bool:
                return usage_rows.record(handle, session_id, delta, task=task)

            await run_blocking(run, label="usage")

        return write

    def _read_config(self) -> None:
        options = self.options
        state = self.binding.state
        loop = options.loop or LoopOptions.from_scope(self.scope, platform=options.platform)
        self.runtime.options = replace(
            loop,
            context_length=loop.context_length or state.context_length,
            reasoning_effort=loop.reasoning_effort or state.reasoning_effort,
        )

    def _open_context_engine(self) -> None:
        runtime = self.runtime
        state = self.binding.state
        todo = self.todo
        # P08-17's AuxClient when the host wired one (``Agent.aux``); else the main route,
        # recorded in the ledger under the ``compression`` task tag.
        binding = self.binding
        main_route = RecordingCaller(
            self.caller, self.usage, lambda: binding.state.route, task="compression"
        )
        summarizer = self.options.summarizer or AuxSummarizer(
            self.aux, fallback=caller_summarizer(main_route)
        )
        engine = build_engine(
            self.scope,
            session_id=self.session_id,
            clock=self.scope.clock,
            store=runtime.store,
            summarizer=summarizer,
            window=runtime.options.context_length or state.context_length,
            window_source=state.window.source,
            max_output=runtime.options.max_tokens,
            model=state.model,
            provider=state.provider,
            base_url=state.base_url,
            todo=(lambda: todo.todos.render()) if todo is not None else None,
        )
        self.context_engine = engine
        runtime.context = engine
        if engine is not None:
            self.usage.add_listener(engine.on_usage)

    async def _follow_route(self, before: RouteState, after: RouteState) -> None:
        """Route dependent: keep the runtime in step with the bound route (either direction)."""
        runtime = self._runtime
        if runtime is None:
            return
        runtime.route = after.route
        runtime.options = replace(
            runtime.options,
            context_length=after.context_length,
            reasoning_effort=after.reasoning_effort,
        )
        runtime.prompt_context = replace(
            runtime.prompt_context,
            model=after.model,
            provider=after.provider,
            context_length=after.context_length,
        )
        runtime.tools = self._tool_array(after)
        if runtime.context is not None:
            runtime.context.set_route(
                window=after.context_length,
                max_output=runtime.options.max_tokens,
                model=after.model,
                provider=after.provider,
                source=after.window.source,
            )

    # -- continuation (rotating compaction) -------------------------------------------------------

    def on_session_moved(self, listener: Callable[[str, str], object]) -> Callable[[], None]:
        """Call ``listener(old id, new id)`` when a rotating compaction moves the session."""
        self._moved.append(listener)
        return lambda: self._moved.remove(listener) if listener in self._moved else None

    def _continued(self, old: str, new: str) -> None:
        """The runtime moved to the continuation ``new``: follow it (see the module notes)."""
        if old == new:
            return
        if old not in self._former_ids:
            self._former_ids.append(old)
        self.sink.session_id = new
        self.usage.session_id = new
        if self.fast is not None:  # a /fast choice follows the conversation
            self.fast.table.move(
                fast_mode.session_key(self.scope, old), fast_mode.session_key(self.scope, new)
            )
        for listener in list(self._moved):
            try:
                listener(old, new)
            except Exception:
                log.warning("a session-moved listener failed", exc_info=True)

    # -- background work --------------------------------------------------------------------------

    def spawn[T](self, coro: Coroutine[Any, Any, T], *, name: str | None = None) -> asyncio.Task[T]:
        """Start ``coro`` with the agent's scope in a copied context (R01.16)."""
        task = spawn_with_scope(coro, scope=self.scope, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # -- turns ------------------------------------------------------------------------------------

    async def start_turn(
        self,
        message: Any,
        *,
        system: str | None = None,
        history: Sequence[TranscriptRow] | None = None,
        persist_text: str | None = None,
        turn_id: str | None = None,
        generation: int | None = None,
    ) -> RunningTurn:
        """Start a turn and return at once; ``await running.task`` gives the ``TurnResult``."""
        if self._closed:
            raise AgentClosed(f"the agent of session {self.session_id} is closed")
        await self.open()
        if self.current is not None:
            raise AgentBusy(f"session {self.session_id} is already running a turn")
        runtime = self.runtime
        for hook in list(runtime.before_turn):
            try:
                outcome = hook()
                if inspect.isawaitable(outcome):
                    await outcome
            except Exception:
                log.warning("a pre-turn hook of %s failed", self.session_id, exc_info=True)
        if system is not None and system != runtime.prompt_context.system_message:
            runtime.prompt_context = replace(runtime.prompt_context, system_message=system)
        number = generation if generation is not None else self._generation + 1
        self._generation = max(self._generation, number)
        ident = turn_id or f"turn_{number}_{secrets.token_hex(4)}"
        state = new_turn_state(
            runtime,
            message=message,
            turn_id=ident,
            generation=number,
            persist_text=persist_text,
            history=history,
            sink=self.sink.emit,
            broker=self.broker,
            next_seq=self.sink.next_seq,
        )
        if self.fast is not None:
            self.fast.begin_turn()  # ``auto`` asks for the priority tier for a window per turn
        task = self.spawn(run_turn(state), name=f"turn {ident}")
        running = RunningTurn(ident, number, state, task)
        self._current = running
        return running

    async def run_turn(
        self,
        message: Any,
        *,
        system: str | None = None,
        history: Sequence[TranscriptRow] | None = None,
        persist_text: str | None = None,
    ) -> TurnResult:
        """Run one turn to its end. Cancelling the caller interrupts the turn (by system)."""
        running = await self.start_turn(
            message, system=system, history=history, persist_text=persist_text
        )
        try:
            return await asyncio.shield(running.task)
        except asyncio.CancelledError:
            running.state.control.interrupt(running.generation, by_system=True)
            raise

    async def chat(self, text: str) -> str:
        """One turn; the final text (the funnel's failure text when the turn failed)."""
        result = await self.run_turn(text)
        return result.final_text or ""

    async def compress(
        self, focus: str | None = None, *, keep_last: int | None = None, preview: bool = False
    ) -> Outcome:
        """Manual ``/compress [focus] [here N] [--preview]`` between turns (P08-05).

        Clears the summary cooldown, ignores the attempt cap and the breaker, and condenses the
        stored transcript now; ``preview`` only reports what would be condensed.
        """
        if self._closed:
            raise AgentClosed(f"the agent of session {self.session_id} is closed")
        await self.open()
        if self.current is not None:
            raise AgentBusy(f"session {self.session_id} is running a turn")
        runtime = self.runtime
        engine = runtime.context
        if engine is None:
            return Outcome("nothing", reason="compression is off")
        state = new_turn_state(
            runtime,
            message="",
            turn_id=f"compress_{secrets.token_hex(4)}",
            generation=self._generation,
            sink=self.sink.emit,
            next_seq=self.sink.next_seq,
        )
        state.rows = await runtime.store.load(runtime.session_id) if runtime.store else []
        ctx = replace(runtime.prompt_context, session_id=runtime.session_id)
        state.system_prompt = await runtime.prompts.restore_or_build(
            ctx, history=[r.to_message() for r in state.rows]
        )
        await engine.on_session_start(state.rows)
        request = CompressRequest(gate="manual", focus=focus, keep_last=keep_last, preview=preview)
        return await compress_now(state, request)

    # -- control of the running turn (loop thread) -------------------------------------------------

    def interrupt(
        self, generation: int | None = None, *, hard: bool = False, by_system: bool = False
    ) -> bool:
        current = self.current
        if current is None:
            return False
        wanted = current.generation if generation is None else generation
        return current.state.control.interrupt(wanted, hard=hard, by_system=by_system)

    def steer(self, text: str, *, ticket: object | None = None) -> bool:
        current = self.current
        if current is None:
            return False
        return current.state.control.steer(text, ticket=ticket)

    def withdraw_steer(self, ticket: object) -> bool:
        current = self._current
        if current is None:
            return False
        return current.state.control.withdraw_steer(ticket)

    def redirect(self, text: str) -> RedirectOutcome:
        current = self.current
        if current is None:
            return "rejected"
        control = current.state.control
        during_tools = control.phase == "tools"
        if not control.redirect(text):
            return "rejected"
        return "steered" if during_tools else "redirected"

    # -- closing ----------------------------------------------------------------------------------

    def on_close(self, closer: Any, *, name: str = "") -> None:
        """Run ``closer()`` (plain or async) once when the agent closes, newest first."""
        self._closers.append((name or getattr(closer, "__name__", "closer"), closer))

    async def close(self) -> list[str]:
        """Give back everything the agent holds (once; see the module notes). Returns the
        failures that were logged."""
        if self._closed:
            return []
        self._closed = True
        errors: list[str] = []
        current = self.current
        if current is not None:
            current.state.control.interrupt(current.generation, by_system=True)
            try:
                await asyncio.wait_for(asyncio.shield(current.task), CLOSE_WAIT_S)
            except Exception as exc:
                errors.append(f"turn: {type(exc).__name__}: {exc}")
                current.task.cancel()
        for name, closer in reversed(self._closers):
            try:
                outcome = closer()
                if asyncio.iscoroutine(outcome):
                    await outcome
            except Exception as exc:
                errors.append(f"{name}: {type(exc).__name__}: {exc}")
                log.warning("closer %s of session %s failed", name, self.session_id, exc_info=True)
        if self._sink is not None:
            registry = self.options.cleanups or TASK_CLEANUPS
            for former in self._former_ids:  # cleanups registered before a continuation
                errors.extend(await registry.run(former))
            errors.extend(await registry.run(self.session_id))
        for task in list(self._tasks):
            task.cancel()
        for client in self._owned_clients:
            try:
                await client.aclose()
            except Exception as exc:
                errors.append(f"client: {type(exc).__name__}: {exc}")
                log.warning("closing a model client failed", exc_info=True)
        self._owned_clients.clear()
        self.close_errors = errors
        return errors
