"""P22-10: the interactive trajectory saver (file lock, routing, shared converter, redaction,
``chat --save-trajectories`` wiring)."""

from __future__ import annotations

import json
import multiprocessing
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from indus.backends.wait import FOREGROUND
from indus.cli import chat as chat_mod
from indus.cli.args import ChatArgs, CliState, GlobalArgs, HostOverrides
from indus.foundation.scope import Scope
from indus.research import saver
from indus.research.saver import (
    COMPLETED_FILE,
    FAILED_FILE,
    TrajectorySaver,
    append_locked,
    sample_record,
    target_file,
)
from indus.tools.builtin import terminal
from tests.fakes.agent import DictConfig, any_window
from tests.fakes.model import FakeModelServer, fake_route, script_of, text_turn, tool_batch
from tests.unit.cli.commands.cmdrun import write_settings

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "tests" / "fixtures" / "research" / "trajectories"
GOLDEN = ROOT / "tests" / "golden" / "research" / "trajectories"

APPENDS = 200
PAYLOAD = 24_000  # bigger than one pipe/page write, so unlocked appends could interleave


def _writer(path: str, tag: str) -> None:
    for number in range(APPENDS):
        record = {"writer": tag, "n": number, "pad": tag * PAYLOAD}
        append_locked(Path(path), record)


@pytest.fixture(autouse=True)
def keep_cwd() -> Iterator[None]:
    """A chat run ends like a process: it shuts the terminal runtime down and fences foreground
    commands. Later tests get a fresh runtime, an open fence and the old working directory."""
    here = os.getcwd()
    terminal._shared.clear()  # pyright: ignore[reportPrivateUsage]
    yield
    os.chdir(here)
    terminal._shared.clear()  # pyright: ignore[reportPrivateUsage]
    FOREGROUND.reset_for_tests()


def test_two_processes_append_400_valid_lines(tmp_path: Path) -> None:
    target = tmp_path / COMPLETED_FILE
    context = multiprocessing.get_context("spawn")
    workers = [context.Process(target=_writer, args=(str(target), tag)) for tag in "ab"]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=120)
        assert worker.exitcode == 0
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 * APPENDS
    records = [json.loads(line) for line in lines]
    for tag in "ab":
        mine = [r["n"] for r in records if r["writer"] == tag]
        assert mine == list(range(APPENDS))
        assert all(r["pad"] == tag * PAYLOAD for r in records if r["writer"] == tag)


def test_completed_and_failed_go_to_their_own_files(scope: Scope, tmp_path: Path) -> None:
    assert target_file(tmp_path, True) == tmp_path / "trajectory_samples.jsonl"
    assert target_file(tmp_path, False) == tmp_path / "failed_trajectories.jsonl"
    keeper = TrajectorySaver(scope, tmp_path)
    messages = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    keeper.write(messages, "hi", [], "m-1", True)
    keeper.write(messages, "hi", [], "m-1", False)
    keeper.write(messages, "hi", [], "m-1", False)
    done = (tmp_path / COMPLETED_FILE).read_text(encoding="utf-8").splitlines()
    failed = (tmp_path / FAILED_FILE).read_text(encoding="utf-8").splitlines()
    assert len(done) == 1 and len(failed) == 2
    record = json.loads(done[0])
    assert set(record) == {"conversations", "timestamp", "model", "completed"}
    assert record["model"] == "m-1" and record["completed"] is True
    assert record["timestamp"] == scope.clock.now().isoformat()
    assert json.loads(failed[0])["completed"] is False


@pytest.mark.parametrize("name", sorted(p.stem for p in FIXTURES.glob("*.json")))
def test_saved_sample_is_the_shared_converters_output(
    scope: Scope, tmp_path: Path, name: str
) -> None:
    case = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    want = json.loads((GOLDEN / f"{name}.json").read_text(encoding="utf-8"))["turns"]
    path = TrajectorySaver(scope, tmp_path).write(
        case["messages"], case["prompt"], case["tools"], "m", True
    )
    [line] = path.read_text(encoding="utf-8").splitlines()
    assert json.loads(line)["conversations"] == want


