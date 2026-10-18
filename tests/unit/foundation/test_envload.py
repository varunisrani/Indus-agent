from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from indus.foundation import envload
from indus.foundation.envfile import set_values
from indus.foundation.envload import (
    build_env,
    force_ipv4,
    publish_to_process,
    redaction_flag,
    register_managed_keys,
    terminal_bridge,
)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "profile"
    path.mkdir()
    return path


@pytest.fixture(autouse=True)
def clean_registry() -> Iterator[None]:
    saved = set(envload._managed_registry)  # pyright: ignore[reportPrivateUsage]
    envload._managed_registry.clear()  # pyright: ignore[reportPrivateUsage]
    yield
    envload._managed_registry.clear()  # pyright: ignore[reportPrivateUsage]
    envload._managed_registry.update(saved)  # pyright: ignore[reportPrivateUsage]


def test_file_beats_shell(home: Path) -> None:
    (home / ".env").write_text("OPENAI_API_KEY=new\n")
    view = build_env(home, {"OPENAI_API_KEY": "old", "PATH": "/bin"})
    assert view["OPENAI_API_KEY"] == "new"
    assert view["PATH"] == "/bin"


def test_view_keeps_inherited_as_base_and_changes_as_overlay(home: Path) -> None:
    (home / ".env").write_text("OPENAI_API_KEY=new\nEXTRA=1\n")
    set_values(home / ".env", {"GONE": "x"})
    set_values(home / ".env", {"GONE": None})  # recorded as ours, now removed
    inherited = {"OPENAI_API_KEY": "old", "PATH": "/bin", "GONE": "shell"}
    view = build_env(home, inherited)
    assert dict(view.base) == inherited
    assert dict(view.overlay) == {"OPENAI_API_KEY": "new", "EXTRA": "1", "GONE": None}
    assert view.to_dict() == {"OPENAI_API_KEY": "new", "PATH": "/bin", "EXTRA": "1"}


def test_removed_recorded_key_is_cleared(home: Path) -> None:
    env = home / ".env"
    set_values(env, {"OPENAI_API_KEY": "k1", "OTHER": "x"})
    set_values(env, {"OPENAI_API_KEY": None})
    view = build_env(home, {"OPENAI_API_KEY": "k1-stale-from-earlier-publish"})
    assert "OPENAI_API_KEY" not in view
    assert view["OTHER"] == "x"


def test_recorded_key_cleared_even_when_file_deleted(home: Path) -> None:
    env = home / ".env"
    set_values(env, {"OPENAI_API_KEY": "k1"})
    env.unlink()
    assert "OPENAI_API_KEY" not in build_env(home, {"OPENAI_API_KEY": "k1"})


def test_registry_key_absent_from_file_is_cleared(home: Path) -> None:
    register_managed_keys("ANTHROPIC_API_KEY")
    (home / ".env").write_text("OTHER=1\n")
    view = build_env(home, {"ANTHROPIC_API_KEY": "shell"})
    assert "ANTHROPIC_API_KEY" not in view


def test_managed_keys_argument_counts_like_registry(home: Path) -> None:
    (home / ".env").write_text("OTHER=1\n")
    view = build_env(home, {"X_TOKEN": "shell"}, managed_keys=["X_TOKEN"])
    assert "X_TOKEN" not in view


def test_bare_profile_keeps_shell_registry_keys(home: Path) -> None:
    register_managed_keys("ANTHROPIC_API_KEY")
    view = build_env(home, {"ANTHROPIC_API_KEY": "shell"})
    assert view["ANTHROPIC_API_KEY"] == "shell"


def test_unmanaged_shell_keys_survive(home: Path) -> None:
    (home / ".env").write_text("A=1\n")
    assert build_env(home, {"HOME": "/home/u"})["HOME"] == "/home/u"


def test_location_names_never_registered_or_cleared(home: Path) -> None:
    register_managed_keys("INDUS_HOME", "INDUS_PROFILE", "bad name")
    assert envload.managed_key_names() == frozenset()
    (home / ".env").write_text("A=1\n")
    view = build_env(home, {"INDUS_HOME": str(home)}, managed_keys=["INDUS_HOME"])
    assert view["INDUS_HOME"] == str(home)


def test_location_lines_in_file_are_ignored(home: Path, caplog: pytest.LogCaptureFixture) -> None:
    (home / ".env").write_text("INDUS_HOME=/elsewhere\nINDUS_PROFILE=other\n")
    with caplog.at_level(logging.WARNING, logger="indus.foundation.envload"):
        view = build_env(home, {"INDUS_HOME": str(home)})
    assert view["INDUS_HOME"] == str(home)
    assert "INDUS_PROFILE" not in view
    assert len(caplog.records) == 2


