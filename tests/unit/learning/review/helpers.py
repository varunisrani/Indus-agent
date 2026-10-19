"""Builders for the review tests: a scripted model caller, a parent session with real tools, and
a scripted auxiliary client for routed reviews."""

from __future__ import annotations

import contextlib
import itertools
import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from indus.core.events import EventSink
from indus.core.loop import run_turn
from indus.core.persist import MemoryStore
from indus.core.prompt.builder import BuildContext
from indus.core.prompt.lifecycle import PromptLifecycle
from indus.core.turn_state import LoopOptions, SessionRuntime, TurnResult, new_turn_state
from indus.foundation.scope import Scope
from indus.learning.memory.tool import memory_wiring
from indus.learning.memory.tool import register_tools as register_memory
from indus.learning.review.fork import ReviewParent
from indus.learning.skills import manage_tool
from indus.learning.skills.manage_tool import register_tools as register_skill_manage
from indus.learning.skills.view_tools import register_tools as register_skill_views
from indus.protocol.events import EventBase
from indus.providers.auxiliary import AuxClient, Candidate
from indus.providers.route import RuntimeRoute
from indus.providers.streaming import CallOutcome, CallResult
from indus.providers.types import ChatRequest, NormalizedResponse, ToolCall, Usage
from indus.tools.definitions import definitions
from indus.tools.registry import ToolRegistry, ToolTraits
from tests.fakes.clock import FakeClock, checkpoint
from tests.fakes.turn import StubBuilder


@contextlib.contextmanager
def skill_write_origins() -> Iterator[list[str]]:
    """The write origin of every successful ``skill_manage`` call while the block runs."""
    seen: list[str] = []

    def listener(event: manage_tool.SkillWriteEvent) -> None:
        seen.append(event.origin)

    manage_tool.SUCCESS_LISTENERS.append(listener)
    try:
        yield seen
    finally:
        manage_tool.SUCCESS_LISTENERS.remove(listener)


MODEL = "fake-model"
MAIN_ROUTE = RuntimeRoute(
    provider="fake", model=MODEL, api_mode="chat_completions", base_url="https://fake.invalid/v1"
)
_ids = itertools.count(1)


