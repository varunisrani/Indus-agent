"""Helpers for building real agents and services against the fake model (P04-20, P04-21).

``fake_options(server, ...)`` gives ``AgentOptions`` whose clients come from the
``FakeModelServer``, whose window lookup needs no endpoint, and whose prompt is the stub builder's
fixed text. ``run_driven(coro, clock)`` awaits a coroutine while moving the fake clock whenever it
waits (the same driver ``TurnKit`` uses).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Mapping
from typing import Any

from indus.core.agent import AgentOptions
from indus.providers.model_list import ModelInfo
from indus.providers.route import RuntimeRoute
from indus.tools.registry import ToolRegistry
from tests.fakes.clock import FakeClock
from tests.fakes.model import FakeModelServer
from tests.fakes.turn import StubBuilder, drive

WINDOW = 200_000


async def any_window(route: RuntimeRoute) -> list[ModelInfo]:
    """A model list that gives every model asked for a 200K window."""
    return [ModelInfo(route.model, WINDOW)]


def fake_options(
    server: FakeModelServer,
    *,
    registry: ToolRegistry | None = None,
    session_id: str | None = "s1",
    **values: Any,
) -> AgentOptions:
    values.setdefault("builder", StubBuilder())
    return AgentOptions(
        session_id=session_id,
        client_factory=lambda route: server.client(),
        list_models=any_window,
        registry=registry if registry is not None else ToolRegistry(),
        **values,
    )


async def run_driven[T](coro: Coroutine[Any, Any, T], clock: FakeClock, step: float = 1.0) -> T:
    task = asyncio.ensure_future(coro)
    await drive(task, clock, step)
    return task.result()


async def eventually(check: Callable[[], object], limit: float = 5.0) -> None:
    """Let the loop run (real time, short slices) until ``check()`` is true."""
    loop = asyncio.get_running_loop()
    end = loop.time() + limit
    while not check():
        if loop.time() > end:
            raise AssertionError("the condition never held")
        idle = loop.create_future()
        await asyncio.wait({idle}, timeout=0.001)
        idle.cancel()


class DictConfig:
    """A config view over a nested mapping (dotted paths)."""

    def __init__(self, data: Mapping[str, Any]) -> None:
        self._data = data

    def get(self, path: str, /) -> object:
        node: object = self._data
        for part in path.split("."):
            if not isinstance(node, Mapping) or part not in node:
                return None
            node = node[part]  # pyright: ignore[reportUnknownVariableType]
        return node  # pyright: ignore[reportUnknownVariableType]
