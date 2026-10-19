"""P22-03: the shared trajectory converter (goldens, rules, the format doc)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from indus.research.trajectory import (
    IMAGE_PLACEHOLDER,
    UNKNOWN_TOOL,
    convert,
    result_names,
    system_value,
    tool_list,
)

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "tests" / "fixtures" / "research" / "trajectories"
GOLDEN = ROOT / "tests" / "golden" / "research" / "trajectories"
DOC = ROOT / "docs" / "user" / "reference" / "trajectory-format.md"
CASES = sorted(p.stem for p in FIXTURES.glob("*.json"))


def load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def golden(name: str) -> dict[str, Any]:
    return json.loads((GOLDEN / f"{name}.json").read_text(encoding="utf-8"))


def turns_of(name: str) -> list[dict[str, str]]:
    case = load(name)
    return convert(case["messages"], prompt=case["prompt"], tools=case["tools"]).turns


@pytest.mark.parametrize("name", CASES)
def test_golden_conversion(name: str) -> None:
    case = load(name)
    got = convert(case["messages"], prompt=case["prompt"], tools=case["tools"])
    want = golden(name)
    assert got.turns == want["turns"]
    assert got.bad_arguments == want["bad_arguments"]


def test_there_is_a_golden_for_every_fixture() -> None:
    assert sorted(p.stem for p in GOLDEN.glob("*.json")) == CASES
    assert len(CASES) >= 5


@pytest.mark.parametrize("name", CASES)
def test_shape_rules_hold_for_every_case(name: str) -> None:
    turns = turns_of(name)
    assert [t["from"] for t in turns[:2]] == ["system", "human"]
    assert turns[1]["value"] == load(name)["prompt"]
    for turn in turns:
        assert set(turn) == {"from", "value"}
        assert turn["from"] in {"system", "human", "gpt", "tool"}
        assert turn["value"] == turn["value"].rstrip()
        if turn["from"] == "gpt":
            assert re.match(r"\A<think>\n(?:.*\n)?</think>(\n|\Z)", turn["value"], re.DOTALL)
    text = json.dumps(turns)
    assert "stored system prompt" not in text
    assert "base64" not in text


def test_reordered_responses_get_their_names_by_id() -> None:
    tool_turn = turns_of("reordered_by_id")[3]["value"]
    blocks = re.findall(r"<tool_response>\n(.*?)\n</tool_response>", tool_turn, re.DOTALL)
    assert [(b["tool_call_id"], b["name"]) for b in map(json.loads, blocks)] == [
        ("call_size", "terminal"),
        ("call_read", "read_file"),
    ]


def test_missing_ids_fall_back_to_position_and_extras_are_unknown() -> None:
    calls = [
        {"function": {"name": "a", "arguments": "{}"}},
        {"function": {"name": "b", "arguments": "{}"}},
    ]
    assert result_names(calls, [{}, {}, {}]) == ["a", "b", UNKNOWN_TOOL]
    with_ids = [{"id": "1", **calls[0]}, {"id": "2", **calls[1]}]
    results = [{"tool_call_id": "2"}, {"tool_call_id": "9"}, {}]
    assert result_names(with_ids, results) == ["b", UNKNOWN_TOOL, UNKNOWN_TOOL]


def test_image_results_keep_only_text() -> None:
    tool_turn = turns_of("image_result")[3]["value"]
    assert IMAGE_PLACEHOLDER in tool_turn
    assert "photo.jpg: a cat on a sofa" in tool_turn
    assert "iVBOR" not in tool_turn and "QUJD" not in tool_turn


def test_unparseable_arguments_become_empty_and_are_counted() -> None:
    case = load("think_and_bad_arguments")
    result = convert(case["messages"], prompt=case["prompt"], tools=case["tools"])
    assert result.bad_arguments == 2
    assert '{"name":"terminal","arguments":{}}' in result.turns[2]["value"]


def test_prefill_rows_before_the_prompt_never_appear() -> None:
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "assistant", "content": "PREFILL EXAMPLE"},
        {"role": "user", "content": "the prompt"},
        {"role": "assistant", "content": "answer", "reasoning": "why"},
    ]
    turns = convert(messages, prompt="the prompt").turns
    assert "PREFILL" not in json.dumps(turns)
    assert turns[-1] == {"from": "gpt", "value": "<think>\nwhy\n</think>\nanswer"}


def test_system_turn_lists_the_tools_compactly() -> None:
    tools = load("reordered_by_id")["tools"]
    value = system_value(tools)
    listed = json.loads(value.split("<tools>\n", 1)[1].split("\n</tools>", 1)[0])
    assert listed == tool_list(tools)
    assert [t["name"] for t in listed] == ["read_file", "terminal"]
    bare = [{"name": "x", "description": "d", "parameters": {}}]
    assert tool_list(bare) == bare


def test_format_doc_example_matches_real_output() -> None:
    """ACC-O3-2 style: the documented example is what the converter produces."""
    text = DOC.read_text(encoding="utf-8")
    block = text.split("<!-- example: reordered_by_id -->", 1)[1]
    body = block.split("```json\n", 1)[1].split("\n```", 1)[0]
    documented = json.loads(body)
    assert documented["conversations"][1:] == turns_of("reordered_by_id")[1:]
    assert documented["conversations"][0]["from"] == "system"
