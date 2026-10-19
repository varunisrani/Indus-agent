"""Shared fixtures of the cron unit tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.unit.cron import world


@pytest.fixture(autouse=True)
def _close_worlds() -> Iterator[None]:
    """Close the cron contexts a test opened with ``world.make_world``."""
    yield
    while world.OPEN:
        world.OPEN.pop().close()
