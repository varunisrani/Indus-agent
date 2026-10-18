import subprocess
import sys

import pytest

import indus
from indus.cli.entry import main


def test_version_string() -> None:
    assert indus.__version__ == "0.0.1"


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--version"], env={}) == 0
    assert capsys.readouterr().out.splitlines()[0] == "Indus Agent 0.0.1"


def test_module_entry_point() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "indus", "--version"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0
    assert result.stdout.splitlines()[0] == "Indus Agent 0.0.1"
