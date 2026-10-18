"""Options for the golden-file tests.

``--update-golden`` rewrites the checked-in files from the current code instead of failing on a
difference. Pytest reads this option only when the run names a path under ``tests/golden``:

    uv run pytest tests/golden/prompt_surface --update-golden

A regenerated fixture lands together with a note saying why the output changed.
"""

from __future__ import annotations

import pytest

UPDATE_GOLDEN = "--update-golden"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        UPDATE_GOLDEN,
        action="store_true",
        default=False,
        help="rewrite golden files from the current output instead of comparing against them",
    )


@pytest.fixture
def update_golden(request: pytest.FixtureRequest) -> bool:
    return bool(request.config.getoption(UPDATE_GOLDEN, default=False))
