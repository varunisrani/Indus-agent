"""What a session host adds to an agent for the learning loop (P09 wave 1 wiring).

Tools. ``memory`` (P09-02) and ``session_search`` (P09-25) are agent-level tools: the registry holds
their schema (so toolset selection and the tool array see them) and the loop runs them through its
inline table. ``skills_list`` / ``skill_view`` (P09-08) and ``skill_manage`` (P09-11, writes gated
by ``learning.pending``) are plain registry tools. ``tool_registry``
builds the registry a host passes as ``AgentOptions.registry``: the P05 built-ins plus these. The
learning modules are registered here, not in ``tools.builtin``, because tools sit below learning in
the layer order.

Per session, ``wire_session`` gives the host:

- inline executors for the selected agent-level tools only. An unselected tool gets none, so the
  model cannot reach it and the prompt (which names inline tools) carries no guidance for it;
- the prompt builder, composed outside in: ``MemoryPromptBuilder`` (the frozen notes and user
  profile blocks plus the memory guidance; only when ``memory`` is selected) over
  ``SkillsIndexBuilder`` (the skills index frozen with the prompt, followed by the auto-load and
  ``-s`` preload block) over the P04 ``PromptBuilder``. Each wrapper only fills its own
  ``BuildContext`` slots, so the composition order changes nothing in the text; where the blocks
  land is the P04 builder's volatile tier: memory, user profile, then the skills index (P09-04);
- one prologue note source (``AgentOptions.note_sources``) that stages the skills-index change
  note (P09-07) on the turn's note channel, once per change, without touching the prompt.

``session_search`` condenses long reads through the session's bound auxiliary client, which only
exists once the agent is open: the host calls ``SessionLearning.bind_aux(agent.aux)`` after
``Agent.start``.

The background review (P09-14..16) is attached per agent after ``Agent.start`` with
``SessionLearning.attach_review(agent, no_review=...)``: the runner and trigger of
``review.fork.install_review``; the runner's ``cancel_for_live_turn`` as a ``before_turn`` hook (a
live turn stops an in-flight review); the agent's own successful ``memory`` and ``skill_manage``
writes reset the matching counter. ``review_runner(agent)`` finds the runner again for ``/refine``.
"""

from __future__ import annotations

import weakref
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from indus.core.notes import NoteChannel
from indus.core.prompt.builder import BuildContext, PromptBuilder
from indus.core.prompt.lifecycle import Builder, PromptStore
from indus.core.turn_state import InlineExecutor, NoteSource
from indus.foundation.clock import Clock
from indus.foundation.scope import Scope
from indus.learning import session_search
from indus.learning.memory import tool as memory_tool
from indus.learning.memory.prompt_block import FrozenMemory, MemoryPromptBuilder
from indus.learning.memory.store import MemoryStore
from indus.learning.review.fork import ReviewRunner, install_review
from indus.learning.skills import manage_tool, view_tools
from indus.learning.skills.invoke import auto_load_block
from indus.learning.skills.prompt_index import (
    IndexDeltaTracker,
    SkillsIndexBuilder,
    catalog_index_source,
    stage_index_delta,
)
from indus.tools import toolsets
from indus.tools.builtin import load_builtins
from indus.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from indus.core.agent import Agent

__all__ = [
    "AGENT_LEVEL_TOOLS",
    "SessionLearning",
    "active_toolsets",
    "register_tools",
    "review_runner",
    "tool_registry",
    "wire_session",
]

_RUNNERS: weakref.WeakKeyDictionary[Agent, ReviewRunner] = weakref.WeakKeyDictionary()

AGENT_LEVEL_TOOLS: tuple[str, ...] = (memory_tool.TOOL_NAME, session_search.TOOL_NAME)
"""Learning tools the loop runs inline (registered for their schema only)."""


def register_tools(registry: ToolRegistry) -> None:
    """Register every learning tool: memory, session_search, skills_list, skill_view and
    skill_manage."""
    memory_tool.register_tools(registry)
    session_search.register_tools(registry)
    view_tools.register_tools(registry)
    manage_tool.register_tools(registry)


def tool_registry(clock: Clock | None = None) -> ToolRegistry:
    """A registry holding the built-in tools and the learning tools."""
    registry = ToolRegistry(clock=clock)
    load_builtins(registry)
    register_tools(registry)
    return registry


