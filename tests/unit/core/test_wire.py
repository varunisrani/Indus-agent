"""Wire copy and send-time sanitizer (P04-04)."""

from __future__ import annotations

import copy
import json
import random
from typing import Any

import pytest

from indus.core.repair import repair_transcript
from indus.core.transcript import DISPLAY_STEER, TranscriptRow, call_id
from indus.core.wire import (
    EMPTY_PLACEHOLDER,
    FALLBACK_TOOL_NAME,
    STUB_RESULT,
    ArgsMemo,
    HealLedger,
    WireRules,
    build_wire_copy,
    canonical_arguments,
    coerce_tool_name,
    prepare_wire,
    repair_arguments,
    rules_for,
    sanitize_wire,
)
from indus.providers.route import RuntimeRoute


def call(cid: str, name: str = "read_file", args: str = "{}") -> dict[str, Any]:
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": args}}


def m_user(text: Any) -> dict[str, Any]:
    return {"role": "user", "content": text}


def m_asst(text: Any = None, *calls: dict[str, Any], **kw: Any) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant", "content": text, **kw}
    if calls:
        msg["tool_calls"] = list(calls)
    return msg


def m_tool(cid: str | None, text: str = "ok", name: str | None = "read_file") -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "tool", "content": text, "tool_call_id": cid}
    if name is not None:
        msg["name"] = name
    return msg


