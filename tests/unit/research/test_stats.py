"""P22-04: tool and reasoning statistics."""

from __future__ import annotations

import json
from typing import Any

import pytest

from indus.core import untrusted
from indus.research.stats import (
    Tally,
    normalise,
    reasoning_counts,
    tool_succeeded,
    tool_tallies,
    turn_has_reasoning,
)

JUDGEMENT: list[tuple[str, object, bool]] = [
    ("plain text", "file written", True),
    ("exit code is not a failure", json.dumps({"output": "boom", "exit_code": 1}), True),
    ("error prefix", "Error: x", False),
    ("error prefix any case", "  ERROR: disk full", False),
    ("error word inside text", "no error: all fine", True),
    ("empty text", "", False),
    ("blank text", "   ", False),
    ("non-object JSON", "[]", True),
    ("error field", json.dumps({"error": "not found"}), False),
    ("null error field", json.dumps({"error": None, "output": "ok"}), True),
    ("nested content error", json.dumps({"content": {"error": "denied"}}), False),
    ("success false", json.dumps({"success": False, "message": "nope"}), False),
]


@pytest.mark.parametrize(("case", "content", "ok"), JUDGEMENT, ids=[c[0] for c in JUDGEMENT])
def test_success_judgement(case: str, content: object, ok: bool) -> None:
    assert tool_succeeded(content) is ok


def test_parts_and_fenced_results_are_judged_on_their_text() -> None:
    assert tool_succeeded([{"type": "text", "text": "a chart"}, {"type": "image_url"}])
    fenced = untrusted.fence("web_extract", json.dumps({"error": "page refused the request"}))
    assert fenced != json.dumps({"error": "page refused the request"})  # really fenced
    assert tool_succeeded(fenced) is False
    long_ok = untrusted.fence("web_extract", "a long page body " * 5)
    assert tool_succeeded(long_ok) is True


def call(cid: str | None, name: str) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "function", "function": {"name": name, "arguments": "{}"}}
    if cid is not None:
        out["id"] = cid
    return out


MESSAGES: list[dict[str, Any]] = [
    {"role": "user", "content": "go"},
    {
        "role": "assistant",
        "content": None,
        "reasoning": "plan",
        "tool_calls": [call("a", "terminal"), call("b", "read_file")],
    },
    {"role": "tool", "tool_call_id": "b", "content": json.dumps({"error": "missing"})},
    {"role": "tool", "tool_call_id": "a", "content": "ok"},
    {"role": "tool", "tool_call_id": "zzz", "content": "stray"},
    {
        "role": "assistant",
        "content": "<think>hm</think> again",
        "tool_calls": [call(None, "terminal")],
    },
    {"role": "tool", "content": "Error: nope"},
    {"role": "assistant", "content": "done"},
]


def test_tallies_match_results_by_id_then_position() -> None:
    tallies = tool_tallies(MESSAGES)
    assert tallies == {"terminal": Tally(2, 1, 1), "read_file": Tally(1, 0, 1)}


def test_normalise_adds_zero_rows_and_keeps_unexpected_names() -> None:
    tallies = {"terminal": Tally(2, 2, 0), "teleport": Tally(1, 0, 1)}
    stats, errors = normalise(tallies, ["terminal", "read_file", "patch"])
    assert list(stats) == ["patch", "read_file", "terminal", "teleport"]
    assert stats["patch"] == {"count": 0, "success": 0, "failure": 0}
    assert stats["teleport"] == {"count": 1, "success": 0, "failure": 1}
    assert errors == {"patch": 0, "read_file": 0, "terminal": 0, "teleport": 1}


def test_rows_with_disjoint_tools_get_identical_columns() -> None:
    registered = ["terminal", "read_file", "web_search"]
    one, one_errors = normalise({"terminal": Tally(1, 1, 0)}, registered)
    two, two_errors = normalise({"web_search": Tally(3, 2, 1)}, registered)
    assert list(one) == list(two)
    assert list(one_errors) == list(two_errors)


def test_reasoning_counts() -> None:
    counts = reasoning_counts(MESSAGES)
    assert (counts.total, counts.with_reasoning, counts.without_reasoning) == (3, 2, 1)
    assert counts.any
    assert not turn_has_reasoning({"role": "assistant", "content": "x", "reasoning": "  \n"})
    assert not reasoning_counts([{"role": "assistant", "content": "plain"}]).any
