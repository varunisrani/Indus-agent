"""P19-16: ``indus mcp serve`` — ``messages_send`` through the daemon.

The daemon side is real code on a real control socket in the profile home: the bridge's
``deliver-message`` verb (``gateway.daemon.bridge_verbs``) over a real ``DeliveryRouter`` and an
``OutboundSender`` with a ``DeliveryLedger`` and a recording adapter. The MCP side is the server
under the SDK's in-memory client.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, cast

from indus.foundation.scope import Scope
from indus.gateway.daemon.bridge_verbs import BridgeVerbs
from indus.gateway.delivery.directory import ChannelDirectory
from indus.gateway.delivery.parts import RouterPart
from indus.gateway.delivery.router import DeliveryRouter
from indus.mcp_serve.send import NO_DAEMON
from indus.mcp_serve.server import open_bridge
from indus.service import messaging_bridge as wire
from indus.state import delivery_ledger as ledger_table
from tests.contract.mcp_serve.conftest import FakeDaemon, World, call, connected
from tests.unit.backends._aio import aio
from tests.unit.gateway.delivery.kit import ledgered
from tests.unit.gateway.outbound.recording import RecordingAdapter

CHATS = {
    "telegram": [
        {"id": "-100", "name": "Family", "type": "group"},
        {"id": "-200", "name": "Family Office", "type": "group"},
        {"id": "-300", "name": "Work", "type": "group"},
    ]
}


def write_directory(scope: Scope) -> None:
    path = scope.home / "gateway" / "channel_directory.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"updated_at": None, "platforms": CHATS}))


@asynccontextmanager
async def daemon_with_telegram(
    scope: Scope, world: World
) -> AsyncIterator[tuple[FakeDaemon, RecordingAdapter]]:
    """A control socket serving the real bridge verbs over a router with telegram attached."""
    adapter, sender, book = ledgered(scope, world.store, name="telegram")
    router = DeliveryRouter(
        scope, homes=lambda _name: None, directory=ChannelDirectory(scope).load()
    )
    router.attach("telegram", sender)
    part = RouterPart()
    part.router = router
    daemon = cast(Any, SimpleNamespace(parts=[part], host=None, store=world.store))
    handlers = {verb: handler for verb, (handler, _s) in BridgeVerbs(daemon).handlers().items()}
    try:
        async with FakeDaemon(scope, handlers) as served:
            yield served, adapter
    finally:
        router.close()
        await book.aclose()


@aio
async def test_one_send_is_one_ledger_backed_platform_send(scope: Scope, world: World) -> None:
    sid = world.conversation("agent:main:telegram:dm:12345", chat_id="12345")
    async with daemon_with_telegram(scope, world) as (daemon, adapter):
        bridge = await open_bridge(scope, start_events=False)
        try:
            async with connected(bridge) as client:
                sent = await call(
                    client, "messages_send", {"target": "telegram:12345", "message": "hi there"}
                )
        finally:
            await bridge.aclose()
    assert sent["sent"] is True and sent["target"] == "telegram:12345"
    assert sent["platform"] == "telegram" and sent["chat_id"] == "12345"
    assert sent["message_id"]
    texts = [op.text for op in adapter.ops if op.op == "send"]
    assert [(op.chat_id, op.text) for op in adapter.ops if op.op == "send"] == [
        ("12345", "hi there")
    ], texts
    sends = [params for verb, params in daemon.requests if verb == wire.DELIVER_VERB]
    assert sends == [{"text": "hi there", "target": "telegram:12345"}]
    rows = ledger_table.rows_for_session_key(world.store, wire.ledger_key("telegram", "12345"))
    assert len(rows) == 1 and rows[0].state == "delivered" and rows[0].content == "hi there"
    # the chat's own conversation key is never used: crash recovery reads rows under it
    assert ledger_table.rows_for_session_key(world.store, "agent:main:telegram:dm:12345") == []
    assert sid


@aio
async def test_friendly_names_resolve_through_the_directory(scope: Scope, world: World) -> None:
    write_directory(scope)
    async with daemon_with_telegram(scope, world) as (daemon, adapter):
        bridge = await open_bridge(scope, start_events=False)
        try:
            async with connected(bridge) as client:
                family = await call(
                    client, "messages_send", {"target": "telegram:family", "message": "dinner?"}
                )
                work = await call(
                    client, "messages_send", {"target": "telegram:#Work", "message": "late"}
                )
                ambiguous = await call(
                    client, "messages_send", {"target": "telegram:fam", "message": "x"}
                )
        finally:
            await bridge.aclose()
    assert family["sent"] is True and family["chat_id"] == "-100"
    assert work["sent"] is True and work["chat_id"] == "-300"
    assert "more than one" in ambiguous["error"]
    assert ambiguous["candidates"] == ["telegram:-100 (Family)", "telegram:-200 (Family Office)"]
    assert [(op.chat_id, op.text) for op in adapter.ops if op.op == "send"] == [
        ("-100", "dinner?"),
        ("-300", "late"),
    ]
    sends = [params for verb, params in daemon.requests if verb == wire.DELIVER_VERB]
    assert sends[0] == {"text": "dinner?", "platform": "telegram", "chat_id": "-100"}
    assert len(sends) == 2  # the ambiguous name never reached the daemon


@aio
async def test_the_daemons_refusals_come_back_as_errors(scope: Scope, world: World) -> None:
    async with daemon_with_telegram(scope, world) as (_daemon, adapter):
        bridge = await open_bridge(scope, start_events=False)
        try:
            async with connected(bridge) as client:
                offline = await call(
                    client, "messages_send", {"target": "slack:C0123ABCD", "message": "hello"}
                )
                malformed = await call(
                    client, "messages_send", {"target": "slack:C1", "message": "hello"}
                )
                unknown = await call(
                    client, "messages_send", {"target": "telegram:#nowhere", "message": "hello"}
                )
        finally:
            await bridge.aclose()
    assert offline["sent"] is False and offline["error"].startswith("platform_not_connected")
    assert (
        malformed["error"].startswith("bad_target") and "not a slack target" in malformed["error"]
    )
    assert unknown["error"].startswith("unknown_channel")
    assert adapter.ops == []


@aio
async def test_without_a_daemon_or_arguments_the_answer_is_an_error(scope: Scope) -> None:
    bridge = await open_bridge(scope, start_events=False)
    try:
        async with connected(bridge) as client:
            absent = await call(
                client, "messages_send", {"target": "telegram:1", "message": "hello"}
            )
            assert absent == {"error": NO_DAEMON}
            for target, message in (
                ("", "hello"),
                ("telegram:1", "   "),
                ("telegram", "hello"),
                ("all:x", "hello"),
                ("origin:x", "hello"),
                ("telegram:", "hello"),
            ):
                answer = await call(client, "messages_send", {"target": target, "message": message})
                assert set(answer) == {"error"}, (target, answer)
            huge = await call(
                client,
                "messages_send",
                {"target": "telegram:1", "message": "x" * (wire.MAX_MESSAGE_CHARS + 1)},
            )
            assert "longer than" in huge["error"]
    finally:
        await bridge.aclose()
