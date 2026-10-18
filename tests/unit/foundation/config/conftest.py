"""Fixtures for the config tests: the shared profile scope with this suite's clock and env."""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from indus.foundation.config import loader
from indus.foundation.scope import EnvView, Scope
from tests.fakes.clock import FakeClock


@pytest.fixture(autouse=True)
def _fresh_loader() -> Iterator[None]:
    loader.reset()
    yield
    loader.reset()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 3, 1, 12, 0, tzinfo=UTC))


@pytest.fixture
def cfg_scope(scope: Scope, clock: FakeClock) -> Scope:
    """The shared ``scope`` fixture (``work`` profile) with an empty env view and ``clock``."""
    scope.home.chmod(0o700)
    return dataclasses.replace(scope.with_env(EnvView()), clock=clock)


@pytest.fixture
def cfg_home(cfg_scope: Scope) -> Path:
    return cfg_scope.home
