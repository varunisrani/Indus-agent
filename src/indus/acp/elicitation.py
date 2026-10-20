"""A clarify question in an editor: a form elicitation, or a permission card (P19-11).

Each question goes out as ``elicitation/create`` (form mode, one property ``answer``: free text,
one of the offered choices, or for a multi-select an array of them) when the editor said at
``initialize`` that it renders forms (``clientCapabilities.elicitation.form``). Editors without
forms see the question as a permission card instead: every choice is an ``allow_once`` button and
a ``reject_once`` Skip ends the list.

Answers are lists of strings (our ``ClarifyAnswer``): the picked choice, the picks of a
multi-select, the typed text; an empty list is a skip. ``None`` means "no answer" (the editor went
away): the request stays open for another viewer.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final, cast

from indus.protocol.server_requests import ClarifyQuestion

__all__ = [
    "ELICITATION_METHOD",
    "SKIP_OPTION",
    "card",
    "card_answer",
    "form_answer",
    "form_request",
]

ELICITATION_METHOD: Final = "elicitation/create"
SKIP_OPTION: Final = "skip"


def form_request(editor_id: str, question: ClarifyQuestion) -> dict[str, Any]:
    """``elicitation/create`` params for one question."""
    schema: dict[str, Any] = {"type": "string", "title": "Answer"}
    if question.choices:
        options = list(question.choices)
        if question.multi_select:
            schema = {"type": "array", "items": {"type": "string", "enum": options}}
        else:
            schema = {"type": "string", "enum": options}
    return {
        "sessionId": editor_id,
        "mode": "form",
        "message": question.text,
        "requestedSchema": {
            "type": "object",
            "properties": {"answer": schema},
            "required": ["answer"],
        },
    }


def form_answer(response: object) -> list[str] | None:
    """The answer of an elicitation response: accept → the value(s); decline / cancel → skip
    (``[]``); anything else → None."""
    if not isinstance(response, Mapping):
        return None
    body = cast(Mapping[str, Any], response)
    action = body.get("action")
    if action in ("decline", "cancel"):
        return []
    if action != "accept":
        return None
    content = body.get("content")
    value = cast(Mapping[str, Any], content).get("answer") if isinstance(content, Mapping) else None
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list) and all(isinstance(v, str) for v in cast(list[object], value)):
        return [str(v) for v in cast(list[object], value)]
    return None


def card(
    question: ClarifyQuestion, card_id: str
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, list[str]]]:
    """``(tool call, options, option id → answer)`` of the fallback card for one question."""
    choices = list(question.choices or [])
    options = [
        {"optionId": f"choice-{index}", "name": choice, "kind": "allow_once"}
        for index, choice in enumerate(choices)
    ]
    options.append({"optionId": SKIP_OPTION, "name": "Skip", "kind": "reject_once"})
    answers = {f"choice-{index}": [choice] for index, choice in enumerate(choices)}
    answers[SKIP_OPTION] = []
    tool_call: dict[str, Any] = {
        "toolCallId": card_id,
        "title": question.text,
        "kind": "other",
        "status": "pending",
        "content": [{"type": "content", "content": {"type": "text", "text": question.text}}],
        "rawInput": {"question": question.text, "choices": choices},
    }
    return tool_call, options, answers


def card_answer(response: object, answers: Mapping[str, list[str]]) -> list[str] | None:
    """The answer a card response picked: a choice, or skip for Skip and for a dismissed card;
    an unknown option id is no answer."""
    if not isinstance(response, Mapping):
        return None
    outcome = cast(Mapping[str, Any], response).get("outcome")
    if not isinstance(outcome, Mapping):
        return None
    picked = cast(Mapping[str, Any], outcome)
    if picked.get("outcome") == "cancelled":
        return []
    if picked.get("outcome") != "selected":
        return None
    found = answers.get(str(picked.get("optionId")))
    return list(found) if found is not None else None
