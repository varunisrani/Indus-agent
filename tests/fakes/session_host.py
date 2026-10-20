"""A real ``DaemonSessionHost`` over the fake model, for transport tests (P17-13/14).

``open_host`` builds the P04 ``SessionService`` on a scripted fake model server and opens the
daemon host over it (store writes run inline). ``Holder`` is an inline tool that parks the turn
until released or interrupted; ``confirm`` asks for an approval and reports the answer.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from indus.core.persist import StateStore
from indus.core.turn_state import TurnState
from indus.foundation.scope import Scope
from indus.protocol.requests_port import RequestNotAnswered
from indus.protocol.server_requests import ApprovalAnswer
from indus.service.daemon_host import DaemonSessionHost
from indus.service.event_log import EventLog
from indus.service.session_service import SessionService
from indus.state.registry import StoreHandle
from indus.tools.calls import ToolCall, ToolResult
from tests.fakes.agent import fake_options
from tests.fakes.model import FakeModelServer, Turn, fake_route, script_of


class Holder:
    """An inline tool that holds the turn until released (or the turn is interrupted)."""

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.entered = 0

    async def __call__(self, call: ToolCall, state: TurnState) -> ToolResult:
        self.entered += 1
        waits = {
            asyncio.ensure_future(self.gate.wait()),
            asyncio.ensure_future(state.control.stop.wait()),
        }
        await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        for task in waits:
            task.cancel()
        return ToolResult("held")


async def confirm(call: ToolCall, state: TurnState) -> ToolResult:
    """Ask for approval of a fixed command; the answer becomes the tool result."""
    broker = state.control.broker
    assert broker is not None
    try:
        answer = await broker.ask(
            "approval", {"command": "rm -r build", "description": "clean up"}, turn_id=state.turn_id
        )
    except RequestNotAnswered as exc:
        return ToolResult(f"not approved: {exc}", is_error=True)
    assert isinstance(answer, ApprovalAnswer)
    return ToolResult(f"approved: {answer.choice.value}")


async def inline[T](fn: Callable[[], T]) -> T:
    return fn()


async def open_host(
    scope: Scope,
    store: StoreHandle,
    tmp_path: Path,
    *turns: Turn,
    tools: Mapping[str, Any] | None = None,
    event_log: EventLog | None = None,
) -> DaemonSessionHost:
    """An open daemon host whose model answers ``turns`` in order."""
    server = FakeModelServer(script_of(*turns), clock=scope.clock)
    options = fake_options(server, session_id=None, inline=dict(tools or {}))
    service = SessionService(
        scope,
        fake_route(server),
        toolsets=[],
        options=options,
        store=StateStore(store),
        lock_dir=tmp_path / "locks",
    )
    return await DaemonSessionHost.open(
        service, store, instance_id="daemon-1", offload=inline, event_log=event_log
    )
