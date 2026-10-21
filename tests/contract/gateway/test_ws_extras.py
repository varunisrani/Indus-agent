"""P24 integration: the projects / pet / wake / capabilities / bots / groups methods on the
daemon's WebSocket (``gateway.ws.extras``), in strict mode (every result is checked against its
declared model), over the endpoint rig of ``test_ws_endpoint``."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from indus.foundation.scope import Scope
from indus.protocol.wake import WAKE_DETECTED_EVENT, WakeDetected
from indus.service.rpc.dispatch import DEADLINES
from indus.state.registry import StoreHandle
from tests.contract.gateway.test_ws_endpoint import rig, store
from tests.unit.backends._aio import aio

__all__ = ["store"]  # the fixture, imported for pytest


@aio
async def test_the_static_groups_answer_with_their_declared_models(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    r = await rig(scope, store, tmp_path)
    client = await r.attached(surface="gui")
    projects = await client.request("projects.list", {})
    assert projects["result"]["projects"] == []
    pets = await client.request("pet.list", {})
    assert pets["result"] == {"pets": [], "active": "", "enabled": False}
    counts = await client.request("capabilities.summary", {})
    assert set(counts["result"]) == {"skills", "toolsets", "mcp_servers", "plugins"}
    status = await client.request("wake.status", {})
    assert status["result"]["enabled"] is False and status["result"]["listening"] is False
    await client.close()
    await r.close()


@aio
async def test_bots_open_mints_the_forever_chat_once_and_lists_it(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    r = await rig(scope, store, tmp_path)
    client = await r.attached(surface="gui")
    before = (await client.request("bots.list", {}))["result"]["bots"]
    mine = {b["profile"]: b for b in before}[scope.profile_id]
    assert mine["canonical"] is None  # kept as null on the wire
    first = (await client.request("bots.open", {"profile": scope.profile_id}))["result"]
    again = (await client.request("bots.open", {"profile": scope.profile_id}))["result"]
    assert first["created"] is True and again == {
        "session_id": first["session_id"],
        "created": False,
    }
    after = (await client.request("bots.list", {}))["result"]["bots"]
    listed = {b["profile"]: b for b in after}[scope.profile_id]
    assert listed["canonical"] == {"session_id": first["session_id"]}
    unknown = await client.request("bots.open", {"profile": "nobody-here"})
    assert "error" in unknown
    await client.close()
    await r.close()


@aio
async def test_a_group_room_takes_messages_and_stops(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    r = await rig(scope, store, tmp_path)
    client = await r.attached(surface="gui")
    caps = (await client.request("groups.capabilities", {}))["result"]
    assert "groups.send" in caps["methods"] and caps["driver"] is True
    members = [{"handle": "ada", "profile": "ada"}, {"handle": "bob", "profile": "bob"}]
    made = await client.request(
        "groups.create", {"room_id": "r-1", "name": "Plan", "members": members}
    )
    assert made["result"]["room"]["room_id"] == "r-1"
    sent = await client.request(
        "groups.send", {"room_id": "r-1", "client_event_id": "c-1", "text": "hello both"}
    )
    assert sent["result"]["held"] == []
    log = (await client.request("groups.log", {"room_id": "r-1"}))["result"]
    assert [e["kind"] for e in log["events"]][-1] == "message.user"
    stopped = await client.request("groups.stop", {"room_id": "r-1", "cancel_id": "s-1"})
    assert stopped["result"]["cancelled"] == [] and stopped["result"]["stopping"] == []
    missing = await client.request("groups.state", {"room_id": "nope"})
    assert "error" in missing
    await client.close()
    await r.close()


@aio
async def test_a_wake_detection_reaches_the_owning_connection_and_close_releases_it(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    r = await rig(scope, store, tmp_path)
    extras = r.endpoint.extras
    extras.bind_loop(asyncio.get_running_loop())
    released: list[object] = []
    extras.wake.service.release_owner = lambda owner: released.append(owner) or True  # type: ignore[method-assign]
    client = await r.attached(surface="gui")
    (connection,) = r.endpoint.connections.values()
    heard = WakeDetected(phrase="hey indus", surface="gui")
    # detections come from the audio thread
    worker = threading.Thread(target=extras._emit, args=(connection, WAKE_DETECTED_EVENT, heard))  # pyright: ignore[reportPrivateUsage]
    worker.start()
    worker.join(5)
    frame = await client.until(lambda f: f.get("method") == WAKE_DETECTED_EVENT)
    assert frame["params"] == {"phrase": "hey indus", "surface": "gui"}
    await client.close()
    assert released == [connection]
    await r.close()


def test_the_long_deadlines_are_the_dispatchers() -> None:
    assert DEADLINES["wake.start"] == 180.0
    assert DEADLINES["pet.generate"] > 420.0 and DEADLINES["pet.hatch"] > 3600.0


@aio
async def test_a_session_created_in_a_project_folder_joins_the_project(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    r = await rig(scope, store, tmp_path)
    client = await r.attached(surface="gui")
    folder = tmp_path / "repo"
    (folder / "src").mkdir(parents=True)
    made = await client.request("projects.create", {"name": "Repo", "folders": [str(folder)]})
    project_id = made["result"]["project"]["id"]
    created = await client.request("session.create", {"cwd": str(folder / "src")})
    session_id = created["result"]["session_id"]
    got = await client.request("projects.get", {"project": project_id})
    assert session_id in got["result"]["project"]["sessions"]
    elsewhere = await client.request("session.create", {"cwd": str(tmp_path)})
    again = await client.request("projects.get", {"project": project_id})
    assert elsewhere["result"]["session_id"] not in again["result"]["project"]["sessions"]
    await client.close()
    await r.close()
