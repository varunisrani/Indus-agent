"""Packaging metadata in ``pyproject.toml`` (P23-01, R10.11, R10.12, ACC-S8-1)."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

import indus

ROOT = Path(__file__).resolve().parents[3]

# The extras `messaging` and `all` are written out as exact pins (the pins guard refuses a
# self-reference); these are the extras each one stands for.
MESSAGING_MEMBERS = (
    "discord",
    "slack",
    "mattermost",
    "google-chat",
    "qqbot",
    "dingtalk",
    "wecom",
    "feishu",
    "simplex",
    "buzz",
    "weixin",
    "yuanbao",
)
ALL_MEMBERS = ("web", "docs", "pty", "dashboard", "browser", "acp", "messaging")


def _pyproject() -> dict[str, Any]:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _requirements(data: dict[str, Any]) -> list[str]:
    project = data["project"]
    out = list(project.get("dependencies", []))
    for reqs in project.get("optional-dependencies", {}).values():
        out.extend(reqs)
    for reqs in data.get("dependency-groups", {}).values():
        out.extend(r for r in reqs if isinstance(r, str))
    out.extend(data["build-system"]["requires"])
    return out


def test_one_interpreter_line() -> None:
    assert _pyproject()["project"]["requires-python"] == ">=3.13,<3.15"


def test_no_interpreter_markers_or_open_ranges() -> None:
    for req in _requirements(_pyproject()):
        assert "python_version" not in req and "python_full_version" not in req, req
        spec = req.split(";", 1)[0]
        if ">=" in spec:
            assert re.search(r">=[^,]+,\s*<", spec), f"unbounded >= in {req!r}"
        assert "==" in spec, f"not an exact pin: {req!r}"


def test_source_version_is_zero() -> None:
    data = _pyproject()
    assert "version" not in data["project"]
    assert data["project"]["dynamic"] == ["version"]
    assert data["tool"]["hatch"]["version"]["path"] == "src/indus/__init__.py"
    assert indus.__version__ == "0.0.0"


def test_console_script_and_build_backend() -> None:
    data = _pyproject()
    assert data["project"]["scripts"] == {"indus": "indus.cli.entry:main"}
    assert data["build-system"]["build-backend"] == "hatchling.build"
    assert all("==" in r for r in data["build-system"]["requires"])


def _union(extras: dict[str, list[str]], members: tuple[str, ...]) -> set[str]:
    return {req for name in members for req in extras[name]}


def test_messaging_and_all_are_unions_of_their_members() -> None:
    extras = _pyproject()["project"]["optional-dependencies"]
    assert set(extras["messaging"]) == _union(extras, MESSAGING_MEMBERS)
    assert len(extras["messaging"]) == len(set(extras["messaging"]))
    assert set(extras["all"]) == _union(extras, ALL_MEMBERS)
    assert len(extras["all"]) == len(set(extras["all"]))


def test_installer_extras_are_the_all_members() -> None:
    """The installers' extras (``stages.json``, read by install.sh and install.ps1) and the Docker
    image's default extras are the members of ``all``."""
    table = json.loads((ROOT / "scripts/install/stages.json").read_text(encoding="utf-8"))
    extras = table["extras"]
    installer = {*extras["always"], *extras["unless_skip_browser"]}
    installer |= set(extras["unless_skip_computer_use"])
    assert installer == set(ALL_MEMBERS)
    dockerfile = (ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
    found = re.search(r'^ARG INDUS_EXTRAS="([^"]*)"$', dockerfile, re.MULTILINE)
    assert found is not None
    assert found.group(1).split() == extras["always"], "the image carries the installer's extras"


def test_package_data_sources_exist() -> None:
    wheel = _pyproject()["tool"]["hatch"]["build"]["targets"]["wheel"]
    for source in wheel["force-include"]:
        assert (ROOT / source).exists(), source
    assert "src/indus/web/static/" in wheel["artifacts"]
    assert "src/indus/_tui_dist/" in wheel["artifacts"]
