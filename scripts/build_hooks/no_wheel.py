"""Hatchling build hook: no wheels or sdists outside our Nix build (P23-01, S7, D-16.14).

Indus Agent is installed by `install.sh` / `install.ps1`, the Docker image or the Nix flake, never
from PyPI. A wheel or sdist built anywhere else would be a package nobody tested (no built web or
TUI pages, no pinned bootstrap), so the hook refuses it unless ``INDUS_NIX_BUILD=1`` (set only by
the Nix derivation). Editable installs (``uv sync``, ``pip install -e``) reach hatchling as the
``wheel`` target with version ``editable`` and are always allowed.

`refusal` is the whole decision and imports nothing, so tests call it without hatchling installed.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

NIX_FLAG = "INDUS_NIX_BUILD"

MESSAGE = (
    "Indus Agent is not distributed as a wheel or an sdist (no PyPI).\n"
    "Install it with one of the supported channels:\n"
    "  - the installer:  curl -fsSL <release>/install.sh | bash   (Windows: install.ps1)\n"
    "  - the Docker image: docker run ghcr.io/<owner>/indus-agent\n"
    "  - the Nix flake:  nix run <flake>#default\n"
    "Developing? Use an editable install instead: uv sync\n"
    f"(Package builders: only the Nix derivation may build one, with {NIX_FLAG}=1.)"
)


def refusal(target: str, version: str, env: Mapping[str, str]) -> str | None:
    """The refusal message for this build, or None when it may go ahead."""
    if target == "wheel" and version == "editable":
        return None
    if env.get(NIX_FLAG) == "1":
        return None
    if target in ("wheel", "sdist"):
        return MESSAGE
    return None


if TYPE_CHECKING:
    _Base = object
else:
    try:
        from hatchling.builders.hooks.plugin.interface import BuildHookInterface as _Base
    except ImportError:  # only the build environment has hatchling
        _Base = object


class NoWheelHook(_Base):
    """Raises before any file is written when `refusal` says no."""

    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        target = str(getattr(self, "target_name", ""))
        message = refusal(target, version, os.environ)
        if message is not None:
            raise RuntimeError(message)
