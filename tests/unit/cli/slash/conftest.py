"""Fixtures for the slash-command tests: a private store and a clean approval state."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from indus.foundation.scope import Scope
from indus.safety.approvals import APPROVALS, session_key
from indus.state import registry


@pytest.fixture
def store(scope: Scope) -> Iterator[registry.StoreHandle]:
    handle = registry.open_unshared(scope)
    yield handle
    handle.close()
    registry.close_all()


@pytest.fixture(autouse=True)
def _clean_approvals(scope: Scope) -> Iterator[None]:
    yield
    for sid in ("s1", "s2", "new1", "new2"):
        APPROVALS.clear_session(session_key(scope, sid))
