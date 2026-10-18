"""PR body check, including the CI entry point."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tooling.checks import prbody
from tooling.checks.common import DEFAULT_ROOT, Context


@pytest.mark.parametrize(
    "body",
    [
        "Reference: agent/loop.py, agent/retry.py\nTask note: docs/notes/P01/loop.md\n",
        "## Summary\n- **Reference:** none (docs-only change)\n- **Task note:** none (typo fix)\n",
    ],
)
def test_good_bodies(body: str):
    assert prbody.check_body(body) == []


@pytest.mark.parametrize(
    ("body", "needle"),
    [
        ("Task note: docs/notes/a.md\n", "Reference:"),
        ("Reference: a.py\n", "Task note:"),
        ("Reference:\nTask note: x.md\n", "empty"),
        ("Reference: none\nTask note: x.md\n", "needs a reason"),
    ],
)
def test_bad_bodies(body: str, needle: str):
    problems = prbody.check_body(body)
    assert problems and needle in problems[0]


def test_no_body_is_skipped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.delenv("PR_BODY", raising=False)
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    assert prbody.run(Context(root=tmp_path)).code == 0


def test_event_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.delenv("PR_BODY", raising=False)
    event = tmp_path / "event.json"
    event.write_text('{"pull_request": {"body": "Reference: a.py"}}', encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    assert prbody.run(Context(root=tmp_path)).code == 1


def test_cli_entry_point(tmp_path: Path):
    good = tmp_path / "good.md"
    good.write_text("Reference: none (new file)\nTask note: docs/notes/x.md\n", encoding="utf-8")
    bad = tmp_path / "bad.md"
    bad.write_text("nothing here\n", encoding="utf-8")
    cmd = [sys.executable, "-m", "tooling.checks.prbody", "--body-file"]
    ok = subprocess.run([*cmd, str(good)], cwd=DEFAULT_ROOT, capture_output=True, text=True)
    fail = subprocess.run([*cmd, str(bad)], cwd=DEFAULT_ROOT, capture_output=True, text=True)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert fail.returncode == 1
    assert "Reference:" in fail.stdout
