"""P11-15: the shared shadow git store."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from indus.backends import checkpoint_api
from indus.backends.checkpoints import install
from indus.backends.checkpoints.store import (
    MAX_FILES,
    ShadowStore,
    Skipped,
    Snapshot,
    check_commit,
    check_relpath,
    project_key,
    store_factory,
)
from indus.foundation.scope import Scope
from tests.conftest import FAKE_CLOCK_START
from tests.fakes.clock import FakeClock
from tests.unit.backends.checkpoints._helpers import make_store, user_git, write


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(FAKE_CLOCK_START)


@pytest.fixture
def store(tmp_path: Path, clock: FakeClock) -> ShadowStore:
    return make_store(tmp_path / "home", clock)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    write(root, "src/app.py", "print('one')\n")
    write(root, "README.md", "hello\n")
    return root


def test_snapshot_creates_a_ref_and_metadata(store: ShadowStore, project: Path) -> None:
    taken = store.snapshot(project, "first", session_id="s1", turn_id="t1")
    assert isinstance(taken, Snapshot)
    key = project_key(project)
    assert taken.key == key and taken.files_changed == 2
    assert store.tip(key) == taken.commit
    assert (store.store_dir / "refs" / "indus" / key).is_file()
    meta = store.read_meta(key)
    assert meta is not None and meta["workdir"] == str(project.resolve())
    entry = store.ledger(key)[-1]
    assert entry["commit"] == taken.commit and entry["session"] == "s1" and entry["turn"] == "t1"
    names = store.must(["ls-tree", "-r", "--name-only", taken.commit]).text.split()
    assert names == ["README.md", "src/app.py"]


def test_snapshot_leaves_the_user_repo_alone(store: ShadowStore, project: Path) -> None:
    user_git(project, "init", "-q")
    user_git(project, "add", "README.md")
    user_git(project, "commit", "-q", "-m", "base")
    write(project, "README.md", "edited\n")

    def state() -> tuple[str, str, bytes, list[str]]:
        git_dir = project / ".git"
        files = sorted(str(p.relative_to(git_dir)) for p in git_dir.rglob("*") if p.is_file())
        return (
            user_git(project, "status", "--porcelain"),
            user_git(project, "for-each-ref"),
            (git_dir / "index").read_bytes(),
            files,
        )

    before = state()
    taken = store.snapshot(project, "first")
    assert isinstance(taken, Snapshot)
    write(project, "src/app.py", "print('two')\n")
    assert isinstance(store.snapshot(project, "second"), Snapshot)
    after = state()
    assert after[1:] == before[1:]
    assert after[0] == before[0]  # src/ was already untracked; the edit hides inside it
    names = store.must(["ls-tree", "-r", "--name-only", taken.commit]).text.split()
    assert names == ["README.md", "src/app.py"]


def test_unchanged_is_skipped(store: ShadowStore, project: Path, clock: FakeClock) -> None:
    first = store.snapshot(project, "first")
    assert isinstance(first, Snapshot)
    clock.advance(60)
    again = store.snapshot(project, "again")
    assert isinstance(again, Skipped) and again.kind == "unchanged"
    meta = store.read_meta(first.key)
    assert meta is not None and meta["last_touch"] > meta["created_at"]
    write(project, "README.md", "changed\n")
    second = store.snapshot(project, "second")
    assert isinstance(second, Snapshot) and second.files_changed == 1
    assert store.commits(first.key) == [first.commit, second.commit]


def test_empty_missing_and_broad_folders(store: ShadowStore, tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert isinstance(store.snapshot(empty, "x"), Skipped)
    missing = store.snapshot(tmp_path / "nope", "x")
    assert isinstance(missing, Skipped) and missing.kind == "missing"
    root = store.snapshot("/", "x")
    assert isinstance(root, Skipped) and root.kind == "too_broad"
    home = make_store(tmp_path / "h2", FakeClock(FAKE_CLOCK_START), too_broad=[tmp_path])
    broad = home.snapshot(tmp_path, "x")
    assert isinstance(broad, Skipped) and broad.kind == "too_broad"


def test_file_limit_skips(tmp_path: Path, clock: FakeClock, project: Path) -> None:
    assert MAX_FILES == 50_000
    small = make_store(tmp_path / "home", clock, max_files=3)
    for i in range(2):
        write(project, f"more/{i}.txt", "x")
    over = small.snapshot(project, "x")
    assert isinstance(over, Skipped) and over.kind == "too_many_files"
    assert "more than 3 files" in over.reason
    for i in range(50):
        write(project, f"node_modules/pkg/{i}.js", "x")
    (project / "more" / "0.txt").unlink()
    (project / "more" / "1.txt").unlink()
    assert isinstance(small.snapshot(project, "x"), Snapshot)  # node_modules is not counted


def test_excludes_honoured(tmp_path: Path, clock: FakeClock, project: Path) -> None:
    store = make_store(tmp_path / "home", clock, max_file_size_mb=0.001)
    write(project, "node_modules/lib/index.js", "x")
    write(project, ".env", "TOKEN=" + "abc")
    write(project, "build/out.bin", "x")
    write(project, "app.log", "x")
    write(project, "big.dat", b"\0" * 4096)
    taken = store.snapshot(project, "x")
    assert isinstance(taken, Snapshot)
    names = store.must(["ls-tree", "-r", "--name-only", taken.commit]).text.split()
    assert names == ["README.md", "src/app.py"]
    exclude = (store.store_dir / "info" / "exclude").read_text()
    assert "node_modules/" in exclude and ".venv/" in exclude and "__pycache__/" in exclude


def test_two_projects_share_objects(store: ShadowStore, tmp_path: Path) -> None:
    blob = os.urandom(200_000)
    one = tmp_path / "one"
    write(one, "data.bin", blob)
    assert isinstance(store.snapshot(one, "x"), Snapshot)
    after_one = store.size()
    two = tmp_path / "two"
    write(two, "data.bin", blob)
    assert isinstance(store.snapshot(two, "x"), Snapshot)
    after_two = store.size()
    assert after_two - after_one < (after_one * 0.5)
    projects = {p.key: p for p in store.list_projects()}
    assert set(projects) == {project_key(one), project_key(two)}
    assert all(p.commits == 1 and p.exists for p in projects.values())


def test_nested_repo_noted(store: ShadowStore, project: Path) -> None:
    inner = project / "vendor" / "lib"
    write(inner, "a.txt", "x")
    user_git(inner, "init", "-q")
    user_git(inner, "add", "a.txt")
    user_git(inner, "commit", "-q", "-m", "inner")
    taken = store.snapshot(project, "with nested")
    assert isinstance(taken, Snapshot) and taken.nested == ("vendor/lib",)
    subject = store.must(["log", "-1", "--format=%s", taken.commit]).text
    assert "nested repositories not captured: vendor/lib" in subject


def test_history_capped_by_keep(store: ShadowStore, project: Path, clock: FakeClock) -> None:
    trees: list[str] = []
    for i in range(5):
        write(project, "README.md", f"v{i}\n")
        clock.advance(10)
        taken = store.snapshot(project, f"v{i}", keep=3)
        assert isinstance(taken, Snapshot)
        trees.append(store.must(["rev-parse", taken.commit + "^{tree}"]).text)
    commits = store.commits(project_key(project))
    assert len(commits) == 3
    kept = [store.must(["rev-parse", c + "^{tree}"]).text for c in commits]
    assert kept == trees[-3:]
    assert (store.store_dir / ".gc-pending").exists()


@pytest.mark.parametrize("bad", ["", "-p", "--all", "xyz1", "abc", "HEAD", "a" * 65, "12 34"])
def test_validators_reject_bad_hashes(bad: str) -> None:
    with pytest.raises(ValueError):
        check_commit(bad)


def test_validators(tmp_path: Path) -> None:
    assert check_commit("ABCDEF12") == "abcdef12"
    assert check_relpath("src/../src/a.py", tmp_path) == "src/a.py"
    for bad in ("/etc/passwd", "../x", "a/../../x", "", "~/x", "."):
        with pytest.raises(ValueError):
            check_relpath(bad, tmp_path)
    (tmp_path / "link").symlink_to("/etc")
    with pytest.raises(ValueError):
        check_relpath("link/passwd", tmp_path)


def test_git_missing_is_a_skip(tmp_path: Path, clock: FakeClock, project: Path) -> None:
    store = make_store(tmp_path / "home", clock, git="")
    store._git = None  # pyright: ignore[reportPrivateUsage]
    skipped = store.snapshot(project, "x")
    assert isinstance(skipped, Skipped) and skipped.kind == "error"
    assert "git is not installed" in skipped.reason


def test_protocol_methods_and_factory(scope: Scope, project: Path) -> None:
    assert store_factory(scope) is None  # no store until the first checkpoint
    real = ShadowStore.for_scope(scope)
    assert isinstance(real.snapshot(project, "x"), Snapshot)
    found = store_factory(scope)
    assert found is not None and found.base == scope.home / "checkpoints"
    assert found.size() > 0
    (only,) = found.list_projects()
    assert only.workdir == str(project.resolve()) and only.size_bytes > 0
    previous = checkpoint_api.set_store_factory(None)
    try:
        install()
        assert checkpoint_api.store_for(scope) is not None
    finally:
        checkpoint_api.set_store_factory(previous)
    cleared = found.clear()
    assert cleared.deleted and cleared.bytes_freed > 0
    assert not found.base.exists() and found.list_projects() == []
