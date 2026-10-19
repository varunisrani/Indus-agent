"""P22-07: the per-prompt worker on the fake model (in process)."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from indus.core.prompt import builder as prompt_builder
from indus.foundation.clock import SystemClock
from indus.foundation.scope import Scope
from indus.providers.types import ChatRequest
from indus.research import worker
from indus.research.batch_io import batch_path, read_batch_file
from indus.research.dataset import DatasetRow
from indus.research.worker import (
    BatchJob,
    Conversation,
    ImagePrep,
    SessionAgentRunner,
    WorkerSettings,
    registered_tools,
    run_batch,
    shape_request,
)
from indus.service.session_service import SessionService
from tests.fakes.agent import DictConfig, any_window
from tests.fakes.model import FakeModelServer, fake_route, load_script

PREFILL = (
    {"role": "user", "content": "PREFILL-QUESTION"},
    {"role": "assistant", "content": "PREFILL-ANSWER"},
)


@pytest.fixture
def real(scope: Scope) -> Scope:
    return dataclasses.replace(scope, clock=SystemClock())


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    path = tmp_path / "runs" / "r"
    path.mkdir(parents=True)
    return path


@pytest.fixture
def no_context_files(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Any read of project context files or the persona fails the test."""
    calls: list[str] = []

    async def refuse(*args: Any, **kwargs: Any) -> str:
        calls.append("read")
        raise AssertionError("a batch agent read context files")

    monkeypatch.setattr(prompt_builder, "load_project_context", refuse)
    monkeypatch.setattr(prompt_builder, "load_persona", refuse)
    return calls


def settings_for(run_dir: Path, **values: Any) -> WorkerSettings:
    base: dict[str, Any] = {
        "home": str(run_dir),
        "env": {},
        "run_dir": str(run_dir),
        "run_name": "r",
        "distribution": "terminal_only",
        "registered": registered_tools(),
        "valid_toolsets": ("terminal", "file", "web", "search", "vision", "image_gen", "browser"),
    }
    base.update(values)
    return WorkerSettings(**base)


def runner_for(
    scope: Scope, settings: WorkerSettings, server: FakeModelServer
) -> SessionAgentRunner:
    return SessionAgentRunner(
        scope,
        settings,
        route=fake_route(server),
        client_factory=lambda _route: server.client(),
        list_models=any_window,
    )


def job(*prompts: str, batch: int = 0, first: int = 0) -> BatchJob:
    items = tuple((first + i, DatasetRow(first + i, p)) for i, p in enumerate(prompts))
    return BatchJob(batch, items)


def rows_of(run_dir: Path, batch: int = 0) -> list[dict[str, Any]]:
    return list(read_batch_file(batch_path(run_dir, batch), lambda t: None))


def test_tool_loop_writes_a_durable_row_and_closes_the_agent(
    real: Scope, run_dir: Path, no_context_files: list[str]
) -> None:
    server = FakeModelServer(load_script("research_tool_loop"))
    settings = settings_for(
        run_dir, prefill=PREFILL, ephemeral_system_prompt="EPHEMERAL-RULE: be brief"
    )
    runner = runner_for(real, settings, server)
    outcome = asyncio.run(run_batch(real, settings, job("Is the file there?"), runner=runner))
    assert outcome.completed == [0] and outcome.failed == 0 and outcome.discarded == 0
    assert no_context_files == []
    assert len(runner.agents) == 1 and runner.agents[0].closed

    [row] = rows_of(run_dir)
    assert row["prompt_index"] == 0
    assert row["completed"] is True and row["partial"] is False
    assert row["api_calls"] == 2
    assert row["metadata"]["toolsets"] == ["terminal", "file"]
    assert row["metadata"]["model"] == server.model
    assert row["tool_stats"]["read_file"] == {"count": 1, "success": 0, "failure": 1}
    assert set(row["tool_stats"]) == set(registered_tools())  # columnar (R10.5)
    assert row["reasoning"]["turns_with_reasoning"] == 2
    froms = [t["from"] for t in row["conversations"]]
    assert froms == ["system", "human", "gpt", "tool", "gpt"]
    assert row["conversations"][1]["value"] == "Is the file there?"

    # prefill and the ephemeral prompt reach the request, never the sample
    sent = server.model_requests[0].body["messages"]
    assert sent[0]["role"] == "system"
    assert "EPHEMERAL-RULE" in json.dumps(sent[0])
    assert [m["content"] for m in sent[1:3]] == ["PREFILL-QUESTION", "PREFILL-ANSWER"]
    sample = json.dumps(row["conversations"])
    assert "PREFILL" not in sample and "EPHEMERAL-RULE" not in sample
    # the second request carries the prefill again (request time, every request)
    later = [m.get("content") for m in server.model_requests[1].body["messages"]]
    assert later.count("PREFILL-QUESTION") == 1


