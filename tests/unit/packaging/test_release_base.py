"""One release base (D-16.690): every place that names where releases and images live is
``RELEASE_REPOSITORY`` (``src/indus/foundation/versions.py``) or a copy held equal to it here.

The owner has not settled the GitHub owner yet; changing the constant and rerunning the generators
(``scripts/install/pins.py update``, ``scripts/nix/pins.py``) must be the whole change, and these
tests name every copy that would be left behind.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from indus.foundation.config.schema import default_for
from indus.foundation.versions import RELEASE_IMAGE, RELEASE_REPOSITORY, SANDBOX_IMAGE
from scripts.release.claims import IMAGES

ROOT = Path(__file__).resolve().parents[3]
GITHUB = f"https://github.com/{RELEASE_REPOSITORY}"

# Product files that may name our repository or registry (generated build output, tests and
# dependencies are not scanned).
SCANNED = (
    "src",
    "scripts",
    "docker",
    "nix",
    ".github",
    "flake.nix",
    "apps/desktop/electron",
    "apps/desktop/src",
    "apps/desktop/scripts",
    "apps/desktop/build",
    "apps/desktop/package.json",
    "apps/desktop/electron-builder.config.cjs",
)
SUFFIXES = {
    ".py", ".sh", ".ps1", ".cmd", ".json", ".yml", ".yaml", ".nix", ".ts", ".tsx", ".mjs",
    ".cjs", "",
}  # fmt: skip
SKIP_PARTS = {"node_modules", "__pycache__", "dist", "dist-electron"}
SKIP_DIRS = ("apps/desktop/build/release",)  # packaged output
# Our repository (its name starts with "indus"), our images, the releases API.
OURS = re.compile(
    r"(?:github\.com[:/]|api\.github\.com/repos/)([A-Za-z0-9_.-]+)/([iI]ndus[A-Za-z0-9_.-]*)"
    r"|ghcr\.io/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)"
)


def _files() -> list[Path]:
    found: list[Path] = []
    for rel in SCANNED:
        path = ROOT / rel
        if path.is_file():
            found.append(path)
        elif path.is_dir():
            found.extend(
                p
                for p in sorted(path.rglob("*"))
                if p.is_file()
                and p.suffix in SUFFIXES
                and not SKIP_PARTS.intersection(p.relative_to(ROOT).parts)
                and not p.relative_to(ROOT).as_posix().startswith(SKIP_DIRS)
                and not p.name.endswith((".test.ts", ".test.tsx", ".test.mjs"))
            )
    return found


def test_every_named_repository_or_image_is_the_release_base() -> None:
    images = {RELEASE_IMAGE.removeprefix("ghcr.io/"), SANDBOX_IMAGE.removeprefix("ghcr.io/")}
    wrong: list[str] = []
    for path in _files():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for match in OURS.finditer(text):
            owner, name, image = match.groups()
            if image is not None:
                if image.lower() not in images:
                    wrong.append(f"{path.relative_to(ROOT)}: ghcr.io/{image}")
                continue
            slug = f"{owner}/{name.removesuffix('.git')}"
            if slug.lower() != RELEASE_REPOSITORY.lower():
                wrong.append(f"{path.relative_to(ROOT)}: {match.group(0)}")
    assert wrong == [], f"name {RELEASE_REPOSITORY} (RELEASE_REPOSITORY) instead: {wrong}"


def test_installer_pins_name_the_release_base() -> None:
    pins = json.loads((ROOT / "scripts/install/pins.json").read_text(encoding="utf-8"))
    assert pins["release"]["repository"] == RELEASE_REPOSITORY
    for tool in ("uv", "git_for_windows"):
        for entry in pins[tool]["targets"].values():
            assert entry["mirror"].startswith(f"{GITHUB}/releases/download/"), entry["mirror"]


def test_install_cmd_downloads_the_release_base_installer() -> None:
    text = (ROOT / "scripts/install.cmd").read_bytes().decode("ascii")
    url = f"{GITHUB}/releases/latest/download/install.ps1"
    assert f'set "INDUS_INSTALL_PS1={url}"' in text


def test_docker_images_are_the_release_images() -> None:
    images = json.loads((ROOT / "docker/pins.json").read_text(encoding="utf-8"))["images"]
    base = f"{images['registry']}/{images['namespace']}"
    assert f"{base}/{images['product']}" == RELEASE_IMAGE
    assert f"{base}/{images['sandbox']}" == SANDBOX_IMAGE
    for name in ("compose.yaml", "compose.windows.yaml"):
        assert f"${{INDUS_IMAGE:-{RELEASE_IMAGE}:stable}}" in (ROOT / "docker" / name).read_text()
    for name in ("Dockerfile", "sandbox/Dockerfile"):
        text = (ROOT / "docker" / name).read_text(encoding="utf-8")
        assert f'org.opencontainers.image.source="{GITHUB}"' in text, name


def test_release_images_and_the_sandbox_default() -> None:
    assert {spec.repository for spec in IMAGES} == {RELEASE_IMAGE, SANDBOX_IMAGE}
    assert default_for("terminal.docker_image") == f"{SANDBOX_IMAGE}:stable-slim"
    aliases = {spec.tag_for("stable") for spec in IMAGES if spec.repository == SANDBOX_IMAGE}
    assert aliases == {"stable-slim", "stable-browser"}


def test_nix_container_default_is_the_release_image() -> None:
    pins = json.loads((ROOT / "nix/pins.json").read_text(encoding="utf-8"))
    assert pins["image"] == RELEASE_IMAGE
    module = (ROOT / "nix/nixos-module.nix").read_text(encoding="utf-8")
    assert 'default = "${pins.image}:stable";' in module


DESKTOP_RELEASE_BASE = ROOT / "apps/desktop/electron/releaseBase.ts"


def test_desktop_release_base_is_the_release_repository() -> None:
    text = DESKTOP_RELEASE_BASE.read_text(encoding="utf-8")
    found = re.findall(r'^export const RELEASE_REPOSITORY = "([^"]+)";$', text, re.M)
    assert found == [RELEASE_REPOSITORY]
    assert "https://github.com/${RELEASE_REPOSITORY}/releases/download" in text
    assert "https://api.github.com/repos/${RELEASE_REPOSITORY}/releases" in text


@pytest.mark.parametrize(
    ("rel", "line"),
    [
        (
            "apps/desktop/electron/updates/feed.ts",
            "export const DEFAULT_RELEASES_API = RELEASES_API;",
        ),
        (
            "apps/desktop/electron/installer/runner.ts",
            "export const DEFAULT_RELEASE_BASE = RELEASE_DOWNLOAD_BASE;",
        ),
    ],
)
def test_desktop_defaults_derive_from_the_desktop_release_base(rel: str, line: str) -> None:
    text = (ROOT / rel).read_text(encoding="utf-8")
    assert '../releaseBase.ts";' in text
    assert line in text


@pytest.mark.parametrize(
    "text",
    [
        "https://github.com/someone/indus-agent/releases",
        "https://api.github.com/repos/someone/indus-agent/releases",
        "ghcr.io/someone/indus-agent:stable",
    ],
)
def test_the_scan_catches_another_owner(text: str) -> None:
    match = OURS.search(text)
    assert match is not None
    owner, name, image = match.groups()
    named = image if image is not None else f"{owner}/{name}"
    assert named.lower() not in {
        RELEASE_REPOSITORY,
        RELEASE_IMAGE.removeprefix("ghcr.io/"),
        SANDBOX_IMAGE.removeprefix("ghcr.io/"),
    }
