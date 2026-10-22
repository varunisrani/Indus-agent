"""``indus --version`` in a source checkout names the release HEAD is on (F-4, wave 46)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from indus.cli.version import checkout_release, head_commit, version_lines

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def git(cwd: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }
    out = subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True, timeout=60
    )
    return out.stdout.strip()


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A source tree shaped like an install (``src/indus`` + ``pyproject.toml``) with one commit."""
    tree = tmp_path / "indus-agent"
    (tree / "src" / "indus").mkdir(parents=True)
    (tree / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    git(tree, "init", "-q", "-b", "main")
    git(tree, "add", "-A")
    git(tree, "commit", "-q", "-m", "one")
    return tree


def tag(tree: Path, name: str, *, annotated: bool = True) -> None:
    if annotated:
        git(tree, "tag", "--annotate", name, "-m", '{"schema": 1}')
    else:
        git(tree, "tag", name)


def test_a_checkout_on_an_annotated_release_tag_prints_that_release(
    checkout: Path, tmp_path: Path
) -> None:
    tag(checkout, "v1.2.3")
    lines = version_lines(checkout / "src" / "indus", root=tmp_path / "root")
    assert lines[0] == "Indus Agent 1.2.3"


def test_packed_tags_are_peeled_and_the_newest_release_wins(checkout: Path, tmp_path: Path) -> None:
    tag(checkout, "v1.2.3")
    tag(checkout, "v1.2.4-canary.20261020T101010Z")
    tag(checkout, "v1.2.4")
    tag(checkout, "not-a-release")
    git(checkout, "pack-refs", "--all")
    assert not (checkout / ".git" / "refs" / "tags" / "v1.2.4").exists()
    assert checkout_release(checkout, root=tmp_path) == "1.2.4"


def test_a_lightweight_tag_and_a_detached_head_count(checkout: Path, tmp_path: Path) -> None:
    tag(checkout, "v0.4.0", annotated=False)
    git(checkout, "checkout", "-q", "--detach", "v0.4.0")
    assert head_commit(checkout) == git(checkout, "rev-parse", "HEAD")
    assert checkout_release(checkout, root=tmp_path) == "0.4.0"


def test_a_commit_past_the_release_keeps_the_source_version(checkout: Path, tmp_path: Path) -> None:
    tag(checkout, "v1.0.0")
    git(checkout, "commit", "-q", "--allow-empty", "-m", "two")
    assert checkout_release(checkout, root=tmp_path) is None
    lines = version_lines(checkout / "src" / "indus", root=tmp_path)
    assert lines[0] == "Indus Agent 0.0.0"


def test_malformed_tags_are_ignored(checkout: Path, tmp_path: Path) -> None:
    tag(checkout, "v01.2.3")
    tag(checkout, "v1.2.3-rc.1")
    assert checkout_release(checkout, root=tmp_path) is None


def test_the_installer_marker_names_the_release_for_its_commit(
    checkout: Path, tmp_path: Path
) -> None:
    head = git(checkout, "rev-parse", "HEAD")
    marker = {"schema": 1, "commit": head, "channel": "stable", "version": "2.0.1"}
    (checkout / ".indus-bootstrap.json").write_text(json.dumps(marker), encoding="utf-8")
    assert checkout_release(checkout, root=tmp_path) == "2.0.1"
    marker["commit"] = "0" * 40
    (checkout / ".indus-bootstrap.json").write_text(json.dumps(marker), encoding="utf-8")
    assert checkout_release(checkout, root=tmp_path) is None


def test_an_update_receipt_names_the_release_for_its_commit(checkout: Path, tmp_path: Path) -> None:
    head = git(checkout, "rev-parse", "HEAD")
    receipts = tmp_path / "root" / "logs" / "update_receipts"
    receipts.mkdir(parents=True)
    (receipts / "20261020T000000Z.json").write_text("{not json", encoding="utf-8")
    (receipts / "20261021T000000Z.json").write_text(
        json.dumps({"schema": 1, "to_commit": head, "to_version": "3.1.0"}), encoding="utf-8"
    )
    (receipts / "20261022T000000Z.json").write_text(
        json.dumps({"schema": 1, "to_commit": "f" * 40, "to_version": "3.2.0"}), encoding="utf-8"
    )
    assert checkout_release(checkout, root=tmp_path / "root") == "3.1.0"


def test_a_linked_worktree_reads_the_shared_tags(checkout: Path, tmp_path: Path) -> None:
    tag(checkout, "v5.0.0")
    linked = tmp_path / "linked"
    git(checkout, "worktree", "add", "-q", "--detach", str(linked), "v5.0.0")
    assert checkout_release(linked, root=tmp_path) == "5.0.0"


def test_a_tree_without_git_has_no_release(tmp_path: Path) -> None:
    assert head_commit(tmp_path) is None
    assert checkout_release(tmp_path, root=tmp_path) is None
