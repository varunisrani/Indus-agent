"""Building and running one subagent (P10-02).

``build_child(parent, task, ...)`` makes a child ``Agent`` for one normalised task:

- its own session row in the parent's store: source ``subagent``, ``parent_session_id`` and the
  ``_delegate_from`` marker, so session lists hide it and deleting the parent deletes it (B15);
- its own terminal task id (its session id) on the parent's environment registry;
- tools = (``delegation.child_toolsets`` ∪ the parent's MCP tools when inherited) ∩ the parent's
  tools, minus ``BLOCKED_TOOLS``, every send tool and — at depth ≥ ``max_depth`` —
  ``delegate_task`` (``child_tool_names``);
- the parent's route (``delegation.model`` may pin another model), its ``Scope`` (credentials),
  credential pools and auxiliary client; the iteration cap from P10-01; platform ``subagent``;
- approvals go to the parent's surface through ``SubagentBroker``, which labels each request with
  the subagent id and refuses password prompts (sudo stays headless);
- ``ChildHooks`` wraps the parent's tool hooks and, on a background ``terminal`` result, replaces
  the "notify is off" note with one for subagents: wait, kill or hand off (R02.17);
- every answered model call of the child is rolled up into the parent's usage ledger under the
  task label ``subagent`` (the child writes no usage rows of its own, so nothing counts twice).

``ChildAgent.run()`` sends the brief (goal, context, image references, the output contract when
the task has a schema), checks a schema answer once more after one correction (P10-04), then
cleans up through ``ChildResources`` (P10-06) and the worktree (P10-07) and closes the child.
When any of its turns saw outside text (``TurnResult.read_untrusted``), the outcome says so and
the parent fences the summary (SEC-4).
``interrupt``, ``steer`` and ``progress`` serve the batch runner and background control.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from indus.backends.processes import ProcessRegistry
from indus.core.agent import Agent, AgentOptions
from indus.core.prompt.lifecycle import Builder
from indus.core.subagents.batch import ChildOutcome, Progress, Status
from indus.core.subagents.output_schema import (
    SCHEMA_MISS_NOTE,
    check_answer,
    contract_text,
    correction_text,
)
from indus.core.subagents.teardown import ChildResources
from indus.core.subagents.worktree import (
    ChildWorktree,
    close_worktree,
    open_worktree,
    scope_in_tree,
)
from indus.core.turn_state import LoopOptions, TurnResult
from indus.foundation.ids import new_session_id
from indus.foundation.scope import Scope
from indus.protocol.errors import SERVER_REQUEST_UNSUPPORTED, make_error
from indus.protocol.events import CancelReason, ExitReason
from indus.protocol.requests_port import AnswerModel, RequestUnsupported, ServerRequestPort
from indus.state.sessions import MARKER_DELEGATE
from indus.tools import toolsets
from indus.tools.builtin.code import kernel as code_kernel
from indus.tools.builtin.delegate import TOOL_NAME as DELEGATE_TOOL
from indus.tools.builtin.delegate import DelegateTask, DelegationLimits
from indus.tools.calls import ToolCall, ToolContext, ToolResult
from indus.tools.hooks import BeforeVerdict, ToolHooks
from indus.tools.registry import ToolRegistry

__all__ = [
    "BLOCKED_TOOLS",
    "CHILD_NOTIFY_NOTE",
    "SOURCE",
    "ChildAgent",
    "ChildHooks",
    "SubagentBroker",
    "build_child",
    "child_tool_names",
    "is_mcp_toolset",
    "is_send_tool",
]

log = logging.getLogger(__name__)

SOURCE = "subagent"
USAGE_TASK = "subagent"
BLOCKED_TOOLS = frozenset({"clarify", "memory", "cronjob_manage", "send_message"})
ANSWERABLE_KINDS = frozenset({"approval"})

CHILD_NOTIFY_NOTE = (
    "You are a subagent: nothing will tell you when this process ends, and it is stopped when you "
    "finish. Before you end, wait for it (process_manage wait), kill it, or hand it to your parent "
    "(process_manage handoff with a purpose) if it has to keep running."
)
SYSTEM_BRIEF = (
    "You are a subagent: another agent gave you one task and will read only your final reply. "
    "Work on the task alone; nobody can answer questions, so make reasonable choices and say "
    "which ones you made. Use your tools to check your work. Your final reply is all the other "
    "agent gets back: lead with the outcome, then list the files you changed, the commands that "
    "matter, what you could not do and why. Keep it short and factual."
)
_OK_EXITS = frozenset({ExitReason.COMPLETED})
_CAPPED_EXITS = frozenset({ExitReason.MAX_ITERATIONS, ExitReason.BUDGET_EXHAUSTED})
_STOPPED_EXITS = frozenset({ExitReason.INTERRUPTED_BY_USER, ExitReason.INTERRUPTED_BY_SYSTEM})


# -- tools ----------------------------------------------------------------------------------------


def is_send_tool(name: str) -> bool:
    """A tool that sends a message on the user's behalf (never given to a child)."""
    return name == "send_message" or name.startswith("send_")


