"""P22-14: the single-tool SWE runner (fake model, fake and local environments)."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from indus.backends.contract import BackendFlags, ExecResult
from indus.backends.registry import BackendEntry, CreateRequest, EnvironmentRegistry
from indus.backends.task_overrides import TASK_OVERRIDES, TaskOverride
from indus.foundation.clock import SystemClock
from indus.foundation.scope import Scope
from indus.providers.types import NormalizedResponse, ToolCall
from indus.research.swe_runner import (
    SENTINEL,
    TERMINAL_TOOL,
    PromptError,
    RegistryEnvironments,
    SweRunner,
    SweSettings,
    load_prompts,
)
from indus.research.trajectory import convert

ROOT = Path(__file__).resolve().parents[3]
GOLDEN = ROOT / "tests" / "golden" / "research" / "swe" / "sentinel_run.json"


def call(command: Any = None, *, cid: str = "c1", name: str = "terminal", **extra: Any) -> ToolCall:
    args: dict[str, Any] = dict(extra)
    if command is not None:
        args["command"] = command
    return ToolCall(id=cid, name=name, arguments=json.dumps(args))


def answer(text: str = "", *calls: ToolCall, reasoning: str | None = None) -> NormalizedResponse:
    return NormalizedResponse(
        content=text,
        tool_calls=tuple(calls),
        finish_reason="tool_calls" if calls else "stop",
        reasoning=reasoning,
    )


@dataclass
class FakeChat:
    replies: list[NormalizedResponse | Exception]
    requests: list[list[dict[str, Any]]] = field(default_factory=list[list[dict[str, Any]]])
    tools: list[list[dict[str, Any]]] = field(default_factory=list[list[dict[str, Any]]])

    async def __call__(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Any:
        self.requests.append([dict(m) for m in messages])
        self.tools.append(tools)
        step = self.replies.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


@dataclass
class FakeTask:
    outputs: dict[str, str]
    ran: list[tuple[str, float]] = field(default_factory=list[tuple[str, float]])
    closed: bool = False

    async def run(self, command: str, timeout: float) -> dict[str, Any]:
        self.ran.append((command, timeout))
        return {"output": self.outputs.get(command, ""), "exit_code": 0, "error": None}

    async def close(self) -> None:
        self.closed = True


@dataclass
class FakeEnvironments:
    outputs: dict[str, str] = field(default_factory=dict[str, str])
    opened: list[str] = field(default_factory=list[str])
    tasks: list[FakeTask] = field(default_factory=list[FakeTask])
    closed: bool = False
    fail_open: bool = False

    async def open(self, task_id: str) -> FakeTask:
        if self.fail_open:
            raise RuntimeError("no sandbox for you")
        self.opened.append(task_id)
        task = FakeTask(self.outputs)
        self.tasks.append(task)
        return task

    async def aclose(self) -> None:
        self.closed = True


def runner(scope: Scope, chat: FakeChat, envs: FakeEnvironments, **settings: Any) -> SweRunner:
    return SweRunner(scope, SweSettings(**settings), chat=chat, environments=envs, model="m-1")


def sentinel_script() -> list[NormalizedResponse | Exception]:
    return [
        answer("Let me look.", call("ls", cid="a1"), reasoning="Start by listing."),
        answer("", call(f"echo {SENTINEL} made it", cid="a2", timeout=5)),
        answer("never asked"),
    ]


def test_sentinel_stops_the_loop(scope: Scope) -> None:
    chat = FakeChat(sentinel_script())
    envs = FakeEnvironments({"ls": "a.py\n", f"echo {SENTINEL} made it": f"{SENTINEL} made it\n"})
    record = asyncio.run(runner(scope, chat, envs).run_task("make it", 3))
    assert record["completed"] is True and record["api_calls"] == 2
    assert len(chat.requests) == 2 and len(chat.replies) == 1
    assert envs.opened == ["swe_3"] and envs.tasks[0].closed
    assert envs.tasks[0].ran == [("ls", 60.0), (f"echo {SENTINEL} made it", 5.0)]
    assert chat.tools[0] == [TERMINAL_TOOL]
    assert chat.requests[0][0]["role"] == "system" and SENTINEL in chat.requests[0][0]["content"]
    assert record["metadata"]["model"] == "m-1" and record["metadata"]["env"] == "local"


def test_output_is_the_shared_converters_format(scope: Scope) -> None:
    chat = FakeChat(sentinel_script())
    envs = FakeEnvironments({"ls": "a.py\n", f"echo {SENTINEL} made it": f"{SENTINEL} made it\n"})
    record = asyncio.run(runner(scope, chat, envs).run_task("make it"))
    second_request = chat.requests[1][1:]  # the stored conversation after the first round
    final = [
        *second_request,
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "a2",
                    "type": "function",
                    "function": {"name": "terminal", "arguments": '{"timeout": 5, "command": '
                                 f'"echo {SENTINEL} made it"}}'},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "a2",
            "content": json.dumps(
                {"content": {"output": f"{SENTINEL} made it\n", "exit_code": 0, "error": None}}
            ),
        },
    ]  # fmt: skip
    expected = convert(final, prompt="make it", tools=[TERMINAL_TOOL]).turns
    assert record["conversations"] == expected
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert record["conversations"] == golden["turns"]
    for turn in record["conversations"]:
        if turn["from"] == "gpt":
            assert turn["value"].startswith("<think>\n")


def test_missing_command_and_other_tools_run_nothing(scope: Scope) -> None:
    chat = FakeChat(
        [
            answer("", call(None, cid="x1"), call("ls", cid="x2", name="browser")),
            answer("All done."),
        ]
    )
    envs = FakeEnvironments()
    record = asyncio.run(runner(scope, chat, envs).run_task("t"))
    assert envs.tasks[0].ran == []
    assert record["completed"] is True and record["api_calls"] == 2
    tool_turn = record["conversations"][3]["value"]
    assert "no command was given" in tool_turn and "unknown tool" in tool_turn


def test_max_iterations_and_model_errors_end_not_completed(scope: Scope) -> None:
    chat = FakeChat([answer("", call("true", cid=f"k{i}")) for i in range(3)])
    record = asyncio.run(runner(scope, chat, FakeEnvironments(), max_iterations=3).run_task("t"))
    assert record["completed"] is False and record["api_calls"] == 3
    chat = FakeChat([answer("", call("true")), RuntimeError("HTTP 500")])
    envs = FakeEnvironments()
    record = asyncio.run(runner(scope, chat, envs).run_task("t"))
    assert record["completed"] is False and record["api_calls"] == 2 and envs.tasks[0].closed


def test_file_is_appended_and_a_failed_task_leaves_an_error_line(
    scope: Scope, tmp_path: Path
) -> None:
    output = tmp_path / "out.jsonl"
    output.write_text('{"earlier": true}\n', encoding="utf-8")
    chat = FakeChat([answer("one"), answer("two")])
    envs = FakeEnvironments()
    asyncio.run(runner(scope, chat, envs).run_all(["a", "b"], output))
    broken = FakeEnvironments(fail_open=True)
    asyncio.run(runner(scope, FakeChat([]), broken).run_all(["c"], output))
    lines = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert lines[0] == {"earlier": True}
    assert [line["completed"] for line in lines[1:3]] == [True, True]
    assert lines[3]["conversations"] == [] and lines[3]["completed"] is False
    assert "no sandbox for you" in lines[3]["error"] and lines[3]["api_calls"] == 0
    assert envs.closed and broken.closed


def test_a_failed_tasks_error_line_is_masked(scope: Scope, tmp_path: Path) -> None:
    secret = "ghp_" + "Y8" * 18  # built at run time (no credential-shaped literal)

    class Leaky(FakeEnvironments):
        async def open(self, task_id: str) -> FakeTask:
            raise RuntimeError(f"auth failed for token {secret}")

    output = tmp_path / "out.jsonl"
    asyncio.run(runner(scope, FakeChat([]), Leaky()).run_all(["c"], output))
    text = output.read_text(encoding="utf-8")
    assert secret not in text and "auth failed" in json.loads(text)["error"]


def test_secrets_in_command_output_are_masked(scope: Scope) -> None:
    secret = "ghp_" + "Z9" * 18
    chat = FakeChat([answer("", call("env")), answer("ok")])
    envs = FakeEnvironments({"env": f"TOKEN={secret}\n"})
    record = asyncio.run(runner(scope, chat, envs).run_task("t"))
    assert secret not in json.dumps(record)


def test_load_prompts(tmp_path: Path) -> None:
    path = tmp_path / "p.jsonl"
    path.write_text(
        '{"prompt": "first"}\n\n{"task": "second"}\nplain third\n{"other": 1}\n[1, 2]\n',
        encoding="utf-8",
    )
    assert load_prompts(path) == ["first", "second", "plain third", "[1, 2]"]
    with pytest.raises(PromptError):
        load_prompts(tmp_path / "missing.jsonl")


# -- registry environments -------------------------------------------------------------------------


@dataclass
class ScriptedEnvironment:
    request: CreateRequest
    cleaned: list[bool] = field(default_factory=list[bool])

    @property
    def name(self) -> str:
        return "fakebox"

    @property
    def flags(self) -> BackendFlags:
        return BackendFlags(isolated=True, container_paths=True)

    @property
    def cwd(self) -> str:
        return str(self.request.overrides.get("cwd"))

    async def execute(self, command: str, **_: Any) -> ExecResult:
        return ExecResult(output=f"ran {command} in {self.cwd}", returncode=0)

    async def cleanup(self) -> None:
        self.cleaned.append(True)

    def describe(self) -> str:
        return "fakebox"


def test_registry_environments_use_per_task_overrides_and_release(scope: Scope) -> None:
    """The task's overrides go through P21-14's registry: registered before the first command,
    resolved by the environment registry (own task key, image mapped per backend), cleared on
    release."""
    made: list[ScriptedEnvironment] = []
    seen: list[TaskOverride | None] = []

    async def factory(request: CreateRequest) -> ScriptedEnvironment:
        seen.append(TASK_OVERRIDES.get(scope, "swe_7"))
        env = ScriptedEnvironment(request)
        made.append(env)
        return env

    registry = EnvironmentRegistry(
        [BackendEntry("docker", factory, BackendFlags(isolated=True))], clock=SystemClock()
    )
    envs = RegistryEnvironments(
        scope, SweSettings(env="docker", image="python:3.13-slim"), registry
    )

    async def go() -> dict[str, Any]:
        task = await envs.open("swe_7")
        result = await task.run("pwd", 9)
        await task.close()
        return result

    try:
        result = asyncio.run(go())
    finally:
        TASK_OVERRIDES.clear(scope, "swe_7")
    assert result == {"output": "ran pwd in /tmp", "exit_code": 0, "error": None}
    [env] = made
    assert seen == [TaskOverride(image="python:3.13-slim", backend="docker", cwd="/tmp")]
    assert env.request.task_id == "swe_7" and env.request.key.kind == "task"
    assert env.request.overrides["task"] == "swe_7" and env.request.overrides["cwd"] == "/tmp"
    # Docker reads its image from config: the override reaches the factory as a config overlay.
    assert env.request.scope.config is not None
    assert env.request.scope.config.get("terminal.docker_image") == "python:3.13-slim"
    assert env.cleaned == [True] and registry.live() == {}
    assert TASK_OVERRIDES.get(scope, "swe_7") is None


def test_registry_environments_clear_the_override_when_release_fails(scope: Scope) -> None:
    class Broken(EnvironmentRegistry):
        async def release(self, *args: Any, **kwargs: Any) -> bool:
            raise RuntimeError("release went wrong")

    envs = RegistryEnvironments(scope, SweSettings(env="modal"), Broken([], clock=SystemClock()))

    async def go() -> None:
        task = await envs.open("swe_9")
        assert TASK_OVERRIDES.get(scope, "swe_9") is not None
        await task.close()

    asyncio.run(go())
    assert TASK_OVERRIDES.get(scope, "swe_9") is None


def test_unknown_backend_is_an_error_result(scope: Scope) -> None:
    registry = EnvironmentRegistry([], clock=SystemClock())
    envs = RegistryEnvironments(scope, SweSettings(env="modal"), registry)

    async def go() -> dict[str, Any]:
        task = await envs.open("swe_0")
        try:
            return await task.run("ls", 5)
        finally:
            await task.close()

    result = asyncio.run(go())
    assert result["exit_code"] == -1 and "modal" in result["error"]


def test_local_run_completes_on_the_sentinel(scope: Scope, tmp_path: Path) -> None:
    """ACC-O5-1: the real local backend, a fresh temp folder per task, removed afterwards."""
    real = dataclasses.replace(scope, clock=SystemClock())
    temps: list[Path] = []

    def make_temp() -> str:
        path = tmp_path / f"task-{len(temps)}"
        path.mkdir()
        temps.append(path)
        return str(path)

    chat = FakeChat(
        [
            answer("", call("printf 'hi' > made.txt && cat made.txt", cid="l1")),
            answer("", call(f"echo {SENTINEL} wrote made.txt", cid="l2")),
        ]
    )
    envs = RegistryEnvironments(real, SweSettings(), make_temp=make_temp)
    output = tmp_path / "swe.jsonl"
    swe = SweRunner(real, SweSettings(), chat=chat, environments=envs, model="m")
    [record] = asyncio.run(swe.run_all(["write a file"], output))
    assert record["completed"] is True and record["api_calls"] == 2
    assert '"output":"hi"' in record["conversations"][3]["value"]
    assert temps and not temps[0].exists()
    assert json.loads(output.read_text(encoding="utf-8"))["completed"] is True


# -- the provider caller and the CLI verb ----------------------------------------------------------


def test_provider_chat_sends_one_request_with_the_terminal_tool(scope: Scope) -> None:
    from types import SimpleNamespace

    from indus.research.swe_runner import provider_chat

    seen: list[dict[str, Any]] = []

    class Aux:
        async def call_route(self, task: str, route: Any, messages: Any, **options: Any) -> Any:
            seen.append({"task": task, "route": route, "messages": messages, **options})
            return SimpleNamespace(response=answer("done"))

    settings = SweSettings(model="swe-model", base_url="http://127.0.0.1:9/v1")
    chat, model = provider_chat(scope, settings, client=Aux())
    response = asyncio.run(chat([{"role": "user", "content": "x"}], [TERMINAL_TOOL]))
    assert model == "swe-model" and response.content == "done"
    [call_seen] = seen
    assert call_seen["task"] == "swe" and call_seen["tools"] == [TERMINAL_TOOL]
    assert call_seen["timeout_s"] == 300.0 and call_seen["route"].model == "swe-model"


def test_cli_swe_runs_tasks_and_appends(
    capsys: pytest.CaptureFixture[str],
    scope: Scope,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from indus.cli.commands import research as research_cmd
    from tests.unit.cli.commands.cmdrun import run_cli

    chat = FakeChat([answer("first done"), answer("second done")])
    envs = FakeEnvironments()
    monkeypatch.setattr(research_cmd, "swe_chat", lambda scope, settings: (chat, "m-cli"))
    monkeypatch.setattr(research_cmd, "swe_environments", lambda scope, settings: envs)
    prompts = tmp_path / "p.jsonl"
    prompts.write_text('{"prompt": "one"}\ntwo\n', encoding="utf-8")
    output = tmp_path / "o.jsonl"
    ran = run_cli(
        capsys, scope, "research", "swe", "--prompts", str(prompts), "--output", str(output)
    )
    assert ran.code == 0, ran.err
    lines = [json.loads(x) for x in output.read_text(encoding="utf-8").splitlines()]
    assert [line["conversations"][1]["value"] for line in lines] == ["one", "two"]
    assert lines[0]["metadata"]["model"] == "m-cli" and "2 of 2" in ran.out
    assert envs.opened == ["swe_0", "swe_1"]


def test_cli_swe_usage_errors(
    capsys: pytest.CaptureFixture[str], scope: Scope, tmp_path: Path
) -> None:
    from tests.unit.cli.commands.cmdrun import run_cli

    assert run_cli(capsys, scope, "research", "swe").code == 2
    both = run_cli(capsys, scope, "research", "swe", "--task", "x", "--prompts", "p.jsonl")
    assert both.code == 2
    empty = tmp_path / "empty.jsonl"
    empty.write_text("\n", encoding="utf-8")
    assert run_cli(capsys, scope, "research", "swe", "--prompts", str(empty)).code == 2
    missing = run_cli(capsys, scope, "research", "swe", "--prompts", str(tmp_path / "nope"))
    assert missing.code == 2
    bad_env = run_cli(capsys, scope, "research", "swe", "--task", "x", "--env", "vercel")
    assert bad_env.code == 2
