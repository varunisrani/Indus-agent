"""ACP sessions on the daemon (P19 follow-ups): ``no_mcp`` leaves the profile's configured MCP
servers out, and a model switch is written to the session row and comes back after a restart."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.service import editor_mcp
from indus.service.session_mutations import stored_pick
from indus.state import registry, sessions
from indus.state.registry import StoreHandle
from tests.fakes.agent import DictConfig
from tests.fakes.model import FakeModelServer, fake_route, script_of, text_turn
from tests.unit.backends._aio import aio
from tests.unit.service.test_session_mutations import Rig, open_rig

NO_MCP = {"platform_toolsets": {"acp": ["file", "no_mcp"]}}


@pytest.fixture
def store(scope: Scope) -> Iterator[StoreHandle]:
    handle = registry.open_unshared(scope)
    yield handle
    handle.close()


class Upkeep:
    """The service's MCP step: adds the umbrella and, here, one configured server's toolset."""

    def __init__(self) -> None:
        self.attached: list[Any] = []

    async def start(self) -> None:
        return None

    def selection(self, selection: Sequence[str] | None) -> list[str] | None:
        if selection is None:
            return None
        chosen = list(selection)
        return chosen if "mcp" in chosen else [*chosen, "mcp", "mcp-files"]

    def attach(self, agent: Any) -> None:
        self.attached.append(agent)

    async def shutdown(self) -> None:
        return None


async def _rig(scope: Scope, store: StoreHandle, tmp_path: Path, **kw: Any) -> Rig:
    rig = await open_rig(scope, store, tmp_path, text_turn("one"), text_turn("two"), **kw)
    rig.service.mcp = Upkeep()  # pyright: ignore[reportAttributeAccessIssue]
    return rig


def test_the_no_mcp_marker_and_the_filter() -> None:
    assert editor_mcp.without_config_servers(
        ["file", "mcp", "mcp-files", "mcp-editor-docs", "mcpish"]
    ) == ["file", "mcp-editor-docs", "mcpish"]


@aio
async def test_no_mcp_leaves_the_configured_servers_out_of_an_acp_session(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    rig = await _rig(scope.with_config(DictConfig(NO_MCP)), store, tmp_path, toolsets=None)
    sid = await rig.create()
    assert rig.service.agent(sid).toolset_selection == ["file"]
    # A session of another surface keeps what the MCP step gave it.
    tui = await rig.create("tui", toolsets=["file"])
    assert rig.service.agent(tui).toolset_selection == ["file", "mcp", "mcp-files"]
    await rig.close()


@aio
async def test_without_no_mcp_an_acp_session_gets_the_umbrella(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    config = DictConfig({"platform_toolsets": {"acp": ["file"]}})
    rig = await _rig(scope.with_config(config), store, tmp_path, toolsets=None)
    sid = await rig.create()
    assert rig.service.agent(sid).toolset_selection == ["file", "mcp", "mcp-files"]
    await rig.close()


@aio
async def test_no_mcp_also_holds_for_a_selection_the_client_named(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    rig = await _rig(scope.with_config(DictConfig(NO_MCP)), store, tmp_path, toolsets=None)
    sid = await rig.create(toolsets=["file", "web"])
    assert rig.service.agent(sid).toolset_selection == ["file", "web"]
    await rig.close()


@aio
async def test_a_daemon_model_switch_writes_the_row_and_survives_a_restart(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    rig = await open_rig(scope, store, tmp_path, text_turn("one"), toolsets=[])
    sid = await rig.create()
    await rig.host.wait(await rig.turn(sid, "hi", "t-1"))  # the row is written by the reply
    done = await rig.mutate(sid, "model", model="other-model", provider="other")
    assert done.status == "applied"
    row = sessions.get(store, sid)
    assert row is not None and (row["model"], row["billing_provider"]) == ("other-model", "other")
    await rig.close()

    # A restart: the stored ACP session comes back on the model its row names.
    rig = await open_rig(scope, store, tmp_path, text_turn("two"), toolsets=[])
    await rig.host.resume(sid, source="acp")
    await rig.host.wait(await rig.turn(sid, "again", "t-2"))
    assert rig.server.model_requests[-1].body["model"] == "other-model"
    await rig.close()


@aio
async def test_a_refused_or_guarded_switch_writes_nothing(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    rig = await open_rig(scope, store, tmp_path, text_turn("one"), toolsets=[])
    sid = await rig.create()
    await rig.host.wait(await rig.turn(sid, "hi", "t-1"))
    before = sessions.get(store, sid)
    rig.host.mutations.model_guard = lambda used, window: "too small"
    asked = await rig.mutate(sid, "model", model="small-model")
    assert asked.status == "confirm"
    after = sessions.get(store, sid)
    assert before is not None and after is not None and after["model"] == before["model"]
    await rig.close()


def test_stored_pick(scope: Scope) -> None:
    server = FakeModelServer(script_of(text_turn("x")), clock=scope.clock)
    route = fake_route(server)
    assert stored_pick(None, route) is None
    assert stored_pick({"model": ""}, route) is None
    assert stored_pick({"model": route.model, "billing_provider": route.provider}, route) is None
    assert stored_pick({"model": route.model}, route) is None
    assert stored_pick({"model": "other", "billing_provider": None}, route) == ("other", None)
    assert stored_pick({"model": route.model, "billing_provider": "b"}, route) == (route.model, "b")
