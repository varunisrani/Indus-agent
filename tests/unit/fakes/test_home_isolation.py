import os
from pathlib import Path

import pytest

from tests.conftest import isolate_indus_env


def test_home_is_under_tmp_path(tmp_path: Path, isolated_home: Path) -> None:
    home = Path(os.environ["INDUS_HOME"])
    assert home == isolated_home
    assert home.is_relative_to(tmp_path)
    assert home.is_dir()
    assert [n for n in os.environ if n.startswith("INDUS_")] == ["INDUS_HOME"]


def test_inherited_indus_vars_are_cleared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INDUS_LEAK", "from-the-shell")
    monkeypatch.setenv("INDUS_HOME", "/somewhere/real")
    home = isolate_indus_env(monkeypatch, tmp_path / "other")
    assert "INDUS_LEAK" not in os.environ
    assert os.environ["INDUS_HOME"] == str(home)
    assert home.is_dir()
