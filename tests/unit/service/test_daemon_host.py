"""P17-08: the daemon session host (viewers, detach, fan-out, ACP lifetime, busy modes, wiring)."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from indus.core.persist import StateStore
from indus.core.turn_state import TurnState
from indus.foundation.scope import Scope
from indus.protocol.errors import SESSION_NOT_FOUND
from indus.protocol.methods import ProtocolError, SessionCreateParams
from indus.protocol.requests_port import RequestNotAnswered
from indus.protocol.server_requests import ApprovalAnswer
from indus.service.admissions import CANCELLED, TurnTicket
from indus.service.creation_policy import CreationPolicy, PolicyConflict, PolicyRefused
from indus.service.daemon_host import ACP_END_REASON, DaemonSessionHost
from indus.service.event_log import Replay, ReplayGap
from indus.service.fences import (
    ControlApplied,
    ControlStatus,
    RequestNotOpen,
    Responded,
    StaleGeneration,
    StopOutcome,
)
from indus.service.request_redelivery import Offer
from indus.service.session_service import SessionService
from indus.service.turn_liveness import ActivityMeter
from indus.service.viewers import Viewer, busy_default
from indus.state import registry, sessions
from indus.state import runtime_ledger as ledger
from indus.state.registry import StoreHandle
from indus.state.runtime_ledger import LedgerRefusal
from indus.tools.calls import ToolCall, ToolResult
from tests.fakes.agent import eventually, fake_options
from tests.fakes.model import FakeModelServer, Turn, fake_route, script_of, text_turn, tool_batch
from tests.unit.backends._aio import aio

CWD = os.path.abspath(os.sep + os.path.join("work", "repo"))


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


async def confirm(call: ToolCall, state: TurnState) -> ToolResult:
    broker = state.control.broker
    assert broker is not None
    try:
        answer = await broker.ask(
            "approval", {"command": "rm -r build", "description": "clean up"}, turn_id=state.turn_id
        )
    except RequestNotAnswered as exc:
        return ToolResult(f"not approved: {exc}", is_error=True)
    assert isinstance(answer, ApprovalAnswer)
    return ToolResult(f"approved: {answer.choice.value}")


class Watcher:
    """A recording viewer."""

    def __init__(self, cid: str, surface: str = "cli", kinds: tuple[str, ...] = ("approval",)):
        self.events: list[dict[str, Any]] = []
        self.offers: list[Offer] = []
        self.viewer = Viewer.of(
            cid, surface, kinds, send=self.events.append, offer=self.offers.append
        )

    def types(self) -> list[str]:
        return [str(e["type"]) for e in self.events]


async def inline[T](fn: Callable[[], T]) -> T:
    return fn()


async def open_host(
    scope: Scope,
    store: StoreHandle,
    tmp_path: Path,
    *turns: Turn,
    tools: Mapping[str, Any] | None = None,
) -> DaemonSessionHost:
    server = FakeModelServer(script_of(*turns), clock=scope.clock)
    options = fake_options(server, session_id=None, inline=dict(tools or {}))
    service = SessionService(
        scope,
        fake_route(server),
        toolsets=[],
        options=options,
        store=StateStore(store),
        lock_dir=tmp_path / "locks",
    )
    return await DaemonSessionHost.open(service, store, instance_id="daemon-1", offload=inline)


def contiguous(events: list[dict[str, Any]]) -> bool:
    seqs = [int(e["seq"]) for e in events]
    return seqs == list(range(seqs[0], seqs[0] + len(seqs))) if seqs else True


# -- detach and fan-out ---------------------------------------------------------------------------


@aio
async def test_detach_mid_turn_completes_and_persists(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    holder = Holder()
    host = await open_host(
        scope,
        store,
        tmp_path,
        tool_batch(("hold", {})),
        text_turn("all done"),
        tools={"hold": holder},
    )
    cli = Watcher("c1")
    sid = (await host.create(viewer=cli.viewer)).session_id
    receipt = await host.submit(
        sid, "do it", principal="cli:me", request_id="r1", connection_id="c1"
    )
    await eventually(lambda: holder.entered)
    assert await host.detach(sid, "c1") is True
    seen = len(cli.events)
    holder.gate.set()
    done = await host.wait(receipt.admission_id)
    assert done.state == "terminal" and done.outcome == "completed"
    row = ledger.get(store, receipt.admission_id)
    assert row is not None and row.final_text == "all done"
    stored = await StateStore(store).load(sid)
    assert [r.role for r in stored][0] == "user" and stored[-1].content == "all done"
    assert len(cli.events) == seen  # nothing reaches a detached viewer
    replay = host.events_since(sid, host.replay_epoch, 0)
    assert isinstance(replay, Replay) and "turn.done" in [e["type"] for e in replay.events]
    assert sid in host.hosted()  # a CLI session outlives its viewers
    await host.aclose()


@aio
async def test_two_viewers_receive_every_event_in_order(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    host = await open_host(scope, store, tmp_path, text_turn("Hel", "lo", " there"))
    cli, tui, stranger = Watcher("c1"), Watcher("c2", "tui"), Watcher("c3")
    sid = (await host.create(viewer=cli.viewer)).session_id
    attached = host.attach(sid, tui.viewer)
    assert attached.replay_epoch == host.replay_epoch and attached.key == sid
    other = (await host.create(viewer=stranger.viewer)).session_id
    assert other != sid
    receipt = await host.submit(sid, "hi", principal="cli:me", request_id="r1")
    await host.wait(receipt.admission_id)
    await eventually(lambda: "turn.done" in tui.types())
    assert cli.events == tui.events and contiguous(cli.events)
    assert all(e["replay_epoch"] == host.replay_epoch for e in cli.events)
    assert "".join(str(e["text"]) for e in cli.events if e["type"] == "message.delta") == (
        "Hello there"
    )
    assert stranger.events == []
    assert host.state(sid).viewers == ("c1", "c2")
    await host.aclose()


@aio
async def test_a_failing_viewer_does_not_stop_the_others(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    def broken(event: dict[str, Any]) -> None:
        raise RuntimeError("socket gone")

    host = await open_host(scope, store, tmp_path, text_turn("ok"))
    good = Watcher("c2")
    sid = (await host.create(viewer=Viewer.of("c1", "web", send=broken))).session_id
    host.attach(sid, good.viewer)
    await host.wait((await host.submit(sid, "hi", principal="p", request_id="r1")).admission_id)
    await eventually(lambda: "turn.done" in good.types())
    await host.aclose()


@aio
async def test_reattach_same_connection_is_one_viewer(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    host = await open_host(scope, store, tmp_path, text_turn("ok"))
    first, again = Watcher("c1"), Watcher("c1")
    sid = (await host.create(viewer=first.viewer)).session_id
    host.attach(sid, again.viewer)
    assert host.state(sid).viewers == ("c1",)
    await host.wait((await host.submit(sid, "hi", principal="p", request_id="r1")).admission_id)
    await eventually(lambda: "turn.done" in again.types())
    assert first.events == []  # replaced by the second attach
    assert await host.detach_connection("c1") == [sid]
    assert host.state(sid).viewers == ()
    await host.aclose()


# -- ACP lifetime ---------------------------------------------------------------------------------


@aio
async def test_acp_session_ends_after_last_detach_only_when_idle(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    holder = Holder()
    host = await open_host(
        scope, store, tmp_path, tool_batch(("hold", {})), text_turn("done"), tools={"hold": holder}
    )
    idle_acp = (await host.create(source="acp", viewer=Watcher("a0", "acp").viewer)).session_id
    await host.detach(idle_acp, "a0")
    assert idle_acp not in host.hosted() and host.key_of(idle_acp) is None

    busy_acp = (await host.create(source="acp", viewer=Watcher("a1", "acp").viewer)).session_id
    receipt = await host.submit(busy_acp, "work", principal="acp:x", request_id="r1")
    await eventually(lambda: holder.entered)
    await host.detach(busy_acp, "a1")
    assert busy_acp in host.hosted() and host.state(busy_acp).close_pending
    holder.gate.set()
    done = await host.wait(receipt.admission_id)
    assert done.outcome == "completed"
    await eventually(lambda: busy_acp not in host.hosted())
    row = sessions.get(store, busy_acp)
    assert row is not None and row["end_reason"] == ACP_END_REASON and row["ended_at"]
    await host.aclose()


@aio
async def test_reattach_cancels_pending_acp_close(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    holder = Holder()
    host = await open_host(
        scope, store, tmp_path, tool_batch(("hold", {})), text_turn("done"), tools={"hold": holder}
    )
    sid = (await host.create(source="acp", viewer=Watcher("a1", "acp").viewer)).session_id
    receipt = await host.submit(sid, "work", principal="acp:x", request_id="r1")
    await eventually(lambda: holder.entered)
    await host.detach(sid, "a1")
    assert host.state(sid).close_pending
    host.attach(sid, Watcher("a2", "acp").viewer)
    assert not host.state(sid).close_pending
    holder.gate.set()
    await host.wait(receipt.admission_id)
    await eventually(lambda: not host._tasks)  # pyright: ignore[reportPrivateUsage]
    assert sid in host.hosted()
    await host.aclose()
    assert host.hosted() == []  # the daemon stop ends every session


# -- busy modes -----------------------------------------------------------------------------------


def test_busy_default_per_surface() -> None:
    assert busy_default("tui") == "queue"
    for surface in ("cli", "gui", "acp", "web", "messaging"):
        assert busy_default(surface) == "interrupt"


@aio
async def test_tui_input_queues_and_cli_input_interrupts(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    holder = Holder()
    host = await open_host(
        scope,
        store,
        tmp_path,
        tool_batch(("hold", {})),
        text_turn("second"),
        text_turn("third"),
        tools={"hold": holder},
    )
    cli, tui = Watcher("c1"), Watcher("t1", "tui")
    sid = (await host.create(viewer=cli.viewer)).session_id
    host.attach(sid, tui.viewer)
    first = await host.submit(sid, "one", principal="p", request_id="r1", connection_id="c1")
    await eventually(lambda: holder.entered)
    second = await host.submit(sid, "two", principal="p", request_id="r2", connection_id="t1")
    assert second.state == "queued" and second.position == 1
    assert (await host.receipt(first.admission_id)).state == "started"  # the TUI queued
    third = await host.submit(sid, "three", principal="p", request_id="r3", connection_id="c1")
    assert third.state == "queued"
    done = await host.wait(first.admission_id)
    assert done.outcome == "interrupted"  # the CLI interrupted it
    assert (await host.wait(second.admission_id)).outcome == "completed"
    assert (await host.wait(third.admission_id)).outcome == "completed"
    texts = [
        r.final_text for r in (ledger.get(store, a.admission_id) for a in (second, third)) if r
    ]
    assert texts == ["second", "third"]
    await host.aclose()


# -- what an attached client sees at a turn's end (P17-26) ---------------------------------------


@aio
async def test_a_turn_ends_with_its_final_message_and_the_usage(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    """After ``turn.done``: ``message.complete`` (text and reason) then ``session.usage``, live and
    in a replay, as the TUI's own bridge sends them."""
    host = await open_host(scope, store, tmp_path, text_turn("Hel", "lo"))
    tui = Watcher("t1", "tui")
    sid = (await host.create(viewer=tui.viewer)).session_id
    first = await host.submit(sid, "hi", principal="p", request_id="r1", connection_id="t1")
    await host.wait(first.admission_id)
    await eventually(lambda: "session.usage" in tui.types())
    ends = [t for t in tui.types() if t in ("turn.done", "message.complete", "session.usage")]
    assert ends == ["turn.done", "message.complete", "session.usage"]
    (final,) = [e for e in tui.events if e["type"] == "message.complete"]
    (done,) = [e for e in tui.events if e["type"] == "turn.done"]
    assert (final["text"], final["reason"], final["turn_id"]) == (
        "Hello",
        "completed",
        done["turn_id"],
    )
    (usage,) = [e for e in tui.events if e["type"] == "session.usage"]
    assert usage["usage"]["api_calls"] == 1 and "context_window" in usage["usage"]
    assert contiguous(tui.events)
    replay = host.events_since(sid, host.replay_epoch, 0)
    assert isinstance(replay, Replay)
    turn_end = [e["type"] for e in replay.events if e["type"] != "admission.state"]
    assert turn_end[-2:] == ["message.complete", "session.usage"]
    await host.aclose()