def test_no_reasoning_run_leaves_a_tombstone(real: Scope, run_dir: Path) -> None:
    server = FakeModelServer(load_script("research_no_reasoning"))
    settings = settings_for(run_dir)
    runner = runner_for(real, settings, server)
    outcome = asyncio.run(run_batch(real, settings, job("2 + 2?", first=5), runner=runner))
    assert outcome.completed == [5] and outcome.discarded == 1
    assert rows_of(run_dir) == [
        {"prompt_index": 5, "discarded": "no_reasoning", "prompt": "2 + 2?"}
    ]
    assert runner.agents[0].closed


def test_hallucinated_tool_is_counted_under_its_own_name(real: Scope, run_dir: Path) -> None:
    server = FakeModelServer(load_script("research_hallucinated_tool"))
    settings = settings_for(run_dir)
    runner = runner_for(real, settings, server)
    asyncio.run(run_batch(real, settings, job("go to mars"), runner=runner))
    [row] = rows_of(run_dir)
    assert row["tool_stats"]["teleport_user"]["count"] == 1
    assert "teleport_user" not in registered_tools()


def test_agent_is_closed_when_the_run_raises(
    real: Scope, run_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = FakeModelServer(load_script("research_tool_loop"))
    settings = settings_for(run_dir)
    runner = runner_for(real, settings, server)

    async def boom(self: SessionService, session_id: str, turn_id: str | None = None) -> Any:
        raise RuntimeError("wait exploded")

    monkeypatch.setattr(SessionService, "wait", boom)
    outcome = asyncio.run(run_batch(real, settings, job("q"), runner=runner))
    assert outcome.failed == 1 and outcome.completed == []
    assert runner.agents and all(a.closed for a in runner.agents)
    assert not batch_path(run_dir, 0).exists()  # nothing written: retried next run


def test_provider_failure_writes_nothing(real: Scope, run_dir: Path) -> None:
    from tests.fakes.model import parse_script

    script = parse_script({"turns": [{"steps": [{"fail": {"status": 400, "body": "bad"}}]}]})
    server = FakeModelServer(script)
    settings = settings_for(run_dir)
    lines: list[str] = []
    outcome = asyncio.run(
        run_batch(
            real, settings, job("q"), runner=runner_for(real, settings, server), out=lines.append
        )
    )
    assert outcome.failed == 1 and outcome.completed == []
    assert "provider_failed" in lines[0]
    assert not batch_path(run_dir, 0).exists()


class FakeCommand:
    def __init__(self, *answers: tuple[int, str]) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[list[str], float]] = []

    async def __call__(self, argv: Sequence[str], timeout: float) -> tuple[int, str]:
        self.calls.append((list(argv), timeout))
        return self.answers.pop(0)


def test_failed_pull_costs_zero_model_calls(real: Scope, run_dir: Path) -> None:
    docker = real.with_config(DictConfig({"terminal": {"backend": "docker"}}))
    server = FakeModelServer(load_script("research_tool_loop"))
    settings = settings_for(run_dir)
    command = FakeCommand((1, "no such image"), (1, "pull denied: " + "x" * 900))
    lines: list[str] = []
    outcome = asyncio.run(
        run_batch(
            docker,
            settings,
            BatchJob(0, ((0, DatasetRow(0, "build", image="private/img:1")),)),
            runner=runner_for(docker, settings, server),
            images=ImagePrep(docker, command=command),
            out=lines.append,
        )
    )
    assert outcome.failed == 1
    assert server.model_requests == []
    assert [c[0][:2] for c in command.calls] == [["docker", "image"], ["docker", "pull"]]
    assert [c[1] for c in command.calls] == [10.0, 600.0]
    assert "pull denied" in lines[0] and len(lines[0]) < 700


