"""The state of one turn, passed to the loop's phase functions (no reflection, no rebinding).

Two layers:

- ``SessionRuntime`` lives as long as the session does inside a process (the Agent of P04-20 owns
  it): the provider caller, the store, the tool registry and tool array, the prompt lifecycle, the
  wire heal ledger and argument memo, inline tool executors and history hydrators, and the
  ``LoopOptions``. ``environments`` is the process's terminal ``EnvironmentRegistry``; the host
  sets it (P06 wiring) and the tool round offers it to handlers as ``environments``.
- ``TurnState`` is one turn: the live rows, counters, the control handle (interrupt / steer /
  redirect), the outcome. ``new_turn_state`` is the documented factory the Agent calls; then
  ``await indus.core.loop.run_turn(state)``.

Rows are stored in list order: ``flush`` writes every row that is neither scaffold nor stored yet
(``row_id`` is ``None``) and waits for each commit. A store failure becomes ``PersistFailed``;
anything that is not an ``Exception`` (process death in tests) passes through untouched.
"""

from __future__ import annotations

import itertools
import logging
import random
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from indus.backends.registry import EnvironmentRegistry
from indus.core.budget import DEFAULT_MAX_ITERATIONS, IterationBudget, max_iterations_from
from indus.core.exit_reasons import ExitReason, Failure
from indus.core.guardrails import GuardDecision, GuardrailConfig, ToolGuard
from indus.core.interrupts import TurnControl
from indus.core.persist import TranscriptStore
from indus.core.response_checks import ThinkScrubber, TurnChecks, scrubber_for
from indus.core.tool_exec import ExecLimits
from indus.core.transcript import TranscriptRow
from indus.core.wire import ArgsMemo, HealLedger
from indus.protocol.events import EventBase
from indus.protocol.requests_port import ServerRequestPort
from indus.tools.budget import Budget, budget_for_window
from indus.tools.calls import ToolCall, ToolResult
from indus.tools.hooks import NO_HOOKS, ToolHooks
from indus.tools.registry import ToolRegistry
from indus.tools.spill import SpillSink

if TYPE_CHECKING:
    from indus.core.model_call import ModelCaller
    from indus.core.prompt.builder import BuildContext
    from indus.core.prompt.lifecycle import PromptLifecycle
    from indus.foundation.clock import Clock
    from indus.foundation.scope import Scope

log = logging.getLogger(__name__)

__all__ = [
    "EventSink",
    "Hydrator",
    "InlineExecutor",
    "LoopOptions",
    "PersistFailed",
    "SessionRuntime",
    "TurnResult",
    "TurnState",
    "new_turn_state",
]

type EventSink = Callable[[EventBase], object]
type InlineExecutor = Callable[[ToolCall, TurnState], Awaitable[ToolResult]]
type Hydrator = Callable[[Sequence[TranscriptRow]], object]


class PersistFailed(Exception):
    """A transcript write failed; the turn ends ``persistence_failed``."""


@dataclass(frozen=True)
class LoopOptions:
    """Numbers and switches for the loop. ``from_scope`` reads ``agent.max_turns``."""

    max_iterations: int = DEFAULT_MAX_ITERATIONS
    max_restarts: int = 8
    max_outer_errors: int = 8
    max_invalid_strikes: int = 3
    max_bad_json_retries: int = 3
    max_child_calls: int = 3
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    context_length: int | None = None
    model_output_limit: int | None = None
    intent_ack: bool = True
    platform: str = "cli"
    exec_limits: ExecLimits = field(default_factory=ExecLimits)
    guardrails: GuardrailConfig | None = None
    result_budget: Budget | None = None
    rng_seed: int | None = None

    @classmethod
    def from_scope(cls, scope: Scope | None, **values: Any) -> LoopOptions:
        if scope is not None and scope.config is not None and "max_iterations" not in values:
            try:
                values["max_iterations"] = max_iterations_from(scope.config.get("agent.max_turns"))
            except Exception:
                log.debug("agent.max_turns unreadable; using the default", exc_info=True)
        return cls(**values)

    def guard_config(self) -> GuardrailConfig:
        return self.guardrails or GuardrailConfig.for_platform(self.platform)

    def budget(self) -> Budget:
        return self.result_budget or budget_for_window(self.context_length)


