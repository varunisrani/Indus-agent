"""The execution-contract suite runs once per registered backend (P05-04).

``contract_entries`` lists the backend rows under test. Later backends (containers and SSH in P11,
cloud sandboxes in P21) add their row here, guarded by a skip when their daemon or credentials
are missing; every test in ``test_execution_contract.py`` then runs against them unchanged.

Each backend answers the same small command set the way bash does. The fake backend runs no shell,
so the commands real bash would interpret are scripted for it below.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from indus.backends.contract import Environment
from indus.backends.registry import (
    BackendEntry,
    CreateRequest,
    EnvironmentRegistry,
    default_entries,
)
from indus.backends.wait import FOREGROUND
from indus.foundation.scope import Scope
from tests.fakes.backend.environment import FakeEnvironment, Rule

# Output a real shell prints for these commands; the fake backend is scripted with the same.
ODD_COMMAND = "printf '\\377\\376odd\\000bytes\\r\\n\\033[0m'"
ODD_OUTPUT = "��odd\x00bytes\r\n\x1b[0m"
BIG_COMMAND = "head -c 60000 /dev/zero | tr '\\0' x"
BIG_OUTPUT = "x" * 60_000


async def _create_fake(request: CreateRequest) -> Environment:
    cwd = str(request.overrides.get("cwd") or "/")
    env = FakeEnvironment(cwd=cwd, timeout=60, capture_home=request.scope.home)
    env.script(ODD_COMMAND, Rule(output=ODD_OUTPUT))
    env.script(BIG_COMMAND, Rule(output=BIG_OUTPUT))
    await env.start()
    return env


def contract_entries() -> list[BackendEntry]:
    """Every backend the contract suite covers."""
    fake = FakeEnvironment()
    return [*default_entries(), BackendEntry("fake", _create_fake, fake.flags)]


BACKENDS = [entry.name for entry in contract_entries()]


@dataclass
class Backend:
    name: str
    scope: Scope
    workdir: str  # an existing directory with a ``sub`` directory inside

    @asynccontextmanager
    async def open(self) -> AsyncIterator[Environment]:
        registry = EnvironmentRegistry(contract_entries())
        _, env = await registry.acquire(
            self.scope,
            task_id="contract",
            overrides={"backend": self.name, "cwd": self.workdir},
        )
        try:
            yield env
        finally:
            await registry.shutdown(fence=False)


@pytest.fixture(params=BACKENDS)
def backend(request: pytest.FixtureRequest, scope: Scope, tmp_path: Path) -> Iterator[Backend]:
    work = tmp_path / "work"
    (work / "sub").mkdir(parents=True)
    FOREGROUND.reset_for_tests()
    yield Backend(str(request.param), scope, os.path.realpath(work))
    FOREGROUND.reset_for_tests()
