"""A bridge on a fake model and a real P02 store, driven through its dispatcher (P12-28..31)."""

from __future__ import annotations

import contextlib
import itertools
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from indus.core.persist import StateStore
from indus.foundation.scope import Scope
from indus.protocol.envelope import Request
from indus.protocol.events import EVENTS
from indus.protocol.server_requests import SERVER_REQUEST_KINDS, SERVER_REQUEST_METHOD
from indus.service.session_service import SessionService
from indus.state import registry
from indus.state.registry import StoreHandle
from indus.tui_bridge.bridge import Bridge
from indus.tui_bridge.dispatch import Dispatcher
from tests.fakes.agent import eventually, fake_options
from tests.fakes.clock import FakeClock
from tests.fakes.model import FakeModelServer, Turn, fake_route, script_of


@dataclass
class Recorder:
    """An emitter that keeps every notification."""

    frames: list[tuple[str, dict[str, Any]]] = field(
        default_factory=list[tuple[str, dict[str, Any]]]
    )

    def notify(self, method: str, params: Mapping[str, Any]) -> bool:
        self.frames.append((method, dict(params)))
        return True

    def of(self, method: str) -> list[dict[str, Any]]:
        return [params for name, params in self.frames if name == method]

    def methods(self) -> list[str]:
        return [name for name, _ in self.frames]

    def check_all(self) -> None:
        """Every frame validates against its declared model."""
        for method, params in self.frames:
            if method == SERVER_REQUEST_METHOD:
                SERVER_REQUEST_KINDS[params["kind"]][0].model_validate(params)
            else:
                EVENTS[method].model_validate(params)


@dataclass
class Rig:
    bridge: Bridge
    dispatcher: Dispatcher
    recorder: Recorder
    server: FakeModelServer
    handle: StoreHandle
    ids: itertools.count[int] = field(default_factory=lambda: itertools.count(1))

    async def call(self, method: str, **params: Any) -> dict[str, Any]:
        request = Request(id=next(self.ids), method=method, params=params)
        return await self.dispatcher.handle(request)

    async def ok(self, method: str, **params: Any) -> dict[str, Any]:
        answer = await self.call(method, **params)
        assert "result" in answer, answer
        return answer["result"]

    async def err(self, method: str, **params: Any) -> dict[str, Any]:
        answer = await self.call(method, **params)
        assert "error" in answer, answer
        return answer["error"]

    async def service(self) -> SessionService:
        return await self.bridge.service()

    async def idle(self, session_id: str) -> None:
        service = await self.service()
        await eventually(lambda: service.agent(session_id).current is None)

    async def turn_done(self, count: int = 1) -> None:
        await eventually(lambda: len(self.recorder.of("message.complete")) >= count)


@contextlib.asynccontextmanager
async def open_rig(
    scope: Scope,
    clock: FakeClock,
    tmp_path: Path,
    *turns: Turn,
    inline: Mapping[str, Any] | None = None,
    strict: bool = True,
    environments: Any = None,
) -> AsyncIterator[Rig]:
    server = FakeModelServer(script_of(*turns), clock=clock)
    handle = registry.open_unshared(scope)
    store = StateStore(handle)
    route = fake_route(server)
    recorder = Recorder()

    def make(bridge: Bridge) -> SessionService:
        return SessionService(
            scope,
            route,
            toolsets=[],
            options=fake_options(server, session_id=None, inline=dict(inline or {})),
            store=store,
            lock_dir=tmp_path / "locks",
            agent_factory=bridge.requests.agent_factory(route, store),
        )

    bridge = Bridge(
        scope,
        recorder,
        make_service=make,
        store=lambda: handle,
        strict=strict,
        environments=environments,
    )
    rig = Rig(bridge, Dispatcher(bridge), recorder, server, handle)
    try:
        yield rig
    finally:
        await bridge.aclose()
        handle.close()