def text(
    content: str, *, prompt_tokens: int = 100, completion_tokens: int = 10
) -> NormalizedResponse:
    return NormalizedResponse(
        content=content,
        finish_reason="stop",
        usage=Usage(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
    )


def calls(
    *pairs: tuple[str, Mapping[str, Any]], prompt_tokens: int = 100, completion_tokens: int = 10
) -> NormalizedResponse:
    tool_calls = tuple(
        ToolCall(f"call_{next(_ids)}", name, json.dumps(dict(args))) for name, args in pairs
    )
    return NormalizedResponse(
        content=None,
        tool_calls=tool_calls,
        finish_reason="tool_calls",
        usage=Usage(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
    )


@dataclass
class Stall:
    """A step that waits on the fake clock (until the call is cancelled)."""

    seconds: float = 10_000.0


@dataclass
class ScriptedCaller:
    """A ``ModelCaller`` that answers from ``steps`` and keeps every request."""

    steps: list[Any]
    clock: FakeClock | None = None
    model: str = MODEL
    requests: list[ChatRequest] = field(default_factory=list[ChatRequest])
    default: NormalizedResponse = field(default_factory=lambda: text("Nothing worth keeping."))

    async def call(
        self, request: ChatRequest, *, on_event: Any, on_wait: Any = None
    ) -> CallOutcome:
        self.requests.append(request)
        step = self.steps.pop(0) if self.steps else self.default
        if isinstance(step, Stall):
            assert self.clock is not None
            await self.clock.sleep(step.seconds)
            step = self.default
        if isinstance(step, BaseException):
            raise step
        return CallResult(cast(NormalizedResponse, step))


@dataclass
class ToolLog:
    runs: list[tuple[str, dict[str, Any]]] = field(default_factory=list[tuple[str, dict[str, Any]]])
    broker_seen: list[object] = field(default_factory=list[object])


def _schema(**props: Any) -> dict[str, Any]:
    return {"description": "test tool", "parameters": {"type": "object", "properties": props}}


def register_fakes(registry: ToolRegistry, log: ToolLog) -> None:
    """``terminal``, ``read_file``, ``search_files`` and ``ask_human`` (wants a broker)."""

    def terminal(args: Mapping[str, Any]) -> str:
        log.runs.append(("terminal", dict(args)))
        return "ran"

    def read_file(args: Mapping[str, Any]) -> str:
        log.runs.append(("read_file", dict(args)))
        return "file text"

    def search_files(args: Mapping[str, Any]) -> str:
        log.runs.append(("search_files", dict(args)))
        return "no matches"

    async def ask_human(args: Mapping[str, Any], *, broker: object = None) -> str:
        log.runs.append(("ask_human", dict(args)))
        log.broker_seen.append(broker)
        if broker is None:
            return "denied: nobody can approve this here"
        await cast(Any, broker).ask("approval", {"command": "x"})
        return "approved"

    traits = ToolTraits(read_only=True)
    registry.register("terminal", "terminal", _schema(command={"type": "string"}), terminal)
    registry.register(
        "read_file", "file", _schema(path={"type": "string"}), read_file, traits=traits
    )
    registry.register(
        "search_files", "file", _schema(pattern={"type": "string"}), search_files, traits=traits
    )
    registry.register("ask_human", "extra", _schema(), ask_human)


@dataclass
class FakeBroker:
    asked: list[tuple[str, Mapping[str, Any]]] = field(
        default_factory=list[tuple[str, Mapping[str, Any]]]
    )

    async def ask(self, kind: str, payload: Mapping[str, Any], **_: Any) -> Any:
        self.asked.append((kind, payload))
        return None


@dataclass
class Parent:
    """A parent session: real loop, real memory tool, the real skill tools and a few fakes."""

    scope: Scope
    caller: ScriptedCaller
    runtime: SessionRuntime
    log: ToolLog
    events: list[EventBase]
    sink: EventSink
    turns: int = 0

    async def turn(self, message: str, *, broker: Any = None) -> TurnResult:
        self.turns += 1
        state = new_turn_state(
            self.runtime,
            message=message,
            turn_id=f"turn-{self.turns}",
            generation=self.turns,
            sink=self.sink.emit,
            next_seq=self.sink.next_seq,
            broker=broker,
        )
        return await run_turn(state)

    def review_parent(self, *, caller: Any = None, aux: Any = None) -> ReviewParent:
        return ReviewParent(
            self.runtime,
            caller if caller is not None else self.caller,
            aux=cast(AuxClient | None, aux),
            sink=self.sink,
        )


def make_parent(
    scope: Scope,
    caller: ScriptedCaller,
    *,
    tools: Sequence[str] = (
        "terminal",
        "read_file",
        "search_files",
        "ask_human",
        "memory",
        "skills_list",
        "skill_view",
        "skill_manage",
    ),
    options: LoopOptions | None = None,
    run_context: str | None = "interactive",
) -> Parent:
    registry = ToolRegistry(clock=scope.clock)
    log = ToolLog()
    register_fakes(registry, log)
    register_memory(registry)
    register_skill_views(registry)
    register_skill_manage(registry)
    wiring = memory_wiring(scope)
    inline = {"memory": wiring.execute} if "memory" in tools else {}
    ctx = BuildContext(scope=scope, model=MODEL, provider="fake", platform="cli", session_id="s1")
    runtime = SessionRuntime(
        session_id="s1",
        caller=caller,
        clock=scope.clock,
        prompts=PromptLifecycle(StubBuilder()),
        prompt_context=ctx,
        store=MemoryStore(),
        registry=registry,
        tools=definitions(registry, list(tools), scope=scope),
        scope=scope,
        route=MAIN_ROUTE,
        options=options or LoopOptions(reasoning_effort="medium", max_tokens=4000),
        inline=inline,
        source="cli",
        run_context=run_context,
    )
    events: list[EventBase] = []
    sink = EventSink("s1", listeners=(events.append,))
    return Parent(scope, caller, runtime, log, events, sink)


# -- a scripted auxiliary client (routed reviews) -------------------------------------------------


@dataclass
class AuxReply:
    response: NormalizedResponse


@dataclass
class FakeAux:
    """The ``AuxClient`` surface the review uses: candidates, route_for, window_for, call."""

    route: RuntimeRoute
    steps: list[NormalizedResponse]
    effort: str = ""
    window: int = 100_000
    calls: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    def candidates(self, task: str) -> list[Candidate]:
        return [
            Candidate(
                "pin", self.route.provider, self.route.model, self.route.base_url, self.effort
            )
        ]

    def route_for(self, candidate: Candidate) -> RuntimeRoute:
        return self.route

    async def window_for(self, task: str, main_window: int) -> int:
        return self.window

    async def call(self, task: str, messages: Sequence[Any], **options: Any) -> AuxReply:
        self.calls.append({"task": task, "messages": list(messages), **options})
        step = self.steps.pop(0) if self.steps else text("Nothing worth keeping.")
        await checkpoint()
        return AuxReply(step)
