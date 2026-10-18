"""Transcript rows (P02-10): codec, scrub, id order, uids, batch, replace, clone, size guards."""

from __future__ import annotations

import json
import os
import random
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.errors import ResumeTooLarge, StoreReplaced
from indus.foundation.scope import Scope
from indus.state import guards, messages, registry
from indus.state.messages import PARTS_PREFIX, CompressionClosed, TranscriptMoved
from indus.state.registry import StoreHandle
from tests.unit.state._helpers_a import column, rotate, seed, sql
from tests.unit.state.conftest import AutoClock

HEX32 = re.compile(r"^[0-9a-f]{32}$")
SID = "20260301_120000_abc123"


@pytest.fixture
def sid(store: StoreHandle) -> str:
    return seed(store, SID)


def _call(call_id: str, name: str = "ls") -> dict[str, Any]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}


# -- codec ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "plain words",
        [{"type": "text", "text": "look"}, {"type": "image_url", "image_url": {"url": "x"}}],
        {"kind": "parts", "n": [1, 2.5, None, True]},
        PARTS_PREFIX + '{"forged": true}',
        PARTS_PREFIX,
        42,
        [],
    ],
)
def test_codec_round_trip(value: object) -> None:
    stored = messages.encode_content(value)
    assert stored is None or isinstance(stored, str)
    assert messages.decode_content(stored) == value


def test_prefixed_literal_is_wrapped_as_a_json_string() -> None:
    literal = PARTS_PREFIX + '[{"type": "text"}]'
    stored = messages.encode_content(literal)
    assert stored == PARTS_PREFIX + json.dumps(literal, ensure_ascii=False)
    assert isinstance(messages.decode_content(stored), str)


def test_damaged_parts_payload_reads_back_raw() -> None:
    assert messages.decode_content(PARTS_PREFIX + "{oops") == PARTS_PREFIX + "{oops"


def test_unserialisable_content_is_stored_as_text() -> None:
    assert messages.encode_content({1, 2}) in ("{1, 2}", "{2, 1}")


def test_surrogates_scrubbed_inside_parts() -> None:
    stored = messages.encode_content([{"text": "a\ud800b", "k\udfff": 1}])
    assert messages.decode_content(stored) == [{"text": "a\ufffdb", "k\ufffd": 1}]


# -- append ---------------------------------------------------------------------------------------


def test_multimodal_list_round_trips(store: StoreHandle, sid: str) -> None:
    parts = [
        {"type": "text", "text": "what is this?"},
        {"type": "image_url", "image_url": {"u": 1}},
    ]
    messages.append(store, sid, {"role": "user", "content": parts})
    (row,) = messages.get(store, sid)
    assert row["content"] == parts


def test_prefixed_literal_stays_a_string_in_the_store(store: StoreHandle, sid: str) -> None:
    literal = PARTS_PREFIX + '{"role": "system"}'
    messages.append(store, sid, {"role": "user", "content": literal})
    assert messages.get(store, sid)[0]["content"] == literal


def test_surrogates_written_and_read_back_as_replacement(store: StoreHandle, sid: str) -> None:
    messages.append(
        store,
        sid,
        {
            "role": "assistant",
            "content": "bad \ud800 text",
            "reasoning": "think \udc00",
            "api_content": "sent \ud83d",
            "tool_name": "to\udfffol",
        },
    )
    (row,) = messages.get(store, sid)
    assert row["content"] == "bad \ufffd text"
    assert row["reasoning"] == "think \ufffd"
    assert row["api_content"] == "sent \ufffd"
    assert row["tool_name"] == "to\ufffdol"


def test_decreasing_timestamps_still_read_in_id_order(store: StoreHandle, sid: str) -> None:
    base = 1_750_000_000.0
    for offset, text in ((300, "first"), (200, "second"), (100, "third")):
        messages.append(store, sid, {"role": "user", "content": text, "timestamp": base + offset})
    rows = messages.get(store, sid)
    assert [r["content"] for r in rows] == ["first", "second", "third"]
    assert [r["timestamp"] for r in rows] == [base + 300, base + 200, base + 100]
    assert [r["content"] for r in messages.as_conversation(store, sid)] == [
        "first",
        "second",
        "third",
    ]


