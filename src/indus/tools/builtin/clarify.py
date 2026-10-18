# pyright: strict
"""``clarify``: ask the person one to five questions in one go and wait for the answers.

The questions travel as one ``clarify`` server request through the session's broker port
(``indus.protocol.requests_port``); the tool round offers the broker to handlers as ``broker``.
Each question may offer up to ``MAX_CHOICES`` choices (the first of two or more is shown as the
recommended one) and may allow several picks (``multi_select``); without choices it is open.

The result lists one response per question, in the order asked: ``answered`` (with the person's
answer, the recommendation marker removed), ``skipped`` (the person left it empty) or
``unanswered`` (the wait ended first), and the ``outcome`` of the wait: ``submitted``,
``cancelled`` (the turn was interrupted or ended, or someone else answered), ``timed_out``
(``agent.clarify_timeout`` seconds passed) or ``undelivered`` (no surface here can show it).

Checks done here rather than in the schema (some local model servers reject long string limits
in a grammar): each choice is at most ``MAX_CHOICE_CHARS`` characters; blank choices are dropped
and a question whose choices are all blank is refused.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from indus.foundation.config.schema import default_for
from indus.foundation.scope import Scope
from indus.protocol.events import CancelReason
from indus.protocol.requests_port import RequestUnsupported, RequestWithdrawn, ServerRequestPort
from indus.protocol.server_requests import ClarifyAnswer
from indus.tools.calls import ToolResult
from indus.tools.registry import ToolRegistry, ToolTraits

__all__ = [
    "CLARIFY_SCHEMA",
    "MAX_CHOICES",
    "MAX_CHOICE_CHARS",
    "MAX_QUESTIONS",
    "RECOMMENDED",
    "Question",
    "clarify",
    "normalize_questions",
    "register_tools",
    "strip_recommended",
]

log = logging.getLogger(__name__)

TOOL_NAME = "clarify"
TOOLSET = "clarify"
MAX_QUESTIONS = 5
MAX_CHOICES = 4
MAX_CHOICE_CHARS = 8_000
RECOMMENDED = "(recommended)"
TIMEOUT_KEY = "agent.clarify_timeout"
_FORM = (
    "Send questions as a list: [{question, choices?, multi_select?}] (one question is a list "
    "of one)."
)


@dataclass(frozen=True)
class Question:
    id: str
    text: str
    choices: tuple[str, ...] | None  # as the model sent them (trimmed, at most four)
    multi_select: bool

    def shown_choices(self) -> list[str] | None:
        """The choices as the person sees them: the first of two or more marked recommended."""
        if not self.choices:
            return None
        shown = list(self.choices)
        if len(shown) > 1 and strip_recommended(shown[0]) == shown[0]:
            shown[0] = f"{shown[0]} {RECOMMENDED}"
        return shown


def strip_recommended(text: str) -> str:
    """``text`` without a trailing recommendation marker (any letter case)."""
    clean = text.strip()
    if clean.casefold().endswith(RECOMMENDED.casefold()):
        return clean[: -len(RECOMMENDED)].rstrip()
    return clean


class QuestionError(ValueError):
    pass


def normalize_questions(raw: object) -> list[Question]:
    """The questions as sent, checked and trimmed; ``QuestionError`` says what to fix."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raise QuestionError(f"questions must be a list. {_FORM}") from None
    if isinstance(raw, Mapping):
        raw = [cast(object, raw)]
    if not isinstance(raw, list) or not raw:
        raise QuestionError(f"questions must be a non-empty list. {_FORM}")
    items = cast(list[object], raw)
    if len(items) > MAX_QUESTIONS:
        raise QuestionError(f"at most {MAX_QUESTIONS} questions fit in one call; got {len(items)}.")
    questions: list[Question] = []
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise QuestionError(f"questions[{index}] must be an object. {_FORM}")
        entry = cast(Mapping[str, Any], item)
        text = str(entry.get("question") or "").strip()
        if not text:
            raise QuestionError(f"questions[{index}].question is empty.")
        choices = _choices(entry.get("choices"), index)
        multi = bool(entry.get("multi_select")) and choices is not None
        questions.append(Question(f"q{index}", text, choices, multi))
    return questions


def _choices(raw: object, index: int) -> tuple[str, ...] | None:
    if raw is None:
        return None
    if not isinstance(raw, list) or not all(isinstance(c, str) for c in cast(list[object], raw)):
        raise QuestionError(f"questions[{index}].choices must be a list of texts.")
    given = cast(list[str], raw)
    for position, choice in enumerate(given):
        size = len(choice.strip())
        if size > MAX_CHOICE_CHARS:
            raise QuestionError(
                f"questions[{index}].choices[{position}] has {size:,} characters; the limit is "
                f"{MAX_CHOICE_CHARS:,}. Put the detail in the question and send it again."
            )
    kept = [c.strip() for c in given if c.strip()]
    if given and not kept:
        raise QuestionError(
            f"questions[{index}].choices are all blank; send real choices, or leave choices out "
            "for an open question."
        )
    return tuple(kept[:MAX_CHOICES]) or None


