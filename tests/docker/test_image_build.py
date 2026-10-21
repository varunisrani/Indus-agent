"""The product image, built (P23-08, P23-10, ACC-S3-1, ACC-S3-2). See conftest for the lane."""

from __future__ import annotations

import json
import secrets
from collections.abc import Iterator

import pytest

from tests.docker.conftest import VARIANT
from tests.integration.backends._live import docker
from tests.unit.packaging.docker_files import banned_hits

pytestmark = pytest.mark.docker


@pytest.fixture
def volume(product_image: str) -> Iterator[str]:
    name = f"indus-test-{secrets.token_hex(4)}"
    assert docker("volume", "create", name).returncode == 0
    try:
        yield name
    finally:
        docker("volume", "rm", "-f", name)


def test_version_prints_the_product_and_the_docker_install(product_image: str) -> None:
    done = docker("run", "--rm", product_image, "--version", timeout=180)
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("indus ") or "\nindus " in done.stdout
    assert "docker" in done.stdout


def test_programs_run_as_uid_10000_after_stage2(product_image: str, volume: str) -> None:
    done = docker(
        "run", "--rm", "-v", f"{volume}:/opt/data", product_image, "id", "-u", timeout=180
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().splitlines()[-1] == "10000"


def test_uid_remap_through_the_environment(product_image: str, volume: str) -> None:
    done = docker(
        "run",
        "--rm",
        "-e",
        "INDUS_UID=1234",
        "-e",
        "INDUS_GID=2345",
        "-v",
        f"{volume}:/opt/data",
        product_image,
        "sh",
        "-c",
        "id -u; id -g; stat -c %u:%g /opt/data",
        timeout=180,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().splitlines()[-3:] == ["1234", "2345", "1234:2345"]


def test_a_docker_run_user_start_is_refused(product_image: str) -> None:
    """ACC-S3-2 / R10.25."""
    done = docker("run", "--rm", "--user", "1000:1000", product_image, "--version", timeout=180)
    assert done.returncode != 0
    assert "INDUS_UID" in done.stderr + done.stdout


def test_a_leading_flag_goes_to_indus(product_image: str) -> None:
    done = docker("run", "--rm", product_image, "--help", timeout=180)
    assert done.returncode == 0, done.stderr
    assert "Usage:" in done.stdout and "indus" in done.stdout


def test_data_is_a_volume_and_the_entry_is_ours(product_image: str) -> None:
    done = docker("image", "inspect", "--format", "{{json .Config}}", product_image)
    config = json.loads(done.stdout)
    assert "/opt/data" in (config.get("Volumes") or {})
    assert config["Entrypoint"] == ["/opt/indus/docker/entrypoint.sh"]
    assert not config.get("Cmd")
    assert "INDUS_HOME=/opt/data" in config["Env"]


def test_no_reference_names_in_history_or_the_install_tree(product_image: str) -> None:
    history = docker("history", "--no-trunc", "--format", "{{.CreatedBy}}", product_image)
    assert history.returncode == 0, history.stderr
    files = docker(
        "run",
        "--rm",
        "--entrypoint",
        "sh",
        product_image,
        "-c",
        "find /opt/indus \\( -path /opt/indus/.venv -o -path /opt/indus/python \\)"
        " -prune -o -print",
        timeout=180,
    )
    assert files.returncode == 0, files.stderr
    assert banned_hits({"docker history": history.stdout, "file list": files.stdout}) == []


def test_browser_variant_ships_chromium(product_image: str) -> None:
    if VARIANT != "browser":
        pytest.skip("slim variant")
    done = docker("run", "--rm", product_image, "chromium", "--version", timeout=180)
    assert done.returncode == 0, done.stderr
    assert "Chromium" in done.stdout


def test_stamp_file_is_in_the_code_tree(product_image: str) -> None:
    done = docker("run", "--rm", "--entrypoint", "cat", product_image, "/opt/indus/.install_method")
    assert done.stdout.strip() == "docker"
