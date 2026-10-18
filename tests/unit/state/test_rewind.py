"""Rewind (/undo, /retry), recent user turns, clear, and branch."""

from __future__ import annotations

import json
from typing import Any

import pytest

from indus.state import compression, rewind, session_meta
from indus.state.compression import CompressionBusy
from indus.state.handoff import HANDOFF_FINISH
from indus.state.registry import StoreHandle
from indus.state.rewind import BranchRefused, RewindMismatch, RewindUnavailable
from indus.state.titles import TitleTaken
from tests.unit.state._helpers_b import (
    add_message,
    add_session,
    live_contents,
    message_rows,
    seed_turns,
    session,
)

CALLS = [{"id": "c1", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}]


def warm(store: StoreHandle, sid: str) -> list[dict[str, Any]]:
    """What a running process would hold: the live rows as plain dicts."""
    out: list[dict[str, Any]] = []
    for row in message_rows(store, sid, include_inactive=False):
        content = row["content"]
        if isinstance(content, str) and content.startswith("\x00indus.parts:"):
            content = json.loads(content[len("\x00indus.parts:") :])
        out.append({"role": row["role"], "content": content})
    return out


@pytest.fixture
def chat(store: StoreHandle) -> str:
    add_session(store, "s1", title="name")
    seed_turns(store, "s1", 3)
    return "s1"


def test_rewind_last_exchange_returns_its_text(store: StoreHandle, chat: str) -> None:
    result = rewind.rewind(store, chat, -1, warm(store, chat))
    assert result.target_text == "q3"
    assert result.removed == 2
    assert live_contents(store, chat) == ["q1", "a1", "q2", "a2"]
    assert result.new_head_id == message_rows(store, chat, include_inactive=False)[-1]["id"]
    meta = session(store, chat)
    assert meta is not None
    assert (meta["rewind_count"], meta["message_count"]) == (1, 4)


def test_rewind_by_ordinal_and_clamping(store: StoreHandle, chat: str) -> None:
    assert rewind.rewind(store, chat, 1, None).target_text == "q2"
    assert live_contents(store, chat) == ["q1", "a1"]
    result = rewind.rewind(store, chat, -9, None)  # clamps to the oldest turn
    assert result.target_text == "q1"
    assert result.new_head_id is None
    assert live_contents(store, chat) == []
    meta = session(store, chat)
    assert meta is not None and (meta["rewind_count"], meta["message_count"]) == (2, 0)


def test_tool_exchange_goes_with_its_turn(store: StoreHandle) -> None:
    add_session(store, "s2")
    for i in (1, 2):
        add_message(store, "s2", "user", [{"type": "text", "text": f"q{i}"}, {"type": "image"}])
        add_message(store, "s2", "assistant", None, tool_calls=CALLS)
        add_message(store, "s2", "tool", f"out{i}", tool_call_id="c1")
        add_message(store, "s2", "assistant", f"a{i}")
    result = rewind.rewind(store, "s2", -1, warm(store, "s2"))
    assert result.target_text == "q2"
    assert [r["role"] for r in message_rows(store, "s2", include_inactive=False)] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    meta = session(store, "s2")
    assert meta is not None and (meta["message_count"], meta["tool_call_count"]) == (4, 1)


@pytest.mark.parametrize("change", ["extra turn", "missing turn", "edited target"])
def test_mismatched_warm_history_changes_nothing(
    store: StoreHandle, chat: str, change: str
) -> None:
    history = warm(store, chat)
    if change == "extra turn":
        history.append({"role": "user", "content": "typed but never saved"})
    elif change == "missing turn":
        history = history[2:]
    else:
        history[-2] = {"role": "user", "content": "q3, edited"}
    before = message_rows(store, chat)
    with pytest.raises(RewindMismatch):
        rewind.rewind(store, chat, -1, history)
    assert message_rows(store, chat) == before
    meta = session(store, chat)
    assert meta is not None and meta["rewind_count"] == 0


def test_nothing_to_rewind(store: StoreHandle, chat: str) -> None:
    add_session(store, "empty")
    with pytest.raises(RewindUnavailable):
        rewind.rewind(store, "empty", -1, [])
    with pytest.raises(RewindUnavailable):
        rewind.rewind(store, chat, 3, None)
    assert len(live_contents(store, chat)) == 6


def test_rewound_rows_stay_for_audit(store: StoreHandle, chat: str) -> None:
    rewind.rewind(store, chat, -1, None)
    everything = message_rows(store, chat)
    assert [r["content"] for r in everything] == ["q1", "a1", "q2", "a2", "q3", "a3"]
    assert [r["active"] for r in everything] == [1, 1, 1, 1, 0, 0]


def test_handoff_rows_are_not_user_turns(store: StoreHandle) -> None:
    add_session(store, "c1")
    add_message(store, "c1", "user", "summary so far", finish_reason=HANDOFF_FINISH)
    add_message(store, "c1", "assistant", "ok", finish_reason=HANDOFF_FINISH)
    add_message(store, "c1", "user", "next ask")
    add_message(store, "c1", "assistant", "answer")
    # A warm list built without the marker still lines up (handoff text recognised).
    history = [{"role": m["role"], "content": m["content"]} for m in warm(store, "c1")]
    assert rewind.rewind(store, "c1", 0, history).target_text == "next ask"
    assert live_contents(store, "c1") == ["summary so far", "ok"]
    with pytest.raises(RewindUnavailable):
        rewind.rewind(store, "c1", -1, None)