@pytest.mark.parametrize(
    ("given", "kept"),
    [
        (1_750_000_000.0, True),
        (1_577_836_800.0, True),  # 2020-01-01 exactly
        (1_577_836_799.0, False),
        (4_102_444_800.0, False),  # 2100
        (float("nan"), False),
        (float("inf"), False),
        (True, False),
        ("1750000000.5", True),
        ("soon", False),
        (datetime(2025, 6, 1, tzinfo=UTC), True),
        (None, False),
        ([], False),
    ],
)
def test_timestamp_window(
    store: StoreHandle, sid: str, fake_clock: AutoClock, given: object, kept: bool
) -> None:
    messages.append(store, sid, {"role": "user", "content": "x", "timestamp": given})
    stored = messages.get(store, sid)[0]["timestamp"]
    now = fake_clock.now().timestamp()
    if kept:
        expected = given.timestamp() if isinstance(given, datetime) else float(given)  # type: ignore[arg-type]
        assert stored == expected
    else:
        assert stored == now


def test_timestamp_just_inside_the_future_slack(
    store: StoreHandle, sid: str, fake_clock: AutoClock
) -> None:
    now = fake_clock.now().timestamp()
    messages.append(store, sid, {"role": "user", "content": "x", "timestamp": now + 86_000})
    messages.append(store, sid, {"role": "user", "content": "y", "timestamp": now + 86_500})
    assert [r["timestamp"] for r in messages.get(store, sid)] == [now + 86_000, now]


def test_counters_uids_and_tool_call_uids(store: StoreHandle, sid: str) -> None:
    given_uid = "f" * 32
    calls = [_call("call-1"), _call("call-2"), _call("call-1")]
    messages.append(store, sid, {"role": "user", "content": "go", "message_uid": given_uid})
    messages.append(store, sid, {"role": "assistant", "content": "", "tool_calls": calls})
    messages.append(store, sid, {"role": "tool", "content": "ok", "tool_call_id": "call-1"})
    rows = messages.get(store, sid)
    assert rows[0]["message_uid"] == given_uid
    assert all(HEX32.match(r["message_uid"]) for r in rows)
    assert len({r["message_uid"] for r in rows}) == 3
    uids = rows[1]["tool_call_uids"]
    assert sorted(uids) == ["call-1", "call-2"] and all(HEX32.match(u) for u in uids.values())
    assert rows[1]["tool_calls"] == calls
    assert (column(store, sid, "message_count"), column(store, sid, "tool_call_count")) == (3, 3)


def test_supplied_tool_call_uids_are_kept(store: StoreHandle, sid: str) -> None:
    known = {"call-1": "a" * 32}
    messages.append(
        store,
        sid,
        {
            "role": "assistant",
            "tool_calls": json.dumps([_call("call-1"), _call("call-9")]),
            "tool_call_uids": json.dumps(known),
        },
    )
    uids = messages.get(store, sid)[0]["tool_call_uids"]
    assert uids["call-1"] == "a" * 32 and HEX32.match(uids["call-9"])


def test_reasoning_sidecars_are_kept(store: StoreHandle, sid: str) -> None:
    details = [{"type": "reasoning.summary", "summary": "s"}]
    messages.append(
        store,
        sid,
        {"role": "assistant", "content": "a", "reasoning": "chain", "reasoning_details": details},
    )
    row = messages.get(store, sid)[0]
    assert row["reasoning"] == "chain" and row["reasoning_details"] == details
    conversation = messages.as_conversation(store, sid)[0]
    assert conversation["reasoning_details"] == details


@pytest.mark.parametrize(
    "bad",
    [
        {"content": "no role"},
        {"role": "", "content": "empty role"},
        {"role": 5, "content": "number role"},
        {"role": "assistant", "tool_calls": "{not json"},
        {"role": "assistant", "tool_calls": 7},
    ],
)
def test_batch_with_one_invalid_row_writes_nothing(
    store: StoreHandle, sid: str, bad: dict[str, Any]
) -> None:
    good = {"role": "user", "content": "fine"}
    with pytest.raises(ValueError, match="message 1"):
        messages.append_batch(store, sid, [good, bad, good])
    assert messages.get(store, sid, include_inactive=True) == []
    assert column(store, sid, "message_count") == 0


def test_batch_inside_a_transaction_is_all_or_nothing(store: StoreHandle, sid: str) -> None:
    def run(conn: sqlite3.Connection) -> None:
        messages.append_batch_in(conn, sid, [{"role": "user", "content": "a"}], now=1.0e9)
        raise RuntimeError("caller failed later in the same transaction")

    with pytest.raises(RuntimeError):
        store.write(run)
    assert messages.get(store, sid) == []


