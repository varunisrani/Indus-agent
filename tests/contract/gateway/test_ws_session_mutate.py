"""``session.mutate`` and ``acp.editor_policy`` over the daemon's WebSocket (D-16.602).

The endpoint runs over a real ``DaemonSessionHost`` on the fake model (``test_ws_endpoint.rig``);
the editor MCP servers are scripted in-memory servers behind the real P13 client.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from indus.foundation.scope import Scope
from indus.protocol.attach import ACP_EDITOR_POLICY
from indus.protocol.errors import (
    IDEMPOTENCY_CONFLICT,
    INVALID_PARAMS,
    SESSION_BUSY,
    SESSION_NOT_FOUND,
)
from indus.state import registry, sessions
from indus.state.registry import StoreHandle
from tests.contract.gateway.test_ws_endpoint import done, error_of, rig
from tests.fakes.agent import eventually
from tests.fakes.mcp.scripted import FakeFleet
from tests.fakes.model import text_turn, tool_batch
from tests.fakes.session_host import Holder
from tests.unit.backends._aio import aio

DOCS = {"name": "docs", "command": "docs-server", "args": [], "env": []}


@pytest.fixture
def store(scope: Scope) -> Iterator[StoreHandle]:
    handle = registry.open_unshared(scope)
    yield handle
    handle.close()


def mutate(sid: str, operation: str, request_id: str, **payload: object) -> dict[str, object]:
    return {
        "session_id": sid,
        "operation": operation,
        "payload": payload,
        "request_id": request_id,
    }


@aio
async def test_the_daemon_advertises_the_editor_policy_and_serves_the_mutation(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    r = await rig(scope, store, tmp_path, text_turn("hello"))
    r.host.mutations.opener = FakeFleet(scope.clock)  # pyright: ignore[reportArgumentType]
    client = await r.attached("acp")
    other = await r.attached("tui")
    described = await client.request("runtime.describe")
    assert ACP_EDITOR_POLICY in described["result"]["capabilities"]
    assert "acp" in described["result"]["creatable_sources"]
    sid = (await client.request("session.create", {"cwd": str(tmp_path)}))["result"]["session_id"]

    added = await client.request(
        "session.mutate", mutate(sid, "editor_mcp", "r-1", mcp_servers=[DOCS])
    )
    assert added["result"] == {"status": "applied", "servers": ["docs"], "changed": []}
    foreign = await other.request("session.mutate", mutate(sid, "rename", "r-2", title="x"))
    assert error_of(foreign) == SESSION_NOT_FOUND.code  # another connection's session
    unknown = await client.request("session.mutate", mutate(sid, "teleport", "r-3"))
    assert error_of(unknown) == INVALID_PARAMS.code
    reused = await client.request("session.mutate", mutate(sid, "rename", "r-1", title="x"))
    assert error_of(reused) == IDEMPOTENCY_CONFLICT.code
    again = await client.request(
        "session.mutate", mutate(sid, "editor_mcp", "r-1", mcp_servers=[DOCS])
    )
    assert again["result"] == added["result"]

    await client.request("prompt.submit", {"session_id": sid, "text": "hi", "request_id": "p-1"})
    await client.until(done(sid))
    fork = await client.request("session.mutate", mutate(sid, "branch", "r-4"))
    child = fork["result"]["session_id"]
    assert fork["result"]["status"] == "applied" and fork["result"]["servers"] == ["docs"]
    row = sessions.get(store, child)
    assert row is not None and row["parent_session_id"] == sid
    named = await client.request("session.mutate", mutate(sid, "rename", "r-5", title="Build"))
    assert named["result"] == {"status": "applied", "title": "Build"}
    await client.close()
    await other.close()
    await r.close()


@aio
async def test_idle_only_changes_answer_busy_while_a_turn_runs(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    holder = Holder()
    r = await rig(
        scope, store, tmp_path, tool_batch(("hold", {})), text_turn("done"), tools={"hold": holder}
    )
    client = await r.attached("acp")
    sid = (await client.request("session.create", {}))["result"]["session_id"]
    first = await client.request("prompt.submit", {"session_id": sid, "text": "work"})
    generation = first["result"]["generation"]
    await eventually(lambda: holder.entered == 1)
    busy = await client.request("session.mutate", mutate(sid, "reset", "r-1"))
    assert error_of(busy) == SESSION_BUSY.code
    picked = await client.request(
        "session.mutate", {**mutate(sid, "model", "r-2", model="later"), "generation": generation}
    )
    assert picked["result"] == {"status": "deferred", "model": "later"}
    stale = await client.request(
        "session.mutate", {**mutate(sid, "model", "r-3", model="x"), "generation": generation + 5}
    )
    assert stale["result"]["status"] == "refused"
    assert stale["result"]["reason"] == "stale_generation"
    holder.gate.set()
    await client.until(done(sid))
    await client.close()
    await r.close()
