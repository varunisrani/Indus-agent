"""A scripted ``PromptDriver``: answers come from a list instead of a keyboard.

Answers: a string for a text or secret line, an int (or a row label) for a choice, a list of ints
(or labels) for a checklist, ``BACK`` for the left arrow and ``CANCEL`` for Ctrl+C. Every
question asked is recorded in ``asked`` (replayed answers never reach the driver).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from indus.cli.prompts import BackPressed, Cancelled, Interactive

BACK = object()
CANCEL = object()


class ScriptExhausted(AssertionError):
    """The flow asked more questions than the script answers."""


@dataclass
class ScriptedDriver:
    answers: list[Any]
    asked: list[str] = field(default_factory=list[str])
    secrets_asked: list[str] = field(default_factory=list[str])

    def _next(self, question: str) -> Any:
        self.asked.append(question)
        if not self.answers:
            raise ScriptExhausted(f"no scripted answer for {question!r}")
        answer = self.answers.pop(0)
        if answer is BACK:
            raise BackPressed()
        if answer is CANCEL:
            raise Cancelled()
        return answer

    def text(self, question: str, *, secret: bool, allow_back: bool) -> str:
        if secret:
            self.secrets_asked.append(question)
        answer = self._next(question)
        if not isinstance(answer, str):
            raise AssertionError(f"{question!r} wants text, the script has {answer!r}")
        return answer

    def choose(self, question: str, rows: Sequence[str], default: int, *, allow_back: bool) -> int:
        answer = self._next(question)
        if isinstance(answer, str):
            matches = [i for i, row in enumerate(rows) if answer in row]
            if not matches:
                raise AssertionError(f"no row of {question!r} contains {answer!r}: {list(rows)}")
            return matches[0]
        if not isinstance(answer, int) or not 0 <= answer < len(rows):
            raise AssertionError(f"{question!r} has {len(rows)} rows; the script has {answer!r}")
        return answer

    def checklist(
        self, question: str, rows: Sequence[str], checked: set[int], *, allow_back: bool
    ) -> list[int]:
        answer = self._next(question)
        if not isinstance(answer, list):
            raise AssertionError(f"{question!r} wants a list, the script has {answer!r}")
        out: list[int] = []
        for item in answer:
            if isinstance(item, str):
                out.append(next(i for i, row in enumerate(rows) if item in row))
            else:
                out.append(int(item))
        return sorted(out)


def scripted(*answers: Any, guidance: str = "", lines: list[str] | None = None) -> Interactive:
    """An ``Interactive`` on a terminal answered by ``answers``; printed lines go to ``lines``."""
    sink = lines if lines is not None else []
    return Interactive(
        tty=True, guidance=guidance, driver=ScriptedDriver(list(answers)), say=sink.append
    )
