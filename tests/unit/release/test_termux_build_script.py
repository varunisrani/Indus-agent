"""P24-21: the Termux build script's stage plan and refusals, and the builder Dockerfile (static);
W50-d-3: the repository is validated between assembly and signing.

No container runs here: the dry run prints the plan, the refusals happen before any tool is called
(fake tools on PATH record every call), and the full host half runs against fake docker / git / uv
with the real dpkg-deb, from a copy of the scripts whose base digest is filled in."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.termux import repo
from tooling.checks import pins
from tooling.checks.common import Result

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts/termux/build.sh"
DOCKERFILE = ROOT / "scripts/termux/builder.Dockerfile"
STAGES = [
    "identity",
    "preflight",
    "source",
    "image",
    "runtimes",
    "wheels",
    "venv",
    "pack",
    "repo",
    "validate",
    "sign",
]

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")


def run(
    tmp_path: Path,
    *args: str,
    shims: dict[str, str] | None = None,
    script: Path = SCRIPT,
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
        ["bash", str(script), *args],
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
    plan = re.findall(r"^  (\d+) (\w+)$", done.stdout, re.MULTILINE)
    assert [name for _n, name in plan] == STAGES
    assert [int(n) for n, _name in plan] == list(range(1, 12))
    assert STAGES.index("repo") < STAGES.index("validate") < STAGES.index("sign")
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


# -- the host half with fake tools (W50-d-3) -------------------------------------------------------

FAKE_DOCKER = r"""
work=""; last=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        build) exit 0 ;;
        -v) case "$2" in *:/work) work="${2%:/work}" ;; esac; shift ;;
    esac
    last="$1"; shift
done
case "$last" in
    runtimes)
        mkdir -p "$work/payload/runtime/data/data/com.termux/files/usr/bin"
        printf 'elf\n' > "$work/payload/runtime/data/data/com.termux/files/usr/bin/python3.14"
        printf 'python 3.14.0\n' > "$work/payload/manifest.txt" ;;
    venv)
        mkdir -p "$work/root/venv/bin"
        [ -n "${SKIP_PYTHON:-}" ] || printf 'elf\n' > "$work/root/venv/bin/python" ;;
esac
"""

FAKE_UV = r"""
while [ "$#" -gt 0 ]; do
    [ "$1" = "--output-file" ] && printf 'click==8.3.0\n' > "$2"
    shift
done
"""


def _copy_with_a_digest(tmp_path: Path) -> Path:
    """The Termux scripts in a scratch tree, the Dockerfile's placeholder replaced by a digest."""
    here = tmp_path / "tree/scripts/termux"
    here.mkdir(parents=True)
    for name in ("build.sh", "repo.py", "__init__.py"):
        shutil.copy2(ROOT / "scripts/termux" / name, here / name)
    digest = "sha256:" + "ab" * 32
    text = DOCKERFILE.read_text(encoding="utf-8").replace("sha256:" + "0" * 64, digest)
    (here / "builder.Dockerfile").write_text(text, encoding="utf-8")
    (tmp_path / "tree/src").symlink_to(ROOT / "src")  # repo.py imports the one tag grammar
    return here / "build.sh"


def _fake_tools(tmp_path: Path) -> dict[str, str]:
    source = tmp_path / "tagged-tree"
    (source / "src/indus").mkdir(parents=True)
    (source / "pyproject.toml").write_text("[project]\nname = 'indus'\n", encoding="utf-8")
    (source / "src/indus/__init__.py").write_text('__version__ = "0.0.0"\n', encoding="utf-8")
    git = (
        f'case "$1" in -C) shift 2 ;; esac\n'
        f'[ "$1" = "archive" ] && exec tar -c -C "{source}" .\n'
        f"exit 0"
    )
    return {"uname": "echo aarch64", "docker": FAKE_DOCKER, "git": git, "uv": FAKE_UV}


needs_dpkg = pytest.mark.skipif(shutil.which("dpkg-deb") is None, reason="dpkg-deb is missing")


@needs_dpkg
def test_the_host_half_validates_before_it_signs(tmp_path: Path) -> None:
    """Every stage runs in order; the real `.deb` passes validation (launcher, venv, app, the `apt`
    stamp); without a key the sign stage ends the build with 3 and no signature exists."""
    script = _copy_with_a_digest(tmp_path)
    out = tmp_path / "out"
    done = run(
        tmp_path, "--tag", "v1.2.3", "--out", str(out), script=script, shims=_fake_tools(tmp_path)
    )
    assert done.returncode == 3, done.stderr
    ran = re.findall(r"^==> stage (\d+)/11 (\w+):", done.stdout, re.MULTILINE)
    assert [name for _n, name in ran] == STAGES
    assert [int(n) for n, _name in ran] == list(range(1, 12))
    assert "indus-stable: valid; indus-agent 1.2.3-1 is laid out for Termux" in done.stderr
    assert "valid but left UNSIGNED (no --key-file given)" in done.stderr
    dists = out / "repo/dists/indus-stable"
    assert (dists / "Release").is_file()
    assert not (dists / "InRelease").exists() and not (out / "repo/key.asc").exists()
    [deb] = (out / "repo/pool/main/i/indus-agent").glob("*.deb")
    assert deb.name == "indus-agent_1.2.3-1_aarch64.deb"
    assert repo.check_payload(deb.read_bytes()) == []
    assert not (out / "work").exists()  # the private work folder is removed


@needs_dpkg
def test_a_package_that_fails_validation_is_never_signed(tmp_path: Path) -> None:
    """The venv stage leaves no python: validate reports it and the build stops with 4 before the
    sign stage, even with a key file given."""
    script = _copy_with_a_digest(tmp_path)
    out = tmp_path / "out"
    key = tmp_path / "key.asc"
    key.write_text("never read\n", encoding="utf-8")
    tools = _fake_tools(tmp_path)
    tools["docker"] = "SKIP_PYTHON=1\nexport SKIP_PYTHON\n" + FAKE_DOCKER
    args = ("--tag", "v1.2.3", "--out", str(out), "--key-file", str(key))
    done = run(tmp_path, *args, script=script, shims=tools)
    assert done.returncode == 4, done.stderr
    assert "no environment python" in done.stderr and "nothing was signed" in done.stderr
    assert "==> stage 10/11 validate" in done.stdout and "stage 11/11" not in done.stdout
    assert not (out / "repo/dists/indus-stable/InRelease").exists()
    assert not (out / "repo/key.asc").exists()
