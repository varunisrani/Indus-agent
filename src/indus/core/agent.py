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
7. ``context_engine`` — an empty slot P08 fills.

``run_turn`` runs one turn and returns its ``TurnResult``; ``chat`` returns just the final text.
While a turn runs, ``interrupt`` / ``steer`` / ``redirect`` reach it (loop thread only). Every task
the agent starts goes through ``spawn``, which runs it with the agent's own ``Scope`` in a copied
context, whatever scope the caller has (R01.16). ``close`` stops a running turn, runs the agent's
closers and the task cleanups tool backends registered, and closes clients the agent owns; every
step is guarded and the whole thing runs once.

R01.5: the caller reads ``binding.state`` once per request. A request built for another model is
not sent, and a retry is never sent once the binding's generation moved: the call ends
``RouteChanged`` and the loop rebuilds the request for the new route.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Coroutine, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

import httpx

from indus.core.cleanup import TASK_CLEANUPS, CleanupRegistry
from indus.core.events import EventListener, EventSink, as_sink
from indus.core.loop import run_turn
from indus.core.model_call import OnEvent, OnWait, RouteCaller
from indus.core.persist import MemoryStore, StateStore, TranscriptStore
from indus.core.prompt.builder import BuildContext, PromptBuilder
from indus.core.prompt.lifecycle import Builder, PromptLifecycle, PromptStore, SessionPrompts
from indus.core.route import ClientFactory, RouteBinding, RouteState, pooled_client, prompt_identity
from indus.core.transcript import TranscriptRow
from indus.core.turn_state import (
    Hydrator,
    InlineExecutor,
    LoopOptions,
    SessionRuntime,
    TurnResult,
    TurnState,
    new_turn_state,
)
from indus.foundation.clock import Clock
from indus.foundation.errors import IndusError
from indus.foundation.ids import new_session_id
from indus.foundation.scope import Scope, spawn_with_scope
from indus.protocol.requests_port import ServerRequestPort
from indus.providers.context_length import ListModels
from indus.providers.retry import DEFAULT_MAX_RETRIES, clamp_max_retries
from indus.providers.route import RuntimeRoute
from indus.providers.streaming import CallOutcome, RouteChanged, StaleStreak, StreamFlags
from indus.providers.timeouts import TimeoutPolicy
from indus.providers.types import ChatRequest
from indus.tools import toolsets
from indus.tools.builtin import load_builtins
from indus.tools.builtin.todo import TOOL_NAME as TODO_TOOL
from indus.tools.builtin.todo import TodoWiring, todo_wiring
from indus.tools.definitions import definitions
from indus.tools.hooks import NO_HOOKS, ToolHooks
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
    _flags: dict[tuple[str, str, str], StreamFlags] = field(
        default_factory=dict[tuple[str, str, str], StreamFlags]
    )
    _streaks: dict[tuple[str, str, str], StaleStreak] = field(
        default_factory=dict[tuple[str, str, str], StaleStreak]
    )

    @property
    def model(self) -> str:
        return self.binding.state.model

    def caller_for(self, state: RouteState, generation: int) -> RouteCaller:
        key = (state.provider, state.base_url, state.model)
        binding = self.binding
        return RouteCaller(
            client=state.client,
            route=state.route,
            clock=self.clock,
            policy=self.policy,
            max_retries=self.max_retries,
            flags=self._flags.setdefault(key, StreamFlags()),
            streak=self._streaks.setdefault(key, StaleStreak()),
            epoch=lambda: binding.generation,
            still_current=lambda: binding.generation == generation,
        )

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
        caller = self.caller_for(state, generation)
        return await caller.call(request, on_event=on_event, on_wait=on_wait)


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
        self.context_engine: object | None = None
        self._route_in = route
        self._sink_in = sink
        self._binding: RouteBinding | None = route if isinstance(route, RouteBinding) else None
        self._sink: EventSink | None = None
        self._caller: BoundCaller | None = None
        self._runtime: SessionRuntime | None = None
        self._registry: ToolRegistry | None = None
        self._selected: list[str] = []
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
            self.context_engine = None  # P08 puts the compressor here
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
        self._caller = BoundCaller(
            self.binding,
            self.scope.clock,
            max_retries=self._max_retries(),
            policy=self.options.policy,
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
        self._selected = sorted(chosen.tools)

    def _tool_array(self, state: RouteState) -> list[dict[str, Any]]:
        assert self._registry is not None
        return definitions(self._registry, self._selected, state.route, scope=self.scope)

    def _open_session(self) -> None:
        options = self.options
        state = self.binding.state
        store = options.store if options.store is not None else MemoryStore()
        prompt_store = options.prompt_store
        if prompt_store is None and isinstance(store, StateStore):
            prompt_store = SessionPrompts(store.handle)
        lifecycle = PromptLifecycle(options.builder or PromptBuilder(), store=prompt_store)
        tools = self._tool_array(state)
        inline, hydrators = dict(options.inline), dict(options.hydrators)
        if TODO_TOOL in self._selected and TODO_TOOL not in inline:
            # The session's task list (P05-24): run by the loop, rebuilt from history on resume.
            self.todo = todo_wiring()
            inline[TODO_TOOL] = self.todo.execute
            hydrators.setdefault("todo", self.todo.hydrate)
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
        self._runtime = SessionRuntime(
            session_id=self.session_id,
            caller=self.caller,
            clock=self.scope.clock,
            prompts=lifecycle,
            prompt_context=context,
            store=store,
            registry=self._registry or ToolRegistry(clock=self.scope.clock),
            tools=tools,
            scope=self.scope,
            route=state.route,
            hooks=options.hooks,
            inline=inline,
            hydrators=hydrators,
            source=options.source or options.platform,
            session_fields=options.session_fields,
        )
        self.binding.add_dependent(prompt_identity(lifecycle))
        self.binding.add_dependent(self._follow_route)

    def _read_config(self) -> None:
        options = self.options
        state = self.binding.state
        loop = options.loop or LoopOptions.from_scope(self.scope, platform=options.platform)
        self.runtime.options = replace(
            loop,
            context_length=loop.context_length or state.context_length,
            reasoning_effort=loop.reasoning_effort or state.reasoning_effort,
        )

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
