"""P11-18: P07's ``indus checkpoints`` cases rerun against the real shadow store."""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from indus.backends import checkpoint_api
from indus.backends.checkpoints import install
from indus.backends.checkpoints.store import ShadowStore, Snapshot, project_key
from indus.foundation.scope import Scope
from tests.fakes.clock import FakeClock
from tests.unit.backends.checkpoints._helpers import write
from tests.unit.cli.commands._helpers_d import run, store_globals

__all__ = ["store_globals"]

DAY = 86_400.0


@pytest.fixture
def real_store(scope: Scope, fake_clock: FakeClock, tmp_path: Path) -> Iterator[ShadowStore]:
    """Three projects: ``live-old`` (30 days), ``gone`` (1 day, folder deleted), ``live-new``
    (12 hours, 2 MB of its own data)."""
    previous = checkpoint_api.set_store_factory(None)
    install()
    store = ShadowStore.for_scope(scope)
    for name, data, wait in (
        ("live-old", b"old", 29 * DAY),
        ("gone", b"gone", 0.5 * DAY),
        ("live-new", os.urandom(2 * 1024 * 1024), 0.5 * DAY),
    ):
        folder = tmp_path / "work" / name
        write(folder, "data.bin", data)
        assert isinstance(store.snapshot(folder, "x"), Snapshot)
        fake_clock.advance(wait)
    shutil.rmtree(tmp_path / "work" / "gone")
    yield store
    checkpoint_api.set_store_factory(previous)


def _rows(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.endswith(("  live", "  orphan"))]


def test_status_and_list(scope: Scope, real_store: ShadowStore) -> None:
    result = run(scope, "checkpoints", "status")
    assert result.code == 0, result.err
    assert f"Checkpoint store: {real_store.base}" in result.out
    assert "Projects:         3" in result.out
    rows = _rows(result.out)
    assert [row.split()[0].rsplit("/", 1)[1] for row in rows] == ["live-new", "gone", "live-old"]
    assert "12h ago" in rows[0] and rows[0].endswith("live")
    assert rows[1].endswith("orphan") and "30d ago" in rows[2]
    assert "MB" in rows[0]  # live-new holds its own 2 MB
    limited = run(scope, "checkpoints", "list", "--limit", "1")
    assert limited.code == 0 and "and 2 more" in limited.out
    bare = run(scope, "checkpoints")
    assert bare.code == 0 and "Projects:" in bare.out


def test_prune_with_force(scope: Scope, real_store: ShadowStore, tmp_path: Path) -> None:
    result = run(scope, "checkpoints", "prune", "-f", "--max-size-mb", "1")
    assert result.code == 0, result.err
    assert "Deleted orphaned: 1" in result.out
    assert "Deleted unused:   1" in result.out
    assert "Deleted for size: 1" in result.out
    assert real_store.list_projects() == []
    assert real_store.tip(project_key(tmp_path / "work" / "live-new")) is None


def test_prune_orphans_need_a_yes(scope: Scope, real_store: ShadowStore, tmp_path: Path) -> None:
    refused = run(scope, "checkpoints", "prune")
    assert refused.code == 1 and "-f" in refused.err
    no = run(scope, "checkpoints", "prune", tty=True, input="n\n")
    assert no.code == 1
    assert len(real_store.list_projects()) == 3
    keep = run(scope, "checkpoints", "prune", "--keep-orphans")
    assert keep.code == 0, keep.err
    left = {Path(str(p.workdir)).name for p in real_store.list_projects()}
    assert left == {"gone", "live-new"}  # live-old was stale; the orphan stays
    yes = run(scope, "checkpoints", "prune", tty=True, input="y\n")
    assert yes.code == 0, yes.err
    assert {Path(str(p.workdir)).name for p in real_store.list_projects()} == {"live-new"}


def test_clear(scope: Scope, real_store: ShadowStore) -> None:
    refused = run(scope, "checkpoints", "clear")
    assert refused.code == 1 and "-f" in refused.err
    assert real_store.exists()
    done = run(scope, "checkpoints", "clear", "-f")
    assert done.code == 0 and "Cleared" in done.out
    assert not real_store.base.exists()
    again = run(scope, "checkpoints", "clear", "-f")
    assert again.code == 0 and "Nothing to clear" in again.out


def test_no_store_yet(scope: Scope) -> None:
    previous = checkpoint_api.set_store_factory(None)
    install()
    try:
        for argv in (["status"], ["prune"], ["clear"]):
            result = run(scope, "checkpoints", *argv)
            assert result.code == 0 and "checkpoints have been taken" in result.out
    finally:
        checkpoint_api.set_store_factory(previous)
