"""P07-17: `indus tools` — per-platform toolsets in `platform_toolsets.<platform>`."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from indus.cli import prompts
from indus.cli.commands import tools as tools_cmd
from indus.foundation import config, yamlio
from indus.foundation.config import loader
from indus.foundation.scope import Scope
from indus.tools import toolsets
from indus.tools.builtin import load_builtins
from indus.tools.registry import ToolRegistry
from tests.fakes.prompts import CANCEL, scripted
from tests.unit.cli.commands.cmdrun import run_cli

SCHEMA: dict[str, Any] = {
    "name": "radar_scan",
    "description": "Scan.",
    "parameters": {"type": "object", "properties": {}},
}


def _registry(scope: Scope) -> ToolRegistry:
    registry = ToolRegistry(clock=scope.clock)
    load_builtins(registry)
    registry.register(
        "radar_scan",
        "radar",
        SCHEMA,
        lambda **_: "",
        probe=lambda s: False,
        requires_secrets=("RADAR_API_KEY",),
    )
    registry.register(
        "sonar_ping",
        "sonar",
        {**SCHEMA, "name": "sonar_ping"},
        lambda **_: "",
        probe=lambda s: False,
    )
    return registry


@pytest.fixture(autouse=True)
def fake_registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(tools_cmd, "registry_factory", _registry)
    toolsets.clear_memo()
    yield
    toolsets.clear_memo()


def _saved(scope: Scope) -> Any:
    path = scope.home / "config.yaml"
    if not path.exists():
        return None
    raw = yamlio.load_safe(path.read_bytes()) or {}
    return raw.get("platform_toolsets")


def _offered(scope: Scope, platform: str = "cli") -> frozenset[str]:
    names = tools_cmd.saved_selection(scope, platform) or [tools_cmd.default_bundle(platform)]
    return toolsets.select(names, registry=_registry(scope), scope=scope, platform=platform).tools


def test_list_default_bundle(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    ran = run_cli(capsys, scope, "tools", "list")
    assert ran.code == 0, ran.err
    assert "Toolsets for cli (the indus-cli bundle):" in ran.out
    assert "[x] terminal" in ran.out
    assert "[x] web" in ran.out
    assert "[ ] vision" in ran.out
    assert _saved(scope) is None


def test_failing_probe_reason_shown(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    ran = run_cli(capsys, scope, "tools", "list")
    assert "radar_scan: needs RADAR_API_KEY" in ran.out
    assert "sonar_ping: its readiness check says no here" in ran.out
    assert "vision_analyze: not part of this build yet" in ran.out


def test_enable_disable_persist(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    ran = run_cli(capsys, scope, "tools", "enable", "vision", "radar")
    assert ran.code == 0, ran.err
    assert "Enabled for cli: vision, radar" in ran.out
    assert _saved(scope) == {"cli": ["indus-cli", "vision", "radar"]}
    loader.invalidate()
    assert {"vision_analyze", "radar_scan"} <= _offered(scope)

    ran = run_cli(capsys, scope, "tools", "disable", "radar")
    assert ran.code == 0, ran.err
    saved = _saved(scope)["cli"]
    assert "radar" not in saved and "vision" in saved and "terminal" in saved
    assert "radar_scan" not in _offered(scope)

    ran = run_cli(capsys, scope, "tools", "disable", "radar")
    assert "Nothing changed." in ran.out


def test_disable_web_from_default_bundle(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    ran = run_cli(capsys, scope, "tools", "disable", "web")
    assert ran.code == 0, ran.err
    offered = _offered(scope)
    assert "web_search" not in offered and "web_extract" not in offered
    assert {"terminal", "read_file", "todo_list"} <= offered
    assert "search" not in _saved(scope)["cli"]


def test_disable_bundle_keeps_core(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    run_cli(capsys, scope, "tools", "enable", "debugging", "vision")
    ran = run_cli(capsys, scope, "tools", "disable", "debugging")
    assert ran.code == 0, ran.err
    offered = _offered(scope)
    assert {"terminal", "web_search", "read_file", "vision_analyze"} <= offered


def test_unknown_toolset_did_you_mean(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    ran = run_cli(capsys, scope, "tools", "enable", "vison")
    assert ran.code == 1
    assert "Did you mean: vision" in ran.err
    assert "Nothing was written." in ran.err
    assert _saved(scope) is None
    ran = run_cli(capsys, scope, "tools", "list", "--platform", "pager")
    assert ran.code == 1
    assert "Unknown platform" in ran.err


def test_platform_option_and_summary(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    run_cli(capsys, scope, "tools", "disable", "web", "--platform", "cron")
    ran = run_cli(capsys, scope, "tools", "--summary")
    assert ran.code == 0
    lines = ran.out.strip().splitlines()
    assert lines[0].startswith("cli: ") and "web" in lines[0]
    assert lines[1].startswith("cron: ") and "web" not in lines[1]


def test_bare_without_terminal_lists(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    ran = run_cli(capsys, scope, "tools")
    assert ran.code == 0
    assert "Toolsets for cli" in ran.out


def _script(monkeypatch: pytest.MonkeyPatch, *answers: Any) -> None:
    io = scripted(*answers)
    monkeypatch.setattr(prompts, "io_factory", lambda _state, _guidance: io)


def test_checklist_toggles_and_saves(
    capsys: pytest.CaptureFixture[str], scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = tools_cmd.list_rows(scope, "cli", _registry(scope))
    keep = [i for i, r in enumerate(rows) if r.enabled and r.name not in ("web", "search")]
    _script(monkeypatch, keep)
    ran = run_cli(capsys, scope, "tools", tty=True)
    assert ran.code == 0, ran.err
    assert "Saved" in ran.out
    assert "web_search" not in _offered(scope)


def test_checklist_cancel_writes_nothing(
    capsys: pytest.CaptureFixture[str], scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    _script(monkeypatch, CANCEL)
    ran = run_cli(capsys, scope, "tools", tty=True)
    assert "No change." in ran.out
    assert _saved(scope) is None


def test_checklist_offers_web_provider_and_key(
    capsys: pytest.CaptureFixture[str], scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    # web on (default) but no provider ready: pick Brave (first) and give its key
    from indus.tools.builtin.web.providers import default_registry

    names = [p.name for p in default_registry().all() if p.can_search]
    fake = "test" + "-brave-" + "0000"
    rows = tools_cmd.list_rows(scope, "cli", _registry(scope))
    _script(monkeypatch, [i for i, r in enumerate(rows) if r.enabled], names.index("brave"), fake)
    monkeypatch.setattr(tools_cmd, "_web_reason", lambda s: "no provider")
    ran = run_cli(capsys, scope, "tools", tty=True)
    assert ran.code == 0, ran.err
    view = config.load(scope)
    assert view.get("web.backend") == "brave"
    assert scope.secrets is not None and scope.secrets.get("BRAVE_SEARCH_API_KEY") == fake


def test_chat_uses_the_saved_cli_selection(
    capsys: pytest.CaptureFixture[str], scope: Scope
) -> None:
    from indus.cli.runtime import DEFAULT_TOOLSETS, toolset_selection
    from indus.cli.session_select import LaunchPolicy

    assert toolset_selection(LaunchPolicy(), scope) == list(DEFAULT_TOOLSETS)
    assert run_cli(capsys, scope, "tools", "disable", "web").code == 0
    chosen = toolset_selection(LaunchPolicy(), scope)
    assert chosen == tools_cmd.saved_selection(scope, "cli") and "web" not in chosen
    assert toolset_selection(LaunchPolicy(toolsets=("web",)), scope) == ["web"]
    assert toolset_selection(LaunchPolicy(safe_mode=True), scope) == list(DEFAULT_TOOLSETS)
