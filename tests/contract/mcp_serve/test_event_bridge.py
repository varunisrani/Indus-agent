"""P19-15: ``indus mcp serve`` — the event bridge and the long poll.

The bridge is driven tick by tick (``EventBridge.tick``) and waits run on the fake clock; the
events are read back through the MCP SDK's in-memory client.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.scope import EnvView, Scope, make_scope
from indus.mcp_serve import event_bridge as bridge_mod
from indus.mcp_serve.event_bridge import QUEUE_LIMIT
from indus.mcp_serve.server import Bridge, open_bridge
from indus.mcp_serve.store import StoreReader
from indus.state import connection
from indus.state import messages as message_rows
from tests.contract.mcp_serve.conftest import World, call, connected
from tests.fakes.agent import eventually
from tests.fakes.clock import FakeClock, checkpoint
from tests.unit.backends._aio import aio

BASE = 1_767_250_000.0  # inside the store's accepted timestamp window


async def opened(scope: Scope) -> Bridge:
    return await open_bridge(scope, start_events=False)


def contents(answer: dict[str, Any]) -> list[str]:
    return [str(e.get("content")) for e in answer["events"]]


@aio
async def test_history_is_not_replayed_and_one_new_message_is_one_event(
    scope: Scope, world: World
) -> None:
    sid = world.conversation("agent:main:telegram:dm:1")
    world.message(sid, "user", "old question")
    world.message(sid, "assistant", "old answer")
    bridge = await opened(scope)
    try:
        assert await bridge.events.tick() == 0
        new_id = world.message(sid, "user", "x" * 600)
        assert await bridge.events.tick() == 1
        async with connected(bridge) as client:
            polled = await call(client, "events_poll")
            assert len(polled["events"]) == 1 and "dropped" not in polled
            event = polled["events"][0]
            assert event["type"] == "message" and event["cursor"] == 1
            assert event["session_key"] == "agent:main:telegram:dm:1"
            assert event["role"] == "user" and event["content"] == "x" * 500
            assert event["message_id"] == str(new_id) and event["timestamp"].startswith("2026-")
            assert polled["next_cursor"] == 1
            again = await call(client, "events_poll", {"after_cursor": 1})
            assert again == {"events": [], "next_cursor": 1}
    finally:
        await bridge.aclose()


@aio
async def test_an_unchanged_store_is_not_read_and_one_commit_is_one_reload(
    scope: Scope, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = world.conversation("k1")
    loads: list[int] = []
    real = bridge_mod.conversations_in

    def counted(conn: sqlite3.Connection) -> Any:
        loads.append(1)
        return real(conn)

    monkeypatch.setattr(bridge_mod, "conversations_in", counted)
    bridge = await opened(scope)
    try:
        for _ in range(10):
            await bridge.events.tick()
        assert loads == []
        world.message(sid, "user", "one write")
        for _ in range(10):
            await bridge.events.tick()
        assert loads == [1]
        assert contents(bridge.events.poll(0, None, 20)) == ["one write"]
    finally:
        await bridge.aclose()


@aio
async def test_a_commit_from_another_connection_is_seen_whatever_the_file_times(
    scope: Scope, world: World
) -> None:
    """The gate is the change counter: a commit in the same mtime tick as the baseline, or one
    that only reached the WAL (where the store runs in WAL mode), still opens it."""
    sid = world.conversation("k1")
    bridge = await opened(scope)
    try:
        path = connection.default_store_path(scope)
        files = [p for p in path.parent.iterdir() if p.name.startswith(path.name)]
        pinned = {p: p.stat().st_mtime_ns for p in files}
        raw = sqlite3.connect(str(path))
        with raw:
            for i in range(5):
                raw.execute(
                    "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)",
                    (sid, "assistant", f"reply {i}", BASE + i),
                )
        raw.close()
        for file, mtime in pinned.items():
            os.utime(file, ns=(mtime, mtime))
        await bridge.events.tick()
        assert contents(bridge.events.poll(0, None, 20)) == [f"reply {i}" for i in range(5)]
    finally:
        await bridge.aclose()


@aio
async def test_new_conversations_are_delivered_but_an_old_session_is_not_replayed(
    scope: Scope, world: World
) -> None:
    old = "s-old"
    world.conversation("first", session_id=old)
    world.message(old, "user", "history of an old chat")
    bridge = await opened(scope)
    try:
        fresh = world.conversation("agent:main:discord:dm:new", platform="discord")
        world.message(fresh, "user", "hello after the start")
        # a second key pointed at the old session (as a rotation or a shared transcript would)
        world.conversation("second", session_id=old)
        await bridge.events.tick()
        events = bridge.events.poll(0, None, 20)["events"]
        assert [(e["session_key"], e["content"]) for e in events] == [
            ("agent:main:discord:dm:new", "hello after the start")
        ]
        world.message(old, "assistant", "news in the old chat")
        await bridge.events.tick()
        later = bridge.events.poll(1, None, 20)["events"]
        assert sorted(e["session_key"] for e in later) == ["first", "second"]
        assert {e["content"] for e in later} == {"news in the old chat"}
    finally:
        await bridge.aclose()


@aio
async def test_rows_copied_by_a_compaction_are_not_news(scope: Scope, world: World) -> None:
    sid = world.conversation("k1")
    carried = world.message(sid, "user", "said before the start")
    bridge = await opened(scope)
    try:
        world.message(sid, "user", "said after the start")
        await bridge.events.tick()
        assert contents(bridge.events.poll(0, None, 20)) == ["said after the start"]

        def copy(conn: sqlite3.Connection) -> list[int]:
            return message_rows.clone_rows_in(conn, sid, [carried])

        world.store.write(copy)
        await bridge.events.tick()
        assert contents(bridge.events.poll(1, None, 20)) == []
    finally:
        await bridge.aclose()


@aio
async def test_order_is_the_row_id_when_timestamps_go_backwards(scope: Scope, world: World) -> None:
    a = world.conversation("a")
    b = world.conversation("b")
    bridge = await opened(scope)
    try:
        world.message(b, "user", "one", timestamp=BASE + 30)
        world.message(a, "user", "two", timestamp=BASE + 20)
        world.message(b, "assistant", "three", timestamp=BASE + 10)
        world.message(a, "tool", "not text", tool_call_id="t")
        await bridge.events.tick()
        events = bridge.events.poll(0, None, 20)["events"]
        assert [e["content"] for e in events] == ["one", "two", "three"]
        assert [e["cursor"] for e in events] == [1, 2, 3]
        only_a = bridge.events.poll(0, "a", 20)["events"]
        assert [e["content"] for e in only_a] == ["two"]
    finally:
        await bridge.aclose()


@aio
async def test_the_queue_keeps_the_latest_1000_and_reports_what_was_dropped(
    scope: Scope, world: World
) -> None:
    sid = world.conversation("k1")
    bridge = await opened(scope)
    try:
        message_rows.append_batch(
            world.store,
            sid,
            [{"role": "user", "content": f"m{i}"} for i in range(QUEUE_LIMIT + 1)],
        )
        assert await bridge.events.tick() == QUEUE_LIMIT + 1
        async with connected(bridge) as client:
            first = await call(client, "events_poll", {"limit": 999})
            assert first["dropped"] == 1 and len(first["events"]) == 200
            assert first["events"][0]["cursor"] == 2 and first["events"][0]["content"] == "m1"
            nxt = await call(client, "events_poll", {"after_cursor": first["next_cursor"]})
            assert "dropped" not in nxt and nxt["events"][0]["cursor"] == 202
            assert len(nxt["events"]) == 20
            bad = await call(client, "events_poll", {"after_cursor": "x", "limit": "x"})
            assert bad["dropped"] == 1 and len(bad["events"]) == 20
    finally:
        await bridge.aclose()


@aio
async def test_events_wait_answers_at_its_timeout_exactly(
    scope: Scope, world: World, fake_clock: FakeClock
) -> None:
    world.conversation("k1")
    bridge = await opened(scope)
    try:
        async with connected(bridge) as client:
            task = asyncio.create_task(call(client, "events_wait", {"timeout_ms": 1000}))
            for _ in range(4):
                await eventually(lambda: fake_clock.pending > 0)
                fake_clock.advance(0.2)
                await checkpoint()
            await eventually(lambda: fake_clock.pending > 0)
            assert not task.done()
            assert fake_clock.monotonic() == pytest.approx(0.8)
            fake_clock.advance(0.2)
            answer = await asyncio.wait_for(task, 5)
            assert answer == {"event": None, "reason": "timeout"}
            zero = await call(client, "events_wait", {"timeout_ms": 0})
            assert zero == {"event": None, "reason": "timeout"}
    finally:
        await bridge.aclose()


@aio
async def test_events_wait_wakes_on_a_new_event_and_honours_the_filter(
    scope: Scope, world: World, fake_clock: FakeClock
) -> None:
    a = world.conversation("a")
    b = world.conversation("b")
    bridge = await opened(scope)
    try:
        async with connected(bridge) as client:
            task = asyncio.create_task(
                call(client, "events_wait", {"session_key": "b", "timeout_ms": 60_000})
            )
            await eventually(lambda: fake_clock.pending > 0)
            world.message(a, "user", "for a")
            await bridge.events.tick()
            await eventually(lambda: fake_clock.pending > 0)
            assert not task.done()
            world.message(b, "user", "for b")
            await bridge.events.tick()
            answer = await asyncio.wait_for(task, 5)
            assert answer["event"]["content"] == "for b" and answer["event"]["cursor"] == 2
            assert fake_clock.monotonic() == 0.0  # no time had to pass
            ready = await call(client, "events_wait", {"after_cursor": "x", "timeout_ms": "x"})
            assert ready["event"]["cursor"] == 1
    finally:
        await bridge.aclose()


@aio
async def test_a_store_that_appears_after_the_start_is_watched(scope: Scope) -> None:
    bridge = await opened(scope)
    try:
        assert await bridge.events.tick() == 0
        made = World(scope)
        try:
            sid = made.conversation("k1")
            made.message(sid, "user", "the first message ever")
        finally:
            made.close()
        await bridge.events.tick()
        assert contents(bridge.events.poll(0, None, 20)) == ["the first message ever"]
    finally:
        await bridge.aclose()


@aio
async def test_the_background_task_ticks_on_the_clock(
    scope: Scope, world: World, fake_clock: FakeClock
) -> None:
    sid = world.conversation("k1")
    bridge = await open_bridge(scope)
    try:
        await eventually(lambda: fake_clock.pending > 0)
        world.message(sid, "user", "seen by the watcher")
        fake_clock.advance(0.2)
        await eventually(lambda: bridge.events.cursor == 1)
        assert contents(bridge.events.poll(0, None, 20)) == ["seen by the watcher"]
    finally:
        await bridge.aclose()


@pytest.mark.parametrize("name", ["home#1", "home%23", "home?x"])
def test_the_change_counter_watches_the_real_store_under_a_uri_special_home(
    tmp_path: Path, fake_clock: FakeClock, name: str
) -> None:
    home = tmp_path / name / "profiles" / "work"
    home.mkdir(parents=True)
    special = make_scope(
        home, env=EnvView.from_process().with_overlay({"INDUS_HOME": str(home)}), clock=fake_clock
    )
    made = World(special)
    reader = StoreReader(special)
    try:
        made.conversation("k1")
        before = reader.data_version()
        made.conversation("k2")
        assert reader.data_version() != before
        assert (
            reader.read(
                lambda conn: conn.execute("SELECT COUNT(*) FROM gateway_routes").fetchone()[0]
            )
            == 2
        )
    finally:
        reader.close()
        made.close()
