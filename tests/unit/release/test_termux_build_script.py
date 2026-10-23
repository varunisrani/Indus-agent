"""P24-21: the Termux build script's stage plan and refusals, and the builder Dockerfile (static).

No container runs here: the dry run prints the plan, and the refusals happen before any tool is
called (fake tools on PATH record every call)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tooling.checks import pins
from tooling.checks.common import Result

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts/termux/build.sh"
DOCKERFILE = ROOT / "scripts/termux/builder.Dockerfile"
STAGES = ["identity", "preflight", "source", "image", "runtimes", "wheels", "venv", "pack", "repo"]

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")


def run(
    tmp_path: Path, *args: str, shims: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    python = bin_dir / "python3"
    if not python.exists():
        python.symlink_to(sys.executable)
    for name, body in (shims or {}).items():
        shim = bin_dir / name
        shim.write_text(f"#!/bin/sh\necho {name} \"$@\" >> '{tmp_path}/calls.log'\n{body}\n")
        shim.chmod(0o755)
    env = {"PATH": f"{bin_dir}{os.pathsep}/usr/bin:/bin", "HOME": str(tmp_path), "LC_ALL": "C"}
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )


def test_dry_run_lists_the_stages_in_order(tmp_path: Path) -> None:
    tag = "v1.2.4-canary.20261021T064100Z"
    done = run(tmp_path, "--tag", tag, "--out", str(tmp_path / "out"), "--dry-run")
    assert done.returncode == 0, done.stderr
    assert f"tag {tag} -> indus-agent 1.2.4~canary.20261021T064100Z-1" in done.stdout
    assert "(suite indus-canary, android_24_arm64_v8a)" in done.stdout
    plan = re.findall(r"^  (\d) (\w+)$", done.stdout, re.MULTILINE)
    assert [name for _n, name in plan] == STAGES
    assert [int(n) for n, _name in plan] == list(range(1, 10))
    assert not (tmp_path / "out").exists()  # a dry run writes nothing


def test_the_placeholder_digest_is_reported(tmp_path: Path) -> None:
    done = run(tmp_path, "--tag", "v1.2.3", "--out", str(tmp_path / "out"), "--dry-run")
    assert "placeholder digest (owner-to-fill)" in done.stdout


@pytest.mark.parametrize("tag", ["v01.2.3", "v1.2.3+canary.20261021T064100Z", "latest"])
def test_a_malformed_tag_stops_at_identity(tmp_path: Path, tag: str) -> None:
    done = run(tmp_path, "--tag", tag, "--out", str(tmp_path / "out"), "--dry-run")
    assert done.returncode == 2
    assert "is not a release tag" in done.stderr and "plan" not in done.stdout


@pytest.mark.parametrize("args", [[], ["--tag", "v1.2.3"], ["--out", "x"], ["--bogus"]])
def test_usage_errors_exit_2(tmp_path: Path, args: list[str]) -> None:
    done = run(tmp_path, *args)
    assert done.returncode == 2 and "usage: build.sh" in done.stderr


def test_preflight_refuses_before_any_tool_runs(tmp_path: Path) -> None:
    """On an aarch64 host with every tool present, the placeholder base still stops the build in
    preflight: no image is built, no container runs, nothing is exported."""
    tools = {name: "exit 0" for name in ("docker", "git", "uv", "dpkg-deb")}
    tools["uname"] = "echo aarch64"
    done = run(tmp_path, "--tag", "v1.2.3", "--out", str(tmp_path / "out"), shims=tools)
    assert done.returncode == 2
    assert "placeholder digest (owner-to-fill); fill in the digest first" in done.stderr
    calls = (tmp_path / "calls.log").read_text() if (tmp_path / "calls.log").exists() else ""
    assert not [line for line in calls.splitlines() if not line.startswith("uname")]


def test_preflight_refuses_a_non_arm_host(tmp_path: Path) -> None:
    done = run(
        tmp_path, "--tag", "v1.2.3", "--out", str(tmp_path / "o"), shims={"uname": "echo x86_64"}
    )
    assert done.returncode == 2 and "aarch64 host" in done.stderr


def test_unknown_container_stage_is_refused(tmp_path: Path) -> None:
    done = run(tmp_path, "--in-container", "nope")
    assert done.returncode == 2 and "unknown container stage" in done.stderr


def test_the_script_keeps_its_contract() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash\n") and "set -Eeuo pipefail" in text
    assert 'PLATFORM_TAG="android_24_arm64_v8a"' in text and 'PYTHON_ABI="cp314"' in text
    assert re.search(r"run_in none .*-- venv", text)  # the environment installs offline
    assert "--no-index" in text and "dpkg-deb --build -Zxz --root-owner-group" in text
    assert "Architecture: aarch64" in text
    assert os.access(SCRIPT, os.X_OK)


# -- the builder Dockerfile ------------------------------------------------------------------------


def test_the_builder_base_is_a_digest_arg() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    [arg] = re.findall(r"^ARG BASE_IMAGE=(\S+)$", text, re.MULTILINE)
    assert re.fullmatch(r"docker\.io/termux/termux-docker:aarch64@sha256:[0-9a-f]{64}", arg)
    assert re.findall(r"^FROM (\S+)", text, re.MULTILINE) == ["${BASE_IMAGE}"]
    assert "USER 1000:1000" in text  # Termux's apt refuses root
    assert re.search(r"\bwheel==\d+\.\d+\.\d+\b", text)
    assert ":latest" not in text


def test_the_pins_check_flags_the_placeholder() -> None:
    result = Result("pins")
    assert pins.check_dockerfile(DOCKERFILE, ROOT, result) == 1
    if "0" * 64 in DOCKERFILE.read_text(encoding="utf-8"):
        assert result.violations == []
        assert [w for w in result.warnings if "owner-to-fill placeholder" in w]
    else:  # the owner filled it in
        assert result.violations == [] and result.warnings == []
