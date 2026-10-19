"""The execution-contract suite runs once per registered backend (P05-04, P11-21).

``contract_entries`` lists the backend rows under test: the local backend with real bash, Docker
over the fake ``docker`` client (``tests/fakes/backend/docker_cli.py``), SSH over the fake ``ssh``
and ``scp`` clients (``tests/fakes/backend/ssh_cli.py``) and the scripted fake backend. Every test
in ``test_execution_contract.py`` runs against each of them unchanged; the cases that only make
sense for a backend that is not this machine are in ``test_docker_ssh_contract.py``. Singularity
runs over the fake ``apptainer`` CLI (``tests/fakes/backend/fake_apptainer.py``) in the shared and
shell rows (P21-04). The cloud sandboxes (``modal``, ``daytona``, ``vercel_sandbox``) run through
their real backend modules over the fake SDKs (``tests/fakes/backend/fake_*.py``) and one fake
vendor account (``sdk_kit.Cloud``) per test (P21-16); their own cases are in
``test_sdk_backends_contract.py``. The rig's SDK rows carry no probe: the fakes stand for the SDK
extras, which CI does not install. The same cases against a real Docker daemon, a real sshd and
the real vendors are the live lanes in ``tests/integration/backends/`` (markers ``docker`` and
``network``).

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
from tests.fakes.backend.fake_apptainer import FakeApptainer
from tests.fakes.backend.sdk_kit import Cloud
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
SDK_ROWS = ("modal", "daytona", "vercel_sandbox")
SHELL_ROWS = ("local", *REMOTE_ROWS, "singularity", *SDK_ROWS)
RECORD_KINDS = {"modal": "modal", "vercel_sandbox": "vercel"}


def vendor_credentials(name: str) -> dict[str, str]:
    """Made-up credentials a fake SDK accepts (built at run time, never a literal)."""
    salt = os.urandom(4).hex()
    if name == "modal":
        return {"MODAL_TOKEN_ID": "id-" + salt, "MODAL_TOKEN_SECRET": "sec-" + salt}
    if name == "daytona":
        return {"DAYTONA_API_KEY": "dk-" + salt}
    if name == "vercel_sandbox":
        return {
            "VERCEL_TOKEN": "vt-" + salt,
            "VERCEL_PROJECT_ID": "prj_" + salt,
            "VERCEL_TEAM_ID": "team_" + salt,
        }
    return {}


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
    apptainer: FakeApptainer | None = None
    runtime_dir: str | None = None
    cloud: Cloud | None = None
    sdk: Any = None  # the fake SDK of the row under test (FakeModal, FakeDaytona, FakeVercel)
    credentials: dict[str, str] = field(default_factory=lambda: {})
    opened: list[Environment] = field(default_factory=lambda: [])

    def entries(self) -> list[BackendEntry]:
        """The rows of ``contract_entries`` whose factories use this rig's fake clients."""
        rows: list[BackendEntry] = []
        for entry in contract_entries():
            if entry.name == "docker":
                rows.append(dataclasses.replace(entry, factory=self._create_docker))
            elif entry.name in SDK_ROWS:
                rows.append(dataclasses.replace(entry, factory=self._create_sdk, probe=None))
            else:
                rows.append(entry)
        return rows

    @property
    def sandbox_tmp(self) -> str:
        assert self.cloud is not None
        return str(self.cloud.root / "tmp")

    async def _create_sdk(self, request: CreateRequest) -> Environment:
        from indus.backends.sdk_entries import task_key_for
        from indus.backends.sdk_ports import ProfileRecords, VendorSupport

        assert self.sdk is not None, "an SDK row needs its fake SDK"
        scope = request.scope
        common: dict[str, Any] = {
            "support": VendorSupport(),
            "session_id": request.session_id,
            "task_key": task_key_for(request),
            "overrides": request.overrides,
            "temp_dir": self.sandbox_tmp,
        }
        name = request.key.backend
        if name == "modal":
            from indus.backends.modal.exec import open_modal

            env: Environment = await open_modal(
                scope,
                records=ProfileRecords.for_scope(scope, RECORD_KINDS[name]),
                sdk=self.sdk.module,
                **common,
            )
        elif name == "daytona":
            from indus.backends.daytona import open_daytona

            env = await open_daytona(scope, sdk=self.sdk.module, **common)
        else:
            from indus.backends.vercel.exec import open_vercel

            env = await open_vercel(
                scope,
                records=ProfileRecords.for_scope(scope, RECORD_KINDS[name]),
                sdk=self.sdk.sync_module,
                **common,
            )
        self.opened.append(env)
        return env

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
    from indus.backends.sdk_entries import sdk_entries
    from indus.backends.singularity import singularity_entry

    fake = FakeEnvironment()
    rows = [e for e in default_entries() if e.name in ("local", *REMOTE_ROWS)]
    return [
        *rows,
        singularity_entry(),
        *sdk_entries(),
        BackendEntry("fake", _create_fake, fake.flags),
    ]


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
    elif name == "singularity" and rig.apptainer is not None:
        image = rig.apptainer.root / "sandbox.sif"
        settings.update(
            backend="singularity", singularity_image=str(image), container_persistent=False
        )
        overlay["PATH"] = rig.apptainer.path_env(path)
    elif name in SDK_ROWS:
        settings.update(backend=name, container_persistent=False, docker_image=DOCKER_IMAGE)
        secrets = {**rig.credentials, **(secrets or {})}
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
    elif name == "singularity":
        from indus.backends import singularity

        instance_tmp = root / "instance-tmp"
        instance_tmp.mkdir(parents=True)
        monkeypatch.setattr(singularity, "SANDBOX_TEMP", str(instance_tmp))
        rig.apptainer = FakeApptainer(root / "fake-apptainer")
        rig.apptainer.install()
        (rig.apptainer.root / "sandbox.sif").write_text("SIF\n", encoding="utf-8")
    elif name in SDK_ROWS:
        rig.cloud = Cloud(root / "cloud")
        rig.credentials = vendor_credentials(name)
        # A new sandbox starts from its image's files: the image holds the workdir and ``sub``.
        keep = {str(rig.cloud.root / "work" / "sub" / ".keep"): b""}
        rig.cloud.images[DOCKER_IMAGE] = keep
        rig.cloud.images[""] = keep  # Vercel's default image
        if name == "modal":
            from tests.fakes.backend.fake_modal import FakeModal

            rig.sdk = FakeModal(rig.cloud)
        elif name == "daytona":
            from tests.fakes.backend.fake_daytona import FakeDaytona

            rig.sdk = FakeDaytona(rig.cloud)
        else:
            from tests.fakes.backend.fake_vercel import FakeVercel

            rig.sdk = FakeVercel(rig.cloud, workspace=str(rig.cloud.root / "workspace"))
    return rig, cleanup


def _make_backend(
    name: str, scope: Scope, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Backend]:
    if name in (*REMOTE_ROWS, "singularity", *SDK_ROWS) and shutil.which("bash") is None:
        pytest.skip("the fake docker, ssh, apptainer and SDK sandboxes run commands with bash")
    if name in SDK_ROWS and shutil.which("timeout") is None:
        pytest.skip("the SDK sandboxes wrap every command in the timeout guard")
    rig, cleanup = _install_row(name, tmp_path / "rig", monkeypatch)
    # A fake cloud maps only paths inside its own root as they are; the workdir lives there.
    work = rig.cloud.root / "work" if rig.cloud is not None else tmp_path / "work"
    (work / "sub").mkdir(parents=True)
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
        if rig.cloud is not None:
            rig.cloud.release()
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


@pytest.fixture(params=list(SHELL_ROWS))
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


@pytest.fixture(params=list(SDK_ROWS))
def sdk_backend(
    request: pytest.FixtureRequest,
    scope: Scope,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Backend]:
    """The cloud sandbox rows (over their fake SDKs)."""
    yield from _make_backend(str(request.param), scope, tmp_path, monkeypatch)
