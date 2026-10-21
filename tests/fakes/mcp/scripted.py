"""Scripted in-memory MCP servers for the host-level tests (P13-05..08).

``FakeFleet`` is an ``Opener`` (``lifecycle.Opener``): each configured server name maps to a
``FakeServer`` that says how opening goes (delay on the fake clock, a hang, an error) and what the
session answers (tools as raw JSON dicts, so hints like ``"readOnlyHint": "yes"`` reach the wire
tap exactly as a server would send them; calls; resources; prompts). The fleet counts how many
servers are opening at once and keeps every session so a test can push notifications.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, cast

import mcp_types as types
from mcp.shared.message import SessionMessage

from indus.foundation.scope import Scope
from indus.mcp.config import McpServer
from indus.mcp.connect import (  # pyright: ignore[reportPrivateUsage]
    ConnectOptions,
    LiveSession,
    _note_raw_hints,
)
from tests.fakes.clock import FakeClock, hold_for_off_loop

UNSET: Any = object()


def tool(
    name: str,
    *,
    read_only: Any = UNSET,
    description: str | None = None,
    schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A tool as a server lists it on the wire."""
    raw: dict[str, Any] = {
        "name": name,
        "inputSchema": schema or {"type": "object", "properties": {"x": {"type": "string"}}},
    }
    if description is not None:
        raw["description"] = description
    if read_only is not UNSET:
        raw["annotations"] = {"readOnlyHint": read_only}
    return raw


def text_result(text: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)])


Answer = Callable[[str, dict[str, Any]], Any]


@dataclass
class FakeServer:
    tools: list[dict[str, Any]] = field(default_factory=lambda: [tool("read", read_only=True)])
    capabilities: types.ServerCapabilities = field(
        default_factory=lambda: types.ServerCapabilities(tools=types.ToolsCapability())
    )
    open_delay: float = 0.0
    open_errors: list[BaseException] = field(default_factory=list[BaseException])
    hang: bool = False
    answer: Answer | None = None
    resources: list[types.Resource] = field(default_factory=list[types.Resource])
    prompts: list[types.Prompt] = field(default_factory=list[types.Prompt])
    # recordings
    opens: int = 0
    listings: int = 0
    calls: list[tuple[str, dict[str, Any]]] = field(
        default_factory=list[tuple[str, dict[str, Any]]]
    )
    live: LiveSession | None = None
    options: ConnectOptions | None = None


class FakeRaw:
    """The part of the SDK ``ClientSession`` the MCP client uses."""

    protocol_version = "2025-11-25"
    server_info = None
    instructions = None

    def __init__(self, server: FakeServer, hints: dict[str, object]) -> None:
        self.server = server
        self.hints = hints

    @property
    def server_capabilities(self) -> types.ServerCapabilities:
        return self.server.capabilities

    async def list_tools(self, params: Any = None) -> types.ListToolsResult:
        self.server.listings += 1
        raw = {"tools": [dict(t) for t in self.server.tools]}
        wire = types.JSONRPCResponse(jsonrpc="2.0", id=self.server.listings, result=raw)
        _note_raw_hints(SessionMessage(wire), self.hints)
        return types.ListToolsResult.model_validate(raw)

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        args = dict(arguments or {})
        self.server.calls.append((name, args))
        if self.server.answer is None:
            return text_result(f"{name} ok")
        produced = self.server.answer(name, args)
        if inspect.isawaitable(produced):
            produced = await produced
        return produced

    async def send_ping(self) -> None:
        return None

    async def list_resources(self, params: Any = None) -> types.ListResourcesResult:
        return types.ListResourcesResult(resources=self.server.resources)

    async def read_resource(self, uri: str) -> types.ReadResourceResult:
        return types.ReadResourceResult(
            contents=[types.TextResourceContents(uri=uri, text=f"contents of {uri}")]
        )

    async def list_prompts(self, params: Any = None) -> types.ListPromptsResult:
        return types.ListPromptsResult(prompts=self.server.prompts)

    async def get_prompt(
        self, name: str, arguments: dict[str, str] | None = None
    ) -> types.GetPromptResult:
        text = f"prompt {name} with {sorted((arguments or {}).items())}"
        return types.GetPromptResult(
            messages=[
                types.PromptMessage(role="user", content=types.TextContent(type="text", text=text))
            ]
        )


class FakeFleet:
    """An opener serving ``servers`` by name; counts concurrent opens."""

    def __init__(self, clock: FakeClock, servers: dict[str, FakeServer] | None = None) -> None:
        self.clock = clock
        self.servers: dict[str, FakeServer] = dict(servers or {})
        self.opening = 0
        self.max_opening = 0

    def __call__(
        self,
        name: str,
        server: McpServer,
        scope: Scope,
        *,
        clock: Any,
        options: ConnectOptions | None = None,
    ) -> Any:
        return self._open(name, server, options)

    @asynccontextmanager
    async def _open(
        self, name: str, config: McpServer, options: ConnectOptions | None
    ) -> AsyncGenerator[LiveSession]:
        fake = self.servers.setdefault(name, FakeServer())
        fake.opens += 1
        self.opening += 1
        self.max_opening = max(self.max_opening, self.opening)
        try:
            if fake.hang:
                await asyncio.Event().wait()
            if fake.open_delay:
                await self.clock.sleep(fake.open_delay)
            if fake.open_errors:
                raise fake.open_errors.pop(0)
        finally:
            self.opening -= 1
        hints: dict[str, object] = {}
        live = LiveSession(
            name, config.kind, cast(Any, FakeRaw(fake, hints)), asyncio.Event(), hints
        )
        fake.live = live
        fake.options = options
        yield live

    async def notify(self, name: str, notification: types.ServerNotification) -> None:
        options = self.servers[name].options
        assert options is not None and options.on_notification is not None
        await options.on_notification(name, notification)


async def run_for(clock: FakeClock, seconds: float, step: float = 1.0) -> None:
    """Move the fake clock ``seconds`` in ``step``s, letting every task settle in between."""
    await clock.run_until_idle()
    moved = 0.0
    while moved < seconds:
        await hold_for_off_loop(clock)  # never move the clock under an awaited off-loop call
        await clock.advance_and_settle(step)
        moved += step


async def drive[T](
    clock: FakeClock, work: Awaitable[T], *, step: float = 1.0, limit: float = 3600.0
) -> T:
    """Await ``work`` while moving the fake clock until it finishes (at most ``limit`` s)."""
    task = asyncio.ensure_future(work)
    await clock.run_until_idle()
    moved = 0.0
    while not task.done() and moved < limit:
        await hold_for_off_loop(clock)  # never move the clock under an awaited off-loop call
        if task.done():
            break
        await clock.advance_and_settle(step)
        moved += step
    return await task
