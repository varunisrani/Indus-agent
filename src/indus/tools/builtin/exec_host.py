# pyright: strict
"""The slice of a terminal environment that file tools run helper commands through.

Tools sit above backends (D-04.7) and may import the backend contract; these narrow protocols
stay because the search and lint helpers need only this much, and their tests fake only this
much. Every ``indus.backends.contract.Environment`` meets them structurally. Helper commands
always ask for the whole output (``bounded=False``): bounding is for the model-facing terminal
path only (R02.2).
"""

from __future__ import annotations

import shlex
from typing import Any, Protocol

__all__ = ["CancelLike", "ExecHost", "ExecOutput", "quote_path"]


class CancelLike(Protocol):
    """The turn's stop flag (``asyncio.Event`` / ``threading.Event`` fit)."""

    def is_set(self) -> bool: ...


class ExecOutput(Protocol):
    """What one command produced (``indus.backends.contract.ExecResult`` fits)."""

    @property
    def output(self) -> str: ...

    @property
    def returncode(self) -> int: ...

    @property
    def timed_out(self) -> bool: ...

    @property
    def interrupted(self) -> bool: ...


class ExecHost(Protocol):
    """Somewhere a shell command runs: the task's terminal environment."""

    @property
    def cwd(self) -> str: ...

    async def execute(
        self,
        command: str,
        *,
        timeout: float | None = None,
        cwd: str | None = None,
        stdin: str | None = None,
        bounded: bool = True,
        cancel: Any = None,
    ) -> ExecOutput: ...


def quote_path(path: str) -> str:
    """Quote ``path`` for bash while keeping a leading ``~`` meaning that shell's home.

    The environment's own shell expands ``~``, so a sandbox resolves it to the sandbox home.
    A path starting with ``-`` is fine as long as callers put it after ``--``.
    """
    if path == "~":
        return '"$HOME"'
    if path.startswith("~/"):
        rest = path[2:]
        return '"$HOME"' if not rest else f'"$HOME"/{shlex.quote(rest)}'
    return shlex.quote(path)
