"""P17-11: the control socket's attach verbs (``session-ticket``, ``describe``) and their client."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.gateway.control_socket import ControlServer
from indus.gateway.daemon.attach_verbs import AttachHost, AttachVerbs
from indus.protocol.methods import PROTOCOL_VERSION
from indus.service import gateway_control as wire
from indus.service.daemon_host import CREATABLE_SOURCES, HOST_CAPABILITIES, DaemonSessionHost
from indus.service.ws_auth import (
    SUBPROTOCOL,
    TICKET_PROTOCOL_PREFIX,
    WS_INTERACTIVE,
    TicketBook,
    UpgradeAccepted,
    UpgradeRefused,
    authenticate_upgrade,
    stable_profile_id,
)
from indus.state import registry
from indus.state.registry import StoreHandle
from tests.fakes.agent import eventually
from tests.fakes.model import text_turn, tool_batch
from tests.short_paths import short_dir
from tests.unit.service.test_daemon_host import Holder, open_host

WS_URL = "ws://127.0.0.1:47011/ws"


@pytest.fixture
def store(scope: Scope) -> Iterator[StoreHandle]:
    handle = registry.open_unshared(scope)
    yield handle
    handle.close()


@pytest.fixture
def short_home() -> Iterator[Path]:
    with short_dir("igw-") as home:
        yield home


def verbs_for(home: Path, *, url: str | None = WS_URL, **kw: Any) -> AttachVerbs:
    return AttachVerbs(home, TicketBook.for_home(home), ws_url=lambda: url, version="9.9.9", **kw)


def served(
    home: Path,
    verbs: AttachVerbs,
    body: Callable[[ControlServer], Awaitable[None]],
    **kw: Any,
) -> None:
    async def main() -> None:
        server = ControlServer(home, status=lambda: {"state": "running"}, env={}, **kw)
        verbs.install(server)
        await server.start()
        try:
            await body(server)
        finally:
            await server.stop()
            verbs.close()

    asyncio.run(main())


def upgrade_scope(ticket: str) -> dict[str, Any]:
    protocols = [SUBPROTOCOL, TICKET_PROTOCOL_PREFIX + ticket]
    return {
        "type": "websocket",
        "client": ("127.0.0.1", 50001),
        "headers": [(b"sec-websocket-protocol", ", ".join(protocols).encode())],
        "subprotocols": protocols,
        "query_string": b"",
    }


def test_round_trip_describe_ticket_upgrade(
    short_home: Path,
    scope: Scope,
    store: StoreHandle,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    verbs = verbs_for(short_home)
    seen: dict[str, Any] = {}

    async def body(_server: ControlServer) -> None:
        host = await open_host(scope, store, tmp_path)
        typed: AttachHost = host  # the daemon session host is what ``describe`` reads
        verbs.host = typed
        try:
            described = await asyncio.to_thread(wire.describe, short_home)
        finally:
            await host.aclose()
        assert described is not None
        seen["described"] = described
        seen["reply"] = await asyncio.to_thread(
            wire.session_ticket,
            short_home,
            WS_INTERACTIVE,
            profile_id=described["profile_id"],
            instance_id=described["instance_id"],
        )

    with caplog.at_level(logging.DEBUG):
        served(short_home, verbs, body)
    described = seen["described"]
    assert described == {
        "profile_id": stable_profile_id(short_home),
        "instance_id": verbs.book.instance_id,
        "code_version": "9.9.9",
        "protocol_version": PROTOCOL_VERSION,
        "ws_url": WS_URL,
        "creatable_sources": ["cli", "tui", "gui", "acp", "web"],
        "capabilities": list(HOST_CAPABILITIES),
    }
    assert list(CREATABLE_SOURCES) == described["creatable_sources"]
    assert "messaging" not in described["creatable_sources"]
    assert {"events.since", "server_requests.approval", "request.decline"} <= set(
        described["capabilities"]
    )
    reply: wire.TicketReply = seen["reply"]
    assert reply.ok and reply.error is None
    assert reply.ws_url == WS_URL and reply.expires_in == 30.0
    assert (reply.profile_id, reply.instance_id) == (
        described["profile_id"],
        described["instance_id"],
    )
    assert reply.ticket not in repr(reply)
    assert reply.ticket not in caplog.text
    assert "minted a ws-interactive ticket" in caplog.text
    # The ticket opens exactly one upgrade. (The book was revoked at stop; mint again for the
    # upgrade half against a live book.)
    book = TicketBook.for_home(short_home)
    live = AttachVerbs(short_home, book, ws_url=lambda: WS_URL)
    answer = asyncio.run(live.session_ticket({"purpose": WS_INTERACTIVE}))
    first = authenticate_upgrade(upgrade_scope(answer["ticket"]), WS_INTERACTIVE, book=book)
    assert isinstance(first, UpgradeAccepted)
    second = authenticate_upgrade(upgrade_scope(answer["ticket"]), WS_INTERACTIVE, book=book)
    assert isinstance(second, UpgradeRefused) and second.close_code == 4401


def test_close_revokes_tickets(short_home: Path) -> None:
    verbs = verbs_for(short_home)
    answer = asyncio.run(verbs.session_ticket({"purpose": WS_INTERACTIVE}))
    assert verbs.book.outstanding() == 1
    verbs.close()
    refused = authenticate_upgrade(upgrade_scope(answer["ticket"]), WS_INTERACTIVE, book=verbs.book)
    assert refused == UpgradeRefused(4401, "ticket unknown")


def test_describe_answers_while_a_turn_runs(
    short_home: Path, scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    """A real host turn parked in a tool: ``describe`` and ``session-ticket`` answer at once and
    the turn is never disturbed."""
    verbs = verbs_for(short_home)
    timings: list[float] = []

    async def body(_server: ControlServer) -> None:
        holder = Holder()
        host: DaemonSessionHost = await open_host(
            scope,
            store,
            tmp_path,
            tool_batch(("hold", {})),
            text_turn("done"),
            tools={"hold": holder},
        )
        verbs.host = host
        try:
            sid = (await host.create()).session_id
            receipt = await host.submit(sid, "go", principal="cli:me", request_id="r1")
            await eventually(lambda: holder.entered)
            running = host.state(sid).running
            assert running is not None
            started = time.monotonic()
            described = await asyncio.to_thread(wire.describe, short_home)
            timings.append(time.monotonic() - started)
            assert described is not None
            assert described["creatable_sources"] == list(host.creatable_sources())
            assert described["capabilities"] == list(host.capabilities())
            ticket = await asyncio.to_thread(wire.session_ticket, short_home, WS_INTERACTIVE)
            assert ticket.ok
            assert host.state(sid).running == running  # the turn was never disturbed
            assert holder.entered == 1
            holder.gate.set()
            done = await host.wait(receipt.admission_id)
            assert done.outcome == "completed"
        finally:
            await host.aclose()

    served(short_home, verbs, body)
    assert timings[0] < 1.0


def test_an_unverified_peer_gets_no_ticket(short_home: Path) -> None:
    verbs = verbs_for(short_home)
    results: dict[str, Any] = {}

    async def body(_server: ControlServer) -> None:
        results["ticket"] = await asyncio.to_thread(wire.session_ticket, short_home, WS_INTERACTIVE)
        results["describe"] = await asyncio.to_thread(wire.describe, short_home)
        results["identify"] = await asyncio.to_thread(wire.identify, short_home)

    served(short_home, verbs, body, peer=lambda _sock: None)
    assert results["ticket"].error == "forbidden" and not results["ticket"].ticket
    assert results["describe"] is not None and results["identify"] is not None
    assert verbs.book.outstanding() == 0


def test_owner_only_holds_even_when_registered_without_the_flag(short_home: Path) -> None:
    verbs = verbs_for(short_home)
    server = ControlServer(short_home, env={})
    for verb, (handler, seconds) in verbs.handlers().items():
        server.register(verb, handler, timeout=seconds)  # the DaemonPorts path: no flag
    line = json.dumps(
        {"protocol": 1, "verb": "session-ticket", "id": 3, "params": {"purpose": WS_INTERACTIVE}}
    ).encode()
    answer = json.loads(asyncio.run(server.handle_line(line)))
    assert answer == {
        "ok": False,
        "protocol": 1,
        "error": "forbidden",
        "detail": "the peer could not be verified",
        "id": 3,
    }
    ok = json.loads(asyncio.run(server.handle_line(line, peer_verified=True)))
    assert ok["ok"] is True and ok["result"]["ws_url"] == WS_URL


def test_another_users_peer_is_forbidden(short_home: Path) -> None:
    verbs = verbs_for(short_home)
    results: list[wire.TicketReply] = []

    async def body(_server: ControlServer) -> None:
        results.append(await asyncio.to_thread(wire.session_ticket, short_home, WS_INTERACTIVE))

    served(short_home, verbs, body, peer=lambda _sock: os.getuid() + 1)
    assert results[0].error == "forbidden"


def test_group_writable_home_refused_by_the_client(
    short_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    verbs = verbs_for(short_home)
    results: dict[str, Any] = {}

    async def body(_server: ControlServer) -> None:
        os.chmod(short_home, 0o770)
        monkeypatch.setattr(wire, "_private_group", lambda _gid: False)
        results["ticket"] = await asyncio.to_thread(wire.session_ticket, short_home, WS_INTERACTIVE)
        results["describe"] = await asyncio.to_thread(wire.describe, short_home)
        # The daemon refuses as well, even to a caller that skipped the client's check.
        try:
            await verbs.session_ticket({"purpose": WS_INTERACTIVE})
        except Exception as exc:
            results["server"] = getattr(exc, "code", type(exc).__name__)
        os.chmod(short_home, 0o700)

    served(short_home, verbs, body)
    assert results["ticket"].error == "unsafe_home"
    assert results["describe"] is None
    assert results["server"] == "unsafe_home"
    assert verbs.book.outstanding() == 0


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({}, "bad_params"),
        ({"purpose": 5}, "bad_params"),
        ({"purpose": WS_INTERACTIVE, "subject": "auth:x"}, "bad_params"),
        ({"purpose": "dashboard-root"}, "bad_purpose"),
        ({"purpose": WS_INTERACTIVE, "profile_id": "0" * 16}, "profile_mismatch"),
        ({"purpose": WS_INTERACTIVE, "instance_id": "old-start"}, "stale_instance"),
    ],
)
def test_refusals(short_home: Path, params: dict[str, Any], code: str) -> None:
    verbs = verbs_for(short_home)
    line = json.dumps({"protocol": 1, "verb": "session-ticket", "id": 1, "params": params})
    server = ControlServer(short_home, env={})
    verbs.install(server)
    answer = json.loads(asyncio.run(server.handle_line(line.encode(), peer_verified=True)))
    assert answer["ok"] is False and answer["error"] == code
    assert verbs.book.outstanding() == 0


def test_no_ticket_without_a_ws_endpoint_and_capacity(short_home: Path) -> None:
    server = ControlServer(short_home, env={})
    verbs_for(short_home, url=None).install(server)
    line = json.dumps(
        {"protocol": 1, "verb": "session-ticket", "id": 1, "params": {"purpose": WS_INTERACTIVE}}
    ).encode()
    answer = json.loads(asyncio.run(server.handle_line(line, peer_verified=True)))
    assert answer["error"] == "ws_unavailable"
    described = json.loads(
        asyncio.run(server.handle_line(b'{"protocol":1,"verb":"describe","id":2}'))
    )
    assert described["ok"] is True and described["result"]["ws_url"] is None

    tight = AttachVerbs(short_home, TicketBook.for_home(short_home, limit=1), ws_url=lambda: WS_URL)
    server2 = ControlServer(short_home, env={})
    tight.install(server2)
    first = json.loads(asyncio.run(server2.handle_line(line, peer_verified=True)))
    second = json.loads(asyncio.run(server2.handle_line(line, peer_verified=True)))
    assert first["ok"] is True and second["error"] == "capacity"
    assert first["result"]["ticket"] not in json.dumps(second)


def test_describe_refuses_params_and_works_without_a_host(short_home: Path) -> None:
    verbs = AttachVerbs(short_home, TicketBook.for_home(short_home))  # the daemon before P17-13
    server = ControlServer(short_home, env={})
    verbs.install(server)
    bad = json.loads(
        asyncio.run(server.handle_line(b'{"protocol":1,"verb":"describe","params":{"x":1}}'))
    )
    assert bad["error"] == "bad_params"
    good = json.loads(asyncio.run(server.handle_line(b'{"protocol":1,"verb":"describe"}')))
    assert good["result"]["creatable_sources"] == [] and good["result"]["capabilities"] == []


def test_verbs_are_unsupported_until_registered(short_home: Path) -> None:
    server = ControlServer(short_home, env={})
    for verb in ("session-ticket", "describe"):
        line = json.dumps({"protocol": 1, "verb": verb}).encode()
        assert json.loads(asyncio.run(server.handle_line(line)))["error"] == "unsupported"


@pytest.mark.parametrize(
    ("result", "expect", "code"),
    [
        ({"ticket": "short"}, {}, "bad_response"),
        ({"ws_url": "ws://10.0.0.5:4000/ws"}, {}, "bad_response"),
        ({"ws_url": "ws://127.0.0.1:4000/ws?ticket=abc"}, {}, "bad_response"),
        ({"ws_url": "ws://u:p@127.0.0.1:4000/ws"}, {}, "bad_response"),
        ({"ws_url": "http://127.0.0.1:4000/ws"}, {}, "bad_response"),
        ({"ws_url": "ws://127.0.0.1/ws"}, {}, "bad_response"),
        ({"profile_id": 7}, {}, "bad_response"),
        ({}, {"profile_id": "other"}, "profile_mismatch"),
        ({}, {"instance_id": "other"}, "stale_instance"),
        ({}, {}, None),
    ],
)
def test_client_checks_the_answer(
    short_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    result: dict[str, Any],
    expect: dict[str, str],
    code: str | None,
) -> None:
    ticket = "A" * 43
    base = {
        "ticket": ticket,
        "ws_url": "ws://[::1]:4000/ws",
        "profile_id": "p1",
        "instance_id": "i1",
        "expires_in": 30,
    }
    answered = wire.ControlReply(True, result={**base, **result})
    monkeypatch.setattr(wire, "query", lambda *_a, **_k: answered)
    reply = wire.session_ticket(
        short_home,
        WS_INTERACTIVE,
        profile_id=expect.get("profile_id"),
        instance_id=expect.get("instance_id"),
    )
    assert reply.error == code
    assert reply.ok is (code is None)
    if code is not None:
        assert reply.ticket == "" and ticket not in repr(reply)
