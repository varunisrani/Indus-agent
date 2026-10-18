"""The clarify tool over a fake broker port (P05-24)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.scope import Scope, make_scope
from indus.protocol.envelope import ErrorObject
from indus.protocol.events import CancelReason
from indus.protocol.requests_port import AnswerModel, RequestUnsupported, RequestWithdrawn
from indus.protocol.server_requests import ClarifyAnswer, ClarifyRequest, SudoAnswer
from indus.tools.builtin import clarify as clarify_module
from indus.tools.builtin.clarify import (
    CLARIFY_SCHEMA,
    MAX_CHOICE_CHARS,
    QuestionError,
    clarify,
    normalize_questions,
    strip_recommended,
)
from indus.tools.calls import ToolCall, ToolContext
from indus.tools.dispatch import dispatch
from indus.tools.registry import ToolRegistry
from tests.fakes.agent import DictConfig


class FakePort:
    """Answers a clarify ask with a scripted reply (or raises), recording what was asked."""

    def __init__(self, reply: Mapping[str, list[str]] | BaseException | AnswerModel) -> None:
        self.reply = reply
        self.asked: list[tuple[str, dict[str, Any], float | None, str | None]] = []

    async def ask(
        self,
        kind: str,
        payload: Mapping[str, Any],
        *,
        timeout: float | None = None,
        turn_id: str | None = None,
    ) -> AnswerModel:
        self.asked.append((kind, dict(payload), timeout, turn_id))
        # the request the broker would build must be a valid protocol model
        ClarifyRequest(request_id="r1", session_id="s1", **payload)
        if isinstance(self.reply, BaseException):
            raise self.reply
        if isinstance(self.reply, Mapping):
            return ClarifyAnswer(answers=dict(self.reply))
        return self.reply

    def cancel_all(self, reason: CancelReason) -> int:
        return 0

    def human_wait_seconds(self) -> float:
        return 0.0


def ask(port: FakePort | None, questions: object, scope: Scope | None = None) -> dict[str, Any]:
    text = asyncio.run(clarify({"questions": questions}, broker=port, scope=scope, turn_id="t1"))
    return json.loads(text)


def q(text: str, choices: list[str] | None = None, multi: bool | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"question": text}
    if choices is not None:
        out["choices"] = choices
    if multi is not None:
        out["multi_select"] = multi
    return out


def test_answered_and_skipped_in_question_order() -> None:
    port = FakePort({"q0": ["Postgres (Recommended)"], "q1": [], "q2": ["fast", "cheap"]})
    body = ask(
        port,
        [
            q("Which database?", ["Postgres", "SQLite"]),
            q("Anything else?"),
            q("What matters?", ["fast", "cheap", "safe"], multi=True),
        ],
    )
    assert body["outcome"] == "submitted"
    assert body["responses"] == [
        {
            "question": "Which database?",
            "choices_offered": ["Postgres", "SQLite"],
            "status": "answered",
            "user_response": "Postgres",
        },
        {
            "question": "Anything else?",
            "choices_offered": None,
            "status": "skipped",
            "user_response": None,
        },
        {
            "question": "What matters?",
            "choices_offered": ["fast", "cheap", "safe"],
            "status": "answered",
            "user_response": ["fast", "cheap"],
        },
    ]
    kind, payload, timeout, turn = port.asked[0]
    assert (kind, timeout, turn) == ("clarify", 3600.0, "t1")
    shown = payload["questions"]
    assert [x["id"] for x in shown] == ["q0", "q1", "q2"]
    assert shown[0]["choices"] == ["Postgres (recommended)", "SQLite"]
    assert shown[1]["choices"] is None
    assert shown[2]["multi_select"] is True


def test_recommended_marker_rules() -> None:
    (one,) = normalize_questions([q("Sure?", ["yes"])])
    assert one.shown_choices() == ["yes"]
    (already,) = normalize_questions([q("Pick", ["A (RECOMMENDED)", "B"])])
    assert already.shown_choices() == ["A (RECOMMENDED)", "B"]
    assert strip_recommended("  B (recommended) ") == "B"
    assert strip_recommended("B (Recommended)") == "B"
    assert strip_recommended("recommended") == "recommended"
    port = FakePort({"q0": ["A (recommended)", "B"]})
    body = ask(port, [q("Pick", ["A", "B", "C"], multi=True)])
    assert body["responses"][0]["user_response"] == ["A", "B"]


def test_free_text_answer_to_a_choice_question() -> None:
    body = ask(FakePort({"q0": ["  my own idea  "]}), [q("Pick", ["A", "B"])])
    assert body["responses"][0] == {
        "question": "Pick",
        "choices_offered": ["A", "B"],
        "status": "answered",
        "user_response": "my own idea",
    }


def test_blank_answers_count_as_skipped() -> None:
    body = ask(FakePort({"q0": ["   "]}), [q("Name?")])
    assert body["responses"][0]["status"] == "skipped"


@pytest.mark.parametrize(
    ("reason", "outcome"),
    [
        (CancelReason.TIMED_OUT, "timed_out"),
        (CancelReason.INTERRUPTED, "cancelled"),
        (CancelReason.TURN_ENDED, "cancelled"),
        (CancelReason.SESSION_CLOSED, "cancelled"),
        (CancelReason.ANSWERED_ELSEWHERE, "cancelled"),
    ],
)
def test_withdrawn_questions_are_unanswered(reason: CancelReason, outcome: str) -> None:
    body = ask(FakePort(RequestWithdrawn("r1", reason)), [q("A?"), q("B?", ["x", "y"])])
    assert body["outcome"] == outcome
    assert [r["status"] for r in body["responses"]] == ["unanswered", "unanswered"]
    assert all(r["user_response"] is None for r in body["responses"])
    assert body["notice"]


def test_undelivered_when_nobody_can_answer() -> None:
    error = ErrorObject(code=4002, message="this client cannot answer clarify requests")
    body = ask(FakePort(RequestUnsupported("clarify", error)), [q("A?")])
    assert body["outcome"] == "undelivered" and body["responses"][0]["status"] == "unanswered"
    body = ask(None, [q("A?")])
    assert body["outcome"] == "undelivered" and "notice" in body


def test_wrong_answer_kind_is_an_error() -> None:
    body = ask(FakePort(SudoAnswer(password=None)), [q("A?")])
    assert "wrong kind" in body["error"]


def test_timeout_comes_from_config(tmp_path: Path) -> None:
    home = tmp_path / "h"
    home.mkdir()
    for value, expected in [(90, 90.0), ("45", 45.0), (0, None), (-1, None), ("soon", 3600.0)]:
        scope = make_scope(home).with_config(DictConfig({"agent": {"clarify_timeout": value}}))
        port = FakePort({"q0": ["ok"]})
        ask(port, [q("A?")], scope)
        assert port.asked[0][2] == expected, value


@pytest.mark.parametrize(
    ("questions", "message"),
    [
        (None, "non-empty list"),
        ([], "non-empty list"),
        ("not json", "must be a list"),
        ([q(f"Q{n}?") for n in range(6)], "at most 5"),
        (["plain text"], "must be an object"),
        ([q("   ")], "is empty"),
        ([q("Pick", ["a", 3])], "list of texts"),  # type: ignore[list-item]
        ([{"question": "Pick", "choices": "a, b"}], "list of texts"),
        ([q("Pick", ["  ", ""])], "all blank"),
        ([q("Pick", ["x" * (MAX_CHOICE_CHARS + 1), "b"])], "8,000"),
    ],
)
def test_bad_questions_are_refused(questions: object, message: str) -> None:
    port = FakePort({})
    body = ask(port, questions)
    assert message in body["error"]
    assert port.asked == []


def test_question_shapes_are_normalised() -> None:
    long_ok = "line\n" * 1500  # 7,500 characters, under the limit
    questions = normalize_questions(
        json.dumps(
            [
                q("  Pick  ", [" a ", "", "b", "c", "d", "e"]),
                q("Multi without choices", None, multi=True),
                q("Empty choices", []),
                q("Long", [long_ok, "short"]),
            ]
        )
    )
    first, second, third, fourth = questions
    assert first.text == "Pick" and first.choices == ("a", "b", "c", "d")
    assert second.choices is None and second.multi_select is False
    assert third.choices is None
    assert fourth.choices is not None and fourth.choices[0] == long_ok.strip()
    (single,) = normalize_questions({"question": "One?"})
    assert single.id == "q0"
    with pytest.raises(QuestionError):
        normalize_questions([q("A")] * 6)


def test_registered_and_dispatched_with_the_broker(tmp_path: Path) -> None:
    registry = ToolRegistry()
    clarify_module.register_tools(registry)
    entry = registry.get("clarify")
    assert entry is not None and entry.traits.interactive and entry.is_async
    port = FakePort({"q0": ["yes"]})
    ctx = ToolContext(turn_id="t9", extras={"broker": port})
    call = ToolCall("clarify", {"questions": [q("Go?", ["yes", "no"])]}, "c1")
    result = asyncio.run(dispatch(call, ctx, registry=registry))
    assert not result.is_error
    assert json.loads(str(result.content))["responses"][0]["user_response"] == "yes"
    assert port.asked[0][3] == "t9"


def test_schema_leaves_choice_length_to_the_handler() -> None:
    item = CLARIFY_SCHEMA["parameters"]["properties"]["questions"]["items"]
    assert "maxLength" not in json.dumps(item)
    assert CLARIFY_SCHEMA["parameters"]["properties"]["questions"]["maxItems"] == 5
