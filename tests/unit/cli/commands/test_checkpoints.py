"""P07-24: `indus checkpoints` against a fake `CheckpointStore`."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from indus.backends import checkpoint_api
from indus.backends.checkpoint_api import (
    CheckpointProject,
    ClearResult,
    PrunePlan,
    PruneResult,
    plan_prune,
)
from indus.cli.cmdkit import age_label
from indus.foundation.scope import Scope
from tests.unit.cli.commands._helpers_d import DAY, run

MB = 1024 * 1024


@dataclass
class FakeStore:
    base: Path
    projects: list[CheckpointProject]
    pruned: list[PrunePlan] = field(default_factory=lambda: [])
    cleared: int = 0
    clear_ok: bool = True

    def list_projects(self) -> list[CheckpointProject]:
        return list(self.projects)

    def size(self) -> int:
        return sum(p.size_bytes for p in self.projects)

    def prune(self, plan: PrunePlan) -> PruneResult:
        self.pruned.append(plan)
        gone = set(plan.keys)
        freed = sum(p.size_bytes for p in self.projects if p.key in gone)
        self.projects = [p for p in self.projects if p.key not in gone]
        return PruneResult(
            scanned=len(self.projects) + len(gone),
            deleted_orphan=len(plan.orphans),
            deleted_stale=len(plan.stale),
            deleted_oversize=len(plan.oversize),
            bytes_freed=freed,
        )

    def clear(self) -> ClearResult:
        self.cleared += 1
        if not self.clear_ok:
            return ClearResult(False, 0, ["permission denied"])
        freed = self.size()
        self.projects = []
        return ClearResult(True, freed)


def _project(
    key: str, *, days: float | None, exists: bool = True, mb: int = 1, now: float
) -> CheckpointProject:
    touch = None if days is None else now - days * DAY
    return CheckpointProject(key, f"/work/{key}", 3, mb * MB, touch, exists)


@pytest.fixture
def store(scope: Scope, tmp_path: Path) -> Iterator[FakeStore]:
    now = scope.clock.now().timestamp()
    fake = FakeStore(
        tmp_path / "checkpoints",
        [
            _project("live-new", days=0.5, now=now, mb=2),
            _project("live-old", days=30, now=now),
            _project("gone", days=1, exists=False, now=now),
        ],
    )
    previous = checkpoint_api.set_store_factory(lambda _scope: fake)
    yield fake
    checkpoint_api.set_store_factory(previous)


@pytest.mark.parametrize(
    ("seconds", "label"),
    [
        (None, "—"),
        (-5, "now"),
        (0, "0s ago"),
        (59, "59s ago"),
        (60, "1m ago"),
        (3599, "59m ago"),
        (3600, "1h ago"),
        (86399, "23h ago"),
        (86400, "1d ago"),
        (10 * 86400 + 5, "10d ago"),
    ],
)
def test_age_formatting(seconds: float | None, label: str) -> None:
    assert age_label(seconds) == label


def test_plan_order_orphans_stale_then_oldest_to_the_cap() -> None:
    now = 1_800_000_000.0
    projects = [
        _project("orphan", days=0.1, exists=False, now=now, mb=5),
        _project("stale", days=10, now=now, mb=5),
        _project("old", days=5, now=now, mb=5),
        _project("mid", days=3, now=now, mb=5),
        _project("new", days=1, now=now, mb=5),
    ]
    plan = plan_prune(projects, now=now, retention_days=7, max_size_bytes=7 * MB)
    assert plan.orphans == ("orphan",)
    assert plan.stale == ("stale",)
    assert plan.oversize == ("old", "mid")
    assert plan.keys == ("orphan", "stale", "old", "mid")


def test_plan_keeps_orphans_when_asked_and_binds_to_the_preview() -> None:
    now = 1_800_000_000.0
    projects = [
        _project("shown", days=1, exists=False, now=now),
        _project("late", days=1, exists=False, now=now),
        _project("live", days=1, now=now),
    ]
    kept = plan_prune(projects, now=now, retention_days=7, max_size_bytes=0, delete_orphans=False)
    assert kept.orphans == () and kept.oversize == ("live",)
    bound = plan_prune(
        projects, now=now, retention_days=7, max_size_bytes=100 * MB, orphan_allowlist={"shown"}
    )
    assert bound.orphans == ("shown",)


def test_status_rows(scope: Scope, store: FakeStore) -> None:
    result = run(scope, "checkpoints", "status")
    assert result.code == 0, result.err
    assert "Projects:         3" in result.out
    assert "Total size:       4.0 MB" in result.out
    lines = [line for line in result.out.splitlines() if "/work/" in line]
    assert [line.split()[0] for line in lines] == ["/work/live-new", "/work/gone", "/work/live-old"]
    assert "12h ago" in lines[0] and "live" in lines[0]
    assert lines[1].endswith("orphan")
    limited = run(scope, "checkpoints", "list", "--limit", "1")
    assert "and 2 more" in limited.out
    bare = run(scope, "checkpoints")
    assert bare.code == 0 and "Projects:" in bare.out


def test_prune_with_force(scope: Scope, store: FakeStore) -> None:
    result = run(scope, "checkpoints", "prune", "-f", "--max-size-mb", "1")
    assert result.code == 0, result.err
    (plan,) = store.pruned
    assert plan.orphans == ("gone",) and plan.stale == ("live-old",)
    assert plan.oversize == ("live-new",)
    assert "Deleted orphaned: 1" in result.out


def test_prune_orphans_need_a_yes(scope: Scope, store: FakeStore) -> None:
    refused = run(scope, "checkpoints", "prune")
    assert refused.code == 1
    assert "-f" in refused.err and store.pruned == []
    no = run(scope, "checkpoints", "prune", tty=True, input="n\n")
    assert no.code == 1 and store.pruned == []
    keep = run(scope, "checkpoints", "prune", "--keep-orphans")
    assert keep.code == 0, keep.err
    assert store.pruned[-1].orphans == () and store.pruned[-1].stale == ("live-old",)


def test_prune_counts_errors(
    scope: Scope, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store, "prune", lambda plan: replace(PruneResult(), errors=2))
    result = run(scope, "checkpoints", "prune", "-f")
    assert result.code == 1 and "2 project(s)" in result.err


def test_clear_without_force_on_non_tty_exits_1(scope: Scope, store: FakeStore) -> None:
    result = run(scope, "checkpoints", "clear")
    assert result.code == 1
    assert "-f" in result.err and store.cleared == 0


def test_clear_with_force_and_failure(scope: Scope, store: FakeStore) -> None:
    store.clear_ok = False
    failed = run(scope, "checkpoints", "clear", "-f")
    assert failed.code == 2 and "permission denied" in failed.err
    store.clear_ok = True
    done = run(scope, "checkpoints", "clear", "-f")
    assert done.code == 0 and "Cleared" in done.out
    again = run(scope, "checkpoints", "clear", "-f")
    assert again.code == 0 and "Nothing to clear" in again.out


def test_no_store_yet(scope: Scope) -> None:
    previous = checkpoint_api.set_store_factory(None)
    try:
        for argv in (["status"], ["prune"], ["clear"]):
            result = run(scope, "checkpoints", *argv)
            assert result.code == 0 and "checkpoints have been taken" in result.out
    finally:
        checkpoint_api.set_store_factory(previous)
