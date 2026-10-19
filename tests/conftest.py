"""Fixtures shared by every Python test.

Every test runs with ``INDUS_HOME`` in its own temp dir and without any inherited ``INDUS_*``
variable, so nothing a test does can reach a real home.
"""

from __future__ import annotations

import gc
import os
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from indus.foundation.scope import EnvView, Scope, make_scope
from indus.foundation.secrets import open_secret_store
from tests.fakes.clock import FakeClock
from tests.fakes.model import Script, load_script


def pytest_collection_finish(session: pytest.Session) -> None:
    """Before wall-clock (`perf`) tests, move the collected test objects out of the GC's reach.

    Collecting the whole suite leaves ~300k objects behind; one full collection over them pauses
    the interpreter for ~200 ms, which lands inside the D-04.1 heartbeat windows and fails them
    for a reason that has nothing to do with the code under test.
    """
    if any(item.get_closest_marker("perf") for item in session.items):
        gc.collect()
        gc.freeze()


FAKE_CLOCK_START = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
# P02-21: the stress test's window in seconds. Read at import, before ``isolated_home`` drops
# every ``INDUS_*`` variable; ``INDUS_STRESS_SECONDS=10`` shortens it for local runs.
STRESS_SECONDS = float(os.environ.get("INDUS_STRESS_SECONDS") or 60)


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
def scope(isolated_home: Path, fake_clock: FakeClock) -> Scope:
    """A ``work`` profile under the isolated root, with a fake clock (P01-06).

    The env view is a snapshot of the isolated process env with ``INDUS_HOME`` pinned to the
    profile. Secrets use the owner-only file backend (never the OS keychain); config starts unset
    and is read with ``config.load(scope)``.
    """
    home = isolated_home / "profiles" / "work"
    home.mkdir(parents=True, exist_ok=True)
    (home / "profile.yaml").touch()
    env = EnvView.from_process().with_overlay({"INDUS_HOME": str(home)})
    scope = make_scope(home, env=env, clock=fake_clock)
    return scope.with_secrets(open_secret_store(home, scope.profile_id, mode="file"))


@pytest.fixture
def fake_model_script() -> Callable[[str], Script]:
    """Load a script from ``tests/fixtures/model_scripts/`` by name (``"hello"``) or path."""
    return load_script


@pytest.fixture(autouse=True)
def no_os_keychain(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No test reaches the real OS keychain: the system lookup yields keyring's "fail" backend,
    so ``auto`` falls back to the owner-only file. Tests that need a keychain pass their own."""
    from keyring.backends import fail

    from indus.foundation import secrets

    secrets.reset_probe_cache()
    monkeypatch.setattr(secrets, "_system_keyring", fail.Keyring)
    yield
    secrets.reset_probe_cache()
