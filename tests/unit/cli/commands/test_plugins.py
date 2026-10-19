"""``indus plugins`` verbs (P13-23) against local fixture repos."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from indus.foundation.config import loader
from indus.foundation.scope import Scope
from indus.plugins.install import Installer
from tests.unit.cli.commands._helpers_d import run
from tests.unit.plugins._git_repos import (
    CAUTION_INIT,
    DANGEROUS_INIT,
    PluginRepo,
    git_scope,
    plugin_files,
)


@pytest.fixture
def gscope(scope: Scope, tmp_path: Path) -> Scope:
    return git_scope(scope, tmp_path)


@pytest.fixture
def repo(tmp_path: Path) -> PluginRepo:
    return PluginRepo(tmp_path / "src")


def setting(scope: Scope, key: str) -> object:
    return loader.load(scope).get(key)


def test_install_list_info_remove(gscope: Scope, repo: PluginRepo) -> None:
    revision = repo.commit(plugin_files(description="Says hello"))
    installed = run(gscope, "plugins", "install", repo.url)
    assert installed.code == 0, installed.err
    assert "custom, unreviewed source" in installed.out
    assert f"Installed hello at {revision[:12]}." in installed.out
    assert "Run `indus plugins enable hello`" in installed.out
    listed = run(gscope, "plugins", "list")
    assert listed.code == 0
    row = next(line for line in listed.out.splitlines() if line.startswith("hello"))
    assert "not-enabled" in row and f"git@{revision[:8]}" in row
    as_json = json.loads(run(gscope, "plugins", "list", "--json").out)
    assert as_json[0]["name"] == "hello" and as_json[0]["state"] == "not-enabled"
    info = run(gscope, "plugins", "info", "hello")
    assert "Description: Says hello" in info.out and f"at {revision}" in info.out
    assert "Scan: safe (indus-scan-v1)" in info.out
    refused = run(gscope, "plugins", "remove", "hello")
    assert refused.code == 1 and "plugins remove hello -y" in refused.err
    run(gscope, "plugins", "enable", "hello")
    removed = run(gscope, "plugins", "remove", "hello", "-y")
    assert removed.code == 0 and "cleared plugins.enabled" in removed.out
    assert not (gscope.home / "plugins" / "hello").exists()
    assert setting(gscope, "plugins.enabled") == []
    assert run(gscope, "plugins", "remove", "hello", "-y").code == 1


def test_pin_shows_in_list(gscope: Scope, repo: PluginRepo) -> None:
    first = repo.commit(plugin_files())
    repo.commit({"x.txt": "x\n"})
    result = run(gscope, "plugins", "install", repo.url, "--ref", first)
    assert result.code == 0 and "(pinned)" in result.out
    assert f"git pinned@{first[:8]}" in run(gscope, "plugins", "list").out
    short = run(gscope, "plugins", "install", repo.url, "--ref", first[:7], "--force")
    assert short.code == 1 and "full 40-character commit SHA" in short.err
    update = run(gscope, "plugins", "update", "hello")
    assert update.code == 1 and "pinned" in update.err


def test_caution_needs_force_without_a_terminal(gscope: Scope, repo: PluginRepo) -> None:
    repo.commit(plugin_files(init=CAUTION_INIT))
    stopped = run(gscope, "plugins", "install", repo.url)
    assert stopped.code == 1 and "caution" in stopped.err and "pipe_to_shell" in stopped.err
    assert run(gscope, "plugins", "install", repo.url, "--force").code == 0


def test_caution_confirmed_on_a_terminal(gscope: Scope, repo: PluginRepo) -> None:
    repo.commit(plugin_files(init=CAUTION_INIT))
    result = run(gscope, "plugins", "install", repo.url, tty=True, input="y\nn\n")
    assert result.code == 0, result.err
    assert "pipe_to_shell" in result.out and "Not enabled" in result.out


def test_dangerous_blocked_even_with_force(gscope: Scope, repo: PluginRepo) -> None:
    repo.commit(plugin_files(init=DANGEROUS_INIT))
    result = run(gscope, "plugins", "install", repo.url, "--force")
    assert result.code == 1 and "Blocked" in result.err and "dangerous" in result.err
    assert not (gscope.home / "plugins" / "hello").exists()


def test_enable_capabilities_and_secret_free_info(gscope: Scope, repo: PluginRepo) -> None:
    repo.commit(
        plugin_files(
            capabilities='["llm.model"]',
            requires_env='[{"name": "HELLO_SERVICE_TOKEN", "secret": true}]',
        )
    )
    installed = run(gscope, "plugins", "install", repo.url, "--enable")
    assert installed.code == 0, installed.err
    assert "Needs settings you have not set yet: HELLO_SERVICE_TOKEN" in installed.out
    assert "Enabled hello" in installed.out
    assert "Capabilities not granted (no terminal to ask): llm.model" in installed.out
    assert setting(gscope, "plugins.enabled") == ["hello"]
    shown = run(gscope, "plugins", "capabilities", "hello")
    assert "llm.model" in shown.out and "not granted" in shown.out
    assert run(gscope, "plugins", "capabilities", "hello", "--grant", "tools.override").code == 1
    granted = run(gscope, "plugins", "capabilities", "hello", "--grant", "llm.model")
    assert granted.code == 0 and "llm.model          granted" in granted.out
    secret = "value-" + "that-must-not-print"
    assert gscope.secrets is not None
    gscope.secrets.set("HELLO_SERVICE_TOKEN", secret)  # type: ignore[attr-defined]
    info = run(gscope, "plugins", "info", "hello")
    assert "Needs HELLO_SERVICE_TOKEN: set" in info.out and secret not in info.out
    assert "llm.model (granted)" in info.out and "not a sandbox" in info.out
    disabled = run(gscope, "plugins", "disable", "hello")
    assert disabled.code == 0 and setting(gscope, "plugins.disabled") == ["hello"]
    assert "disabled" in run(gscope, "plugins", "list").out


def test_terminal_install_asks_enable_and_capabilities(gscope: Scope, repo: PluginRepo) -> None:
    repo.commit(plugin_files(capabilities='["platform.actions"]'))
    result = run(gscope, "plugins", "install", repo.url, tty=True, input="y\ny\n")
    assert result.code == 0, result.err
    assert "Enabled hello" in result.out and "platform.actions" in result.out
    entry = setting(gscope, "plugins.entries")
    assert isinstance(entry, dict)
    assert [g["name"] for g in entry["hello"]["granted_capabilities"]] == ["platform.actions"]


def test_dependencies_without_a_terminal_leave_it_disabled(gscope: Scope, repo: PluginRepo) -> None:
    repo.commit(plugin_files(python_dependencies='["requests==2.32.3"]'))
    result = run(gscope, "plugins", "install", repo.url, "--enable")
    assert result.code == 0, result.err
    assert "the plugin stays off" in result.out and "Not enabled" in result.out
    assert setting(gscope, "plugins.enabled") == []
    assert (gscope.home / "plugins" / "hello").is_dir()
    assert run(gscope, "plugins", "enable", "hello").code == 1


def test_memory_provider_install(gscope: Scope, repo: PluginRepo) -> None:
    repo.commit(plugin_files(kind="exclusive"))
    result = run(gscope, "plugins", "install", repo.url, "--enable")
    assert result.code == 0 and "memory.provider" in result.out
    assert setting(gscope, "memory.provider") == "hello"
    assert setting(gscope, "plugins.enabled") == []


def test_update_and_check_updates(gscope: Scope, repo: PluginRepo) -> None:
    repo.commit(plugin_files())
    run(gscope, "plugins", "install", repo.url)
    assert run(gscope, "plugins", "update").code == 2
    assert "hello: up to date" in run(gscope, "plugins", "check-updates").out
    new = repo.commit({"x.txt": "x\n"})
    checked = run(gscope, "plugins", "check-updates")
    assert f"-> {new[:12]}" in checked.out
    assert "update available" in run(gscope, "plugins", "list").out
    updated = run(gscope, "plugins", "update", "--all")
    assert updated.code == 0 and f"-> {new[:12]}" in updated.out
    assert Installer(gscope).record("hello")["revision"] == new  # type: ignore[index]


def test_dangerous_update_disables(gscope: Scope, repo: PluginRepo) -> None:
    repo.commit(plugin_files())
    run(gscope, "plugins", "install", repo.url, "--enable")
    repo.commit({"__init__.py": DANGEROUS_INIT})
    result = run(gscope, "plugins", "update", "hello", "--force")
    assert result.code == 1 and "disabled" in result.err
    assert setting(gscope, "plugins.disabled") == ["hello"]


def test_validate(scope: Scope, tmp_path: Path) -> None:
    good = tmp_path / "good"
    good.mkdir()
    for rel, text in plugin_files().items():
        (good / rel).write_text(text, encoding="utf-8")
    ok = run(scope, "plugins", "validate", str(good))
    assert ok.code == 0 and "Manifest ok: hello 1.0.0" in ok.out and ": safe" in ok.out
    (good / "__init__.py").write_text(DANGEROUS_INIT, encoding="utf-8")
    bad = run(scope, "plugins", "validate", str(good))
    assert bad.code == 1 and "dangerous" in bad.out
    empty = tmp_path / "empty"
    empty.mkdir()
    assert run(scope, "plugins", "validate", str(empty)).code == 1


def test_unknown_plugin(scope: Scope) -> None:
    for verb in ("info", "enable", "disable", "capabilities"):
        result = run(scope, "plugins", verb, "ghost")
        assert result.code == 1 and "indus plugins list" in result.err
