"""Fixtures shared by every Python test.

Every test runs with ``INDUS_HOME`` in its own temp dir and without any inherited ``INDUS_*``
variable, so nothing a test does can reach a real home.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.fakes.clock import FakeClock
from tests.fakes.model import Script, load_script

FAKE_CLOCK_START = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)


def isolate_indus_env(monkeypatch: pytest.MonkeyPatch, home: Path) -> Path:
    """Drop every ``INDUS_*`` variable and point ``INDUS_HOME`` at a fresh ``home``."""
    for name in [n for n in os.environ if n.startswith("INDUS_")]:
        monkeypatch.delenv(name)
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("INDUS_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return isolate_indus_env(monkeypatch, tmp_path / "home")


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock(FAKE_CLOCK_START)


@pytest.fixture
def fake_model_script() -> Callable[[str], Script]:
    """Load a script from ``tests/fixtures/model_scripts/`` by name (``"hello"``) or path."""
    return load_script
