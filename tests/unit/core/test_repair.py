"""Transcript rows and live-history alternation repair (P04-03)."""

from __future__ import annotations

import copy
import random
from collections import Counter
from typing import Any

from indus.core.repair import PASS_NAMES, repair_transcript
from indus.core.transcript import DISPLAY_STEER, TranscriptRow, call_id
from indus.state.messages import PARTS_PREFIX, encode_content


def call(cid: str, name: str = "read_file", args: str = "{}") -> dict[str, Any]:
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": args}}


def user(text: Any, **kw: Any) -> TranscriptRow:
    return TranscriptRow(role="user", content=text, **kw)


def asst(text: Any = None, calls: list[str] | None = None, **kw: Any) -> TranscriptRow:
    return TranscriptRow(
        role="assistant",
        content=text,
        tool_calls=[call(c) for c in calls] if calls else None,
        **kw,
    )


def tool(cid: str | None, text: str = "ok") -> TranscriptRow:
    return TranscriptRow(role="tool", content=text, tool_call_id=cid, name="read_file")


def shape(rows: list[TranscriptRow]) -> list[tuple[Any, ...]]:
    out: list[tuple[Any, ...]] = []
    for row in rows:
        if row.role == "tool":
            out.append(("tool", row.tool_call_id))
        elif row.role == "assistant":
            out.append(("assistant", row.content, tuple(row.call_ids())))
        else:
            out.append((row.role, row.content))
    return out


# -- the row type --------------------------------------------------------------------------------


def test_row_round_trips_message_and_store_shapes() -> None:
    msg = {
        "role": "tool",
        "content": "result",
        "tool_call_id": "c1",
        "tool_name": "read_file",
        "id": 7,
        "message_uid": "u" * 32,
        "timestamp": 1_700_000_000.0,
        "platform_message_id": "p1",
    }
    row = TranscriptRow.from_store(msg)
    assert (
        row.name == "read_file" and row.row_id == 7 and row.extra == {"platform_message_id": "p1"}
    )
    assert row.to_message() == {
        "role": "tool",
        "content": "result",
        "tool_call_id": "c1",
        "name": "read_file",
    }
    stored = row.to_store()
    assert stored["tool_name"] == "read_file" and "name" not in stored
    assert stored["message_uid"] == "u" * 32 and "display_kind" not in stored


def test_row_keeps_reasoning_details_in_arrival_order() -> None:
    details = [{"type": "thinking", "n": i} for i in range(3)]
    row = TranscriptRow.from_message(
        {"role": "assistant", "content": "x", "reasoning_details": details}
    )
    assert [d["n"] for d in row.reasoning_details or []] == [0, 1, 2]
    details[0]["n"] = 99  # the row holds its own copy
    assert (row.reasoning_details or [])[0]["n"] == 0


def test_has_payload_counts_text_parts_calls_and_reasoning() -> None:
    assert not asst("  ").has_payload()
    assert not asst([{"type": "text", "text": " "}]).has_payload()
    assert asst([{"type": "image_url", "image_url": {"url": "x"}}]).has_payload()
    assert asst(None, ["c1"]).has_payload()
    assert asst(None, reasoning="hmm").has_payload()


# -- pass tables ---------------------------------------------------------------------------------


def test_empty_history_needs_no_work() -> None:
    rows, report = repair_transcript([])
    assert rows == [] and report.total == 0 and report.sources == ()


def test_clean_history_is_returned_unchanged() -> None:
    rows = [user("hi"), asst(None, ["c1"]), tool("c1"), asst("done"), user("thanks")]
    out, report = repair_transcript(rows)
    assert report.total == 0
    assert all(a is b for a, b in zip(out, rows, strict=True))


def test_consecutive_assistants_merge_calls_and_text() -> None:
    first = asst("Looking.", ["c1"], api_content="Looking.", reasoning="r1")
    second = asst("Also this.", ["c2"], reasoning="r2")
    second.reasoning_details = [{"sig": "b"}]
    first.reasoning_details = [{"sig": "a"}]
    rows = [user("go"), first, second, tool("c1"), tool("c2")]
    out, report = repair_transcript(rows)
    assert report.counts["merged_assistants"] == 1
    merged = out[1]
    assert merged.content == "Looking.\nAlso this."
    assert merged.call_ids() == ["c1", "c2"]
    assert merged.reasoning == "r1"
    assert merged.reasoning_details == [{"sig": "a"}, {"sig": "b"}]
    assert merged.api_content is None  # stale sidecar dropped with the changed content
    assert shape(out)[2:] == [("tool", "c1"), ("tool", "c2")]


def test_assistant_merge_keeps_sidecar_when_content_unchanged() -> None:
    first = asst("Same.", api_content="Same.")
    out, _ = repair_transcript([user("q"), first, asst("")])
    assert out[1].content == "Same." and out[1].api_content == "Same."


def test_assistant_merge_takes_later_reasoning_only_when_missing() -> None:
    out, _ = repair_transcript([user("q"), asst("a"), asst("b", reasoning="later")])
    assert out[1].reasoning == "later"


