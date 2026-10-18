"""The config loader (P01-10): merge, expansion, cache, last-known-good (R04.6)."""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

import pytest

from indus.foundation.config import backups, loader
from indus.foundation.config.loader import ConfigView, FailedRead
from indus.foundation.scope import EnvView, Scope, make_scope
from tests.fakes.clock import FakeClock

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "config"


def _write(scope: Scope, text: str) -> Path:
    path = scope.home / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _touch_forward(path: Path, ns: int = 1_000_000_000) -> None:
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + ns))


def test_missing_file_gives_defaults(cfg_scope: Scope) -> None:
    view = loader.load(cfg_scope)
    assert isinstance(view, ConfigView) and not view.failed
    assert view.get("approvals.mode") == "manual"
    assert not view.is_set("approvals.mode")
    assert view.source("approvals.mode") == "default"


def test_user_merges_with_default_siblings(cfg_scope: Scope) -> None:
    _write(cfg_scope, "approvals:\n  deny: [rm]\n")
    view = loader.load(cfg_scope)
    assert view.get("approvals.deny") == ["rm"]
    assert view.get("approvals.mode") == "manual"
    assert view.get("approvals.timeout") == 300
    assert view.is_set("approvals.deny")
    assert not view.is_set("approvals.mode")
    assert view.source("approvals.deny") == "user"
    assert view.source("approvals.mode") == "default"
    assert view.source("nope.nothing") is None


def test_null_section_keeps_defaults(cfg_scope: Scope) -> None:
    _write(cfg_scope, "terminal:\nlogging:\n  level: DEBUG\n")
    view = loader.load(cfg_scope)
    assert view.get("terminal.backend") == "local"
    assert view.get("logging.level") == "DEBUG"
    assert view.get("logging.max_size_mb") == 5


def test_model_shorthand_and_api_base_canonicalised(cfg_scope: Scope) -> None:
    _write(cfg_scope, "model: test-model\n")
    view = loader.load(cfg_scope)
    assert view.get("model.default") == "test-model"
    assert view.get("model.provider") == ""
    assert view.is_set("model.default")
    _write(cfg_scope, "model:\n  default: m\n  api_base: http://h/v1\n")
    loader.invalidate()
    view = loader.load(cfg_scope)
    assert view.get("model.base_url") == "http://h/v1"
    assert view.get("model.api_base") is None


def test_env_expansion(cfg_scope: Scope) -> None:
    shutil.copy(FIXTURES / "templated.yaml", cfg_scope.home / "config.yaml")
    scope = cfg_scope.with_env(
        EnvView({"OPENAI_BASE_URL": "https://api.example.test/v1", "TEST_PORT": "8080"})
    )
    view = loader.load(scope)
    assert view.get("model.base_url") == "https://api.example.test/v1"
    assert view.get("model.api_mode") == "${env:TEST_API_MODE}"  # unset → literal
    assert view.get("providers.local.base_url") == "http://localhost:8080/v1"
    assert view.get("providers.local.key_ref") == "${file:/run/secret}"  # not an env source
    assert view.source("model.base_url") == "env-expanded"
    assert view.source("model.default") == "user"
    assert view.source("providers") == "env-expanded"
    assert view.user_raw()["model"]["base_url"] == "${OPENAI_BASE_URL}"


def test_keys_are_never_expanded(cfg_scope: Scope) -> None:
    _write(cfg_scope, "providers:\n  ${NAME}:\n    base_url: x\n")
    view = loader.load(cfg_scope.with_env(EnvView({"NAME": "boom"})))
    assert "${NAME}" in view.get("providers")


def test_view_is_read_only(cfg_scope: Scope) -> None:
    _write(cfg_scope, "approvals:\n  deny: [rm]\n")
    view = loader.load(cfg_scope)
    view.get("approvals.deny").append("mkfs")
    view.as_dict()["approvals"]["deny"].append("dd")
    again = loader.load(cfg_scope)
    assert again is view
    assert again.get("approvals.deny") == ["rm"]


def test_cache_invalidated_on_file_change(cfg_scope: Scope) -> None:
    path = _write(cfg_scope, "logging:\n  level: DEBUG\n")
    first = loader.load(cfg_scope)
    assert loader.load(cfg_scope) is first
    path.write_text("logging:\n  level: ERROR\n", encoding="utf-8")
    _touch_forward(path)
    second = loader.load(cfg_scope)
    assert second is not first
    assert second.get("logging.level") == "ERROR"


def test_cache_invalidated_on_env_change(cfg_scope: Scope) -> None:
    _write(cfg_scope, "model:\n  base_url: ${OPENAI_BASE_URL}\n")
    a = cfg_scope.with_env(EnvView({"OPENAI_BASE_URL": "https://one.test"}))
    b = cfg_scope.with_env(EnvView({"OPENAI_BASE_URL": "https://two.test"}))
    first = loader.load(a)
    assert loader.load(a) is first
    second = loader.load(b)
    assert second.get("model.base_url") == "https://two.test"
    assert loader.load(cfg_scope.with_env(EnvView({}))).get("model.base_url") == (
        "${OPENAI_BASE_URL}"
    )