@dataclass
class SessionRuntime:
    """Per-session pieces the Agent keeps across turns (see the module notes)."""

    session_id: str
    caller: ModelCaller
    clock: Clock
    prompts: PromptLifecycle
    prompt_context: BuildContext
    store: TranscriptStore | None = None
    registry: ToolRegistry = field(default_factory=ToolRegistry)
    tools: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    scope: Scope | None = None
    route: object = None
    options: LoopOptions = field(default_factory=LoopOptions)
    hooks: ToolHooks = NO_HOOKS
    inline: Mapping[str, InlineExecutor] = field(default_factory=dict[str, InlineExecutor])
    hydrators: Mapping[str, Hydrator] = field(default_factory=dict[str, Hydrator])
    spill_sink: SpillSink | None = None
    source: str = ""
    session_fields: Mapping[str, Any] = field(default_factory=dict[str, Any])
    heals: HealLedger = field(default_factory=HealLedger)
    memo: ArgsMemo = field(default_factory=ArgsMemo)
    hydrated: bool = False
    profile: object = None
    environments: EnvironmentRegistry | None = None

    def tool_names(self) -> frozenset[str]:
        names: set[str] = set(self.inline)
        for tool in self.tools:
            fn = tool.get("function")
            if isinstance(fn, Mapping):
                name = fn.get("name")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
                if isinstance(name, str):
                    names.add(name)
        return frozenset(names)


@dataclass
class TurnResult:
    """What a turn returns. ``current_turn_user_index`` addresses ``rows``."""

    session_id: str
    turn_id: str
    generation: int
    exit_reason: ExitReason
    final_text: str | None
    rows: list[TranscriptRow]
    current_turn_user_index: int
    api_calls: int
    failure: Failure | None = None
    reasoning: str | None = None
    pending_steer: str | None = None
    guardrail: GuardDecision | None = None
    intervened: bool = False
    cleanup_errors: tuple[str, ...] = ()

    @property
    def failure_reason(self) -> str | None:
        return self.failure.reason if self.failure else None

    @property
    def failure_retryable(self) -> bool | None:
        return self.failure.retryable if self.failure else None


