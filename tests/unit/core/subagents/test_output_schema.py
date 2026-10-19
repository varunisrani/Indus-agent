"""Output schemas for subagent answers (P10-04)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from indus.core.subagents.output_schema import (
    MAX_ERROR_CHARS,
    MAX_ERRORS,
    SCHEMA_MISS_NOTE,
    OutputSchemaInvalid,
    check_answer,
    check_schema,
    contract_text,
    correction_text,
    last_json_value,
)
from indus.foundation.scope import Scope
from tests.fakes.agent import run_driven
from tests.fakes.clock import FakeClock
from tests.fakes.model import text_turn, tool_batch
from tests.fakes.subagents import RoutedModel, parent_agent

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"files": {"type": "integer"}, "names": {"type": "array"}},
    "required": ["files", "names"],
}


# -- pieces ---------------------------------------------------------------------------------------


def test_schema_meta_check() -> None:
    assert check_schema(SCHEMA) == SCHEMA
    with pytest.raises(OutputSchemaInvalid, match="not a valid JSON Schema"):
        check_schema({"type": "no-such-type"})
    with pytest.raises(OutputSchemaInvalid):
        check_schema({"properties": {"a": {"minimum": "ten"}}})


def test_last_json_value_prefers_the_last_and_reads_fences() -> None:
    assert last_json_value('first {"a": 1} then {"b": 2}') == (True, {"b": 2})
    fenced = 'Here you go:\n```json\n{"files": 3, "names": ["a"]}\n```\nThat is all {not json}.'
    assert last_json_value(fenced) == (True, {"files": 3, "names": ["a"]})
    assert last_json_value("an array [1, 2] at the end") == (True, [1, 2])
    nested = 'outer {"a": {"b": [1, {"c": 2}]}} done'
    assert last_json_value(nested) == (True, {"a": {"b": [1, {"c": 2}]}})
    assert last_json_value("no json at all") == (False, None)
    assert last_json_value("[" * 5_000) == (False, None)


def test_check_answer_errors_are_bounded() -> None:
    good = check_answer('Done.\n{"files": 2, "names": ["x", "y"]}', SCHEMA)
    assert good.valid and good.value == {"files": 2, "names": ["x", "y"]}
    bad = check_answer('{"files": "two"}', SCHEMA)
    assert not bad.valid
    assert any("$.files" in e for e in bad.errors)
    assert any("names" in e for e in bad.errors)
    many: dict[str, Any] = {
        "type": "object",
        "properties": {f"k{i}": {"type": "integer"} for i in range(30)},
    }
    wrong = json.dumps({f"k{i}": "x" * 500 for i in range(30)})
    errors = check_answer(wrong, many).errors
    assert len(errors) == MAX_ERRORS
    assert all(len(e) <= MAX_ERROR_CHARS for e in errors)
    assert check_answer("nothing", SCHEMA).errors == (
        "no JSON object or array was found in the answer",
    )


def test_contract_and_correction_texts() -> None:
    contract = contract_text(SCHEMA)
    assert '"required"' in contract and "JSON" in contract
    correction = correction_text(("$.files: 'two' is not of type 'integer'",))
    assert "$.files" in correction
    assert '"required"' not in correction  # the schema is not repeated


# -- through a real child -------------------------------------------------------------------------


def _delegate(schema: Any) -> Any:
    return tool_batch(
        ("delegate_task", {"tasks": [{"goal": "count files", "output_schema": schema}]})
    )


def _result(scope: Scope, clock: FakeClock, model: RoutedModel) -> dict[str, Any]:
    async def main() -> Any:
        parent = await parent_agent(scope, model)
        result = await parent.run_turn("count them")
        await parent.close()
        return result

    result = asyncio.run(run_driven(main(), clock))
    tool = next(r for r in result.rows if r.role == "tool")
    return json.loads(tool.content)


def test_valid_first_try(scope: Scope, fake_clock: FakeClock) -> None:
    model = RoutedModel(
        fake_clock,
        parent=[_delegate(SCHEMA), text_turn("ok")],
        children={"count files": [text_turn('{"files": 3, "names": ["a", "b", "c"]}')]},
    )
    entry = _result(scope, fake_clock, model)["results"][0]
    assert entry["status"] == "completed" and entry["schema_valid"] is True
    assert "errors" not in entry
    brief = model.child("count files").model_requests[0].body["messages"]
    first_user = next(m for m in brief if m["role"] == "user")["content"]
    assert "Answer format" in first_user and '"required"' in first_user


def test_invalid_then_corrected_after_one_retry(scope: Scope, fake_clock: FakeClock) -> None:
    model = RoutedModel(
        fake_clock,
        parent=[_delegate(SCHEMA), text_turn("ok")],
        children={
            "count files": [
                text_turn("There are three files: a, b and c."),
                text_turn('```json\n{"files": 3, "names": ["a", "b", "c"]}\n```'),
            ]
        },
    )
    entry = _result(scope, fake_clock, model)["results"][0]
    assert entry["schema_valid"] is True and entry["status"] == "completed"
    assert '"files": 3' in entry["summary"]
    requests = model.child("count files").model_requests
    assert len(requests) == 2
    last_user = [m for m in requests[1].body["messages"] if m["role"] == "user"][-1]
    assert "did not match the required answer format" in last_user["content"]


def test_invalid_twice_keeps_the_raw_text(scope: Scope, fake_clock: FakeClock) -> None:
    model = RoutedModel(
        fake_clock,
        parent=[_delegate(SCHEMA), text_turn("ok")],
        children={
            "count files": [
                text_turn("three files"),
                text_turn('{"files": "three"}'),
            ]
        },
    )
    entry = _result(scope, fake_clock, model)["results"][0]
    assert entry["status"] == "completed"
    assert entry["schema_valid"] is False
    assert entry["summary"] == '{"files": "three"}'
    assert 0 < len(entry["errors"]) <= MAX_ERRORS
    assert SCHEMA_MISS_NOTE in entry["note"]
    assert model.child("count files").turns_left == 0  # exactly one correction round


def test_malformed_schema_is_rejected_before_any_child_starts(
    scope: Scope, fake_clock: FakeClock
) -> None:
    model = RoutedModel(
        fake_clock,
        parent=[_delegate({"type": 12}), text_turn("I will do it myself")],
        children={"count files": [text_turn("never asked")]},
    )
    body = _result(scope, fake_clock, model)
    assert "task 0" in body["error"] and "not a valid JSON Schema" in body["error"]
    assert model.child("count files").model_requests == []
