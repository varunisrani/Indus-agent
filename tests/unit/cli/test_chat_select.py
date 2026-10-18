"""P06-03: the chat command's interface choice, session selection and frozen launch flags."""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from indus.cli import chat as chat_mod
from indus.cli import resume
from indus.cli.args import BARE_CONTINUE, ChatArgs, CliState, GlobalArgs, HostOverrides
from indus.cli.handoff import ReplLaunch
from indus.cli.launch import run_launch
from indus.cli.session_select import (
    LAUNCH_KEY,
    LaunchPolicy,
    SelectionError,
    check_frozen,
    launch_from_args,
    select_session,
    stored_launch,
    workspace_key,
)
from indus.foundation.scope import Scope
from indus.state import registry, sessions, titles
from indus.state.registry import StoreHandle
from tests.fakes.agent import any_window
from tests.fakes.model import FakeModelServer, fake_route, script_of, text_turn


@dataclass
class RecordingRepl:
    """Takes the place of the REPL entry: records each ``ReplLaunch`` and, with ``run_seed``,
    runs the seeded ``-q`` text as one turn through the host (as the REPL would)."""

    code: int = 0
    run_seed: bool = False
    launches: list[ReplLaunch] = field(default_factory=list[ReplLaunch])
    replies: list[str | None] = field(default_factory=list[str | None])

    def __call__(self, launch: ReplLaunch) -> int:
        self.launches.append(launch)
        if self.run_seed and launch.seed is not None:
            ticket = launch.host.submit(launch.session_id, launch.seed)
            result = launch.host.wait(launch.session_id, ticket.turn_id, timeout=30.0)
            self.replies.append(result.final_text)
        return self.code


A1 = "20260101_090000_aaaaa1"
A2 = "20260101_100000_aaaaa2"
B1 = "20260101_110000_bbbbb1"
T1 = "20260101_080000_ttttt1"


@pytest.fixture(autouse=True)
def keep_cwd() -> Iterator[None]:
    here = os.getcwd()
    yield
    os.chdir(here)


@pytest.fixture
def workspaces(tmp_path: Path) -> tuple[Path, Path]:
    a = tmp_path / "repo_a"
    b = tmp_path / "repo_b"
    for repo in (a, b):
        (repo / ".git").mkdir(parents=True)
        (repo / "sub").mkdir()
    return a, b


@pytest.fixture
def store(scope: Scope) -> Iterator[StoreHandle]:
    handle = registry.acquire(scope)
    yield handle
    registry.release(handle)


def add(store: StoreHandle, sid: str, where: Path, at: float, **extra: Any) -> None:
    sessions.create(
        store, sid, extra.pop("source", "cli"), cwd=str(where), git_root=str(where),
        started_at=at, **extra,
    )  # fmt: skip


@pytest.fixture
def filled(store: StoreHandle, workspaces: tuple[Path, Path]) -> StoreHandle:
    a, b = workspaces
    add(store, T1, a, 1000.0)
    titles.set_title(store, T1, "latest")
    add(store, A1, a, 2000.0)
    titles.set_title(store, A1, "first idea")
    add(store, A2, a, 3000.0)
    add(store, B1, b, 4000.0)
    return store


def bare() -> GlobalArgs:
    return GlobalArgs(continue_=BARE_CONTINUE)


# -- selection -------------------------------------------------------------------------------------


def test_no_selection_flags_means_new(filled: StoreHandle, workspaces: tuple[Path, Path]) -> None:
    assert select_session(filled, GlobalArgs(), cwd=workspaces[0]) is None


def test_breadcrumb_wins(filled: StoreHandle, workspaces: tuple[Path, Path]) -> None:
    a, _ = workspaces
    assert select_session(filled, bare(), cwd=a, breadcrumb=B1) == B1


def test_breadcrumb_for_a_missing_session_falls_back(
    filled: StoreHandle, workspaces: tuple[Path, Path]
) -> None:
    a, _ = workspaces
    assert select_session(filled, bare(), cwd=a, breadcrumb="20990101_000000_gone00") == A2


def test_workspace_fallback(filled: StoreHandle, workspaces: tuple[Path, Path]) -> None:
    a, b = workspaces
    assert select_session(filled, bare(), cwd=a / "sub") == A2
    assert select_session(filled, bare(), cwd=b) == B1
    assert select_session(filled, GlobalArgs(resume="latest"), cwd=b / "sub") == B1


def test_no_session_in_workspace_exits_1(filled: StoreHandle, tmp_path: Path) -> None:
    lonely = tmp_path / "elsewhere"
    lonely.mkdir()
    with pytest.raises(SelectionError) as caught:
        select_session(filled, bare(), cwd=lonely)
    assert caught.value.code == 1


def test_latest_keyword_beats_title(filled: StoreHandle, workspaces: tuple[Path, Path]) -> None:
    a, _ = workspaces
    assert select_session(filled, GlobalArgs(resume="latest"), cwd=a) == A2
    assert select_session(filled, GlobalArgs(resume="LATEST"), cwd=a) == A2
    assert select_session(filled, GlobalArgs(continue_="latest"), cwd=a) == T1
    assert select_session(filled, GlobalArgs(resume=T1), cwd=a) == T1


