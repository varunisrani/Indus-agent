"""P07-25: `indus completion` and the dynamic completers."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import click
import pytest

from indus.cli import app as app_mod
from indus.cli.args import CliState
from indus.cli.commands import completion
from indus.foundation.scope import Scope
from tests.fakes.clock import FakeClock
from tests.unit.cli.commands.cmdrun import root_scope, run_cli

HEADS = {
    "bash": "_indus_completion() {",
    "zsh": "#compdef indus",
    "fish": "function _indus_completion;",
}


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_script_heads(capsys: pytest.CaptureFixture[str], scope: Scope, shell: str) -> None:
    ran = run_cli(capsys, scope, "completion", shell)
    assert ran.code == 0
    assert ran.out.lstrip().startswith(HEADS[shell])
    assert "_INDUS_COMPLETE" in ran.out


def test_default_shell_is_bash(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    ran = run_cli(capsys, scope, "completion")
    assert ran.out.lstrip().startswith(HEADS["bash"])
    assert run_cli(capsys, scope, "completion", "tcsh").code == 2


def _ctx(scope: Scope) -> click.Context:
    return click.Context(app_mod.cli, obj=CliState(scope=scope))


def test_profile_completion_from_root(isolated_home: Path, fake_clock: FakeClock) -> None:
    scope = root_scope(isolated_home, fake_clock)
    for name in ("work", "weekend", "hobby"):
        home = isolated_home / "profiles" / name
        home.mkdir(parents=True)
        (home / "profile.yaml").touch()
    (isolated_home / "profiles" / "wreck").mkdir()  # no marker: not a profile
    found = [c.value for c in completion.complete_profiles(_ctx(scope), None, "w")]
    assert found == ["weekend", "work"]
    everything = [c.value for c in completion.complete_profiles(_ctx(scope), None, "")]
    assert everything == ["default", "hobby", "weekend", "work"]


def _make_store(path: Path, rows: list[tuple[str, str | None, str]]) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE sessions (id TEXT, title TEXT, source TEXT, started_at REAL)")
    for index, (title, source, sid) in enumerate(rows):
        conn.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)", (sid, title, source, float(index)))
    conn.commit()
    conn.close()


def test_titles_from_a_fixture_store(scope: Scope) -> None:
    _make_store(
        scope.home / "state.db",
        [
            ("Fix the build", "cli", "a"),
            ("fix flaky test", "cli", "b"),
            ("Hidden helper", "subagent", "c"),
            ("Weekly report", None, "d"),
        ],
    )
    titles = completion.session_titles(scope.home)
    assert titles == ["Weekly report", "fix flaky test", "Fix the build"]
    found = [c.value for c in completion.complete_session_titles(_ctx(scope), None, "fix")]
    assert found == ["fix flaky test", "Fix the build"]


def test_missing_store_gives_nothing(scope: Scope) -> None:
    assert completion.session_titles(scope.home) == []


def test_slow_store_gives_nothing_without_hanging(scope: Scope) -> None:
    (scope.home / "state.db").write_bytes(b"")
    release = threading.Event()

    def stuck(path: Path) -> sqlite3.Connection:
        release.wait(5)
        raise sqlite3.OperationalError("never answered")

    try:
        assert completion.session_titles(scope.home, budget=0.05, opener=stuck) == []
    finally:
        release.set()


def test_store_open_is_read_only(scope: Scope, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_store(scope.home / "state.db", [("one", "cli", "a")])
    seen: list[str] = []
    real = sqlite3.connect

    def spy(database: str, *args: object, **kwargs: object) -> sqlite3.Connection:
        seen.append(str(database))
        return real(database, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(sqlite3, "connect", spy)
    assert completion.session_titles(scope.home) == ["one"]
    assert seen and all(s.endswith("?mode=ro") for s in seen)


def test_toolset_completion_after_a_comma(scope: Scope) -> None:
    found = [c.value for c in completion.complete_toolsets(_ctx(scope), None, "web,te")]
    assert found == ["web,terminal"]
    assert "all" in [c.value for c in completion.complete_toolsets(_ctx(scope), None, "a")]
    assert [c.value for c in completion.complete_toolsets(_ctx(scope), None, "web,we")] == []


def test_app_options_carry_the_hooks() -> None:
    params = {p.name: p for p in app_mod.cli.params}
    for name in ("profile", "toolsets", "resume", "continue_"):
        assert params[name]._custom_shell_complete is not None, name  # pyright: ignore[reportPrivateUsage]


def test_shell_request_completes_profiles(
    isolated_home: Path,
    fake_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scope = root_scope(isolated_home, fake_clock)
    home = isolated_home / "profiles" / "work"
    home.mkdir(parents=True)
    (home / "profile.yaml").touch()
    monkeypatch.setenv("_INDUS_COMPLETE", "bash_complete")
    monkeypatch.setenv("COMP_WORDS", "indus -p w")
    monkeypatch.setenv("COMP_CWORD", "2")
    with pytest.raises(SystemExit) as stop:
        app_mod.dispatch([], CliState(scope=scope))
    assert stop.value.code == 0
    assert capsys.readouterr().out.strip() == "plain,work"
