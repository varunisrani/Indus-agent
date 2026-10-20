"""``session.mutate`` on the daemon host, one operation at a time (D-16.602, P19-04/05/06/12/13).

Every test runs the real ``DaemonSessionHost`` over the fake model. Editor MCP servers are the
scripted in-memory ones (``tests.fakes.mcp.scripted``), connected through the real P13 client.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from indus.core.persist import StateStore
from indus.core.turn_state import TurnState
from indus.foundation.scope import Scope
from indus.mcp.naming import tool_name
from indus.protocol.attach import SessionMutateParams, SessionMutateResult
from indus.protocol.errors import IDEMPOTENCY_CONFLICT, INVALID_PARAMS, SESSION_BUSY
from indus.protocol.methods import ProtocolError, SessionCreateParams
from indus.service import editor_mcp
from indus.service.daemon_host import DaemonSessionHost
from indus.service.session_service import SessionService
from indus.service.viewers import Viewer
from indus.state import messages, registry, sessions
from indus.state.registry import StoreHandle
from indus.tools.calls import ToolCall, ToolResult
from indus.tools.session_registry import SessionRegistry
from tests.fakes.agent import eventually, fake_options
from tests.fakes.mcp.scripted import FakeFleet
from tests.fakes.model import FakeModelServer, Turn, fake_route, script_of, text_turn, tool_batch
from tests.unit.backends._aio import aio

CWD = os.path.abspath(os.sep + os.path.join("work", "repo"))
OTHER = os.path.abspath(os.sep + os.path.join("work", "other"))
DOCS = {"name": "docs", "command": "docs-server", "args": ["--serve"], "env": []}


@pytest.fixture
def store(scope: Scope) -> Iterator[StoreHandle]:
    handle = registry.open_unshared(scope)
    yield handle
    handle.close()


class Holder:
    """An inline tool that holds the turn until released (or the turn is interrupted)."""

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.entered = 0

    async def __call__(self, call: ToolCall, state: TurnState) -> ToolResult:
        self.entered += 1
        waits = {
            asyncio.ensure_future(self.gate.wait()),
            asyncio.ensure_future(state.control.stop.wait()),
        }
        await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        for task in waits:
            task.cancel()
        return ToolResult("held")


async def inline[T](fn: Callable[[], T]) -> T:
    return fn()


@dataclass
class Rig:
    host: DaemonSessionHost
    server: FakeModelServer
    fleet: FakeFleet
    events: list[dict[str, Any]]
    calls: int = 0

    @property
    def service(self) -> SessionService:
        return self.host.service

    async def create(self, source: str = "acp", **params: Any) -> str:
        viewer = Viewer.of(f"c-{len(self.host.hosted())}", source, send=self.events.append)
        made = await self.host.create(
            SessionCreateParams(cwd=CWD, **params), source=source, viewer=viewer
        )
        return made.session_id

    async def mutate(self, sid: str, operation: str, **payload: Any) -> SessionMutateResult:
        self.calls += 1
        return await self.mutate_as(sid, operation, f"r-{self.calls}", payload)

    async def mutate_as(
        self,
        sid: str,
        operation: str,
        request_id: str,
        payload: dict[str, Any],
        generation: int | None = None,
    ) -> SessionMutateResult:
        key = self.host.key_of(sid)
        assert key is not None
        params = SessionMutateParams.model_validate(
            {
                "session_id": sid,
                "operation": operation,
                "payload": payload,
                "request_id": request_id,
                "generation": generation,
            }
        )
        return await self.host.mutations.mutate(key, params)

    async def turn(self, sid: str, text: str, request_id: str) -> str:
        receipt = await self.host.submit(
            sid, text, principal="attach:acp", request_id=request_id, surface="acp"
        )
        return receipt.admission_id

    def notices(self) -> list[str]:
        return [str(e.get("text")) for e in self.events if e.get("type") == "notice"]

    async def close(self) -> None:
        await self.host.aclose()


async def open_rig(
    scope: Scope,
    store: StoreHandle,
    tmp_path: Path,
    *turns: Turn,
    tools: dict[str, Any] | None = None,
    toolsets: list[str] | None = None,
) -> Rig:
    server = FakeModelServer(script_of(*turns), clock=scope.clock)
    options = fake_options(server, session_id=None, inline=dict(tools or {}))
    service = SessionService(
        scope,
        fake_route(server),
        toolsets=toolsets,
        options=options,
        store=StateStore(store),
        lock_dir=tmp_path / "locks",
    )
    host = await DaemonSessionHost.open(service, store, instance_id="daemon-1", offload=inline)
    fleet = FakeFleet(scope.clock)  # pyright: ignore[reportArgumentType]
    host.mutations.opener = fleet
    base = fake_route(server)
    host.mutations.resolve_route = lambda model, provider: replace(
        base, model=model, provider=provider or base.provider
    )
    return Rig(host, server, fleet, [])


def editor_tools(rig: Rig, sid: str) -> list[str]:
    return [n for n in rig.service.agent(sid).tool_names if n.startswith("mcp__editor")]


# -- editor MCP ------------------------------------------------------------------------------------


@aio
async def test_editor_servers_reach_their_session_only_and_the_record_holds_no_secret(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    rig = await open_rig(scope, store, tmp_path, toolsets=[])
    secret = "editor" + "-value-" + "77aa11"
    spec = {**DOCS, "env": [{"name": "DOCS_TOKEN", "value": secret}]}
    sid = await rig.create()
    other = await rig.create("tui", toolsets=None)
    done = await rig.mutate(sid, "editor_mcp", mcp_servers=[spec])
    assert done.status == "applied" and done.servers == ["docs"]
    assert editor_tools(rig, sid) == [tool_name("editor-docs", "read")]
    agent = rig.service.agent(sid)
    assert isinstance(agent.options.registry, SessionRegistry)
    # Another session — even one that selects every toolset — never sees them.
    assert editor_tools(rig, other) == []
    shared = rig.service.agent(other).options.registry
    assert shared is not None and tool_name("editor-docs", "read") not in shared.names(scope)
    row = sessions.get(store, sid)
    assert row is not None
    config = sessions.parse_model_config(row["model_config"])
    record = config[editor_mcp.RECORD_KEY]
    assert set(record["fingerprints"]) == {"docs"} and "docs" in record["schemas"]
    assert secret not in json.dumps(config)
    found = [p for p in scope.home.rglob("*") if p.is_file() and secret.encode() in p.read_bytes()]
    assert found == []
    await rig.close()


@aio
async def test_a_changed_spec_is_refused_and_the_same_one_after_a_restart_connects(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    rig = await open_rig(scope, store, tmp_path, toolsets=[])
    sid = await rig.create()
    await rig.mutate(sid, "editor_mcp", mcp_servers=[DOCS])
    changed = {**DOCS, "args": ["--serve", "--other"]}
    refused = await rig.mutate(sid, "editor_mcp", mcp_servers=[changed])
    assert refused.status == "refused" and refused.reason == "editor_mcp_changed"
    assert refused.refused_servers == ["docs"]
    assert rig.host.mutations.servers(sid)[0]["args"] == ["--serve"]  # nothing changed
    again = await rig.mutate(sid, "editor_mcp", mcp_servers=[DOCS])
    assert again.status == "applied" and again.servers == ["docs"]
    added = await rig.mutate(sid, "editor_mcp", mcp_servers=[DOCS, {**DOCS, "name": "tickets"}])
    assert added.status == "applied" and sorted(added.servers or []) == ["docs", "tickets"]
    assert rig.fleet.servers["editor-docs"].opens == 1  # an unchanged server is not restarted
    # A daemon restart: specs are gone from memory, the record stays in the row.
    await rig.close()
    rig = await open_rig(scope, store, tmp_path, toolsets=[])
    await rig.host.resume(sid, source="acp")
    assert editor_tools(rig, sid) == []  # nothing until the editor sends its servers again
    bad = await rig.mutate(sid, "editor_mcp", mcp_servers=[changed])
    assert bad.status == "refused" and bad.refused_servers == ["docs"]
    good = await rig.mutate(sid, "editor_mcp", mcp_servers=[DOCS])
    assert good.status == "applied" and editor_tools(rig, sid) == [tool_name("editor-docs", "read")]
    await rig.close()


@aio
async def test_a_bad_server_list_is_invalid_and_names_no_spec_value(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    rig = await open_rig(scope, store, tmp_path, toolsets=[])
    sid = await rig.create()
    secret = "argv" + "-secret-" + "5c5c"
    for servers in ([{"command": secret}], [{"name": "x", "type": "ftp", "command": secret}]):
        with pytest.raises(ProtocolError) as bad:
            await rig.mutate(sid, "editor_mcp", mcp_servers=servers)
        assert bad.value.error is INVALID_PARAMS and secret not in str(bad.value.message)
    with pytest.raises(ProtocolError):
        await rig.mutate(sid, "editor_mcp", mcp_servers=[DOCS] * 17)
    huge = {**DOCS, "args": ["x" * 70000]}
    with pytest.raises(ProtocolError):
        await rig.mutate(sid, "editor_mcp", mcp_servers=[huge])
    tui = await rig.create("tui")
    with pytest.raises(ProtocolError) as not_acp:
        await rig.mutate(tui, "editor_mcp", mcp_servers=[DOCS])
    assert not_acp.value.error is INVALID_PARAMS  # editor servers belong to ACP sessions
    await rig.close()


@aio
async def test_servers_sent_mid_turn_wait_for_the_next_turn_and_close_with_the_session(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    holder = Holder()
    rig = await open_rig(
        scope,
        store,
        tmp_path,
        tool_batch(("hold", {})),
        text_turn("one"),
        text_turn("two"),
        tools={"hold": holder},
        toolsets=[],
    )
    sid = await rig.create()
    first = await rig.turn(sid, "work", "t-1")
    await eventually(lambda: holder.entered == 1)
    waiting = await rig.mutate(sid, "editor_mcp", mcp_servers=[DOCS])
    assert waiting.status == "deferred" and editor_tools(rig, sid) == []
    holder.gate.set()
    await rig.host.wait(first)
    second = await rig.turn(sid, "more", "t-2")
    await rig.host.wait(second)
    assert editor_tools(rig, sid) == [tool_name("editor-docs", "read")]
    live = rig.fleet.servers["editor-docs"].live
    assert live is not None
    await rig.host.close_session(sid)
    assert rig.host.mutations.servers(sid) == []
    await rig.close()


# -- branch, workspace -----------------------------------------------------------------------------


@aio
async def test_branch_copies_the_session_with_the_parents_servers_or_the_named_ones(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    rig = await open_rig(scope, store, tmp_path, text_turn("hello"), toolsets=[])
    sid = await rig.create()
    await rig.mutate(sid, "editor_mcp", mcp_servers=[DOCS])
    await rig.host.wait(await rig.turn(sid, "hi", "t-1"))
    child = await rig.mutate_as(sid, "branch", "b-1", {})
    assert child.status == "applied" and child.session_id and child.servers == ["docs"]
    row = sessions.get(store, child.session_id)
    assert row is not None and row["parent_session_id"] == sid
    assert [m["content"] for m in messages.get(store, child.session_id)][-1] == "hello"
    again = await rig.mutate_as(sid, "branch", "b-1", {})
    assert again == child  # the same request id never makes a second copy
    with pytest.raises(ProtocolError) as reused:
        await rig.mutate_as(sid, "rename", "b-1", {"title": "x"})
    assert reused.value.error is IDEMPOTENCY_CONFLICT
    named = await rig.mutate(sid, "branch", mcp_servers=[{**DOCS, "name": "tickets"}])
    assert named.servers == ["tickets"]
    assert named.session_id is not None
    await rig.host.resume(named.session_id, source="acp")
    assert editor_tools(rig, named.session_id) == [tool_name("editor-tickets", "read")]
    assert editor_mcp.load_record(store, named.session_id) is not None
    await rig.close()


@aio
async def test_idle_only_changes_are_refused_while_a_turn_runs(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    holder = Holder()
    rig = await open_rig(
        scope, store, tmp_path, tool_batch(("hold", {})), text_turn("done"), tools={"hold": holder}
    )
    sid = await rig.create()
    admission = await rig.turn(sid, "work", "t-1")
    await eventually(lambda: holder.entered == 1)
    for operation, payload in (
        ("branch", {}),
        ("workspace", {"cwd": OTHER}),
        ("rewind", {"count": 1}),
        ("reset", {}),
        ("compress", {}),
    ):
        with pytest.raises(ProtocolError) as busy:
            await rig.mutate(sid, operation, **payload)
        assert busy.value.error is SESSION_BUSY, operation
    stale = await rig.mutate_as(sid, "model", "m-1", {"model": "other"}, generation=99)
    assert stale.status == "refused" and stale.reason == "stale_generation"
    holder.gate.set()
    await rig.host.wait(admission)
    await rig.close()


@aio
async def test_workspace_moves_the_row_and_the_agent(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    rig = await open_rig(scope, store, tmp_path, text_turn("hello"), toolsets=[])
    sid = await rig.create()
    await rig.host.wait(await rig.turn(sid, "hi", "t-1"))
    moved = await rig.mutate(sid, "workspace", cwd=OTHER)
    assert moved.status == "applied" and moved.cwd == OTHER
    row = sessions.get(store, sid)
    assert row is not None and row["cwd"] == OTHER
    assert str(rig.service.agent(sid).options.cwd) == OTHER
    nothing = await rig.mutate(sid, "workspace", cwd=" ")
    assert nothing.status == "refused" and nothing.reason == "no_folder"
    await rig.close()


# -- model -----------------------------------------------------------------------------------------


@aio
async def test_a_model_switch_applies_now_or_asks_first(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    rig = await open_rig(scope, store, tmp_path, text_turn("one"), toolsets=[])
    sid = await rig.create()
    rig.host.mutations.model_guard = lambda used, window: "too small for this conversation"
    asked = await rig.mutate(sid, "model", model="small-model")
    assert asked.status == "confirm" and asked.message == "too small for this conversation"
    assert rig.service.agent(sid).binding.state.model != "small-model"  # nothing changed
    done = await rig.mutate(sid, "model", model="small-model", confirm=True)
    assert done.status == "applied" and done.model == "small-model"
    await rig.host.wait(await rig.turn(sid, "hi", "t-1"))
    assert rig.server.model_requests[-1].body["model"] == "small-model"
    empty = await rig.mutate(sid, "model", model="")
    assert empty.status == "refused" and empty.reason == "no_model"
    await rig.close()


@aio
async def test_a_pick_made_mid_turn_lands_at_the_next_turn_and_survives_a_rewind(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    holder = Holder()
    rig = await open_rig(
        scope,
        store,
        tmp_path,
        tool_batch(("hold", {})),
        text_turn("one"),
        text_turn("two"),
        text_turn("three"),
        tools={"hold": holder},
        toolsets=[],
    )
    sid = await rig.create()
    first = await rig.turn(sid, "work", "t-1")
    await eventually(lambda: holder.entered == 1)
    running = rig.host.state(sid).running
    picked = await rig.mutate_as(sid, "model", "m-1", {"model": "next-model"}, generation=running)
    assert picked.status == "deferred" and picked.model == "next-model"
    holder.gate.set()
    await rig.host.wait(first)
    held_models = {r.body["model"] for r in rig.server.model_requests}
    assert "next-model" not in held_models  # the running turn kept its route
    await rig.host.wait(await rig.turn(sid, "again", "t-2"))
    assert rig.server.model_requests[-1].body["model"] == "next-model"
    taken = await rig.mutate(sid, "rewind", count=1)  # rebuilds the agent from the store
    assert taken.status == "applied" and taken.removed and taken.text == "again"
    await rig.host.wait(await rig.turn(sid, "once more", "t-3"))
    assert rig.server.model_requests[-1].body["model"] == "next-model"
    await rig.close()


@aio
async def test_a_deferred_pick_refused_at_the_boundary_is_announced(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    holder = Holder()
    rig = await open_rig(
        scope,
        store,
        tmp_path,
        tool_batch(("hold", {})),
        text_turn("one"),
        text_turn("two"),
        tools={"hold": holder},
        toolsets=[],
    )
    sid = await rig.create()
    first = await rig.turn(sid, "work", "t-1")
    await eventually(lambda: holder.entered == 1)
    assert (await rig.mutate(sid, "model", model="gone-model")).status == "deferred"

    def unavailable(model: str, provider: str | None) -> Any:
        raise RuntimeError("no such model")

    rig.host.mutations.resolve_route = unavailable
    holder.gate.set()
    await rig.host.wait(first)
    await rig.host.wait(await rig.turn(sid, "again", "t-2"))
    assert any("gone-model was not applied" in text for text in rig.notices())
    await rig.close()


# -- history, title, commands ----------------------------------------------------------------------


@aio
async def test_rewind_and_reset_change_the_stored_conversation(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    rig = await open_rig(scope, store, tmp_path, text_turn("one"), text_turn("two"), toolsets=[])
    sid = await rig.create()
    await rig.host.wait(await rig.turn(sid, "first", "t-1"))
    await rig.host.wait(await rig.turn(sid, "second", "t-2"))
    taken = await rig.mutate(sid, "rewind", count=1)
    assert taken.status == "applied" and taken.text == "second" and taken.removed == 2
    assert [m["content"] for m in messages.get(store, sid)] == ["first", "one"]
    cleared = await rig.mutate(sid, "reset")
    assert cleared.status == "applied" and (cleared.removed or 0) >= 2
    assert messages.get(store, sid) == []
    nothing = await rig.mutate(sid, "rewind")
    assert nothing.removed == 0
    await rig.close()


@aio
async def test_rename_before_the_first_reply_writes_the_row_and_tells_the_viewers(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    rig = await open_rig(scope, store, tmp_path, toolsets=[])
    sid = await rig.create()
    assert sessions.get(store, sid) is None
    named = await rig.mutate(sid, "rename", title="  Fix the build  ")
    assert named.status == "applied" and named.title == "Fix the build"
    row = sessions.get(store, sid)
    assert row is not None and row["title"] == "Fix the build" and row["source"] == "acp"
    await eventually(lambda: any(e.get("type") == "session.title" for e in rig.events))
    blank = await rig.mutate(sid, "rename", title="")
    assert blank.status == "refused"
    await rig.close()


@aio
async def test_information_commands_answer_text_and_others_are_refused(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    rig = await open_rig(scope, store, tmp_path, toolsets=[])
    sid = await rig.create()
    status = await rig.mutate(sid, "command", name="status", arg="")
    assert status.status == "applied" and status.text and "Idle." in status.text
    assert sid in status.text
    version = await rig.mutate(sid, "command", name="version")
    assert version.text and version.text.startswith("Indus Agent ")
    usage = await rig.mutate(sid, "command", name="usage")
    assert usage.text and "Model calls:" in usage.text
    tools = await rig.mutate(sid, "command", name="tools")
    assert tools.status == "applied" and tools.text
    other = await rig.mutate(sid, "command", name="quit")
    assert other.status == "refused" and other.reason == "unsupported_command"
    await rig.close()


@aio
async def test_compress_with_too_little_history_says_nothing(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    rig = await open_rig(scope, store, tmp_path, toolsets=[])
    sid = await rig.create()
    done = await rig.mutate(sid, "compress")
    assert done.status == "applied" and done.outcome == "nothing" and done.session_id == sid
    await rig.close()


@aio
async def test_an_acp_session_without_toolsets_gets_the_editor_bundle(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    rig = await open_rig(scope, store, tmp_path, toolsets=None)
    sid = await rig.create()
    other = await rig.create("tui")
    assert rig.service.agent(sid).toolset_selection == [editor_mcp.ACP_BUNDLE]
    assert rig.service.agent(other).toolset_selection is None
    await rig.close()
