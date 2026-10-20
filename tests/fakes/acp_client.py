"""A minimal ACP client for tests (P19-02..12).

``AcpPair`` connects an ``AcpAgent`` to a client over the SDK's in-memory transport pair, both sides
being the SDK's own JSON-RPC ``Connection`` — the same framing code the stdio server runs. Every
frame the client receives (responses and notifications) is recorded in arrival order in
``frames``; requests from the agent (permission cards, elicitations) are answered by ``script``:
``{method: callable(params) -> result}`` (an unscripted method is answered ``-32601``).

Nothing here is product code.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import Any

from acp import RequestError
from acp._transport import memory_transport_pair  # pyright: ignore[reportPrivateUsage]
from acp.connection import Connection, StreamDirection, StreamEvent

from indus.acp.agent import AcpAgent
from indus.acp.entry import SdkPeer

__all__ = ["AcpPair"]

type Scripted = Callable[[Mapping[str, Any]], Any]


class AcpPair:
    def __init__(self, agent: AcpAgent, *, script: Mapping[str, Scripted] | None = None) -> None:
        self.agent = agent
        self.script: dict[str, Scripted] = dict(script or {})
        self.frames: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []
        server_end, client_end = memory_transport_pair()
        self.server = Connection(agent.dispatch, server_end)
        agent.peer = SdkPeer(self.server)
        self.client = Connection(self._answer, client_end, observers=[self._record])

    def _record(self, event: StreamEvent) -> None:
        if event.direction is StreamDirection.INCOMING:
            self.frames.append(event.message)

    async def _answer(self, method: str, params: Any, is_notification: bool) -> Any:
        if is_notification:
            return None
        self.requests.append({"method": method, "params": params})
        found = self.script.get(method)
        if found is None:
            raise RequestError.method_not_found(method)
        result = found(params or {})
        if asyncio.iscoroutine(result):
            result = await result
        return result

    async def call(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        return await self.client.send_request(method, dict(params or {}))

    async def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        await self.client.send_notification(method, dict(params or {}))

    async def settle(self) -> None:
        """Let queued frames arrive (a few loop turns)."""
        for _ in range(20):
            loop = asyncio.get_running_loop()
            waiter: asyncio.Future[None] = loop.create_future()
            loop.call_soon(waiter.set_result, None)
            await waiter

    def updates(self, session_id: str | None = None) -> list[dict[str, Any]]:
        """``session/update`` payloads received, in order (for one session when given)."""
        out: list[dict[str, Any]] = []
        for frame in self.frames:
            if frame.get("method") != "session/update":
                continue
            params = frame.get("params") or {}
            if session_id is None or params.get("sessionId") == session_id:
                out.append(params["update"])
        return out

    def kinds(self, session_id: str | None = None) -> list[str]:
        return [u.get("sessionUpdate", "") for u in self.updates(session_id)]

    def index_of_response(self, result_key: str | None = None) -> list[int]:
        """Positions of response frames in ``frames``."""
        return [
            i
            for i, frame in enumerate(self.frames)
            if "id" in frame
            and "method" not in frame
            and (result_key is None or result_key in (frame.get("result") or {}))
        ]

    async def close(self) -> None:
        await self.client.close()
        await self.server.close()
        await self.agent.aclose()
