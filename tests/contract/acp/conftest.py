"""Fixtures of the ACP handler suites: the profile's store, opened for writing by the test (the
hosts read it through read-only connections, as they do in production)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from indus.foundation.scope import Scope
from indus.state import registry
from indus.state.registry import StoreHandle


@pytest.fixture
def store(scope: Scope) -> Iterator[StoreHandle]:
    handle = registry.open_unshared(scope)
    yield handle
    handle.close()