@dataclass
class TurnState:
    runtime: SessionRuntime
    turn_id: str
    message: Any
    persist_text: str | None = None
    history: list[TranscriptRow] | None = None
    control: TurnControl = field(default_factory=TurnControl)
    sink: EventSink | None = None
    next_seq: Callable[[], int] = field(default_factory=lambda: itertools.count().__next__)
    rows: list[TranscriptRow] = field(default_factory=list[TranscriptRow])
    user_index: int = -1
    budget: IterationBudget = field(default_factory=IterationBudget)
    checks: TurnChecks = field(default_factory=TurnChecks)
    guard: ToolGuard = field(default_factory=ToolGuard)
    scrubber: ThinkScrubber = field(default_factory=scrubber_for)
    rng: random.Random = field(default_factory=random.Random)
    api_calls: int = 0
    restart_count: int = 0
    outer_errors: int = 0
    invalid_strikes: int = 0
    bad_json_retries: int = 0
    grace: bool = False
    grace_used: bool = False
    output_cap: int | None = None
    last_cap: int | None = None
    reasoning_off: bool = False
    streamed_text: str = ""
    final_text: str | None = None
    exit: ExitReason | None = None
    failure: Failure | None = None
    last_activity: float = 0.0
    activity_note: str = ""
    cleanup_errors: list[str] = field(default_factory=list[str])
    pending_steer: str | None = None
    turn_tool_chars: int = 0
    stored_at_start: int = 0
    system_prompt: str = ""

    # -- views -----------------------------------------------------------------------------------

    @property
    def generation(self) -> int:
        return self.control.generation

    @property
    def session_id(self) -> str:
        return self.runtime.session_id

    @property
    def clock(self) -> Clock:
        return self.runtime.clock

    @property
    def options(self) -> LoopOptions:
        return self.runtime.options

    @property
    def ended(self) -> bool:
        return self.exit is not None

    def tail_role(self) -> str | None:
        for row in reversed(self.rows):
            if not row.scaffold:
                return row.role
        return None

    def turn_rows(self) -> list[TranscriptRow]:
        return self.rows[self.user_index + 1 :] if self.user_index >= 0 else []

    # -- outcome -----------------------------------------------------------------------------------

    def end(
        self,
        reason: ExitReason,
        *,
        final_text: str | None = None,
        failure: Failure | None = None,
    ) -> None:
        """Record why the turn ends; the first call wins."""
        if self.exit is not None:
            return
        self.exit = reason
        self.failure = failure
        if final_text is not None:
            self.final_text = final_text

    # -- rows --------------------------------------------------------------------------------------

    def add(self, *rows: TranscriptRow) -> None:
        for row in rows:
            if row.timestamp is None and not row.scaffold:
                row.timestamp = self.clock.now().timestamp()
            self.rows.append(row)

    def drop_scaffold(self, *, this_turn_only: bool = True) -> None:
        start = self.user_index + 1 if this_turn_only and self.user_index >= 0 else 0
        kept = self.rows[:start] + [r for r in self.rows[start:] if not r.scaffold]
        self.rows[:] = kept

    async def flush(self) -> None:
        """Store every pending durable row, in order; ``PersistFailed`` on a store error."""
        store = self.runtime.store
        for row in self.rows:
            if row.scaffold or row.row_id is not None:
                continue
            if store is None:
                row.row_id = 0
                continue
            try:
                row.row_id = await store.append(self.session_id, row)
            except Exception as exc:
                log.warning(
                    "could not store a %s row of session %s: %s", row.role, self.session_id, exc
                )
                raise PersistFailed(str(exc)) from exc

    # -- side channels -----------------------------------------------------------------------------

    def emit(self, model: type[EventBase], **fields: Any) -> None:
        """Send one event to the sink; a failing sink is logged, never fatal."""
        if self.sink is None:
            return
        try:
            event = model(session_id=self.session_id, seq=self.next_seq(), **fields)
            self.sink(event)
        except Exception:
            log.warning("event sink failed for %s", model.__name__, exc_info=True)

    def touch(self, note: str) -> None:
        self.last_activity = self.clock.monotonic()
        self.activity_note = note


def new_turn_state(
    runtime: SessionRuntime,
    *,
    message: Any,
    turn_id: str,
    generation: int = 0,
    persist_text: str | None = None,
    history: Sequence[TranscriptRow] | None = None,
    sink: EventSink | None = None,
    broker: ServerRequestPort | None = None,
    next_seq: Callable[[], int] | None = None,
    budget: IterationBudget | None = None,
) -> TurnState:
    """Build the state for one turn.

    ``history``: the conversation so far (``None`` loads it from the runtime's store in the
    prologue). ``persist_text``: the clean text to store when ``message`` carries API-only
    prefixes. ``budget``: a shared iteration budget (delegation); a fresh one by default.
    """
    options = runtime.options
    state = TurnState(
        runtime=runtime,
        turn_id=turn_id,
        message=message,
        persist_text=persist_text,
        history=[r.clone() for r in history] if history is not None else None,
        control=TurnControl(generation=generation, broker=broker),
        sink=sink,
        budget=budget or IterationBudget(options.max_iterations),
        guard=ToolGuard(options.guard_config()),
        scrubber=scrubber_for(runtime.profile),
        rng=random.Random(options.rng_seed),
    )
    if next_seq is not None:
        state.next_seq = next_seq
    return state
