"""Shared pieces of the ``indus mcp serve`` contract tests (P19-14..17).

- ``World``: a profile store with gateway conversations (session row + routing-index row) and
  their messages, written through the real state API, the way the gateway writes them.
- ``connected(bridge)``: the MCP SDK's in-memory client on the bridge's server.
- ``call(client, tool, args)``: one tool call, its JSON text parsed.
- ``FakeDaemon``: a real control socket in the profile home answering the verbs a test gives it
  (recording every request). Nothing here binds a network port.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

import pytest
from mcp import Client

from indus.foundation.scope import Scope
from indus.gateway.control_socket import ControlServer
from indus.mcp_serve.server import Bridge
from indus.state import gateway_routes, registry, sessions
from indus.state import messages as message_rows
from indus.state.registry import StoreHandle

type Handler = Callable[[dict[str, Any]], Mapping[str, Any] | Awaitable[Mapping[str, Any]]]


class World:
    """A profile store holding gateway conversations (see the module notes)."""

    def __init__(self, scope: Scope) -> None:
        self.scope = scope
        self.store: StoreHandle = registry.open_unshared(scope)
        self._count = 0

    def conversation(
        self,
        key: str,
        *,
        platform: str = "telegram",
        chat_id: str | None = None,
        chat_type: str = "dm",
        display: str | None = None,
        chat_name: str | None = None,
        user_name: str | None = None,
        thread_id: str | None = None,
        session_id: str | None = None,
    ) -> str:
        """Create a conversation (or re-point ``key`` at ``session_id``); its session id."""
        self._count += 1
        sid = session_id or f"s-{self._count:03d}"
        if sessions.get(self.store, sid) is None:
            sessions.create(self.store, sid, platform)
        origin: dict[str, Any] = {"platform": platform, "chat_id": chat_id or f"c{self._count}"}
        if chat_name:
            origin["chat_name"] = chat_name
        if user_name:
            origin["user_name"] = user_name
        if thread_id:
            origin["thread_id"] = thread_id
        gateway_routes.get_or_create(
            self.store,
            key,
            new_session_id=lambda: sid,
            platform=platform,
            chat_type=chat_type,
            origin=origin,
            display_name=display,
        )
        return sid

    def message(self, sid: str, role: str, content: object, **extra: Any) -> int:
        return message_rows.append(self.store, sid, {"role": role, "content": content, **extra})

    def close(self) -> None:
        self.store.close()


@pytest.fixture
def world(scope: Scope) -> Iterator[World]:
    built = World(scope)
    yield built
    built.close()


@asynccontextmanager
async def connected(bridge: Bridge) -> AsyncIterator[Client]:
    async with Client(bridge.server) as client:
        yield client


async def call(client: Client, tool: str, args: Mapping[str, Any] | None = None) -> dict[str, Any]:
    result = await client.call_tool(tool, dict(args or {}))
    assert not result.is_error, result
    block = result.content[0]
    text = getattr(block, "text", None)
    assert isinstance(text, str), block
    loaded = json.loads(text)
    assert isinstance(loaded, dict)
    return loaded


async def tool_names(client: Client) -> list[str]:
    return [tool.name for tool in (await client.list_tools()).tools]


class FakeDaemon:
    """A real control socket in ``scope.home`` answering ``handlers`` (see the module notes)."""

    def __init__(self, scope: Scope, handlers: Mapping[str, Handler]) -> None:
        self.scope = scope
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.server = ControlServer(scope.home, status=lambda: {"state": "running"}, env={})
        for verb, handler in handlers.items():
            self.server.register(verb, self._recorded(verb, handler), timeout=10.0)

    def _recorded(self, verb: str, handler: Handler) -> Handler:
        async def run(params: dict[str, Any]) -> Mapping[str, Any]:
            self.requests.append((verb, dict(params)))
            answer = handler(params)
            if isinstance(answer, Awaitable):
                return await answer
            return answer

        return run

    async def __aenter__(self) -> FakeDaemon:
        await self.server.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.server.stop()
