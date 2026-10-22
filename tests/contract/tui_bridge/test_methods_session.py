"""Session and prompt methods over the bridge dispatcher (P12-29): fake model, real P02 store."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path

from indus.core.transcript import TranscriptRow
from indus.foundation.scope import Scope
from indus.protocol.errors import IDEMPOTENCY_CONFLICT, SESSION_BUSY, SESSION_NOT_FOUND
from indus.state import sessions
from indus.tui_bridge.methods_session import display_messages, flatten_content
from tests.contract.tui_bridge._harness import open_rig
from tests.fakes.agent import eventually
from tests.fakes.clock import FakeClock
from tests.fakes.model import Finish, Stall, Text, Turn, text_turn


def stalled(text: str, seconds: float = 100.0) -> Turn:
    return Turn((Text((text,)), Stall(seconds), Finish("stop")))


async def release(clock: FakeClock, done: Callable[[], bool], step: float = 1.0) -> None:
    """Move the fake clock in small steps until ``done()`` holds."""

    def moved() -> bool:
        if done():
            return True
        clock.advance(step)
        return False

    await eventually(moved)


def test_create_twice_with_one_key_gives_one_session(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        async with open_rig(scope, fake_clock, tmp_path) as rig:
            first = await rig.ok("session.create", idempotency_key="k-1", title="plans")
            again = await rig.ok("session.create", idempotency_key="k-1", title="plans")
            assert first["created"] is True and again["created"] is False
            assert again["session_id"] == first["session_id"]
            clash = await rig.err("session.create", idempotency_key="k-1", title="other")
            assert clash["code"] == IDEMPOTENCY_CONFLICT.code
            info = rig.recorder.of("session.info")
            assert info and info[0]["session_id"] == first["session_id"]
            assert info[0]["title"] == "plans" and info[0]["running"] is False
            rig.recorder.check_all()

    asyncio.run(main())


def test_submit_streams_events_with_growing_seq(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        async with open_rig(scope, fake_clock, tmp_path, text_turn("Hel", "lo")) as rig:
            sid = (await rig.ok("session.create"))["session_id"]
            ticket = await rig.ok("prompt.submit", session_id=sid, text="hi")
            assert ticket["status"] == "streaming" and ticket["generation"] == 1
            await rig.turn_done()
            methods = rig.recorder.methods()
            for name in ("turn.started", "message.delta", "turn.done", "message.complete"):
                assert name in methods, name
            assert methods.index("turn.done") < methods.index("message.complete")
            done = rig.recorder.of("message.complete")[0]
            assert done["text"] == "Hello" and done["reason"] == "completed"
            await eventually(lambda: bool(rig.recorder.of("session.usage")))
            seqs = [p["seq"] for _m, p in rig.recorder.frames if p.get("session_id") == sid]
            assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))
            rig.recorder.check_all()

    asyncio.run(main())


def test_resume_flattens_images_and_finds_titles(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        async with open_rig(scope, fake_clock, tmp_path, text_turn("seen")) as rig:
            sid = (await rig.ok("session.create", title="pics"))["session_id"]
            await rig.ok("prompt.submit", session_id=sid, text="first")
            await rig.turn_done()
            await rig.idle(sid)
            assert (await rig.ok("session.close", session_id=sid))["closed"] is True
            service = await rig.service()
            parts = [
                {"type": "text", "text": "look at"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ]
            await service.store.append(sid, TranscriptRow(role="user", content=parts))
            resumed = await rig.ok("session.resume", session_id="pics")
            assert resumed["session_id"] == sid and resumed["title"] == "pics"
            texts = [(m["role"], m["text"]) for m in resumed["messages"]]
            assert ("user", "first") in texts and ("assistant", "seen") in texts
            assert texts[-1] == ("user", "look at\n[image]")
            assert all(isinstance(m.get("row_id"), int) for m in resumed["messages"])
            missing = await rig.err("session.resume", session_id="no such title")
            assert missing["code"] == SESSION_NOT_FOUND.code
            history = await rig.ok("session.history", session_id=sid)
            assert history["messages"] == resumed["messages"]

    asyncio.run(main())


def test_display_projection_drops_scaffold_and_names_tools() -> None:
    rows = [
        TranscriptRow(role="system", content="prompt"),
        TranscriptRow(
            role="assistant",
            content=None,
            tool_calls=[{"id": "c1", "function": {"name": "read_file", "arguments": "{}"}}],
        ),
        TranscriptRow(role="tool", content="data", tool_call_id="c1"),
        TranscriptRow(role="user", content="hidden", scaffold=True),
        TranscriptRow(role="assistant", content=[{"type": "text", "text": "done"}]),
    ]
    shown = display_messages(rows)
    assert [(m.role.value, m.text, m.name) for m in shown] == [
        ("tool", "data", "read_file"),
        ("assistant", "done", None),
    ]
    assert flatten_content([{"type": "input_image"}, "x"]) == "[image]\nx"


def test_display_projection_gives_tool_rows_their_arguments() -> None:
    """W47-a: a transcript rebuilt from history shows the same summary and Arguments as live
    rows, masked as ``tool.started`` masks them."""
    secret = "sk-" + "live" + "A" * 30
    command = f"curl -H 'Authorization: Bearer {secret}' https://example.invalid"
    rows = [
        TranscriptRow(
            role="assistant",
            content=None,
            tool_calls=[
                {"id": "c1", "function": {"name": "read_file", "arguments": '{"path": "a.py"}'}},
                {"id": "c2", "function": {"name": "terminal", "arguments": {"command": command}}},
                {"id": "c3", "function": {"name": "odd", "arguments": "not json"}},
            ],
        ),
        TranscriptRow(role="tool", content="data", tool_call_id="c1"),
        TranscriptRow(role="tool", content="ran", tool_call_id="c2"),
        TranscriptRow(role="tool", content="?", tool_call_id="c3"),
        TranscriptRow(role="tool", content="orphan", tool_call_id="c9", name="x"),
        TranscriptRow(role="assistant", content="done"),
    ]
    shown = display_messages(rows)
    assert shown[0].arguments == {"path": "a.py"}
    ran = shown[1].arguments
    assert ran is not None and secret not in str(ran)
    assert str(ran["command"]).startswith("curl ")
    assert shown[2].arguments is None and shown[3].arguments is None
    assert shown[4].arguments is None  # only tool rows carry them
    assert "arguments" not in shown[4].model_dump(exclude_none=True)


def test_list_and_most_recent_hide_machine_sessions(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        async with open_rig(scope, fake_clock, tmp_path, text_turn("ok")) as rig:
            sid = (await rig.ok("session.create", title="mine"))["session_id"]
            sessions.create(rig.handle, "20260101_000000_aaaaaa", "subagent")
            sessions.create(rig.handle, "20260101_000001_bbbbbb", "oneshot")
            listed = await rig.ok("session.list", limit=10)
            ids = [row["session_id"] for row in listed["sessions"]]
            assert ids == [sid]
            with_children = await rig.ok("session.list", limit=10, include_subagents=True)
            assert "20260101_000000_aaaaaa" in [r["session_id"] for r in with_children["sessions"]]
            recent = await rig.ok("session.most_recent")
            assert recent["session"]["session_id"] == sid

    asyncio.run(main())


def test_title_undo_and_branch(scope: Scope, fake_clock: FakeClock, tmp_path: Path) -> None:
    async def main() -> None:
        turns = (text_turn("one"), text_turn("two"))
        async with open_rig(scope, fake_clock, tmp_path, *turns) as rig:
            sid = (await rig.ok("session.create"))["session_id"]
            early = await rig.ok("session.title", session_id=sid, title="later name")
            assert early == {"title": "later name", "pending": True}
            await rig.ok("prompt.submit", session_id=sid, text="first")
            await rig.turn_done()
            await rig.idle(sid)
            await eventually(lambda: bool(rig.recorder.of("session.title")))
            assert (await rig.ok("session.title", session_id=sid))["title"] == "later name"
            await rig.ok("prompt.submit", session_id=sid, text="second")
            await rig.turn_done(2)
            await rig.idle(sid)
            branch = await rig.ok("session.branch", session_id=sid, title="side")
            assert branch["session_id"] != sid and branch["title"] == "side"
            undone = await rig.ok("session.undo", session_id=sid, count=1)
            assert undone["removed"] == 2 and undone["prefill"] == "second"
            texts = [
                m["text"] for m in (await rig.ok("session.history", session_id=sid))["messages"]
            ]
            assert texts == ["first", "one"]
            nothing = await rig.ok("session.undo", session_id=branch["session_id"], count=5)
            assert nothing["removed"] == 4  # both exchanges of the branch

    asyncio.run(main())


def test_interrupt_returns_at_once(scope: Scope, fake_clock: FakeClock, tmp_path: Path) -> None:
    async def main() -> None:
        async with open_rig(scope, fake_clock, tmp_path, stalled("thinking")) as rig:
            sid = (await rig.ok("session.create"))["session_id"]
            idle = await rig.ok("session.interrupt", session_id=sid)
            assert idle == {"interrupted": False}
            await rig.ok("prompt.submit", session_id=sid, text="go")
            started = time.monotonic()
            stopped = await rig.ok("session.interrupt", session_id=sid, hard=True)
            assert time.monotonic() - started < 0.5
            assert stopped == {"interrupted": True}
            await rig.turn_done()
            assert rig.recorder.of("message.complete")[0]["reason"].startswith("interrupted")

    asyncio.run(main())


def test_steer_on_idle_is_rejected(scope: Scope, fake_clock: FakeClock, tmp_path: Path) -> None:
    async def main() -> None:
        async with open_rig(scope, fake_clock, tmp_path) as rig:
            sid = (await rig.ok("session.create"))["session_id"]
            answer = await rig.ok("session.steer", session_id=sid, text="also this")
            assert answer == {"queued": False}
            redirect = await rig.ok("session.redirect", session_id=sid, text="no")
            assert redirect == {"outcome": "rejected"}

    asyncio.run(main())


def test_busy_submit_rejects_or_queues(scope: Scope, fake_clock: FakeClock, tmp_path: Path) -> None:
    async def main() -> None:
        turns = (stalled("long", 5.0), text_turn("queued answer"))
        async with open_rig(scope, fake_clock, tmp_path, *turns) as rig:
            sid = (await rig.ok("session.create"))["session_id"]
            first = await rig.ok("prompt.submit", session_id=sid, text="slow one")
            refused = await rig.err("prompt.submit", session_id=sid, text="again")
            assert refused["code"] == SESSION_BUSY.code
            queued = await rig.ok("prompt.submit", session_id=sid, text="next", when_busy="queue")
            assert queued["status"] == "queued" and queued["turn_id"] == first["turn_id"]
            await release(fake_clock, lambda: len(rig.recorder.of("message.complete")) >= 1)
            await rig.turn_done(2)
            starts = rig.recorder.of("turn.started")
            assert len(starts) == 2 and starts[1]["generation"] == 2
            assert rig.recorder.of("message.complete")[1]["text"] == "queued answer"
            rig.recorder.check_all()

    asyncio.run(main())


def test_steer_during_compression_is_queued(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        async with open_rig(
            scope, fake_clock, tmp_path, stalled("busy"), text_turn("after")
        ) as rig:
            sid = (await rig.ok("session.create"))["session_id"]
            await rig.ok("prompt.submit", session_id=sid, text="work")
            view = rig.bridge.view(sid)
            assert view is not None
            view.status = "compressing"
            answer = await rig.ok("session.steer", session_id=sid, text="note this")
            assert answer == {"queued": True}
            assert list(view.queue) == ["note this"]
            via_submit = await rig.ok(
                "prompt.submit", session_id=sid, text="and this", when_busy="steer"
            )
            assert via_submit["status"] == "queued"
            await rig.ok("session.interrupt", session_id=sid, hard=True)
            assert not view.queue  # a hard stop drops what was queued

    asyncio.run(main())


def test_background_prompt_reports_to_its_session(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        async with open_rig(scope, fake_clock, tmp_path, text_turn("side answer")) as rig:
            sid = (await rig.ok("session.create"))["session_id"]
            task = await rig.ok("prompt.background", session_id=sid, text="look it up")
            assert task["task_id"] != sid
            await eventually(lambda: bool(rig.recorder.of("notice")))
            notice = rig.recorder.of("notice")[0]
            assert notice["session_id"] == sid and "side answer" in notice["text"]
            service = await rig.service()
            await eventually(lambda: task["task_id"] not in service.attached())

    asyncio.run(main())


def test_usage_and_close(scope: Scope, fake_clock: FakeClock, tmp_path: Path) -> None:
    async def main() -> None:
        async with open_rig(scope, fake_clock, tmp_path) as rig:
            sid = (await rig.ok("session.create"))["session_id"]
            usage = await rig.ok("session.usage", session_id=sid)
            assert usage["usage"]["api_calls"] == 0
            assert (await rig.ok("session.close", session_id=sid))["closed"] is True
            assert (await rig.ok("session.close", session_id=sid))["closed"] is False
            gone = await rig.err("session.usage", session_id=sid)
            assert gone["code"] == SESSION_NOT_FOUND.code

    asyncio.run(main())