def test_image_rows_register_a_per_task_override(real: Scope) -> None:
    from indus.backends.task_overrides import TASK_OVERRIDES, TaskOverride

    docker = real.with_config(DictConfig({"terminal": {"backend": "docker"}}))
    present = FakeCommand((0, ""))
    try:
        prep = ImagePrep(docker, command=present)
        assert asyncio.run(prep(DatasetRow(0, "p", image="a:1"), "task_0")) is None
        assert len(present.calls) == 1  # found locally: no pull
        assert TASK_OVERRIDES.get(docker, "task_0") == TaskOverride(image="a:1")

        local = FakeCommand()
        prep = ImagePrep(real, command=local)  # local backend: no docker check at all
        row = DatasetRow(1, "p", image="a:1", cwd="/work/w1")
        assert asyncio.run(prep(row, "task_1")) is None
        assert TASK_OVERRIDES.get(real, "task_1") == TaskOverride(image="a:1", cwd="/work/w1")
        assert asyncio.run(prep(DatasetRow(2, "p"), "task_2")) is None
        assert TASK_OVERRIDES.get(real, "task_2") is None
        assert local.calls == []

        refused = ImagePrep(real, command=local, register=worker.refuse_images)
        row = DatasetRow(3, "p", image="a:1")
        assert asyncio.run(refused(row, "task_3")) == worker.NO_IMAGE_OVERRIDES
    finally:
        TASK_OVERRIDES.reset()

    seen: list[str] = []

    async def accept(task_id: str, row: DatasetRow) -> str | None:
        seen.append(task_id)
        return None

    prep = ImagePrep(real, register=accept)
    assert asyncio.run(prep(DatasetRow(3, "p", image="a:1"), "task_3")) is None
    assert seen == ["task_3"]


class Scripted:
    """A runner that returns canned conversations and records what it was asked."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, list[str], str]] = []

    async def __call__(self, prompt: str, toolsets: Sequence[str], task_id: str) -> Conversation:
        self.asked.append((prompt, list(toolsets), task_id))
        messages = [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": "ok", "reasoning": "r"},
        ]
        return Conversation(messages, [], "completed", 1, "m")


def test_an_image_row_runs_and_releases_its_own_task_environment(
    real: Scope, run_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from indus.backends.registry import EnvKey
    from indus.backends.task_overrides import TASK_OVERRIDES
    from indus.tools.builtin import terminal

    server = FakeModelServer(load_script("research_tool_loop"))
    settings = settings_for(run_dir)
    environments = terminal.runtime().environments
    released: list[EnvKey] = []
    original = environments.release

    async def spy(scope: Scope, **ids: Any) -> bool:
        released.append(environments.key_for(scope, **ids))
        return await original(scope, **ids)

    monkeypatch.setattr(environments, "release", spy)
    items = ((0, DatasetRow(0, "Is the file there?", image="img:1", cwd=str(run_dir))),)
    try:
        outcome = asyncio.run(
            run_batch(real, settings, BatchJob(0, items), runner=runner_for(real, settings, server))
        )
    finally:
        TASK_OVERRIDES.reset()
    assert outcome.completed == [0]
    assert [(k.kind, k.ident) for k in released] == [("task", "task_0")]
    assert environments.key_for(real, task_id="task_0").kind == "session"  # cleared afterwards


def test_skips_known_indices_and_seeds_toolsets_per_prompt(run_dir: Path, real: Scope) -> None:
    settings = settings_for(run_dir, distribution="balanced", seed=3)
    first, second = Scripted(), Scripted()
    items = tuple((i, DatasetRow(i, f"p{i}")) for i in range(6))
    asyncio.run(run_batch(real, settings, BatchJob(0, items, frozenset({1})), runner=first))
    asyncio.run(run_batch(real, settings, BatchJob(1, tuple(reversed(items))), runner=second))
    assert [a[0] for a in first.asked] == ["p0", "p2", "p3", "p4", "p5"]
    assert first.asked[0][2] == "task_0"
    by_prompt = {a[0]: a[1] for a in second.asked}
    assert all(by_prompt[p] == ts for p, ts, _ in first.asked)


def test_stops_between_prompts_when_told(run_dir: Path, real: Scope) -> None:
    settings = settings_for(run_dir)
    runner = Scripted()
    alive = iter([True, False])
    items = tuple((i, DatasetRow(i, f"p{i}")) for i in range(3))
    outcome = asyncio.run(
        run_batch(real, settings, BatchJob(0, items), runner=runner, keep_going=lambda: next(alive))
    )
    assert outcome.stopped and outcome.completed == [0]


def test_shape_request_puts_prefill_after_the_system_message_and_merges_routing() -> None:
    request = ChatRequest(
        model="m",
        messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
        extra_body={"provider": {"sort": "price"}, "other": 1},
    )
    shaped = shape_request(request, PREFILL, {"only": ["a"], "sort": "latency"})
    assert [m["content"] for m in shaped.messages] == [
        "s",
        "PREFILL-QUESTION",
        "PREFILL-ANSWER",
        "u",
    ]
    assert shaped.extra_body == {"provider": {"sort": "latency", "only": ["a"]}, "other": 1}
    assert [m["content"] for m in request.messages] == ["s", "u"]  # the original is untouched
