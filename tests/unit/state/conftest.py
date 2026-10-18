"""Fixtures for the store tests: a self-advancing fake clock, a scripted connection, a store.

Every test here runs with the linked SQLite version pinned to one outside the WAL-reset bug
range, so the results do not depend on the host's SQLite build; ``test_journal.py`` passes
explicit versions where the gate itself is under test.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.state import guards, journal, readpool, registry
from tests.conftest import FAKE_CLOCK_START
from tests.fakes.clock import FakeClock

PINNED_SQLITE = (3, 51, 3)


class AutoClock(FakeClock):
    """A fake clock whose blocking sleep moves time itself and records every pause."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.sleeps: list[float] = []

    def sleep_sync(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("sleep seconds must be >= 0")
        self.sleeps.append(seconds)
        self.advance(seconds)


@pytest.fixture
def fake_clock() -> AutoClock:
    return AutoClock(FAKE_CLOCK_START)


@pytest.fixture(autouse=True)
def _store_globals(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(journal, "linked_sqlite_version", lambda: PINNED_SQLITE)
    journal.reset_once_logs()
    guards.reset_health()
    readpool.reset_fd_cache()
    yield
    registry.close_all()
    guards.reset_health()
    journal.reset_once_logs()
    readpool.reset_fd_cache()


@pytest.fixture
def store(scope: Scope) -> Iterator[registry.StoreHandle]:
    """An open, private store handle in the test's ``work`` profile."""
    handle = registry.open_unshared(scope)
    yield handle
    handle.close()


def locked_error(text: str = "database is locked") -> sqlite3.OperationalError:
    exc = sqlite3.OperationalError(text)
    exc.sqlite_errorcode = sqlite3.SQLITE_BUSY  # type: ignore[attr-defined]
    exc.sqlite_errorname = "SQLITE_BUSY"  # type: ignore[attr-defined]
    return exc


@dataclass
class Rule:
    pattern: re.Pattern[str]
    make: Callable[[], BaseException]
    while_: Callable[[], bool]
    hits: int = 0


@dataclass
class FakeConnection:
    """Wraps a real connection; statements matching a rule raise while the rule says so."""

    real: sqlite3.Connection
    rules: list[Rule] = field(default_factory=lambda: [])
    statements: list[str] = field(default_factory=lambda: [])

    def fail(
        self,
        pattern: str,
        make: Callable[[], BaseException],
        *,
        times: int | None = None,
        while_: Callable[[], bool] | None = None,
    ) -> Rule:
        if while_ is None:
            remaining = [times if times is not None else 1_000_000]

            def countdown() -> bool:
                remaining[0] -= 1
                return remaining[0] >= 0

            while_ = countdown
        rule = Rule(re.compile(pattern, re.IGNORECASE), make, while_)
        self.rules.append(rule)
        return rule

    def execute(self, sql: str, parameters: Any = (), /) -> Any:
        self.statements.append(sql)
        for rule in self.rules:
            if rule.pattern.search(sql) and rule.while_():
                rule.hits += 1
                raise rule.make()
        return self.real.execute(sql, parameters)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.real, name)


def make_scope_store_path(scope: Scope) -> Path:
    return scope.home / "state.db"
