"""Segment planner (P04-14)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from indus.core.tool_plan import (
    BUILTIN_TRAITS,
    PlanCall,
    canonical_path,
    paths_overlap,
    plan_from,
    plan_segments,
    traits_for,
)
from indus.tools.registry import NO_TRAITS, ToolRegistry, ToolTraits

READ = ToolTraits(read_only=True, path_arg="path")
WRITE = ToolTraits(path_arg="path", writer=True)
LOOK = ToolTraits(read_only=True)
ASK = ToolTraits(interactive=True)


def call(traits: ToolTraits, path: str | None = None, **extra: Any) -> PlanCall:
    args: dict[str, Any] | None = {"path": path} if path is not None else {}
    return PlanCall(name="t", args=args, traits=traits, **extra)


def shape(calls: list[PlanCall], tmp: Path) -> list[tuple[str, list[int]]]:
    return [(s.kind, s.indices) for s in plan_segments(calls, cwd=tmp, batch_deadline_s=420.0)]


@pytest.mark.parametrize(
    ("calls", "expected"),
    [
        ([], []),
        ([call(LOOK)], [("sequential", [0])]),
        ([call(LOOK), call(LOOK), call(LOOK)], [("parallel", [0, 1, 2])]),
        # readers may share a subtree
        ([call(READ, "a"), call(READ, "a/b.txt")], [("parallel", [0, 1])]),
        # a writer conflicts with an overlapping reader in either order: new run after it
        (
            [call(READ, "a/b.txt"), call(WRITE, "a"), call(READ, "c")],
            [("sequential", [0]), ("parallel", [1, 2])],
        ),
        (
            [call(WRITE, "a"), call(READ, "a/x"), call(READ, "z")],
            [("sequential", [0]), ("parallel", [1, 2])],
        ),
        # writers on unrelated paths run together
        ([call(WRITE, "a"), call(WRITE, "b")], [("parallel", [0, 1])]),
        # a barrier in the middle splits the runs and nothing crosses it
        (
            [call(LOOK), call(LOOK), call(ASK), call(LOOK), call(LOOK)],
            [("parallel", [0, 1]), ("sequential", [2]), ("parallel", [3, 4])],
        ),
        # runs of one become sequential and merge with their neighbours
        (
            [call(LOOK), call(NO_TRAITS), call(LOOK), call(NO_TRAITS)],
            [("sequential", [0, 1, 2, 3])],
        ),
    ],
)
def test_segment_table(calls: list[PlanCall], expected: list[Any], tmp_path: Path) -> None:
    assert shape(calls, tmp_path) == expected


def test_barrier_kinds(tmp_path: Path) -> None:
    unparsed = PlanCall("t", None, LOOK)
    unknown = PlanCall("t", {}, LOOK, known=False)
    slow = PlanCall("t", {}, LOOK, deadline_s=900.0)
    pathless_writer = PlanCall("t", {}, WRITE)
    for barrier in (unparsed, unknown, slow, pathless_writer):
        segs = shape([call(LOOK), barrier, call(LOOK)], tmp_path)
        assert segs == [("sequential", [0, 1, 2])], barrier


def test_default_path_reserves_the_working_directory(tmp_path: Path) -> None:
    search = ToolTraits(read_only=True, path_arg="path", path_default=".")
    calls = [PlanCall("s", {}, search), call(WRITE, "sub/file")]
    assert shape(calls, tmp_path) == [("sequential", [0, 1])]


def test_order_is_never_changed(tmp_path: Path) -> None:
    import random

    rng = random.Random(7)
    kinds = [LOOK, READ, WRITE, ASK, NO_TRAITS]
    for _ in range(300):
        calls = [call(rng.choice(kinds), rng.choice(["a", "a/b", "c", "d/e"])) for _ in range(9)]
        flat = [i for _, idx in shape(calls, tmp_path) for i in idx]
        assert flat == list(range(len(calls)))


def test_paths(tmp_path: Path) -> None:
    a = canonical_path("x", tmp_path)
    assert a.is_absolute() and a == canonical_path(str(tmp_path / "x"))
    assert paths_overlap(a, canonical_path("x/y", tmp_path))
    assert not paths_overlap(a, canonical_path("xy", tmp_path))


def test_traits_from_registry_then_builtin_table() -> None:
    registry = ToolRegistry()
    registry.register("read_file", "file", {"parameters": {"type": "object"}}, lambda a: "")
    registry.register("mine", "x", {"parameters": {"type": "object"}}, lambda a: "", traits=LOOK)
    assert traits_for("read_file", registry.get("read_file")) == BUILTIN_TRAITS["read_file"]
    assert traits_for("mine", registry.get("mine")) == LOOK
    planned = plan_from([("mine", {}), ("nope", {})], registry.get)
    assert planned[0].known and not planned[1].known
