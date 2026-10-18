"""A terminal backend that answers commands from a script and records how it was called.

Skeleton for P05: terminal backends will be tested against it through the backend interface.
"""

from __future__ import annotations

import fnmatch
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str
    duration: float


@dataclass(frozen=True)
class CommandCall:
    argv: tuple[str, ...]
    cwd: str | None
    env: Mapping[str, str] | None


class UnscriptedCommand(LookupError):
    """The backend got a command no rule matches (a test bug, never a silent default)."""


class FakeBackend:
    """Rules are shell-style globs over the joined command line; the first match wins."""

    def __init__(self) -> None:
        self._rules: list[tuple[str, CommandResult]] = []
        self.calls: list[CommandCall] = []

    def script(
        self,
        pattern: str,
        exit_code: int = 0,
        stdout: str = "",
        stderr: str = "",
        duration: float = 0.0,
    ) -> None:
        self._rules.append((pattern, CommandResult(exit_code, stdout, stderr, duration)))

    async def run(
        self,
        argv: Sequence[str],
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        call = CommandCall(tuple(argv), cwd, None if env is None else dict(env))
        self.calls.append(call)
        line = shlex.join(call.argv)
        for pattern, result in self._rules:
            if fnmatch.fnmatchcase(line, pattern):
                return result
        raise UnscriptedCommand(f"no scripted result for command: {line}")


__all__ = ["CommandCall", "CommandResult", "FakeBackend", "UnscriptedCommand"]