def test_bom_file_loads(cfg_scope: Scope) -> None:
    (cfg_scope.home / "config.yaml").write_bytes(b"\xef\xbb\xbflogging:\n  level: DEBUG\n")
    assert loader.load(cfg_scope).get("logging.level") == "DEBUG"


def test_good_load_records_good_backup(cfg_scope: Scope) -> None:
    path = _write(cfg_scope, "approvals:\n  deny: [rm]\n")
    loader.load(cfg_scope)
    good = backups.list_backups(path, "good")
    assert len(good) == 1
    assert good[0].read_bytes() == path.read_bytes()


def test_broken_after_good_serves_last_known_good(
    cfg_scope: Scope, caplog: pytest.LogCaptureFixture
) -> None:
    path = _write(cfg_scope, "approvals:\n  deny: [rm]\nlogging:\n  level: ERROR\n")
    assert loader.load(cfg_scope).get("approvals.deny") == ["rm"]
    shutil.copy(FIXTURES / "broken.yaml", path)
    _touch_forward(path)
    caplog.set_level(logging.ERROR, logger="indus.foundation.config.loader")
    view = loader.load(cfg_scope)
    assert not view.failed
    assert view.error is not None
    assert view.get("approvals.deny") == ["rm"]
    assert view.get("logging.level") == "ERROR"
    errors = [r.getMessage() for r in caplog.records]
    assert len(errors) == 1 and "line" in errors[0]
    # Served again from the cache: still one error line, and a corrupt snapshot was taken.
    loader.load(cfg_scope)
    assert len(caplog.records) == 1
    assert len(backups.list_backups(path, "corrupt")) == 1


def test_fresh_process_uses_newest_good_backup(cfg_scope: Scope) -> None:
    path = _write(cfg_scope, "approvals:\n  deny: [rm]\n")
    loader.load(cfg_scope)
    shutil.copy(FIXTURES / "broken.yaml", path)
    loader.reset()  # a new process: nothing in memory
    view = loader.load(cfg_scope)
    assert not view.failed
    assert view.get("approvals.deny") == ["rm"]


def test_broken_first_load_is_failed_read(cfg_scope: Scope) -> None:
    shutil.copy(FIXTURES / "broken.yaml", cfg_scope.home / "config.yaml")
    view = loader.load(cfg_scope)
    assert isinstance(view, FailedRead) and view.failed
    assert view.get("approvals.mode") == "manual"
    assert view.get("approvals.deny") == []
    assert isinstance(view.as_dict(), loader.FailedReadDict)


def test_non_mapping_root_is_treated_as_broken(cfg_scope: Scope) -> None:
    _write(cfg_scope, "- just\n- a list\n")
    assert loader.load(cfg_scope).failed


def test_unreadable_file_is_treated_as_broken(
    cfg_scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(cfg_scope, "logging:\n  level: DEBUG\n")
    real_read = Path.read_bytes

    def denied(self: Path) -> bytes:
        if self == path:
            raise PermissionError(13, "Permission denied", str(self))
        return real_read(self)

    monkeypatch.setattr(Path, "read_bytes", denied)
    view = loader.load(cfg_scope)
    assert view.failed and isinstance(view.error, PermissionError)


def test_fixed_file_is_read_again(cfg_scope: Scope) -> None:
    path = cfg_scope.home / "config.yaml"
    shutil.copy(FIXTURES / "broken.yaml", path)
    assert loader.load(cfg_scope).failed
    path.write_text("logging:\n  level: DEBUG\n", encoding="utf-8")
    _touch_forward(path)
    view = loader.load(cfg_scope)
    assert not view.failed and view.get("logging.level") == "DEBUG"


def test_profiles_do_not_share_cache(tmp_path: Path, clock: FakeClock) -> None:
    homes = [tmp_path / "profiles" / name for name in ("a", "b")]
    for i, home in enumerate(homes):
        home.mkdir(parents=True)
        (home / "config.yaml").write_text(f"agent:\n  max_turns: {10 + i}\n", encoding="utf-8")
    views = [loader.load(make_scope(h, clock=clock)) for h in homes]
    assert [v.get("agent.max_turns") for v in views] == [10, 11]


def test_dotted_model_ids(cfg_scope: Scope) -> None:
    shutil.copy(FIXTURES / "dotted_models.yaml", cfg_scope.home / "config.yaml")
    view = loader.load(cfg_scope)
    assert view.get("providers.p.models.grok-4.6.context_length") == 131072
    assert view.is_set("providers.p.models.glm-5.3.supports_vision")
    assert "providers.p.models.grok-4.6" in view
