"""The single-tool coding runner behind ``indus research swe`` (P22-14).

A minimal loop for data generation: the model gets one tool, ``terminal``, and a task. Each task
gets its own environment from the backend registry (``local``, ``docker`` or ``modal``), keyed by
a per-task id (``swe_<n>``) with per-task overrides ``{backend, image, cwd}``: locally the cwd is
a fresh temporary folder, removed afterwards; in a container it is ``/tmp``. Then, up to
``max_iterations`` times: one Chat Completions request through the provider layer (300 s deadline)
with our system prompt and only the ``terminal`` tool. An answer without tool calls completes the
task. Otherwise every call runs its ``command`` (with its own ``timeout``, else
``command_timeout``); a call without a command, or naming another tool, gets an error result and
runs nothing. Output containing ``SENTINEL`` completes the task once the batch of calls is done.
A failed model request ends the loop, not completed. The environment is always released.

Every task becomes one line ``{conversations, completed, api_calls, metadata {model, env,
timestamp}}`` in the output file; ``conversations`` comes from the shared converter, so gpt turns
start with a think block exactly as in batch rows (R10.7). Lines are appended, never truncated,
with the saver's locked append, after the P05 redactor. A task that raised leaves ``{conversations:
[], completed: false, api_calls: 0, error, metadata}``.

Per-task overrides: each task's ``{backend, image, cwd}`` is registered in P21-14's
``backends.task_overrides.TASK_OVERRIDES`` under the task id before its first command; the
environment registry resolves it on every call (its own ``task:<id>`` key, the image mapped per
backend, a pinned Docker image kept) and the override is cleared when the task's environment is
released.
"""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import Any, Protocol, cast

from indus.backends.task_overrides import TASK_OVERRIDES, TaskOverride
from indus.foundation.scope import Scope
from indus.research.saver import append_locked, redact_turns
from indus.research.trajectory import convert
from indus.safety.redaction import redact_output

__all__ = [
    "DEFAULT_OUTPUT",
    "ENVIRONMENTS",
    "SENTINEL",
    "TERMINAL_TOOL",
    "PromptError",
    "RegistryEnvironments",
    "SweRunner",
    "SweSettings",
    "load_prompts",
    "provider_chat",
]

log = logging.getLogger(__name__)

SENTINEL = "INDUS_SWE_TASK_COMPLETE"
ENVIRONMENTS = ("local", "docker", "modal")
DEFAULT_OUTPUT = "swe-trajectories.jsonl"
REQUEST_TIMEOUT_S = 300.0
CONTAINER_CWD = "/tmp"  # inside the container, not on the host
TASK = "swe"

TERMINAL_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "terminal",
        "description": (
            "Run one bash command in this task's own sandbox and get its output and exit code. "
            "Files you create stay there for the rest of the task. Use non-interactive commands "
            "only (no editors or pagers), install what you need with pip or apt-get, and pipe "
            f"long output through head or tail. When the task is done, run: echo {SENTINEL} "
            "followed by a one-line summary."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The bash command to run."},
                "timeout": {
                    "type": "integer",
                    "description": "Seconds the command may take (default 60).",
                },
            },
            "required": ["command"],
        },
    },
}

SYSTEM_PROMPT = (
    "You solve software tasks by running bash commands with the terminal tool, one step at a "
    "time. Look before you change anything, keep commands short and non-interactive, and check "
    f"your work. When the task is finished, run `echo {SENTINEL} <one-line summary>`; that ends "
    "the task."
)


class PromptError(ValueError):
    """The prompts file cannot be used (CLI exit 2)."""


@dataclass(frozen=True)
class SweSettings:
    env: str = "local"
    image: str | None = None
    cwd: str | None = None
    model: str | None = None
    provider: str | None = None
    base_url: str | None = None
    max_iterations: int = 15
    command_timeout: float = 60.0