def test_assistant_part_lists_are_concatenated() -> None:
    parts = [{"type": "text", "text": "see"}]
    out, _ = repair_transcript([user("q"), asst("one"), asst(parts)])
    assert out[1].content == [{"type": "text", "text": "one"}, {"type": "text", "text": "see"}]


def test_stray_results_are_dropped() -> None:
    rows = [
        tool("c0"),  # before any assistant
        user("q"),
        asst(None, ["c1"]),
        tool("c1"),
        tool("c1"),  # second answer to the same call
        tool("zz"),  # unknown id
        tool(None),  # no id
        user("next"),
        tool("c1"),  # after a user row
        asst("end"),
    ]
    out, report = repair_transcript(rows)
    assert report.counts["dropped_results"] == 5
    assert shape(out) == [
        ("user", "q"),
        ("assistant", None, ("c1",)),
        ("tool", "c1"),
        ("user", "next"),
        ("assistant", "end", ()),
    ]
    assert report.dropped == (0, 4, 5, 6, 8)


def test_two_calls_sharing_an_id_keep_two_results() -> None:
    rows = [user("q"), asst(None, ["dup", "dup"]), tool("dup"), tool("dup"), tool("dup")]
    out, _ = repair_transcript(rows)
    assert shape(out) == [
        ("user", "q"),
        ("assistant", None, ("dup", "dup")),
        ("tool", "dup"),
        ("tool", "dup"),
    ]


def test_unanswered_calls_are_pruned() -> None:
    rows = [user("q"), asst("doing", ["c1", "c2"]), tool("c2"), asst("done")]
    out, report = repair_transcript(rows)
    assert report.counts["pruned_calls"] == 1
    assert shape(out)[1] == ("assistant", "doing", ("c2",))


def test_result_after_a_user_row_does_not_answer() -> None:
    rows = [user("q"), asst("doing", ["c1"]), user("hurry"), tool("c1"), asst("x")]
    out, _ = repair_transcript(rows)
    assert shape(out) == [
        ("user", "q"),
        ("assistant", "doing", ()),
        ("user", "hurry"),
        ("assistant", "x", ()),
    ]


def test_payload_less_row_left_without_calls_is_dropped() -> None:
    rows = [user("q"), asst(None, ["c1"]), user("again")]
    out, report = repair_transcript(rows)
    assert shape(out) == [("user", "q\n\nagain")]
    assert report.counts["pruned_calls"] == 1 and report.counts["merged_users"] == 1


def test_pruned_row_keeps_uids_of_kept_calls_only() -> None:
    row = asst("x", ["c1", "c2"], tool_call_uids={"c1": "u1", "c2": "u2"})
    out, _ = repair_transcript([user("q"), row, tool("c1"), asst("y")])
    assert out[1].tool_call_uids == {"c1": "u1"}


def test_user_rows_merge_without_losing_text() -> None:
    out, report = repair_transcript([user("one"), user(""), user("two"), asst("ok")])
    assert shape(out) == [("user", "one\n\ntwo"), ("assistant", "ok", ())]
    assert report.counts["merged_users"] == 2


def test_user_string_and_parts_merge_into_parts() -> None:
    image = {"type": "image_url", "image_url": {"url": "data:x"}}
    out, _ = repair_transcript([user("look"), user([image])])
    assert out[0].content == [{"type": "text", "text": "look"}, image]


def test_nothing_is_folded_into_a_steer_row() -> None:
    steer = user("[steer] use the other file", display_kind=DISPLAY_STEER)
    rows = [user("q"), asst(None, ["c1"]), tool("c1"), steer, user("new prompt")]
    out, report = repair_transcript(rows)
    assert report.counts["merged_users"] == 0
    assert out[3].is_steer and out[3].content == "[steer] use the other file"
    assert out[4].content == "new prompt"


def test_a_steer_row_is_not_folded_into_the_row_before_it() -> None:
    out, _ = repair_transcript([user("q"), user("[steer] x", display_kind=DISPLAY_STEER)])
    assert len(out) == 2 and out[1].is_steer


def test_scaffold_and_durable_user_rows_stay_apart() -> None:
    out, _ = repair_transcript([user("real"), user("[nudge]", scaffold=True)])
    assert len(out) == 2 and out[1].scaffold and not out[0].scaffold


def test_stored_parts_are_decoded_before_merging() -> None:
    parts = [{"type": "text", "text": "a"}, {"type": "image_url", "image_url": {"url": "u"}}]
    encoded = encode_content(parts)
    assert isinstance(encoded, str) and encoded.startswith(PARTS_PREFIX)
    out, report = repair_transcript([user(encoded), user("b")])
    assert report.counts["decoded"] == 1
    assert out[0].content == [*parts, {"type": "text", "text": "b"}]


def test_damaged_parts_payload_is_never_merged() -> None:
    broken = PARTS_PREFIX + "[{not json"
    out, _ = repair_transcript([user(broken), user("b")])
    assert [r.content for r in out] == [broken, "b"]


