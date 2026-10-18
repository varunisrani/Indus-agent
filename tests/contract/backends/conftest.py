"""The execution-contract suite runs once per registered backend (P05-04, P11-21).

``contract_entries`` lists the backend rows under test: the local backend with real bash, Docker
over the fake ``docker`` client (``tests/fakes/backend/docker_cli.py``), SSH over the fake ``ssh``
and ``scp`` clients (``tests/fakes/backend/ssh_cli.py``) and the scripted fake backend. Every test
in ``test_execution_contract.py`` runs against each of them unchanged; the cases that only make
sense for a backend that is not this machine are in ``test_docker_ssh_contract.py``. Cloud
sandboxes (P21) add their rows here. The same cases against a real Docker daemon and a real sshd
are the live lanes in ``tests/integration/backends/`` (markers ``docker`` and ``network``).

The fake clients run "container" and "remote" commands on this machine, so each test points the
Docker sandbox temp dir and the SSH remote temp dir at its own directory: nothing lands in the
shared /tmp.

Each backend answers the same small command set the way bash does. The fake backend runs no shell,
so the commands real bash would interpret are scripted for it below.
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import tempfile
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
from tests.fakes.agent import DictConfig
from tests.fakes.backend.docker_cli import FakeDocker
from tests.fakes.backend.environment import FakeEnvironment, Rule
from tests.fakes.backend.ssh_cli import FakeSsh, install, redirect_remote_tmp
from tests.fakes.terminal import FakeSecrets

# Output a real shell prints for these commands; the fake backend is scripted with the same.
ODD_COMMAND = "printf '\\377\\376odd\\000bytes\\r\\n\\033[0m'"
ODD_OUTPUT = "��odd\x00bytes\r\n\x1b[0m"
BIG_COMMAND = "head -c 60000 /dev/zero | tr '\\0' x"
BIG_OUTPUT = "x" * 60_000

DOCKER_IMAGE = "registry.test/indus-sandbox:contract"
SSH_HOST = "box.test"
SSH_USER = "dev"
REMOTE_ROWS = ("docker", "ssh")


async def _create_fake(request: CreateRequest) -> Environment:
    cwd = str(request.overrides.get("cwd") or "/")
    env = FakeEnvironment(cwd=cwd, timeout=60, capture_home=request.scope.home)
    env.script(ODD_COMMAND, Rule(output=ODD_OUTPUT))
    env.script(BIG_COMMAND, Rule(output=BIG_OUTPUT))
    await env.start()
    return env


def _no_reaper(*_: object) -> None:
    return None


@dataclass
class Rig:
    """The fake clients of one test (installed only for the row under test)."""

    root: Path
    docker: FakeDocker | None = None
    ssh: FakeSsh | None = None
    runtime_dir: str | None = None
    opened: list[Environment] = field(default_factory=lambda: [])

    def entries(self) -> list[BackendEntry]:
        """The rows of ``contract_entries`` whose factories use this rig's fake clients."""
        rows: list[BackendEntry] = []
        for entry in contract_entries():
            if entry.name == "docker":
                rows.append(dataclasses.replace(entry, factory=self._create_docker))
            else:
                rows.append(entry)
        return rows

    async def _create_docker(self, request: CreateRequest) -> Environment:
        from indus.backends.docker.environment import open_docker

        assert self.docker is not None, "the docker row needs the fake client"
        env = await open_docker(
            request, runner=self.docker.runner, os_name="linux", reaper=_no_reaper
        )
        self.opened.append(env)
        return env


def contract_entries() -> list[BackendEntry]:
    """Every backend the contract suite covers (the registry's rows plus the fake)."""
    fake = FakeEnvironment()
    rows = [e for e in default_entries() if e.name in ("local", *REMOTE_ROWS)]
    return [*rows, BackendEntry("fake", _create_fake, fake.flags)]


BACKENDS = [entry.name for entry in contract_entries()]


