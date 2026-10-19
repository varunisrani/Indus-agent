"""Shared bits for the plugin tests: fixture plugins copied into a profile, config, writers."""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from indus.foundation.scope import Scope
from tests.fakes.agent import DictConfig

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "plugins"


def with_config(scope: Scope, data: Mapping[str, Any]) -> Scope:
    return scope.with_config(DictConfig(data))


def install_fixture(root: Path, name: str, *, as_name: str | None = None) -> Path:
    """Copy fixture plugin ``name`` into ``root`` (a plugins folder) and return its folder."""
    target = root / (as_name or name)
    shutil.copytree(FIXTURES / name, target)
    return target


def write_plugin(
    root: Path,
    folder: str,
    manifest: Mapping[str, Any] | None = None,
    code: str = "def register(ctx):\n    pass\n",
    *,
    as_json: bool = False,
) -> Path:
    """A plugin folder written on the spot: ``plugin.yaml`` (or ``.json``) plus ``__init__.py``."""
    target = root / folder
    target.mkdir(parents=True, exist_ok=True)
    data = dict(manifest if manifest is not None else {"name": folder})
    if as_json:
        (target / "plugin.json").write_text(json.dumps(data), encoding="utf-8")
    else:
        lines = [f"{k}: {json.dumps(v)}" for k, v in data.items()]
        (target / "plugin.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (target / "__init__.py").write_text(code, encoding="utf-8")
    return target