def ids_of(msgs: list[dict[str, Any]]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for msg in msgs:
        if msg["role"] == "assistant":
            out.extend(("call", call_id(c)) for c in msg.get("tool_calls") or [])
        elif msg["role"] == "tool":
            out.append(("result", msg["tool_call_id"]))
    return out


def dump(msgs: list[dict[str, Any]]) -> bytes:
    return json.dumps(msgs, ensure_ascii=False, separators=(",", ":")).encode()


STRICT = WireRules()


# -- strict alternation validator ----------------------------------------------------------------


def assert_strict(msgs: list[dict[str, Any]], rules: WireRules = STRICT) -> None:
    seen_ids: set[str] = set()
    open_calls: set[str] | None = None
    for index, msg in enumerate(msgs):
        role = msg["role"]
        assert role in rules.roles
        if index and role != "tool":
            assert msgs[index - 1]["role"] != role, (index, msgs)
        if "tool_calls" in msg:
            assert msg["tool_calls"], msg
        if role in ("user", "assistant") and index < len(msgs) - 1:
            assert msg.get("content") or msg.get("tool_calls"), msg
        if role == "assistant":
            assert open_calls is None or not open_calls, ("unanswered", open_calls)
            open_calls = set()
            for c in msg.get("tool_calls") or []:
                cid = call_id(c)
                assert cid and cid not in seen_ids, cid
                assert coerce_tool_name(c["function"]["name"]) == c["function"]["name"]
                seen_ids.add(cid)
                open_calls.add(cid)
        elif role == "tool":
            assert open_calls is not None and msg["tool_call_id"] in open_calls, msg
            open_calls.discard(msg["tool_call_id"])
        else:
            assert open_calls is None or not open_calls, ("unanswered", open_calls)
            open_calls = None
    assert not open_calls


# -- wire copy -----------------------------------------------------------------------------------


def test_wire_copy_substitutes_sidecar_and_strips_storage_fields() -> None:
    rows = [
        TranscriptRow(role="user", content="hi  ", api_content="hi", message_uid="u1", row_id=3),
        TranscriptRow(
            role="assistant",
            content="shown",
            api_content="sent before",
            tool_calls=[call("c1")],
            reasoning="r",
            reasoning_details=[{"sig": "x"}],
            finish_reason="tool_calls",
            scaffold=False,
            display_kind="marker",
        ),
        TranscriptRow(
            role="tool",
            content="out",
            tool_call_id="c1",
            name="read_file",
            api_content="ignored for tools",
        ),
    ]
    wire = build_wire_copy(rows)
    assert wire[0] == {"role": "user", "content": "hi"}
    assert wire[1] == {
        "role": "assistant",
        "content": "sent before",
        "tool_calls": [call("c1")],
        "reasoning": "r",
        "reasoning_details": [{"sig": "x"}],
    }
    assert wire[2] == {"role": "tool", "content": "out", "tool_call_id": "c1", "name": "read_file"}


def test_empty_sidecar_is_ignored() -> None:
    wire = build_wire_copy([TranscriptRow(role="user", content="real", api_content="")])
    assert wire[0]["content"] == "real"


def test_durable_rows_are_never_mutated() -> None:
    rows = [
        TranscriptRow(role="user", content=[{"type": "text", "text": " a "}]),
        TranscriptRow(
            role="assistant", content="", tool_calls=[call("c1", "bad name!", '{"b":1, "a":2}')]
        ),
        TranscriptRow(role="tool", content=" r ", tool_call_id="c1", name="other"),
        TranscriptRow(role="assistant", content="  ", tool_calls=[call("c1")]),
        TranscriptRow(role="user", content="next"),
    ]
    before = copy.deepcopy(rows)
    identities = [id(r.tool_calls) for r in rows]
    out = prepare_wire(rows, STRICT)
    assert rows == before
    assert [id(r.tool_calls) for r in rows] == identities
    out[1]["tool_calls"][0]["function"]["name"] = "changed"
    assert rows == before


def test_sanitize_does_not_mutate_its_input() -> None:
    msgs = [m_user(""), m_asst(None, call("a"), call("a")), m_tool("a"), m_user(" q ")]
    before = copy.deepcopy(msgs)
    sanitize_wire(msgs, STRICT)
    assert msgs == before


def test_same_input_twice_gives_identical_bytes() -> None:
    rows = [
        TranscriptRow(role="user", content="go"),
        TranscriptRow(
            role="assistant", content=None, tool_calls=[call("c", args='{"z":1,"a":[2, 1]}')]
        ),
        TranscriptRow(role="tool", content="x", tool_call_id="c", name="read_file"),
        TranscriptRow(role="assistant", content=None, tool_calls=[call("c")]),
        TranscriptRow(role="tool", content="y", tool_call_id="c", name="read_file"),
        TranscriptRow(role="assistant", content="done"),
    ]
    assert dump(prepare_wire(rows)) == dump(prepare_wire(rows))
    assert dump(prepare_wire(rows, memo=ArgsMemo())) == dump(prepare_wire(rows))


# -- id renames ----------------------------------------------------------------------------------


def test_repeated_ids_in_one_row_get_d1_d2() -> None:
    msgs = [
        m_user("q"),
        m_asst(None, call("x"), call("x"), call("x")),
        m_tool("x", "1"),
        m_tool("x", "2"),
        m_tool("x", "3"),
        m_asst("done"),
    ]
    out = sanitize_wire(msgs, STRICT)
    assert ids_of(out) == [
        ("call", "x"),
        ("call", "x_d1"),
        ("call", "x_d2"),
        ("result", "x"),
        ("result", "x_d1"),
        ("result", "x_d2"),
    ]
    assert [m["content"] for m in out if m["role"] == "tool"] == ["1", "2", "3"]
    assert_strict(out)


def test_constant_id_across_turns_is_renamed_stably() -> None:
    turn = [m_asst(None, call("call_0")), m_tool("call_0")]
    msgs = [m_user("q"), *turn, *copy.deepcopy(turn), *copy.deepcopy(turn), m_asst("end")]
    out = sanitize_wire(msgs, STRICT)
    assert ids_of(out) == [
        ("call", "call_0"),
        ("result", "call_0"),
        ("call", "call_0_d1"),
        ("result", "call_0_d1"),
        ("call", "call_0_d2"),
        ("result", "call_0_d2"),
    ]
    assert_strict(out)


def test_appending_a_turn_keeps_the_prefix_bytes() -> None:
    base = [m_user("q"), m_asst(None, call("k")), m_tool("k"), m_asst(None, call("k")), m_tool("k")]
    longer = [*copy.deepcopy(base), m_asst(None, call("k")), m_tool("k"), m_asst("done")]
    short_out = sanitize_wire([*base, m_asst("x")], STRICT)
    long_out = sanitize_wire(longer, STRICT)
    assert dump(long_out[:5]) == dump(short_out[:5])


def test_suffix_collision_takes_the_next_free_number() -> None:
    msgs = [
        m_user("q"),
        m_asst(None, call("a"), call("a_d1"), call("a")),
        m_tool("a"),
        m_tool("a_d1"),
        m_tool("a"),
        m_asst("end"),
    ]
    out = sanitize_wire(msgs, STRICT)
    assert [c for k, c in ids_of(out) if k == "call"] == ["a", "a_d1", "a_d2"]
    assert_strict(out)


def test_call_without_id_gets_a_positional_id_and_a_stub() -> None:
    msgs = [
        m_user("q"),
        m_asst(None, {"type": "function", "function": {"name": "t", "arguments": "{}"}}),
        m_user("more"),
    ]
    out = sanitize_wire(msgs, STRICT)
    assert ids_of(out) == [("call", "call_1_0"), ("result", "call_1_0")]
    assert out[2]["content"] == STUB_RESULT
    assert_strict(out)


# -- the other steps -----------------------------------------------------------------------------


def test_roles_the_route_rejects_are_dropped() -> None:
    msgs = [
        {"role": "developer", "content": "d"},
        m_user("q"),
        {"role": "function", "content": "f"},
    ]
    anthropic = rules_for(RuntimeRoute("p", "m", "anthropic_messages", "https://x.invalid"))
    chat = rules_for(RuntimeRoute("p", "m", "chat_completions", "https://x.invalid"))
    assert [m["role"] for m in sanitize_wire(msgs, anthropic)] == ["user"]
    assert [m["role"] for m in sanitize_wire(msgs, chat)] == ["developer", "user"]


def test_profile_can_declare_its_own_rules() -> None:
    class Profile:
        wire_rules = WireRules(positional_pairing=False)

    class Route:
        api_mode = "chat_completions"
        profile = Profile()

    assert rules_for(Route()) is Profile.wire_rules
    assert rules_for(None) == WireRules()


def test_empty_non_final_messages_are_healed_and_counted() -> None:
    ledger = HealLedger()
    msgs = [m_user(""), m_asst(" "), m_user("q"), m_asst("")]
    out = sanitize_wire(msgs, STRICT, heals=ledger)
    assert [m["content"] for m in out] == [EMPTY_PLACEHOLDER, EMPTY_PLACEHOLDER, "q", ""]
    assert ledger.events == 1 and ledger.healed == 2


def test_heal_notice_is_asked_for_once() -> None:
    ledger = HealLedger()
    for _ in range(5):
        sanitize_wire([m_user(""), m_asst("x")], STRICT, heals=ledger)
    assert ledger.events == 5
    assert [ledger.take_notice() for _ in range(3)] == [True, False, False]
    assert not HealLedger(notice_after=0, events=9).take_notice()


def test_empty_tool_calls_arrays_are_removed() -> None:
    msgs = [
        m_user("q"),
        {"role": "assistant", "content": "a", "tool_calls": []},
        m_user("r"),
        {"role": "assistant", "content": "b", "tool_calls": "junk"},
    ]
    out = sanitize_wire(msgs, STRICT)
    assert all("tool_calls" not in m for m in out)


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("read_file", "read_file"),
        ("multi.tool use", "multi_tool_use"),
        ("rm -rf /; echo", "rm_-rf_echo"),
        ("!!!", FALLBACK_TOOL_NAME),
        ("", FALLBACK_TOOL_NAME),
        (None, FALLBACK_TOOL_NAME),
        ("x" * 80, "x" * 64),
    ],
)
def test_tool_names_are_coerced(raw: Any, want: str) -> None:
    assert coerce_tool_name(raw) == want


