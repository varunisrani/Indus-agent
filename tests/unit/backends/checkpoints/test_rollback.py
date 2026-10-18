"""P11-17: list, diff and restore checkpoints."""

from __future__ import annotations

from pathlib import Path

import pytest

from indus.backends.checkpoints import rollback
from indus.backends.checkpoints.rollback import RollbackError
from indus.backends.checkpoints.store import ShadowStore, Snapshot
from tests.conftest import FAKE_CLOCK_START
from tests.fakes.clock import FakeClock
from tests.unit.backends.checkpoints._helpers import make_store, write


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(FAKE_CLOCK_START)


@pytest.fixture
def store(tmp_path: Path, clock: FakeClock) -> ShadowStore:
    return make_store(tmp_path / "home", clock, max_file_size_mb=0.01)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    write(root, "a.txt", "alpha v1\n")
    write(root, "lib/b.txt", "beta v1\n")
    return root


def snap(store: ShadowStore, folder: Path, reason: str, clock: FakeClock, **kw: str) -> str:
    clock.advance(30)
    taken = store.snapshot(folder, reason, **kw)  # pyright: ignore[reportArgumentType]
    assert isinstance(taken, Snapshot), taken
    return taken.commit


def test_list_newest_first_with_counts(store: ShadowStore, project: Path, clock: FakeClock) -> None:
    first = snap(store, project, "one", clock, session_id="s1", turn_id="t1")
    write(project, "a.txt", "alpha v2\n")
    write(project, "c.txt", "new\n")
    second = snap(store, project, "two", clock, session_id="s1", turn_id="t2")
    rows = rollback.list_checkpoints(store, project)
    assert [(r.number, r.commit, r.reason) for r in rows] == [(1, second, "two"), (2, first, "one")]
    assert rows[0].files_changed == 2 and rows[1].files_changed == 2
    assert rows[0].session_id == "s1" and rows[0].turn_id == "t2"
    assert rows[0].at > rows[1].at
    assert rollback.list_checkpoints(store, project.parent) == []
    everything = rollback.list_all(store)
    assert [cp.commit for _folder, cp in everything] == [second, first]


def test_restore_round_trip(store: ShadowStore, project: Path, clock: FakeClock) -> None:
    write(project, "node_modules/dep.js", "keep me\n")
    write(project, "big.bin", b"\1" * 20_000)
    snap(store, project, "before edit", clock)
    write(project, "a.txt", "alpha v2\n")
    (project / "lib" / "b.txt").unlink()
    write(project, "new/dir/c.txt", "created later\n")
    write(project, "node_modules/later.js", "also keep\n")
    write(project, "big2.bin", b"\2" * 20_000)
    clock.advance(30)
    result = rollback.restore(store, project, 1)
    assert (project / "a.txt").read_bytes() == b"alpha v1\n"
    assert (project / "lib" / "b.txt").read_text() == "beta v1\n"
    assert not (project / "new").exists()
    assert result.removed == ("new/dir/c.txt",)
    for kept in ("node_modules/dep.js", "node_modules/later.js", "big.bin", "big2.bin"):
        assert (project / kept).exists(), kept
    assert result.safety is not None
    rows = rollback.list_checkpoints(store, project)
    assert rows[0].commit == result.safety and rows[0].reason.startswith("before rollback to")
    undo = rollback.restore(store, project, 1)
    assert (project / "a.txt").read_text() == "alpha v2\n"
    assert (project / "new" / "dir" / "c.txt").exists()
    assert undo.safety is not None


def test_restore_file_touches_one_file(store: ShadowStore, project: Path, clock: FakeClock) -> None:
    snap(store, project, "one", clock)
    write(project, "a.txt", "alpha v2\n")
    write(project, "lib/b.txt", "beta v2\n")
    clock.advance(30)
    result = rollback.restore_file(store, project, 1, "lib/b.txt")
    assert result.file == "lib/b.txt"
    assert (project / "lib" / "b.txt").read_text() == "beta v1\n"
    assert (project / "a.txt").read_text() == "alpha v2\n"
    (project / "lib" / "b.txt").unlink()
    rollback.restore_file(store, project, 2, "./lib/../lib/b.txt")
    assert (project / "lib" / "b.txt").read_text() == "beta v1\n"


def test_diff_matches_git_diff(store: ShadowStore, project: Path, clock: FakeClock) -> None:
    first = snap(store, project, "one", clock)
    write(project, "a.txt", "alpha v2\nmore\n")
    write(project, "c.txt", "new file\n")
    text = rollback.diff(store, project, 1)
    assert "+alpha v2" in text and "-alpha v1" in text and "new file" in text
    current = snap(store, project, "now", clock)
    expected = store.must(["diff", "--no-color", "--no-ext-diff", "--no-renames", first, current])
    assert text == expected.out.decode()
    assert rollback.diff(store, project, 1) == ""


def test_bad_numbers_and_paths(store: ShadowStore, project: Path, clock: FakeClock) -> None:
    with pytest.raises(RollbackError, match="no checkpoints"):
        rollback.restore(store, project, 1)
    snap(store, project, "one", clock)
    for number in (0, 2, -1):
        with pytest.raises(RollbackError, match="from 1 to 1"):
            rollback.restore(store, project, number)
    for path in ("/etc/passwd", "../outside", ""):
        with pytest.raises(RollbackError):
            rollback.restore_file(store, project, 1, path)
    with pytest.raises(RollbackError, match="does not hold"):
        rollback.restore_file(store, project, 1, "missing.txt")
    assert (project / "a.txt").read_text() == "alpha v1\n"


def test_failed_safety_snapshot_refuses(
    store: ShadowStore, project: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    snap(store, project, "one", clock)
    write(project, "a.txt", "alpha v2\n")
    monkeypatch.setattr(store, "max_files", 1)
    with pytest.raises(RollbackError, match="safety checkpoint failed"):
        rollback.restore(store, project, 1)
    assert (project / "a.txt").read_text() == "alpha v2\n"
