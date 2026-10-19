"""P13-14: plugin sources, their order, category folders and the allow-list gate."""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.plugins.discovery import PROJECT_PLUGINS_ENV, discover, set_enabled
from tests.unit.plugins._helpers import install_fixture, with_config, write_plugin

SENTINEL_CODE = (
    "import os, pathlib\n"
    "pathlib.Path(os.environ['INDUS_HOME'], 'imported-{name}').touch()\n"
    "def register(ctx):\n    pass\n"
)


@pytest.fixture
def bundled(tmp_path: Path) -> Path:
    folder = tmp_path / "bundled"
    folder.mkdir()
    return folder


@pytest.fixture
def user(scope: Scope) -> Path:
    folder = scope.home / "plugins"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


@dataclass
class FakeEntryPoint:
    name: str
    value: str
    loads: list[str]

    def load(self) -> Any:
        self.loads.append(self.name)
        raise AssertionError("discovery must never load an entry point")


def test_user_copy_overrides_bundled(scope: Scope, bundled: Path, user: Path) -> None:
    write_plugin(bundled, "tidy", {"name": "tidy", "version": "1.0"})
    write_plugin(user, "tidy", {"name": "tidy", "version": "2.0"})
    found = discover(with_config(scope, {"plugins": {"enabled": ["tidy"]}}), bundled_dir=bundled)
    record = found.get("tidy")
    assert record is not None and record.manifest is not None
    assert (record.source, record.manifest.version, record.state) == ("user", "2.0", "enabled")


def test_memory_provider_collision_keeps_bundled(scope: Scope, bundled: Path, user: Path) -> None:
    write_plugin(bundled / "memory", "recall", {"name": "recall", "version": "1.0"})
    write_plugin(user / "memory", "recall", {"name": "recall", "version": "9.0"})
    found = discover(scope, bundled_dir=bundled)
    record = found.get("memory/recall")
    assert record is not None and record.manifest is not None
    assert (record.source, record.manifest.version) == ("bundled", "1.0")
    assert record.handler == "memory" and not record.gated
    assert found.loadable() == []  # memory providers have their own selection
    assert [r.key for r in found.category("memory")] == ["memory/recall"]


def test_category_folders_route_one_level_down(scope: Scope, bundled: Path, user: Path) -> None:
    write_plugin(user / "tts", "voice", {"name": "fancy-voice"})
    write_plugin(user / "context_engine", "folding", {"name": "folding"})
    write_plugin(user / "notacategory", "inner", {"name": "inner"})
    found = discover(scope, bundled_dir=bundled)
    assert sorted(found.records) == ["context_engine/folding", "tts/voice"]
    voice = found.get("tts/voice")
    assert voice is not None and voice.state == "not-enabled" and voice.gated
    engine = found.get("context_engine/folding")
    assert engine is not None and engine.handler == "context_engine"


def test_bundled_backend_bypasses_gate_user_backend_does_not(
    scope: Scope, bundled: Path, user: Path
) -> None:
    write_plugin(bundled / "image_gen", "painter", {"name": "painter"})
    write_plugin(user / "image_gen", "sketcher", {"name": "sketcher"})
    write_plugin(bundled, "general", {"name": "general"})
    found = discover(scope, bundled_dir=bundled)
    painter, sketcher, general = (
        found.get("image_gen/painter"),
        found.get("image_gen/sketcher"),
        found.get("general"),
    )
    assert painter is not None and painter.state == "enabled" and not painter.gated
    assert sketcher is not None and sketcher.state == "not-enabled"
    assert general is not None and general.state == "not-enabled"  # bundled general: opt-in
    assert [r.key for r in found.loadable()] == ["image_gen/painter"]


def test_disabled_beats_enabled(scope: Scope, bundled: Path, user: Path) -> None:
    write_plugin(user, "both", {"name": "both"})
    write_plugin(bundled / "web", "searcher", {"name": "searcher"})
    config = {"plugins": {"enabled": ["both"], "disabled": ["both", "web/searcher"]}}
    found = discover(with_config(scope, config), bundled_dir=bundled)
    both = found.get("both")
    searcher = found.get("web/searcher")
    assert both is not None and both.state == "disabled"
    assert searcher is not None and searcher.state == "disabled"  # even past the bypass
    assert found.loadable() == []


def test_enabled_by_manifest_name(scope: Scope, bundled: Path, user: Path) -> None:
    write_plugin(user / "platforms", "chatline", {"name": "chatline-adapter"})
    found = discover(
        with_config(scope, {"plugins": {"enabled": ["chatline-adapter"]}}), bundled_dir=bundled
    )
    record = found.get("platforms/chatline")
    assert record is not None and record.state == "enabled"


def test_not_enabled_plugin_is_listed_but_never_imported(
    scope: Scope, bundled: Path, user: Path
) -> None:
    write_plugin(user, "quiet", {"name": "quiet"}, SENTINEL_CODE.format(name="quiet"))
    before = set(sys.modules)
    found = discover(scope, bundled_dir=bundled)
    record = found.get("quiet")
    assert record is not None and record.state == "not-enabled"
    assert "indus plugins enable quiet" in record.reason
    assert not Path(os.environ["INDUS_HOME"], "imported-quiet").exists()
    assert not any("quiet" in name for name in set(sys.modules) - before)