def test_invalid_names_in_history_are_coerced_and_results_follow() -> None:
    msgs = [
        m_user("q"),
        m_asst(None, call("c1", "ns.tool")),
        m_tool("c1", name="ns.tool"),
        m_asst("e"),
    ]
    out = sanitize_wire(msgs, STRICT)
    assert out[1]["tool_calls"][0]["function"]["name"] == "ns_tool"
    assert out[2]["name"] == "ns_tool"


def test_results_without_ids_are_dropped() -> None:
    msgs = [
        m_user("q"),
        m_asst(None, call("c1")),
        m_tool("c1"),
        m_tool(""),
        m_tool(None),
        m_asst("e"),
    ]
    out = sanitize_wire(msgs, STRICT)
    assert ids_of(out) == [("call", "c1"), ("result", "c1")]


def test_positional_pairing_drops_displaced_results_and_stubs_missing_ones() -> None:
    msgs = [
        m_tool("c1"),  # before its call
        m_user("q"),
        m_asst(None, call("c1"), call("c2")),
        m_tool("c2"),
        m_user("interrupt"),
        m_tool("c1"),  # displaced past a user row
        m_asst("done"),
    ]
    out = sanitize_wire(msgs, STRICT)
    assert ids_of(out) == [("call", "c1"), ("call", "c2"), ("result", "c2"), ("result", "c1")]
    stub = out[3]
    assert stub == {
        "role": "tool",
        "tool_call_id": "c1",
        "content": STUB_RESULT,
        "name": "read_file",
    }
    assert_strict(out)


