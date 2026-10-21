"""``scripts/install/build.py`` loaded by path, with the paths and helpers the installer tests use
(the built ``install.ps1``, its fragments, the stage table and the ``install.cmd`` shim)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


def _load() -> ModuleType:
    path = Path(__file__).resolve().parents[3] / "scripts" / "install" / "build.py"
    spec = importlib.util.spec_from_file_location("indus_install_build_for_tests", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUILD = _load()
ROOT: Path = BUILD.ROOT
PS_DIR: Path = BUILD.PS_DIR
STAGES_JSON: Path = BUILD.STAGES
BUILT_PS1: Path = ROOT / "scripts" / "install.ps1"
CMD_SHIM: Path = BUILD.INSTALL_CMD


def render_ps1() -> str:
    """What ``build.py`` writes to ``scripts/install.ps1``."""
    return BUILD.render_ps1()


def repository() -> str:
    """The release origin as a URL: ``pins.json`` ``release.repository`` (D-16.690)."""
    return f"https://github.com/{BUILD.release_repository()}"


def latest_asset_url(repository_url: str, name: str) -> str:
    """The newest release's asset under ``repository_url``."""
    slug = repository_url.removeprefix("https://github.com/").strip("/")
    return BUILD.latest_asset_url(slug, name)
