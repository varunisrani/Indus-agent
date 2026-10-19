"""The session's ``delegate_task`` executor (P10-01..07).

One ``Delegation`` per agent that offers ``delegate_task``; the agent puts ``execute`` in its
inline table. A call is normalised (P10-01), then:

- ``list`` / ``steer`` / ``stop`` act on the session's background children (P10-05);
- ``spawn`` is refused past ``delegation.max_depth``; every ``output_schema`` is meta-checked
  before any child exists (P10-04); a one-shot run (``run_context`` ``single_query``) may start
  at most ``delegation.oneshot_max_children`` children in total. Each task gets a child
  (P10-02), with a worktree when trusted config asks (P10-07).
- Background or not: a top-level agent (depth 0) whose host takes results between turns
  (``deliver`` set — ``SessionService`` sets it) on an interactive surface starts the children in
  the background and returns their ids. Everything else — children (depth > 0), one-shot runs,
  scheduled and unattended runs, hosts without a delivery path — waits for the batch (P10-03)
  and returns its results; the parent's interrupt reaches every child, and each heartbeat touches
  the parent turn's liveness.

The agent's close stops every child it still has (session end).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Coroutine, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from indus.backends.processes import ProcessRegistry
from indus.core.subagents.background import BackgroundDelegations
from indus.core.subagents.batch import ChildOutcome, Progress, result_entry, run_batch
from indus.core.subagents.child import ChildAgent, build_child, new_subagent_id
from indus.core.subagents.output_schema import OutputSchemaInvalid, check_schema
from indus.core.turn_state import TurnState
from indus.tools.builtin.delegate import (
    DelegateInputError,
    DelegateRequest,
    DelegateTask,
    DelegationLimits,
    normalise,
)
from indus.tools.calls import ToolCall, ToolResult

if TYPE_CHECKING:
    from indus.core.agent import Agent

__all__ = ["BACKGROUND_NOTE", "Delegation", "default_processes"]

log = logging.getLogger(__name__)

INTERACTIVE = frozenset({"interactive"})
ONE_SHOT = "single_query"
BACKGROUND_NOTE = (
    "The subagents are working in the background. Their results arrive as a new message after "
    "this turn; do not wait or poll for them. Use action=list, steer or stop to control them."
)
NO_BACKGROUND = "No subagents run in the background in this session."


def default_processes() -> ProcessRegistry | None:
    """The process's background job registry (the terminal runtime's)."""
    try:
        from indus.tools.builtin.terminal import runtime

        return runtime().processes
    except Exception:
        log.debug("no process registry for subagent cleanup", exc_info=True)
        return None


def _json(body: Mapping[str, Any]) -> ToolResult:
    return ToolResult.ok(json.dumps(dict(body), ensure_ascii=False))


class _Unbuilt:
    """A task whose child could not be made: it reports the failure in its place."""

    def __init__(self, subagent_id: str, goal: str, error: str) -> None:
        self._id, self._goal, self._error = subagent_id, goal, error

    @property
    def subagent_id(self) -> str:
        return self._id

    @property
    def goal(self) -> str:
        return self._goal

    async def run(self) -> ChildOutcome:
        return ChildOutcome("failed", note=f"The subagent could not be started: {self._error}")

    def interrupt(self) -> None:
        return None

    def progress(self) -> Progress:
        return Progress(None)


class Delegation:
    """``delegate_task`` for one agent (see the module notes)."""

    def __init__(
        self,
        agent: Agent,
        *,
        processes: Callable[[], ProcessRegistry | None] = default_processes,
    ) -> None:
        self.agent = agent
        self.depth = agent.options.delegation_depth
        self.processes = processes
        self.deliver: Callable[[str], object] | None = None
        self.worktree: bool | None = None  # a trusted per-call override (internal callers)
        self.oneshot_spent = 0
        self._background: BackgroundDelegations | None = None
        agent.on_close(self.close, name="subagents")

    # -- settings ---------------------------------------------------------------------------------

    @property
    def limits(self) -> DelegationLimits:
        return DelegationLimits.from_scope(self.agent.scope)

    def _run_context(self) -> str | None:
        try:
            return self.agent.runtime.run_context
        except RuntimeError:
            return None

    def runs_in_background(self) -> bool:
        context = self._run_context()
        return (
            self.depth == 0
            and self.deliver is not None
            and (context is None or context in INTERACTIVE)
        )

    @property
    def background(self) -> BackgroundDelegations | None:
        return self._background

    def _background_for(self, limits: DelegationLimits) -> BackgroundDelegations:
        if self._background is None:
            agent = self.agent

            def spawn(coro: Coroutine[Any, Any, None]) -> Any:
                return agent.spawn(coro, name="background subagents")

            self._background = BackgroundDelegations(
                clock=agent.scope.clock,
                scope=agent.scope,
                limit=limits.max_async_children,
                summary_cap=limits.summary_max_chars,
                deliver=lambda text: self._deliver(text),
                spawn=spawn,
            )
        return self._background

    def _deliver(self, text: str) -> None:
        deliver = self.deliver
        if deliver is None:
            log.warning("background subagent results dropped: the host takes no deliveries")
            return
        deliver(text)

    # -- the executor -----------------------------------------------------------------------------

    async def execute(self, call: ToolCall, state: TurnState) -> ToolResult:
        limits = self.limits
        try:
            request = normalise(call.args, max_children=limits.max_concurrent_children)
        except DelegateInputError as exc:
            return ToolResult.error(str(exc), error_type="invalid_arguments")
        if request.ignored:
            log.debug("delegate_task ignored arguments: %s", ", ".join(request.ignored))
        if request.action == "list":
            children = self._background.list() if self._background is not None else []
            return _json(
                {"subagents": children} if children else {"subagents": [], "note": NO_BACKGROUND}
            )
        if request.action == "steer":
            if self._background is None:
                return ToolResult.error(NO_BACKGROUND, error_type="not_found")
            outcome = self._background.steer(request.subagent_id, request.message)
            return _control_result(outcome)
        if request.action == "stop":
            if self._background is None:
                return ToolResult.error(NO_BACKGROUND, error_type="not_found")
            return _control_result(await self._background.stop(request.subagent_id))
        return await self._spawn(request, state, limits)

    async def _spawn(
        self, request: DelegateRequest, state: TurnState, limits: DelegationLimits
    ) -> ToolResult:
        if self.depth >= limits.max_depth:
            return ToolResult.error(
                f"Subagents cannot be started from here: the delegation depth limit is reached "
                f"(depth {self.depth}, delegation.max_depth {limits.max_depth}). Do the work "
                "yourself.",
                error_type="depth_limit",
            )
        schemas: list[dict[str, Any] | None] = []
        for index, task in enumerate(request.tasks):
            if task.output_schema is None:
                schemas.append(None)
                continue
            try:
                schemas.append(check_schema(task.output_schema))
            except OutputSchemaInvalid as exc:
                return ToolResult.error(f"task {index}: {exc}", error_type="invalid_arguments")
        if self._run_context() == ONE_SHOT and limits.oneshot_max_children > 0:
            cap = limits.oneshot_max_children
            if self.oneshot_spent + len(request.tasks) > cap:
                return ToolResult.error(
                    f"This one-shot run may start {cap} subagents in total and "
                    f"{self.oneshot_spent} already ran (delegation.oneshot_max_children). Do the "
                    "rest of the work yourself.",
                    error_type="subagent_budget",
                )
            self.oneshot_spent += len(request.tasks)
        background = self.runs_in_background()
        children = await self._build(request.tasks, schemas, limits, state)
        if background:
            built = [c for c in children if isinstance(c, ChildAgent)]
            failed = [c for c in children if not isinstance(c, ChildAgent)]
            ids = self._background_for(limits).start(built) if built else []
            body: dict[str, Any] = {
                "subagent_ids": ids,
                "status": "started",
                "note": BACKGROUND_NOTE,
            }
            if failed:
                body["failed"] = [
                    result_entry(c, await c.run(), limits.summary_max_chars) for c in failed
                ]
            return _json(body)
        outcomes = await run_batch(
            children,
            cap=limits.max_concurrent_children,
            clock=self.agent.scope.clock,
            scope=self.agent.scope,
            stop=state.control.stop,
            on_beat=lambda: state.touch("subagents working"),
        )
        for child, outcome in zip(children, outcomes, strict=True):
            if isinstance(child, ChildAgent):
                await child.discard(outcome)  # only one that never ran is still open
        results = [
            result_entry(child, outcome, limits.summary_max_chars)
            for child, outcome in zip(children, outcomes, strict=True)
        ]
        return _json({"results": results})

    async def _build(
        self,
        tasks: Sequence[DelegateTask],
        schemas: Sequence[dict[str, Any] | None],
        limits: DelegationLimits,
        state: TurnState,
    ) -> list[ChildAgent | _Unbuilt]:
        agent = self.agent
        use_tree = self.worktree if self.worktree is not None else limits.worktree
        processes = self.processes()
        turn_id = state.turn_id
        built: list[ChildAgent | _Unbuilt] = []
        for index, (task, schema) in enumerate(zip(tasks, schemas, strict=True)):
            ident = new_subagent_id(index)
            try:
                child = await build_child(
                    agent,
                    task,
                    index=index,
                    depth=self.depth + 1,
                    limits=limits,
                    schema=schema,
                    parent_turn=lambda: turn_id,
                    processes=processes,
                    use_worktree=use_tree,
                    subagent_id=ident,
                )
            except Exception as exc:
                log.warning("could not build subagent %s", ident, exc_info=True)
                built.append(_Unbuilt(ident, task.goal, f"{type(exc).__name__}: {exc}"))
                continue
            built.append(child)
        return built

    # -- stop surfaces ----------------------------------------------------------------------------

    async def stop_all(self) -> int:
        """Stop every background child of this session; how many were still working."""
        if self._background is None:
            return 0
        return await self._background.stop_all()

    async def close(self) -> None:
        if self._background is not None:
            await self._background.close()


def _control_result(body: Mapping[str, Any]) -> ToolResult:
    if "error" in body and len(body) == 1:
        return ToolResult.error(str(body["error"]), error_type="subagent_control")
    return _json(body)
