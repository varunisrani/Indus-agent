"""P13-22: capability, dependency and env consent (R07.19)."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.plugins import consent
from indus.plugins.context import PluginHost
from indus.plugins.hooks import HookBus
from indus.plugins.manifest import EnvRequirement, PluginManifest
from indus.tools.registry import ToolRegistry
from tests.fakes.agent import DictConfig

NOW = "2026-10-19T10:00:00+00:00"


def manifest(*caps: str, **fields: Any) -> PluginManifest:
    return PluginManifest(name="hello", capabilities=caps, source="user", **fields)


def has(scope: Scope, tree: Mapping[str, Any], plugin: PluginManifest, cap: str) -> bool:
    configured = scope.with_config(DictConfig(tree))
    host = PluginHost(configured, ToolRegistry(), HookBus())
    return host.context(plugin).has_capability(cap)


def test_interactive_grant_stored_with_hash(scope: Scope) -> None:
    plugin = manifest("tools.override", "llm.model")
    tree: dict[str, Any] = {}
    shown: list[str] = []

    def ask(text: str) -> bool:
        shown.append(text)
        return True

    result = consent.consent_capabilities(tree, plugin, ask=ask, now=NOW)
    assert result.asked and result.granted == ["tools.override", "llm.model"]
    assert "not a sandbox" in shown[0] and consent.CAPABILITIES["llm.model"] in shown[0]
    entry = tree["plugins"]["entries"]["hello"]
    assert entry[consent.GRANTED_KEY] == [
        {"name": "tools.override", "granted_at": NOW},
        {"name": "llm.model", "granted_at": NOW},
    ]
    assert entry[consent.CONSENT_KEY] == {
        "hash": consent.declared_hash(["llm.model", "tools.override"]),
        "declared": ["llm.model", "tools.override"],
        "at": NOW,
    }
    assert has(scope, tree, plugin, "llm.model")
    # Asked once: the same declared set is not asked again.
    again = consent.consent_capabilities(tree, plugin, ask=lambda t: pytest.fail("asked twice"))
    assert not again.asked


def test_refusal_is_recorded_and_capability_stays_off(scope: Scope) -> None:
    plugin = manifest("llm.provider")
    tree: dict[str, Any] = {}
    result = consent.consent_capabilities(tree, plugin, ask=lambda text: False, now=NOW)
    assert result.refused == ["llm.provider"] and result.granted == []
    assert tree["plugins"]["entries"]["hello"][consent.GRANTED_KEY] == []
    assert not has(scope, tree, plugin, "llm.provider")
    assert consent.new_capabilities(tree, "hello", ["llm.provider"]) == []


def test_update_adding_capability_stays_off_until_granted(scope: Scope) -> None:
    tree: dict[str, Any] = {}
    consent.consent_capabilities(tree, manifest("tools.override"), ask=lambda t: True, now=NOW)
    newer = manifest("tools.override", "llm.model")
    waiting = consent.consent_capabilities(tree, newer, ask=None)
    assert waiting.waiting == ["llm.model"] and not waiting.asked
    assert has(scope, tree, newer, "tools.override") and not has(scope, tree, newer, "llm.model")
    shown: list[str] = []
    consent.consent_capabilities(tree, newer, ask=lambda t: shown.append(t) is None)
    assert "llm.model" in shown[0] and "tools.override" not in shown[0]
    assert has(scope, tree, newer, "llm.model")


def test_unknown_capabilities_dropped(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        found = consent.declared_capabilities(manifest("nope", "tools.override", "tools.override"))
    assert found == ["tools.override"]
    assert "unknown capability 'nope'" in caplog.text


def test_non_tty_grants_nothing() -> None:
    tree: dict[str, Any] = {}
    result = consent.consent_capabilities(tree, manifest("platform.actions"), ask=None)
    assert result.waiting == ["platform.actions"] and result.granted == []
    assert tree == {}  # nothing recorded: the question comes again on a terminal


def test_revoke_and_set_grants_keep_times() -> None:
    tree: dict[str, Any] = {}
    consent.set_grants(tree, "hello", ["llm.model"], ["llm.model", "llm.task"], now=NOW)
    later = "2026-10-20T00:00:00+00:00"
    consent.set_grants(tree, "hello", ["llm.task"], ["llm.model", "llm.task"], now=later)
    entry = tree["plugins"]["entries"]["hello"]
    assert entry[consent.GRANTED_KEY] == [
        {"name": "llm.model", "granted_at": NOW},
        {"name": "llm.task", "granted_at": later},
    ]
    assert consent.revoke_capabilities(tree, "hello", ["llm.model"]) == ["llm.model"]
    assert consent.granted_capabilities(tree, "hello") == ["llm.task"]
    # The old plain-string form still reads.
    old = {"plugins": {"entries": {"hello": {"granted_capabilities": ["llm.agent", "bogus"]}}}}
    assert consent.granted_capabilities(old, "hello") == ["llm.agent"]


def test_pinned_requirements() -> None:
    ok = manifest(python_dependencies=("requests==2.32.3", "rich[jupyter]==13.9.4"))
    assert consent.pinned_requirements(ok) == ["requests==2.32.3", "rich[jupyter]==13.9.4"]
    loose = manifest(python_dependencies=("requests>=2", "rich"))
    with pytest.raises(consent.ConsentError, match="not pinned exactly: requests>=2, rich"):
        consent.pinned_requirements(loose)


def test_install_dependencies(scope: Scope, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(consent, "which", lambda name, path=None: None)
    ok, why = consent.install_dependencies(scope, ["requests==2.32.3"])
    assert not ok and "uv is not on PATH" in why
    monkeypatch.setattr(consent, "which", lambda name, path=None: "/opt/uv")
    seen: list[Sequence[str]] = []

    def runner(
        argv: Sequence[str], cwd: Path, env: Mapping[str, str], timeout: float
    ) -> tuple[int, str]:
        seen.append(argv)
        return 0, "installed"

    assert consent.install_dependencies(scope, ["requests==2.32.3"], runner=runner) == (
        True,
        "installed",
    )
    assert seen[0][:4] == ["/opt/uv", "pip", "install", "--python"]
    assert seen[0][-1] == "requests==2.32.3"
    assert consent.install_dependencies(scope, []) == (True, "")


def test_missing_env(scope: Scope) -> None:
    plugin = manifest(
        requires_env=("PLAIN_SET", EnvRequirement(name="SERVICE_TOKEN", secret=True), "ABSENT")
    )
    scoped = scope.with_env(scope.env.with_overlay({"PLAIN_SET": "x"}))
    assert [m.name for m in consent.missing_env(scoped, plugin)] == ["SERVICE_TOKEN", "ABSENT"]
    assert scoped.secrets is not None
    scoped.secrets.set("SERVICE_TOKEN", "value-" + "for-test")  # type: ignore[attr-defined]
    assert [m.name for m in consent.missing_env(scoped, plugin)] == ["ABSENT"]


def test_memory_provider_kind() -> None:
    assert consent.is_memory_provider(manifest(kind="exclusive"))
    assert consent.is_memory_provider(manifest(category="memory"))
    assert not consent.is_memory_provider(manifest())