def test_live_compression_lock_refuses(store: StoreHandle, chat: str) -> None:
    assert compression.acquire(store, chat, "rotator")
    with pytest.raises(CompressionBusy):
        rewind.rewind(store, chat, -1, None)
    assert len(live_contents(store, chat)) == 6


def test_recent_user_messages(store: StoreHandle, chat: str) -> None:
    add_message(store, chat, "user", "  many\n\nwords   " + "x" * 100)
    add_message(store, chat, "user", "summary", finish_reason=HANDOFF_FINISH)
    add_message(store, chat, "user", [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}])
    add_message(store, chat, "user", "gone", active=0)
    turns = rewind.recent_user_messages(store, chat, 3)
    assert [t.preview for t in turns] == ["ab", "many words " + "x" * 66 + "...", "q3"]
    assert len(turns[1].preview) == 80
    assert turns[0].id > turns[1].id > turns[2].id
    assert rewind.recent_user_messages(store, chat, 0) == []
    assert len(rewind.recent_user_messages(store, chat)) == 5


def test_clear_zeroes_counters(store: StoreHandle) -> None:
    add_session(store, "s3")
    add_message(store, "s3", "assistant", None, tool_calls=CALLS)
    store.write(lambda c: c.execute("UPDATE sessions SET tool_call_count = 1 WHERE id = 's3'"))
    assert rewind.clear(store, "s3") == 1
    assert message_rows(store, "s3") == []
    meta = session(store, "s3")
    assert meta is not None and (meta["message_count"], meta["tool_call_count"]) == (0, 0)


# -- branch ------------------------------------------------------------------------------------


@pytest.fixture
def routed(store: StoreHandle) -> str:
    add_session(
        store,
        "p1",
        title="name",
        model="model-a",
        billing_provider="prov",
        billing_base_url="https://api.example.test",
        cwd="/work",
        profile_id="work",
        pinned=1,
        model_config={"_delegate_from": "older", "temperature": 1},
    )
    seed_turns(store, "p1", 2)
    return "p1"


def test_branch_copies_up_to_the_boundary(store: StoreHandle, routed: str) -> None:
    rows = message_rows(store, routed)
    child = rewind.branch(store, routed, upto_message_id=rows[1]["id"])
    copied = message_rows(store, child)
    assert [r["content"] for r in copied] == ["q1", "a1"]
    assert [r["message_uid"] for r in copied] == [r["message_uid"] for r in rows[:2]]
    assert all(c["id"] > r["id"] for c, r in zip(copied, rows, strict=False))
    meta = session(store, child)
    assert meta is not None
    assert (meta["title"], meta["title_source"]) == ("name #2", "derived")
    assert meta["parent_session_id"] == routed
    assert json.loads(meta["model_config"]) == {"_branched_from": routed}
    assert (meta["model"], meta["billing_provider"], meta["cwd"], meta["profile_id"]) == (
        "model-a",
        "prov",
        "/work",
        "work",
    )
    assert (meta["pinned"], meta["message_count"]) == (0, 2)
    parent = session(store, routed)
    assert parent is not None and parent["ended_at"] is None and parent["message_count"] == 4
    assert len(message_rows(store, routed)) == 4


def test_branch_whole_transcript_with_user_title(store: StoreHandle, routed: str) -> None:
    child = rewind.branch(store, routed, title="my fork")
    assert live_contents(store, child) == ["q1", "a1", "q2", "a2"]
    meta = session(store, child)
    assert meta is not None and (meta["title"], meta["title_source"]) == ("my fork", "user")
    second = rewind.branch(store, routed)
    third = rewind.branch(store, routed)
    titles = [(session(store, s) or {}).get("title") for s in (second, third)]
    assert titles == ["name #2", "name #3"]


def test_assistant_boundary_takes_its_tool_results(store: StoreHandle) -> None:
    add_session(store, "p2")
    add_message(store, "p2", "user", "go")
    call = add_message(store, "p2", "assistant", None, tool_calls=CALLS)
    add_message(store, "p2", "tool", "result", tool_call_id="c1")
    add_message(store, "p2", "assistant", "done")
    child = rewind.branch(store, "p2", upto_message_id=call)
    assert [r["role"] for r in message_rows(store, child)] == ["user", "assistant", "tool"]
    meta = session(store, child)
    assert meta is not None and (meta["message_count"], meta["tool_call_count"]) == (3, 1)
    assert meta["title"] == "branch"


def test_branch_refusals_write_nothing(store: StoreHandle, routed: str) -> None:
    add_session(store, "other", title="taken")
    foreign = add_message(store, "other", "user", "x")
    dead = add_message(store, routed, "user", "rewound", active=0)
    for bad in (dead, foreign, 999_999):
        with pytest.raises(BranchRefused):
            rewind.branch(store, routed, upto_message_id=bad)
    with pytest.raises(BranchRefused):
        rewind.branch(store, "missing")
    with pytest.raises(TitleTaken):
        rewind.branch(store, routed, title="taken")
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 2


def test_branch_copies_branch_safe_metadata(
    store: StoreHandle, routed: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, str]] = []

    def copy(conn: object, src: str, dst: str, *, now: float) -> int:
        seen.append((src, dst))
        return 0

    monkeypatch.setattr(session_meta, "copy_branch_safe_in", copy)
    child = rewind.branch(store, routed)
    assert seen == [(routed, child)]