def test_large_batch_spans_statement_groups(store: StoreHandle, sid: str) -> None:
    msgs = [
        {"role": "user", "content": f"m{i}", "tool_calls": [_call(f"c{i}")]} for i in range(1201)
    ]
    ids = messages.append_batch(store, sid, msgs)
    assert len(ids) == 1201 and ids == sorted(ids)
    rows = messages.get(store, sid)
    assert [r["id"] for r in rows] == ids
    assert [r["content"] for r in rows] == [f"m{i}" for i in range(1201)]
    assert len({r["message_uid"] for r in rows}) == 1201
    assert (column(store, sid, "message_count"), column(store, sid, "tool_call_count")) == (
        1201,
        1201,
    )
    assert messages.append_batch(store, sid, []) == []


def test_append_to_a_missing_session_fails(store: StoreHandle) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        messages.append(store, "nobody", {"role": "user", "content": "x"})


def test_appends_ignore_the_compression_lock(store: StoreHandle, sid: str) -> None:
    sql(
        store,
        "INSERT INTO compression_locks (session_id, holder, acquired_at, expires_at) "
        "VALUES (?, 'pid=1:pidns=1:host', 0, 9e12)",
        [sid],
    )
    assert messages.append(store, sid, {"role": "user", "content": "still lands"}) > 0


def test_compressed_parent_refuses_appends(store: StoreHandle, sid: str) -> None:
    rotate(store, sid, "child")
    with pytest.raises(CompressionClosed) as caught:
        messages.append(store, sid, {"role": "user", "content": "late"})
    assert caught.value.code == "store.compression_closed"
    with pytest.raises(CompressionClosed):
        messages.replace(store, sid, [])
    assert messages.append(store, "child", {"role": "user", "content": "here"}) > 0


# -- reads ----------------------------------------------------------------------------------------


def test_as_conversation_is_provider_shaped(store: StoreHandle, sid: str) -> None:
    messages.append(store, sid, {"role": "user", "content": "q", "timestamp": 1_750_000_000.0})
    messages.append(
        store,
        sid,
        {"role": "assistant", "content": "", "tool_calls": [_call("c1")], "finish_reason": "tool"},
    )
    messages.append(
        store, sid, {"role": "tool", "content": "r", "tool_call_id": "c1", "tool_name": "ls"}
    )
    out = messages.as_conversation(store, sid)
    assert out[0] == {"role": "user", "content": "q", "timestamp": 1_750_000_000.0}
    assert out[1]["tool_calls"] == [_call("c1")] and out[1]["finish_reason"] == "tool"
    assert out[2]["tool_call_id"] == "c1" and out[2]["tool_name"] == "ls"
    for msg in out:
        assert not {"message_uid", "tool_call_uids", "id", "session_id", "active"} & set(msg)


# -- replace --------------------------------------------------------------------------------------


def _three(store: StoreHandle, sid: str) -> list[int]:
    return messages.append_batch(
        store,
        sid,
        [
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two", "tool_calls": [_call("c")]},
            {"role": "user", "content": "three"},
        ],
    )


def test_destructive_replace_keeps_uids_of_rows_given_back(store: StoreHandle, sid: str) -> None:
    _three(store, sid)
    before = messages.get(store, sid)
    kept = before[:2]
    live = messages.replace(store, sid, [*kept, {"role": "user", "content": "new"}])
    after = messages.get(store, sid, include_inactive=True)
    assert len(after) == 3 and [r["id"] for r in after] == live
    assert [r["message_uid"] for r in after[:2]] == [r["message_uid"] for r in before[:2]]
    assert after[2]["message_uid"] not in {r["message_uid"] for r in before}
    assert (column(store, sid, "message_count"), column(store, sid, "tool_call_count")) == (3, 1)


def test_soft_replace_archives_the_divergent_tail(store: StoreHandle, sid: str) -> None:
    ids = _three(store, sid)
    current = messages.get(store, sid)
    live = messages.replace(
        store, sid, [*current[:2], {"role": "user", "content": "rewritten"}], soft=True
    )
    assert live[:2] == ids[:2] and live[2] > ids[2]
    everything = messages.get(store, sid, include_inactive=True)
    assert [(r["content"], r["active"]) for r in everything] == [
        ("one", 1),
        ("two", 1),
        ("three", 0),
        ("rewritten", 1),
    ]
    assert column(store, sid, "message_count") == 3


