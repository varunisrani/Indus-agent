"""Event projection (P12-29): tool-progress modes, text cuts, redaction, todo list, validation."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import JsonValue

from indus.foundation.scope import Scope
from indus.protocol.events import EVENTS, ToolCompleted, ToolStarted, TurnStarted
from indus.tui_bridge.bridge import ContractViolation, SessionView
from indus.tui_bridge.events import (
    PREVIEW_CHARS,
    VERBOSE_CHARS,
    VERBOSE_LINES,
    cut_head,
    cut_tail,
)
from tests.contract.tui_bridge._harness import Rig, open_rig
from tests.fakes.agent import eventually
from tests.fakes.clock import FakeClock

SID = "sess-e"


def started(seq: int, call: str, name: str, **args: JsonValue) -> ToolStarted:
    return ToolStarted(
        session_id=SID, seq=seq, turn_id="t1", call_id=call, name=name, arguments=args
    )


def completed(seq: int, call: str, name: str, result: str, *, ok: bool = True) -> ToolCompleted:
    return ToolCompleted(
        session_id=SID,
        seq=seq,
        turn_id="t1",
        call_id=call,
        name=name,
        ok=ok,
        duration_ms=3,
        result=result,
    )


def feed(rig: Rig, view: SessionView, *events: object) -> list[str]:
    before = len(rig.recorder.frames)
    for event in events:
        rig.bridge.projector._on_event(view, event)  # pyright: ignore[reportPrivateUsage, reportArgumentType]
    return [m for m, _ in rig.recorder.frames[before:]]


def test_verbose_text_is_cut_at_a_line_boundary() -> None:
    lines = [f"line {n:03d} " + "x" * 90 for n in range(40)]
    text = "\n".join(lines)
    cut = cut_tail(text)
    head, _, kept = cut.partition("\n")
    assert head.startswith("[… ") and head.endswith(" omitted]")
    assert len(kept) <= VERBOSE_CHARS and kept.count("\n") < VERBOSE_LINES
    assert kept.startswith("line ") and kept.endswith(lines[-1])
    assert kept in text and text.index(kept) > 0 and text[text.index(kept) - 1] == "\n"
    omitted_lines = text[: text.index(kept)].count("\n")
    assert f"{omitted_lines} lines" in head
    short = "a\nb\nc"
    assert cut_tail(short) == short
    many = "\n".join(str(n) for n in range(30))
    kept_many = cut_tail(many).partition("\n")[2]
    assert kept_many.splitlines() == [str(n) for n in range(14, 30)]


def test_head_preview_is_one_line() -> None:
    assert cut_head("first line\nsecond") == "first line …"
    long = "y" * 500
    assert len(cut_head(long)) == PREVIEW_CHARS
    assert cut_head("\n\n  only  ") == "  only  "


def test_modes_filter_tool_events(scope: Scope, fake_clock: FakeClock, tmp_path: Path) -> None:
    async def main() -> None:
        async with open_rig(scope, fake_clock, tmp_path) as rig:
            await rig.service()
            view = SessionView(SID, tool_mode="off")
            turn = TurnStarted(session_id=SID, seq=0, turn_id="t1", generation=1)
            off = feed(
                rig,
                view,
                turn,
                started(1, "c1", "read_file", path="a"),
                completed(2, "c1", "read_file", "data"),
                started(3, "c2", "read_file", path="b"),
                completed(4, "c2", "read_file", "boom", ok=False),
                started(5, "c3", "write_file", path="c"),
                completed(6, "c3", "write_file", "ok"),
            )
            assert off == ["turn.started", "tool.completed", "tool.started", "tool.completed"]
            exit_code = json.dumps({"output": "", "exit_code": 2, "error": None})
            quiet_fail = feed(
                rig,
                view,
                started(7, "c4", "terminal", command="false"),
                completed(8, "c4", "terminal", exit_code),
                started(9, "c5", "terminal", command="true"),
                completed(10, "c5", "terminal", json.dumps({"output": "", "exit_code": 0})),
            )
            assert quiet_fail == ["tool.completed"]
            view = SessionView(SID, tool_mode="new")
            new = feed(
                rig,
                view,
                turn,
                started(1, "c1", "search", q="a"),
                completed(2, "c1", "search", "x"),
                started(3, "c2", "search", q="b"),
                completed(4, "c2", "search", "y"),
                started(5, "c3", "read_file", path="z"),
            )
            assert new == ["turn.started", "tool.started", "tool.completed", "tool.started"]
            view = SessionView(SID, tool_mode="all")
            every = feed(rig, view, turn, started(1, "c1", "search"), started(2, "c2", "search"))
            assert every == ["turn.started", "tool.started", "tool.started"]
            for method, params in rig.recorder.frames:
                EVENTS[method].model_validate(params)

    asyncio.run(main())


def test_verbose_result_is_redacted_before_cut(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
        filler = "\n".join("z" * 70 for _ in range(30))
        result = f"{filler}\ntoken {token}\nlast line"
        async with open_rig(scope, fake_clock, tmp_path) as rig:
            await rig.service()
            verbose = SessionView(SID, tool_mode="verbose")
            feed(rig, verbose, completed(1, "c1", "terminal", result))
            sent = rig.recorder.of("tool.completed")[-1]["result"]
            assert token not in sent and "last line" in sent and "omitted]" in sent
            plain = SessionView(SID, tool_mode="all")
            feed(rig, plain, completed(2, "c2", "terminal", f"token {token}\nmore"))
            preview = rig.recorder.of("tool.completed")[-1]["result"]
            assert token not in preview and preview.startswith("token ")
            big = {"text": "q" * 5000}
            feed(rig, plain, started(3, "c3", "write_file", **big))
            capped = rig.recorder.of("tool.started")[-1]["arguments"]["text"]
            assert len(capped) == 200

    asyncio.run(main())


def test_todo_completion_projects_the_list(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        async with open_rig(scope, fake_clock, tmp_path) as rig:
            service = await rig.service()
            sid = (await rig.ok("session.create"))["session_id"]
            view = rig.bridge.view(sid)
            assert view is not None
            listing = {
                "todos": [
                    {"id": "1", "content": "write tests", "status": "in_progress"},
                    {"id": "2", "content": "ship", "status": "pending", "parent": "1"},
                ],
                "revision": 3,
                "counts": {"pending": 1},
            }
            event = ToolCompleted(
                session_id=sid,
                seq=service.events(sid)[-1].seq + 1,
                turn_id="t1",
                call_id="c9",
                name="todo_list",
                ok=True,
                duration_ms=1,
                result=json.dumps(listing),
            )
            rig.bridge.projector._on_event(view, event)  # pyright: ignore[reportPrivateUsage]
            await eventually(lambda: bool(rig.recorder.of("todo.updated")))
            todo = rig.recorder.of("todo.updated")[0]
            assert todo["revision"] == 3 and todo["todos"][1]["parent"] == "1"
            for result in ("not json", json.dumps({"todos": [], "revision": 0})):
                rig.bridge.projector._on_event(  # pyright: ignore[reportPrivateUsage]
                    view, event.model_copy(update={"result": result})
                )
            assert len(rig.recorder.of("todo.updated")) == 1
            rig.recorder.check_all()

    asyncio.run(main())


def test_bad_payload_raises_in_strict_mode(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        async with open_rig(scope, fake_clock, tmp_path) as rig:
            projector = rig.bridge.projector
            with pytest.raises(ContractViolation):
                projector.check(ToolStarted, {"type": "tool.started", "session_id": SID})
            rig.bridge.strict = False
            assert projector.check(ToolStarted, {"type": "tool.started"}) is False

    asyncio.run(main())
