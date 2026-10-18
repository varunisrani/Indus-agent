"""Live Docker lane (P11-21): the contract against a real daemon, marker ``docker``.

Runs only where a Docker daemon answers and ``INDUS_LIVE_DOCKER_IMAGE`` (see ``_live``) is
present; skipped everywhere else. Checks the hardened run, that no process on the machine shows a
passthrough value on its command line while the client runs (R02.7, the ``/proc/<pid>/cmdline``
check), and a background job that notifies when it ends.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest

from indus.backends.contract import ExecResult
from indus.backends.docker import probe, reaper
from indus.backends.process_notify import Deliveries
from indus.backends.processes import ProcessRegistry, SpawnRequest
from indus.backends.registry import EnvironmentRegistry
from indus.foundation.scope import Scope
from tests.fakes.agent import DictConfig
from tests.fakes.terminal import FakeSecrets, Inbox, forge_token
from tests.integration.backends._live import (
    LIVE_IMAGE,
    REAL,
    cmdlines,
    docker_problem,
    poll_until_exit,
)

pytestmark = pytest.mark.docker

NAME = "LIVE_TOKEN"


@pytest.fixture
def live_scope(scope: Scope) -> Iterator[Scope]:
    problem = docker_problem(LIVE_IMAGE)
    if problem is not None:
        pytest.skip(problem)
    probe.clear_caches()
    reaper.reset_for_tests()
    config = {
        "terminal": {
            "backend": "docker",
            "docker_image": LIVE_IMAGE,
            "container_persistent": False,
            "docker_orphan_reaper": False,
            "env_passthrough": [NAME],
            "timeout": 60,
        }
    }
    yield scope.with_config(DictConfig(config))
    probe.clear_caches()


def test_hardened_run_and_values_off_the_command_line(live_scope: Scope, tmp_path: Path) -> None:
    value = forge_token()
    scope = live_scope.with_secrets(FakeSecrets({NAME: value}))

    async def go() -> tuple[ExecResult, ExecResult, list[str]]:
        registry = EnvironmentRegistry()
        try:
            _, env = await registry.acquire(scope, task_id="live")
            hello = await env.execute("echo hello && id -u")
            running = asyncio.ensure_future(env.execute(f'sleep 2; printf %s "${NAME}"'))
            await REAL.sleep(1)
            seen = cmdlines()
            return hello, await running, seen
        finally:
            await registry.shutdown(fence=False)

    hello, secret, seen = asyncio.run(go())
    assert hello.output.startswith("hello\n") and hello.returncode == 0
    assert secret.output == value
    assert not any(value in line for line in seen)  # R02.7 on the live client


def test_background_job_notifies_on_completion(live_scope: Scope) -> None:
    async def go() -> list[str]:
        registry = EnvironmentRegistry()
        deliveries = Deliveries()
        inbox = Inbox()
        deliveries.register(str(live_scope.home), "s1", inbox)
        processes = ProcessRegistry(deliveries)
        try:
            key, env = await registry.acquire(live_scope, task_id="live")
            record = await processes.spawn(
                SpawnRequest(
                    "sleep 1 && echo done",
                    live_scope,
                    "s1",
                    str(key),
                    "/",
                    {},
                    notify_on_exit=True,
                    environment=env,
                )
            )
            await poll_until_exit(record)
            assert record.exited and record.exit_code == 0 and "done" in record.buffer
            return [e.kind for e in inbox.events]
        finally:
            await processes.shutdown()
            await registry.shutdown(fence=False)

    assert "completion" in asyncio.run(go())