def is_mcp_toolset(name: str) -> bool:
    return name.startswith(("mcp-", "mcp_", "mcp:"))


def child_tool_names(
    parent_tools: Iterable[str],
    *,
    limits: DelegationLimits,
    depth: int,
    registry: ToolRegistry | None = None,
    scope: Scope | None = None,
) -> frozenset[str]:
    """The tools a child at ``depth`` gets (see the module notes)."""
    parent = set(parent_tools)
    wanted: set[str] = set()
    for name in limits.child_toolsets:
        wanted |= set(toolsets.resolve(name, registry, scope))
    if limits.inherit_mcp_toolsets and registry is not None:
        wanted |= {e.name for e in registry.entries(scope) if is_mcp_toolset(e.toolset)}
    if depth < limits.max_depth:
        wanted.add(DELEGATE_TOOL)  # may delegate further while depth remains
    names = wanted & parent
    blocked = {n for n in names if n in BLOCKED_TOOLS or is_send_tool(n)}
    if depth >= limits.max_depth:
        blocked.add(DELEGATE_TOOL)
    return frozenset(names - blocked)


# -- approvals ------------------------------------------------------------------------------------


class SubagentBroker:
    """The parent's broker as a child sees it: approvals only, each labelled with the child."""

    def __init__(
        self,
        parent: ServerRequestPort,
        label: str,
        *,
        parent_turn: Callable[[], str | None] = lambda: None,
    ) -> None:
        self.parent = parent
        self.label = label
        self.parent_turn = parent_turn

    async def ask(
        self,
        kind: str,
        payload: Mapping[str, Any],
        *,
        timeout: float | None = None,
        turn_id: str | None = None,
    ) -> AnswerModel:
        if kind not in ANSWERABLE_KINDS:
            raise RequestUnsupported(
                kind,
                make_error(
                    SERVER_REQUEST_UNSUPPORTED,
                    f"a subagent cannot ask the person ({kind}); it runs headless",
                ),
            )
        labelled = dict(payload)
        description = str(labelled.get("description") or "").strip()
        labelled["description"] = f"[{self.label}] {description}".strip()
        return await self.parent.ask(kind, labelled, timeout=timeout, turn_id=self.parent_turn())

    def cancel_all(self, reason: CancelReason) -> int:
        """Nothing to withdraw here: the parent's broker owns the requests (and withdraws them
        when the parent's turn ends)."""
        return 0

    def human_wait_seconds(self) -> float:
        try:
            return float(self.parent.human_wait_seconds())
        except Exception:
            return 0.0


# -- tool hooks -----------------------------------------------------------------------------------


