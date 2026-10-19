"""The P10 session features over a real ``SessionService`` (P10-21..26): the host seams (idle,
inject, input probe, turn-end listeners, notify) and each command end to end against the fake
model."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from indus.core import moa
from indus.core.goals import GoalStore
from indus.core.moa_presets import MoaSettings, Preset, Slot
from indus.core.session_scheduler import TurnEnd
from indus.foundation.scope import Scope
from indus.protocol.events import GoalUpdate, HeartbeatFired, LoopUpdate, Notice, NoticeLevel
from indus.service.automation import SessionAutomation
from indus.service.session_service import SessionService
from tests.fakes.agent import eventually, fake_options
from tests.fakes.clock import FakeClock
from tests.fakes.model import FakeModelServer, Turn, fake_route, script_of, text_turn
from tests.unit.core.test_goals import DONE, NOT_DONE, FakeJudge
from tests.unit.core.test_moa import FakeAux
from tests.unit.core.test_side_question import as_aux


def make(
    scope: Scope, clock: FakeClock, tmp_path: Path, *turns: Turn, **values: Any
) -> tuple[SessionService, FakeModelServer, SessionAutomation]:
    server = FakeModelServer(script_of(*turns), clock=clock)
    svc = SessionService(
        scope,
        fake_route(server),
        toolsets=[],
        options=fake_options(server, session_id=None),
        lock_dir=tmp_path / "locks",
    )
    automation = SessionAutomation(svc, **values)
    return svc, server, automation


def user_texts(server: FakeModelServer) -> list[str]:
    out: list[str] = []
    for request in server.model_requests:
        users = [m for m in request.body["messages"] if m["role"] == "user"]
        out.append(str(users[-1]["content"]))
    return out


def test_host_seams(scope: Scope, fake_clock: FakeClock, tmp_path: Path) -> None:
    svc, _server, _auto = make(scope, fake_clock, tmp_path, text_turn("a"), text_turn("b"))
    ended: list[TurnEnd] = []

    async def main() -> None:
        sid = (await svc.create()).session_id
        undo = svc.on_turn_end(ended.append)
        assert svc.is_idle(sid) and svc.generation(sid) == 0
        assert not svc.is_idle("nope")
        queued = {sid}
        svc.set_input_probe(lambda s: s in queued)
        assert not svc.is_idle(sid)
        assert not await svc.inject(sid, "hello", origin="test")
        queued.clear()
        assert await svc.inject(sid, "hello", origin="test", if_generation=0)
        assert not svc.is_idle(sid)
        assert not await svc.inject(sid, "again", origin="test")  # busy
        await svc.wait(sid)
        assert not await svc.inject(sid, "x", origin="test", if_generation=0)  # moved on
        svc.notify(sid, Notice, level=NoticeLevel.INFO, text="hi", key="k")
        assert isinstance(svc.events(sid)[-1], Notice)
        undo()
        assert await svc.inject(sid, "second", origin="test")
        await svc.wait(sid)
        await svc.aclose()

    asyncio.run(main())
    assert len(ended) == 1 and ended[0].completed and ended[0].final_text == "a"
    assert ended[0].generation == 1


def test_goal_runs_until_the_judge_says_done(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    judge = FakeJudge([NOT_DONE, NOT_DONE, DONE])
    svc, server, auto = make(
        scope,
        fake_clock,
        tmp_path,
        text_turn("step one"),
        text_turn("step two"),
        text_turn("all done"),
        judge=judge,
    )

    async def main() -> str:
        auto.start()
        sid = (await svc.create()).session_id
        reply = await auto.command("goal", sid, "make the tests pass --max-turns 5")
        assert reply.lines[0] == "Goal set (5 turns): make the tests pass"
        assert reply.submit == "make the tests pass"
        await svc.submit(sid, reply.submit)
        await eventually(lambda: len(judge.calls) == 3)
        await eventually(lambda: svc.is_idle(sid))
        await auto.stop()
        await svc.aclose()
        return sid

    sid = asyncio.run(main())
    assert len(server.model_requests) == 3
    texts = user_texts(server)
    assert texts[0] == "make the tests pass"
    assert texts[1].startswith("[Standing goal — keep going]\nGoal: make the tests pass")
    state = asyncio.run(GoalStore(auto.meta).load(sid))
    assert state is not None and state.status == "done" and state.turns_used == 3


def test_goal_events_reach_the_session_stream(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    svc, _server, auto = make(
        scope, fake_clock, tmp_path, text_turn("done"), judge=FakeJudge([DONE])
    )

    async def main() -> list[Any]:
        auto.start()
        sid = (await svc.create()).session_id
        reply = await auto.command("goal", sid, "x")
        await svc.submit(sid, reply.submit or "")
        await eventually(lambda: any(isinstance(e, Notice) for e in svc.events(sid)))
        events = svc.events(sid)
        await auto.stop()
        await svc.aclose()
        return events

    events = asyncio.run(main())
    updates = [e for e in events if isinstance(e, GoalUpdate)]
    assert [u.status for u in updates] == ["active", "done"]


def test_heartbeat_fires_through_the_scheduler(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    svc, server, auto = make(scope, fake_clock, tmp_path, text_turn("nothing new"))

    async def main() -> list[Any]:
        sid = (await svc.create()).session_id
        reply = await auto.command("heartbeat", sid, "every 1m look at the inbox")
        assert reply.lines[0].startswith("Heartbeat h1 set: every 1m")
        fake_clock.advance(60)
        assert await auto.scheduler.poll_once() == 1
        result = await svc.wait(sid)
        assert result.final_text == "nothing new"
        events = svc.events(sid)
        await svc.aclose()
        return events

    events = asyncio.run(main())
    assert user_texts(server)[0].startswith("[Heartbeat h1")
    assert [e.heartbeat_id for e in events if isinstance(e, HeartbeatFired)] == ["h1"]


def test_loop_first_run_starts_at_once(scope: Scope, fake_clock: FakeClock, tmp_path: Path) -> None:
    svc, server, auto = make(
        scope, fake_clock, tmp_path, text_turn("checked.\nLOOP_COMPLETE"), judge=FakeJudge()
    )

    async def main() -> list[Any]:
        auto.start()
        sid = (await svc.create()).session_id
        reply = await auto.command("loop", sid, "5m check the deploy")
        assert reply.lines[0] == "Loop l1 set (every 5m): check the deploy"
        await svc.wait(sid)
        await eventually(lambda: any(isinstance(e, Notice) for e in svc.events(sid)))
        events = svc.events(sid)
        await auto.stop()
        await svc.aclose()
        return events

    events = asyncio.run(main())
    assert len(server.model_requests) == 1
    assert [e.status for e in events if isinstance(e, LoopUpdate)] == ["active", "active", "done"]


def test_btw_answers_from_the_live_agent(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    svc, server, auto = make(
        scope, fake_clock, tmp_path, text_turn("we chose blue"), text_turn("Blue.")
    )

    async def main() -> Any:
        sid = (await svc.create()).session_id
        await svc.submit(sid, "pick a colour")
        await svc.wait(sid)
        empty = await auto.command("btw", sid, "  ")
        reply = await auto.command("btw", sid, "what did we pick?")
        assert svc.is_idle(sid) and svc.generation(sid) == 1
        await svc.aclose()
        return empty, reply

    empty, reply = asyncio.run(main())
    assert empty.lines == ("Ask something: /btw <question>",)
    assert reply.lines == ("Blue.",) and reply.submit is None
    assert len(server.model_requests) == 2


def test_moa_command_arms_the_next_turn(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    preset = Preset("duo", (Slot("alpha", "a-1"), Slot("beta", "b-1")))
    settings = MoaSettings(presets={"duo": preset}, default_preset="duo")
    aux = FakeAux(strict=False)
    svc, server, auto = make(
        scope,
        fake_clock,
        tmp_path,
        text_turn("with advice"),
        text_turn("plain"),
        aux=as_aux(aux),
        settings=settings,
    )

    async def main() -> list[Any]:
        sid = (await svc.create()).session_id
        status = await auto.command("moa", sid, "")
        reply = await auto.command("moa", sid, "duo plan the release")
        armed = await auto.command("moa", sid, "status")
        await svc.submit(sid, reply.submit or "")
        await svc.wait(sid)
        await svc.submit(sid, "and now?")
        await svc.wait(sid)
        on = await auto.command("moa", sid, "on")
        off = await auto.command("moa", sid, "off")
        missing = await auto.command("moa", sid, "on nope")
        await svc.aclose()
        return [status, reply, armed, on, off, missing]

    moa.ARMED.entries.clear()
    status, reply, armed, on, off, missing = asyncio.run(main())
    assert status.lines[0] == "Mixture of agents: off. Presets: duo."
    assert reply.submit == "plan the release"
    assert reply.lines == ("Mixture of agents for this turn (duo): alpha:a-1, beta:b-1",)
    assert armed.lines[0].startswith("Mixture of agents: armed for the next turn (preset duo)")
    first, second = user_texts(server)
    assert first.startswith("plan the release\n\n[Advice from 2 other model(s)")
    assert second == "and now?"
    assert aux.tasks == ["moa", "moa"]
    assert on.lines[0].startswith("Mixture of agents is on (preset duo")
    assert off.lines == ("Mixture of agents is off.",)
    assert missing.lines == ("No MoA preset is called 'nope'. indus moa list shows them.",)
    moa.ARMED.entries.clear()


def test_moa_without_presets_says_how_to_make_one(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    svc, _server, auto = make(scope, fake_clock, tmp_path, settings=MoaSettings())

    async def main() -> Any:
        sid = (await svc.create()).session_id
        reply = await auto.command("moa", sid, "hello there")
        await svc.aclose()
        return reply

    reply = asyncio.run(main())
    assert reply.lines == ("No MoA preset yet. Make one with: indus moa configure <name>",)
    assert reply.submit is None


def test_a_rotating_compaction_carries_the_features(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    preset = Preset("duo", (Slot("alpha", "a-1"),))
    svc, _server, auto = make(
        scope,
        fake_clock,
        tmp_path,
        settings=MoaSettings(presets={"duo": preset}),
        aux=as_aux(FakeAux()),
    )

    async def main() -> list[Any]:
        sid = (await svc.create()).session_id
        await auto.command("goal", sid, "carry me")
        await auto.command("loop", sid, "5m poll")  # not idle-fired: no turn script needed
        await auto.command("heartbeat", sid, "every 10m look")
        await auto.command("moa", sid, "on")
        await auto.move(sid, "child")
        out = [
            await auto.goals.goal_command("child", "status"),
            await auto.loops.command("child", "list"),
            await auto.heartbeats.command("child", "list"),
            await auto.goals.goal_command(sid, "status"),
            moa.ARMED.peek(scope, "child") is not None,
        ]
        await svc.aclose()
        return out

    moa.ARMED.entries.clear()
    goal, loops_list, beats, old_goal, armed = asyncio.run(main())
    moa.ARMED.entries.clear()
    assert goal.lines == ("Goal active (0/20 turns): carry me",)
    assert loops_list.lines[0].startswith("l1 [active] every 5m")
    assert beats.lines[0].startswith("h1 [active] every 10m")
    assert old_goal.lines[0] == "No goal is set."
    assert armed