def test_soft_replace_with_nothing_in_common(store: StoreHandle, sid: str) -> None:
    _three(store, sid)
    messages.replace(store, sid, [{"role": "user", "content": "fresh"}], soft=True)
    assert [r["content"] for r in messages.get(store, sid)] == ["fresh"]
    assert len(messages.get(store, sid, include_inactive=True)) == 4


def test_soft_replace_with_the_same_rows_changes_no_ids(store: StoreHandle, sid: str) -> None:
    ids = _three(store, sid)
    assert messages.replace(store, sid, messages.get(store, sid), soft=True) == ids


def test_pinned_replace_refuses_a_moved_live_set(store: StoreHandle, sid: str) -> None:
    ids = _three(store, sid)
    messages.append(store, sid, {"role": "assistant", "content": "arrived meanwhile"})
    with pytest.raises(TranscriptMoved) as caught:
        messages.replace(store, sid, [], expect_active_ids=ids)
    assert caught.value.code == "store.transcript_moved"
    assert len(messages.get(store, sid)) == 4
    current = [r["id"] for r in messages.get(store, sid)]
    assert messages.replace(store, sid, [], soft=True, expect_active_ids=current) == []


# -- clone ----------------------------------------------------------------------------------------


def test_clone_is_byte_exact_with_fresh_ids(store: StoreHandle, sid: str) -> None:
    seed(store, "target")
    ids = _three(store, sid)
    sql(store, "UPDATE messages SET active = 0, compacted = 1 WHERE id = ?", [ids[0]])

    new_ids = store.write(lambda c: messages.clone_rows_in(c, "target", [ids[2], ids[0], 999_999]))
    assert len(new_ids) == 2 and new_ids == sorted(new_ids) and new_ids[0] > ids[2]
    with store.read() as conn:
        src = conn.execute(
            "SELECT * FROM messages WHERE id IN (?, ?) ORDER BY id", (ids[0], ids[2])
        )
        dst = conn.execute("SELECT * FROM messages WHERE session_id = 'target' ORDER BY id")
        pairs = list(zip(src.fetchall(), dst.fetchall(), strict=True))
    skip = {"id", "session_id", "active", "compacted"}
    for old_row, new_row in pairs:
        old, new = dict(old_row), dict(new_row)
        assert {k: v for k, v in old.items() if k not in skip} == {
            k: v for k, v in new.items() if k not in skip
        }
        assert (new["active"], new["compacted"]) == (1, 0)
    assert column(store, "target", "message_count") == 2
    assert store.write(lambda c: messages.clone_rows_in(c, "target", [])) == []


def test_clone_copies_tool_call_counts(store: StoreHandle, sid: str) -> None:
    seed(store, "target")
    ids = _three(store, sid)
    store.write(lambda c: messages.clone_rows_in(c, "target", ids))
    assert column(store, "target", "tool_call_count") == 1


# -- size guards ----------------------------------------------------------------------------------


def _bulk(store: StoreHandle, sid: str, count: int, *, active: int = 1) -> None:
    sql(
        store,
        "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < ?) "
        "INSERT INTO messages (session_id, role, content, timestamp, active) "
        "SELECT ?, 'user', 'x', 1750000000.0, ? FROM n",
        [count, sid, active],
    )


def test_resume_guard_counts_past_the_limit(store: StoreHandle, sid: str) -> None:
    _bulk(store, sid, 20_001)
    with pytest.raises(ResumeTooLarge) as caught:
        messages.check_resume_size(store, sid)
    err = caught.value
    assert (err.numeric, err.count, err.code) == (4130, 20_001, "store.resume_too_large")
    assert "20001" in str(err)
    assert messages.check_resume_size(store, sid, limit=0) == 0
    assert messages.check_resume_size(store, sid, limit=20_001) == 20_001


def test_resume_guard_counts_live_rows_across_the_lineage(store: StoreHandle, sid: str) -> None:
    _bulk(store, sid, 6)
    _bulk(store, sid, 50, active=0)
    rotate(store, sid, "tip")
    _bulk(store, "tip", 5)
    assert messages.check_resume_size(store, "tip", limit=11) == 11
    with pytest.raises(ResumeTooLarge) as caught:
        messages.check_resume_size(store, "tip", limit=10)
    assert caught.value.count == 11
    assert messages.check_resume_size(store, sid, limit=10) == 6


