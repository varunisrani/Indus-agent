# pyright: strict
"""Plugging the hooks bus and plugin prompt sections into one session's agent options.

A session host that loaded plugins (P13-15) and registered shell hooks (P13-19) on a ``HookBus``
calls ``plugin_options`` once per session to get ``AgentOptions`` with:

- ``hooks``: the P04 ``ToolHooks`` seam backed by the bus (``pre_tool_call``, ``post_tool_call``,
  ``transform_tool_result``);
- ``llm_context``: the prologue's per-turn source, the bus's ``pre_llm_call`` (P13-17), whose text
  rides the user message, never the system prompt;
- ``builder``: wrapped so the plugin prompt sections are rendered once and frozen (P13-18). The
  unwrapped builder stays the one subagents start from.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from indus.core.agent import AgentOptions
from indus.core.prompt.builder import PromptBuilder
from indus.core.turn_state import LlmContextSource
from indus.plugins.context import PluginHost
from indus.plugins.hooks import AskApproval, HookBus, PluginToolHooks
from indus.plugins.prompt_sections import FrozenSections, PluginSectionsBuilder

__all__ = ["llm_context_source", "plugin_options"]


def llm_context_source(bus: HookBus) -> LlmContextSource:
    """The prologue's ``llm_context`` over ``bus``: ``pre_llm_call`` answers joined, or None."""

    async def source(payload: Mapping[str, Any]) -> str | None:
        if not bus.has("pre_llm_call"):
            return None
        return await bus.pre_llm_call(**dict(payload))

    return source


def plugin_options(
    options: AgentOptions,
    bus: HookBus,
    *,
    session_id: str,
    host: PluginHost | None = None,
    ask_approval: AskApproval | None = None,
) -> AgentOptions:
    """``options`` with the bus seams and (with a ``host``) the frozen plugin sections."""
    base = options.builder if options.builder is not None else PromptBuilder()
    builder = base
    if host is not None:
        frozen = FrozenSections(
            host.render_prompt_inputs,
            prompts=options.prompt_store,
            session_ids=(session_id,),
            profile_name=host.scope.profile_id,
            deadline_s=bus.timeout_s,
            clock=bus.clock,
            pool=bus.pool,
        )
        builder = PluginSectionsBuilder(base, frozen)
    return replace(
        options,
        hooks=PluginToolHooks(bus, ask_approval=ask_approval),
        llm_context=llm_context_source(bus),
        builder=builder,
        subagent_builder=options.subagent_builder or base,
    )
