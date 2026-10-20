"""P19-06: ``session/load``, ``session/resume`` and ``session/fork`` against both hosts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from acp.schema import ForkSessionResponse, LoadSessionResponse, SessionNotification

from indus.acp.backend import AcpBackend
from indus.foundation.scope import Scope
from indus.state import sessions
from indus.state.registry import StoreHandle
from tests.contract.acp.harness import KINDS, open_harness, seed_session
from tests.unit.backends._aio import aio


def _policy(backend: Any, session_id: str) -> Any:
    return backend.editor_policy(session_id)


ROWS: list[dict[str, Any]] = [
    {"role": "user", "content": "fix the bug in a.py"},
    {
        "role": "assistant",
        "content": "Let me look.",
        "reasoning": "The file is small; read it first.",
        "tool_calls": [
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "read_file", "arguments": json.dumps({"path": "a.py"})},
            }
        ],
    },
    {"role": "tool", "tool_call_id": "call-1", "tool_name": "read_file", "content": "print(1)"},
    {"role": "assistant", "content": "It prints 1; nothing to fix."},
]
HISTORY_KINDS = [
    "user_message_chunk",
    "agent_thought_chunk",
    "agent_message_chunk",
    "tool_call",
    "tool_call_update",
    "agent_message_chunk",
]
SPEC = {"name": "docs", "command": "/usr/bin/true", "args": [], "env": []}


def _response_index(frames: list[dict[str, Any]], request_method_result: str) -> int:
    return max(
        i
        for i, f in enumerate(frames)
        if "id" in f and "method" not in f and request_method_result in str(f.get("result"))
    )


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("method", ["session/load", "session/resume"])
@aio
async def test_the_whole_history_streams_before_the_response(
    kind: str, method: str, scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    seed_session(store, "s-hist", cwd=str(tmp_path), rows=ROWS)
    async with open_harness(kind, scope, store, tmp_path) as h:
        result = await h.call(
            method, {"sessionId": "s-hist", "cwd": str(tmp_path), "mcpServers": []}
        )
        LoadSessionResponse.model_validate(result)
        await h.pair.settle()
        frames = h.pair.frames
        answered = next(i for i, f in enumerate(frames) if "id" in f and "method" not in f)
        history = [
            (i, f["params"])
            for i, f in enumerate(frames)
            if f.get("method") == "session/update"
            and f["params"]["update"]["sessionUpdate"] in set(HISTORY_KINDS)
        ]
        assert [p["update"]["sessionUpdate"] for _, p in history] == HISTORY_KINDS
        assert all(i < answered for i, _ in history), (answered, history)
        assert all(p["sessionId"] == "s-hist" for _, p in history)
        for _, params in history:
            SessionNotification.model_validate(params)  # every update is a vendor shape
        call = history[3][1]["update"]
        assert call["toolCallId"] == "call-1" and call["rawInput"] == {"path": "a.py"}
        done = history[4][1]["update"]
        assert done["status"] == "completed"
        assert done["content"][0]["content"]["text"] == "print(1)"
        later = [
            f["params"]["update"]["sessionUpdate"]
            for f in frames[answered:]
            if f.get("method") == "session/update"
        ]
        assert later[:1] == ["available_commands_update"]


@pytest.mark.parametrize("kind", KINDS)
@aio
async def test_a_compressed_session_loaded_by_its_old_id_is_addressed_by_that_id(
    kind: str, scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    seed_session(store, "s-root", cwd=str(tmp_path), rows=ROWS[:1])
    sessions.end(store, "s-root", "compression")
    seed_session(store, "s-tip", cwd=str(tmp_path), rows=ROWS[1:])
    store.write(
        lambda conn: conn.execute(
            "UPDATE sessions SET parent_session_id = 's-root' WHERE id = 's-tip'"
        )
    )
    async with open_harness(kind, scope, store, tmp_path) as h:
        await h.call(
            "session/load", {"sessionId": "s-root", "cwd": str(tmp_path), "mcpServers": []}
        )
        await h.pair.settle()
        addressed = {
            f["params"]["sessionId"] for f in h.pair.frames if f.get("method") == "session/update"
        }
        assert addressed == {"s-root"}
        assert h.pair.kinds("s-root")[:2] == ["agent_thought_chunk", "agent_message_chunk"]
        held = h.agent.sessions.by_editor("s-root")
        assert held is not None and held.key in {"s-tip", "s-root"}
        assert h.agent.sessions.key_for("s-root") == held.key


@pytest.mark.parametrize("kind", KINDS)
@aio
async def test_a_load_from_another_folder_moves_the_session(
    kind: str, scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    seed_session(store, "s-move", cwd=str(old), rows=ROWS[:1])
    seed_session(store, "s-nocwd", cwd=None, rows=ROWS[:1])
    async with open_harness(kind, scope, store, tmp_path) as h:
        await h.call("session/load", {"sessionId": "s-move", "cwd": str(new), "mcpServers": []})
        row = sessions.get(store, "s-move")
        assert row is not None and row["cwd"] == str(new)
        moved = h.agent.sessions.by_editor("s-move")
        assert moved is not None and moved.cwd == str(new)
        await h.call("session/load", {"sessionId": "s-nocwd", "cwd": str(new), "mcpServers": []})
        texts = [
            u["content"]["text"]
            for u in h.pair.updates("s-nocwd")
            if u["sessionUpdate"] == "agent_message_chunk"
        ]
        assert any("no working folder" in t for t in texts)
        row = sessions.get(store, "s-nocwd")
        assert row is not None and not row["cwd"]


@pytest.mark.parametrize("kind", KINDS)
@aio
async def test_fork_gets_the_parents_mcp_servers_or_the_editors_own(
    kind: str, scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    seed_session(store, "s-parent", cwd=str(tmp_path), rows=ROWS)
    async with open_harness(kind, scope, store, tmp_path) as h:
        await h.call(
            "session/load", {"sessionId": "s-parent", "cwd": str(tmp_path), "mcpServers": [SPEC]}
        )
        result = await h.call(
            "session/fork", {"sessionId": "s-parent", "cwd": str(tmp_path), "mcpServers": []}
        )
        ForkSessionResponse.model_validate(result)
        child = result["sessionId"]
        assert child != "s-parent"
        backend: AcpBackend = await h.backend()
        policy = _policy(backend, child)
        assert policy is not None and policy.server_names == ["docs"]
        if h.daemon is not None:
            assert [s["name"] for s in h.daemon.editor_mcp[child]] == ["docs"]
        other = dict(SPEC, name="tickets")
        again = await h.call(
            "session/fork", {"sessionId": "s-parent", "cwd": str(tmp_path), "mcpServers": [other]}
        )
        policy = _policy(backend, again["sessionId"])
        assert policy is not None and policy.server_names == ["tickets"]
        row = sessions.get(store, child)
        assert row is not None and row["parent_session_id"] == "s-parent"
        assert h.agent.sessions.by_editor(child) is not None
        # the fork is a new thread: no history replay for the child
        assert h.pair.kinds(child)[:1] == ["available_commands_update"] or h.pair.kinds(child) == []


@pytest.mark.parametrize("kind", KINDS)
@aio
async def test_an_unknown_session_is_not_found(
    kind: str, scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    from acp import RequestError

    async with open_harness(kind, scope, store, tmp_path) as h:
        with pytest.raises(RequestError) as missing:
            await h.call(
                "session/load", {"sessionId": "nope", "cwd": str(tmp_path), "mcpServers": []}
            )
        assert missing.value.code == -32002
