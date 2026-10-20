"""P19-05: ``session/new`` against both hosts (R09.9 order, WSL cwd, launch policy, MCP specs)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from acp import RequestError
from acp.schema import NewSessionResponse

from indus.acp.backend import AcpBackend
from indus.foundation.scope import Scope
from indus.state import messages
from indus.state.registry import StoreHandle
from tests.contract.acp.harness import KINDS, open_harness
from tests.fakes.agent import eventually
from tests.fakes.model import text_turn
from tests.unit.backends._aio import aio


def _policy(backend: Any, session_id: str) -> Any:
    return backend.editor_policy(session_id)


INIT = {"protocolVersion": 1}


@pytest.mark.parametrize("kind", KINDS)
@aio
async def test_the_response_frame_comes_before_the_post_attach_updates(
    kind: str, scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    async with open_harness(kind, scope, store, tmp_path) as h:
        await h.call("initialize", INIT)
        result = await h.call("session/new", {"cwd": str(tmp_path), "mcpServers": []})
        NewSessionResponse.model_validate(result)
        await h.pair.settle()
        sid = result["sessionId"]
        frames = h.pair.frames
        answered = next(
            i for i, f in enumerate(frames) if (f.get("result") or {}).get("sessionId") == sid
        )
        notices = [
            (i, f["params"]["update"]["sessionUpdate"])
            for i, f in enumerate(frames)
            if f.get("method") == "session/update" and f["params"]["sessionId"] == sid
        ]
        kinds = [k for _, k in notices]
        assert kinds[0] == "available_commands_update"
        assert all(i > answered for i, _ in notices), (answered, notices)
        if kind == "attached":
            assert kinds == ["available_commands_update", "usage_update"]
            usage = h.pair.updates(sid)[1]
            assert h.daemon is not None  # the daemon's own counters (``session.usage``)
            window = h.daemon.host.service.agent(sid).binding.state.window.tokens
            assert usage == {"sessionUpdate": "usage_update", "size": window, "used": 0}
        commands = h.pair.updates(sid)[0]["availableCommands"]
        assert {"help", "model", "compress"} <= {c["name"] for c in commands}
        assert result["modes"] == {
            "currentModeId": "default",
            "availableModes": [
                {"id": "default", "name": "Default", "description": "Ask before edits."}
            ],
        }


@pytest.mark.parametrize("kind", KINDS)
@aio
async def test_a_windows_cwd_is_translated_under_wsl(
    kind: str, scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    async with open_harness(kind, scope, store, tmp_path, wsl=True) as h:
        result = await h.call("session/new", {"cwd": "E:\\Projects\\x", "mcpServers": []})
        held = h.agent.sessions.by_editor(result["sessionId"])
        assert held is not None and held.cwd == "/mnt/e/Projects/x"
    async with open_harness(kind, scope, store, tmp_path, wsl=False) as h:
        result = await h.call("session/new", {"cwd": "E:\\Projects\\x", "mcpServers": []})
        held = h.agent.sessions.by_editor(result["sessionId"])
        assert held is not None and held.cwd == "E:\\Projects\\x"


@pytest.mark.parametrize("kind", KINDS)
@aio
async def test_safe_mode_without_a_provider_is_refused_before_anything_is_created(
    kind: str, scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    env = {"INDUS_SAFE_MODE": "1", "INDUS_MODEL": "some-model"}
    async with open_harness(kind, scope, store, tmp_path, env=env) as h:
        with pytest.raises(RequestError) as refused:
            await h.call("session/new", {"cwd": str(tmp_path), "mcpServers": []})
        assert "provider" in str(refused.value)
        assert (refused.value.data or {}).get("reason") == "creation_refused"
        assert list(h.agent.sessions) == []
        if h.daemon is not None:
            assert h.daemon.host.hosted() == []


@aio
async def test_safe_mode_with_model_and_provider_creates_locally_and_needs_local_when_attached(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    env = {"INDUS_SAFE_MODE": "1", "INDUS_MODEL": "some-model", "INDUS_PROVIDER": "fake"}
    async with open_harness("local", scope, store, tmp_path, env=env) as h:
        result = await h.call("session/new", {"cwd": str(tmp_path), "mcpServers": []})
        assert result["sessionId"]
    async with open_harness("attached", scope, store, tmp_path, env=env) as h:
        with pytest.raises(RequestError) as refused:
            await h.call("session/new", {"cwd": str(tmp_path), "mcpServers": []})
        assert "--local" in str(refused.value)
        assert (refused.value.data or {}).get("reason") == "launch_unavailable"


@pytest.mark.parametrize("kind", KINDS)
@aio
async def test_editor_mcp_env_is_never_written_to_the_store(
    kind: str, scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    marker = "editor" + "-env-" + "9f3c2b7e1d"
    spec = {
        "name": "docs",
        "command": "/usr/bin/true",
        "args": ["--serve"],
        "env": [{"name": "DOCS_TOKEN", "value": marker}],
    }
    async with open_harness(kind, scope, store, tmp_path, text_turn("hello")) as h:
        result = await h.call("session/new", {"cwd": str(tmp_path), "mcpServers": [spec]})
        sid = result["sessionId"]
        backend: AcpBackend = await h.backend()
        policy = _policy(backend, sid)
        assert policy is not None and policy.server_names == ["docs"]
        if h.daemon is not None:
            assert h.daemon.editor_mcp[sid][0]["name"] == "docs"
        await backend.submit(sid, "hi", input_id="a" * 32)
        await eventually(
            lambda: any(m["role"] == "assistant" for m in messages.get(store, sid)), limit=10
        )
    home = scope.home
    found = [p for p in home.rglob("*") if p.is_file() and marker.encode() in p.read_bytes()]
    assert found == []
