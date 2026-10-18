"""The config writer seam (P01-11): R04.3 fail-closed, R04.4 no defaults, R04.5 round trip."""

from __future__ import annotations

import ast
import os
import shutil
import stat
from pathlib import Path

import pytest

from indus.foundation import yamlio
from indus.foundation.config import backups, loader, schema, writer
from indus.foundation.config.errors import ConfigFailedRead, ConfigManaged, ConfigUnreadable
from indus.foundation.scope import EnvView, Scope

REPO = Path(__file__).resolve().parents[4]
SRC = REPO / "src"
FIXTURES = REPO / "tests" / "fixtures" / "config"
WRITER = Path("indus") / "foundation" / "config" / "writer.py"
DUMPERS = {"dump_roundtrip", "dump_plain"}
CONFIG_HINTS = ("config.yaml", "config_path", "CONFIG_FILE")


def _config(scope: Scope) -> Path:
    return scope.home / "config.yaml"


def _install(scope: Scope, fixture: str) -> Path:
    path = _config(scope)
    shutil.copy(FIXTURES / fixture, path)
    return path


# -- ratchet ------------------------------------------------------------------------------------


def find_config_writers(src: Path) -> list[str]:
    """Calls of a yamlio dumper inside code that names config.yaml, outside the writer seam."""
    offenders: list[str] = []
    for file in sorted(src.rglob("*.py")):
        rel = file.relative_to(src)
        if rel == WRITER:
            continue
        text = file.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=str(file))
        defs = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        scopes: list[ast.AST] = [n for n in tree.body if not isinstance(n, defs)]
        scopes += [
            n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
        ]
        for scope_node in scopes:
            body = ast.get_source_segment(text, scope_node)
            if not body or not any(hint in body for hint in CONFIG_HINTS):
                continue
            for node in ast.walk(scope_node):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name in DUMPERS:
                    offenders.append(f"{rel}:{node.lineno}: {name}")
    return sorted(set(offenders))


def test_config_writer_ratchet_on_src() -> None:
    assert find_config_writers(SRC) == []


def test_config_writer_ratchet_catches_planted_writer(tmp_path: Path) -> None:
    copy = tmp_path / "src"
    shutil.copytree(SRC, copy, ignore=shutil.ignore_patterns("__pycache__"))
    (copy / "indus" / "core" / "rogue.py").write_text(
        "from indus.foundation import fsio, yamlio\n"
        "\n"
        "def save(home, data):\n"
        "    fsio.atomic_write_text(home / 'config.yaml', yamlio.dump_plain(data))\n"
        "\n"
        "def save_skin(home, data):\n"
        "    fsio.atomic_write_text(home / 'skin.yaml', yamlio.dump_plain(data))\n",
        encoding="utf-8",
    )
    assert find_config_writers(copy) == ["indus/core/rogue.py:4: dump_plain"]


# -- round trip ---------------------------------------------------------------------------------


def test_set_one_key_keeps_comments_and_order(cfg_scope: Scope) -> None:
    path = _install(cfg_scope, "commented.yaml")
    before = path.read_text(encoding="utf-8")
    writer.save(cfg_scope, {"logging": {"level": "ERROR"}})
    after = path.read_text(encoding="utf-8")
    assert after == before.replace("level: WARNING", "level: ERROR")


def test_saving_the_loaded_view_changes_nothing(cfg_scope: Scope) -> None:
    path = _install(cfg_scope, "commented.yaml")
    before = path.read_text(encoding="utf-8")
    writer.save(cfg_scope, loader.load(cfg_scope))
    assert path.read_text(encoding="utf-8") == before
    writer.save(cfg_scope, loader.load(cfg_scope).as_dict(), replace=True)
    assert path.read_text(encoding="utf-8") == before


def test_yaml11_spelling_survives(cfg_scope: Scope) -> None:
    path = _config(cfg_scope)
    path.write_text("network:\n  force_ipv4: yes   # v4 only\non: off\n", encoding="utf-8")
    writer.save(cfg_scope, {"logging": {"level": "DEBUG"}})
    text = path.read_text(encoding="utf-8")
    assert text.startswith("network:\n  force_ipv4: yes   # v4 only\non: off\n")
    assert yamlio.load_safe(text)["logging"] == {"level": "DEBUG"}


def test_new_ambiguous_string_is_quoted(cfg_scope: Scope) -> None:
    writer.save(cfg_scope, {"approvals": {"mode": "off"}, "display": {"language": "no"}})
    reread = yamlio.load_safe(_config(cfg_scope))
    assert reread["approvals"]["mode"] == "off"
    assert reread["display"]["language"] == "no"


def test_bool_int_flip_is_a_change(cfg_scope: Scope) -> None:
    path = _config(cfg_scope)
    path.write_text("custom:\n  flag: 1\n", encoding="utf-8")
    writer.save(cfg_scope, {"custom": {"flag": True}})
    assert yamlio.load_safe(path)["custom"]["flag"] is True