@dataclass
class Backend:
    name: str
    scope: Scope
    workdir: str  # an existing directory with a ``sub`` directory inside
    rig: Rig
    base: Scope = field(repr=False)
    terminal: dict[str, Any] = field(default_factory=lambda: {})

    def configure(self, *, secrets: Mapping[str, str] | None = None, **terminal: Any) -> None:
        """Add ``terminal.*`` settings (and secrets) to this backend's scope."""
        self.terminal.update(terminal)
        self.scope = _row_scope(self.base, self.name, self.rig, self.terminal, secrets)

    def registry(self) -> EnvironmentRegistry:
        return EnvironmentRegistry(self.rig.entries())

    def overrides(self) -> dict[str, Any]:
        return {"backend": self.name, "cwd": self.workdir}

    @asynccontextmanager
    async def open(self) -> AsyncIterator[Environment]:
        registry = self.registry()
        _, env = await registry.acquire(self.scope, task_id="contract", overrides=self.overrides())
        try:
            yield env
        finally:
            await registry.shutdown(fence=False)


def _row_scope(
    base: Scope,
    name: str,
    rig: Rig,
    terminal: Mapping[str, Any],
    secrets: Mapping[str, str] | None,
) -> Scope:
    settings: dict[str, Any] = {"timeout": 60}
    path = base.env.get("PATH", "") or os.defpath
    overlay: dict[str, str] = {}
    if name == "docker" and rig.docker is not None:
        settings.update(backend="docker", docker_image=DOCKER_IMAGE)
        overlay["PATH"] = f"{rig.docker.bin_dir}{os.pathsep}{path}"
    elif name == "ssh" and rig.ssh is not None:
        settings.update(backend="ssh", ssh_host=SSH_HOST, ssh_user=SSH_USER)
        overlay["PATH"] = rig.ssh.path_env(path)
        overlay["XDG_RUNTIME_DIR"] = rig.runtime_dir or ""
    settings.update(terminal)
    scope = base.with_config(DictConfig({"terminal": settings}))
    if overlay:
        scope = scope.with_env(scope.env.with_overlay(overlay))
    return scope.with_secrets(FakeSecrets(secrets)) if secrets is not None else scope


def _install_row(name: str, root: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Rig, list[str]]:
    rig = Rig(root)
    cleanup: list[str] = []
    if name == "docker":
        from indus.backends.docker import environment as docker_environment
        from indus.backends.docker import probe, reaper

        probe.clear_caches()
        reaper.reset_for_tests()
        sandbox_tmp = root / "sandbox-tmp"
        sandbox_tmp.mkdir(parents=True)
        monkeypatch.setattr(docker_environment, "SANDBOX_TEMP", str(sandbox_tmp))
        rig.docker = FakeDocker(root / "fake-docker")
        rig.docker.install()
        rig.docker.add_image(DOCKER_IMAGE)
    elif name == "ssh":
        redirect_remote_tmp(monkeypatch, root / "remote-tmp")
        rig.ssh = install(root / "ssh-bin", root / "remote-home")
        # Control sockets need a short private dir (the macOS 104-byte limit).
        rig.runtime_dir = tempfile.mkdtemp(prefix="isc", dir="/tmp")  # noqa: S108
        cleanup.append(rig.runtime_dir)
    return rig, cleanup


def _make_backend(
    name: str, scope: Scope, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Backend]:
    if name in REMOTE_ROWS and shutil.which("bash") is None:
        pytest.skip("the fake docker and ssh clients run commands with bash")
    work = tmp_path / "work"
    (work / "sub").mkdir(parents=True)
    rig, cleanup = _install_row(name, tmp_path / "rig", monkeypatch)
    FOREGROUND.reset_for_tests()
    backend = Backend(name, scope, os.path.realpath(work), rig, base=scope)
    backend.configure()
    try:
        yield backend
    finally:
        FOREGROUND.reset_for_tests()
        if name == "docker":
            from indus.backends.docker import probe, reaper

            probe.clear_caches()
            reaper.reset_for_tests()
        for directory in cleanup:
            shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture(params=BACKENDS)
def backend(
    request: pytest.FixtureRequest,
    scope: Scope,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Backend]:
    yield from _make_backend(str(request.param), scope, tmp_path, monkeypatch)


@pytest.fixture(params=["local", *REMOTE_ROWS])
def shell_backend(
    request: pytest.FixtureRequest,
    scope: Scope,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Backend]:
    """The rows that run a real shell (no scripted fake)."""
    yield from _make_backend(str(request.param), scope, tmp_path, monkeypatch)


@pytest.fixture(params=list(REMOTE_ROWS))
def remote_backend(
    request: pytest.FixtureRequest,
    scope: Scope,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Backend]:
    """The rows that are not this machine."""
    yield from _make_backend(str(request.param), scope, tmp_path, monkeypatch)
