"""Fixtures shared by every Python test.

Every test runs with ``INDUS_HOME`` in its own temp dir and without any inherited ``INDUS_*``
variable, so nothing a test does can reach a real home.
"""

from __future__ import annotations

import gc
import logging
import os
import sys
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from indus.foundation.scope import EnvView, Scope, make_scope
from indus.foundation.secrets import open_secret_store
from tests import platform_marks, self_chat_guard
from tests.fakes.clock import FakeClock
from tests.fakes.model import Script, load_script


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Host gating (P23-01, R10.21): bad ``platforms`` marks are a usage error; tests this host
    does not match are deselected (`tests.platform_marks`)."""
    platform_marks.apply(config, items)


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
def restore_log_levels() -> Iterator[None]:
    """Put back every logger's level after each test. A host's log setup (``indus.foundation.log``,
    run by CLI commands tests invoke in-process) lowers the root logger to the file level; left
    behind, libraries' INFO lines (a test client's request line with its query string) reach
    every later test's ``caplog`` (P24 integration: ``test_tickets_never_reach_a_log_record``
    failed only after such a test)."""
    manager = logging.Logger.manager
    root = logging.getLogger()
    before = root.level
    levels = {
        name: logger.level
        for name, logger in list(manager.loggerDict.items())
        if isinstance(logger, logging.Logger)
    }
    yield
    root.setLevel(before)
    for name, logger in list(manager.loggerDict.items()):
        if isinstance(logger, logging.Logger):
            logger.setLevel(levels.get(name, logging.NOTSET))


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


@pytest.fixture(autouse=True)
def reopen_after_a_host_exit() -> Iterator[None]:
    """Undo what a simulated process exit leaves behind, after every test.

    A host's exit path (``indus cron tick``, ``indus -z``, the gateway daemon's stop, the TUI
    bridge) shuts the process's terminal runtime down, which closes the shared environment
    registry and raises the one-way foreground fence (``FOREGROUND``). In a real process nothing
    runs after that; in this one interpreter every later test would get "[host is exiting:
    command not started]" (rc 130) from its first command. So the fence is lowered and a closed
    shared runtime is dropped once the test is over. Modules not imported yet are left alone.
    """
    yield
    wait = sys.modules.get("indus.backends.wait")
    terminal = sys.modules.get("indus.tools.builtin.terminal")
    if terminal is not None:
        shared = terminal._shared  # pyright: ignore[reportPrivateUsage]
        found = shared.get("default")
        if found is not None and found.environments._closed:  # pyright: ignore[reportPrivateUsage]
            del shared["default"]
    if wait is not None and wait.FOREGROUND.fenced:
        wait.FOREGROUND.reset_for_tests()


@pytest.fixture(autouse=True)
def drop_platform_target_rules() -> Iterator[None]:
    """A gateway router installs the per-platform target rules (P16-32) process-wide; remove them
    after every test so a test that parses targets without a router sees P14's plain grammar
    whatever ran before it. Not imported yet → nothing to do."""
    yield
    platform_targets = sys.modules.get("indus.gateway.adapters.targets")
    if platform_targets is not None:
        platform_targets.uninstall()


@pytest.fixture(autouse=True)
def no_self_chat_holder_left() -> Iterator[None]:
    """A test may not leave a ``DeliveryRouter`` open: its hold on the process-wide ``self-chat``
    deliverer would outlive the test and break later ones that expect none (``self_chat_guard``).
    The leaking test fails at teardown; its holds are released first."""
    before = self_chat_guard.holders()
    yield
    leaked = self_chat_guard.release_leaked(before)
    if leaked:
        pytest.fail(
            f"{leaked} DeliveryRouter(s) left open: close() each router the test builds",
            pytrace=False,
        )


@pytest.fixture(autouse=True)
def no_language_servers(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Hosts built in a test join no language-server tier (P24-15/16 wiring): the venv carries
    ``pyright-langserver``, and a test that edits a file inside a git work tree must not start it.
    A test that wants the tier passes ``LspUpkeep(scope, settings=...)``. Whatever tier a test left
    installed is dropped afterwards."""
    from indus.service import lsp_host
    from indus.tools.lsp import diagnostics
    from indus.tools.lsp.manager import LspSettings

    monkeypatch.setattr(lsp_host, "settings_for", lambda _scope: LspSettings(enabled=False))
    yield
    lsp_host._TIER.clear()  # pyright: ignore[reportPrivateUsage]
    diagnostics.install_active(None)