@dataclass
class ChildHooks:
    """The parent's hooks plus the subagent note on background terminal results."""

    inner: ToolHooks

    @property
    def empty(self) -> bool:
        return False

    async def before(self, call: ToolCall, ctx: ToolContext) -> BeforeVerdict | None:
        return await self.inner.before(call, ctx)

    async def after(
        self, call: ToolCall, ctx: ToolContext, result: ToolResult, duration_s: float
    ) -> None:
        await self.inner.after(call, ctx, result, duration_s)

    async def transform(
        self, call: ToolCall, ctx: ToolContext, result: ToolResult
    ) -> ToolResult | None:
        changed = await self.inner.transform(call, ctx, result)
        current = changed if changed is not None else result
        if call.name != "terminal" or not isinstance(current.content, str):
            return changed
        try:
            body: object = json.loads(current.content)
        except ValueError:
            return changed
        if not isinstance(body, dict):
            return changed
        data = cast(dict[str, Any], body)
        if "notify_unsupported" not in data and "notify_on_complete" not in data:
            return changed
        data["notify_on_complete"] = False
        data["notify_unsupported"] = CHILD_NOTIFY_NOTE
        data.pop("hint", None)
        text = json.dumps(data, ensure_ascii=False)
        return ToolResult(text, is_error=current.is_error, error_type=current.error_type)


# -- the child ------------------------------------------------------------------------------------


def brief_text(task: DelegateTask, schema: Mapping[str, Any] | None) -> str:
    """The child's first message: its task in our own words."""
    parts = [f"Your task:\n{task.goal}"]
    if task.context:
        parts.append(f"What you need to know:\n{task.context}")
    if task.images:
        listed = "\n".join(f"- {image}" for image in task.images)
        parts.append(
            "Images that belong to this task (look at them with vision_analyze when you have "
            f"it):\n{listed}"
        )
    if schema is not None:
        parts.append(contract_text(schema))
    return "\n\n".join(parts)


def _turn_status(result: TurnResult | None) -> tuple[Status, str]:
    if result is None:
        return "interrupted", "The subagent was stopped before it started."
    reason = result.exit_reason
    if reason in _OK_EXITS:
        return "completed", ""
    if reason in _STOPPED_EXITS:
        return "interrupted", "The subagent was stopped; the summary is what it had so far."
    if reason in _CAPPED_EXITS and (result.final_text or "").strip():
        return "completed", f"The subagent reached its limit ({reason.value}) and summarised."
    detail = result.failure_reason or reason.value
    return "failed", f"The subagent's run ended without a result ({detail})."


