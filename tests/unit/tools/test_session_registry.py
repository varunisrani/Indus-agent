"""``SessionRegistry``: one session's tools over the shared registry (P19-13 on the daemon)."""

from __future__ import annotations

from typing import Any

from indus.foundation.scope import Scope
from indus.tools import toolsets
from indus.tools.registry import ToolRegistry, scope_key
from indus.tools.session_registry import SessionRegistry


def _schema(name: str) -> dict[str, Any]:
    return {"name": name, "description": name, "parameters": {"type": "object", "properties": {}}}


def _handler(**_: Any) -> str:
    return "ok"


def test_reads_see_both_sides_and_writes_stay_in_the_session(scope: Scope) -> None:
    shared = ToolRegistry()
    shared.register("shared_tool", "files", _schema("shared_tool"), _handler)
    own = SessionRegistry(shared)
    profile = scope_key(scope)
    own.register(
        "mcp__editor_docs__read", "mcp-editor-docs", _schema("x"), _handler, profile=profile
    )
    own.set_toolset_alias("mcp", "mcp-editor-docs", profile=profile)
    assert own.get("shared_tool", scope) is not None
    assert own.get("mcp__editor_docs__read", scope) is not None
    assert {"shared_tool", "mcp__editor_docs__read"} <= set(own.names(scope))
    assert own.names_in_toolset("mcp", scope) == ["mcp__editor_docs__read"]
    assert own.own_names() == ["mcp__editor_docs__read"]
    # The shared registry, and so every other session, never sees the session's tool.
    assert shared.get("mcp__editor_docs__read", scope) is None
    assert "mcp-editor-docs" not in shared.toolset_names(scope)
    assert "mcp__editor_docs__read" not in toolsets.resolve("all", shared, scope)
    assert "mcp__editor_docs__read" in toolsets.resolve("all", own, scope)


def test_the_generation_moves_with_either_side(scope: Scope) -> None:
    shared = ToolRegistry()
    own = SessionRegistry(shared)
    start = own.generation
    shared.register("a", "files", _schema("a"), _handler)
    after_shared = own.generation
    own.register("b", "mine", _schema("b"), _handler)
    assert start < after_shared < own.generation
    assert own.probes is shared.probes


def test_a_session_tool_wins_over_a_shared_one_of_the_same_name(scope: Scope) -> None:
    shared = ToolRegistry()
    shared.register("same", "files", _schema("same"), _handler)
    own = SessionRegistry(shared)
    profile = scope_key(scope)
    own.register("same", "mine", _schema("same"), _handler, profile=profile)
    found = own.get("same", scope)
    assert found is not None and found.toolset == "mine"
    assert shared.get("same", scope) is not None and shared.get("same", scope).toolset == "files"  # pyright: ignore[reportOptionalMemberAccess]
