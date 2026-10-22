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


def _p(method: str, **params: Any) -> Any:
    from indus.protocol.methods import METHODS

    return METHODS[method].params.model_validate(params)