def test_by_title_id_and_prefix(filled: StoreHandle, workspaces: tuple[Path, Path]) -> None:
    a, _ = workspaces
    assert select_session(filled, GlobalArgs(continue_="first idea"), cwd=a) == A1
    assert select_session(filled, GlobalArgs(resume=B1), cwd=a) == B1
    assert select_session(filled, GlobalArgs(resume="20260101_1100"), cwd=a) == B1


def test_miss_names_the_text(filled: StoreHandle, workspaces: tuple[Path, Path]) -> None:
    with pytest.raises(SelectionError) as caught:
        select_session(filled, GlobalArgs(continue_="nothing here"), cwd=workspaces[0])
    assert caught.value.code == 1
    assert caught.value.lines()[0] == "No session found matching 'nothing here'."
    assert len(caught.value.lines()) == 2


def test_oneshot_sessions_count_as_cli_history(
    store: StoreHandle, workspaces: tuple[Path, Path]
) -> None:
    a, _ = workspaces
    add(store, A1, a, 2000.0)
    add(store, A2, a, 3000.0, source="oneshot")
    add(store, B1, a, 4000.0, source="tool")
    assert select_session(store, bare(), cwd=a) == A2


def test_workspace_key(workspaces: tuple[Path, Path], tmp_path: Path) -> None:
    a, _ = workspaces
    assert workspace_key(a / "sub") == a
    plain = tmp_path / "plain"
    plain.mkdir()
    assert workspace_key(plain) in (plain, *plain.parents)


# -- frozen launch flags ---------------------------------------------------------------------------


def test_frozen_flag_conflict_and_same_value(tmp_path: Path) -> None:
    stored = LaunchPolicy(cwd=str(tmp_path), model="m1", toolsets=("a", "b"), yolo=False)
    _, given = launch_from_args(GlobalArgs(model="m1", toolsets="b,a"), ChatArgs(), cwd=tmp_path)
    assert check_frozen(stored, given) == []
    _, given = launch_from_args(GlobalArgs(model="m2", yolo=True), ChatArgs(), cwd=tmp_path)
    assert check_frozen(stored, given) == ["--model", "--yolo"]
    _, given = launch_from_args(GlobalArgs(in_dir="x"), ChatArgs(max_turns=3), cwd=tmp_path / "x")
    assert check_frozen(stored, given) == ["--in", "--max-turns"]


def test_launch_record_round_trip(tmp_path: Path) -> None:
    policy, _ = launch_from_args(
        GlobalArgs(model="m", reasoning="high", toolsets="web", skills=("a,b",), safe_mode=True),
        ChatArgs(max_turns=9),
        cwd=tmp_path,
    )
    row = {"model_config": {LAUNCH_KEY: policy.record()}, "cwd": str(tmp_path)}
    assert stored_launch(row) == policy
    assert stored_launch({"model_config": "{}"}) is None


# -- the chat command ------------------------------------------------------------------------------


def make_state(scope: Scope, server: FakeModelServer, *, tty: bool = True) -> CliState:
    scope = scope.with_env(scope.env.with_overlay({resume.TERMINAL_ENV: "test-pane"}))
    return CliState(
        scope=scope,
        overrides=HostOverrides(
            route=fake_route(server),
            client_factory=lambda route: server.client(),
            list_models=any_window,
        ),
        stdin_isatty=lambda: tty,
        stdout_isatty=lambda: tty,
    )


@pytest.fixture
def repl(monkeypatch: pytest.MonkeyPatch) -> RecordingRepl:
    stand_in = RecordingRepl()
    monkeypatch.setattr(chat_mod, "load_repl", lambda: stand_in)
    return stand_in


def test_the_repl_entry_is_the_launch_adapter() -> None:
    assert chat_mod.load_repl() is run_launch


def test_non_tty_forces_classic(scope: Scope) -> None:
    server = FakeModelServer(script_of(text_turn("x")))
    state = make_state(scope, server, tty=False)
    tui = scope.with_env(scope.env.with_overlay({"INDUS_TUI": "1"}))
    state.scope = tui
    assert chat_mod.choose_interface(state, GlobalArgs()) == ("classic", None)
    state.stdin_isatty = lambda: True
    state.stdout_isatty = lambda: True
    kind, note = chat_mod.choose_interface(state, GlobalArgs())
    assert kind == "classic" and note
    assert chat_mod.choose_interface(state, GlobalArgs(interface="cli")) == ("classic", None)


def test_tui_flag_exits_2(scope: Scope, capsys: pytest.CaptureFixture[str]) -> None:
    state = make_state(scope, FakeModelServer(script_of(text_turn("x"))))
    assert chat_mod.run_chat(state, GlobalArgs(interface="tui"), ChatArgs()) == 2
    assert "not part of this build" in capsys.readouterr().err


