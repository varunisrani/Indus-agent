"""Building a subagent (P10-02): tools, session row, approvals, terminal notes."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from indus.core.agent import Agent
from indus.core.persist import StateStore
from indus.core.subagents.child import (
    CHILD_NOTIFY_NOTE,
    ChildHooks,
    SubagentBroker,
    build_child,
    child_tool_names,
)
from indus.foundation.scope import Scope
from indus.protocol.events import CancelReason
from indus.protocol.requests_port import RequestUnsupported
from indus.protocol.server_requests import ApprovalAnswer, ApprovalRequest
from indus.state import deletion, listing, registry, sessions
from indus.tools.builtin.delegate import DelegateTask, DelegationLimits
from indus.tools.builtin.terminal import NOT_DELIVERABLE_NOTE
from indus.tools.calls import ToolCall, ToolContext, ToolResult
from indus.tools.hooks import NO_HOOKS
from indus.tools.registry import ToolRegistry
from tests.fakes.agent import DictConfig, run_driven
from tests.fakes.broker import FakeBroker
from tests.fakes.clock import FakeClock
from tests.fakes.model import text_turn, tool_batch
from tests.fakes.subagents import RoutedModel, parent_agent, subagent_registry
from tests.unit.tools.builtin._terminal_rig import Rig

FULL_PARENT = frozenset(
    {
        "terminal",
        "process_manage",
        "read_file",
        "write_file",
        "patch",
        "search_files",
        "web_search",
        "web_extract",
        "todo_list",
        "clarify",
        "delegate_task",
    }
)


# -- the tool list --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("parent", "expected"),
    [
        (
            FULL_PARENT,
            {
                "terminal",
                "process_manage",
                "read_file",
                "write_file",
                "patch",
                "search_files",
                "web_search",
                "web_extract",
            },
        ),
        (  # a parent without web: the child never gains it
            FULL_PARENT - {"web_search", "web_extract"},
            {"terminal", "process_manage", "read_file", "write_file", "patch", "search_files"},
        ),
        ({"read_file", "clarify", "delegate_task"}, {"read_file"}),
        (set[str](), set[str]()),
    ],
)
def test_child_toolset_is_the_intersection(parent: set[str], expected: set[str]) -> None:
    names = child_tool_names(parent, limits=DelegationLimits(), depth=1)
    assert names == expected


def test_blocked_tools_never_reach_a_child_whatever_the_config() -> None:
    reg = ToolRegistry()
    for name, toolset in [
        ("clarify", "clarify"),
        ("memory", "memory"),
        ("cronjob_manage", "cron"),
        ("send_message", "messaging"),
        ("send_sms", "messaging"),
        ("delegate_task", "delegation"),
        ("read_file", "file"),
    ]:
        reg.register(name, toolset, {"name": name, "parameters": {"type": "object"}}, lambda a: "")
    limits = DelegationLimits(
        child_toolsets=("file", "clarify", "memory", "cron", "messaging", "delegation")
    )
    parent = set(reg.names())
    assert child_tool_names(parent, limits=limits, depth=1, registry=reg) == {"read_file"}


def test_delegate_task_only_while_depth_remains() -> None:
    deep = DelegationLimits(max_depth=2)
    assert "delegate_task" in child_tool_names(FULL_PARENT, limits=deep, depth=1)
    assert "delegate_task" not in child_tool_names(FULL_PARENT, limits=deep, depth=2)


def test_mcp_toolsets_follow_the_parent_when_inherited() -> None:
    reg = ToolRegistry()
    schema: dict[str, Any] = {"parameters": {"type": "object"}}
    reg.register("gh_issue", "mcp-github", {"name": "gh_issue", **schema}, lambda a: "")
    reg.register("read_file", "file", {"name": "read_file", **schema}, lambda a: "")
    parent = {"gh_issue", "read_file"}
    on = child_tool_names(parent, limits=DelegationLimits(), depth=1, registry=reg)
    off = child_tool_names(
        parent, limits=DelegationLimits(inherit_mcp_toolsets=False), depth=1, registry=reg
    )
    assert "gh_issue" in on and "gh_issue" not in off
    assert child_tool_names({"read_file"}, limits=DelegationLimits(), depth=1, registry=reg) == {
        "read_file"
    }


# -- a real child ---------------------------------------------------------------------------------


def _tool_names(agent: Agent) -> set[str]:
    return {t["function"]["name"] for t in agent.runtime.tools}


def test_built_child_has_its_own_route_tools_and_brief(scope: Scope, fake_clock: FakeClock) -> None:
    configured = scope.with_config(
        DictConfig(
            {
                "delegation": {
                    "child_toolsets": ["file", "clarify", "todo", "delegation"],
                    "model": "pinned-model",
                    "max_iterations": 17,
                }
            }
        )
    )
    model = RoutedModel(fake_clock)

    async def main() -> tuple[Agent, Agent]:
        parent = await parent_agent(
            configured, model, ["file", "terminal", "clarify", "todo", "delegation"]
        )
        limits = DelegationLimits.from_scope(configured)
        child = await build_child(
            parent, DelegateTask("look around", "ctx"), index=0, depth=1, limits=limits
        )
        await child.agent.close()
        await parent.close()
        return parent, child.agent

    parent, child = asyncio.run(run_driven(main(), fake_clock))
    assert {"clarify", "todo_list", "delegate_task"} <= set(parent.tool_names)
    names = _tool_names(child)
    # todo is configured and the parent has it; clarify and delegate_task are blocked anyway.
    assert names == {"read_file", "write_file", "patch", "search_files", "todo_list"}
    assert child.runtime.inline.get("delegate_task") is None
    assert child.binding.state.model == "pinned-model"
    assert child.runtime.options.max_iterations == 17
    assert child.runtime.options.platform == "subagent"
    assert child.session_id != parent.session_id
    assert child.runtime.store is parent.runtime.store
    assert child.pools is parent.pools


def test_child_session_is_hidden_and_deleted_with_the_parent(
    scope: Scope, fake_clock: FakeClock
) -> None:
    handle = registry.open_unshared(scope)
    store = StateStore(handle)
    model = RoutedModel(
        fake_clock,
        parent=[
            tool_batch(("delegate_task", {"tasks": [{"goal": "count files"}]})),
            text_turn("ok"),
        ],
        children={"count files": [text_turn("there are 3")]},
    )

    async def main() -> str:
        parent = await parent_agent(scope, model, store=store)
        await parent.run_turn("delegate please")
        await parent.close()
        return parent.session_id

    try:
        parent_id = asyncio.run(run_driven(main(), fake_clock))
        with handle.read() as conn:
            kids = [r["id"] for r in deletion.children_in(conn, [parent_id])]
        assert len(kids) == 1
        child_row = sessions.get(handle, kids[0])
        assert child_row is not None
        assert child_row["source"] == "subagent"
        assert sessions.child_kind(child_row) == "delegate"
        shown = [s.id for s in listing.list_sessions(handle)]
        assert parent_id in shown and kids[0] not in shown
        with_children = listing.list_sessions(handle, listing.ListFilters(include_subagents=True))
        assert kids[0] in [s.id for s in with_children]
        assert deletion.delete_session(handle, parent_id)
        assert sessions.get(handle, kids[0]) is None
    finally:
        handle.close()


# -- approvals ------------------------------------------------------------------------------------


def test_child_approvals_reach_the_parent_with_a_label(fake_clock: FakeClock) -> None:
    fake = FakeBroker(fake_clock)
    broker = SubagentBroker(fake.broker, "subagent sa-0-abcd", parent_turn=lambda: "t-parent")

    async def main() -> object:
        answer = await broker.ask(
            "approval", {"command": "rm -rf build", "description": "recursive delete"}
        )
        with pytest.raises(RequestUnsupported):
            await broker.ask("sudo", {"reason": "needs root"})
        with pytest.raises(RequestUnsupported):
            await broker.ask("clarify", {"questions": []})
        return answer

    answer = asyncio.run(fake.run(main()))
    assert isinstance(answer, ApprovalAnswer)
    assert len(fake.requests) == 1
    request = fake.requests[0]
    assert isinstance(request, ApprovalRequest)
    assert request.description == "[subagent sa-0-abcd] recursive delete"
    assert request.turn_id == "t-parent" and request.session_id == fake.session_id
    # The child never withdraws the parent's requests (the parent's turn end does).
    assert broker.cancel_all(CancelReason.TURN_ENDED) == 0
    assert broker.human_wait_seconds() >= 0.0


# -- terminal results (R02.17) --------------------------------------------------------------------


def test_child_background_terminal_result_has_no_notify_and_the_subagent_note(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path)
    body = rig.run(rig.call("child-1", command="server", background=True, notify=True))
    assert body["notify_on_complete"] is False
    assert body["notify_unsupported"] == NOT_DELIVERABLE_NOTE

    hooks = ChildHooks(NO_HOOKS)
    raw = ToolResult.ok(json.dumps(body))
    changed = asyncio.run(hooks.transform(ToolCall("terminal", {}, "c1"), ToolContext(), raw))
    assert changed is not None
    shown = json.loads(changed.text)
    assert shown["notify_on_complete"] is False
    assert shown["notify_unsupported"] == CHILD_NOTIFY_NOTE
    assert "hand it to your parent" in shown["notify_unsupported"]
    # Other tools pass through untouched.
    other = asyncio.run(hooks.transform(ToolCall("read_file", {}, "c2"), ToolContext(), raw))
    assert other is None


def test_registry_helper_loads_delegate() -> None:
    assert subagent_registry().get("delegate_task") is not None
