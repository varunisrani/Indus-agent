"""DEMO (P24 "Done when"): a group room where two bots discuss a task until they are told to stop,
on the real gateway daemon (wave 42 wiring of P24-06).

One multiplexing host serves the launch profile (``default``, member ``alpha``) and profile ``b``
(member ``beta``); its room loop drives ``<root>/rooms.db``. A person's message goes in through
``groups.send`` (what the desktop and TUI call over the daemon's WebSocket); the members answer in
turn, each in its own profile's session service (the fake model plays both). A stop directive
holds ``alpha`` and ``groups.stop`` stops the turn alpha is running; afterwards ``beta`` answers
alone and the room goes quiet.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from indus.gateway.rooms.driver import RoomDriver
from indus.gateway.rooms.loop import RoomPart
from indus.gateway.rooms.rpc import GroupsRpc
from indus.state.gateway_runtime import EXIT_OK
from indus.state.registry import StoreHandle
from indus.state.rooms import RoomStore
from tests.fakes.model import Finish, Stall, Text, Turn, text_turn
from tests.integration.gateway.daemon_kit import fly, model_server
from tests.integration.gateway.multiplex.host_kit import World

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX locks and sockets")

MEMBERS = [{"handle": "alpha", "profile": "default"}, {"handle": "beta", "profile": "b"}]


def sessions_in(store: StoreHandle) -> int:
    with store.read() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])


def test_a_room_with_two_members_taking_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = World(tmp_path, monkeypatch, config="")
    server = model_server(
        text_turn("Four."),
        text_turn("Agreed: four."),
        Turn((Text(("Let me double-check",)), Stall(10_000.0), Finish("stop"))),
        text_turn("Summary: two plus two is four."),
    )

    async def main() -> None:
        host = world.host(server, tmp_path, parts=[RoomPart(poll=0.05)])
        flight = await fly(host)
        store = RoomStore(world.scope())
        rpc = GroupsRpc(store, RoomDriver(store), process_generation=host.instance_id)

        def log(kind: str) -> list[dict[str, Any]]:
            events = store.read_events("r1", since_seq=0, limit=500).events
            return [e.to_dict() for e in events if e.kind == kind]

        def said() -> list[tuple[str, str]]:
            return [(e["actor"]["id"], e["payload"]["text"]) for e in log("message.member")]

        def statuses() -> list[str]:
            return [t.status for t in rpc.driver.list_tasks("r1")]

        try:
            mux = host.mux
            assert mux is not None
            await flight.until(lambda: "b" in mux.daemons)
            secondary = mux.daemons["b"]
            before = (sessions_in(host.store), sessions_in(secondary.store))
            rpc.call("groups.create", {"room_id": "r1", "name": "Maths", "members": MEMBERS})
            rpc.call(
                "groups.send",
                {"room_id": "r1", "client_event_id": "c1", "text": "Discuss: what is 2 + 2?"},
            )
            await flight.until(lambda: len(said()) == 2)
            assert said() == [("alpha", "Four."), ("beta", "Agreed: four.")]
            # alpha's third turn runs (the model stalls on it)
            await flight.until(lambda: statuses() == ["settled", "settled", "running"])
            held = rpc.call(
                "groups.send", {"room_id": "r1", "client_event_id": "c2", "text": "@alpha stop"}
            )
            assert held["held"] == ["alpha"]
            stopped = rpc.call("groups.stop", {"room_id": "r1", "cancel_id": "stop-1"})
            assert len(stopped["stopping"]) == 1
            await flight.until(lambda: statuses() == ["settled", "settled", "cancelled"])
            rpc.call(
                "groups.send",
                {"room_id": "r1", "client_event_id": "c3", "text": "@beta sum it up, please"},
            )
            await flight.until(lambda: len(said()) == 3)
            await flight.until(lambda: host.parts[0].busy_reason() is None)
            assert said()[-1] == ("beta", "Summary: two plus two is four.")
            assert statuses() == ["settled", "settled", "cancelled", "settled"]  # alpha is held
            # Each member's turns ran in its own profile: alpha twice here, beta twice in b.
            after = (sessions_in(host.store), sessions_in(secondary.store))
            assert (after[0] - before[0], after[1] - before[1]) == (2, 2)
            bodies = [str(r.body) for r in server.model_requests]
            assert "Four." in bodies[1] and "Discuss: what is 2 + 2?" in bodies[0]
            assert [e["payload"]["reason"] for e in log("turn.cancelled")] == ["stopped"]
        finally:
            store.close()
            assert await flight.stop() == EXIT_OK

    asyncio.run(main())
