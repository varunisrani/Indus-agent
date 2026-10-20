"""P19-10: session events → ACP updates (golden list, dedupe, open tools, message ids)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from acp.schema import SessionNotification

from indus.acp import tool_kinds
from indus.acp.agent import AcpAgent
from indus.acp.backend import AcpBackend, HostSelector
from indus.acp.projection import Projection, plan_entries, remainder
from indus.foundation.scope import Scope
from indus.protocol.bridge import UsageNumbers
from indus.protocol.events import (
    EventBase,
    ExitReason,
    MessageComplete,
    MessageDelta,
    Notice,
    NoticeLevel,
    ReasoningDelta,
    SessionTitle,
    SessionUsage,
    TodoItem,
    TodoUpdated,
    ToolCompleted,
    ToolStarted,
    TurnDone,
    TurnStarted,
)
from tests.unit.backends._aio import aio

SID = "s-proj"
TURN = "turn-1"


class Recorder:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def notify(self, method: str, params: Mapping[str, Any]) -> None:
        assert method == "session/update"
        self.sent.append(dict(params))

    async def request(self, method: str, params: Mapping[str, Any]) -> Any:
        raise AssertionError("no requests in projection tests")


def _agent(scope: Scope) -> tuple[AcpAgent, Recorder]:
    async def never() -> AcpBackend:
        raise AssertionError("no backend in projection tests")

    selector = HostSelector(local_only=True, allow_fallback=False, attached=never, local=never)
    peer = Recorder()
    agent = AcpAgent(scope, selector, peer=peer, env={})
    agent.sessions.hold(SID, SID)
    return agent, peer


class Seq:
    def __init__(self) -> None:
        self.n = 0

    def __call__(self) -> int:
        self.n += 1
        return self.n


def _replayed(seq: Seq) -> list[EventBase]:
    events: list[EventBase] = [
        TurnStarted(session_id=SID, seq=seq(), turn_id=TURN, generation=1),
        ReasoningDelta(session_id=SID, seq=seq(), turn_id=TURN, text="thinking"),
        MessageDelta(session_id=SID, seq=seq(), turn_id=TURN, text="Let me "),
        MessageDelta(session_id=SID, seq=seq(), turn_id=TURN, text="look."),
        ToolStarted(
            session_id=SID,
            seq=seq(),
            turn_id=TURN,
            call_id="c1",
            name="read_file",
            arguments={"path": "/w/a.py"},
        ),  # fmt: skip
        ToolCompleted(
            session_id=SID,
            seq=seq(),
            turn_id=TURN,
            call_id="c1",
            name="read_file",
            ok=True,
            duration_ms=3,
            result="1|print('hi')",
        ),  # fmt: skip
        ToolStarted(
            session_id=SID,
            seq=seq(),
            turn_id=TURN,
            call_id="c2",
            name="write_file",
            arguments={"path": "/w/b.txt", "content": "new"},
        ),  # fmt: skip
        ToolCompleted(
            session_id=SID,
            seq=seq(),
            turn_id=TURN,
            call_id="c2",
            name="write_file",
            ok=False,
            duration_ms=3,
            result="Blocked: denied",
        ),  # fmt: skip
        ToolStarted(
            session_id=SID,
            seq=seq(),
            turn_id=TURN,
            call_id="c3",
            name="todo_list",
            arguments={"todos": [{"id": "1", "content": "ship", "status": "pending"}]},
        ),  # fmt: skip
        ToolCompleted(
            session_id=SID,
            seq=seq(),
            turn_id=TURN,
            call_id="c3",
            name="todo_list",
            ok=True,
            duration_ms=1,
            result=json.dumps(
                {
                    "todos": [
                        {"id": "1", "content": "ship", "status": "in_progress"},
                        {"id": "2", "content": "old", "status": "cancelled"},
                    ]
                }
            ),
        ),  # fmt: skip
        ToolStarted(
            session_id=SID,
            seq=seq(),
            turn_id=TURN,
            call_id="c4",
            name="terminal",
            arguments={"command": "rm -r build"},
        ),  # fmt: skip
        MessageDelta(session_id=SID, seq=seq(), turn_id=TURN, text="Done"),
        TurnDone(session_id=SID, seq=seq(), turn_id=TURN, reason=ExitReason.COMPLETED),
        MessageComplete(
            session_id=SID, seq=seq(), turn_id=TURN, text="Done.", reason=ExitReason.COMPLETED
        ),
        SessionUsage(
            session_id=SID,
            seq=seq(),
            usage=UsageNumbers(
                input_tokens=10, output_tokens=5, context_tokens=1200, context_window=200_000
            ),
        ),  # fmt: skip
    ]
    return events


def _shape(update: Mapping[str, Any]) -> tuple[Any, ...]:
    kind = update["sessionUpdate"]
    if kind in ("agent_message_chunk", "agent_thought_chunk"):
        return (kind, update["content"]["text"])
    if kind == "tool_call":
        return (kind, update["toolCallId"], update["kind"], update["title"], update["status"])
    if kind == "tool_call_update":
        return (kind, update["toolCallId"], update["status"])
    if kind == "plan":
        return (kind, tuple((e["content"], e["status"]) for e in update["entries"]))
    if kind == "usage_update":
        return (kind, update["size"], update["used"])
    return (kind,)


GOLDEN: list[tuple[Any, ...]] = [
    ("agent_thought_chunk", "thinking"),
    ("agent_message_chunk", "Let me "),
    ("agent_message_chunk", "look."),
    ("tool_call", "c1", "read", "read_file: /w/a.py", "in_progress"),
    ("tool_call_update", "c1", "completed"),
    ("tool_call", "c2", "edit", "write_file: /w/b.txt", "in_progress"),
    ("tool_call_update", "c2", "failed"),
    ("tool_call", "c3", "other", "todo_list", "in_progress"),
    ("tool_call_update", "c3", "completed"),
    ("plan", (("ship", "in_progress"), ("[cancelled] old", "completed"))),
    ("tool_call", "c4", "execute", "terminal: rm -r build", "in_progress"),
    ("agent_message_chunk", "Done"),
    ("tool_call_update", "c4", "failed"),
    ("agent_message_chunk", "."),
    ("usage_update", 200_000, 1200),
]


@aio
async def test_a_replayed_turn_projects_to_the_golden_updates(scope: Scope) -> None:
    agent, peer = _agent(scope)
    projection: Projection = agent.projection
    for event in _replayed(Seq()):
        projection.on_event(SID, event)
    await projection.settled(SID, TURN, grace=1.0)
    updates = [frame["update"] for frame in peer.sent]
    for frame in peer.sent:
        SessionNotification.model_validate(frame)  # every update is a valid vendor frame
    assert [_shape(u) for u in updates] == GOLDEN
    ids = [u.get("messageId") for u in updates if u["sessionUpdate"].endswith("_chunk")]
    assert ids[0] == ids[1] == ids[2]  # thought and text of one message share the id
    assert ids[3] != ids[2]  # the text after the tools is a new message
    assert ids[3] == ids[4]  # the remainder belongs to the message it completes
    write = next(
        u for u in updates if u.get("toolCallId") == "c2" and u["sessionUpdate"] == "tool_call"
    )
    assert write["content"] == [{"type": "diff", "path": "/w/b.txt", "newText": "new"}]
    abandoned = updates[12]
    assert abandoned["content"][0]["content"]["text"] == tool_kinds.ABANDONED_TEXT
    assert projection.usage_of(SID) is not None
    await agent.aclose()


@aio
async def test_duplicate_and_snapshot_events_are_dropped(scope: Scope) -> None:
    agent, peer = _agent(scope)
    projection = agent.projection
    projection.track(SID, 5)
    old = MessageDelta(session_id=SID, seq=5, turn_id=TURN, text="seen in the snapshot")
    new = MessageDelta(session_id=SID, seq=6, turn_id=TURN, text="new")
    for event in (old, new, new):
        projection.on_event(SID, event)
    await projection.flush()
    assert [f["update"]["content"]["text"] for f in peer.sent] == ["new"]
    await agent.aclose()


@aio
async def test_a_gap_fails_waiters_and_moves_the_floor(scope: Scope) -> None:
    agent, peer = _agent(scope)
    projection = agent.projection
    projection.on_event(SID, MessageDelta(session_id=SID, seq=40, turn_id=TURN, text="old run"))
    projection.on_gap(SID, {"reason": "epoch_changed", "replay_epoch": "e2", "last_seq": 3})
    assert projection.broken(SID).is_set()
    projection.on_event(SID, MessageDelta(session_id=SID, seq=4, turn_id="t2", text="new run"))
    await projection.flush()
    assert [f["update"]["content"]["text"] for f in peer.sent] == ["old run", "new run"]
    await agent.aclose()


@aio
async def test_titles_only_when_changed_and_notices_as_text(scope: Scope) -> None:
    agent, peer = _agent(scope)
    projection = agent.projection
    projection.known_title(SID, "Old")
    seq = Seq()
    for title in ("Old", "New", "New"):
        projection.on_event(SID, SessionTitle(session_id=SID, seq=seq(), title=title))
    projection.on_event(
        SID,
        Notice(session_id=SID, seq=seq(), level=NoticeLevel.WARNING, text="Falling back"),
    )
    projection.on_event(
        SID,
        Notice(session_id=SID, seq=seq(), level=NoticeLevel.INFO, text="docs", key="tools_changed"),
    )
    projection.on_event(
        SID,
        TodoUpdated(
            session_id=SID,
            seq=seq(),
            todos=[TodoItem(id="1", content="plan it", status="completed")],
            revision=1,
        ),
    )
    await projection.flush()
    updates = [f["update"] for f in peer.sent]
    assert updates[0] == {"sessionUpdate": "session_info_update", "title": "New"}
    assert updates[1]["content"]["text"] == "Falling back\n"
    assert "Editor MCP tools changed (docs)" in updates[2]["content"]["text"]
    assert updates[3]["entries"] == [
        {"content": "plan it", "priority": "medium", "status": "completed"}
    ]
    assert len(updates) == 4
    await agent.aclose()


@aio
async def test_a_retry_receipt_shows_the_reply_once(scope: Scope) -> None:
    agent, peer = _agent(scope)
    projection = agent.projection
    projection.show_receipt(SID, 7, "the answer")  # generation 7 settled before its events
    projection.on_event(SID, TurnStarted(session_id=SID, seq=1, turn_id=TURN, generation=7))
    projection.on_event(
        SID,
        MessageComplete(
            session_id=SID, seq=2, turn_id=TURN, text="the answer", reason=ExitReason.COMPLETED
        ),
    )
    assert await projection.reply_shown(SID, 7, grace=1.0)
    await projection.settled(SID, TURN, grace=1.0)
    texts = [f["update"]["content"]["text"] for f in peer.sent]
    assert texts == ["the answer"]
    await agent.aclose()


def test_the_final_text_is_shown_exactly_once() -> None:
    assert remainder("Hello world", "Hello ", []) == "world"
    assert remainder("Hello", "Hello", []) == ""
    assert remainder("Fresh", "", []) == "Fresh"
    assert remainder("Checking.", "", ["Checking."]) == ""
    assert remainder("Other", "Partial", []) == "\n\nOther"
    assert remainder("   ", "x", []) == ""


def test_plan_entries_keep_cancelled_items() -> None:
    entries = plan_entries(
        [{"id": "a", "content": "one", "status": "cancelled"}, {"content": " "}, "x"]
    )
    assert entries == [{"content": "[cancelled] one", "priority": "medium", "status": "completed"}]
    assert plan_entries("nope") is None
