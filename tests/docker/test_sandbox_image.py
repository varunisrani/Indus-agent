"""The sandbox images, built (P23-09, D-16.64). See conftest for the lane."""

from __future__ import annotations

import secrets

import pytest

from indus.backends.docker.args import run_args
from indus.backends.docker.probe import HostFacts
from indus.backends.docker.settings import DockerSettings
from tests.docker.conftest import VARIANT
from tests.integration.backends._live import docker

pytestmark = pytest.mark.docker


def test_slim_toolbox(sandbox_image: str) -> None:
    done = docker(
        "run",
        "--rm",
        sandbox_image,
        "bash",
        "-lc",
        "git --version && python3 --version && rg --version && make --version",
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    assert "git version" in done.stdout and "Python 3" in done.stdout and "ripgrep" in done.stdout


def test_browser_target_starts_chromium_headless(sandbox_image: str) -> None:
    if VARIANT != "browser":
        pytest.skip("slim target")
    done = docker(
        "run",
        "--rm",
        "--shm-size",
        "1g",
        sandbox_image,
        "chromium",
        "--headless",
        "--no-sandbox",
        "--disable-gpu",
        "--dump-dom",
        "about:blank",
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    assert "<html" in done.stdout.lower()


def test_the_backend_hardened_flags_run_unmodified(sandbox_image: str) -> None:
    flags = run_args(DockerSettings(image=sandbox_image), HostFacts(cgroups=True))
    name = f"indus-sandbox-test-{secrets.token_hex(4)}"
    started = docker(
        "run",
        "-d",
        "--init",
        "--name",
        name,
        *flags.args,
        sandbox_image,
        "sleep",
        "infinity",
        timeout=120,
    )
    try:
        assert started.returncode == 0, started.stderr
        done = docker(
            "exec",
            name,
            "bash",
            "-c",
            "cd /tmp && echo hi > probe && cat probe && git init -q repo && id -u",
            timeout=60,
        )
        assert done.returncode == 0, done.stderr
        assert done.stdout.split() == ["hi", "0"]
    finally:
        docker("rm", "-f", name, timeout=60)


def test_non_root_user_without_sudo(sandbox_image: str) -> None:
    done = docker(
        "run",
        "--rm",
        "--user",
        "indus",
        sandbox_image,
        "bash",
        "-c",
        "id -u; command -v sudo || echo no-sudo",
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["10000", "no-sudo"]
