"""Every execute_code test ends with the kernel pools closed and no child process left."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.unit.tools.builtin.code._rig import live_children, no_stray


@pytest.fixture(autouse=True)
def no_stray_processes() -> Iterator[None]:
    yield from no_stray(live_children())