def test_loose_routes_keep_known_results_in_place() -> None:
    loose = WireRules(positional_pairing=False)
    msgs = [m_user("q"), m_asst(None, call("c1")), m_user("x"), m_tool("c1"), m_tool("nope")]
    out = sanitize_wire(msgs, loose)
    assert [m["role"] for m in out] == ["user", "assistant", "user", "tool"]


def test_result_names_are_realigned_with_their_call() -> None:
    msgs = [
        m_user("q"),
        m_asst(None, call("c1", "tool_bridge")),
        m_tool("c1", name="inner_tool"),
        m_asst("e"),
    ]
    out = sanitize_wire(msgs, STRICT)
    assert out[2]["name"] == "tool_bridge"


def test_result_without_a_name_stays_without_one() -> None:
    msgs = [m_user("q"), m_asst(None, call("c1")), m_tool("c1", name=None), m_asst("e")]
    assert "name" not in sanitize_wire(msgs, STRICT)[2]


def test_thinking_only_rows_are_dropped_and_users_merged() -> None:
    msgs = [
        m_user("one"),
        m_asst(None, reasoning="hmm"),
        m_user("two"),
        m_asst("", reasoning_details=[{"sig": "s"}]),
    ]
    out = sanitize_wire(msgs, STRICT)
    assert out == [{"role": "user", "content": "one\n\ntwo"}]


def test_thinking_only_rows_stay_when_the_route_allows_them() -> None:
    rules = WireRules(drop_thinking_only=False)
    out = sanitize_wire([m_user("q"), m_asst(None, reasoning="hmm"), m_user("r")], rules)
    assert [m["role"] for m in out] == ["user", "assistant", "user"]


def test_steer_and_prompt_rows_merge_on_the_wire_only() -> None:
    rows = [
        TranscriptRow(role="user", content="q"),
        TranscriptRow(role="assistant", content=None, tool_calls=[call("c1")]),
        TranscriptRow(role="tool", content="r", tool_call_id="c1"),
        TranscriptRow(role="user", content="[steer] x", display_kind=DISPLAY_STEER),
        TranscriptRow(role="user", content="next"),
    ]
    out = prepare_wire(rows, STRICT)
    assert out[-1] == {"role": "user", "content": "[steer] x\n\nnext"}
    assert rows[3].content == "[steer] x"


def test_string_content_is_trimmed_and_surrogates_scrubbed() -> None:
    out = sanitize_wire([m_user("  hi \ud800 "), m_asst("ok")], STRICT)
    assert out[0]["content"] == "hi �"


# -- arguments -----------------------------------------------------------------------------------