@dataclass
class ChildAgent:
    """One running (or queued) subagent."""

    subagent_id: str
    index: int
    task: DelegateTask
    agent: Agent
    depth: int
    parent_session_id: str
    resources: ChildResources
    schema: Mapping[str, Any] | None = None
    worktree: ChildWorktree | None = None
    worktree_note: str = ""
    status: str = "queued"
    started_at: datetime | None = None
    ended_at: datetime | None = None
    outcome: ChildOutcome | None = None
    stopped_by: str = ""
    read_untrusted: bool = False  # one of its turns saw outside text (SEC-4)
    _stop: bool = False
    _done: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def goal(self) -> str:
        return self.task.goal

    @property
    def session_id(self) -> str:
        return self.agent.session_id

    @property
    def done(self) -> bool:
        return self._done.is_set()

    async def wait(self) -> ChildOutcome | None:
        await self._done.wait()
        return self.outcome

    # -- live view --------------------------------------------------------------------------------

    @property
    def iterations(self) -> int:
        return self.agent.usage.api_calls

    def last_activity(self) -> datetime | None:
        current = self.agent.current
        clock = self.agent.scope.clock
        if current is None:
            return self.ended_at or self.started_at
        ago = max(0.0, clock.monotonic() - current.state.last_activity)
        return datetime.fromtimestamp(clock.now().timestamp() - ago, tz=clock.now().tzinfo)

    def progress(self) -> Progress:
        current = self.agent.current
        if current is None:
            return Progress(("between turns", self.agent.generation))
        state = current.state
        marker = (current.generation, state.api_calls, len(state.rows), state.last_activity)
        return Progress(marker, in_tool=state.control.phase == "tools")

    # -- control ----------------------------------------------------------------------------------

    def interrupt(self) -> None:
        """Stop the child's turn (it ends through its own exit funnel); a queued child never
        starts."""
        self._stop = True
        self.agent.interrupt(by_system=True)

    def steer(self, text: str) -> bool:
        """Hand ``text`` to the running turn (delivered after its next tool results)."""
        return self.agent.steer(text)

    # -- running ----------------------------------------------------------------------------------

    async def _turn(self, message: str) -> TurnResult | None:
        if self._stop:
            return None
        running = await self.agent.start_turn(message)
        if self._stop:
            running.state.control.interrupt(running.generation, by_system=True)
        try:
            result = await asyncio.shield(running.task)
        except asyncio.CancelledError:
            running.state.control.interrupt(running.generation, by_system=True)
            raise
        if result.read_untrusted:
            self.read_untrusted = True
        return result

    async def _work(self) -> ChildOutcome:
        result = await self._turn(brief_text(self.task, self.schema))
        status, note = _turn_status(result)
        summary = (result.final_text or "") if result is not None else ""
        if status != "completed" or self.schema is None:
            return ChildOutcome(status, summary=summary, note=note)
        check = check_answer(summary, self.schema)
        if not check.valid and not self._stop:
            again = await self._turn(correction_text(check.errors))
            again_status, again_note = _turn_status(again)
            if again is not None and again_status == "completed":
                retry_text = again.final_text or ""
                retry = check_answer(retry_text, self.schema)
                if retry.valid:
                    return ChildOutcome("completed", summary=retry_text, schema_valid=True)
                check = retry if retry_text.strip() else check
                summary = retry_text if retry_text.strip() else summary
            elif again_status == "interrupted":
                note = again_note
        if check.valid:
            return ChildOutcome("completed", summary=summary, schema_valid=True)
        return ChildOutcome(
            "completed",
            summary=summary,
            schema_valid=False,
            errors=check.errors,
            note=" ".join(n for n in (note, SCHEMA_MISS_NOTE) if n),
        )

    async def run(self) -> ChildOutcome:
        """Run the task to its end, then clean up; never raises for the child's own failures."""
        clock = self.agent.scope.clock
        self.status = "running"
        self.started_at = clock.now()
        outcome = ChildOutcome("failed", note="The subagent did not run.")
        try:
            if self._stop:
                outcome = ChildOutcome(
                    "interrupted", note="The subagent was stopped before it started."
                )
            else:
                await self.resources.open()
                outcome = await self._work()
        except asyncio.CancelledError:
            outcome = ChildOutcome("interrupted", note="The subagent was cancelled.")
            raise
        except Exception as exc:
            log.warning("subagent %s failed", self.subagent_id, exc_info=True)
            outcome = ChildOutcome("failed", note=f"{type(exc).__name__}: {exc}")
        finally:
            outcome = await self._finish(outcome)
        return outcome

    async def discard(self, outcome: ChildOutcome) -> None:
        """A child that never ran: close its agent and settle it with ``outcome``."""
        if self.done:
            return
        try:
            await self.agent.close()
        except Exception:
            log.warning("closing unstarted subagent %s failed", self.subagent_id, exc_info=True)
        self.outcome = outcome
        self.status = outcome.status
        self._done.set()

    async def _finish(self, outcome: ChildOutcome) -> ChildOutcome:
        orphans = await self.resources.close(status=outcome.status)
        tree: Mapping[str, Any] | None = None
        if self.worktree is not None:
            tree = await close_worktree(self.agent.scope, self.worktree)
        notes = [n for n in (outcome.note, self.worktree_note) if n]
        if self.stopped_by:
            notes.append(self.stopped_by)
        outcome = replace(
            outcome,
            note=" ".join(notes),
            orphaned_processes=tuple(orphans),
            worktree=tree,
            iterations=self.iterations,
            read_untrusted=outcome.read_untrusted or self.read_untrusted,
        )
        try:
            await self.agent.close()
        except Exception:
            log.warning("closing subagent %s failed", self.subagent_id, exc_info=True)
        self.outcome = outcome
        self.status = outcome.status
        self.ended_at = self.agent.scope.clock.now()
        self._done.set()
        return outcome


# -- building -------------------------------------------------------------------------------------


def new_subagent_id(index: int) -> str:
    return f"sa-{index}-{secrets.token_hex(4)}"


def _child_builder(parent: Agent) -> Builder | None:
    """The parent's base prompt builder: never its session-level memory or skills blocks (P09)."""
    options = parent.options
    return options.subagent_builder if options.subagent_builder is not None else options.builder


