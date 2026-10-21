"""Compose files and the hadolint policy (P23-11, D-16.25)."""

from __future__ import annotations

import json
import re
import shlex
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML

from indus.foundation.scope import Scope
from indus.web.command import Launch, run_launch
from tests.unit.packaging.docker_files import DOCKERFILE, ROOT, SANDBOX_DOCKERFILE
from tests.unit.web.conftest import basic_config

LINUX = ROOT / "docker" / "compose.yaml"
WINDOWS = ROOT / "docker" / "compose.windows.yaml"
HADOLINT = ROOT / ".hadolint.yaml"


def load(path: Path) -> dict[str, Any]:
    data = YAML(typ="safe").load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def services(path: Path) -> dict[str, dict[str, Any]]:
    return load(path)["services"]


def pinned_image() -> str:
    pins = json.loads((ROOT / "docker" / "pins.json").read_text(encoding="utf-8"))["images"]
    return f"{pins['registry']}/{pins['namespace']}/{pins['product']}"


# -- both files -----------------------------------------------------------------------------------


@pytest.mark.parametrize("path", [LINUX, WINDOWS])
def test_two_services_from_one_image(path: Path) -> None:
    svc = services(path)
    assert sorted(svc) == ["dashboard", "gateway"]
    assert svc["gateway"]["image"] == svc["dashboard"]["image"]
    assert svc["gateway"]["image"] == "${INDUS_IMAGE:-" + pinned_image() + ":stable}"
    assert svc["gateway"]["command"] == ["gateway", "run"]
    assert svc["dashboard"]["depends_on"] == ["gateway"]
    for one in svc.values():
        assert one["restart"] == "unless-stopped"
        assert one["environment"] == {
            "INDUS_UID": "${INDUS_UID:-10000}",
            "INDUS_GID": "${INDUS_GID:-10000}",
        }
        assert len(one["volumes"]) == 1 and one["volumes"][0].endswith(":/opt/data")


@pytest.mark.parametrize("path", [LINUX, WINDOWS])
def test_no_insecure_switch_and_no_latest_tag(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert "--insecure" not in text and ":latest" not in text
    for one in services(path).values():
        assert not any("insecure" in str(word) for word in one["command"])


def test_linux_uses_host_networking_and_a_loopback_dashboard() -> None:
    svc = services(LINUX)
    assert all(one["network_mode"] == "host" for one in svc.values())
    assert svc["dashboard"]["command"] == ["dashboard", "--host", "127.0.0.1", "--no-open"]
    assert svc["gateway"]["volumes"] == ["${INDUS_DATA_DIR:-~/.indus}:/opt/data"]
    assert "ports" not in svc["dashboard"]


def test_windows_has_no_host_networking_and_publishes_loopback_only() -> None:
    svc = services(WINDOWS)
    assert all("network_mode" not in one for one in svc.values())
    assert svc["dashboard"]["ports"] == ["127.0.0.1:9119:9119"]
    assert "ports" not in svc["gateway"]
    assert svc["gateway"]["volumes"] == ["${INDUS_DATA_DIR:-${USERPROFILE}/.indus}:/opt/data"]
    header = WINDOWS.read_text(encoding="utf-8")
    assert "login provider" in header and "no switch" in header


# -- the Windows dashboard against the real launch (P18 behaviour) --------------------------------


def dashboard_launch(scope: Scope, served: list[Any]) -> Launch:
    """The Windows compose file's dashboard command, parsed by the real click command."""
    from indus.web.command import dashboard_command

    argv = services(WINDOWS)["dashboard"]["command"]
    assert argv[0] == "dashboard"
    ctx = dashboard_command.make_context("dashboard", list(argv[1:]))
    params = ctx.params
    assert params["host"] == "0.0.0.0" and params["no_open"] is True

    def bind(host: str, port: int) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))  # never the wildcard address in a test
        return sock

    return Launch(
        scope=scope,
        mode="dashboard",
        host=params["host"],
        port=params["port"],
        port_given=True,
        open_browser=not params["no_open"],
        isolated=params["isolated"],
        interactive=False,
        build=False,
        owner=lambda root: None,
        bind=bind,
        serve=lambda app, sock: served.append(app),
    )


def test_windows_dashboard_without_a_login_provider_refuses_to_start(
    scope: Scope, capsys: pytest.CaptureFixture[str]
) -> None:
    served: list[Any] = []
    assert run_launch(dashboard_launch(scope, served)) == 1
    err = capsys.readouterr().err
    assert "needs a login provider to serve beyond this machine" in err
    assert "basic: no username set" in err
    assert served == []


def test_windows_dashboard_with_a_login_provider_serves(scope: Scope) -> None:
    from indus.foundation.config import writer
    from indus.foundation.profiles import DEFAULT_PROFILE, profile_home
    from indus.web.scope import build_profile_scope

    # the machine dashboard serves with the default profile's settings (the container's profile)
    default = build_profile_scope(scope, profile_home(scope.root, DEFAULT_PROFILE))
    writer.save(default, basic_config())
    served: list[Any] = []
    assert run_launch(dashboard_launch(scope, served)) == 0
    assert served and served[0].state.indus.gated


# -- hadolint -------------------------------------------------------------------------------------


def test_hadolint_policy() -> None:
    data = load(HADOLINT)
    assert data["failure-threshold"] == "warning"
    assert data["trustedRegistries"] == ["docker.io", "ghcr.io"]
    text = HADOLINT.read_text(encoding="utf-8")
    for rule in data["ignored"]:
        assert re.match(r"^DL\d{4}$|^SC\d{4}$", rule)
        reason = re.search(rf"#\s*{rule}\b[^\n]*\n(?:\s*#[^\n]*\n)*\s*- {rule}", text)
        assert reason, f"{rule} is ignored without a written reason"


def test_hadolint_is_clean_when_installed() -> None:
    hadolint = shutil.which("hadolint")
    if hadolint is None:
        pytest.skip("hadolint is not installed (the Docker workflow's lint job runs it)")
    done = subprocess.run(
        [hadolint, "--config", str(HADOLINT), str(DOCKERFILE), str(SANDBOX_DOCKERFILE)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_compose_config_is_valid_when_docker_compose_is_installed(tmp_path: Path) -> None:
    docker = shutil.which("docker")
    probe = (
        subprocess.run([docker, "compose", "version"], capture_output=True, check=False)
        if docker
        else None
    )
    if docker is None or probe is None or probe.returncode != 0:
        pytest.skip("docker compose is not installed")
    for path in (LINUX, WINDOWS):
        env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)}
        done = subprocess.run(
            [docker, "compose", "-f", str(path), "config", "--quiet"],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert done.returncode == 0, shlex.join([str(path), done.stderr])
