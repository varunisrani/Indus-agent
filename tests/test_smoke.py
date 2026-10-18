import subprocess
import sys

import pytest

import indus
from indus.cli import main


def test_version_string() -> None:
    assert indus.__version__ == "0.0.1"


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == "indus 0.0.1"


def test_no_args_prints_placeholder(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "not implemented yet" in out
    assert out.count("\n") == 1


def test_module_entry_point() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "indus", "--version"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "indus 0.0.1"
