"""Fixtures of the release-lane end-to-end tests (``world.py`` has the helpers)."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.e2e.upgrade.world import ModelServer, Rehearsal, running_model_server, source_files


@pytest.fixture
def model_server() -> Iterator[ModelServer]:
    with running_model_server() as server:
        yield server


def _world(base: Path, *, smoke: bool) -> Iterator[Rehearsal]:
    world = Rehearsal(base, source_files(), smoke=smoke)
    try:
        yield world
    finally:
        # Releases, checkout and environments are large: never leave them to the basetemp.
        shutil.rmtree(world.base, ignore_errors=True)


@pytest.fixture
def rehearsal(tmp_path: Path) -> Iterator[Rehearsal]:
    yield from _world(tmp_path / "r", smoke=False)


@pytest.fixture
def smoke_world(tmp_path: Path) -> Iterator[Rehearsal]:
    yield from _world(tmp_path / "s", smoke=True)
