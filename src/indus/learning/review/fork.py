"""The review fork (P09-15): replay the conversation in the background and learn from it.

``ReviewRunner`` is one session's review machinery. ``start(request)`` (the P09-14 trigger, or
``/refine``) runs a review as a task started through the parent's ``spawn`` (``Agent.spawn`` copies
the ``Scope`` and context variables, R01.16); ``run(request)`` is the same, awaited.

The fork is a real turn of our loop over a *forked* ``SessionRuntime``:

- shared with the parent: the tool registry, a deep copy of the tool array (advertised tools stay
  byte-identical), loop options (output cap, reasoning effort), copies of the wire heal ledger and
  argument memo, and the parent's stored prompt bytes (served by a fixed builder). On the same
  model the first request is therefore a warm prefix of the parent's last one;
- its own: an in-memory transcript store and session id (``<parent>:review:<hex>``), so nothing
  reaches the parent's transcript and read-before-write evidence starts empty; no event sink, no
  broker (approval prompts never reach a surface: approvals deny), no context engine, no fallback
  chain and no post-turn hooks — so a review never schedules a review (R07.5).

Route: the auxiliary client's candidates for task ``review`` (P08-17, ``auxiliary.review``). The
main route (``auto``) or a pin naming the same provider, model and endpoint → the parent's caller,
recording usage under task ``review`` on the parent session, with the parent's reasoning effort (a
configured effort is ignored, with one warning). Any other route → every request goes through
``AuxClient.call("review", ..., tools=...)`` with the configured effort, and the history is replayed
as a digest (``digest_rows``) because the cache is cold anyway.

Limits: at most ``MAX_ITERATIONS`` (16) model requests — the loop's cap, plus ``GuardedCaller``
refusing anything past it, the loop's grace request included; and an input budget (the sum of input
tokens over the review's requests): ``learning.review.max_input_tokens`` when set (≤ 0: none),
else 75 % of the review model's window capped at 600,000, or 120,000 when the window is unknown.
A request is refused when the tokens spent plus the last request's input would cross it.

Dispatch whitelist (advertised is not permitted): ``skill_manage``, ``skill_view``,
``skills_list``, ``read_file``, ``search_files``, ``memory`` when the review covers memory, and the
``learning.review.extra_tools`` the parent offers. Inline executors are filtered to it and registry
calls go through ``WhitelistHooks``. Writes run with the origin ``background_review``
(``learning.pending.bind_origin``), attended only for ``/refine``.

A live turn calls ``cancel_for_live_turn()``: the fork is interrupted and given 2.0 s (on the
injected clock) to finish; past that the live turn goes ahead anyway. With ``learning.review.defer:
auto`` an automatic review bound for a local server waits in the ``IdleQueue`` instead.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import secrets
from collections.abc import Callable, Collection, Coroutine, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal

from indus.core.loop import run_turn
from indus.core.persist import MemoryStore
from indus.core.prompt.builder import BuildContext, PromptParts
from indus.core.prompt.lifecycle import PromptLifecycle, with_identity
from indus.core.transcript import TranscriptRow, call_name
from indus.core.turn_state import SessionRuntime, TurnState, new_turn_state
from indus.core.usage import RecordingCaller
from indus.core.wire import ArgsMemo, HealLedger
from indus.foundation.errors import ProviderError
from indus.foundation.scope import spawn_with_scope
from indus.learning import pending
from indus.learning.review.counters import KINDS, ReviewRequest, ReviewTrigger, review_enabled
from indus.learning.review.idle_queue import DEFAULT_MAX_AGE_S, Deferred, IdleQueue
from indus.learning.review.prompts import build_review_prompt
from indus.learning.review.summary import Mode, emit_done, log_completion, mode_from, summary_lines
from indus.learning.skills.view_tools import READS
from indus.providers.route import RuntimeRoute
from indus.providers.streaming import CallOutcome, CallResult
from indus.providers.types import ChatRequest
from indus.tools.calls import ToolCall, ToolContext, ToolResult
from indus.tools.hooks import BeforeVerdict, ToolHooks, run_after, run_before, run_transform

if TYPE_CHECKING:
    from indus.core.agent import Agent
    from indus.core.events import EventSink
    from indus.core.model_call import ModelCaller, OnEvent, OnWait
    from indus.foundation.clock import Clock
    from indus.foundation.scope import Scope
    from indus.providers.auxiliary import AuxClient

__all__ = [
    "ASSISTANT_CLIP",
    "BUDGET_CAP",
    "BUDGET_FRACTION",
    "BUDGET_UNKNOWN",
    "CANCEL_WAIT_S",
    "DIGEST_TAIL",
    "MAX_ITERATIONS",
    "READ_TOOLS",
    "SKILL_TOOLS",
    "TASK",
    "USER_CLIP",
    "AuxCaller",
    "GuardedCaller",
    "ReviewOutcome",
    "ReviewParent",
    "ReviewRunner",
    "ReviewSettings",
    "ReviewStopped",
    "WhitelistHooks",
    "allowed_tools",
    "digest_rows",
    "input_budget",
    "install_review",
]

log = logging.getLogger(__name__)

TASK = "review"
MAX_ITERATIONS = 16
BUDGET_CAP = 600_000
BUDGET_FRACTION = 0.75
BUDGET_UNKNOWN = 120_000
DIGEST_TAIL = 24
USER_CLIP = 300
ASSISTANT_CLIP = 200
CANCEL_WAIT_S = 2.0
SKILL_TOOLS = ("skill_manage", "skill_view", "skills_list")
READ_TOOLS = ("read_file", "search_files")
MEMORY_TOOL = "memory"


# -- settings -------------------------------------------------------------------------------------


def _get(scope: Scope | None, key: str) -> Any:
    config = scope.config if scope is not None else None
    if config is None:
        return None
    try:
        return config.get(key)
    except Exception:
        log.debug("%s unreadable", key, exc_info=True)
        return None


def _int_or_none(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)  # pyright: ignore[reportArgumentType]
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class ReviewSettings:
    """``learning.review.*``, ``auxiliary.review.reasoning_effort`` and the notices mode."""

    enabled: bool = True
    extra_tools: tuple[str, ...] = ()
    max_input_tokens: int | None = None
    defer: Literal["auto", "never"] = "auto"
    defer_max_age_s: float = DEFAULT_MAX_AGE_S
    notices: Mode = "on"
    configured_effort: str = ""

    @classmethod
    def from_scope(cls, scope: Scope | None) -> ReviewSettings:
        extra = _get(scope, "learning.review.extra_tools")
        names: tuple[str, ...] = ()
        if isinstance(extra, list):
            names = tuple(
                n.strip()
                for n in extra
                if isinstance(n, str) and n.strip()  # pyright: ignore[reportUnknownVariableType]
            )
        defer = str(_get(scope, "learning.review.defer") or "auto").strip().lower()
        age = _get(scope, "learning.review.defer_max_age_s")
        try:
            max_age = float(age) if age is not None and not isinstance(age, bool) else 0.0
        except (TypeError, ValueError):
            max_age = 0.0
        effort = _get(scope, "auxiliary.review.reasoning_effort")
        return cls(
            enabled=review_enabled(scope),
            extra_tools=names,
            max_input_tokens=_int_or_none(_get(scope, "learning.review.max_input_tokens")),
            defer="never" if defer == "never" else "auto",
            defer_max_age_s=max_age if max_age > 0 else DEFAULT_MAX_AGE_S,
            notices=mode_from(scope),
            configured_effort=effort.strip() if isinstance(effort, str) else "",
        )


def input_budget(explicit: int | None, window: int | None) -> int | None:
    """The review's total input-token budget; ``None`` means no limit (explicit ≤ 0)."""
    if explicit is not None:
        return explicit if explicit > 0 else None
    if window is None or window <= 0:
        return BUDGET_UNKNOWN
    return min(BUDGET_CAP, max(1, int(window * BUDGET_FRACTION)))