def test_usage_errors_exit_2(scope: Scope) -> None:
    state = make_state(scope, FakeModelServer(script_of(text_turn("x"))))
    both = ChatArgs(query="a", query_file="b")
    assert chat_mod.run_chat(state, GlobalArgs(), both) == 2
    assert chat_mod.run_chat(state, GlobalArgs(reasoning="huge"), ChatArgs()) == 2


def test_in_dir_missing_exits_1(scope: Scope, tmp_path: Path, repl: RecordingRepl) -> None:
    state = make_state(scope, FakeModelServer(script_of(text_turn("x"))))
    args = GlobalArgs(in_dir=str(tmp_path / "nope"))
    assert chat_mod.run_chat(state, args, ChatArgs()) == 1
    assert repl.launches == []


def test_new_session_hands_over_to_the_repl(
    scope: Scope, workspaces: tuple[Path, Path], repl: RecordingRepl
) -> None:
    a, _ = workspaces
    server = FakeModelServer(script_of(text_turn("hello")))
    state = make_state(scope, server)
    repl.run_seed = True
    code = chat_mod.run_chat(
        state, GlobalArgs(in_dir=str(a), model="fake-model-1"), ChatArgs(query="/help !ls")
    )
    assert code == 0
    launch = repl.launches[0]
    assert launch.resumed is False and launch.history == []
    assert launch.seed == "/help !ls"
    assert repl.replies == ["hello"]
    assert server.model_requests[0].body["messages"][-1]["content"] == "/help !ls"
    assert Path.cwd() == a.resolve()
    assert resume.read_breadcrumb(state.scope) == launch.session_id
    handle = registry.acquire(scope)
    try:
        row = sessions.get(handle, launch.session_id)
    finally:
        registry.release(handle)
    assert row is not None and row["git_root"] == str(a.resolve())
    stored = stored_launch(row)
    assert stored is not None and stored.model == "fake-model-1" and stored.provider == "fake"


def test_resume_restores_cwd_history_and_refuses_changed_flags(
    scope: Scope, workspaces: tuple[Path, Path], repl: RecordingRepl, tmp_path: Path
) -> None:
    a, _ = workspaces
    server = FakeModelServer(script_of(text_turn("one"), text_turn("two")))
    state = make_state(scope, server)
    repl.run_seed = True
    assert chat_mod.run_chat(state, GlobalArgs(in_dir=str(a)), ChatArgs(query="hi")) == 0
    first = repl.launches[0].session_id

    os.chdir(tmp_path)
    repl.run_seed = False
    assert chat_mod.run_chat(state, GlobalArgs(resume=first), ChatArgs()) == 0
    again = repl.launches[1]
    assert again.resumed is True and again.session_id == first
    assert [row.role for row in again.history] == ["user", "assistant"]
    assert Path.cwd() == a.resolve()

    os.chdir(tmp_path)
    assert chat_mod.run_chat(state, GlobalArgs(resume=first, no_restore_cwd=True), ChatArgs()) == 0
    assert Path.cwd() == tmp_path

    assert chat_mod.run_chat(state, GlobalArgs(resume=first, model="other"), ChatArgs()) == 2
    assert chat_mod.run_chat(state, GlobalArgs(resume=first, yolo=True), ChatArgs()) == 2
    assert len(repl.launches) == 3

    os.chdir(tmp_path)
    assert chat_mod.run_chat(state, GlobalArgs(continue_=BARE_CONTINUE), ChatArgs()) == 0
    assert repl.launches[-1].session_id == first


def test_continue_miss_exits_1(
    scope: Scope, repl: RecordingRepl, capsys: pytest.CaptureFixture[str]
) -> None:
    state = make_state(scope, FakeModelServer(script_of(text_turn("x"))))
    assert chat_mod.run_chat(state, GlobalArgs(continue_="no such"), ChatArgs()) == 1
    assert "No session found matching 'no such'." in capsys.readouterr().err
    assert repl.launches == []


def test_finite_query_without_a_terminal(
    scope: Scope, tmp_path: Path, capsys: pytest.CaptureFixture[str], repl: RecordingRepl
) -> None:
    os.chdir(tmp_path)
    server = FakeModelServer(script_of(text_turn("the answer")))
    state = make_state(scope, server, tty=False)
    assert chat_mod.run_chat(state, GlobalArgs(), ChatArgs(query="/not a command")) == 0
    out, err = capsys.readouterr()
    assert out == "the answer\n"
    assert err.startswith("session_id: ")
    assert repl.launches == []
    assert server.model_requests[0].body["messages"][-1]["content"] == "/not a command"


def test_query_file_is_read_verbatim(tmp_path: Path) -> None:
    text = "$(rm -rf /) `x` 'quoted' \"double\"\n"
    path = tmp_path / "q.txt"
    path.write_text(text, encoding="utf-8")
    assert chat_mod.read_query(ChatArgs(query_file=str(path))) == text
