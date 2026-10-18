"""Live SSH lane (P11-21): the contract against a real sshd in a throwaway container.

Markers ``docker`` and ``network``; runs only with ``INDUS_LIVE_SSH=1`` and a reachable Docker
daemon (it builds ``tests/fixtures/backends/sshd``, which needs the base image or the network).
The OpenSSH client gets a wrapper first on PATH that keeps its known-hosts file in the test's
temp dir, so the lane never writes into the real home.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from indus.backends.contract import Environment, ExecResult
from indus.backends.process_notify import Deliveries
from indus.backends.processes import ProcessRegistry, SpawnRequest
from indus.backends.registry import EnvironmentRegistry, EnvKey
from indus.foundation.scope import Scope
from tests.fakes.agent import DictConfig
from tests.fakes.terminal import FakeSecrets, Inbox, forge_token
from tests.integration.backends._live import (
    LIVE_SSH,
    REAL,
    cmdlines,
    docker,
    docker_problem,
    poll_until_exit,
)

pytestmark = [pytest.mark.docker, pytest.mark.network]

IMAGE = "indus-test-sshd:live"
FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "backends" / "sshd"
NAME = "LIVE_TOKEN"


def _wrappers(bin_dir: Path, known_hosts: Path) -> None:
    bin_dir.mkdir(parents=True)
    for tool in ("ssh", "scp"):
        real = shutil.which(tool)
        assert real, f"{tool} is needed for the ssh lane"
        wrapper = bin_dir / tool
        wrapper.write_text(f'#!/bin/sh\nexec {real} -o UserKnownHostsFile={known_hosts} "$@"\n')
        wrapper.chmod(0o755)


@pytest.fixture
def sshd(scope: Scope, tmp_path: Path) -> Iterator[Scope]:
    if not LIVE_SSH:
        pytest.skip("set INDUS_LIVE_SSH=1 to run the live ssh lane")
    problem = docker_problem()
    if problem is not None:
        pytest.skip(problem)
    if shutil.which("ssh") is None or shutil.which("ssh-keygen") is None:
        pytest.skip("the OpenSSH client is not installed")
    key = tmp_path / "id_live"
    subprocess.run(  # noqa: S603 - fixed argv
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True
    )
    assert docker("build", "-t", IMAGE, str(FIXTURE), timeout=600).returncode == 0
    started = docker(
        "run", "-d", "--rm", "-p", "127.0.0.1::22",
        "-e", f"AUTHORIZED_KEY={key.with_suffix('.pub').read_text().strip()}", IMAGE,
    )  # fmt: skip
    assert started.returncode == 0, started.stderr
    cid = started.stdout.strip()
    runtime = tempfile.mkdtemp(prefix="isl", dir="/tmp")  # noqa: S108 - short socket dir
    try:
        port = docker("port", cid, "22/tcp").stdout.strip().rsplit(":", 1)[-1]
        bin_dir = tmp_path / "bin"
        _wrappers(bin_dir, tmp_path / "known_hosts")
        config = {
            "terminal": {
                "backend": "ssh",
                "ssh_host": "127.0.0.1",
                "ssh_port": int(port),
                "ssh_user": "indus",
                "ssh_key": str(key),
                "env_passthrough": [NAME],
                "timeout": 60,
            }
        }
        path = f"{bin_dir}{os.pathsep}{scope.env.get('PATH', '')}"
        view = scope.env.with_overlay({"PATH": path, "XDG_RUNTIME_DIR": runtime})
        yield scope.with_config(DictConfig(config)).with_env(view)
    finally:
        docker("rm", "-f", cid)
        shutil.rmtree(runtime, ignore_errors=True)


async def _acquire_when_up(
    registry: EnvironmentRegistry, scope: Scope
) -> tuple[EnvKey, Environment]:
    for _ in range(30):  # sshd needs a moment after the container starts
        try:
            return await registry.acquire(scope, task_id="live")
        except Exception:
            await REAL.sleep(1)
    return await registry.acquire(scope, task_id="live")


def test_runs_remotely_with_values_off_the_command_line(sshd: Scope) -> None:
    value = forge_token()
    scope = sshd.with_secrets(FakeSecrets({NAME: value}))

    async def go() -> tuple[ExecResult, ExecResult, list[str]]:
        registry = EnvironmentRegistry()
        try:
            _, env = await _acquire_when_up(registry, scope)
            who = await env.execute("whoami && cd /tmp && export KEPT=1")
            running = asyncio.ensure_future(env.execute(f'sleep 2; printf %s "${NAME}"'))
            await REAL.sleep(1)
            seen = cmdlines()
            return who, await running, seen
        finally:
            await registry.shutdown(fence=False)

    who, secret, seen = asyncio.run(go())
    assert who.output.strip() == "indus" and who.cwd_observed == "/tmp"
    assert secret.output == value
    assert not any(value in line for line in seen)  # R02.7: SendEnv name only


def test_background_job_notifies_on_completion(sshd: Scope) -> None:
    async def go() -> list[str]:
        registry = EnvironmentRegistry()
        deliveries = Deliveries()
        inbox = Inbox()
        deliveries.register(str(sshd.home), "s1", inbox)
        processes = ProcessRegistry(deliveries)
        try:
            key, env = await _acquire_when_up(registry, sshd)
            record = await processes.spawn(
                SpawnRequest(
                    "sleep 1 && echo done",
                    sshd,
                    "s1",
                    str(key),
                    "~",
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
