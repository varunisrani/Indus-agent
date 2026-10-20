"""P19-11: clarify questions as form elicitations, or as cards with Skip."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from acp.schema import CreateFormSessionElicitationRequest, RequestPermissionRequest

from indus.acp import elicitation
from indus.core.turn_state import TurnState
from indus.foundation.scope import Scope
from indus.protocol.requests_port import RequestNotAnswered
from indus.protocol.server_requests import ClarifyAnswer, ClarifyQuestion
from indus.state import messages
from indus.state.registry import StoreHandle
from indus.tools.calls import ToolCall, ToolResult
from tests.contract.acp.harness import KINDS, Harness, open_harness
from tests.fakes.model import text_turn, tool_batch
from tests.unit.backends._aio import aio

INIT = {"protocolVersion": 1, "clientInfo": {"name": "zed", "version": "1.0"}}
FORMS = {**INIT, "clientCapabilities": {"elicitation": {"form": {}}}}
PROMPT = [{"type": "text", "text": "set it up"}]
QUESTIONS: list[dict[str, Any]] = [
    {"id": "db", "text": "Which database?", "choices": ["sqlite", "postgres"]},
    {"id": "extras", "text": "Extras?", "choices": ["docs", "tests"], "multi_select": True},
]


async def ask(call: ToolCall, state: TurnState) -> ToolResult:
    broker = state.control.broker
    assert broker is not None
    try:
        answer = await broker.ask("clarify", {"questions": QUESTIONS}, turn_id=state.turn_id)
    except RequestNotAnswered as exc:
        return ToolResult(f"not answered: {exc}", is_error=True)
    assert isinstance(answer, ClarifyAnswer)
    return ToolResult(json.dumps(answer.answers, sort_keys=True))


def _answers(store: StoreHandle, sid: str) -> dict[str, list[str]]:
    rows = [m for m in messages.get(store, sid) if m["role"] == "tool"]
    return json.loads(str(rows[-1]["content"]))


async def _run(h: Harness, tmp_path: Path, init: Mapping[str, Any]) -> str:
    await h.call("initialize", dict(init))
    sid = str((await h.call("session/new", {"cwd": str(tmp_path), "mcpServers": []}))["sessionId"])
    result = await h.call("session/prompt", {"sessionId": sid, "prompt": PROMPT})
    assert result["stopReason"] == "end_turn"
    return sid


TURNS = (tool_batch(("ask", {})), text_turn("ok"))


@pytest.mark.parametrize("kind", KINDS)
@aio
async def test_form_elicitation_answers_single_and_multi_select(
    kind: str, scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    def form(params: Mapping[str, Any]) -> dict[str, Any]:
        schema = params["requestedSchema"]["properties"]["answer"]
        if schema["type"] == "array":
            return {"action": "accept", "content": {"answer": ["docs", "tests"]}}
        return {"action": "accept", "content": {"answer": "postgres"}}

    script = {"elicitation/create": form}
    async with open_harness(
        kind, scope, store, tmp_path, *TURNS, tools={"ask": ask}, script=script
    ) as h:
        sid = await _run(h, tmp_path, FORMS)
        assert _answers(store, sid) == {"db": ["postgres"], "extras": ["docs", "tests"]}
        asked = [r["params"] for r in h.pair.requests if r["method"] == "elicitation/create"]
        assert len(asked) == 2
        for params in asked:
            CreateFormSessionElicitationRequest.model_validate(params)
        assert asked[0]["requestedSchema"]["properties"]["answer"] == {
            "type": "string",
            "enum": ["sqlite", "postgres"],
        }
        assert asked[1]["requestedSchema"]["properties"]["answer"]["type"] == "array"
        assert asked[0]["sessionId"] == sid and asked[0]["mode"] == "form"


@aio
async def test_a_declined_form_is_a_skip(scope: Scope, store: StoreHandle, tmp_path: Path) -> None:
    script = {"elicitation/create": lambda _p: {"action": "decline"}}
    async with open_harness(
        "local", scope, store, tmp_path, *TURNS, tools={"ask": ask}, script=script
    ) as h:
        sid = await _run(h, tmp_path, FORMS)
        assert _answers(store, sid) == {"db": [], "extras": []}


@pytest.mark.parametrize("kind", KINDS)
@aio
async def test_without_forms_each_question_is_a_card_with_skip(
    kind: str, scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    def pick(params: Mapping[str, Any]) -> dict[str, Any]:
        if params["toolCall"]["title"] == "Which database?":
            return {"outcome": {"outcome": "selected", "optionId": "choice-0"}}
        return {"outcome": {"outcome": "cancelled"}}  # dismissed: a skip

    script = {"session/request_permission": pick}
    async with open_harness(
        kind, scope, store, tmp_path, *TURNS, tools={"ask": ask}, script=script
    ) as h:
        sid = await _run(h, tmp_path, INIT)
        assert _answers(store, sid) == {"db": ["sqlite"], "extras": []}
        cards = [
            r["params"] for r in h.pair.requests if r["method"] == "session/request_permission"
        ]
        first = RequestPermissionRequest.model_validate(cards[0])
        assert [(o.option_id, o.kind, o.name) for o in first.options] == [
            ("choice-0", "allow_once", "sqlite"),
            ("choice-1", "allow_once", "postgres"),
            ("skip", "reject_once", "Skip"),
        ]


def test_form_answers() -> None:
    assert elicitation.form_answer({"action": "accept", "content": {"answer": "x"}}) == ["x"]
    assert elicitation.form_answer({"action": "accept", "content": {"answer": ["a", "b"]}}) == [
        "a",
        "b",
    ]
    assert elicitation.form_answer({"action": "cancel"}) == []
    assert elicitation.form_answer({"action": "accept", "content": {"answer": 3}}) is None
    assert elicitation.form_answer("nonsense") is None


def test_free_text_questions_ask_for_a_string() -> None:
    params = elicitation.form_request("s1", ClarifyQuestion(id="q", text="Name?"))
    assert params["requestedSchema"]["properties"]["answer"] == {
        "type": "string",
        "title": "Answer",
    }
    _call, options, answers = elicitation.card(ClarifyQuestion(id="q", text="Name?"), "c1")
    assert [o["optionId"] for o in options] == ["skip"]
    assert (
        elicitation.card_answer({"outcome": {"outcome": "selected", "optionId": "nope"}}, answers)
        is None
    )