def active_toolsets(
    selected: Iterable[str], registry: ToolRegistry | None = None
) -> frozenset[str]:
    """Named toolsets whose every tool the session has (for skill visibility conditions)."""
    have = set(selected)
    out: set[str] = set()
    for name in toolsets.toolset_names(registry):
        members = set(toolsets.resolve(name, registry))
        if members and members <= have:
            out.add(name)
    return frozenset(out)


@dataclass
class SessionLearning:
    """One session's learning wiring (see the module notes)."""

    inline: dict[str, InlineExecutor]
    builder: Builder
    note_sources: tuple[NoteSource, ...]
    index: SkillsIndexBuilder
    memory: memory_tool.MemoryWiring | None = None
    frozen: FrozenMemory | None = None
    search: session_search.SessionSearchWiring | None = None
    tracker: IndexDeltaTracker = field(default_factory=IndexDeltaTracker)

    def bind_aux(self, aux: session_search.AuxCaller | None) -> None:
        """Give ``session_search`` the session's bound auxiliary client (after ``Agent.start``)."""
        if self.search is not None:
            self.search.aux = aux

    def attach_review(self, agent: Agent, *, no_review: bool = False) -> ReviewRunner:
        """Give ``agent`` its background review (see the module notes); returns the runner."""
        runner = install_review(agent, no_review=no_review)
        agent.runtime.before_turn.append(runner.cancel_for_live_turn)
        _RUNNERS[agent] = runner
        trigger = runner.trigger
        if trigger is None:
            return runner
        if self.memory is not None:
            self.memory.tool.on_success.append(lambda: trigger.note_write("memory"))

        def skill_written(event: manage_tool.SkillWriteEvent) -> None:
            if event.origin == "foreground" and event.session_id == agent.session_id:
                trigger.note_write("skills")

        manage_tool.SUCCESS_LISTENERS.append(skill_written)

        def detach() -> None:
            if skill_written in manage_tool.SUCCESS_LISTENERS:
                manage_tool.SUCCESS_LISTENERS.remove(skill_written)

        agent.on_close(detach, name="review counter listener")
        return runner


def review_runner(agent: Agent) -> ReviewRunner | None:
    """The review runner ``attach_review`` gave ``agent`` (None: the session has none)."""
    return _RUNNERS.get(agent)


def wire_session(
    scope: Scope,
    selected: Iterable[str],
    *,
    session_id: str,
    prompt_store: PromptStore | None = None,
    preload: Sequence[str] = (),
    registry: ToolRegistry | None = None,
    inner: Builder | None = None,
    memory_store: MemoryStore | None = None,
) -> SessionLearning:
    """The learning wiring for a session whose selected tool names are ``selected``.

    ``prompt_store`` is where the session's prompt is kept (the frozen memory block is cut back out
    of it on resume); ``preload`` is the ``-s`` list loaded with ``skills.auto_load``.
    """
    names = frozenset(selected)
    inline: dict[str, InlineExecutor] = {}
    memory: memory_tool.MemoryWiring | None = None
    search: session_search.SessionSearchWiring | None = None
    if memory_tool.TOOL_NAME in names:
        memory = memory_tool.memory_wiring(scope, store=memory_store)
        inline[memory_tool.TOOL_NAME] = memory.execute
    if session_search.TOOL_NAME in names:
        search = session_search.session_search_wiring(scope)
        inline[session_search.TOOL_NAME] = search.execute

    extra = tuple(preload)

    def auto_loaded(ctx: BuildContext) -> str:
        return auto_load_block(scope, session_id=ctx.session_id or None, extra=extra)

    index = SkillsIndexBuilder(
        inner if inner is not None else PromptBuilder(),
        catalog_index_source(scope, toolsets=active_toolsets(names, registry)),
        extra_blocks=(auto_loaded,),
    )
    builder: Builder = index
    frozen: FrozenMemory | None = None
    if memory is not None:
        frozen = FrozenMemory(memory.tool.store, prompts=prompt_store, session_ids=(session_id,))
        builder = MemoryPromptBuilder(index, frozen)

    wiring = SessionLearning(
        inline=inline,
        builder=builder,
        note_sources=(),
        index=index,
        memory=memory,
        frozen=frozen,
        search=search,
    )

    def skills_delta(prompt: str, ctx: BuildContext, notes: NoteChannel) -> None:
        stage_index_delta(notes, wiring.tracker, prompt, index.current_index(ctx))

    wiring.note_sources = (skills_delta,)
    return wiring