def test_secrets_never_reach_the_disk(scope: Scope, tmp_path: Path) -> None:
    secret = "sk-" + "Q7r" * 12  # built at run time (no credential-shaped literal)
    messages = [
        {"role": "user", "content": f"use the key {secret}"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "terminal", "arguments": json.dumps({"command": "env"})},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": f"OPENAI_API_KEY={secret}\n"},
        {"role": "assistant", "content": f"Found {secret}."},
    ]
    path = TrajectorySaver(scope, tmp_path).write(messages, f"use the key {secret}", [], "m", True)
    text = path.read_text(encoding="utf-8")
    assert secret not in text
    assert len(json.loads(text)["conversations"]) == 5


def test_sample_record_shape() -> None:
    record = sample_record([{"from": "human", "value": "x"}], model="m", completed=False,
                           timestamp="t")  # fmt: skip
    assert record == {
        "conversations": [{"from": "human", "value": "x"}],
        "timestamp": "t",
        "model": "m",
        "completed": False,
    }


def test_a_session_without_a_user_message_saves_nothing(scope: Scope, tmp_path: Path) -> None:
    class Ended:
        session_id = "s"
        completed = True

    class Store:
        rows: dict[str, list[Any]] = {"s": []}

    class Service:
        store = Store()

        def current_id(self, session_id: str) -> str:
            return session_id

        def agent(self, session_id: str) -> Any:
            return None

        def on_turn_end(self, listener: Any) -> Any:
            return lambda: None

    assert TrajectorySaver(scope, tmp_path).save_turn(Service(), Ended()) is None
    assert not (tmp_path / COMPLETED_FILE).exists() and not (tmp_path / FAILED_FILE).exists()


def test_config_switch_and_flag(scope: Scope) -> None:
    assert saver.enabled(scope) is False
    assert saver.enabled(scope, True) is True
    on = scope.with_config(DictConfig({"research": {"save_trajectories": True}}))
    assert saver.enabled(on) is True
    write_settings(scope, "research:\n  save_trajectories: true\n")
    assert saver.enabled(scope) is True  # read from the profile's config file


# -- chat wiring -----------------------------------------------------------------------------------


def state_for(scope: Scope, server: FakeModelServer) -> CliState:
    return CliState(
        scope=scope,
        overrides=HostOverrides(
            route=fake_route(server),
            client_factory=lambda route: server.client(),
            list_models=any_window,
        ),
    )


def run_query(scope: Scope, server: FakeModelServer, *, save: bool) -> int:
    chat = ChatArgs(query="what is here?", oneshot=True, save_trajectories=save)
    return chat_mod.run_chat(state_for(scope, server), GlobalArgs(model="fake-model-1"), chat)


def test_chat_query_with_save_trajectories_writes_a_sample(scope: Scope, tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    (work / "notes.txt").write_text("hello notes\n", encoding="utf-8")
    os.chdir(work)
    server = FakeModelServer(
        script_of(tool_batch(("read_file", {"path": "notes.txt"})), text_turn("It says hello."))
    )
    assert run_query(scope, server, save=True) == 0
    [line] = (work / COMPLETED_FILE).read_text(encoding="utf-8").splitlines()
    record = json.loads(line)
    turns = record["conversations"]
    assert [t["from"] for t in turns] == ["system", "human", "gpt", "tool", "gpt"]
    assert turns[1]["value"] == "what is here?"
    assert '"name":"read_file"' in turns[2]["value"]
    assert "hello notes" in turns[3]["value"]
    assert turns[4]["value"].startswith("<think>") and turns[4]["value"].endswith("It says hello.")
    assert record["completed"] is True and record["model"] == "fake-model-1"
    assert not (work / FAILED_FILE).exists()


def test_chat_without_the_flag_saves_nothing_unless_configured(
    scope: Scope, tmp_path: Path
) -> None:
    os.chdir(tmp_path)
    server = FakeModelServer(script_of(text_turn("one"), text_turn("two")))
    assert run_query(scope, server, save=False) == 0
    assert not (tmp_path / COMPLETED_FILE).exists()
    on = scope.with_config(DictConfig({"research": {"save_trajectories": True}}))
    assert run_query(on, server, save=False) == 0
    assert len((tmp_path / COMPLETED_FILE).read_text(encoding="utf-8").splitlines()) == 1