# -- defaults (R04.4) ---------------------------------------------------------------------------


def test_default_equal_value_not_written(cfg_scope: Scope) -> None:
    writer.save(cfg_scope, {"approvals": {"mode": "manual", "deny": ["rm"]}, "agent": {}})
    raw = yamlio.load_safe(_config(cfg_scope))
    assert raw == {"_config_version": 1, "approvals": {"deny": ["rm"]}}


def test_default_equal_value_kept_when_present_before(cfg_scope: Scope) -> None:
    path = _config(cfg_scope)
    path.write_text("approvals:\n  mode: manual\n", encoding="utf-8")
    writer.save(cfg_scope, {"approvals": {"mode": "manual", "deny": ["rm"]}})
    assert yamlio.load_safe(path)["approvals"] == {"mode": "manual", "deny": ["rm"]}


def test_full_view_save_writes_no_defaults(cfg_scope: Scope) -> None:
    writer.save(cfg_scope, loader.load(cfg_scope))
    raw = yamlio.load_safe(_config(cfg_scope))
    assert raw == {"_config_version": 1}


def test_create_writes_example(cfg_scope: Scope) -> None:
    writer.save(cfg_scope, {"logging": {"level": "DEBUG"}})
    text = _config(cfg_scope).read_text(encoding="utf-8")
    assert text.startswith("_config_version: 1\nlogging:\n  level: DEBUG\n")
    assert schema.render_example() in text
    assert yamlio.load_safe(text) == {"_config_version": 1, "logging": {"level": "DEBUG"}}
    # A second save never appends the example again.
    writer.save(cfg_scope, {"logging": {"level": "ERROR"}})
    assert _config(cfg_scope).read_text(encoding="utf-8").count("# Indus Agent settings.") == 1


def test_empty_existing_file_counts_as_create(cfg_scope: Scope) -> None:
    _config(cfg_scope).write_text("\n", encoding="utf-8")
    writer.save(cfg_scope, {"timezone": "Asia/Kolkata"})
    assert "# Indus Agent settings." in _config(cfg_scope).read_text(encoding="utf-8")


def test_version_stamp_kept(cfg_scope: Scope) -> None:
    path = _config(cfg_scope)
    path.write_text("logging:\n  level: DEBUG\n", encoding="utf-8")
    writer.save(cfg_scope, {"logging": {"level": "ERROR"}})
    assert yamlio.load_safe(path)["_config_version"] == 1
    writer.save(cfg_scope, {"logging": {"level": "ERROR"}}, replace=True)
    assert yamlio.load_safe(path)["_config_version"] == 1


# -- templates ----------------------------------------------------------------------------------


def test_env_template_preserved(cfg_scope: Scope) -> None:
    path = _install(cfg_scope, "templated.yaml")
    scope = cfg_scope.with_env(
        EnvView({"OPENAI_BASE_URL": "https://secret.example.test/v1", "TEST_PORT": "9"})
    )
    view = loader.load(scope)
    assert view.get("model.base_url") == "https://secret.example.test/v1"
    data = view.as_dict()
    data["model"]["default"] = "other-model"
    writer.save(scope, data)
    text = path.read_text(encoding="utf-8")
    assert "secret.example.test" not in text
    assert "base_url: ${OPENAI_BASE_URL}   # comes from .env" in text
    assert "http://localhost:${TEST_PORT}/v1" in text
    assert "default: other-model" in text


def test_changed_templated_value_is_written(cfg_scope: Scope) -> None:
    path = _install(cfg_scope, "templated.yaml")
    scope = cfg_scope.with_env(EnvView({"OPENAI_BASE_URL": "https://a.test"}))
    writer.save(scope, {"model": {"base_url": "https://b.test"}})
    assert yamlio.load_safe(path)["model"]["base_url"] == "https://b.test"


# -- refusals (R04.3) ---------------------------------------------------------------------------


def test_unreadable_file_refused_untouched(
    cfg_scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _install(cfg_scope, "commented.yaml")
    before = path.read_bytes()
    real_read = Path.read_bytes

    def denied(self: Path) -> bytes:
        if self == path:
            raise PermissionError(13, "Permission denied", str(self))
        return real_read(self)

    monkeypatch.setattr(Path, "read_bytes", denied)
    with pytest.raises(ConfigUnreadable) as info:
        writer.save(cfg_scope, {"logging": {"level": "ERROR"}})
    assert info.value.code == "config.unreadable"
    monkeypatch.undo()
    assert path.read_bytes() == before


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads chmod 000 files")
def test_chmod_000_refused(cfg_scope: Scope) -> None:
    path = _install(cfg_scope, "commented.yaml")
    path.chmod(0)
    try:
        with pytest.raises(ConfigUnreadable):
            writer.save(cfg_scope, {"logging": {"level": "ERROR"}})
    finally:
        path.chmod(0o600)


@pytest.mark.parametrize("text", ["approvals:\n  deny: [rm\n", "- a\n- b\n", "a: 1\na: 2\n"])
def test_invalid_yaml_refused_untouched(cfg_scope: Scope, text: str) -> None:
    path = _config(cfg_scope)
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigUnreadable):
        writer.save(cfg_scope, {"approvals": {"deny": []}})
    assert path.read_text(encoding="utf-8") == text
    assert backups.list_backups(path) == []