def test_export_guard_counts_every_row_of_one_session(store: StoreHandle, sid: str) -> None:
    _bulk(store, sid, 3)
    _bulk(store, sid, 2, active=0)
    assert messages.check_export_size(store, sid, limit=5) == 5
    with pytest.raises(ResumeTooLarge) as caught:
        messages.check_export_size(store, sid, limit=4)
    assert (caught.value.numeric, caught.value.count) == (4131, 5)
    assert messages.check_export_size(store, sid, limit=0) == 0
    assert messages.check_export_size(store, sid) == 5


class _Config:
    def __init__(self, values: dict[str, object]) -> None:
        self.values = values

    def get(self, path: str, /) -> object:
        return self.values.get(path)


@pytest.mark.parametrize(
    ("values", "resume", "export"),
    [
        ({}, 20_000, 20_000),
        ({"sessions.resume_message_limit": 3}, 3, 3),
        ({"sessions.resume_message_limit": 0}, 0, 0),
        ({"sessions.resume_message_limit": -1}, 20_000, 20_000),
        ({"sessions.resume_message_limit": "9"}, 20_000, 20_000),
        ({"sessions.resume_message_limit": True}, 20_000, 20_000),
        ({"sessions.resume_message_limit": 3, "sessions.export_message_limit": 7}, 3, 7),
    ],
)
def test_limits_come_from_config(
    scope: Scope, values: dict[str, object], resume: int, export: int
) -> None:
    configured = scope.with_config(_Config(values))
    assert messages.resume_limit(configured) == resume
    assert messages.export_limit(configured) == export


def test_resume_guard_reads_the_scope_config(scope: Scope) -> None:
    handle = registry.open_unshared(
        scope.with_config(_Config({"sessions.resume_message_limit": 2}))
    )
    try:
        seed(handle, SID)
        _bulk(handle, SID, 3)
        with pytest.raises(ResumeTooLarge):
            messages.check_resume_size(handle, SID)
    finally:
        handle.close()


# -- replaced store -------------------------------------------------------------------------------


def test_replaced_store_diverts_pending_messages(
    scope: Scope, store: StoreHandle, sid: str
) -> None:
    other = registry.open_unshared(scope, scope.home / "restored" / "state.db")
    other.close()
    for suffix in ("-wal", "-shm"):
        side = Path(f"{store.path}{suffix}")
        if side.exists():
            os.rename(side, Path(f"{side}.live"))
    os.replace(other.path, store.path)

    with pytest.raises(StoreReplaced):
        messages.append(store, sid, {"role": "user", "content": "keep me"})
    with pytest.raises(StoreReplaced):
        messages.append_batch(store, sid, [{"role": "user", "content": "and me"}])
    lines = guards.divert_path(scope.home, sid).read_text(encoding="ascii").splitlines()
    assert [json.loads(line)["content"] for line in lines] == ["keep me", "and me"]


# -- generated text (seeded table; ``hypothesis`` is not installed) -------------------------------

_POOLS = (
    (0x20, 0x7E),
    (0x00, 0x1F),
    (0xA0, 0x24F),
    (0x4E00, 0x4FFF),
    (0x1F300, 0x1F5FF),
    (0xD800, 0xDFFF),
    (0x200B, 0x202E),
)


def _random_text(rng: random.Random) -> str:
    chars: list[str] = []
    for _ in range(rng.randint(0, 60)):
        low, high = rng.choice(_POOLS)
        chars.append(chr(rng.randint(low, high)))
    text = "".join(chars)
    if rng.random() < 0.1:
        text = PARTS_PREFIX + text
    return text


@pytest.mark.parametrize("seed_value", range(40))
def test_generated_text_round_trips_except_surrogates(
    store: StoreHandle, sid: str, seed_value: int
) -> None:
    rng = random.Random(seed_value)
    texts = [_random_text(rng) for _ in range(5)]
    messages.append_batch(store, sid, [{"role": "user", "content": t} for t in texts])
    back = [r["content"] for r in messages.get(store, sid)]
    assert back == [re.sub("[\ud800-\udfff]", "\ufffd", t) for t in texts]