def _timeout(scope: Scope | None) -> float | None:
    value: object = None
    if scope is not None and scope.config is not None:
        try:
            value = scope.config.get(TIMEOUT_KEY)
        except Exception:
            log.debug("%s unreadable; using the default", TIMEOUT_KEY, exc_info=True)
    if value is None:
        value = default_for(TIMEOUT_KEY)
    try:
        seconds = float(cast(Any, value))
    except (TypeError, ValueError):
        seconds = float(default_for(TIMEOUT_KEY, 3600))
    return seconds if seconds > 0 else None


def _response(question: Question, values: list[str] | None) -> dict[str, Any]:
    status, answer = "unanswered", None
    if values is not None:
        cleaned = [strip_recommended(v) for v in values if v.strip()]
        if cleaned:
            status = "answered"
            answer = cleaned if question.multi_select else cleaned[0]
        else:
            status = "skipped"
    return {
        "question": question.text,
        "choices_offered": list(question.choices) if question.choices else None,
        "status": status,
        "user_response": answer,
    }


def _result(
    questions: list[Question],
    answers: Mapping[str, list[str]] | None,
    outcome: str,
    notice: str | None = None,
) -> str:
    body: dict[str, Any] = {
        "responses": [
            _response(q, None if answers is None else answers.get(q.id, [])) for q in questions
        ],
        "outcome": outcome,
    }
    if notice:
        body["notice"] = notice
    return json.dumps(body, ensure_ascii=False)


_CANCELLED_NOTICE = {
    CancelReason.INTERRUPTED: "the turn was interrupted before an answer came",
    CancelReason.TURN_ENDED: "the turn ended before an answer came",
    CancelReason.SESSION_CLOSED: "the session closed before an answer came",
    CancelReason.ANSWERED_ELSEWHERE: "the question was settled on another screen",
}


async def clarify(
    args: Mapping[str, Any],
    *,
    broker: ServerRequestPort | None = None,
    scope: Scope | None = None,
    turn_id: str | None = None,
) -> str:
    """The ``clarify`` handler (see the module notes)."""
    try:
        questions = normalize_questions(args.get("questions"))
    except QuestionError as exc:
        return ToolResult.error(str(exc)).text
    if broker is None:
        return _result(questions, None, "undelivered", "no one can be asked from here")
    payload = {
        "questions": [
            {
                "id": q.id,
                "text": q.text,
                "choices": q.shown_choices(),
                "multi_select": q.multi_select,
            }
            for q in questions
        ]
    }
    try:
        answer = await broker.ask(TOOL_NAME, payload, timeout=_timeout(scope), turn_id=turn_id)
    except RequestUnsupported:
        return _result(questions, None, "undelivered", "the connected client cannot show questions")
    except RequestWithdrawn as exc:
        if exc.reason is CancelReason.TIMED_OUT:
            return _result(questions, None, "timed_out", "no answer came before the time limit")
        return _result(questions, None, "cancelled", _CANCELLED_NOTICE.get(exc.reason))
    if not isinstance(answer, ClarifyAnswer):
        return ToolResult.error(
            f"the answer to the questions had the wrong kind ({answer.kind})"
        ).text
    return _result(questions, answer.answers, "submitted")


CLARIFY_SCHEMA: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": (
        "Ask the user something when you need a decision, a missing detail or feedback before "
        f"going on. Put every question in questions (1 to {MAX_QUESTIONS}); questions that do "
        "not depend on each other belong in one call, while a question whose wording depends on "
        "another answer needs its own call. Each question can offer up to "
        f"{MAX_CHOICES} choices (put the one you recommend first; it is marked for the user, who "
        "can also type their own answer), allow several picks with multi_select, or be open "
        "(no choices). Give choices only in choices, never listed inside the question text. "
        "The result has one response per question, in order, each answered, skipped or "
        "unanswered, and an outcome: submitted, cancelled, timed_out or undelivered. Decide "
        "small things yourself; risky commands are confirmed by the terminal tool, not here."
    ),
    "parameters": {
        "type": "object",
        "required": ["questions"],
        "properties": {
            "questions": {
                "description": "The questions, in the order to show them.",
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["question"],
                    "properties": {
                        "question": {"description": "The question itself.", "type": "string"},
                        "choices": {
                            "description": "Choices to pick from, recommended one first; leave "
                            "out for an open question.",
                            "type": "array",
                            "items": {"type": "string"},
                            "maxItems": MAX_CHOICES,
                        },
                        "multi_select": {
                            "description": "Allow picking more than one choice.",
                            "type": "boolean",
                        },
                    },
                },
                "minItems": 1,
                "maxItems": MAX_QUESTIONS,
            },
        },
    },
}


def register_tools(registry: ToolRegistry) -> None:
    registry.register(
        TOOL_NAME, TOOLSET, CLARIFY_SCHEMA, clarify, traits=ToolTraits(interactive=True)
    )