def test_failed_read_refused(cfg_scope: Scope) -> None:
    path = _install(cfg_scope, "broken.yaml")
    view = loader.load(cfg_scope)
    assert view.failed
    path.write_text("logging:\n  level: DEBUG\n", encoding="utf-8")  # fixed meanwhile
    for data in (view, view.as_dict()):
        with pytest.raises(ConfigFailedRead) as info:
            writer.save(cfg_scope, data)
        assert info.value.code == "config.failed_read"
    assert path.read_text(encoding="utf-8") == "logging:\n  level: DEBUG\n"


@pytest.mark.parametrize("how", ["env", "marker"])
def test_managed_marker_refused(cfg_scope: Scope, how: str) -> None:
    scope = cfg_scope
    if how == "env":
        scope = cfg_scope.with_env(EnvView({"INDUS_MANAGED": "1"}))
    else:
        (cfg_scope.home / ".managed").write_text("", encoding="utf-8")
    with pytest.raises(ConfigManaged) as info:
        writer.save(scope, {"logging": {"level": "DEBUG"}})
    assert info.value.code == "config.managed"
    assert not _config(cfg_scope).exists()


def test_managed_env_false_is_not_managed(cfg_scope: Scope) -> None:
    scope = cfg_scope.with_env(EnvView({"INDUS_MANAGED": "0"}))
    writer.save(scope, {"logging": {"level": "DEBUG"}})
    assert _config(cfg_scope).exists()


# -- additive vs replace ------------------------------------------------------------------------


def test_additive_save_keeps_omitted_section(cfg_scope: Scope) -> None:
    path = _install(cfg_scope, "commented.yaml")
    writer.save(cfg_scope, {"display": {"language": "en-GB"}})
    raw = yamlio.load_safe(path)
    assert raw["approvals"]["deny"] == ["rm -rf /", "mkfs"]
    assert raw["model"]["provider"] == "openrouter"
    assert raw["display"]["language"] == "en-GB"


def test_replace_deletes_omitted_section(cfg_scope: Scope) -> None:
    path = _install(cfg_scope, "commented.yaml")
    raw = yamlio.load_safe(path)
    del raw["logging"]
    writer.save(cfg_scope, raw, replace=True)
    text = path.read_text(encoding="utf-8")
    assert "logging" not in yamlio.load_safe(text)
    assert "# The model I use most days." in text


def test_scalar_model_shorthand_kept(cfg_scope: Scope) -> None:
    path = _config(cfg_scope)
    path.write_text("model: test-model   # mine\n", encoding="utf-8")
    writer.save(cfg_scope, loader.load(cfg_scope))
    assert path.read_text(encoding="utf-8") == "model: test-model   # mine\n_config_version: 1\n"
    writer.save(cfg_scope, {"model": {"provider": "local"}})
    assert yamlio.load_safe(path)["model"] == {"default": "test-model", "provider": "local"}


# -- file handling ------------------------------------------------------------------------------


def test_new_file_0600_and_existing_mode_kept(cfg_scope: Scope) -> None:
    path = _config(cfg_scope)
    writer.save(cfg_scope, {"timezone": "UTC"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.chmod(0o640)
    writer.save(cfg_scope, {"timezone": "Asia/Kolkata"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_pre_write_backup_and_cache_invalidated(cfg_scope: Scope) -> None:
    path = _install(cfg_scope, "commented.yaml")
    before = path.read_bytes()
    assert loader.load(cfg_scope).get("logging.level") == "WARNING"
    writer.save(cfg_scope, {"logging": {"level": "ERROR"}})
    assert loader.load(cfg_scope).get("logging.level") == "ERROR"
    pre = backups.list_backups(path, "pre-write")
    assert len(pre) == 1 and pre[0].read_bytes() == before


def test_symlinked_config_is_written_through(cfg_scope: Scope, tmp_path: Path) -> None:
    dotfiles = tmp_path / "dotfiles"
    dotfiles.mkdir()
    real = dotfiles / "indus.yaml"
    real.write_text("logging:\n  level: DEBUG\n", encoding="utf-8")
    _config(cfg_scope).symlink_to(real)
    writer.save(cfg_scope, {"logging": {"level": "ERROR"}})
    assert _config(cfg_scope).is_symlink()
    assert yamlio.load_safe(real)["logging"]["level"] == "ERROR"