@aio
async def test_an_interrupted_turn_ends_with_the_interrupt_reason(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    holder = Holder()
    host = await open_host(scope, store, tmp_path, tool_batch(("hold", {})), tools={"hold": holder})
    cli = Watcher("c1")
    sid = (await host.create(viewer=cli.viewer)).session_id
    receipt = await host.submit(sid, "go", principal="p", request_id="r1", connection_id="c1")
    await eventually(lambda: holder.entered)
    running = host.state(sid).running
    assert running is not None
    host.interrupt(sid, running)
    await host.wait(receipt.admission_id)
    await eventually(lambda: "message.complete" in cli.types())
    (done,) = [e for e in cli.events if e["type"] == "turn.done"]
    (final,) = [e for e in cli.events if e["type"] == "message.complete"]
    assert final["reason"] == done["reason"] != "completed"
    await host.aclose()


@aio
async def test_a_surface_session_is_stored_under_that_surface(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    """A session a TUI creates through the daemon is a ``tui`` session (its listings and ``-c``
    find it); the gateway's own sessions keep the service's source."""
    host = await open_host(scope, store, tmp_path)
    tui = (await host.create(SessionCreateParams(title="mine"), source="tui")).session_id
    chat = (await host.create(SessionCreateParams(title="chat"), source="messaging")).session_id
    tui_row, chat_row = sessions.get(store, tui), sessions.get(store, chat)
    assert tui_row is not None and tui_row["source"] == "tui"
    assert chat_row is not None and chat_row["source"] not in ("tui", "messaging")
    await host.aclose()


# -- fenced controls ------------------------------------------------------------------------------


@aio
async def test_interrupt_names_its_generation(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    holder = Holder()
    host = await open_host(scope, store, tmp_path, tool_batch(("hold", {})), tools={"hold": holder})
    sid = (await host.create()).session_id
    receipt = await host.submit(sid, "one", principal="p", request_id="r1")
    await eventually(lambda: holder.entered)
    running = await host.receipt(receipt.admission_id)
    assert running.generation == 1
    stale = host.interrupt(sid, 0)
    assert isinstance(stale, StaleGeneration) and stale.current == 1
    assert holder.entered == 1 and (await host.receipt(receipt.admission_id)).state == "started"
    applied = host.interrupt(sid, 1)
    assert isinstance(applied, ControlApplied) and applied.status is ControlStatus.DELIVERED
    assert (await host.wait(receipt.admission_id)).outcome == "interrupted"
    await host.aclose()


@aio
async def test_stop_is_hard_and_stops_subagents(
    scope: Scope, store: StoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    holder = Holder()
    host = await open_host(scope, store, tmp_path, tool_batch(("hold", {})), tools={"hold": holder})
    stopped: list[str] = []

    async def stop_subagents(session_id: str) -> int:
        stopped.append(session_id)
        return 0

    monkeypatch.setattr(host.service, "stop_subagents", stop_subagents)
    sid = (await host.create()).session_id
    receipt = await host.submit(sid, "one", principal="p", request_id="r1")
    await eventually(lambda: holder.entered)
    result = await host.stop(sid, receipt.admission_id)
    assert result.outcome is StopOutcome.INTERRUPTED
    assert (await host.wait(receipt.admission_id)).outcome == "cancelled"
    await eventually(lambda: stopped == [sid])
    await host.aclose()


@aio
async def test_an_approval_reaches_both_viewers_and_settles_once(
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
    cli, tui, blind = Watcher("c1"), Watcher("t1", "tui"), Watcher("w1", "web", kinds=())
    sid = (await host.create(viewer=cli.viewer)).session_id
    host.attach(sid, tui.viewer)
    host.attach(sid, blind.viewer)
    receipt = await host.submit(sid, "clean", principal="p", request_id="r1")
    await eventually(lambda: cli.offers and tui.offers)
    offer = cli.offers[0]
    assert tui.offers[0].request.request_id == offer.request.request_id and not offer.replayed
    assert offer.generation == 1 and blind.offers == []
    request_id = offer.request.request_id
    assert host.state(sid).open_requests == (request_id,)
    stale = host.respond(sid, request_id, 0, {"choice": "once"})
    assert isinstance(stale, StaleGeneration)
    replay = host.events_since(sid, host.replay_epoch, 0, connection_id="t1")
    assert isinstance(replay, Replay) and tui.offers[-1].replayed
    answered = host.respond(sid, request_id, 1, {"choice": "once"})
    assert answered == Responded(request_id, True)
    assert isinstance(host.respond(sid, request_id, 1, {"choice": "once"}), RequestNotOpen)
    assert (await host.wait(receipt.admission_id)).outcome == "completed"
    await eventually(lambda: "turn.done" in tui.types())
    for watcher in (cli, tui, blind):
        settled = [e for e in watcher.events if e["type"] == "request.settled"]
        assert [(e["request_id"], e["outcome"]) for e in settled] == [(request_id, "answered")]
    await host.aclose()


@aio
async def test_events_since_reports_a_gap_for_another_epoch(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    host = await open_host(scope, store, tmp_path, text_turn("ok"))
    sid = (await host.create()).session_id
    await host.wait((await host.submit(sid, "hi", principal="p", request_id="r1")).admission_id)
    gap = host.events_since(sid, "old-epoch", 0)
    assert isinstance(gap, ReplayGap) and gap.reason == "epoch_changed"
    await host.aclose()


@aio
async def test_a_connection_replays_only_sessions_it_attached(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    host = await open_host(scope, store, tmp_path, text_turn("ok"))
    mine, other = Watcher("c1"), Watcher("c2")
    sid = (await host.create(viewer=mine.viewer)).session_id
    theirs = (await host.create(viewer=other.viewer)).session_id
    await host.wait((await host.submit(sid, "hi", principal="p", request_id="r1")).admission_id)
    assert isinstance(host.events_since(sid, host.replay_epoch, 0, connection_id="c1"), Replay)
    for session, connection in ((sid, "c2"), (theirs, "c1"), (sid, "nobody")):
        with pytest.raises(ProtocolError) as caught:
            host.events_since(session, host.replay_epoch, 0, connection_id=connection)
        assert caught.value.error is SESSION_NOT_FOUND
    await host.aclose()


# -- the turn runner ------------------------------------------------------------------------------


def _ticket(store: StoreHandle, session_id: str, text: str) -> TurnTicket:
    now = time.time()
    row = ledger.AdmissionRow(
        admission_id="adm-direct",
        seq=1,
        principal_id="p",
        session_id=session_id,
        request_id="r-direct",
        payload={"text": text},
        digest="d",
        state="started",
        outcome=None,
        owner_epoch=1,
        generation=7,
        replaces=None,
        final_text=None,
        detail=None,
        created_at=now,
        updated_at=now,
    )
    return TurnTicket(row, ActivityMeter(store.scope.clock))


@aio
async def test_a_stop_before_the_hooks_are_bound_never_starts_the_turn(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    host = await open_host(scope, store, tmp_path)  # an empty script: any model call fails
    sid = (await host.create()).session_id
    ticket = _ticket(store, sid, "never")
    ticket.interrupt(CANCELLED)
    outcome = await host._run_turn(ticket)  # pyright: ignore[reportPrivateUsage]
    assert outcome.outcome == "cancelled"
    assert host.service.agent(sid).generation == 0
    await host.aclose()


@aio
async def test_a_lease_won_after_waiting_reloads_the_session(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    host = await open_host(scope, store, tmp_path, text_turn("fresh"))
    sid = (await host.create()).session_id
    before = host.service.agent(sid)
    ticket = _ticket(store, sid, "after the wait")
    ticket.lease_waited = True
    outcome = await host._run_turn(ticket)  # pyright: ignore[reportPrivateUsage]
    assert outcome.outcome == "completed" and outcome.final_text == "fresh"
    after = host.service.agent(sid)
    assert after is not before and before.closed and after.generation == 7
    await host.aclose()


@aio
async def test_host_injections_and_steer_leftovers_enter_the_ledger(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    host = await open_host(scope, store, tmp_path, text_turn("ticked"))
    sid = (await host.create()).session_id
    assert await host.service.inject(sid, "loop tick", origin="loop tick") is True
    rows = [r for r in ledger.pending(store, sid)] or []
    assert rows and rows[0].principal_id == "internal:loop-tick"
    receipt = await host.wait(rows[0].admission_id)
    assert receipt.outcome == "completed"
    await host.aclose()


# -- compaction and policy ------------------------------------------------------------------------


@aio
async def test_the_key_and_book_follow_a_compaction(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    host = await open_host(scope, store, tmp_path)
    sid = (await host.create()).session_id
    agent = host.service.agent(sid)
    agent._continued(sid, "child-1")  # pyright: ignore[reportPrivateUsage]
    assert host.key_of("child-1") == sid and host.key_of(sid) == sid
    state = host.state("child-1")
    assert state.key == sid and state.session_id == "child-1"
    book = host._live[sid].book  # pyright: ignore[reportPrivateUsage]
    assert book.session_id == "child-1"
    await host.aclose()


@aio
async def test_create_freezes_the_policy_and_resume_checks_it(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    host = await open_host(scope, store, tmp_path)
    policy = CreationPolicy.from_launch({"cwd": CWD, "model": "m1", "provider": "p1"})
    sid = (await host.create(policy=policy)).session_id
    row = sessions.get(store, sid)
    assert row is not None and "launch" in sessions.parse_model_config(row["model_config"])
    same = CreationPolicy.from_launch({"model": "m1"})
    assert (await host.resume(sid, policy=same)).session_id == sid
    with pytest.raises(PolicyConflict) as info:
        await host.resume(sid, policy=CreationPolicy.from_launch({"model": "m2"}))
    assert info.value.field == "model"
    with pytest.raises(PolicyRefused):
        await host.create(policy=CreationPolicy.from_launch({"safe_mode": True, "model": "m1"}))
    await host.aclose()


# -- the in-process forms of the shared interface -------------------------------------------------


@aio
async def test_in_process_service_answers_replay_and_receipt_trivially(
    scope: Scope, tmp_path: Path
) -> None:
    server = FakeModelServer(script_of(text_turn("ok")), clock=scope.clock)
    service = SessionService(
        scope,
        fake_route(server),
        toolsets=[],
        options=fake_options(server, session_id=None),
        lock_dir=tmp_path / "locks",
    )
    sid = (await service.create()).session_id
    await service.submit(sid, "hi")
    await service.wait(sid)
    replay = service.events_since(sid, service.replay_epoch, -1)
    assert isinstance(replay, Replay)
    assert [e["seq"] for e in replay.events] == [e.seq for e in service.events(sid)]
    assert replay.last_seq == replay.events[-1]["seq"]
    later = service.events_since(sid, service.replay_epoch, replay.last_seq)
    assert isinstance(later, Replay) and later.events == ()
    gap = service.events_since(sid, "another-epoch", 0)
    assert isinstance(gap, ReplayGap) and gap.reason == "epoch_changed"
    ahead = service.events_since(sid, service.replay_epoch, replay.last_seq + 5)
    assert isinstance(ahead, ReplayGap) and ahead.reason == "ahead"
    with pytest.raises(LedgerRefusal) as info:
        await service.receipt("adm-anything")
    assert info.value.reason == "not_found"
    await service.aclose()


# -- admission.state and unattended inputs (P17-25 wiring, integration) ---------------------------


@aio
async def test_every_move_of_an_input_reaches_the_viewers_as_admission_state(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    from indus.protocol.events import AdmissionStateChanged

    holder = Holder()
    host = await open_host(
        scope,
        store,
        tmp_path,
        tool_batch(("hold", {})),
        text_turn("first done"),
        text_turn("second done"),
        tools={"hold": holder},
    )
    tui = Watcher("t1", "tui")
    sid = (await host.create(viewer=tui.viewer)).session_id
    first = await host.submit(sid, "one", principal="p", request_id="r1", connection_id="t1")
    await eventually(lambda: holder.entered)
    second = await host.submit(sid, "two", principal="p", request_id="r2", connection_id="t1")
    holder.gate.set()
    await host.wait(second.admission_id)
    await eventually(
        lambda: any(
            e.get("admission_id") == second.admission_id and e.get("state") == "terminal"
            for e in tui.events
        )
    )
    moves = [e for e in tui.events if e["type"] == "admission.state"]
    for event in moves:
        # the declared shape, seq included (the wire's replay epoch is the frame's, not the model's)
        AdmissionStateChanged.model_validate(
            {k: v for k, v in event.items() if k != "replay_epoch"}
        )
    seen = [
        (e["admission_id"], e["state"], e.get("generation"), e.get("position"), e.get("outcome"))
        for e in moves
    ]
    a, b = first.admission_id, second.admission_id
    assert seen == [
        (a, "queued", None, 1, None),
        (a, "started", 1, None, None),
        (b, "queued", None, 1, None),
        (a, "terminal", 1, None, "completed"),
        (b, "started", 2, None, None),
        (b, "terminal", 2, None, "completed"),
    ]
    assert contiguous(tui.events)
    await host.aclose()


@aio
async def test_an_unattended_input_auto_approves_only_its_own_turn(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    from indus.safety.approvals.decision import APPROVALS, session_key

    seen: list[bool] = []
    session: list[str] = []

    async def probe(call: ToolCall, state: TurnState) -> ToolResult:
        seen.append(APPROVALS.auto_approve(session_key(scope, session[0])))
        return ToolResult("looked")

    host = await open_host(
        scope,
        store,
        tmp_path,
        tool_batch(("probe", {})),
        text_turn("ran unattended"),
        tool_batch(("probe", {})),
        text_turn("ran attended"),
        tool_batch(("probe", {})),
        text_turn("ran with yolo"),
        tools={"probe": probe},
    )
    cli = Watcher("c1")
    sid = (await host.create(viewer=cli.viewer)).session_id
    session.append(sid)
    key = session_key(scope, sid)
    try:
        lone = await host.submit(sid, "z run", principal="p", request_id="r1", unattended=True)
        assert (await host.wait(lone.admission_id)).outcome == "completed"
        assert seen == [True] and not APPROVALS.auto_approve(key)  # for that turn only
        row = ledger.get(store, lone.admission_id)
        assert row is not None and row.payload == {"text": "z run", "unattended": True}
        plain = await host.submit(sid, "chat", principal="p", request_id="r2")
        await host.wait(plain.admission_id)
        assert seen == [True, False]
        APPROVALS.set_auto_approve(key, True)  # a person's /yolo is left as it was
        more = await host.submit(sid, "z again", principal="p", request_id="r3", unattended=True)
        await host.wait(more.admission_id)
        assert seen == [True, False, True] and APPROVALS.auto_approve(key)
    finally:
        APPROVALS.set_auto_approve(key, False)
        await host.aclose()


@aio
async def test_a_single_query_input_runs_its_turn_as_single_query_and_asks_nobody(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    """W47 g (D-16.487): an input submitted ``single_query`` (an attached finite ``chat -q``)
    runs its turn with the run context ``--local`` gives it and no answerable kind (clarify,
    sudo and the rest are unsupported, as ``--local`` passes none), for that turn only."""
    from indus.protocol.requests_port import RequestUnsupported
    from indus.service.broker import ServerRequestBroker

    seen: list[tuple[str | None, bool]] = []

    async def probe(call: ToolCall, state: TurnState) -> ToolResult:
        broker = state.control.broker
        assert isinstance(broker, ServerRequestBroker)
        can_ask = "clarify" in broker.answerable_kinds
        seen.append((state.runtime.run_context, can_ask))
        if not can_ask:
            with pytest.raises(RequestUnsupported):
                await broker.ask("clarify", {"questions": [{"id": "q", "text": "which?"}]})
        return ToolResult("looked")

    host = await open_host(
        scope,
        store,
        tmp_path,
        tool_batch(("probe", {})),
        text_turn("ran once"),
        tool_batch(("probe", {})),
        text_turn("ran attended"),
        tools={"probe": probe},
    )
    tui = Watcher("t1", "tui", kinds=("approval", "clarify"))
    sid = (await host.create(viewer=tui.viewer)).session_id
    try:
        lone = await host.submit(sid, "q run", principal="p", request_id="r1", single_query=True)
        assert (await host.wait(lone.admission_id)).outcome == "completed"
        assert seen == [("single_query", False)]
        row = ledger.get(store, lone.admission_id)
        assert row is not None and row.payload == {"text": "q run", "single_query": True}
        assert host.service.agent(sid).runtime.run_context is None  # put back after the turn
        plain = await host.submit(sid, "chat", principal="p", request_id="r2")
        assert (await host.wait(plain.admission_id)).outcome == "completed"
        assert seen[1] == (None, True)  # the next input runs as the session always does
    finally:
        await host.aclose()