def allowed_tools(
    kinds: Iterable[str], offered: Collection[str], extra: Iterable[str] = ()
) -> frozenset[str]:
    """The tools a review may run: the skill and read tools, ``memory`` for memory reviews, and
    configured extras — each only when the parent offers it."""
    wanted = {*SKILL_TOOLS, *READ_TOOLS, *extra}
    if MEMORY_TOOL in set(kinds):
        wanted.add(MEMORY_TOOL)
    elif MEMORY_TOOL in set(extra):
        wanted.discard(MEMORY_TOOL)  # a skill-only review never touches memory
    return frozenset(name for name in wanted if name in offered)


# -- the digest (routed reviews) ------------------------------------------------------------------


def _text_of(row: TranscriptRow) -> str:
    content = row.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:  # pyright: ignore[reportUnknownVariableType]
            if isinstance(part, Mapping):
                text = part.get("text")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
                if isinstance(text, str):
                    parts.append(text)
        return " ".join(parts)
    return ""


def _clip(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


DIGEST_HEAD = (
    "[Summary of the earlier part of this conversation. The most recent messages follow unchanged.]"
)


def digest_rows(rows: Sequence[TranscriptRow], tail: int = DIGEST_TAIL) -> list[TranscriptRow]:
    """``rows`` for a review on another model: the last ``tail`` rows as they are, after one user
    row listing the older turns. The kept run is extended back until it starts on an assistant
    row: never on a tool result (its call would be missing), and never on a user row (two user
    messages in a row would be joined)."""
    kept_from = max(0, len(rows) - tail)
    while 0 < kept_from < len(rows) and rows[kept_from].role != "assistant":
        kept_from -= 1
    if kept_from == 0:
        return [copy.deepcopy(r) for r in rows]
    lines: list[str] = []
    for row in rows[:kept_from]:
        text = _text_of(row)
        if row.role == "user" and text.strip():
            lines.append(f"User: {_clip(text, USER_CLIP)}")
        elif row.role == "assistant":
            if row.tool_calls:
                names = ", ".join(call_name(c) or "?" for c in row.tool_calls)
                lines.append(f"Assistant used tools: {names}")
            if text.strip():
                lines.append(f"Assistant: {_clip(text, ASSISTANT_CLIP)}")
    summary = TranscriptRow(role="user", content=DIGEST_HEAD + "\n" + "\n".join(lines))
    return [summary, *(copy.deepcopy(r) for r in rows[kept_from:])]


# -- the dispatch-side whitelist ------------------------------------------------------------------


def _deny_text(name: str, allowed: Collection[str]) -> str:
    return (
        f"{name} is not available in this background review. Allowed here: "
        f"{', '.join(sorted(allowed))}. Do not call {name} again."
    )


@dataclass
class WhitelistHooks:
    """``ToolHooks`` that block every tool outside ``allowed``, then defer to ``inner``."""

    allowed: frozenset[str]
    inner: ToolHooks
    denied: list[str] = field(default_factory=list[str])

    @property
    def empty(self) -> bool:
        return False

    async def before(self, call: ToolCall, ctx: ToolContext) -> BeforeVerdict | None:
        if call.name not in self.allowed:
            self.denied.append(call.name)
            log.info("background review refused %s (not on its tool list)", call.name)
            return BeforeVerdict(block=_deny_text(call.name, self.allowed))
        return await run_before(self.inner, call, ctx)

    async def after(
        self, call: ToolCall, ctx: ToolContext, result: ToolResult, duration_s: float
    ) -> None:
        await run_after(self.inner, call, ctx, result, duration_s)

    async def transform(
        self, call: ToolCall, ctx: ToolContext, result: ToolResult
    ) -> ToolResult | None:
        return await run_transform(self.inner, call, ctx, result)


# -- callers --------------------------------------------------------------------------------------


class ReviewStopped(ProviderError):
    """The review used its request cap or its input budget; nothing more is sent."""

    default_code = "review.stopped"


@dataclass
class GuardedCaller:
    """The fork's ``ModelCaller``: counts requests and input tokens and refuses the one that
    would pass the cap or cross the budget (see the module notes)."""

    inner: ModelCaller
    max_requests: int = MAX_ITERATIONS
    budget: int | None = None
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    last_input: int = 0
    stopped: str = ""

    @property
    def model(self) -> str:
        return self.inner.model

    def _refuse(self, why: str, text: str) -> ReviewStopped:
        self.stopped = self.stopped or why
        log.info("background review stopped: %s", text)
        return ReviewStopped(text)

    async def call(
        self, request: ChatRequest, *, on_event: OnEvent, on_wait: OnWait | None = None
    ) -> CallOutcome:
        if self.requests >= self.max_requests:
            raise self._refuse("iterations", f"the review used its {self.max_requests} requests")
        budget = self.budget
        if budget is not None and self.requests and self.input_tokens + self.last_input > budget:
            raise self._refuse(
                "budget",
                f"the next request would pass the review's {budget}-token input budget "
                f"({self.input_tokens} spent)",
            )
        self.requests += 1
        outcome = await self.inner.call(request, on_event=on_event, on_wait=on_wait)
        if isinstance(outcome, CallResult) and outcome.response.usage is not None:
            usage = outcome.response.usage
            self.last_input = max(0, usage.prompt_tokens)
            self.input_tokens += self.last_input
            self.output_tokens += max(0, usage.completion_tokens)
            self.cache_read_tokens += max(0, usage.cache_read_tokens)
        return outcome


@dataclass
class AuxCaller:
    """A ``ModelCaller`` over the auxiliary client for a review routed to another model."""

    aux: AuxClient
    route: RuntimeRoute
    effort: str | None = None

    @property
    def model(self) -> str:
        return self.route.model

    async def call(
        self, request: ChatRequest, *, on_event: OnEvent, on_wait: OnWait | None = None
    ) -> CallOutcome:
        result = await self.aux.call(
            TASK,
            request.messages,
            tools=request.tools,
            max_tokens=request.max_tokens,
            reasoning_effort=self.effort,
        )
        return CallResult(result.response, streamed=False)


class _FixedPrompt:
    """A prompt builder that always gives the parent's stored prompt bytes."""

    def __init__(self, text: str) -> None:
        self.text = text

    async def build(self, ctx: BuildContext) -> PromptParts:
        return PromptParts(stable=self.text, context="", volatile="", text=self.text)


# -- the parent and the outcome -------------------------------------------------------------------

type Spawn = Callable[[Coroutine[Any, Any, Any]], asyncio.Task[Any]]


@dataclass
class ReviewParent:
    """What a review takes from its session: the runtime, the same-model caller (recording under
    task ``review``), the auxiliary client, the agent's ``spawn`` and the event sink."""

    runtime: SessionRuntime
    caller: ModelCaller
    aux: AuxClient | None = None
    spawn: Spawn | None = None
    sink: EventSink | None = None

    @property
    def scope(self) -> Scope | None:
        return self.runtime.scope

    @property
    def clock(self) -> Clock:
        return self.runtime.clock

    @classmethod
    def from_agent(cls, agent: Agent) -> ReviewParent:
        binding = agent.binding
        caller = RecordingCaller(agent.caller, agent.usage, lambda: binding.state.route, task=TASK)

        def spawn(coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
            return agent.spawn(coro, name="background review")

        return cls(agent.runtime, caller, agent.aux, spawn, agent.sink)


@dataclass(frozen=True)
class ReviewOutcome:
    """What one review did. ``status``: ``done``, ``cancelled``, ``skipped`` or ``error``."""

    status: str
    kinds: frozenset[str] = frozenset()
    summary_lines: tuple[str, ...] = ()
    rows: tuple[TranscriptRow, ...] = ()
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    stopped: str = ""
    routed: bool = False
    denied: tuple[str, ...] = ()
    fork_session_id: str = ""
    reason: str = ""


@dataclass(frozen=True)
class _Route:
    routed: bool
    caller: ModelCaller
    route: RuntimeRoute | None
    effort: str | None


def _same_route(a: RuntimeRoute, b: RuntimeRoute) -> bool:
    return (a.provider, a.model, a.base_url) == (b.provider, b.model, b.base_url)


async def _settled(task: asyncio.Task[Any], clock: Clock, seconds: float) -> bool:
    """Wait up to ``seconds`` on ``clock`` for ``task``; True when it finished."""
    if task.done():
        return True
    timer = asyncio.ensure_future(clock.sleep(seconds))
    try:
        await asyncio.wait({task, timer}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        timer.cancel()
    return task.done()


def _copy_or[T](value: T, fresh: Callable[[], T]) -> T:
    try:
        return copy.deepcopy(value)
    except Exception:
        return fresh()


# -- the runner -----------------------------------------------------------------------------------


class ReviewRunner:
    """One session's background reviews (see the module notes)."""

    def __init__(
        self, parent: ReviewParent, *, queue: IdleQueue[ReviewRequest] | None = None
    ) -> None:
        self.parent = parent
        self._queue = queue
        self._pump: asyncio.Task[Any] | None = None
        self._task: asyncio.Task[Any] | None = None
        self._state: TurnState | None = None
        self._deferred: Deferred[ReviewRequest] | None = None
        self._cancelled = False
        self._closed = False
        self._warned: set[str] = set()
        self.last: ReviewOutcome | None = None
        self.trigger: ReviewTrigger | None = None
        """The session's counters and trigger once ``install_review`` made them."""

    # -- views ------------------------------------------------------------------------------------

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def queue(self) -> IdleQueue[ReviewRequest]:
        if self._queue is None:
            self._queue = IdleQueue(self.parent.clock, self._dispatch_deferred)
        return self._queue

    def _warn_once(self, key: str, text: str, *args: object) -> None:
        if key in self._warned:
            return
        self._warned.add(key)
        log.warning(text, *args)

    # -- starting ---------------------------------------------------------------------------------

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        if self.parent.spawn is not None:
            return self.parent.spawn(coro)
        return spawn_with_scope(coro, scope=self.parent.scope, name="background review")

    def start(self, request: ReviewRequest) -> asyncio.Task[ReviewOutcome] | None:
        """Start ``request`` in the background (or defer it); ``None`` when not started now."""
        if self._closed:
            return None
        if self.busy:
            log.debug("a review of session %s is still running; skipping", request.session_id)
            return None
        settings = ReviewSettings.from_scope(self.parent.scope)
        if request.reason == "auto" and not request.attended and settings.defer == "auto":
            choice = self._route(settings, warn=False)
            route = choice.route
            if route is not None and route.is_local:
                self._defer(request, settings)
                return None
        return self._launch(request)

    def _launch(
        self, request: ReviewRequest, deferred: Deferred[ReviewRequest] | None = None
    ) -> asyncio.Task[ReviewOutcome]:
        self._cancelled = False
        self._deferred = deferred
        task: asyncio.Task[ReviewOutcome] = self._spawn(self.run(request))
        self._task = task
        return task

    def _defer(self, request: ReviewRequest, settings: ReviewSettings) -> None:
        queue = self.queue
        queue.enqueue(request.session_id, request, max_age_s=settings.defer_max_age_s)
        if self._pump is None or self._pump.done():
            self._pump = self._spawn(queue.run())

    def _dispatch_deferred(self, entry: Deferred[ReviewRequest]) -> None:
        if self._closed:
            return
        if not review_enabled(self.parent.scope):
            log.info("a deferred review was dropped: reviews were turned off meanwhile")
            return
        if self.busy:
            self.queue.requeue(entry)
            return
        self._launch(entry.item, entry)

    # -- live turns -------------------------------------------------------------------------------

    def turn_finished(self, state: TurnState | None = None) -> None:
        """Post-turn hook: the process is quiet again (for the deferral queue)."""
        if self._queue is not None:
            self._queue.note_turn_finished()

    async def cancel_for_live_turn(self) -> bool:
        """Stop a running review before a live turn (waits up to 2.0 s); True when one ran."""
        if self._queue is not None:
            self._queue.note_turn_started()
        task = self._task
        if task is None or task.done():
            return False
        self._cancelled = True
        state = self._state
        if state is not None:
            state.control.interrupt(state.generation, hard=True, by_system=True)
        if not await _settled(task, self.parent.clock, CANCEL_WAIT_S):
            log.warning(
                "the background review did not stop within %.1f s; the live turn goes ahead",
                CANCEL_WAIT_S,
            )
        if self._deferred is not None:
            self.queue.requeue(self._deferred)
            self._deferred = None
        return True

    async def close(self) -> None:
        """Stop everything (session closing); waits up to 2.0 s for a running review."""
        self._closed = True
        if self._pump is not None:
            self._pump.cancel()
        await self.cancel_for_live_turn()

    # -- running ----------------------------------------------------------------------------------

    def _route(self, settings: ReviewSettings, *, warn: bool = True) -> _Route:
        runtime = self.parent.runtime
        main = runtime.route if isinstance(runtime.route, RuntimeRoute) else None
        effort = runtime.options.reasoning_effort
        aux = self.parent.aux
        if aux is not None and main is not None:
            try:
                candidates = aux.candidates(TASK)
                first = candidates[0] if candidates else None
                if first is not None and first.origin != "main":
                    route = aux.route_for(first)
                    if not _same_route(route, main):
                        chosen = first.reasoning_effort or None
                        return _Route(True, AuxCaller(aux, route, chosen), route, chosen)
            except ProviderError as exc:
                if warn:
                    self._warn_once(
                        "route",
                        "the review model in auxiliary.review could not be used (%s); reviews "
                        "run on the chat model",
                        exc.message,
                    )
        if warn and settings.configured_effort:
            self._warn_once(
                "effort",
                "auxiliary.review.reasoning_effort (%s) is ignored while reviews run on the chat "
                "model: they keep its reasoning setting so its prompt cache is reused",
                settings.configured_effort,
            )
        return _Route(False, self.parent.caller, main, effort)

    async def _window(self, choice: _Route) -> int | None:
        runtime = self.parent.runtime
        main_window = runtime.options.context_length
        aux = self.parent.aux
        if not choice.routed or aux is None:
            return main_window
        try:
            return await aux.window_for(TASK, main_window or 0) or None
        except Exception:
            log.debug("the review model's window is unknown", exc_info=True)
            return None

    def _fork_runtime(
        self,
        request: ReviewRequest,
        choice: _Route,
        caller: GuardedCaller,
        allowed: frozenset[str],
        prompt: str,
        window: int | None,
    ) -> SessionRuntime:
        parent = self.parent.runtime
        fork_id = f"{parent.session_id}:review:{secrets.token_hex(4)}"
        options = replace(
            parent.options,
            max_iterations=MAX_ITERATIONS,
            intent_ack=False,
            reasoning_effort=choice.effort,
            context_length=window if choice.routed and window else parent.options.context_length,
        )
        return SessionRuntime(
            session_id=fork_id,
            caller=caller,
            clock=parent.clock,
            prompts=PromptLifecycle(_FixedPrompt(prompt)),
            prompt_context=replace(parent.prompt_context, session_id=fork_id),
            store=MemoryStore(),
            registry=parent.registry,
            tools=copy.deepcopy(parent.tools),
            scope=parent.scope,
            route=choice.route if choice.route is not None else parent.route,
            options=options,
            hooks=WhitelistHooks(allowed, parent.hooks),
            inline={name: run for name, run in parent.inline.items() if name in allowed},
            spill_sink=parent.spill_sink,
            source=parent.source,
            heals=_copy_or(parent.heals, HealLedger),
            memo=_copy_or(parent.memo, ArgsMemo),
            hydrated=True,
            profile=parent.profile,
            run_context="unattended",
        )

    async def run(self, request: ReviewRequest) -> ReviewOutcome:
        """Run one review to its end (see the module notes); never raises."""
        owned = self._task is None or self._task.done()
        if owned:  # awaited directly rather than through ``start``
            self._cancelled = False
            self._task = asyncio.current_task()
        try:
            outcome = await self._run(request)
        except asyncio.CancelledError:
            self.last = ReviewOutcome("cancelled", request.kinds, reason="task cancelled")
            raise
        except Exception as exc:
            log.warning("the background review failed: %s", exc, exc_info=True)
            outcome = ReviewOutcome("error", request.kinds, reason=str(exc))
        finally:
            self._state = None
            if owned:
                self._task = None
        self.last = outcome
        return outcome

    async def _run(self, request: ReviewRequest) -> ReviewOutcome:
        parent = self.parent.runtime
        kinds = frozenset(k for k in request.kinds if k in KINDS)
        prompt_bytes = parent.prompts.prompt
        if not kinds or not request.rows or prompt_bytes is None:
            return ReviewOutcome("skipped", kinds, reason="nothing to review")
        if self._cancelled:
            return ReviewOutcome("cancelled", kinds, reason="a live turn started first")
        settings = ReviewSettings.from_scope(self.parent.scope)
        choice = self._route(settings)
        window = await self._window(choice) if settings.max_input_tokens is None else None
        guard = GuardedCaller(
            choice.caller,
            max_requests=MAX_ITERATIONS,
            budget=input_budget(settings.max_input_tokens, window),
        )
        if choice.routed and choice.route is not None:
            prompt_bytes = with_identity(
                prompt_bytes, model=choice.route.model, provider=choice.route.provider
            )
        allowed = allowed_tools(kinds, parent.tool_names(), settings.extra_tools)
        runtime = self._fork_runtime(request, choice, guard, allowed, prompt_bytes, window)
        history = digest_rows(request.rows) if choice.routed else list(request.rows)
        message = build_review_prompt(kinds, allowed_tools=allowed, focus=request.focus)
        state = new_turn_state(
            runtime,
            message=message,
            turn_id=f"review_{secrets.token_hex(4)}",
            generation=1,
            history=history,
        )
        self._state = state
        if self._cancelled:
            return ReviewOutcome("cancelled", kinds, reason="a live turn started first")
        try:
            with pending.bind_origin("background_review", attended=request.attended):
                result = await run_turn(state)
        finally:
            READS.clear(runtime.session_id)
        rows = tuple(result.rows[result.current_turn_user_index + 1 :])
        try:
            lines = summary_lines(rows, settings.notices)
        except Exception:
            log.warning("could not summarise the review", exc_info=True)
            lines = []
        hooks = runtime.hooks
        denied = tuple(hooks.denied) if isinstance(hooks, WhitelistHooks) else ()
        status = "cancelled" if state.control.interrupted else "done"
        emit_done(self.parent.sink, kinds, lines, settings.notices)
        log_completion(
            parent.session_id,
            kinds=kinds,
            lines=lines,
            requests=guard.requests,
            input_tokens=guard.input_tokens,
            output_tokens=guard.output_tokens,
            cache_read_tokens=guard.cache_read_tokens,
            stop=guard.stopped or status,
            routed=choice.routed,
        )
        return ReviewOutcome(
            status,
            kinds,
            tuple(lines),
            rows,
            guard.requests,
            guard.input_tokens,
            guard.output_tokens,
            guard.stopped,
            choice.routed,
            denied,
            runtime.session_id,
        )


def install_review(
    agent: Agent,
    *,
    no_review: bool = False,
    queue: IdleQueue[ReviewRequest] | None = None,
) -> ReviewRunner:
    """Give ``agent``'s session its review runner and trigger (P09-14/15).

    Adds the trigger and the runner's turn-finished hook to ``agent.runtime.after_turn`` and closes
    the runner with the agent. The host still calls ``await runner.cancel_for_live_turn()`` before
    each live turn.
    """
    runner = ReviewRunner(ReviewParent.from_agent(agent), queue=queue)
    trigger = ReviewTrigger(agent.scope, runner.start, no_review=no_review)
    runner.trigger = trigger
    agent.runtime.after_turn.append(trigger.after_turn)
    agent.runtime.after_turn.append(runner.turn_finished)
    agent.on_close(runner.close, name="background review")
    return runner
