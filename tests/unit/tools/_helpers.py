"""Small builders shared by the tool tests."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from indus.foundation.scope import Scope, make_scope
from tests.fakes.clock import FakeClock


def params(**props: Any) -> dict[str, Any]:
    return {"type": "object", "properties": dict(props)}


def schema(description: str = "test tool", **props: Any) -> dict[str, Any]:
    return {"description": description, "parameters": params(**props)}


def echo(args: Mapping[str, Any]) -> str:
    return f"echo:{sorted(args.items())}"


def other_scope(base: Path, name: str, clock: FakeClock) -> Scope:
    home = base / "profiles" / name
    home.mkdir(parents=True, exist_ok=True)
    return make_scope(home, clock=clock)


class CountingProbe:
    """A probe whose answers are scripted; counts calls."""

    def __init__(self, *answers: bool | Exception) -> None:
        self.answers = list(answers)
        self.calls = 0
        self.__qualname__ = "CountingProbe"

    def __call__(self, scope: Scope) -> bool:
        self.calls += 1
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


def returns(value: Any) -> Callable[..., Any]:
    def handler(args: Mapping[str, Any]) -> Any:
        return value

    return handler
