"""Config backups and the migration runner (P01-12)."""

from __future__ import annotations

import logging
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from indus.foundation import yamlio
from indus.foundation.config import backups, migrations
from indus.foundation.config.errors import ConfigUnreadable
from indus.foundation.scope import Scope
from tests.fakes.clock import FakeClock


def _config(scope: Scope, text: str) -> Path:
    path = scope.home / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


# -- backups ------------------------------------------------------------------------------------


def test_backup_name_dir_and_mode(cfg_scope: Scope, clock: FakeClock) -> None:
    path = _config(cfg_scope, "a: 1\n")
    made = backups.backup_config(path, "good", clock=clock)
    assert made is not None
    assert made == cfg_scope.home / "backups" / "config" / "config.yaml.good.20260301-120000"
    assert made.read_bytes() == b"a: 1\n"
    assert stat.S_IMODE(made.stat().st_mode) == 0o600
    assert stat.S_IMODE(made.parent.stat().st_mode) == 0o700


def test_keep_newest_five(cfg_scope: Scope, clock: FakeClock) -> None:
    path = cfg_scope.home / "config.yaml"
    made: list[Path] = []
    for i in range(6):
        path.write_text(f"a: {i}\n", encoding="utf-8")
        result = backups.backup_config(path, "good", clock=clock)
        assert result is not None
        made.append(result)
        clock.advance(61)
    kept = backups.list_backups(path, "good")
    assert kept == list(reversed(made[1:]))
    assert not made[0].exists()
    assert kept[0].read_text() == "a: 5\n"


def test_reasons_are_counted_separately(cfg_scope: Scope, clock: FakeClock) -> None:
    path = _config(cfg_scope, "a: 1\n")
    for reason in ("good", "pre-write", "corrupt", "pre-migrate"):
        assert backups.backup_config(path, reason, clock=clock) is not None
    assert len(backups.list_backups(path)) == 4
    assert len(backups.list_backups(path, "pre-write")) == 1
    with pytest.raises(ValueError):
        backups.backup_config(path, "whatever", clock=clock)


def test_identical_bytes_skipped(cfg_scope: Scope, clock: FakeClock) -> None:
    path = _config(cfg_scope, "a: 1\n")
    assert backups.backup_config(path, "good", clock=clock) is not None
    clock.advance(5)
    assert backups.backup_config(path, "good", clock=clock) is None
    assert len(backups.list_backups(path, "good")) == 1


def test_missing_or_empty_source_skipped(cfg_scope: Scope, clock: FakeClock) -> None:
    path = cfg_scope.home / "config.yaml"
    assert backups.backup_config(path, "good", clock=clock) is None
    path.write_text("", encoding="utf-8")
    assert backups.backup_config(path, "good", clock=clock) is None
    assert not (cfg_scope.home / "backups").exists()


