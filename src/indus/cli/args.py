"""Plain records the CLI passes around: global flags, chat flags, and the process state.

No click here and nothing heavy: ``entry`` reads the hand-kept flag sets before the app exists,
and ``chat``/``oneshot`` take these records instead of click objects so tests can build them.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field, fields, replace
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from indus.foundation.scope import Scope

__all__ = [
    "AUTO_WORKTREE",
    "BARE_CONTINUE",
    "CHAT_VALUE_FLAGS",
    "FALLBACK_VALUE_FLAGS",
    "LATEST",
    "ChatArgs",
    "CliState",
    "GlobalArgs",
    "HostOverrides",
    "Interface",
    "split_csv",
]

LATEST = "latest"
"""``--resume latest``: this terminal's or this workspace's newest session."""

BARE_CONTINUE = "__latest__"
"""The value click gets for a bare ``-c`` (``-c latest`` is a session called "latest")."""

AUTO_WORKTREE = "__auto__"
"""The value click gets for a bare ``-w`` (the worktree gets a generated name)."""

FALLBACK_VALUE_FLAGS: frozenset[str] = frozenset(
    {
        "-p",
        "--profile",
        "-z",
        "--oneshot",
        "--usage-file",
        "-m",
        "--model",
        "--provider",
        "--reasoning",
        "-t",
        "--toolsets",
        "-r",
        "--resume",
        "--in",
        "-s",
        "--skills",
        "-w",
        "--worktree",
    }
)
"""Top-level flags that take a value, kept by hand for when the app cannot be introspected.
A test keeps it equal to the set derived from the live app."""

CHAT_VALUE_FLAGS: frozenset[str] = frozenset(
    {"-q", "--query", "--query-file", "--max-turns", "--source"}
)
"""Value-taking flags only ``chat`` has (kept equal to the chat command by a test)."""

type Interface = Literal["tui", "cli"]


def split_csv(values: str | Sequence[str] | None) -> list[str] | None:
    """``"a,b"`` or ``["a", "b,c"]`` → ``["a", "b", "c"]`` (no blanks, no repeats); None if
    nothing is left."""
    if values is None:
        return None
    items = [values] if isinstance(values, str) else list(values)
    out: list[str] = []
    for item in items:
        for part in item.split(","):
            word = part.strip()
            if word and word not in out:
                out.append(word)
    return out or None


@dataclass(frozen=True)
class GlobalArgs:
    """Flags accepted before (and, for most, after) the subcommand."""

    model: str | None = None
    provider: str | None = None
    reasoning: str | None = None
    toolsets: str | None = None
    resume: str | None = None
    continue_: str | None = None
    in_dir: str | None = None
    no_restore_cwd: bool = False
    skills: tuple[str, ...] = ()
    yolo: bool = False
    pass_session_id: bool = False
    ignore_user_config: bool = False
    ignore_rules: bool = False
    safe_mode: bool = False
    interface: Interface | None = None
    oneshot: str | None = None
    usage_file: str | None = None
    worktree: str | None = None
    checkpoints: bool = False
    dev: bool = False

    @classmethod
    def from_values(cls, values: Mapping[str, Any]) -> GlobalArgs:
        names = {f.name for f in fields(cls)}
        picked = {k: v for k, v in values.items() if k in names and v is not None}
        if "skills" in picked:
            picked["skills"] = tuple(picked["skills"])
        return cls(**picked)

    def merged(self, later: GlobalArgs) -> GlobalArgs:
        """``later`` (flags given after the subcommand) over ``self``: a value given later wins,
        a flag left at its default never clears one given earlier (R04.21)."""
        changes: dict[str, Any] = {}
        for f in fields(self):
            value = getattr(later, f.name)
            if value is None or value is False or value == ():
                continue
            if f.name == "skills":
                value = (*self.skills, *value)
            changes[f.name] = value
        return replace(self, **changes)

    @property
    def effective_ignore_rules(self) -> bool:
        return self.ignore_rules or self.safe_mode

    @property
    def effective_ignore_user_config(self) -> bool:
        return self.ignore_user_config or self.safe_mode


@dataclass(frozen=True)
class ChatArgs:
    """Flags only ``chat`` has."""

    query: str | None = None
    query_file: str | None = None
    oneshot: bool = False
    quiet: bool = False
    max_turns: int | None = None
    source: str | None = None
    voice: bool = False


@dataclass(frozen=True)
class HostOverrides:
    """Test seam: a route and client factory to use instead of the configured provider.

    ``route`` is a ``RuntimeRoute``; ``client_factory`` and ``list_models`` are passed to the
    agent options as they are. Only test drivers build this; the product never does.
    """

    route: Any = None
    client_factory: Any = None
    list_models: Any = None


def _process_exit(code: int) -> None:
    os._exit(code)


@dataclass
class CliState:
    """What a command needs from the process: the scope, the env it was built from, and how the
    process ends. ``process`` is True only for the real ``indus`` process (never in tests)."""

    scope: Scope
    env: MutableMapping[str, str] = field(default_factory=dict[str, str])
    process: bool = False
    overrides: HostOverrides | None = None
    globals: GlobalArgs = field(default_factory=GlobalArgs)
    hard_exit: bool = False
    exit_fn: Callable[[int], None] = _process_exit
    stdin_isatty: Callable[[], bool] = field(default_factory=lambda: _isatty("stdin"))
    stdout_isatty: Callable[[], bool] = field(default_factory=lambda: _isatty("stdout"))
    selected_by: str | None = None
    """How bootstrap picked the profile (flag, env, sticky, default); None outside a real run."""


def _isatty(name: str) -> Callable[[], bool]:
    """A check of ``sys.<name>`` at call time (test runners swap the streams)."""

    def check() -> bool:
        probe = getattr(getattr(sys, name, None), "isatty", None)
        try:
            return bool(probe()) if callable(probe) else False
        except (OSError, ValueError):
            return False

    return check
