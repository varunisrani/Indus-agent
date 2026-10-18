"""Policy from the base branch (R10.22): a PR cannot loosen the guard that judges it."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import date
from pathlib import Path

import pytest

from tooling.checks import pins, policy
from tooling.checks.common import Context
from tooling.checks.tests.conftest import write

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

PYPROJECT = (
    '[project]\nname = "demo"\ndependencies = {deps}\n\n'
    '[tool.uv]\nexclude-newer = "2026-10-04T00:00:00Z"\n'
)


def git(root: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def base_repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repo"
    write(root / "pyproject.toml", PYPROJECT.format(deps='["httpx==0.28.1"]'))
    write(root / "tooling/checks/pins_allow.json", json.dumps({"allow": {}}))
    write(root / "tooling/checks/pins_meta.json", json.dumps({"lock_date": "2026-10-18"}))
    write(root / "tooling/checks/banned_patterns.json", "{}")
    write(root / "tooling/checks/pins.py", "# code, not policy\n")
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    return root, git(root, "rev-parse", "HEAD")


def judge(root: Path) -> int:
    folder = root / "tooling/checks"
    return pins.run(
        Context(root=root),
        allow_path=folder / "pins_allow.json",
        meta_path=folder / "pins_meta.json",
        today=date(2026, 10, 18),
    ).code


def test_widened_allow_list_in_pr_still_fails(tmp_path: Path):
    root, base = base_repo(tmp_path)
    # The pull request adds a range and allows it in the same change.
    write(root / "pyproject.toml", PYPROJECT.format(deps='["httpx>=0.27,<1"]'))
    widened = {"allow": {"httpx": {"spec": ">=0.27,<1", "reason": "trust me"}}}
    write(root / "tooling/checks/pins_allow.json", json.dumps(widened))
    git(root, "commit", "-qam", "pr")
    assert judge(root) == 0  # judged by its own policy the PR would pass

    report = policy.restore(root, base)

    assert judge(root) == 1
    assert any("pins_allow.json: replaced" in line for line in report)


def test_deleted_policy_is_restored_and_new_one_kept(tmp_path: Path):
    root, base = base_repo(tmp_path)
    (root / "tooling/checks/banned_patterns.json").unlink()
    write(root / "tooling/checks/locales_allow.json", "{}")
    report = "\n".join(policy.restore(root, base))
    assert (root / "tooling/checks/banned_patterns.json").read_text(encoding="utf-8") == "{}"
    assert "banned_patterns.json: restored" in report
    assert "locales_allow.json: new in this change, kept" in report
    assert "pins.py" not in report


def test_unknown_base_is_setup_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    root, _ = base_repo(tmp_path)
    assert policy.main(["--base", "0" * 40, "--root", str(root)]) == 2
    assert "cannot list" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("tooling/checks/similarity_allow.json", True),
        ("tooling/checks/reference.json", True),
        ("tooling/checks/pins_meta.json", True),
        ("tooling/checks/pins.py", False),
        ("tooling/checks/tests/x_allow.json", False),
    ],
)
def test_is_policy(path: str, expected: bool):
    assert policy.is_policy(path) is expected