def test_symlink_at_target_refused(
    cfg_scope: Scope, clock: FakeClock, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = _config(cfg_scope, "secret: 1\n")
    root = backups.backups_dir(path)
    root.mkdir(parents=True)
    victim = tmp_path / "victim.txt"
    victim.write_text("do not touch", encoding="utf-8")
    planted = root / "config.yaml.good.20260301-120000"
    planted.symlink_to(victim)
    caplog.set_level(logging.WARNING, logger="indus.foundation.config.backups")
    assert backups.backup_config(path, "good", clock=clock) is None
    assert victim.read_text(encoding="utf-8") == "do not touch"
    assert planted.is_symlink()
    assert path.read_text(encoding="utf-8") == "secret: 1\n"
    assert any("symlink" in r.getMessage() for r in caplog.records)


def test_backup_failure_is_logged_not_raised(
    cfg_scope: Scope, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    path = _config(cfg_scope, "a: 1\n")
    (cfg_scope.home / "backups").write_text("not a dir", encoding="utf-8")
    caplog.set_level(logging.WARNING, logger="indus.foundation.config.backups")
    assert backups.backup_config(path, "good", clock=clock) is None
    assert any("could not back up" in r.getMessage() for r in caplog.records)


def test_newest_good_bytes(cfg_scope: Scope, clock: FakeClock) -> None:
    path = _config(cfg_scope, "a: 1\n")
    assert backups.newest_good_bytes(path) is None
    backups.backup_config(path, "good", clock=clock)
    clock.advance(1)
    path.write_text("a: 2\n", encoding="utf-8")
    backups.backup_config(path, "good", clock=clock)
    assert backups.newest_good_bytes(path) == b"a: 2\n"


# -- migrations ---------------------------------------------------------------------------------


@pytest.fixture
def step_registry() -> Iterator[dict[int, migrations.Step]]:
    saved = dict(migrations.STEPS)
    yield migrations.STEPS
    migrations.STEPS.clear()
    migrations.STEPS.update(saved)


def test_registry_starts_empty_and_version_is_one() -> None:
    assert migrations.STEPS == {}
    assert migrations.CURRENT_VERSION == 1


@pytest.mark.parametrize(
    ("raw", "version"),
    [
        ({}, 0),
        ({"_config_version": 1}, 1),
        ({"_config_version": True}, 0),
        ({"_config_version": "x"}, 0),
        ({"_config_version": -3}, 0),
        ({"_config_version": "2"}, 2),
    ],
)
def test_read_version(raw: dict[str, Any], version: int) -> None:
    assert migrations.read_version(raw) == version


def test_migrate_unstamped_stamps_and_lists_defaults(cfg_scope: Scope) -> None:
    path = _config(cfg_scope, "# mine\napprovals:\n  deny: [rm]   # keep\n")
    report = migrations.migrate(cfg_scope)
    assert (report.from_version, report.to_version, report.wrote) == (0, 1, True)
    assert report.message == "Config version 0 → 1"
    assert "approvals.mode" in report.new_defaults
    assert "agent.max_turns" in report.new_defaults
    assert "approvals.deny" not in report.new_defaults
    text = path.read_text(encoding="utf-8")
    assert yamlio.load_safe(text) == {"approvals": {"deny": ["rm"]}, "_config_version": 1}
    assert "# mine" in text and "# keep" in text
    assert len(backups.list_backups(path, "pre-migrate")) == 1


def test_migrate_dry_run_writes_nothing(cfg_scope: Scope) -> None:
    path = _config(cfg_scope, "approvals:\n  deny: [rm]\n")
    before = path.read_bytes()
    report = migrations.migrate(cfg_scope, dry_run=True)
    assert report.to_version == 1 and not report.wrote
    assert path.read_bytes() == before
    assert backups.list_backups(path) == []


def test_migrate_malformed_no_side_effects(cfg_scope: Scope) -> None:
    path = _config(cfg_scope, "approvals: [rm\n")
    with pytest.raises(ConfigUnreadable):
        migrations.migrate(cfg_scope)
    assert path.read_text(encoding="utf-8") == "approvals: [rm\n"
    assert not (cfg_scope.home / "backups").exists()


def test_migrate_current_or_missing_is_noop(cfg_scope: Scope) -> None:
    report = migrations.migrate(cfg_scope)
    assert not report.wrote and not (cfg_scope.home / "config.yaml").exists()
    path = _config(cfg_scope, "_config_version: 1\n")
    report = migrations.migrate(cfg_scope)
    assert (report.from_version, report.wrote) == (1, False)
    assert path.read_text(encoding="utf-8") == "_config_version: 1\n"


def test_migrate_newer_file_left_alone(cfg_scope: Scope) -> None:
    path = _config(cfg_scope, "_config_version: 7\n")
    report = migrations.migrate(cfg_scope)
    assert (report.from_version, report.to_version, report.wrote) == (7, 7, False)
    assert "newer" in report.message
    assert path.read_text(encoding="utf-8") == "_config_version: 7\n"


def test_registered_steps_run_in_order(
    cfg_scope: Scope, step_registry: dict[int, migrations.Step]
) -> None:
    calls: list[int] = []

    def step0(raw: dict[str, Any]) -> list[str]:
        calls.append(0)
        raw.setdefault("logging", {})["level"] = "DEBUG"
        return ["logging.level set to DEBUG"]

    step_registry[0] = step0
    path = _config(cfg_scope, "timezone: UTC\n")
    report = migrations.migrate(cfg_scope)
    assert calls == [0]
    assert report.steps == ["logging.level set to DEBUG"]
    assert yamlio.load_safe(path)["logging"] == {"level": "DEBUG"}