def load_prompts(path: Path) -> list[str]:
    """One task per non-blank line: JSON ``{"prompt": …}`` or ``{"task": …}``, or the raw text."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise PromptError(f"cannot read {path}: {exc.strerror or exc}") from None
    tasks: list[str] = []
    for line in lines:
        text = line.strip()
        if not text:
            continue
        try:
            value: Any = json.loads(text)
        except ValueError:
            tasks.append(text)
            continue
        if isinstance(value, dict):
            found = cast(dict[str, Any], value)
            picked = found.get("prompt") or found.get("task")
            if isinstance(picked, str) and picked.strip():
                tasks.append(picked)
            continue
        tasks.append(text)
    return tasks


# -- environments ----------------------------------------------------------------------------------


class TaskEnvironment(Protocol):
    async def run(self, command: str, timeout: float) -> dict[str, Any]: ...

    async def close(self) -> None: ...


class Environments(Protocol):
    async def open(self, task_id: str) -> TaskEnvironment: ...

    async def aclose(self) -> None: ...


def _result(output: str, exit_code: int, error: str | None) -> dict[str, Any]:
    return {"output": output, "exit_code": exit_code, "error": error}


@dataclass
class _RegistryTask:
    owner: RegistryEnvironments
    task_id: str
    temp: Path | None

    async def run(self, command: str, timeout: float) -> dict[str, Any]:
        from indus.backends.registry import BackendStatus

        try:
            # No call overrides: the registry resolves the task's registered override (P21-14).
            done = await self.owner.registry.run(
                command, scope=self.owner.scope, task_id=self.task_id, timeout=timeout
            )
        except Exception as exc:
            return _result("", -1, f"{type(exc).__name__}: {exc}")
        if isinstance(done, BackendStatus):
            return _result("", -1, done.error)
        return _result(done.output, done.returncode, None)

    async def close(self) -> None:
        owner = self.owner
        try:
            await owner.registry.release(owner.scope, task_id=self.task_id)
        except Exception:
            log.warning("releasing the environment of %s failed", self.task_id, exc_info=True)
        finally:
            TASK_OVERRIDES.clear(owner.scope, self.task_id)
            if self.temp is not None:
                shutil.rmtree(self.temp, ignore_errors=True)


@dataclass
class RegistryEnvironments:
    """One registry environment per task (see the module notes)."""

    scope: Scope
    settings: SweSettings
    registry: Any = None
    make_temp: Callable[[], str] = lambda: tempfile.mkdtemp(prefix="indus-swe-")

    def __post_init__(self) -> None:
        if self.registry is None:
            from indus.backends.registry import EnvironmentRegistry

            self.registry = EnvironmentRegistry(clock=self.scope.clock)

    async def open(self, task_id: str) -> TaskEnvironment:
        s = self.settings
        temp: Path | None = None
        if s.cwd:
            cwd = s.cwd
        elif s.env == "local":
            temp = Path(self.make_temp())
            cwd = str(temp)
        else:
            cwd = CONTAINER_CWD
        override = TaskOverride(image=s.image or None, backend=s.env, cwd=cwd)
        TASK_OVERRIDES.set(self.scope, task_id, override)
        return _RegistryTask(self, task_id, temp)

    async def aclose(self) -> None:
        # Our own registry, not the host's: no process-wide foreground fence.
        await self.registry.shutdown(fence=False)


# -- the model -------------------------------------------------------------------------------------

type ChatFn = Callable[[list[dict[str, Any]], list[dict[str, Any]]], Coroutine[Any, Any, Any]]


def provider_chat(scope: Scope, settings: SweSettings, *, client: Any = None) -> tuple[ChatFn, str]:
    """A Chat Completions caller over the profile's provider layer, and the model it uses."""
    from indus.providers.auxiliary import AuxClient
    from indus.providers.route import resolve

    route = resolve(
        scope, model=settings.model, provider=settings.provider, base_url=settings.base_url
    )
    aux = client if client is not None else AuxClient(scope, route)

    async def chat(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Any:
        result = await aux.call_route(
            TASK, route, messages, tools=tools, timeout_s=REQUEST_TIMEOUT_S
        )
        return result.response

    return chat, route.model


# -- the loop --------------------------------------------------------------------------------------


def _arguments(raw: object) -> dict[str, Any]:
    if isinstance(raw, dict):
        return cast(dict[str, Any], raw)
    if isinstance(raw, str) and raw.strip():
        try:
            value = json.loads(raw)
        except ValueError:
            return {}
        return cast(dict[str, Any], value) if isinstance(value, dict) else {}
    return {}


def _timeout(value: object, default: float) -> float:
    if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
        return float(value)
    return default


class SweRunner:
    """See the module notes. ``chat`` and ``environments`` are seams (fakes in tests)."""

    def __init__(
        self,
        scope: Scope,
        settings: SweSettings,
        *,
        chat: ChatFn,
        environments: Environments,
        model: str = "",
        out: Callable[[str], None] = lambda line: None,
    ) -> None:
        self.scope = scope
        self.settings = settings
        self.chat = chat
        self.environments = environments
        self.model = model or settings.model or ""
        self.out = out

    def _metadata(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "env": self.settings.env,
            "timestamp": self.scope.clock.now().astimezone(UTC).isoformat(),
        }

    async def _call(self, env: TaskEnvironment, call: Any) -> dict[str, Any]:
        name = str(getattr(call, "name", "") or "")
        if name != "terminal":
            return _result("", -1, f"unknown tool {name!r}: only terminal is available")
        args = _arguments(getattr(call, "arguments", None))
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            return _result("", -1, "no command was given, so nothing ran")
        return await env.run(command, _timeout(args.get("timeout"), self.settings.command_timeout))

    async def run_task(self, task: str, number: int = 0) -> dict[str, Any]:
        env = await self.environments.open(f"swe_{number}")
        messages: list[dict[str, Any]] = [{"role": "user", "content": task}]
        api_calls = 0
        completed = False
        try:
            while api_calls < max(1, self.settings.max_iterations):
                api_calls += 1
                request = [{"role": "system", "content": SYSTEM_PROMPT}, *messages]
                try:
                    response = await self.chat(request, [TERMINAL_TOOL])
                except Exception as exc:
                    self.out(
                        f"task {number}: the model request failed ({type(exc).__name__}: {exc})"
                    )
                    break
                text = response.content or ""
                reasoning = getattr(response, "reasoning", None)
                calls = list(response.tool_calls or ())
                message: dict[str, Any] = {"role": "assistant", "content": text}
                if reasoning:
                    message["reasoning"] = reasoning
                if not calls:
                    messages.append(message)
                    completed = True
                    break
                message["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": call.arguments},
                    }
                    for call in calls
                ]
                messages.append(message)
                finished = False
                for call in calls:
                    result = await self._call(env, call)
                    finished = finished or SENTINEL in str(result.get("output") or "")
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "name": call.name,
                            "content": json.dumps({"content": result}, ensure_ascii=False),
                        }
                    )
                if finished:
                    completed = True
                    break
        finally:
            await env.close()
        turns = convert(messages, prompt=task, tools=[TERMINAL_TOOL]).turns
        return {
            "conversations": redact_turns(turns, self.scope),
            "completed": completed,
            "api_calls": api_calls,
            "metadata": self._metadata(),
        }

    async def run_all(self, tasks: Sequence[str], output: Path) -> list[dict[str, Any]]:
        """Every task in order, each line appended to ``output`` as soon as it is done."""
        results: list[dict[str, Any]] = []
        try:
            for number, task in enumerate(tasks):
                try:
                    record = await self.run_task(task, number)
                except Exception as exc:
                    log.warning("swe task %s failed", number, exc_info=True)
                    # Exception text can quote a URL or header: masked like the turns are.
                    error = redact_output(
                        f"{type(exc).__name__}: {exc}", force=True, scope=self.scope
                    )
                    record = {
                        "conversations": [],
                        "completed": False,
                        "api_calls": 0,
                        "error": error,
                        "metadata": self._metadata(),
                    }
                append_locked(output, record)
                results.append(record)
                state = "completed" if record["completed"] else "not completed"
                self.out(f"task {number + 1}/{len(tasks)}: {state} ({record['api_calls']} calls)")
        finally:
            await self.environments.aclose()
        return results