def test_passes_repeat_until_nothing_changes() -> None:
    # The orphan result between the two assistant rows only goes in pass 2, after pass 1 ran.
    rows = [user("q"), asst("first"), tool("ghost"), asst("second")]
    out, report = repair_transcript(rows)
    assert shape(out) == [("user", "q"), ("assistant", "first\nsecond", ())]
    assert report.counts["dropped_results"] == 1 and report.counts["merged_assistants"] == 1


def test_input_rows_are_never_mutated() -> None:
    rows = [user("a"), user("b"), asst("x", ["c1"]), asst("y"), tool("c1")]
    before = copy.deepcopy(rows)
    out, _ = repair_transcript(rows)
    assert rows == before
    assert out[0] is not rows[0] and out[1] is not rows[2]


def test_cursor_counts_flushed_survivors() -> None:
    rows = [user("q"), asst("a"), asst("b"), user("c"), user("d"), asst("e")]
    out, report = repair_transcript(rows, flushed=4)
    # stored: q, a, b, c → output q, a+b (rewritten), c+d (first source stored, rewritten)
    assert [s for s in report.sources] == [(0,), (1, 2), (3, 4), (5,)]
    assert report.flushed == 3
    assert report.rewritten == (1, 2)
    assert len(out) == 4


def test_cursor_after_drops_ahead_of_it() -> None:
    rows = [tool("orphan"), user("q"), asst("a"), user("r")]
    _, report = repair_transcript(rows, flushed=2)
    assert report.flushed == 1 and report.rewritten == ()


def test_cursor_is_none_without_one() -> None:
    _, report = repair_transcript([user("a"), user("b")])
    assert report.flushed is None and report.rewritten == ()


def test_report_counts_name_every_pass() -> None:
    _, report = repair_transcript([user("a")])
    assert set(PASS_NAMES) <= set(report.counts) and "decoded" in report.counts


# -- property (seeded-random; hypothesis is not installed) ----------------------------------------


def _random_rows(rng: random.Random) -> list[TranscriptRow]:
    rows: list[TranscriptRow] = []
    ids = ["c1", "c2", "c3", "c4", ""]
    for _ in range(rng.randint(0, 14)):
        kind = rng.choice(["user", "user", "assistant", "assistant", "tool", "tool", "steer"])
        if kind == "user":
            content: Any = rng.choice(
                ["", f"u{rng.randint(0, 99)}", [{"type": "text", "text": "p"}]]
            )
            rows.append(user(content))
        elif kind == "steer":
            rows.append(user(f"steer{rng.randint(0, 9)}", display_kind=DISPLAY_STEER))
        elif kind == "assistant":
            calls = [rng.choice(ids) for _ in range(rng.randint(0, 3))]
            text = rng.choice([None, "", f"a{rng.randint(0, 99)}"])
            rows.append(
                TranscriptRow(
                    role="assistant",
                    content=text,
                    tool_calls=[call(c) for c in calls] or None,
                    reasoning=rng.choice([None, "r"]),
                )
            )
        else:
            rows.append(tool(rng.choice([*ids, None])))
    return rows


def _check_alternation(rows: list[TranscriptRow]) -> None:
    for prev, cur in zip(rows, rows[1:], strict=False):
        if prev.role == cur.role:
            assert prev.role in ("tool", "user"), (prev, cur)
            if prev.role == "user":
                assert prev.is_steer or cur.is_steer, (prev, cur)
    open_calls: Counter[str] | None = None
    for index, row in enumerate(rows):
        if row.role == "assistant":
            if row.tool_calls:
                answers: Counter[str] = Counter()
                for follower in rows[index + 1 :]:
                    if follower.role != "tool":
                        break
                    answers[follower.tool_call_id or ""] += 1
                assert answers == Counter(call_id(c) for c in row.tool_calls), row
            open_calls = Counter(row.call_ids())
        elif row.role == "tool":
            assert open_calls is not None and open_calls[row.tool_call_id or ""] > 0, row
            open_calls[row.tool_call_id or ""] -= 1
        else:
            open_calls = None


def _user_texts(rows: list[TranscriptRow]) -> list[str]:
    texts: list[str] = []
    for row in rows:
        if row.role != "user":
            continue
        if isinstance(row.content, str) and row.content:
            texts.append(row.content)
        elif isinstance(row.content, list):
            texts.extend(p["text"] for p in row.content if isinstance(p, dict) and p.get("text"))
    return texts


def test_property_random_histories_come_out_alternating() -> None:
    rng = random.Random(20261018)
    for _ in range(400):
        rows = _random_rows(rng)
        before = copy.deepcopy(rows)
        out, report = repair_transcript(rows)
        assert rows == before
        _check_alternation(out)
        joined = "\x1f".join(_user_texts(out))
        for text in _user_texts(rows):
            assert text in joined
        again, second = repair_transcript(out)
        assert second.total == 0 and shape(again) == shape(out)
        assert len(report.sources) == len(out)
