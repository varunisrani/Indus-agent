"""The no-sleep rule: ruff TID251 bans time.sleep / asyncio.sleep outside the clock modules."""

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]


def _ruff(*paths: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--no-cache",
            "--config",
            str(REPO / "pyproject.toml"),
            "--select",
            "TID251",
            "--output-format",
            "concise",
            *map(str, paths),
        ],
        capture_output=True,
        text=True,
        cwd=REPO,
        check=False,
    )


@pytest.mark.parametrize(
    "source",
    [
        "import time\n\n\ndef test_x():\n    time.sleep(1)\n",
        "from time import sleep\n\n\ndef test_x():\n    sleep(1)\n",
        "import asyncio\n\n\nasync def test_x():\n    await asyncio.sleep(1)\n",
    ],
)
def test_sleep_in_a_test_file_is_flagged(tmp_path: Path, source: str) -> None:
    target = tmp_path / "test_sleepy.py"
    target.write_text(source, encoding="utf-8")
    result = _ruff(target)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "TID251" in result.stdout
    assert "inject a Clock" in result.stdout


def test_fake_clock_module_is_exempt() -> None:
    result = _ruff(REPO / "tests" / "fakes" / "clock.py")
    assert result.returncode == 0, result.stdout + result.stderr