def _parent_cwd(parent: Agent) -> Path | None:
    cwd = parent.runtime.prompt_context.cwd or parent.options.cwd
    if cwd is not None:
        return Path(cwd)
    raw = parent.options.session_fields.get("cwd")
    return Path(str(raw)) if isinstance(raw, str) and raw else None


async def build_child(
    parent: Agent,
    task: DelegateTask,
    *,
    index: int,
    depth: int,
    limits: DelegationLimits,
    schema: Mapping[str, Any] | None = None,
    parent_turn: Callable[[], str | None] = lambda: None,
    processes: ProcessRegistry | None = None,
    use_worktree: bool = False,
    subagent_id: str | None = None,
) -> ChildAgent:
    """Make (not run) the child for ``task`` (see the module notes)."""
    scope = parent.scope
    runtime = parent.runtime
    ident = subagent_id or new_subagent_id(index)
    session_id = new_session_id(scope.clock)
    cwd = _parent_cwd(parent)
    tree: ChildWorktree | None = None
    tree_note = ""
    child_scope = scope
    if use_worktree:
        folder = cwd if cwd is not None else Path.cwd()
        tree, tree_note = await open_worktree(scope, folder, ident, session_id=session_id)
        if tree is not None:
            child_scope = scope_in_tree(scope, tree)
            cwd = Path(tree.path)
    route = parent.binding.state.route
    if limits.model:
        route = replace(route, model=limits.model)
    names = child_tool_names(
        parent.tool_names, limits=limits, depth=depth, registry=runtime.registry, scope=scope
    )
    fields: dict[str, Any] = {
        "parent_session_id": parent.session_id,
        "model_config": {MARKER_DELEGATE: parent.session_id},
    }
    if cwd is not None:
        fields["cwd"] = str(cwd)
    broker = parent.broker
    options = AgentOptions(
        session_id=session_id,
        platform=SOURCE,
        source=SOURCE,
        store=runtime.store,
        builder=_child_builder(parent),
        registry=runtime.registry,
        client_factory=parent.options.client_factory,
        list_models=parent.options.list_models,
        loop=LoopOptions.from_scope(
            child_scope, platform=SOURCE, max_iterations=limits.max_iterations
        ),
        max_retries=parent.options.max_retries,
        policy=parent.options.policy,
        cwd=cwd,
        system_message=SYSTEM_BRIEF,
        broker=(
            SubagentBroker(broker, f"subagent {ident}", parent_turn=parent_turn)
            if broker is not None
            else None
        ),
        hooks=ChildHooks(parent.options.hooks),
        session_fields=fields,
        cleanups=parent.options.cleanups,
        aux=parent.options.aux,
        pools=parent.pools,
        only_tools=names,
        delegation_depth=depth,
        # The parent's own folder (W47-b); a worktree child's scope already names its tree.
        workdir=parent.options.workdir if tree is None else None,
    )
    agent = await Agent.start(child_scope, route, None, options)
    agent.runtime.run_context = runtime.run_context
    agent.runtime.environments = runtime.environments
    # Its execute_code kernels: pinned outside the top-level LRU, disposed at teardown (P10-06).
    agent.runtime.code_kernel_owner = code_kernel.child_owner(parent.session_id, agent.session_id)
    _roll_up_usage(parent, agent)
    resources = ChildResources(
        scope=child_scope,
        parent_session_id=parent.session_id,
        child_session_id=agent.session_id,
        subagent_id=ident,
        goal=task.goal,
        processes=processes,
    )
    return ChildAgent(
        subagent_id=ident,
        index=index,
        task=task,
        agent=agent,
        depth=depth,
        parent_session_id=parent.session_id,
        resources=resources,
        schema=schema,
        worktree=tree,
        worktree_note=tree_note,
    )


def _roll_up_usage(parent: Agent, child: Agent) -> None:
    """Each answered call of the child lands once, in the parent's ledger, as ``subagent``."""
    child.usage.store = None
    target = parent.usage

    async def forward(usage: Any, task: str) -> None:
        try:
            await target.record(
                usage, route=child.binding.state.route, latency_s=0.0, task=USAGE_TASK
            )
        except Exception:
            log.warning("could not roll subagent usage up to the parent", exc_info=True)

    child.usage.add_listener(forward)
