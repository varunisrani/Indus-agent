"""P24-26: `capabilities.summary` counts a profile's skills, toolsets, MCP servers and plugins."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from indus.foundation import config
from indus.foundation.config import loader
from indus.foundation.scope import Scope
from indus.service import capabilities_rpc
from indus.service.capabilities_rpc import CapabilitiesSummaryParams, summarize


@dataclass
class Ctx:
    scope: Scope


def _skill(home: Path, name: str) -> None:
    folder = home / "skills" / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: about {name}\n---\n\nBody.\n", encoding="utf-8"
    )


def _plugin(home: Path, name: str, **manifest: Any) -> None:
    folder = home / "plugins" / name
    folder.mkdir(parents=True)
    data = {"name": name, "version": "1.0.0", "description": f"The {name} plugin.", **manifest}
    lines = [f"{k}: {json.dumps(v)}" for k, v in data.items()]
    (folder / "plugin.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (folder / "__init__.py").write_text("def register(ctx):\n    pass\n", encoding="utf-8")


def test_an_empty_profile_counts_nothing_but_toolsets(scope: Scope) -> None:
    result = summarize(scope)
    assert result.skills.total == 0 and result.mcp_servers.total == 0
    assert result.plugins.total == 0
    assert result.toolsets.total > 0 and 0 <= result.toolsets.enabled <= result.toolsets.total


def test_counts_follow_the_settings(scope: Scope) -> None:
    for name in ("brew-tea", "stretch", "water-plants"):
        _skill(scope.home, name)
    _plugin(scope.home, "weather")
    _plugin(scope.home, "tides")
    config.save(
        scope,
        {
            "skills": {"disabled": ["stretch"]},
            "mcp_servers": {
                "files": {"command": "files-server"},
                "docs": {"url": "https://mcp.example/docs", "enabled": False},
            },
        },
    )
    loader.invalidate(config.config_path(scope))
    result = summarize(scope)
    assert (result.skills.total, result.skills.enabled) == (3, 2)
    assert (result.mcp_servers.total, result.mcp_servers.enabled) == (2, 1)
    assert result.plugins.total == 2 and result.plugins.enabled == 0


def test_toolset_selection_changes_the_enabled_count(scope: Scope) -> None:
    before = summarize(scope).toolsets
    config.save(scope, {"platform_toolsets": {"cli": ["web"]}})
    loader.invalidate(config.config_path(scope))
    after = summarize(scope).toolsets
    assert after.total == before.total
    assert after.enabled < before.enabled


def test_the_handler_and_its_row() -> None:
    assert [row[0] for row in capabilities_rpc.CAPABILITY_METHODS] == ["capabilities.summary"]
    added: list[str] = []

    class Table:
        def add(self, name: str, handler: object) -> None:
            added.append(name)

    capabilities_rpc.register(Table())
    assert added == ["capabilities.summary"]


def test_the_handler_runs_off_the_loop(scope: Scope) -> None:
    _skill(scope.home, "solo")
    result = asyncio.run(
        capabilities_rpc.capabilities_summary(Ctx(scope), CapabilitiesSummaryParams())
    )
    assert result.skills.total == 1