def test_os_environ_untouched(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "old")
    (home / ".env").write_text("OPENAI_API_KEY=new\nFRESH=1\n")
    before = dict(os.environ)
    view = build_env(home, os.environ, config={"terminal": {"backend": "local"}})
    assert view["OPENAI_API_KEY"] == "new"
    assert dict(os.environ) == before


def test_view_is_read_only(home: Path) -> None:
    view = build_env(home, {"A": "1"})
    with pytest.raises(TypeError):
        view["A"] = "2"  # type: ignore[index]


def test_redaction_flag_bridged_when_unset(home: Path) -> None:
    view = build_env(home, {}, config={"security": {"redact_secrets": False}})
    assert view["INDUS_REDACT_SECRETS"] == "false"
    assert redaction_flag(view) is False
    view = build_env(home, {}, config={"security": {"redact_secrets": True}})
    assert view["INDUS_REDACT_SECRETS"] == "true"


def test_redaction_flag_left_when_set(home: Path) -> None:
    view = build_env(
        home, {"INDUS_REDACT_SECRETS": "true"}, config={"security": {"redact_secrets": False}}
    )
    assert view["INDUS_REDACT_SECRETS"] == "true"
    (home / ".env").write_text("INDUS_REDACT_SECRETS=0\n")
    view = build_env(home, {}, config={"security": {"redact_secrets": True}})
    assert view["INDUS_REDACT_SECRETS"] == "0"
    assert redaction_flag(view) is False


def test_redaction_flag_defaults_on() -> None:
    assert redaction_flag({}) is True
    for word in ["1", "true", "yes", "on", "anything"]:
        assert redaction_flag({"INDUS_REDACT_SECRETS": word}) is True
    for word in ["0", "false", "No", " OFF "]:
        assert redaction_flag({"INDUS_REDACT_SECRETS": word}) is False


def test_terminal_bridge_beats_env_file(home: Path) -> None:
    (home / ".env").write_text("INDUS_TERMINAL_BACKEND=docker\n")
    view = build_env(home, {}, config={"terminal": {"backend": "local", "timeout": 180}})
    assert view["INDUS_TERMINAL_BACKEND"] == "local"
    assert view["INDUS_TERMINAL_TIMEOUT"] == "180"


def test_terminal_bridge_reapplied_on_rebuild(home: Path) -> None:
    config = {"terminal": {"backend": "local"}}
    first = build_env(home, {}, config=config)
    assert first["INDUS_TERMINAL_BACKEND"] == "local"
    # A later edit drops a stray line into .env; the next reload must not switch the backend.
    (home / ".env").write_text("INDUS_TERMINAL_BACKEND=ssh\n")
    second = build_env(home, dict(first), config=config)
    assert second["INDUS_TERMINAL_BACKEND"] == "local"


def test_terminal_bridge_shapes() -> None:
    assert terminal_bridge(None) == {}
    assert terminal_bridge({"terminal": "nope"}) == {}
    assert terminal_bridge(
        {"terminal": {"persist": True, "cwd": ".", "env": {"A": "1"}, "none": None}}
    ) == {"INDUS_TERMINAL_PERSIST": "true", "INDUS_TERMINAL_CWD": "."}


def test_force_ipv4() -> None:
    assert force_ipv4({"network": {"force_ipv4": True}}) is True
    assert force_ipv4({"network": {"force_ipv4": "yes"}}) is False
    assert force_ipv4(None) is False


def test_unreadable_env_file_is_treated_as_empty(
    home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (home / ".env").mkdir()  # reading a directory raises an OSError
    with caplog.at_level(logging.WARNING, logger="indus.foundation.envload"):
        view = build_env(home, {"A": "1"})
    assert dict(view) == {"A": "1"}
    assert caplog.records


def test_publish_to_process_makes_mapping_match() -> None:
    target = {"KEEP": "1", "CHANGE": "old", "GONE": "x"}
    publish_to_process({"KEEP": "1", "CHANGE": "new", "ADD": "a"}, target)
    assert target == {"KEEP": "1", "CHANGE": "new", "ADD": "a"}


def test_publish_to_process_defaults_to_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INDUS_PUBLISH_PROBE", "old")
    view = dict(os.environ)
    view["INDUS_PUBLISH_PROBE"] = "new"
    publish_to_process(view)
    assert os.environ["INDUS_PUBLISH_PROBE"] == "new"
