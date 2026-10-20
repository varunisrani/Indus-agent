"""P19-17: ``indus mcp serve`` — the approval tools, wired to the daemon (D-16.30, R09.10).

The daemon side is real: a ``DaemonSessionHost`` over the fake model whose tool asks for an
approval, a viewer that can show it, and the bridge's ``approvals-open`` / ``approvals-respond``
verbs on a real control socket in the profile home. The MCP side is the server under the SDK's
in-memory client.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from indus.core.persist import StateStore
from indus.core.turn_state import TurnState
from indus.foundation.scope import Scope
from indus.gateway.daemon.bridge_verbs import BridgeVerbs
from indus.mcp_serve.server import APPROVAL_TOOLS, TOOL_ORDER, open_bridge
from indus.protocol.requests_port import RequestNotAnswered
from indus.protocol.server_requests import ApprovalAnswer
from indus.service import messaging_bridge as wire
from indus.service.daemon_host import DaemonSessionHost
from indus.service.request_redelivery import Offer
from indus.service.session_service import SessionService
from indus.service.viewers import Viewer
from indus.tools.calls import ToolCall, ToolResult
from tests.contract.mcp_serve.conftest import FakeDaemon, World, call, connected, tool_names
from tests.fakes.agent import eventually, fake_options
from tests.fakes.clock import FakeClock
from tests.fakes.model import FakeModelServer, fake_route, script_of, text_turn, tool_batch
from tests.unit.backends._aio import aio

CWD = os.path.abspath(os.sep + os.path.join("work", "repo"))
# A credential-shaped value, built at runtime (never a literal in the repository).
TOKEN = "".join(("gh", "p_", "Q7x" * 12))


def asking(answers: list[str]) -> Callable[[ToolCall, TurnState], Any]:
    async def confirm(call_: ToolCall, state: TurnState) -> ToolResult:
        broker = state.control.broker
        assert broker is not None
        try:
            answer = await broker.ask(
                "approval",
                {
                    "command": f"curl -H 'Authorization: token {TOKEN}' https://api.example.test",
                    "description": "calls the API",
                },
                turn_id=state.turn_id,
            )
        except RequestNotAnswered as exc:
            answers.append(f"not answered: {exc}")
            return ToolResult("not approved", is_error=True)
        assert isinstance(answer, ApprovalAnswer)
        answers.append(answer.choice.value)
        return ToolResult(f"approved: {answer.choice.value}")

    return confirm


async def inline[T](fn: Callable[[], T]) -> T:
    return fn()


@asynccontextmanager
async def hosted(
    scope: Scope, world: World, tmp_path: Path, answers: list[str]
) -> AsyncIterator[tuple[DaemonSessionHost, FakeDaemon]]:
    server = FakeModelServer(
        script_of(tool_batch(("confirm", {})), text_turn("done")), clock=scope.clock
    )
    options = fake_options(server, session_id=None, inline={"confirm": asking(answers)})
    service = SessionService(
        scope,
        fake_route(server),
        toolsets=[],
        options=options,
        store=StateStore(world.store),
        lock_dir=tmp_path / "locks",
    )
    host = await DaemonSessionHost.open(service, world.store, instance_id="d-1", offload=inline)
    daemon = cast(Any, SimpleNamespace(host=host, store=world.store, parts=[]))
    handlers = {verb: h for verb, (h, _s) in BridgeVerbs(daemon).handlers().items()}
    try:
        async with FakeDaemon(scope, handlers) as served:
            yield host, served
    finally:
        await host.aclose()


@aio
async def test_without_a_daemon_the_approval_tools_are_not_listed(scope: Scope) -> None:
    bridge = await open_bridge(scope, start_events=False)
    try:
        assert bridge.approvals.available is False
        async with connected(bridge) as client:
            names = await tool_names(client)
        assert names == list(TOOL_ORDER)
    finally:
        await bridge.aclose()


@aio
async def test_a_daemon_without_a_session_host_offers_no_queue(scope: Scope) -> None:
    daemon = cast(Any, SimpleNamespace(host=None, store=None, parts=[]))
    handlers = {verb: h for verb, (h, _s) in BridgeVerbs(daemon).handlers().items()}
    async with FakeDaemon(scope, handlers):
        bridge = await open_bridge(scope, start_events=False)
        try:
            async with connected(bridge) as client:
                assert not set(APPROVAL_TOOLS) & set(await tool_names(client))
        finally:
            await bridge.aclose()


@aio
async def test_an_open_prompt_is_listed_answered_once_and_announced(
    scope: Scope, world: World, tmp_path: Path, fake_clock: FakeClock
) -> None:
    answers: list[str] = []
    offers: list[Offer] = []
    tui = Viewer.of("t1", "tui", ("approval",), send=lambda _e: None, offer=offers.append)
    async with hosted(scope, world, tmp_path, answers) as (host, daemon):
        sid = (await host.create(viewer=tui)).session_id
        world.conversation("agent:main:telegram:dm:9", session_id=sid, chat_id="9")
        receipt = await host.submit(sid, "call the api", principal="p", request_id="r1")
        await eventually(lambda: offers)
        request_id = offers[0].request.request_id

        bridge = await open_bridge(scope, start_events=False)
        try:
            async with connected(bridge) as client:
                names = await tool_names(client)
                assert names == [*TOOL_ORDER, *APPROVAL_TOOLS]

                listed = await call(client, "permissions_list_open")
                assert listed["count"] == 1
                item = listed["approvals"][0]
                assert item["id"] == request_id and item["session_id"] == sid
                assert item["session_key"] == "agent:main:telegram:dm:9"
                assert TOKEN not in item["command"] and "curl" in item["command"]
                assert item["description"] == "calls the API"
                assert item["choices"][0] == "allow-once" and "deny" in item["choices"]
                assert item["generation"] == 1

                await bridge.approvals.watch()
                polled = await call(client, "events_poll")
                requested = [e for e in polled["events"] if e["type"] == "approval_requested"]
                assert [e["approval_id"] for e in requested] == [request_id]
                assert requested[0]["session_key"] == "agent:main:telegram:dm:9"
                assert TOKEN not in str(requested[0])

                bad = await call(
                    client, "permissions_respond", {"id": request_id, "decision": "maybe"}
                )
                assert "allow-once" in bad["error"] and "allow-session" in bad["error"]
                unknown = await call(
                    client, "permissions_respond", {"id": "srq-nope", "decision": "deny"}
                )
                assert "not found" in unknown["error"]

                first = await call(
                    client, "permissions_respond", {"id": request_id, "decision": "allow-once"}
                )
                assert first == {
                    "resolved": True,
                    "approval_id": request_id,
                    "decision": "allow-once",
                }
                outcome = await host.wait(receipt.admission_id)
                assert outcome.outcome == "completed" and answers == ["once"]
                sent = [p for verb, p in daemon.requests if verb == wire.APPROVALS_RESPOND_VERB]
                assert sent == [
                    {
                        "session_id": sid,
                        "request_id": request_id,
                        "generation": 1,
                        "choice": "once",
                    }
                ]

                second = await call(
                    client, "permissions_respond", {"id": request_id, "decision": "deny"}
                )
                assert second == {
                    "resolved": False,
                    "approval_id": request_id,
                    "reason": "settled",
                }
                assert (await call(client, "permissions_list_open"))["count"] == 0

                fake_clock.advance(1.0)
                await bridge.approvals.watch()
                after = await call(client, "events_poll", {"after_cursor": polled["next_cursor"]})
                resolved = [e for e in after["events"] if e["type"] == "approval_resolved"]
                assert resolved == [
                    {
                        "cursor": resolved[0]["cursor"],
                        "type": "approval_resolved",
                        "session_key": "agent:main:telegram:dm:9",
                        "approval_id": request_id,
                        "decision": "allow-once",
                    }
                ]
        finally:
            await bridge.aclose()


@aio
async def test_a_prompt_answered_elsewhere_first_is_settled_here(
    scope: Scope, world: World, tmp_path: Path
) -> None:
    answers: list[str] = []
    offers: list[Offer] = []
    tui = Viewer.of("t1", "tui", ("approval",), send=lambda _e: None, offer=offers.append)
    async with hosted(scope, world, tmp_path, answers) as (host, _daemon):
        sid = (await host.create(viewer=tui)).session_id
        receipt = await host.submit(sid, "call the api", principal="p", request_id="r1")
        await eventually(lambda: offers)
        request_id = offers[0].request.request_id
        bridge = await open_bridge(scope, start_events=False)
        try:
            async with connected(bridge) as client:
                listed = await call(client, "permissions_list_open")
                assert listed["approvals"][0]["session_key"] is None  # not a chat
                # the TUI answers first, through the same fences
                host.respond(sid, request_id, 1, {"choice": "deny"})
                await host.wait(receipt.admission_id)
                late = await call(
                    client, "permissions_respond", {"id": request_id, "decision": "allow-once"}
                )
                assert late == {"resolved": False, "approval_id": request_id, "reason": "settled"}
                assert answers == ["deny"]
        finally:
            await bridge.aclose()
