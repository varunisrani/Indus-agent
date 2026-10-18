"""P11-18: pruning the checkpoint store."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from indus.backends.checkpoint_api import plan_prune
from indus.backends.checkpoints import prune
from indus.backends.checkpoints.store import GitError, ShadowStore, Snapshot, project_key
from tests.conftest import FAKE_CLOCK_START
from tests.fakes.clock import FakeClock
from tests.unit.backends.checkpoints._helpers import make_store, write

DAY = 86_400.0


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(FAKE_CLOCK_START)


@pytest.fixture
def store(tmp_path: Path, clock: FakeClock) -> ShadowStore:
    return make_store(tmp_path / "home", clock)


def project(tmp_path: Path, name: str, size: int = 10) -> Path:
    root = tmp_path / name
    write(root, "file.txt", name * size)
    return root


def snap(store: ShadowStore, folder: Path) -> str:
    taken = store.snapshot(folder, "x")
    assert isinstance(taken, Snapshot), taken
    return taken.commit


def test_plan_deletes_orphan_and_stale(
    store: ShadowStore, tmp_path: Path, clock: FakeClock
) -> None:
    old = project(tmp_path, "old")
    write(old, "data.bin", os.urandom(100_000))
    snap(store, old)
    clock.advance(8 * DAY)
    gone = project(tmp_path, "gone")
    write(gone, "data.bin", os.urandom(100_000))
    snap(store, gone)
    live = project(tmp_path, "live")
    snap(store, live)
    shutil.rmtree(gone)
    now = clock.now().timestamp()
    plan = plan_prune(store.list_projects(), now=now, retention_days=7, max_size_bytes=2**40)
    assert plan.orphans == (project_key(gone),) and plan.stale == (project_key(old),)
    before = store.size()
    result = store.prune(plan)
    assert (result.deleted_orphan, result.deleted_stale, result.errors) == (1, 1, 0)
    assert result.scanned == 3 and result.bytes_freed > 0 and store.size() < before
    assert [p.key for p in store.list_projects()] == [project_key(live)]
    assert store.tip(project_key(gone)) is None
    assert not store.meta_path(project_key(old)).exists()


def test_trim_keeps_newest(store: ShadowStore, tmp_path: Path, clock: FakeClock) -> None:
    folder = project(tmp_path, "p")
    commits = []
    for i in range(4):
        write(folder, "file.txt", f"v{i}")
        clock.advance(60)
        commits.append(snap(store, folder))
    key = project_key(folder)
    dates = [store.must(["log", "-1", "--format=%ct %s", c]).text for c in commits]
    assert prune.trim_history(store, key, 2) == 2
    kept = store.commits(key)
    assert [store.must(["log", "-1", "--format=%ct %s", c]).text for c in kept] == dates[-2:]
    assert prune.trim_history(store, key, 0) == 1  # at least one is always kept
    assert len(store.commits(key)) == 1


def test_rewrite_refuses_when_the_ref_moved(
    store: ShadowStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = project(tmp_path, "p")
    first = snap(store, folder)
    write(folder, "file.txt", "v2")
    second = snap(store, folder)
    key = project_key(folder)
    monkeypatch.setattr(store, "tip", lambda _key: first)  # a reader that saw an older tip
    with pytest.raises(GitError):
        prune.rewrite_history(store, key, [second])
    monkeypatch.undo()
    assert store.tip(key) == second


def test_size_cap_drops_oldest_checkpoints(
    store: ShadowStore, tmp_path: Path, clock: FakeClock
) -> None:
    folders = [tmp_path / "a", tmp_path / "b"]
    for _ in range(3):
        for folder in folders:
            write(folder, "blob.bin", os.urandom(150_000))
            clock.advance(60)
            snap(store, folder)
    prune.gc(store)
    full = store.size()
    dropped, fits = prune.fit_size(store, full - 200_000)
    assert fits and dropped >= 2
    assert store.size() <= full - 200_000
    for folder in folders:
        assert 1 <= len(store.commits(project_key(folder))) < 3
    dropped, fits = prune.fit_size(store, 1)
    assert not fits  # every project keeps its newest checkpoint
    assert all(len(store.commits(project_key(f))) == 1 for f in folders)


def test_prune_store_order(store: ShadowStore, tmp_path: Path, clock: FakeClock) -> None:
    gone = project(tmp_path, "gone")
    snap(store, gone)
    shutil.rmtree(gone)
    live = project(tmp_path, "live")
    snap(store, live)
    kept = prune.prune_store(store, now=clock.now().timestamp(), delete_orphans=False)
    assert kept.result.deleted_orphan == 0 and kept.fits
    done = prune.prune_store(store, now=clock.now().timestamp())
    assert done.result.deleted_orphan == 1 and done.fits


def test_marker_throttles_auto_prune(store: ShadowStore, tmp_path: Path, clock: FakeClock) -> None:
    assert not prune.auto_prune(store, now=clock.now().timestamp()).ran  # no store yet
    stale = project(tmp_path, "stale")
    snap(store, stale)
    gone = project(tmp_path, "gone")
    snap(store, gone)
    shutil.rmtree(gone)
    now = clock.now().timestamp()
    first = prune.auto_prune(store, now=now)
    assert first.ran and first.outcome is not None
    assert first.outcome.result.deleted_orphan == 0  # unattended prunes keep orphans
    assert (store.base / prune.PRUNE_MARKER).exists()
    soon = prune.auto_prune(store, now=now + 23 * 3600)
    assert not soon.ran and soon.reason == "pruned recently"
    later = prune.auto_prune(store, now=now + 8 * DAY)
    assert later.ran and later.outcome is not None
    assert later.outcome.result.deleted_stale == 1
    # retention never reaches a missing folder: only an explicit, confirmed prune deletes it
    assert [p.key for p in store.list_projects()] == [project_key(gone)]
    (store.base / prune.PRUNE_MARKER).write_text("not a number")
    assert prune.auto_prune(store, now=now + 8 * DAY + 1).ran  # corrupt marker = never pruned
