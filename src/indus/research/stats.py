"""Tool and reasoning statistics of one run, from its raw messages (P22-04).

``tool_tallies`` counts, per tool name, the calls and how many results looked like a success or a
failure. The judgement (``tool_succeeded``) is deliberately narrow: a result fails only when it is
a JSON object with a non-null ``error``, a nested ``content`` object with a non-null ``error``, or
``success: false``; text that is not JSON fails only when empty or when it starts with ``error:``
(any case). A non-zero exit code is not a failure: the model is expected to read it and recover.
A result fenced as outside data is judged on what is inside the fence.

``normalise`` lists every tool of the run's registry (zeros when unused) plus any other name the
model used, so every row of a dataset has the same columns (R10.5); the merge drops rows that name
a tool outside the registry.

``turn_has_reasoning``: an assistant message with a non-blank native ``reasoning`` field or a
``<think>`` tag in its text.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from indus.core import loop_texts
from indus.research.trajectory import call_name, content_text, result_names

__all__ = [
    "Reasoning",
    "Tally",
    "error_counts",
    "merge_tallies",
    "normalise",
    "reasoning_counts",
    "tally_dict",
    "tool_succeeded",
    "tool_tallies",
    "turn_has_reasoning",
]

THINK_TAG = "<think>"
_FENCE_OPEN = loop_texts.FENCE_OPEN.format(word=loop_texts.FENCE_WORD, source="")[:-2]
_FENCE_CLOSE = loop_texts.FENCE_CLOSE.format(word=loop_texts.FENCE_WORD)


@dataclass
class Tally:
    count: int = 0
    success: int = 0
    failure: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"count": self.count, "success": self.success, "failure": self.failure}

    def add(self, other: Tally) -> None:
        self.count += other.count
        self.success += other.success
        self.failure += other.failure


@dataclass
class Reasoning:
    total: int = 0
    with_reasoning: int = 0
    without_reasoning: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "total_assistant_turns": self.total,
            "turns_with_reasoning": self.with_reasoning,
            "turns_without_reasoning": self.without_reasoning,
        }

    def add(self, other: Reasoning) -> None:
        self.total += other.total
        self.with_reasoning += other.with_reasoning
        self.without_reasoning += other.without_reasoning

    @property
    def any(self) -> bool:
        return self.with_reasoning > 0


def _unfenced(text: str) -> str:
    """The body of an outside-data fence, or ``text`` itself."""
    stripped = text.strip()
    if not (stripped.startswith(_FENCE_OPEN) and stripped.endswith(_FENCE_CLOSE)):
        return text
    body = stripped[: -len(_FENCE_CLOSE)].rstrip("\n")
    head, _, rest = body.partition("\n")
    if not head.endswith(">"):
        return text
    preamble = loop_texts.FENCE_PREAMBLE
    if rest.startswith(preamble):
        rest = rest[len(preamble) :]
    return rest.lstrip("\n")


def tool_succeeded(content: object) -> bool:
    """See the module notes."""
    if isinstance(content, Mapping):
        value: Any = content
    else:
        text = _unfenced(content_text(content))
        try:
            value = json.loads(text)
        except ValueError:
            return bool(text.strip()) and not text.strip().lower().startswith("error:")
    if not isinstance(value, dict):
        return True
    body = cast(dict[str, Any], value)
    if body.get("error") is not None:
        return False
    inner = body.get("content")
    if isinstance(inner, dict) and cast(dict[str, Any], inner).get("error") is not None:
        return False
    return body.get("success") is not False


def _calls(message: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = message.get("tool_calls")
    if not isinstance(raw, list):
        return []
    return [cast(Mapping[str, Any], c) for c in cast(list[Any], raw) if isinstance(c, Mapping)]


def tool_tallies(messages: Sequence[Mapping[str, Any]]) -> dict[str, Tally]:
    """Calls and success/failure per tool name (results matched as the converter does)."""
    tallies: dict[str, Tally] = {}
    calls: list[Mapping[str, Any]] = []
    position = 0
    while position < len(messages):
        message = messages[position]
        role = message.get("role")
        if role == "assistant":
            calls = _calls(message)
            for call in calls:
                tallies.setdefault(call_name(call), Tally()).count += 1
        elif role == "tool":
            run: list[Mapping[str, Any]] = []
            while position < len(messages) and messages[position].get("role") == "tool":
                run.append(messages[position])
                position += 1
            for result, name in zip(run, result_names(calls, run), strict=True):
                tally = tallies.get(name)
                if tally is None:
                    continue  # a result no call asked for
                if tool_succeeded(result.get("content")):
                    tally.success += 1
                else:
                    tally.failure += 1
            calls = []
            continue
        position += 1
    return tallies


def tally_dict(tallies: Mapping[str, Tally]) -> dict[str, dict[str, int]]:
    return {name: tally.as_dict() for name, tally in tallies.items()}


def merge_tallies(total: dict[str, Tally], more: Mapping[str, Mapping[str, int]]) -> None:
    for name, values in more.items():
        tally = total.setdefault(name, Tally())
        tally.add(
            Tally(
                int(values.get("count", 0)),
                int(values.get("success", 0)),
                int(values.get("failure", 0)),
            )
        )


def error_counts(tallies: Mapping[str, Tally]) -> dict[str, int]:
    return {name: tally.failure for name, tally in tallies.items()}


def normalise(
    tallies: Mapping[str, Tally], registered: Iterable[str]
) -> tuple[dict[str, dict[str, int]], dict[str, int]]:
    """``(tool_stats, tool_error_counts)`` with a row for every registered tool (R10.5)."""
    names = sorted(set(registered))
    extra = sorted(n for n in tallies if n not in set(names))
    stats: dict[str, dict[str, int]] = {}
    errors: dict[str, int] = {}
    for name in [*names, *extra]:
        tally = tallies.get(name, Tally())
        stats[name] = tally.as_dict()
        errors[name] = tally.failure
    return stats, errors


def turn_has_reasoning(message: Mapping[str, Any]) -> bool:
    reasoning = message.get("reasoning")
    if isinstance(reasoning, str) and reasoning.strip():
        return True
    return THINK_TAG in content_text(message.get("content"))


def reasoning_counts(messages: Sequence[Mapping[str, Any]]) -> Reasoning:
    out = Reasoning()
    for message in messages:
        if message.get("role") != "assistant":
            continue
        out.total += 1
        if turn_has_reasoning(message):
            out.with_reasoning += 1
        else:
            out.without_reasoning += 1
    return out
