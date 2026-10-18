"""Cut a tool-call batch into ordered sequential and parallel segments (R01.14).

Each call is either a barrier (runs alone, in order) or admissible to a parallel run:

- barrier: an interactive tool, arguments that are not a JSON object, a tool whose own deadline is
  longer than the batch deadline, a tool with no known traits, anything not read-only;
- admissible: a read-only tool; a path-scoped tool (reader or writer) whose path is known.

Path-scoped calls reserve their canonical path. Readers may overlap readers; a writer conflicts with
any overlapping reservation (one path is a prefix of the other), and a conflict closes the current
run so the call opens a new one. A run of fewer than two calls becomes sequential, neighbouring
sequential pieces merge, and no call moves across another, so the side effects happen in the same
order as fully sequential execution.

Traits come from the registry entry (``ToolEntry.traits``); core tool names without registered
traits fall back to ``BUILTIN_TRAITS``.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from indus.tools.registry import NO_TRAITS, ToolEntry, ToolTraits

__all__ = [
    "BUILTIN_TRAITS",
    "PlanCall",
    "Segment",
    "canonical_path",
    "paths_overlap",
    "plan_segments",
    "traits_for",
]

type SegmentKind = Literal["sequential", "parallel"]

BUILTIN_TRAITS: Mapping[str, ToolTraits] = {
    "read_file": ToolTraits(read_only=True, path_arg="path"),
    "search_files": ToolTraits(read_only=True, path_arg="path", path_default="."),
    "write_file": ToolTraits(path_arg="path", writer=True, mutating=True),
    "patch": ToolTraits(path_arg="path", writer=True, mutating=True),
    "web_search": ToolTraits(read_only=True),
    "web_extract": ToolTraits(read_only=True),
    "session_search": ToolTraits(read_only=True),
    "vision_analyze": ToolTraits(read_only=True),
    "image_generate": ToolTraits(read_only=True),
    "clarify": ToolTraits(interactive=True),
    "terminal": ToolTraits(mutating=True),
    "process_manage": ToolTraits(mutating=True),
    "todo_list": ToolTraits(mutating=True),
    "memory": ToolTraits(mutating=True),
    "delegate_task": ToolTraits(mutating=True),
}


def traits_for(name: str, entry: ToolEntry | None) -> ToolTraits:
    """The entry's traits when it declared any, else the built-in table, else none."""
    if entry is not None and entry.traits != NO_TRAITS:
        return entry.traits
    return BUILTIN_TRAITS.get(name, NO_TRAITS)


@dataclass(frozen=True)
class PlanCall:
    """One call as the planner sees it. ``args`` is ``None`` when they did not parse to an object.

    ``traits`` and ``deadline_s`` come from the registry; ``known`` is False when the tool is not
    registered (dispatch answers it with an error, alone).
    """

    name: str
    args: Mapping[str, Any] | None
    traits: ToolTraits = NO_TRAITS
    deadline_s: float | None = None
    known: bool = True


@dataclass
class Segment:
    kind: SegmentKind
    indices: list[int] = field(default_factory=list[int])


def canonical_path(raw: str, cwd: Path | None = None) -> Path:
    """Absolute, symlink-resolved, case-normalised path; relative paths resolve against ``cwd``."""
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = (cwd if cwd is not None else Path.cwd()) / candidate
    return Path(os.path.normcase(os.path.realpath(candidate)))


def paths_overlap(left: Path, right: Path) -> bool:
    """True when one path lies inside the other (or they are equal)."""
    a, b = left.parts, right.parts
    if not a or not b:
        return False
    n = min(len(a), len(b))
    return a[:n] == b[:n]


def _reservation(call: PlanCall, cwd: Path | None) -> Path | None:
    arg = call.traits.path_arg
    if arg is None or call.args is None:
        return None
    raw = call.args.get(arg)
    if not (isinstance(raw, str) and raw.strip()):
        raw = call.traits.path_default
    if not raw:
        return None
    return canonical_path(raw, cwd)


def _admission(
    call: PlanCall, cwd: Path | None, batch_deadline_s: float | None
) -> tuple[Path | None, bool] | None:
    """``None`` for a barrier, else ``(reserved path or None, is_writer)``."""
    traits = call.traits
    if not call.known or call.args is None or traits.interactive:
        return None
    if (
        call.deadline_s is not None
        and batch_deadline_s is not None
        and call.deadline_s > batch_deadline_s
    ):
        return None
    if traits.path_arg is not None:
        path = _reservation(call, cwd)
        return (path, traits.writer) if path is not None else None
    if traits.read_only:
        return None, False
    return None


def plan_segments(
    calls: Sequence[PlanCall],
    *,
    cwd: Path | None = None,
    batch_deadline_s: float | None = None,
) -> list[Segment]:
    """Ordered segments covering every call index exactly once (see the module notes)."""
    segments: list[Segment] = []
    if len(calls) <= 1:
        return [Segment("sequential", list(range(len(calls))))] if calls else []
    run: list[int] = []
    held: list[tuple[Path, bool]] = []

    def add_sequential(indices: list[int]) -> None:
        if segments and segments[-1].kind == "sequential":
            segments[-1].indices.extend(indices)
        else:
            segments.append(Segment("sequential", list(indices)))

    def close_run() -> None:
        nonlocal run, held
        if len(run) >= 2:
            segments.append(Segment("parallel", run))
        elif run:
            add_sequential(run)
        run, held = [], []

    for index, call in enumerate(calls):
        admitted = _admission(call, cwd, batch_deadline_s)
        if admitted is None:
            close_run()
            add_sequential([index])
            continue
        path, writer = admitted
        if path is not None and any(
            (writer or other_writer) and paths_overlap(path, other) for other, other_writer in held
        ):
            close_run()
        if path is not None:
            held.append((path, writer))
        run.append(index)
    close_run()
    return segments


def plan_from(
    names_args: Sequence[tuple[str, Mapping[str, Any] | None]],
    lookup: Callable[[str], ToolEntry | None],
) -> list[PlanCall]:
    """``PlanCall`` values for ``(name, args)`` pairs, traits looked up per name."""
    out: list[PlanCall] = []
    for name, args in names_args:
        entry = lookup(name)
        out.append(
            PlanCall(
                name=name,
                args=args,
                traits=traits_for(name, entry),
                deadline_s=entry.timeout_s if entry is not None else None,
                known=entry is not None or name in BUILTIN_TRAITS,
            )
        )
    return out
