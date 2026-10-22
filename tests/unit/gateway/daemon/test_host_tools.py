"""The gateway daemon's own tools and their wiring (D-16.799, WB): ``message_agent`` with the
profile's bot transport (P24-07), ``a2a_call`` (P24-10) and each session's chat as
``delivery_origin`` (P24-13 ``kanban_create``, cron's ``origin``)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, cast

import pytest

from indus.foundation.scope import Scope
from indus.gateway.adapters.a2a.tools import TOOL_NAME as A2A_CALL
from indus.gateway.daemon import process
from indus.gateway.daemon.process import gateway_tool_registry
from indus.gateway.http import API_SURFACE, WEBHOOK_SURFACE
from indus.gateway.rooms.peers import LocalBotTransport
from indus.tools import toolsets
from indus.tools.builtin import load_builtins
from indus.tools.builtin.message_agent import TOOL_NAME as MESSAGE_AGENT
from indus.tools.builtin.message_agent import transport_for
from indus.tools.registry import ToolRegistry
from tests.fakes.agent import DictConfig


def test_the_daemon_registers_message_agent_and_a2a_call(scope: Scope) -> None:
    gateway = gateway_tool_registry(scope)
    assert gateway.get(MESSAGE_AGENT) is not None and gateway.get(A2A_CALL) is not None
    plain = ToolRegistry()
    load_builtins(plain)  # the CLI, cron, ACP: neither tool
    assert plain.get(MESSAGE_AGENT) is None and plain.get(A2A_CALL) is None
    # message_agent: canonical bot chats only (platform bots, the indus-bots bundle)
    bots = toolsets.select(["indus-bots"], registry=gateway, scope=scope, platform="bots").tools
    chat = toolsets.select(["indus-gateway"], registry=gateway, scope=scope, platform="telegram")
    assert MESSAGE_AGENT in bots and MESSAGE_AGENT not in chat.tools
    # a2a_call: in the indus-a2a bundle, offered while peers are configured (its probe)
    peered = scope.with_config(DictConfig({"a2a": {"peers": {"spark": {"url": "https://x.test"}}}}))
    assert A2A_CALL in toolsets.select(["indus-a2a"], registry=gateway, platform="a2a").tools
    entry = gateway.get(A2A_CALL)
    assert entry is not None and entry.probe is not None
    assert entry.probe(peered) and not entry.probe(scope)


@dataclass
class DaemonStub:
    scope: Scope
    closers: list[Callable[[], Awaitable[None]]] = field(
        default_factory=list[Callable[[], Awaitable[None]]]
    )
    pipeline: Any = None


def test_the_bot_transport_is_installed_for_the_profile_and_removed_at_stop(scope: Scope) -> None:
    daemon = DaemonStub(scope)
    process._install_bot_transport(cast(Any, daemon))  # pyright: ignore[reportPrivateUsage]
    installed = transport_for(scope)
    assert isinstance(installed, LocalBotTransport) and installed.scope is scope

    async def stop() -> None:
        for closer in daemon.closers:
            await closer()

    asyncio.run(stop())
    assert transport_for(scope) is None


@dataclass
class SourceStub:
    platform: str
    chat_id: str
    thread_id: str | None = None


class PipelineStub:
    def __init__(self, known: dict[str, SourceStub]) -> None:
        self.known = known

    async def origin_source(self, session_id: str) -> SourceStub | None:
        return self.known.get(session_id)


def test_a_session_chat_is_its_delivery_origin(scope: Scope) -> None:
    origin = process._chat_origin  # pyright: ignore[reportPrivateUsage]
    daemon = DaemonStub(scope)

    async def main() -> list[Any]:
        found = [await origin(cast(Any, daemon), "s1")]  # no pipeline yet
        daemon.pipeline = PipelineStub(
            {"s1": SourceStub("telegram", "42", "7"), "s2": SourceStub("slack", "C1")}
        )
        for session_id in ("s1", "s2", "s3"):
            found.append(await origin(cast(Any, daemon), session_id))
        return found

    assert asyncio.run(main()) == [
        None,
        {"platform": "telegram", "chat_id": "42", "thread_id": "7"},
        {"platform": "slack", "chat_id": "C1", "thread_id": None},
        None,
    ]


def test_the_daemon_shapes_bot_chats_on_chat_and_api_services_only(
    scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wave 45 (D-16.980): the chat service (desktop Bots page, attached one-shots) and the API
    server's service (a peer's ``/v1/bots/dm``) run the canonical chat as a bot turn; the webhook
    surface keeps its read-only floor."""
    seen: dict[str, bool] = {}

    async def capture(daemon: Any, **values: Any) -> None:
        seen[values["owner"]] = values.get("bot_chats", False)

    monkeypatch.setattr(process, "_session_service", capture)
    daemon = cast(Any, DaemonStub(scope))

    async def main() -> None:
        await process.build_service(daemon)
        await process.build_surface_service(daemon, API_SURFACE)
        await process.build_surface_service(daemon, WEBHOOK_SURFACE)

    asyncio.run(main())
    assert seen == {"gateway": True, "gateway-api_server": True, "gateway-webhook": False}
