"""Structured answers from subagents: the output contract and its check (P10-04).

A task may carry ``output_schema``, a JSON Schema. ``check_schema`` meta-validates it before any
child starts (the draft comes from ``$schema``, else 2020-12); a bad schema fails the whole
``delegate_task`` call. ``contract_text`` is the block added to the child's brief: end with one
JSON value matching this schema.

``check_answer(text, schema)`` takes the last JSON object or array in the child's final text (a
fenced block counts the same as bare text), validates it and returns ``SchemaCheck``. On failure
the parent sends ``correction_text(errors)`` to the same child once — the errors, not the schema
again — and checks the new answer. Errors are bounded: at most ``MAX_ERRORS`` lines of at most
``MAX_ERROR_CHARS`` characters each. A failed check never discards the child's text.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from jsonschema.validators import (
    validator_for,  # pyright: ignore[reportUnknownVariableType]
)

__all__ = [
    "MAX_ERRORS",
    "MAX_ERROR_CHARS",
    "SCHEMA_MISS_NOTE",
    "OutputSchemaInvalid",
    "SchemaCheck",
    "check_answer",
    "check_schema",
    "contract_text",
    "correction_text",
    "last_json_value",
]

MAX_ERRORS = 10
MAX_ERROR_CHARS = 200
# Only the tail of a very long answer is searched for its closing JSON value.
SEARCH_TAIL_CHARS = 64_000

SCHEMA_MISS_NOTE = (
    "The subagent's answer still did not match output_schema after one correction; its text is "
    "returned as it was. Read it with care."
)
_NO_JSON = "no JSON object or array was found in the answer"


class OutputSchemaInvalid(ValueError):
    """``output_schema`` is not a usable JSON Schema; the message is for the model."""


@dataclass(frozen=True)
class SchemaCheck:
    valid: bool
    errors: tuple[str, ...] = ()
    value: object = None


def _validator_class(schema: Mapping[str, Any]) -> Any:
    return validator_for(schema, default=Draft202012Validator)  # pyright: ignore[reportUnknownVariableType]


def check_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """``schema`` as a plain dict when it is a valid JSON Schema; ``OutputSchemaInvalid`` if not."""
    plain = dict(schema)
    try:
        _validator_class(plain).check_schema(plain)
    except SchemaError as exc:
        raise OutputSchemaInvalid(
            f"output_schema is not a valid JSON Schema: {_clip(str(exc.message))}"
        ) from None
    except Exception as exc:  # an unknown $schema draft and the like
        raise OutputSchemaInvalid(f"output_schema cannot be used: {_clip(str(exc))}") from None
    return plain


def contract_text(schema: Mapping[str, Any]) -> str:
    """The block the child's brief ends with when its task has a schema."""
    try:
        shown = json.dumps(schema, indent=2, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        shown = str(schema)
    return (
        "Answer format (checked by a program): end your final reply with one JSON value that "
        "matches the JSON Schema below and nothing after it. Prose before it is fine but not "
        "needed. A reply that does not match gets one chance to fix it; after that it is passed "
        "on unchecked.\n" + shown
    )


def correction_text(errors: tuple[str, ...] | list[str]) -> str:
    """The one correction message a child gets after a failed check."""
    lines = "\n".join(f"- {e}" for e in errors) or f"- {_NO_JSON}"
    return (
        "Your final reply did not match the required answer format. The checker found:\n"
        f"{lines}\n\n"
        "Reply again with only the corrected JSON value for the schema in your task. Do not do "
        "the work again unless the errors say something is missing."
    )


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_ERROR_CHARS else text[: MAX_ERROR_CHARS - 1] + "…"


# -- finding the answer ---------------------------------------------------------------------------


def _fenced_blocks(text: str) -> Iterator[str]:
    """Bodies of ``` fenced blocks, in order (an unclosed last fence runs to the end)."""
    start = 0
    while True:
        opening = text.find("```", start)
        if opening < 0:
            return
        line_end = text.find("\n", opening)
        if line_end < 0:
            return
        closing = text.find("```", line_end + 1)
        body = text[line_end + 1 : closing if closing >= 0 else len(text)]
        yield body
        if closing < 0:
            return
        start = closing + 3


def _top_level_values(text: str) -> list[object]:
    """JSON objects and arrays in ``text`` that are not inside another one found, in order."""
    decoder = json.JSONDecoder()
    found: list[object] = []
    index = 0
    size = len(text)
    while index < size:
        char = text[index]
        if char not in "{[":
            index += 1
            continue
        try:
            value, end = decoder.raw_decode(text, index)
        except (ValueError, RecursionError):
            index += 1
            continue
        found.append(cast(object, value))
        index = end
    return found


def last_json_value(text: str) -> tuple[bool, object]:
    """``(True, value)`` for the last JSON object or array in ``text``; ``(False, None)`` if none.

    The last fenced block holding JSON wins; else the last top-level value in the bare text.
    """
    tail = text[-SEARCH_TAIL_CHARS:]
    for block in reversed(list(_fenced_blocks(tail))):
        values = _top_level_values(block)
        if values:
            return True, values[-1]
    values = _top_level_values(tail)
    if values:
        return True, values[-1]
    return False, None


def check_answer(text: str, schema: Mapping[str, Any]) -> SchemaCheck:
    """Validate the last JSON value of ``text`` against ``schema`` (assumed checked)."""
    found, value = last_json_value(text or "")
    if not found:
        return SchemaCheck(False, (_NO_JSON,))
    validator = _validator_class(schema)(dict(schema))
    found_errors: list[Any] = list(validator.iter_errors(value))
    problems = sorted(found_errors, key=lambda e: [str(p) for p in e.absolute_path])
    errors: list[str] = []
    for problem in problems[:MAX_ERRORS]:
        where = "".join(
            f"[{p}]" if isinstance(p, int) else f".{p}" for p in list(problem.absolute_path)
        )
        errors.append(_clip(f"${where}: {problem.message}"))
    return SchemaCheck(not errors, tuple(errors), value)