def test_arguments_are_canonical() -> None:
    assert (
        canonical_arguments('{ "b": 1, "a": {"y": 2, "x": [3, 1]} }')
        == '{"a":{"x":[3,1],"y":2},"b":1}'
    )
    assert canonical_arguments('{"t": "ü"}') == '{"t":"ü"}'
    assert canonical_arguments(None) == "{}"
    assert canonical_arguments({"b": 1, "a": 2}) == '{"a":2,"b":1}'


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("", "{}"),
        ("None", "{}"),
        ('{"a": 1,}', '{"a":1}'),
        ('{"a": [1, 2', '{"a":[1,2]}'),
        ('{"code": "}"', '{"code":"}"}'),
        ('{"a": "line\nbreak"}', '{"a":"line\\nbreak"}'),
        ('{"a": "unterminated', "{}"),
        ("not json at all", "{}"),
    ],
)
def test_broken_arguments_are_repaired(raw: str, want: str) -> None:
    assert repair_arguments(raw) == want
    memo = ArgsMemo()
    assert canonical_arguments(raw, memo) == want
    assert raw not in memo  # repairs are not memoised


def test_memo_evicts_oldest_past_the_entry_cap() -> None:
    memo = ArgsMemo(max_entries=3)
    for i in range(5):
        memo.canonical(f'{{"n": {i}}}')
    assert len(memo) == 3
    assert '{"n": 0}' not in memo and '{"n": 4}' in memo


def test_memo_evicts_past_the_byte_cap() -> None:
    memo = ArgsMemo(max_entries=100, max_bytes=200)
    big = json.dumps({"v": "x" * 60})
    memo.canonical(big)
    memo.canonical(json.dumps({"w": "y" * 60}))
    assert len(memo) == 1 and memo.bytes <= 200 + 2 * len(big)
    memo.canonical(json.dumps({"z": "z" * 500}))  # a single oversized entry is still kept
    assert len(memo) == 1


def test_memo_hit_refreshes_the_entry() -> None:
    memo = ArgsMemo(max_entries=2)
    memo.canonical('{"a":1}')
    memo.canonical('{"b":1}')
    memo.canonical('{"a":1}')
    memo.canonical('{"c":1}')
    assert '{"a":1}' in memo and '{"b":1}' not in memo


def test_default_memo_caps_match_the_spec() -> None:
    memo = ArgsMemo()
    assert memo.max_entries == 4096 and memo.max_bytes == 32 * 1024 * 1024


# -- property (seeded-random; hypothesis is not installed) ----------------------------------------


def _random_messages(rng: random.Random) -> list[dict[str, Any]]:
    msgs: list[dict[str, Any]] = []
    if rng.random() < 0.5:
        msgs.append({"role": "system", "content": "sys"})
    ids = ["a", "b", "a_d1", "call_0", ""]
    for _ in range(rng.randint(0, 16)):
        kind = rng.choice(["user", "assistant", "assistant", "tool", "tool", "odd"])
        if kind == "user":
            msgs.append(m_user(rng.choice(["", "  ", "u", [{"type": "text", "text": "p"}]])))
        elif kind == "assistant":
            calls = [
                call(
                    rng.choice(ids),
                    rng.choice(["ok_name", "bad name", ""]),
                    rng.choice(['{"a":1}', "{broken", ""]),
                )
                for _ in range(rng.randint(0, 3))
            ]
            msg = m_asst(rng.choice([None, "", "a"]), *calls)
            if rng.random() < 0.3:
                msg["reasoning"] = "r"
            if rng.random() < 0.1:
                msg["tool_calls"] = []
            msgs.append(msg)
        elif kind == "tool":
            msgs.append(m_tool(rng.choice([*ids, None]), name=rng.choice([None, "x", "ok_name"])))
        else:
            msgs.append({"role": rng.choice(["function", "critic"]), "content": "?"})
    return msgs


def test_property_sanitized_output_is_strictly_alternating() -> None:
    rng = random.Random(4096)
    for _ in range(500):
        msgs = _random_messages(rng)
        before = copy.deepcopy(msgs)
        out = sanitize_wire(msgs, STRICT)
        assert msgs == before
        body = out[1:] if out and out[0]["role"] == "system" else out
        assert_strict(body)
        assert dump(sanitize_wire(msgs, STRICT)) == dump(out)


def test_property_repair_then_wire_is_strictly_alternating() -> None:
    rng = random.Random(77)
    for _ in range(300):
        rows = [
            TranscriptRow.from_message(m) for m in _random_messages(rng) if m["role"] != "system"
        ]
        repaired, _ = repair_transcript(rows)
        assert_strict(prepare_wire(repaired, STRICT))
