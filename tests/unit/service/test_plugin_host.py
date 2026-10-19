"""``service.plugin_host``: the maintenance update check, selections and ``/plugins``."""

from __future__ import annotations

from typing import Any

import pytest

from indus.cli.slash.dispatch import dispatch
from indus.foundation.scope import Scope
from indus.plugins import install
from indus.service import plugin_host
from indus.service.plugin_host import PluginUpkeep, update_check
from indus.tools.registry import ToolRegistry
from tests.unit.cli.slash._fakes import make_ctx


def test_update_check_does_nothing_without_installed_plugins(
    scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[Any] = []
    monkeypatch.setattr(install.Installer, "maybe_check_updates", lambda self: called.append(1))
    assert update_check(scope)() is None
    assert called == [] and not (scope.home / "plugins").exists()


def test_update_check_runs_the_periodic_check_when_something_is_installed(
    scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(install.Installer, "records", lambda self: {"x": {"kind": "git"}})
    monkeypatch.setattr(install.Installer, "maybe_check_updates", lambda self: ["checked"])
    assert update_check(scope)() == ["checked"]


def test_selection_adds_plugin_toolsets_once(scope: Scope) -> None:
    upkeep = PluginUpkeep(scope, ToolRegistry(clock=scope.clock), surface="cli")
    upkeep._toolsets = ["greeter", "web"]  # pyright: ignore[reportPrivateUsage]
    assert upkeep.selection(["indus-cli", "web"]) == ["indus-cli", "web", "greeter"]
    assert upkeep.selection(None) is None


def test_slash_plugins_lists_and_toggles(scope: Scope) -> None:
    folder = scope.home / "plugins" / "greeter"
    folder.mkdir(parents=True)
    (folder / "plugin.yaml").write_text(
        "name: greeter\nversion: 1.0.0\nmanifest_version: 2\n", encoding="utf-8"
    )
    (folder / "__init__.py").write_text("def register(ctx):\n    pass\n", encoding="utf-8")
    ctx = make_ctx(scope)
    dispatch("/plugins", ctx)
    [line] = ctx.lines
    assert line.startswith("greeter") and "not-enabled" in line
    ctx = make_ctx(scope)
    dispatch("/plugins enable greeter", ctx)
    assert ctx.lines[0].startswith("Plugin greeter enabled")
    assert "next time indus starts" in ctx.lines[0]
    ctx = make_ctx(scope)
    dispatch("/plugins enable", ctx)
    assert ctx.lines[0].startswith("Usage: /plugins")
    assert plugin_host.upkeep_for(scope) is None
