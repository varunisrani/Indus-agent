"""``delegate_task``: schema, limits and input normalisation (P10-01)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.tools.builtin.delegate import (
    DELEGATE_SCHEMA,
    INLINE_ONLY_TEXT,
    MAX_TASK_IMAGES,
    TOOL_NAME,
    TOOLSET,
    TRUSTED_FIELDS,
    DelegateInputError,
    DelegationLimits,
    normalise,
    register_tools,
    schema_overrides,
)
from indus.tools.calls import ToolCall, ToolContext
from indus.tools.definitions import definitions
from indus.tools.dispatch import dispatch
from indus.tools.registry import ToolRegistry
from indus.tools.toolsets import resolve
from tests.fakes.agent import DictConfig


def registry() -> ToolRegistry:
    reg = ToolRegistry()
    register_tools(reg)
    return reg


# -- limits ---------------------------------------------------------------------------------------


def test_defaults_follow_d_16_24(scope: Scope) -> None:
    limits = DelegationLimits.from_scope(scope)
    assert limits == DelegationLimits()
    assert (
        limits.max_concurrent_children,
        limits.max_depth,
        limits.max_iterations,
        limits.max_async_children,
        limits.summary_max_chars,
        limits.oneshot_max_children,
    ) == (10, 1, 250, 3, 24_000, 2)
    assert limits.child_toolsets == ("terminal", "file", "web")


def test_limits_from_config_with_floors(scope: Scope) -> None:
    configured = scope.with_config(
        DictConfig(
            {
                "delegation": {
                    "max_concurrent_children": 4,
                    "summary_max_chars": 50,  # below the 2,000 floor
                    "max_depth": 0,  # below the floor of 1
                    "child_toolsets": ["file"],
                    "model": " pinned-model ",
                    "worktree": True,
                    "max_iterations": "nonsense",
                }
            }
        )
    )
    limits = DelegationLimits.from_scope(configured)
    assert limits.max_concurrent_children == 4
    assert limits.summary_max_chars == 2_000
    assert limits.max_depth == 1
    assert limits.child_toolsets == ("file",)
    assert limits.model == "pinned-model"
    assert limits.worktree is True
    assert limits.max_iterations == 250


# -- schema ---------------------------------------------------------------------------------------


def test_registered_in_its_toolset_and_in_core() -> None:
    reg = registry()
    entry = reg.get(TOOL_NAME)
    assert entry is not None and entry.toolset == TOOLSET
    assert resolve("delegation") == [TOOL_NAME]
    assert TOOL_NAME in resolve("core")


def test_schema_snapshot_shows_the_live_cap(scope: Scope) -> None:
    configured = scope.with_config(DictConfig({"delegation": {"max_concurrent_children": 7}}))
    tools = definitions(registry(), [TOOL_NAME], scope=configured)
    fn = tools[0]["function"]
    assert fn["name"] == TOOL_NAME
    tasks = fn["parameters"]["properties"]["tasks"]
    assert "at most 7 per call" in tasks["description"]
    assert set(tasks["items"]["properties"]) == {
        "goal",
        "context",
        "output_schema",
        "images",
        "group",
    }
    assert tasks["items"]["required"] == ["goal"]
    assert fn["parameters"]["properties"]["action"]["enum"] == ["spawn", "list", "steer", "stop"]
    assert "cannot delegate further" in fn["description"]
    # Fields only trusted config may set are never offered to the model.
    offered = set(fn["parameters"]["properties"]) | set(tasks["items"]["properties"])
    assert not offered & TRUSTED_FIELDS
    assert "role" not in offered and "background" not in offered
    # The static schema is never changed by the per-session rewrite.
    static_tasks = DELEGATE_SCHEMA["parameters"]["properties"]["tasks"]
    assert "per session" in static_tasks["description"]


def test_deeper_nesting_is_described_when_allowed(scope: Scope) -> None:
    configured = scope.with_config(DictConfig({"delegation": {"max_depth": 2}}))
    text = str(schema_overrides(configured)["description"])
    assert "delegation.max_depth = 2" in text


# -- normalisation --------------------------------------------------------------------------------


def test_legacy_single_goal_is_folded_into_one_task() -> None:
    request = normalise(
        {
            "goal": "Read setup.cfg",
            "context": "repo at /work",
            "output_schema": {"type": "object"},
            "images": "shot.png",
        },
        max_children=10,
    )
    assert request.action == "spawn"
    assert len(request.tasks) == 1
    task = request.tasks[0]
    assert (task.goal, task.context) == ("Read setup.cfg", "repo at /work")
    assert task.output_schema == {"type": "object"}
    assert task.images == ("shot.png",)


def test_tasks_win_over_a_stray_goal_and_empty_tasks_count_as_absent() -> None:
    both = normalise({"goal": "ignored", "tasks": [{"goal": "kept"}]}, max_children=10)
    assert [t.goal for t in both.tasks] == ["kept"]
    empty = normalise({"goal": "only goal", "tasks": []}, max_children=10)
    assert [t.goal for t in empty.tasks] == ["only goal"]


def test_tasks_as_a_json_string_are_parsed() -> None:
    raw = json.dumps([{"goal": "a"}, {"goal": "b", "context": "c"}])
    request = normalise({"tasks": raw}, max_children=10)
    assert [t.goal for t in request.tasks] == ["a", "b"]
    with pytest.raises(DelegateInputError, match="not valid JSON"):
        normalise({"tasks": "[{goal"}, max_children=10)


def test_role_and_background_are_accepted_and_ignored() -> None:
    request = normalise(
        {"tasks": [{"goal": "x", "role": "orchestrator"}], "role": "leaf", "background": False},
        max_children=10,
    )
    assert len(request.tasks) == 1
    assert {"role", "background", "tasks[0].role"} <= set(request.ignored)


def test_trusted_fields_are_stripped_from_the_normalised_task() -> None:
    task_args: dict[str, Any] = {
        "goal": "x",
        "toolsets": ["terminal"],
        "model": "big-model",
        "worktree": True,
        "acp_command": "evil",
        "max_iterations": 9999,
    }
    request = normalise(
        {"tasks": [task_args], "model": "other", "toolsets": ["web"]}, max_children=10
    )
    task = request.tasks[0]
    assert set(vars(task)) == {"goal", "context", "output_schema", "images", "group"}
    assert {"model", "toolsets", "tasks[0].model", "tasks[0].worktree"} <= set(request.ignored)


def test_image_limits() -> None:
    eight = [f"img{i}.png" for i in range(MAX_TASK_IMAGES)]
    assert (
        len(normalise({"tasks": [{"goal": "g", "images": eight}]}, max_children=1).tasks[0].images)
        == 8
    )
    with pytest.raises(DelegateInputError, match="9 images"):
        normalise({"tasks": [{"goal": "g", "images": [*eight, "nine.png"]}]}, max_children=1)
    with pytest.raises(DelegateInputError, match="non-empty"):
        normalise({"tasks": [{"goal": "g", "images": ["a.png", "  "]}]}, max_children=1)


def test_bad_tasks_are_errors_for_the_model() -> None:
    with pytest.raises(DelegateInputError, match="no tasks given"):
        normalise({}, max_children=10)
    with pytest.raises(DelegateInputError, match="task 1 has no goal"):
        normalise({"tasks": [{"goal": "a"}, {"goal": "  "}]}, max_children=10)
    with pytest.raises(DelegateInputError, match="task 0 must be an object"):
        normalise({"tasks": ["just text"]}, max_children=10)
    with pytest.raises(DelegateInputError, match="at most 2 subagents"):
        normalise({"tasks": [{"goal": "a"}, {"goal": "b"}, {"goal": "c"}]}, max_children=2)
    with pytest.raises(DelegateInputError, match="output_schema must be a JSON Schema object"):
        normalise({"tasks": [{"goal": "a", "output_schema": [1]}]}, max_children=2)
    parsed = normalise(
        {"tasks": [{"goal": "a", "output_schema": '{"type": "object"}'}]}, max_children=2
    )
    assert parsed.tasks[0].output_schema == {"type": "object"}


def test_control_actions() -> None:
    assert normalise({"action": "LIST"}, max_children=1).action == "list"
    steer = normalise(
        {"action": "steer", "subagent_id": "sa-0-1", "message": "hurry"}, max_children=1
    )
    assert (steer.action, steer.subagent_id, steer.message) == ("steer", "sa-0-1", "hurry")
    with pytest.raises(DelegateInputError, match="needs message"):
        normalise({"action": "steer", "subagent_id": "sa-0-1"}, max_children=1)
    with pytest.raises(DelegateInputError, match="needs subagent_id"):
        normalise({"action": "stop"}, max_children=1)
    with pytest.raises(DelegateInputError, match="unknown action"):
        normalise({"action": "pause"}, max_children=1)


# -- inline only ----------------------------------------------------------------------------------


def test_direct_registry_dispatch_returns_the_inline_only_error() -> None:
    reg = registry()
    call = ToolCall(TOOL_NAME, {"goal": "x"}, "c1")
    # The default dispatch refuses every inline name before it looks the tool up.
    refused = asyncio.run(dispatch(call, ToolContext(), registry=reg))
    assert refused.is_error and refused.error_type == "inline_tool"
    assert "run by the agent loop" in refused.text
    # Reaching the registry handler anyway gives the same answer.
    handled = asyncio.run(dispatch(call, ToolContext(), registry=reg, inline=frozenset()))
    assert handled.is_error
    assert json.loads(handled.text)["error"] == INLINE_ONLY_TEXT
