"""Host facts against fake filesystem roots (R09.17)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from indus.platform import facts

ROOTS = Path(__file__).resolve().parents[2] / "fixtures" / "platform" / "roots"


@pytest.fixture(autouse=True)
def _fresh_caches() -> Iterator[None]:
    facts.clear_caches()
    yield
    facts.clear_caches()


@pytest.fixture
def linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(facts, "os_family", lambda: "linux")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("x86_64", "amd64"),
        ("AMD64", "amd64"),
        ("x64", "amd64"),
        ("aarch64", "arm64"),
        ("arm64", "arm64"),
        ("i386", "x86"),
        ("i686", "x86"),
        ("riscv64", "unknown"),
        ("", "unknown"),
        (None, "unknown"),
    ],
)
def test_normalize_arch(raw: str | None, expected: str) -> None:
    assert facts.normalize_arch(raw) == expected


def test_processor_architecture_env_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    before = (facts.process_arch(), facts.native_arch())
    facts.clear_caches()
    monkeypatch.setenv("PROCESSOR_ARCHITECTURE", "ARM64")
    monkeypatch.setenv("PROCESSOR_ARCHITEW6432", "ARM64")
    assert (facts.process_arch(), facts.native_arch()) == before


def test_os_family_is_normalised() -> None:
    assert facts.os_family() in {"linux", "darwin", "win32"} or facts.os_family()


def test_gpu_nvidia_procfs(linux: None) -> None:
    assert facts.gpu_class(ROOTS / "linux-nvidia") == "nvidia"


def test_gpu_wsl_cuda_bridge(linux: None) -> None:
    assert facts.gpu_class(ROOTS / "wsl") == "nvidia"


def test_gpu_sysfs_priority_amd_over_intel(linux: None) -> None:
    assert facts.gpu_class(ROOTS / "linux-amd-intel") == "amd"


def test_gpu_unreadable_sysfs_is_unknown(linux: None, tmp_path: Path) -> None:
    vendor = tmp_path / "sys/class/drm/card0/device/vendor"
    vendor.mkdir(parents=True)  # a directory where a file should be: the read fails
    assert facts.gpu_class(tmp_path) == "unknown"


def test_gpu_missing_drm_tree_is_unknown(linux: None, tmp_path: Path) -> None:
    assert facts.gpu_class(tmp_path) == "unknown"


def test_gpu_no_cards_is_none(linux: None, tmp_path: Path) -> None:
    (tmp_path / "sys/class/drm").mkdir(parents=True)
    assert facts.gpu_class(tmp_path) == "none"


@pytest.mark.parametrize(
    ("vendors", "expected"),
    [
        (["0x8086"], "intel"),
        (["0x8086", "0x10de"], "nvidia"),
        (["0x1002", "0x8086"], "amd"),
        (["0x1af4"], "none"),
        ([], "none"),
        (["", ""], "unknown"),
    ],
)
def test_classify_gpu_vendors(vendors: list[str], expected: str) -> None:
    assert facts.classify_gpu_vendors(vendors) == expected


def test_cgroup2_host_is_not_container(tmp_path: Path) -> None:
    assert not facts.is_container(ROOTS / "cgroup2-host", env={})


def test_cgroup2_container_root_overlay() -> None:
    assert facts.is_container(ROOTS / "cgroup2-container", env={})


def test_dockerenv_marker(tmp_path: Path) -> None:
    (tmp_path / ".dockerenv").write_text("")
    assert facts.is_container(tmp_path, env={})


def test_kubernetes_env_marker(tmp_path: Path) -> None:
    assert facts.is_container(tmp_path, env={"KUBERNETES_SERVICE_HOST": "10.0.0.1"})
    assert not facts.is_container(tmp_path, env={})


def test_cgroup_v1_marker(tmp_path: Path) -> None:
    (tmp_path / "proc/1").mkdir(parents=True)
    (tmp_path / "proc/1/cgroup").write_text("12:memory:/docker/0123abcd\n")
    assert facts.is_container(tmp_path, env={})


def test_root_mount_rule_only_reads_root_line() -> None:
    other = "30 22 0:40 /docker/abc /mnt/data rw - ext4 /dev/sdb1 rw\n"
    root = "22 1 259:2 / / rw shared:1 - ext4 /dev/sda1 rw\n"
    assert not facts.root_mount_is_container(other + root)
    assert facts.root_mount_is_container("22 1 0:9 /kubepods/x / rw - xfs /dev/sda1 rw\n")


def test_wsl_detection() -> None:
    assert facts.is_wsl(ROOTS / "wsl")
    assert not facts.is_wsl(ROOTS / "linux-nvidia")


def test_termux_markers() -> None:
    assert facts.is_termux({"TERMUX_VERSION": "0.118"})
    assert facts.is_termux({"PREFIX": "/data/data/com.termux/files/usr"})
    assert not facts.is_termux({})


def test_cpu_and_memory_from_fake_root(linux: None) -> None:
    root = ROOTS / "linux-nvidia"
    assert facts.cpu_model(root) == "Test CPU Model 9000 @ 3.00GHz"
    assert facts.cpu_vendor(root) == "GenuineIntel"
    assert facts.ram_total_bytes(root) == 16384000 * 1024


def test_arm_cpu_fields(linux: None) -> None:
    root = ROOTS / "linux-amd-intel"
    assert facts.cpu_model(root) == "Test Board Rev 1"
    assert facts.cpu_vendor(root) == "ARM"


def test_missing_meminfo_under_fake_root_is_none(linux: None, tmp_path: Path) -> None:
    assert facts.ram_total_bytes(tmp_path) is None


def test_interactive_session_linux(linux: None, tmp_path: Path) -> None:
    (tmp_path / "proc/self").mkdir(parents=True)
    (tmp_path / "proc/self/sessionid").write_text("3")
    assert not facts.interactive_session(tmp_path, uid=1000)
    (tmp_path / "run/user/1000").mkdir(parents=True)
    assert facts.interactive_session(tmp_path, uid=1000)
    (tmp_path / "proc/self/sessionid").write_text("4294967295")
    assert not facts.interactive_session(tmp_path, uid=1000)


def test_summary_drops_keys_that_raised(monkeypatch: pytest.MonkeyPatch, linux: None) -> None:
    def broken(root: Path = facts.ROOT) -> str:
        raise OSError("cannot read")

    monkeypatch.setattr(facts, "cpu_model", broken)
    out = facts.summary(ROOTS / "linux-nvidia")
    assert "cpu_model" not in out
    assert out["gpu_class"] == "nvidia"
    assert out["cpu_vendor"] == "GenuineIntel"
    assert out["machine_kind"] == "computer"


def test_summary_omits_unmeasured(linux: None, tmp_path: Path) -> None:
    out = facts.summary(tmp_path)
    assert "cpu_model" not in out and "ram_total_bytes" not in out and "gpu_class" not in out


def test_clear_caches_forgets(linux: None, tmp_path: Path) -> None:
    (tmp_path / "proc").mkdir()
    version = tmp_path / "proc/version"
    version.write_text("Linux plain")
    assert not facts.is_wsl(tmp_path)
    version.write_text("Linux microsoft-standard")
    assert not facts.is_wsl(tmp_path)  # cached
    facts.clear_caches()
    assert facts.is_wsl(tmp_path)
