"""P07-13: `indus config check` and `indus config migrate`."""

from __future__ import annotations

from typing import Any

import pytest

from indus.cli.commands import config_check
from indus.foundation import yamlio
from indus.foundation.config import backups, loader, migrations
from indus.foundation.scope import Scope
from tests.unit.cli.commands.cmdrun import run_cli, write_settings


def test_missing_file_is_fine(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    ran = run_cli(capsys, scope, "config", "check")
    assert ran.code == 0
    assert "built-in values apply" in ran.out


def test_clean_file_passes(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    write_settings(scope, "_config_version: 1\nagent:\n  max_turns: 9\n")
    ran = run_cli(capsys, scope, "config", "check")
    assert ran.code == 0
    assert "Config version 1 (current)." in ran.out
    assert "No problems found." in ran.out


def test_seeded_problems_each_reported(
    capsys: pytest.CaptureFixture[str], scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(config_check.DEPRECATED_ENV, "OLD_SWITCH", "use display.interface")
    (scope.home / ".env").write_text("OLD_SWITCH=1\n", encoding="utf-8")
    write_settings(
        scope,
        "_config_version: 1\n"
        "agent:\n  max_turns: lots\n  max_turn: 3\n"
        "approvals:\n  mode: sometimes\n  deny: '[rm -rf /, mkfs]'\n"
        "displya:\n  language: en\n"
        "fallback_providers:\n  - {provider: openrouter, model: m1}\n  - {provider: openai}\n"
        "  - just-a-name\n"
        "providers:\n  good: {base_url: 'http://127.0.0.1:1/v1'}\n  bad: http://x/v1\n",
    )
    ran = run_cli(capsys, scope, "config", "check")
    assert ran.code == 1
    out = ran.out
    assert "error: agent.max_turns" in out
    assert "error: approvals.mode" in out
    assert (
        "warning: agent.max_turn is not a setting this version reads. Did you mean agent.max_turns?"
        in out
    )
    assert "warning: displya is not a setting this version reads. Did you mean display?" in out
    assert "fallback_providers[1] needs both" in out
    assert "fallback_providers[2] needs both" in out
    assert "fallback_providers[0]" not in out
    assert "error: providers.bad must be a mapping" in out
    assert "providers.good" not in out
    assert "approvals.deny holds text that looks like a list" in out
    assert "OLD_SWITCH no longer does anything" in out


def test_old_stamp_reported(
    capsys: pytest.CaptureFixture[str], scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(migrations, "CURRENT_VERSION", 2)
    write_settings(scope, "_config_version: 1\n")
    ran = run_cli(capsys, scope, "config", "check")
    assert ran.code == 0
    assert "Config version 1 → 2 is available: run `indus config migrate`." in ran.out


def test_malformed_file_is_an_error(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    write_settings(scope, "agent: [broken\n")
    ran = run_cli(capsys, scope, "config", "check")
    assert ran.code == 1
    assert "formatting error" in ran.out
    loader.reset()


# -- migrate --------------------------------------------------------------------------------------


def _step(raw: dict[str, Any]) -> list[str]:
    agent = raw.setdefault("agent", {})
    agent["max_turns"] = agent.pop("turn_cap", 150) * 2
    return ["agent.turn_cap became agent.max_turns (doubled)"]


@pytest.fixture
def test_step(monkeypatch: pytest.MonkeyPatch) -> None:
    """A test-only step 1 → 2; the real registry is empty at stamp 1."""
    monkeypatch.setattr(migrations, "CURRENT_VERSION", 2)
    monkeypatch.setitem(migrations.STEPS, 1, _step)


@pytest.mark.usefixtures("test_step")
def test_test_only_step_migrates_and_stamps(
    capsys: pytest.CaptureFixture[str], scope: Scope
) -> None:
    path = write_settings(scope, "_config_version: 1\nagent:\n  turn_cap: 21\n")
    before = path.read_bytes()
    ran = run_cli(capsys, scope, "config", "migrate")
    assert ran.code == 0, ran.err
    assert "Config version 1 → 2" in ran.out
    assert "agent.turn_cap became agent.max_turns" in ran.out
    assert "Dry run: nothing was written." in ran.out
    assert path.read_bytes() == before
    assert backups.list_backups(path, "pre-migrate") == []

    ran = run_cli(capsys, scope, "config", "migrate", "--apply")
    assert ran.code == 0, ran.err
    assert yamlio.load_safe(path.read_bytes()) == {"_config_version": 2, "agent": {"max_turns": 42}}
    saved = backups.list_backups(path, "pre-migrate")
    assert len(saved) == 1
    assert saved[0].read_bytes() == before


def test_migrate_malformed_refused_without_backup(
    capsys: pytest.CaptureFixture[str], scope: Scope
) -> None:
    path = write_settings(scope, "_config_version: 0\nagent: [broken\n")
    before = path.read_bytes()
    ran = run_cli(capsys, scope, "config", "migrate", "--apply")
    assert ran.code == 1
    assert "formatting error" in ran.err
    assert path.read_bytes() == before
    assert not (scope.home / "backups" / "config").exists() or not any(
        p.name.startswith("config.yaml.pre-migrate")
        for p in (scope.home / "backups" / "config").iterdir()
    )
    loader.reset()


def test_migrate_reports_new_defaults_not_written(
    capsys: pytest.CaptureFixture[str], scope: Scope
) -> None:
    path = write_settings(scope, "agent:\n  max_turns: 9\n")
    ran = run_cli(capsys, scope, "config", "migrate", "--apply")
    assert ran.code == 0, ran.err
    assert "Config version 0 → 1" in ran.out
    assert "built-in values apply and nothing is written for them" in ran.out
    assert yamlio.load_safe(path.read_bytes()) == {"_config_version": 1, "agent": {"max_turns": 9}}


def test_migrate_current_and_newer(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    path = write_settings(scope, "_config_version: 1\n")
    assert "is current" in run_cli(capsys, scope, "config", "migrate").out
    write_settings(scope, "_config_version: 7\n")
    before = path.read_bytes()
    ran = run_cli(capsys, scope, "config", "migrate", "--apply")
    assert "newer than this build" in ran.out
    assert path.read_bytes() == before


def test_migrate_apply_refused_on_managed_install(
    capsys: pytest.CaptureFixture[str], scope: Scope
) -> None:
    write_settings(scope, "agent:\n  max_turns: 9\n")
    (scope.home / ".managed").touch()
    ran = run_cli(capsys, scope, "config", "migrate", "--apply")
    assert ran.code == 1
