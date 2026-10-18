"""The ``python -m tooling.checks`` runner."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from tooling.checks import NAMES
from tooling.checks.__main__ import main
from tooling.checks.common import DEFAULT_ROOT, Context, Result, resolve_ref


def run_cli(*args: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    import os

    env = {**os.environ, **(env_extra or {})}
    env.pop("PR_BODY", None)
    env.pop("GITHUB_EVENT_PATH", None)
    return subprocess.run(
        [sys.executable, "-m", "tooling.checks", *args],
        cwd=DEFAULT_ROOT,
        capture_output=True,
        text=True,
        env=env,
    )


def test_unknown_check_is_setup_problem(capsys):
    assert main(["nope"]) == 2


def test_worst_code_and_summary(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = '" + "her" + "mes'\n", encoding="utf-8")
    body = tmp_path / "body.md"
    body.write_text("Reference: none (test)\nTask note: none (test)\n", encoding="utf-8")
    proc = run_cli("--root", str(tmp_path), "banned", "prbody", "--body-file", str(body))
    assert proc.returncode == 1
    assert "banned       FAIL" in proc.stdout and "prbody       ok" in proc.stdout


def test_json_output_and_missing_ref(tmp_path: Path):
    proc = run_cli("--root", str(tmp_path), "--ref", str(tmp_path / "none"), "--json", "assets")
    data = json.loads(proc.stdout)
    assert proc.returncode == 0 and data["checks"][0]["warnings"]
    proc = run_cli(
        "--root", str(tmp_path), "--ref", str(tmp_path / "none"), "--require-ref", "assets"
    )
    assert proc.returncode == 2


def test_crashing_check_is_setup(monkeypatch):
    from tooling.checks import __main__ as runner

    def boom(ctx: Context) -> Result:
        raise RuntimeError("bad")

    monkeypatch.setattr("tooling.checks.prbody.run", boom)
    result = runner.run_check("prbody", Context())
    assert result.code == 2 and "crashed" in result.violations[0]


def test_ref_resolution_order(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("INDUS_REF", str(tmp_path / "env"))
    assert resolve_ref(str(tmp_path / "flag")) == tmp_path / "flag"
    assert resolve_ref(None) == tmp_path / "env"


def test_every_check_runs_alone():
    for name in NAMES:
        proc = subprocess.run(
            [sys.executable, "-m", f"tooling.checks.{name}", "--help"],
            cwd=DEFAULT_ROOT,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, (name, proc.stderr)
