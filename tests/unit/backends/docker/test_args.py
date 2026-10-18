"""The security and resource flags of ``docker run`` (P11-02): argv goldens."""

from __future__ import annotations

import logging
from dataclasses import replace

import pytest

from indus.backends.docker import args as args_mod
from indus.backends.docker.args import PIDS_LIMIT, network_from_extra, run_args
from indus.backends.docker.probe import HostFacts
from indus.backends.docker.settings import DockerConfigError, DockerSettings

BASE = DockerSettings(image="img")
FULL = HostFacts(cgroups=True, storage_quota=True, s6_init=False)

SECURITY = [
    "--cap-drop", "ALL",
    "--cap-add", "DAC_OVERRIDE", "--cap-add", "CHOWN", "--cap-add", "FOWNER",
    "--cap-add", "SETUID", "--cap-add", "SETGID",
    "--security-opt", "no-new-privileges",
    "--tmpfs", "/tmp:rw,nosuid,exec,size=512m",
    "--tmpfs", "/var/tmp:rw,noexec,nosuid,size=256m",
    "--tmpfs", "/run:rw,noexec,nosuid,size=64m",
]  # fmt: skip


@pytest.fixture(autouse=True)
def quota_warning_again() -> None:
    args_mod._warned_storage = False  # pyright: ignore[reportPrivateUsage]


def test_default_golden() -> None:
    built = run_args(BASE, FULL)
    assert list(built.args) == [
        *SECURITY,
        "--cpus", "1",
        "--memory", "5120m",
        "--pids-limit", "2048",
        "--shm-size", "1g",
        "--storage-opt", "size=51200m",
    ]  # fmt: skip
    assert built.extra == ()
    assert built.init


def test_pids_limit_is_2048() -> None:
    assert PIDS_LIMIT == 2048  # the code's value, not the docs' 256 (R02.23)


def test_no_resource_limits_without_cgroups() -> None:
    built = run_args(BASE, HostFacts(cgroups=False, storage_quota=False))
    assert "--cpus" not in built.args
    assert "--memory" not in built.args
    assert "--pids-limit" not in built.args
    assert list(built.args) == [*SECURITY, "--shm-size", "1g"]


def test_zero_cpu_and_memory_are_left_out() -> None:
    built = run_args(replace(BASE, cpu=0, memory_mb=0, disk_mb=0), FULL)
    assert list(built.args) == [*SECURITY, "--pids-limit", "2048", "--shm-size", "1g"]


def test_host_user_drops_privdrop_caps() -> None:
    built = run_args(replace(BASE, run_as_host_user=True), FULL, host_user="1000:1000")
    assert "SETUID" not in built.args and "SETGID" not in built.args
    at = built.args.index("--user")
    assert built.args[at + 1] == "1000:1000"


def test_snap_compat() -> None:
    built = run_args(replace(BASE, snap_compat=True), FULL)
    assert "no-new-privileges" not in built.args
    assert not built.init


def test_s6_image_runs_exec_run_without_init() -> None:
    built = run_args(BASE, replace(FULL, s6_init=True))
    assert "/run:rw,exec,nosuid,size=64m" in built.args
    assert "/run:rw,noexec,nosuid,size=64m" not in built.args
    assert not built.init


@pytest.mark.parametrize("shm", ["", "0", " 0 "])
def test_shm_omitted_when_configured_off(shm: str) -> None:
    assert "--shm-size" not in run_args(replace(BASE, shm_size=shm), FULL).args


def test_shm_left_to_extra_args() -> None:
    built = run_args(replace(BASE, extra_args=("--shm-size=2g",)), FULL)
    assert "--shm-size" not in built.args
    assert built.extra == ("--shm-size=2g",)


def test_storage_quota_only_when_probed(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    facts = replace(FULL, storage_quota=False)
    first = run_args(BASE, facts)
    run_args(BASE, facts)
    assert "--storage-opt" not in first.args
    warnings = [r for r in caplog.records if "disk limit" in r.getMessage()]
    assert len(warnings) == 1


def test_network_off_adds_none_once() -> None:
    built = run_args(replace(BASE, network=False), FULL)
    assert built.args.count("--network=none") == 1


@pytest.mark.parametrize(
    "extra", [("--network", "bridge"), ("--network=host",), ("--net", "my-net")]
)
def test_contradicting_network_is_refused(extra: tuple[str, ...]) -> None:
    with pytest.raises(DockerConfigError, match="docker_network is false"):
        run_args(replace(BASE, network=False, extra_args=extra), FULL)


def test_extra_network_none_is_not_doubled() -> None:
    built = run_args(replace(BASE, network=False, extra_args=("--network", "none")), FULL)
    assert "--network=none" not in built.args
    assert built.extra == ("--network", "none")


def test_network_on_leaves_extra_network_alone() -> None:
    built = run_args(replace(BASE, extra_args=("--network=host",)), FULL)
    assert not any(a.startswith("--network") for a in built.args)
    assert built.extra == ("--network=host",)


def test_network_from_extra_forms() -> None:
    assert network_from_extra(["--network", "x"]) == "x"
    assert network_from_extra(["--net=y"]) == "y"
    assert network_from_extra(["--network"]) == ""
    assert network_from_extra(["--rm"]) is None
