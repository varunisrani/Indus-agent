"""Wave 47 (RS3): room events are pushed to the connections that watch the room.

``groups.watch`` on the daemon's WebSocket (``WsExtras``) registers the calling connection; every
event appended to the room afterwards (a person's message, a member's turn and reply, a stop, an
approval) reaches it as a ``groups.event`` notification, in order and once; ``groups.unwatch``
and a closing connection stop it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from indus.foundation.scope import Scope
from indus.gateway.rooms.watch import RoomWatch
from indus.gateway.ws.extras import WsExtras
from indus.protocol.groups import GroupsRoomParams, GroupsWatchParams
from indus.protocol.methods import NOTIFICATIONS
from indus.state.rooms import RoomStore


class Conn:
    def __init__(self) -> None:
        self.frames: list[tuple[str, dict[str, Any]]] = []

    def notify(self, method: str, params: Mapping[str, Any]) -> None:
        self.frames.append((method, dict(params)))

    def kinds(self) -> list[str]:
        return [p["event"]["kind"] for m, p in self.frames if m == "groups.event"]


class Table:
    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}

    def add(self, name: str, handler: Any) -> None:
        self.handlers[name] = handler


def test_watchers_get_every_new_room_event_in_order_once(scope: Scope) -> None:
    assert "groups.event" in NOTIFICATIONS
    extras = WsExtras(scope, instance_id="inst-1")
    table = Table()
    extras.register(table)
    h = table.handlers
    one, two = Conn(), Conn()

    async def main() -> None:
        extras.bind_loop(asyncio.get_running_loop())
        members = [{"handle": "alpha", "profile": "default"}]
        await h["groups.create"](None, _p("groups.create", room_id="r1", name="P", members=members))
        await h["groups.send"](
            None, _p("groups.send", room_id="r1", client_event_id="c0", text="before")
        )
        watched = await h["groups.watch"](one, GroupsWatchParams(room_id="r1"))
        assert watched.latest_seq == 1
        await h["groups.watch"](two, GroupsWatchParams(room_id="r1", since_seq=0))
        await h["groups.send"](
            None, _p("groups.send", room_id="r1", client_event_id="c1", text="hi")
        )
        # a write by another store (the room loop's) is pushed too
        other = RoomStore(scope)
        try:
            other.append_event(
                "r1",
                "reply:t1",
                "message.member",
                {"kind": "member", "id": "alpha"},
                {"text": "hello"},
                authority=other.gateway_id,
                epoch=1,
            )
        finally:
            other.close()
        await extras.room_watch.poll()
        await extras.room_watch.poll()  # nothing twice
        await h["groups.unwatch"](one, GroupsRoomParams(room_id="r1"))
        await h["groups.send"](
            None, _p("groups.send", room_id="r1", client_event_id="c2", text="x")
        )
        extras.connection_closed(two)
        await extras.room_watch.poll()
        assert not extras.room_watch.watching()
        await extras.aclose_watch()

    try:
        asyncio.run(main())
    finally:
        extras.close()
    assert one.kinds() == ["message.user", "message.member"]
    assert two.kinds() == ["message.user", "message.user", "message.member"]
    assert all(p["room_id"] == "r1" for _, p in one.frames + two.frames)
    seqs = [p["event"]["seq"] for _, p in two.frames]
    assert seqs == sorted(set(seqs))


def test_a_watcher_added_while_a_read_runs_misses_no_event(scope: Scope) -> None:
    """Wave 50 integration: a connection that starts watching from an earlier cursor while a
    poll's read runs off the loop must not take that page (it starts past its cursor); it gets
    every event, in order, on the next poll."""
    store = RoomStore(scope)
    store.create_room("r1", "P", [{"handle": "alpha", "profile": "default"}])
    one, two = Conn(), Conn()
    watch: RoomWatch | None = None

    def read(room_id: str, since: int) -> Any:
        if since == 1 and watch is not None and not two.frames:
            watch.watch(two, room_id, 0)  # what ``groups.watch`` does while the read runs
            store.append_event(
                room_id,
                "c1",
                "message.user",
                {"kind": "user", "id": "owner"},
                {"text": "hi"},
                authority=store.gateway_id,
                epoch=1,
            )
        return store.read_events(room_id, since_seq=since, limit=500, include_disbanded=True)

    async def main() -> None:
        nonlocal watch
        watch = RoomWatch(read)
        store.append_event(
            "r1",
            "c0",
            "message.user",
            {"kind": "user", "id": "owner"},
            {"text": "before"},
            authority=store.gateway_id,
            epoch=1,
        )
        watch.watch(one, "r1", 1)
        await watch.poll()  # reads from 1; ``two`` arrives at cursor 0 during the read
        assert two.kinds() == []
        await watch.poll()
        await watch.aclose()

    try:
        asyncio.run(main())
    finally:
        store.close()
    assert one.kinds() == ["message.user"]
    assert [p["event"]["seq"] for _, p in two.frames] == [1, 2]


def _p(method: str, **params: Any) -> Any:
    from indus.protocol.methods import METHODS

    return METHODS[method].params.model_validate(params)