def test_project_plugins_need_opt_in(scope: Scope, bundled: Path, tmp_path: Path) -> None:
    project = tmp_path / "repo"
    write_plugin(project / ".indus" / "plugins", "local", {"name": "local"})
    assert discover(scope, bundled_dir=bundled, project_root=project).get("local") is None
    via_env = scope.with_env(scope.env.with_overlay({PROJECT_PLUGINS_ENV: "1"}))
    record = discover(via_env, bundled_dir=bundled, project_root=project).get("local")
    assert record is not None and record.source == "project"
    via_config = with_config(scope, {"plugins": {"project": True}})
    assert discover(via_config, bundled_dir=bundled, project_root=project).get("local") is not None


def test_gateway_never_reads_project_plugins(
    scope: Scope, bundled: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    project = tmp_path / "repo"
    write_plugin(project / ".indus" / "plugins", "local", {"name": "local"})
    opted = with_config(scope, {"plugins": {"project": True, "enabled": ["local"]}})
    with caplog.at_level(logging.WARNING):
        found = discover(opted, bundled_dir=bundled, project_root=project, gateway=True)
    assert found.get("local") is None
    assert any("gateway" in w for w in found.warnings)
    assert "gateway" in caplog.text


def test_project_copy_overrides_user(
    scope: Scope, bundled: Path, user: Path, tmp_path: Path
) -> None:
    project = tmp_path / "repo"
    write_plugin(user, "shared", {"name": "shared", "version": "1"})
    write_plugin(project / ".indus" / "plugins", "shared", {"name": "shared", "version": "2"})
    opted = with_config(scope, {"plugins": {"project": True}})
    record = discover(opted, bundled_dir=bundled, project_root=project).get("shared")
    assert record is not None and record.source == "project"


def test_entry_points_are_listed_without_import(scope: Scope, bundled: Path) -> None:
    loads: list[str] = []
    eps = [FakeEntryPoint("pkgplugin", "pkg_mod:register", loads)]
    found = discover(
        with_config(scope, {"plugins": {"enabled": ["pkgplugin"]}}),
        bundled_dir=bundled,
        entry_points=eps,
    )
    record = found.get("pkgplugin")
    assert record is not None and record.manifest is not None
    assert (record.source, record.state, record.manifest.path) == (
        "entrypoint",
        "enabled",
        "pkg_mod:register",
    )
    assert loads == []


def test_entry_point_overrides_user_copy(scope: Scope, bundled: Path, user: Path) -> None:
    write_plugin(user, "dual", {"name": "dual"})
    found = discover(scope, bundled_dir=bundled, entry_points=[FakeEntryPoint("dual", "m", [])])
    record = found.get("dual")
    assert record is not None and record.source == "entrypoint"


def test_broken_manifest_is_listed_as_broken(scope: Scope, bundled: Path, user: Path) -> None:
    folder = user / "cracked"
    folder.mkdir()
    (folder / "plugin.yaml").write_text("name: [oops\n", encoding="utf-8")
    found = discover(with_config(scope, {"plugins": {"enabled": ["cracked"]}}), bundled_dir=bundled)
    record = found.get("cracked")
    assert record is not None and record.state == "broken" and record.error
    assert found.loadable() == []


def test_dunder_and_hidden_folders_are_skipped(scope: Scope, bundled: Path, user: Path) -> None:
    write_plugin(user, "__pycache__", {"name": "cache"})
    write_plugin(user, ".hidden", {"name": "hidden"})
    assert discover(scope, bundled_dir=bundled).records == {}


def test_fixture_plugins_listed(scope: Scope, bundled: Path, user: Path) -> None:
    for name in ("good", "raising", "capabilities"):
        install_fixture(user, name)
    found = discover(with_config(scope, {"plugins": {"enabled": ["good"]}}), bundled_dir=bundled)
    assert sorted(found.records) == ["capabilities", "good", "raising"]
    assert [r.key for r in found.loadable()] == ["good"]


def test_shipped_bundled_folder_is_empty(scope: Scope) -> None:
    assert discover(scope).records == {}


def test_set_enabled_updates_saved_tool_selections() -> None:
    tree: dict[str, Any] = {
        "plugins": {"enabled": [], "disabled": ["tidy"]},
        "platform_toolsets": {"cli": ["file", "web"], "telegram": ["web"], "broken": "x"},
    }
    set_enabled(tree, "tidy", True, toolsets=["tidy-tools"])
    assert tree["plugins"] == {"enabled": ["tidy"], "disabled": []}
    assert tree["platform_toolsets"]["cli"] == ["file", "web", "tidy-tools"]
    assert tree["platform_toolsets"]["telegram"] == ["web", "tidy-tools"]
    set_enabled(tree, "tidy", True, toolsets=["tidy-tools"])  # idempotent
    assert tree["platform_toolsets"]["cli"] == ["file", "web", "tidy-tools"]
    set_enabled(tree, "tidy", False, toolsets=["tidy-tools"])
    assert tree["plugins"] == {"enabled": [], "disabled": ["tidy"]}
    assert tree["platform_toolsets"]["cli"] == ["file", "web"]
    assert tree["platform_toolsets"]["broken"] == "x"


def test_set_enabled_without_saved_selection_adds_none() -> None:
    tree: dict[str, Any] = {}
    set_enabled(tree, "fresh", True, toolsets=["fresh"])
    assert tree == {"plugins": {"enabled": ["fresh"], "disabled": []}}
