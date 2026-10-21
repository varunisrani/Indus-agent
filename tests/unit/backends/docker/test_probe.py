"""Docker client discovery and host probes (P11-01)."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from indus.backends.contract import EnvironmentConnectionError
from indus.backends.docker import probe
from indus.backends.docker.probe import (
    Availability,
    ClientInfo,
    CliResult,
    DockerCli,
    DockerDaemonDown,
    ensure_daemon,
    find_client,
    host_facts,
    image_uses_s6_init,
    start_hint,
)
from indus.backends.docker.settings import DockerConfigError, settings_for
from indus.foundation.config.schema import default_for
from indus.foundation.versions import SANDBOX_IMAGE
from tests.fakes.backend.docker_cli import FakeDocker
from tests.fakes.clock import FakeClock
from tests.unit.backends.docker.conftest import IMAGE, docker_scope


def _cli(fake: FakeDocker, runtime: str = "docker") -> DockerCli:
    client = ClientInfo(str(fake.bin_dir / runtime), "podman" if runtime == "podman" else "docker")
    return DockerCli(client, {"PATH": "/usr/bin:/bin"}, runner=fake.runner)


def _exe(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


# -- discovery ------------------------------------------------------------------------------------


def test_podman_when_docker_is_missing(tmp_path: Path, fake_clock: FakeClock) -> None:
    podman = _exe(tmp_path / "bin" / "podman")
    scope = docker_scope(tmp_path, None, fake_clock, env={"PATH": str(podman.parent)})
    found = find_client(scope)
    assert found == ClientInfo(str(podman), "podman")
    assert found is not None and found.label == "Podman"


def test_docker_before_podman(tmp_path: Path, fake_clock: FakeClock) -> None:
    _exe(tmp_path / "bin" / "podman")
    docker = _exe(tmp_path / "bin" / "docker")
    scope = docker_scope(tmp_path, None, fake_clock, env={"PATH": str(docker.parent)})
    assert find_client(scope) == ClientInfo(str(docker), "docker")


def test_configured_binary_wins(tmp_path: Path, fake_clock: FakeClock) -> None:
    _exe(tmp_path / "bin" / "docker")
    mine = _exe(tmp_path / "elsewhere" / "my-podman")
    from_env = _exe(tmp_path / "env" / "docker")
    scope = docker_scope(
        tmp_path,
        None,
        fake_clock,
        env={"PATH": str(tmp_path / "bin"), "INDUS_DOCKER_BINARY": str(from_env)},
        terminal__docker_binary=str(mine),
    )
    assert find_client(scope) == ClientInfo(str(mine), "podman")
    no_config = docker_scope(
        tmp_path,
        None,
        fake_clock,
        env={"PATH": str(tmp_path / "bin"), "INDUS_DOCKER_BINARY": str(from_env)},
    )
    assert find_client(no_config) == ClientInfo(str(from_env), "docker")


def test_bad_configured_binary_falls_back_to_path(
    tmp_path: Path, fake_clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    docker = _exe(tmp_path / "bin" / "docker")
    scope = docker_scope(
        tmp_path,
        None,
        fake_clock,
        env={"PATH": str(docker.parent)},
        terminal__docker_binary=str(tmp_path / "missing"),
    )
    assert find_client(scope) == ClientInfo(str(docker), "docker")
    assert "not an executable" in caplog.text


def test_no_client_at_all(tmp_path: Path, fake_clock: FakeClock) -> None:
    scope = docker_scope(tmp_path, None, fake_clock, env={"PATH": str(tmp_path / "empty")})
    assert find_client(scope) is None
    problem = probe.quick_check(scope)
    assert problem is not None and "no docker or podman client" in problem


# -- daemon ---------------------------------------------------------------------------------------


def test_daemon_down_is_typed_with_a_hint(fake_docker: FakeDocker) -> None:
    fake_docker.set_daemon(False)
    with pytest.raises(DockerDaemonDown) as caught:
        asyncio.run(ensure_daemon(_cli(fake_docker), os_name="linux"))
    error = caught.value
    assert isinstance(error, EnvironmentConnectionError)  # the registry answers "degraded"
    assert error.code == "backend.docker.daemon"
    assert "daemon is not reachable" in error.reason
    assert "Cannot connect to the Docker daemon" in error.reason
    assert "systemctl start docker" in error.retry_hint


def test_daemon_up_gives_the_version(fake_docker: FakeDocker) -> None:
    assert asyncio.run(ensure_daemon(_cli(fake_docker))) == "27.3.1"


def test_version_timeout_is_daemon_down() -> None:
    async def slow(argv: Sequence[str], timeout: float, env: Mapping[str, str]) -> CliResult:
        assert timeout == probe.VERSION_TIMEOUT_S
        return CliResult(-1, "", "no answer", timed_out=True)

    cli = DockerCli(ClientInfo("/x/docker", "docker"), {}, runner=slow)
    with pytest.raises(DockerDaemonDown, match="did not answer in time"):
        asyncio.run(ensure_daemon(cli, os_name="darwin"))


def test_start_hint_per_os_and_runtime() -> None:
    docker = ClientInfo("/usr/bin/docker", "docker")
    podman = ClientInfo("/usr/bin/podman", "podman")
    assert "systemctl start docker" in start_hint(docker, "linux")
    assert "Docker Desktop" in start_hint(docker, "darwin")
    assert "Docker Desktop" in start_hint(docker, "win32")
    assert "podman machine start" in start_hint(podman, "darwin")
    assert "podman.socket" in start_hint(podman, "linux")


# -- host facts -----------------------------------------------------------------------------------


def test_s6_entrypoint_detected(fake_docker: FakeDocker) -> None:
    fake_docker.add_image("s6:1", ["/init"])
    fake_docker.add_image("s6:2", ["/package/admin/s6-overlay/command/init", "--x"])
    fake_docker.add_image("plain:1", ["/usr/bin/tini", "--"])
    fake_docker.add_image("none:1", None)
    cli = _cli(fake_docker)

    async def go() -> list[bool | None]:
        names = ["s6:1", "s6:2", "plain:1", "none:1", "missing:1"]
        return [await image_uses_s6_init(cli, name) for name in names]

    assert asyncio.run(go()) == [True, True, False, False, None]


def test_cgroup_facts_from_docker_and_podman_info() -> None:
    assert probe.cgroups_from_info({"MemoryLimit": True, "PidsLimit": True, "CpuCfsQuota": True})
    assert not probe.cgroups_from_info(
        {"MemoryLimit": True, "PidsLimit": False, "CpuCfsQuota": True}
    )
    podman = {"host": {"cgroupControllers": ["cpu", "memory", "pids", "io"]}}
    assert probe.cgroups_from_info(podman)
    assert not probe.cgroups_from_info({"host": {"cgroupControllers": ["memory"]}})
    assert probe.cgroups_from_info({"something": "else"}) is None


def test_no_cgroups_on_this_host(fake_docker: FakeDocker) -> None:
    with fake_docker.edit() as state:
        state["info"]["PidsLimit"] = False
    facts = asyncio.run(host_facts(_cli(fake_docker), IMAGE, os_name="linux"))
    assert not facts.cgroups


def test_unreadable_info_is_not_cached(fake_docker: FakeDocker) -> None:
    with fake_docker.edit() as state:
        state["info"] = {"odd": True}
    cli = _cli(fake_docker)
    assert not asyncio.run(host_facts(cli, IMAGE, os_name="linux")).cgroups
    with fake_docker.edit() as state:
        state["info"] = {"MemoryLimit": True, "PidsLimit": True, "CpuCfsQuota": True}
    assert asyncio.run(host_facts(cli, IMAGE, os_name="linux")).cgroups
    calls = fake_docker.subcommands().count("info")
    asyncio.run(host_facts(cli, IMAGE, os_name="linux"))  # both answers cached now
    assert fake_docker.subcommands().count("info") == calls


def test_storage_quota_false_on_macos(fake_docker: FakeDocker) -> None:
    with fake_docker.edit() as state:
        state["info"]["DriverStatus"] = [["Backing Filesystem", "xfs"]]
    facts = asyncio.run(host_facts(_cli(fake_docker), IMAGE, os_name="darwin"))
    assert not facts.storage_quota
    assert "create" not in fake_docker.subcommands()


def test_storage_quota_false_on_ext4(fake_docker: FakeDocker) -> None:
    facts = asyncio.run(host_facts(_cli(fake_docker), IMAGE, os_name="linux"))
    assert not facts.storage_quota
    assert "create" not in fake_docker.subcommands()


def test_storage_quota_on_xfs_is_confirmed_without_pulling(fake_docker: FakeDocker) -> None:
    with fake_docker.edit() as state:
        state["info"]["DriverStatus"] = [["Backing Filesystem", "xfs"]]
    facts = asyncio.run(host_facts(_cli(fake_docker), IMAGE, os_name="linux"))
    assert facts.storage_quota
    create = next(c for c in fake_docker.commands() if c[0] == "create")
    assert "--pull=never" in create and "size=1m" in create
    assert fake_docker.containers() == {}  # the probe container is removed again


def test_storage_quota_refused_by_the_daemon(fake_docker: FakeDocker) -> None:
    with fake_docker.edit() as state:
        state["info"]["DriverStatus"] = [["Backing Filesystem", "xfs"]]
        state["storage_create"] = "reject"
    facts = asyncio.run(host_facts(_cli(fake_docker), IMAGE, os_name="linux"))
    assert not facts.storage_quota


# -- availability ---------------------------------------------------------------------------------


def test_flake_grace_serves_last_good(tmp_path: Path, fake_clock: FakeClock) -> None:
    answers: list[str | None] = [None, "daemon not reachable", "daemon not reachable"]
    asked: list[int] = []

    def check(_scope: object) -> str | None:
        asked.append(1)
        return answers[min(len(asked) - 1, len(answers) - 1)]

    available = Availability(check, clock=fake_clock, ttl_s=5.0, grace_s=60.0)
    scope = docker_scope(tmp_path, None, fake_clock)
    assert available(scope) is None
    fake_clock.advance(10)
    assert available(scope) is None  # failed 10 s after a success: a flake
    fake_clock.advance(55)
    assert available(scope) == "daemon not reachable"  # 65 s since the success: honoured
    assert len(asked) == 3


def test_availability_caches_within_its_ttl(tmp_path: Path, fake_clock: FakeClock) -> None:
    asked: list[int] = []

    def check(_scope: object) -> str | None:
        asked.append(1)
        return "nope"

    available = Availability(check, clock=fake_clock, ttl_s=30.0)
    scope = docker_scope(tmp_path, None, fake_clock)
    assert available(scope) == "nope"
    fake_clock.advance(10)
    assert available(scope) == "nope"
    assert len(asked) == 1
    fake_clock.advance(25)
    available(scope)
    assert len(asked) == 2


def test_quick_check_with_the_fake_client(
    tmp_path: Path, fake_clock: FakeClock, fake_docker: FakeDocker
) -> None:
    scope = docker_scope(tmp_path, fake_docker, fake_clock)
    assert probe.quick_check(scope, sync_runner=fake_docker.sync_runner) is None
    fake_docker.set_daemon(False)
    problem = probe.quick_check(scope, sync_runner=fake_docker.sync_runner)
    assert problem is not None and "daemon is not reachable" in problem


# -- settings and doctor --------------------------------------------------------------------------


def test_container_defaults() -> None:
    # Our own sandbox image (D-16.64) under the one release base (D-16.690), stable channel, slim.
    assert default_for("terminal.docker_image") == f"{SANDBOX_IMAGE}:stable-slim"
    assert default_for("terminal.container_cpu") == 1.0
    assert default_for("terminal.container_memory_mb") == 5120
    assert default_for("terminal.container_disk_mb") == 51200
    assert default_for("terminal.container_persistent") is True
    assert default_for("terminal.docker_persist_across_processes") is True
    assert default_for("terminal.docker_orphan_reaper") is True
    assert default_for("terminal.docker_shm_size") == "1g"
    assert default_for("terminal.docker_network") is True


def test_pinned_image_needs_a_digest(tmp_path: Path, fake_clock: FakeClock) -> None:
    scope = docker_scope(tmp_path, None, fake_clock, terminal__docker_image_pinned=True)
    with pytest.raises(DockerConfigError, match="names no digest"):
        settings_for(scope)
    pinned = docker_scope(
        tmp_path,
        None,
        fake_clock,
        terminal__docker_image_pinned=True,
        terminal__docker_image="registry.test/sandbox@sha256:" + "a" * 64,
    )
    assert settings_for(pinned).image_pinned


def test_doctor_rows(tmp_path: Path, fake_clock: FakeClock, fake_docker: FakeDocker) -> None:
    scope = docker_scope(tmp_path, fake_docker, fake_clock)
    rows = probe.doctor_rows(scope, sync_runner=fake_docker.sync_runner)
    assert [(r.id, r.severity) for r in rows] == [
        ("backend.docker", "ok"),
        ("backend.docker.image", "ok"),
    ]
    assert "27.3.1" in rows[0].message and "daemon answers" in rows[0].message
    other = docker_scope(tmp_path, fake_docker, fake_clock, terminal__docker_image="new:1")
    rows = probe.doctor_rows(other, sync_runner=fake_docker.sync_runner)
    assert rows[-1].severity == "info" and rows[-1].fix == "docker pull new:1"
    fake_docker.set_daemon(False)
    rows = probe.doctor_rows(scope, sync_runner=fake_docker.sync_runner, os_name="linux")
    assert [(r.id, r.severity) for r in rows] == [
        ("backend.docker", "ok"),
        ("backend.docker.daemon", "error"),
    ]
    assert rows[1].fix is not None and "systemctl" in rows[1].fix


def test_doctor_backend_check_reports_docker(
    tmp_path: Path, fake_clock: FakeClock, fake_docker: FakeDocker
) -> None:
    from indus.cli.doctor import DoctorContext
    from indus.cli.doctor.checks import check_backend
    from indus.foundation.config import loader

    scope = docker_scope(tmp_path, fake_docker, fake_clock)
    (scope.home / "config.yaml").write_text(
        f"terminal:\n  backend: docker\n  docker_image: {IMAGE}\n", encoding="utf-8"
    )
    loader.invalidate()
    found = check_backend(DoctorContext(scope.with_config(None)))
    assert [(f.id, f.severity) for f in found] == [
        ("backend.docker", "ok"),
        ("backend.docker.image", "ok"),
    ]
