"""P17-14: subscriptions, cross-surface events, presence, list updates and slow-viewer overflow."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from indus.core.persist import StateStore
from indus.foundation.scope import Scope
from indus.protocol.errors import INVALID_PARAMS, SESSION_NOT_FOUND
from indus.protocol.methods import ProtocolError
from indus.service.daemon_host import DaemonSessionHost
from indus.service.event_log import EventLog, ReplayLimits
from indus.service.fences import Responded
from indus.service.subscriptions import (
    GAP_EVENT,
    RESYNC_METHOD,
    UPDATED_EVENT,
    VIEWERS_EVENT,
    Outbox,
    OutboxLimits,
    SessionWatch,
    Subscriptions,
    capabilities_from,
    serialize,
)
from indus.state import registry
from indus.state.registry import StoreHandle
from tests.fakes.agent import eventually
from tests.fakes.model import text_turn, tool_batch
from tests.fakes.session_host import Holder, confirm, open_host
from tests.unit.backends._aio import aio


@pytest.fixture
def store(scope: Scope) -> Iterator[StoreHandle]:
    handle = registry.open_unshared(scope)
    yield handle
    handle.close()


class Conn:
    """One client connection: an outbox and its subscriptions (nothing drains it by itself)."""

    def __init__(
        self,
        host: DaemonSessionHost,
        cid: str,
        surface: str = "cli",
        *,
        limits: OutboxLimits | None = None,
        watch: SessionWatch | None = None,
        features: tuple[str, ...] = ("server_requests",),
    ) -> None:
        self.outbox = Outbox(limits)
        self.subs = Subscriptions(
            host,
            cid,
            self.outbox,
            watch=watch,
            surface=surface,
            capabilities=capabilities_from(features),
            strict=True,
        )
        self.frames: list[dict[str, Any]] = []

    def drain(self) -> list[dict[str, Any]]:
        while (text := self.outbox.take()) is not None:
            self.frames.append(json.loads(text))
        return self.frames

    def events(self, *types: str) -> list[dict[str, Any]]:
        found = [f["params"] for f in self.drain() if "method" in f and "seq" in f["params"]]
        return [e for e in found if not types or e["type"] in types]

    def methods(self) -> list[str]:
        return [str(f["method"]) for f in self.drain() if "method" in f]


def text_of(events: list[dict[str, Any]]) -> str:
    return "".join(str(e["text"]) for e in events if e["type"] == "message.delta")


def seqs(events: list[dict[str, Any]]) -> list[int]:
    return [int(e["seq"]) for e in events]


async def run_turn(host: DaemonSessionHost, sid: str, text: str, rid: str) -> None:
    receipt = await host.submit(sid, text, principal="cli:a", request_id=rid)
    done = await host.wait(receipt.admission_id)
    assert done.outcome == "completed", done
    host.event_log.flush()


# -- fan-out across surfaces ----------------------------------------------------------------------


@aio
async def test_a_subscriber_sees_every_delta_of_another_surfaces_turn_in_order(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    host = await open_host(scope, store, tmp_path, text_turn("Hel", "lo", " there"))
    a, b = Conn(host, "c-a"), Conn(host, "t-b", "tui")
    sid = (await host.create(source="cli")).session_id
    a.subs.adopt(sid, cursor=0)
    started = await b.subs.subscribe(sid)
    assert started.gap is None and started.replayed == 0
    await run_turn(host, sid, "hi", "r1")
    a_events, b_events = a.events(), b.events()
    assert text_of(b_events) == text_of(a_events) == "Hello there"
    assert seqs(b_events) == list(range(started.last_seq + 1, seqs(b_events)[-1] + 1))
    tail = [e for e in a_events if e["seq"] > started.last_seq]
    assert tail == b_events  # the same numbered events, in the same order
    assert all(e["replay_epoch"] == host.replay_epoch for e in b_events)
    assert a.subs.violations == [] and b.subs.violations == []
    await host.aclose()


@aio
async def test_subscribe_from_a_cursor_replays_once_then_continues_live(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    host = await open_host(scope, store, tmp_path, text_turn("one"), text_turn("two"))
    sid = (await host.create()).session_id
    await run_turn(host, sid, "first", "r1")
    cursor = host.event_log.last_seq(sid) - 2
    late = Conn(host, "c-late")
    found = await late.subs.subscribe(sid, host.replay_epoch, cursor)
    assert found.replayed >= 2 and found.gap is None
    await run_turn(host, sid, "second", "r2")
    events = late.events()
    numbers = seqs(events)
    assert numbers == list(range(cursor + 1, numbers[-1] + 1))  # no gap, no duplicate
    assert numbers[-1] == host.event_log.last_seq(sid)
    assert text_of(events).endswith("two")
    await host.aclose()


@aio
async def test_another_epoch_or_an_evicted_cursor_gives_a_gap_then_live_events(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    small = EventLog(clock=scope.clock, limits=ReplayLimits(events_per_session=3))
    host = await open_host(
        scope, store, tmp_path, text_turn("one"), text_turn("two"), event_log=small
    )
    sid = (await host.create()).session_id
    await run_turn(host, sid, "first", "r1")
    old = Conn(host, "c-old")
    found = await old.subs.subscribe(sid, "an-earlier-run", 4)
    assert found.gap is not None and found.gap.reason == "epoch_changed"
    evicted = Conn(host, "c-evicted")
    found = await evicted.subs.subscribe(sid, host.replay_epoch, 0)
    assert found.gap is not None and found.gap.reason == "evicted"
    first = evicted.drain()[0]
    assert first["method"] == GAP_EVENT and first["params"]["last_seq"] == found.last_seq
    await run_turn(host, sid, "second", "r2")
    assert text_of(evicted.events()) == "two"
    await host.aclose()


# -- slow viewers ---------------------------------------------------------------------------------


@aio
async def test_an_overflowing_viewer_drops_to_a_gap_and_recovers_by_subscribing_again(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    host = await open_host(scope, store, tmp_path, text_turn("a", "b", "c"), text_turn("again"))
    fast = Conn(host, "c-fast")
    slow = Conn(host, "t-slow", "tui", limits=OutboxLimits(viewer_events=2))
    sid = (await host.create()).session_id
    fast.subs.adopt(sid, cursor=0)
    await slow.subs.subscribe(sid)
    await run_turn(host, sid, "go", "r1")
    await eventually(lambda: not slow.subs.is_viewer(sid))
    frames = slow.drain()
    gaps = [f for f in frames if f.get("method") == GAP_EVENT]
    assert len(gaps) == 1 and gaps[0]["params"]["reason"] == "overflow"
    assert slow.outbox.dropped(sid)
    assert text_of(fast.events()) == "abc"  # the other viewer lost nothing
    # Recover: reload a snapshot (the stored transcript), then subscribe live from there.
    stored = await StateStore(store).load(sid)
    assert stored[-1].content == "abc"
    slow.outbox.limits = OutboxLimits()  # it keeps up from now on
    again = await slow.subs.subscribe(sid)
    assert again.gap is None and again.replayed == 0
    assert not slow.outbox.dropped(sid) and slow.subs.is_viewer(sid)
    await run_turn(host, sid, "more", "r2")
    assert text_of(slow.events()) == "again"
    await host.aclose()


def test_outbox_overflow_keeps_other_lanes_and_counts_bytes() -> None:
    told: list[str] = []
    outbox = Outbox(
        OutboxLimits(viewer_events=2),
        on_overflow=lambda lane: told.append(lane) or {"jsonrpc": "2.0", "method": GAP_EVENT},
    )
    for n in range(2):
        assert outbox.put_event("s1", {"method": "message.delta", "params": {"seq": n}})
        assert outbox.put_event("s2", {"method": "message.delta", "params": {"seq": n}})
    assert not outbox.put_event("s1", {"method": "message.delta", "params": {"seq": 3}})
    assert told == ["s1"] and outbox.dropped("s1") and not outbox.dropped("s2")
    assert not outbox.put_event("s1", {"method": "x", "params": {}})  # dropped until reopened
    texts = [json.loads(t) for t in iter(outbox.take, None)]
    assert [t["method"] for t in texts] == ["message.delta", "message.delta", GAP_EVENT]
    assert outbox.queued_bytes == 0 and outbox.lane_events("s2") == 0


def test_outbox_total_cap_marks_it_overloaded() -> None:
    outbox = Outbox(OutboxLimits(total_bytes=200))
    assert outbox.put({"id": 1, "result": "x" * 50})
    assert not outbox.put({"id": 2, "result": "x" * 200})
    assert outbox.overloaded and not outbox.put({"id": 3})


def test_outbox_merges_waiting_list_updates_and_resyncs_past_the_cap() -> None:
    outbox = Outbox(OutboxLimits(pending_updates=2))
    assert outbox.put_update("s1", {"running": True})
    assert outbox.put_update("s1", {"running": False, "title": "Plan"})
    assert len(outbox) == 1
    assert outbox.put_update("s2", {"running": True})
    assert not outbox.put_update("s3", {"running": True})
    frames = [json.loads(t) for t in iter(outbox.take, None)]
    assert [f["method"] for f in frames] == [RESYNC_METHOD]
    assert outbox.put_update("s1", {"running": True})


def test_outbox_wait_and_lone_surrogates() -> None:
    now = [100.0]
    outbox = Outbox(monotonic=lambda: now[0])
    assert outbox.oldest_wait() == 0.0
    outbox.put({"text": "bad \ud800 byte"})
    now[0] = 112.5
    assert outbox.oldest_wait() == 12.5
    text = outbox.take()
    assert text is not None and "�" in text
    text.encode("utf-8")  # sendable
    assert "\ud800" not in serialize({"t": "\udfff"})


# -- requests, presence, list updates, authorisation ----------------------------------------------


@aio
async def test_an_open_approval_is_offered_again_on_subscribe(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    host = await open_host(
        scope,
        store,
        tmp_path,
        tool_batch(("confirm", {})),
        text_turn("cleaned"),
        tools={"confirm": confirm},
    )
    cli = Conn(host, "c-1")
    sid = (await host.create()).session_id
    cli.subs.adopt(sid, cursor=0)
    receipt = await host.submit(sid, "clean", principal="cli:a", request_id="r1")
    await eventually(lambda: "server_request" in cli.methods())
    first = next(f for f in cli.frames if f.get("method") == "server_request")["params"]
    assert first["replayed"] is False and first["generation"] == 1
    tui = Conn(host, "t-1", "tui")
    found = await tui.subs.subscribe(sid, host.replay_epoch, 0)
    assert found.reoffered == 1
    again = next(f for f in tui.drain() if f.get("method") == "server_request")["params"]
    assert again["replayed"] is True
    assert again["request"]["request_id"] == first["request"]["request_id"]
    request_id = str(again["request"]["request_id"])
    answered = host.respond(sid, request_id, 1, {"choice": "once"})
    assert answered == Responded(request_id, True)
    assert (await host.wait(receipt.admission_id)).outcome == "completed"
    host.event_log.flush()
    for conn in (cli, tui):
        settled = conn.events("request.settled")
        assert [(e["request_id"], e["outcome"]) for e in settled] == [(request_id, "answered")]
    await host.aclose()


@aio
async def test_presence_counts_surfaces_as_viewers_come_and_go(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    host = await open_host(scope, store, tmp_path)
    cli, tui = Conn(host, "c-1"), Conn(host, "t-1", "tui")
    sid = (await host.create()).session_id
    cli.subs.adopt(sid, cursor=0)
    await tui.subs.subscribe(sid)
    host.event_log.flush()
    latest = cli.events(VIEWERS_EVENT)[-1]
    assert latest["viewers"] == [{"surface": "cli", "count": 1}, {"surface": "tui", "count": 1}]
    assert latest["count"] == 2
    assert await tui.subs.unsubscribe(sid) is True
    assert await tui.subs.unsubscribe(sid) is False
    latest = cli.events(VIEWERS_EVENT)[-1]
    assert latest["viewers"] == [{"surface": "cli", "count": 1}] and latest["count"] == 1
    assert await cli.subs.close() == [sid]
    assert host.state(sid).viewers == ()
    await host.aclose()


@aio
async def test_a_list_watcher_sees_running_state_and_titles(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    holder = Holder()
    host = await open_host(
        scope, store, tmp_path, tool_batch(("hold", {})), text_turn("done"), tools={"hold": holder}
    )
    hub = SessionWatch(host)
    lister = Conn(host, "w-1", "web", watch=hub)
    assert lister.subs.watch(True) is True and hub.watchers == 1
    sid = (await host.create()).session_id  # never attached by the lister
    receipt = await host.submit(sid, "go", principal="cli:a", request_id="r1")
    await eventually(lambda: holder.entered)
    host.event_log.flush()
    updates = [f["params"] for f in lister.drain() if f.get("method") == UPDATED_EVENT]
    assert updates and updates[-1]["session_id"] == sid and updates[-1]["running"] is True
    host.event_log.append(sid, {"type": "session.title", "title": "Plan the trip"})
    holder.gate.set()
    await host.wait(receipt.admission_id)
    host.event_log.flush()
    merged = [f["params"] for f in lister.drain()[len(lister.frames) - 1 :]]
    last = merged[-1]
    assert last["type"] == UPDATED_EVENT and last["running"] is False
    assert last["title"] == "Plan the trip" and isinstance(last["last_activity"], float)
    hub.report(sid, archived=True)
    assert lister.drain()[-1]["params"]["archived"] is True
    assert lister.subs.watch(False) is False and hub.watchers == 0
    hub.report(sid, pinned=True)
    assert lister.outbox.take() is None
    await host.aclose()


@aio
async def test_a_connection_only_reaches_sessions_it_attached(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    host = await open_host(scope, store, tmp_path)
    owner, other = Conn(host, "c-own"), Conn(host, "c-other")
    sid = (await host.create()).session_id
    owner.subs.adopt(sid, cursor=0)
    assert owner.subs.require(sid) == sid
    for call in (
        lambda: other.subs.require(sid),
        lambda: other.subs.since(sid, host.replay_epoch, 0),
    ):
        with pytest.raises(ProtocolError) as refused:
            call()
        assert refused.value.error is SESSION_NOT_FOUND
    assert await other.subs.unsubscribe(sid) is False
    assert other.subs.refuse("srq-000000000000", -32601) is False
    with pytest.raises(ProtocolError) as unknown:
        await other.subs.subscribe("no-such-session")
    assert unknown.value.error is SESSION_NOT_FOUND
    await host.aclose()


@aio
async def test_a_connection_watches_at_most_its_cap_of_sessions(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    host = await open_host(scope, store, tmp_path)
    conn = Conn(host, "c-1")
    conn.subs.max_sessions = 2
    ids = [(await host.create()).session_id for _ in range(3)]
    await conn.subs.subscribe(ids[0])
    await conn.subs.subscribe(ids[1])
    await conn.subs.subscribe(ids[1])  # again: still two
    with pytest.raises(ProtocolError) as full:
        await conn.subs.subscribe(ids[2])
    assert full.value.error is INVALID_PARAMS
    await host.aclose()


def test_capabilities_from_features() -> None:
    assert capabilities_from(["server_requests"]) == {"approval", "clarify", "sudo", "secret"}
    assert capabilities_from(["server_requests.sudo", "server_requests.nope"]) == {"sudo"}
    assert capabilities_from(["request.decline", "other"]) == {"request.decline"}
