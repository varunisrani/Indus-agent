"""Fakes for subagent tests (P10-02..07).

``RoutedModel`` answers the parent and each subagent from its own scripted ``FakeModelServer``:
a request whose conversation starts with a subagent brief ("Your task:\\n<goal>") goes to the
server registered for that goal's first line; every other request goes to the parent's. So
children running side by side never take each other's turns.

``parent_agent`` starts a real ``Agent`` over it with the built-in tools loaded.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from indus.core.agent import Agent, AgentOptions
from indus.foundation.scope import Scope
from indus.tools.builtin import load_builtins
from indus.tools.registry import ToolRegistry
from tests.fakes.agent import any_window
from tests.fakes.clock import FakeClock
from tests.fakes.model import Turn, script_of
from tests.fakes.model.server import FakeModelServer, fake_route
from tests.fakes.turn import StubBuilder

BRIEF_START = "Your task:\n"


def _messages(body: object) -> list[Mapping[str, Any]]:
    if not isinstance(body, Mapping):
        return []
    found = body.get("messages")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    return list(found) if isinstance(found, list) else []  # pyright: ignore[reportUnknownArgumentType]


def _text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, Mapping)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType, reportUnknownVariableType]
        )
    return ""


def speaker(body: object) -> str | None:
    """The goal (first line) of the subagent whose request this is; None for the parent."""
    for message in _messages(body):
        if message.get("role") != "user":
            continue
        text = _text(message.get("content"))
        if text.startswith(BRIEF_START):
            return text[len(BRIEF_START) :].split("\n", 1)[0]
        return None
    return None


class RoutedModel:
    """One fake server for the parent, one per subagent goal (see the module notes)."""

    def __init__(
        self,
        clock: FakeClock,
        parent: Sequence[Turn] = (),
        children: Mapping[str, Sequence[Turn]] | None = None,
    ) -> None:
        self.clock = clock
        self.parent = FakeModelServer(script_of(*parent, name="parent"), clock=clock)
        self.children: dict[str, FakeModelServer] = {
            goal: FakeModelServer(script_of(*turns, name=goal), clock=clock)
            for goal, turns in (children or {}).items()
        }
        self.transport = httpx.MockTransport(self._handle)

    def client(self, **kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self.transport, **kwargs)

    def child(self, goal: str) -> FakeModelServer:
        return self.children[goal]

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        raw = await request.aread()
        try:
            body: object = json.loads(raw) if raw else None
        except ValueError:
            body = None
        goal = speaker(body)
        if goal is None:
            return await self.parent._handle(request)  # pyright: ignore[reportPrivateUsage]
        server = self.children.get(goal)
        if server is None:
            raise AssertionError(f"no scripted subagent for goal {goal!r}")
        return await server._handle(request)  # pyright: ignore[reportPrivateUsage]


def subagent_registry() -> ToolRegistry:
    registry = ToolRegistry()
    load_builtins(registry)
    return registry


async def parent_agent(
    scope: Scope,
    model: RoutedModel,
    selection: Sequence[str] | None = ("file", "terminal", "delegation"),
    **values: Any,
) -> Agent:
    values.setdefault("builder", StubBuilder())
    values.setdefault("registry", subagent_registry())
    values.setdefault("session_id", "parent")
    options = AgentOptions(
        client_factory=lambda route: model.client(),
        list_models=any_window,
        **values,
    )
    return await Agent.start(
        scope,
        fake_route(model.parent),
        list(selection) if selection is not None else None,
        options,
    )
