"""P07-11: `indus config show/get/path/env-path/edit` (R04.4, R04.15)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from indus.cli.commands import config_read
from indus.foundation import yamlio
from indus.foundation.config import loader
from indus.foundation.scope import Scope
from indus.foundation.secrets import open_secret_store
from tests.unit.cli.commands.cmdrun import run_cli, write_settings

FAKE_KEY = "test" + "-key-" + "0000abcd1234wxyz"


def test_bare_config_is_show(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    write_settings(scope, "agent:\n  max_turns: 42\n")
    ran = run_cli(capsys, scope, "config")
    assert ran.code == 0
    assert "Settings file:" in ran.out
    assert "max_turns: 42" in ran.out


def test_show_masks_credentials(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    write_settings(
        scope,
        f"providers:\n  mine:\n    base_url: http://127.0.0.1:9000/v1\n    api_key: {FAKE_KEY}\n"
        "    key_env: MINE_API_KEY\n",
    )
    ran = run_cli(capsys, scope, "config", "show")
    assert ran.code == 0
    assert FAKE_KEY not in ran.out
    assert "test…wxyz" in ran.out
    assert "key_env: MINE_API_KEY" in ran.out


@pytest.mark.parametrize(
    ("text", "key", "expected"),
    [
        (f"model:\n  api_key: {FAKE_KEY}\n", "model.api_key", "test…wxyz"),
        ("model:\n  api_key: short1\n", "model.api_key", "[redacted]"),
        ("model:\n  api_key: ${OPENAI_API_KEY}\n", "model.api_key", "${OPENAI_API_KEY}"),
        (
            f"providers:\n  p:\n    token: {FAKE_KEY}\n    base_url: http://h/v1\n",
            "providers.p",
            "token: test…wxyz\nbase_url: http://h/v1",
        ),
        (
            f"providers:\n  p:\n    headers:\n      - {{auth_token: {FAKE_KEY}}}\n",
            "providers.p.headers",
            "- auth_token: test…wxyz",
        ),
        ("agent:\n  max_turns: 7\n", "agent.max_turns", "7"),
        ("display:\n  show_reasoning: true\n", "display.show_reasoning", "true"),
        ("model:\n  context_length: null\n", "model.context_length", "null"),
    ],
)
def test_get_masking_table(
    capsys: pytest.CaptureFixture[str], scope: Scope, text: str, key: str, expected: str
) -> None:
    write_settings(scope, text)
    ran = run_cli(capsys, scope, "config", "get", key)
    assert ran.code == 0, ran.err
    assert ran.out.rstrip("\n") == expected


def test_get_raw_and_json(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    write_settings(scope, f"model:\n  api_key: {FAKE_KEY}\nagent:\n  max_turns: 9\n")
    assert run_cli(capsys, scope, "config", "get", "model.api_key", "--raw").out.strip() == FAKE_KEY
    ran = run_cli(capsys, scope, "config", "get", "agent", "--json")
    assert json.loads(ran.out)["max_turns"] == 9
    ran = run_cli(capsys, scope, "config", "get", "model.api_key", "--json")
    assert json.loads(ran.out) == "test…wxyz"


def test_get_scalar_formats(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    write_settings(scope, "approvals:\n  mode: 'off'\n  deny: [rm -rf build]\n")
    assert run_cli(capsys, scope, "config", "get", "approvals.mode").out == "off\n"
    assert run_cli(capsys, scope, "config", "get", "approvals.deny").out == "- rm -rf build\n"
    # built-in values count as set
    assert run_cli(capsys, scope, "config", "get", "terminal.backend").out == "local\n"


def test_get_missing_key_exits_1(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    ran = run_cli(capsys, scope, "config", "get", "nothing.here")
    assert ran.code == 1
    assert ran.out == ""
    assert "Config key not set: nothing.here" in ran.err
    ran = run_cli(capsys, scope, "config", "get", "SOME_UNSET_NAME_123")
    assert ran.code == 1


def test_get_env_shaped_reads_store_then_env_file(
    capsys: pytest.CaptureFixture[str], scope: Scope
) -> None:
    store = open_secret_store(scope.home, scope.profile_id, mode="file")
    store.set("OPENAI_API_KEY", FAKE_KEY)
    (scope.home / ".env").write_text("LOG_STYLE=plain\nOPENAI_API_KEY=ignored-older-copy\n")
    ran = run_cli(capsys, scope, "config", "get", "OPENAI_API_KEY")
    assert ran.out.strip() == "test…wxyz"
    ran = run_cli(capsys, scope, "config", "get", "OPENAI_API_KEY", "--raw")
    assert ran.out.strip() == FAKE_KEY
    assert run_cli(capsys, scope, "config", "get", "LOG_STYLE").out.strip() == "plain"


def test_get_phantom_path_notes_on_stderr(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    write_settings(scope, "agent:\n  max_turn: 3\n")
    ran = run_cli(capsys, scope, "config", "get", "agent.max_turn")
    assert ran.code == 0
    assert ran.out == "3\n"
    assert "not a setting this version knows" in ran.err
    assert "agent.max_turns" in ran.err


def test_path_and_env_path(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    assert run_cli(capsys, scope, "config", "path").out.strip() == str(scope.home / "config.yaml")
    assert run_cli(capsys, scope, "config", "env-path").out.strip() == str(scope.home / ".env")


def test_edit_seeds_example_only(
    capsys: pytest.CaptureFixture[str], scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_editor(argv: Sequence[str]) -> int:
        calls.append(list(argv))
        return 0

    monkeypatch.setattr(config_read, "run_editor", fake_editor)
    scope = scope.with_env(scope.env.with_overlay({"VISUAL": "", "EDITOR": "myedit --wait"}))
    ran = run_cli(capsys, scope, "config", "edit", tty=True)
    assert ran.code == 0, ran.err
    path = scope.home / "config.yaml"
    assert calls == [["myedit", "--wait", str(path)]]
    text = path.read_text(encoding="utf-8")
    assert yamlio.load_safe(text) == {"_config_version": 1}  # R04.4: no default values written
    assert "# Indus Agent settings." in text
    # an existing file is opened as it is
    before = path.read_bytes()
    run_cli(capsys, scope, "config", "edit", tty=True)
    assert path.read_bytes() == before


def test_edit_without_terminal_exits_1(
    capsys: pytest.CaptureFixture[str], scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config_read, "run_editor", lambda argv: pytest.fail("editor started"))
    ran = run_cli(capsys, scope, "config", "edit")
    assert ran.code == 1
    assert "No terminal" in ran.err
    assert (scope.home / "config.yaml").is_file()


def test_edit_refused_on_managed_install(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    (scope.home / ".managed").touch()
    ran = run_cli(capsys, scope, "config", "edit", tty=True)
    assert ran.code == 1
    assert not (scope.home / "config.yaml").exists()


def test_editor_choice(tmp_path: Path) -> None:
    assert config_read.editor_argv({"VISUAL": "vis", "EDITOR": "ed"}) == ["vis"]
    assert config_read.editor_argv({"EDITOR": "code --wait"}) == ["code", "--wait"]
    tool = tmp_path / "bin" / "vi"
    tool.parent.mkdir()
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    assert config_read.editor_argv({"PATH": str(tool.parent)}) == [str(tool)]
    assert config_read.editor_argv({"PATH": str(tmp_path / "none")}) is None


def test_broken_file_is_reported_by_show(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    write_settings(scope, "agent: [unclosed\n")
    ran = run_cli(capsys, scope, "config", "show")
    assert ran.code == 0
    assert "could not be read" in ran.err
    loader.reset()
