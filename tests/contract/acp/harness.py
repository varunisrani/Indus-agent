"""One ACP agent + client over either host, for the handler suites (P19-03..07).

``open_harness(kind, ...)`` builds the agent with a ``HostSelector`` for ``kind``:

- ``local``: ``LocalHost`` over a ``SessionService`` on the fake model and the test's store;
- ``attached``: ``AttachedHost`` over the real P17 daemon endpoint (``tests.fakes.daemon``), with
  the one method the daemon does not serve yet (``session.mutate``) shimmed.

Both write to the same store file the read-only views read. Nothing here is product code.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from indus.acp.agent import AcpAgent
from indus.acp.attached_host import AttachedHost
from indus.acp.backend import AcpBackend, HostSelector
from indus.acp.local_host import LocalHost
from indus.core.persist import StateStore
from indus.foundation.scope import Scope
from indus.protocol.events import EventBase
from indus.service.host_inprocess import ALL_KINDS
from indus.service.remote.channel import Channel, ChannelLimits
from indus.service.remote.host import RemoteSessionService
from indus.service.remote.journal import Journal
from indus.service.session_service import AgentFactory, SessionService
from indus.state import messages, sessions
from indus.state.registry import StoreHandle
from tests.fakes.acp_client import AcpPair
from tests.fakes.agent import fake_options
from tests.fakes.daemon import AcpDaemon, open_daemon
from tests.fakes.model import FakeModelServer, Turn, fake_route, script_of

__all__ = ["KINDS", "Harness", "open_harness", "seed_session"]

KINDS = ("local", "attached")
FAST = ChannelLimits(backoff_first=0.01, backoff_cap=0.05)


@dataclass
class Harness:
    kind: str
    agent: AcpAgent
    pair: AcpPair
    selector: HostSelector
    daemon: AcpDaemon | None = None
    server: FakeModelServer | None = None
    events: list[EventBase] = field(default_factory=list[EventBase])

    async def backend(self) -> AcpBackend:
        return await self.selector.get()

    async def call(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        return await self.pair.call(method, params)


async def _inline(build: Callable[[], SessionService]) -> SessionService:
    return build()


def local_backend(
    scope: Scope,
    store: StoreHandle,
    tmp_path: Path,
    *turns: Turn,
    tools: Mapping[str, Any] | None = None,
) -> tuple[LocalHost, FakeModelServer]:
    server = FakeModelServer(script_of(*turns), clock=scope.clock)
    inline = dict(tools or {})

    def build(factory: AgentFactory) -> SessionService:
        return SessionService(
            scope,
            fake_route(server),
            toolsets=[],
            options=fake_options(
                server, session_id=None, platform="acp", source="acp", inline=inline
            ),
            store=StateStore(store),
            lock_dir=tmp_path / "locks",
            owner="acp",
            answerable_kinds=tuple(ALL_KINDS),
            agent_factory=factory,
        )

    return LocalHost(scope, build, offload=_inline), server


def attached_backend(scope: Scope, daemon: AcpDaemon) -> AttachedHost:
    async def connect() -> RemoteSessionService:
        channel = Channel(daemon.dial, surface="acp", limits=FAST)
        return RemoteSessionService(channel, Journal(scope.home), surface="acp")

    return AttachedHost(scope, connect=connect)


@contextlib.asynccontextmanager
async def open_harness(
    kind: str,
    scope: Scope,
    store: StoreHandle,
    tmp_path: Path,
    *turns: Turn,
    tools: dict[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
    wsl: bool = False,
    script: Mapping[str, Any] | None = None,
) -> AsyncIterator[Harness]:
    daemon: AcpDaemon | None = None
    server: FakeModelServer | None = None
    if kind == "local":
        local, server = local_backend(scope, store, tmp_path, *turns, tools=tools)

        async def build_local() -> AcpBackend:
            return local

        async def no_daemon() -> AcpBackend:
            raise AssertionError("a local harness never attaches")

        selector = HostSelector(
            local_only=True, allow_fallback=False, attached=no_daemon, local=build_local
        )
    else:
        daemon = await open_daemon(scope, store, tmp_path, *turns, tools=tools)
        found = daemon

        async def build_attached() -> AcpBackend:
            host = attached_backend(scope, found)
            await host.start()
            return host

        async def no_local() -> AcpBackend:
            raise AssertionError("an attached harness never runs locally")

        selector = HostSelector(
            local_only=False, allow_fallback=False, attached=build_attached, local=no_local
        )
    agent = AcpAgent(scope, selector, env=dict(env or {}), wsl=wsl)
    pair = AcpPair(agent, script=script)
    harness = Harness(kind, agent, pair, selector, daemon, server)
    try:
        yield harness
    finally:
        await pair.close()
        if daemon is not None:
            await daemon.close()


def seed_session(
    store: StoreHandle,
    session_id: str,
    *,
    cwd: str | None,
    rows: list[dict[str, Any]] | None = None,
    source: str = "acp",
    title: str | None = None,
) -> None:
    """A stored conversation (row + messages) both hosts can resume."""
    fields: dict[str, Any] = {}
    if cwd is not None:
        fields["cwd"] = cwd
    sessions.create(store, session_id, source, **fields)
    for row in rows or []:
        messages.append(store, session_id, row)
    if title is not None:
        from indus.state import titles

        titles.set_title(store, session_id, title)
