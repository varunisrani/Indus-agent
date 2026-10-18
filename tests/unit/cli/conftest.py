"""CLI runs end like a process does: the terminal runtime is shut down and the foreground fence
goes up. Tests in one interpreter get a fresh runtime and an open fence after each test."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from indus.backends.wait import FOREGROUND
from indus.tools.builtin import terminal


@pytest.fixture(autouse=True)
def fresh_terminal_runtime() -> Iterator[None]:
    terminal._shared.clear()  # pyright: ignore[reportPrivateUsage]
    yield
    terminal._shared.clear()  # pyright: ignore[reportPrivateUsage]
    FOREGROUND.reset_for_tests()
