"""P17-13: the daemon's WebSocket endpoint — upgrade auth, the ready frame, limits, the RPC loop.

The endpoint runs over a real ``DaemonSessionHost`` on the fake model; clients are the in-loop
ASGI double (``tests.fakes.asgi_ws``), so no socket is opened (the directory's socket guard stays
on). The real listener (uvicorn on 127.0.0.1) is covered in
``tests/integration/gateway/test_ws_listener.py``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.gateway.ws.server import (
    CLOSE_GOING_AWAY,
    CLOSE_SEND_DEADLINE,
    CLOSE_TOO_BIG,
    CLOSE_TRY_AGAIN,
    CLOSE_UNSUPPORTED,
    WsEndpoint,
    WsLimits,
    loggable_url,
)
from indus.protocol.bridge import SessionHistoryParams, SessionHistoryResult
from indus.protocol.errors import (
    ADMISSION_NOT_FOUND,
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    SESSION_BUSY,
    SESSION_NOT_FOUND,
)
from indus.protocol.methods import PROTOCOL_VERSION
from indus.service.daemon_host import DaemonSessionHost
from indus.service.ws_auth import (
    CLOSE_BAD_TICKET,
    CLOSE_FORBIDDEN,
    SUBPROTOCOL,
    TICKET_PROTOCOL_PREFIX,
    WS_INTERACTIVE,
    TicketBook,
)
from indus.state import registry
from indus.state import runtime_ledger as ledger
from indus.state.registry import StoreHandle
from tests.fakes.agent import eventually
from tests.fakes.asgi_ws import AsgiWsClient, WsClosed
from tests.fakes.model import Turn, text_turn, tool_batch
from tests.fakes.session_host import Holder, confirm, open_host
from tests.unit.backends._aio import aio


@pytest.fixture
def store(scope: Scope) -> Iterator[StoreHandle]:
    handle = registry.open_unshared(scope)
    yield handle
    handle.close()


class Rig:
    """A host, a ticket book and the endpoint over them."""

    def __init__(self, host: DaemonSessionHost, endpoint: WsEndpoint, book: TicketBook) -> None:
        self.host = host
        self.endpoint = endpoint
        self.book = book

    def ticket(self) -> str:
        return self.book.mint(WS_INTERACTIVE).ticket

    def client(
        self,
        ticket: str | None = None,
        *,
        offer_ticket: bool = True,
        query: bytes = b"",
        headers: tuple[tuple[str, str], ...] = (),
        peer: tuple[str, int] = ("127.0.0.1", 50123),
    ) -> AsgiWsClient:
        value = ticket if ticket is not None else self.ticket()
        protocols = [SUBPROTOCOL]
        if offer_ticket:
            protocols.append(TICKET_PROTOCOL_PREFIX + value)
        return AsgiWsClient(
            self.endpoint.app, subprotocols=protocols, query=query, headers=headers, peer=peer
        )

    async def attached(self, surface: str = "cli", **kwargs: Any) -> AsgiWsClient:
        """An open client past ``gateway.ready`` that declared its surface and request kinds."""
        client = self.client(**kwargs)
        assert await client.open() == SUBPROTOCOL
        ready = await client.recv()
        assert ready["method"] == "gateway.ready"
        answer = await client.request(
            "client.capabilities", {"features": ["server_requests"], "surface": surface}
        )
        assert answer["result"]["features"] == ["server_requests"]
        return client

    async def close(self) -> None:
        await self.endpoint.stop()
        assert self.endpoint.violations == []
        await self.host.aclose()


async def rig(
    scope: Scope,
    store: StoreHandle,
    tmp_path: Path,
    *turns: Turn,
    tools: dict[str, Any] | None = None,
    limits: WsLimits | None = None,
    origins: tuple[str, ...] = (),
) -> Rig:
    host = await open_host(scope, store, tmp_path, *turns, tools=tools)
    book = TicketBook.for_home(scope.home)
    endpoint = WsEndpoint(host, book, scope=scope, origins=origins, limits=limits, strict=True)
    return Rig(host, endpoint, book)


async def real_pause(seconds: float) -> None:
    """Let real time pass (the endpoint's socket limits run on the loop's own clock)."""
    idle = asyncio.get_running_loop().create_future()
    await asyncio.wait({idle}, timeout=seconds)
    idle.cancel()


def error_of(frame: dict[str, Any]) -> int:
    assert "error" in frame, frame
    return int(frame["error"]["code"])


def events_of(client: AsgiWsClient, session_id: str) -> list[dict[str, Any]]:
    return [
        n["params"]
        for n in client.notes
        if "seq" in n.get("params", {}) and n["params"].get("session_id") == session_id
    ]


def done(session_id: str):  # noqa: ANN201 - a frame predicate
    return lambda f: f.get("method") == "turn.done" and f["params"]["session_id"] == session_id


# -- the upgrade ----------------------------------------------------------------------------------


@aio
async def test_a_ticket_in_the_subprotocol_is_accepted_once(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    r = await rig(scope, store, tmp_path)
    ticket = r.ticket()
    first = r.client(ticket)
    assert await first.open() == SUBPROTOCOL  # only the protocol, never the ticket, is echoed
    ready = await first.recv()
    assert ready["method"] == "gateway.ready"
    params = ready["params"]
    assert params["replay_epoch"] == r.host.replay_epoch and params["heartbeat"] is True
    assert params["profile_id"] == r.book.profile_id
    assert params["instance_id"] == r.book.instance_id
    assert params["capabilities"] == list(r.host.capabilities())
    assert params["protocol_version"] == PROTOCOL_VERSION
    pong = await first.request("gateway.ping")
    assert pong["result"]["pong"] is True

    second = r.client(ticket)
    await second.open()
    closed = await second.closed_with()
    assert closed.code == CLOSE_BAD_TICKET and second.notes == []
    await first.close()
    await r.close()


@aio
async def test_a_ticket_in_the_url_is_never_used(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    r = await rig(scope, store, tmp_path)
    ticket = r.ticket()
    in_url = r.client(ticket, offer_ticket=False, query=b"ticket=" + ticket.encode())
    await in_url.open()
    assert (await in_url.closed_with()).code == CLOSE_BAD_TICKET
    later = r.client(ticket)  # seen in a URL: spent, useless now
    await later.open()
    assert (await later.closed_with()).code == CLOSE_BAD_TICKET
    await r.close()


@aio
async def test_a_foreign_origin_or_peer_is_refused_4403(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    r = await rig(scope, store, tmp_path, origins=("http://127.0.0.1:5173",))
    evil = r.client(headers=(("Origin", "http://evil"),))
    await evil.open()
    closed = await evil.closed_with()
    assert closed.code == CLOSE_FORBIDDEN and "evil" not in closed.reason
    remote = r.client(peer=("10.0.0.5", 40000))
    await remote.open()
    assert (await remote.closed_with()).code == CLOSE_FORBIDDEN
    allowed = r.client(headers=(("Origin", "http://127.0.0.1:5173"),))
    assert await allowed.open() == SUBPROTOCOL
    assert (await allowed.recv())["method"] == "gateway.ready"
    await allowed.close()
    await r.close()


# -- the RPC loop ---------------------------------------------------------------------------------


@aio
async def test_a_turn_streams_over_the_socket_in_order(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    r = await rig(scope, store, tmp_path, text_turn("Hel", "lo", " there"))
    client = await r.attached()
    created = await client.request("session.create", {})
    sid = created["result"]["session_id"]
    submitted = await client.request("prompt.submit", {"session_id": sid, "text": "hi"})
    result = submitted["result"]
    # An input into an idle session is answered once claimed: its turn's generation, never 0.
    assert result["status"] == "streaming" and result["turn_id"] and result["generation"] == 1
    await client.until(done(sid))
    events = events_of(client, sid)
    numbers = [int(e["seq"]) for e in events]
    assert numbers == list(range(numbers[0], numbers[0] + len(numbers)))
    text = "".join(e["text"] for e in events if e["type"] == "message.delta")
    assert text == "Hello there"
    assert (await r.host.wait(result["turn_id"])).outcome == "completed"
    unserved = await client.request("session.history", {"session_id": sid})
    assert error_of(unserved) == METHOD_NOT_FOUND.code
    await client.close()
    await r.close()


@aio
async def test_an_approval_is_answered_from_another_surface_with_its_generation(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    r = await rig(
        scope,
        store,
        tmp_path,
        tool_batch(("confirm", {})),
        text_turn("cleaned"),
        tools={"confirm": confirm},
    )
    cli = await r.attached("cli")
    tui = await r.attached("tui")
    sid = (await cli.request("session.create", {}))["result"]["session_id"]
    subscribed = await tui.request("session.subscribe", {"session_id": sid})
    assert "gap" not in subscribed["result"] and subscribed["result"]["replayed"] == 0
    await cli.request("prompt.submit", {"session_id": sid, "text": "clean"})
    offer = (await tui.until(lambda f: f.get("method") == "server_request"))["params"]
    await cli.until(lambda f: f.get("method") == "server_request")
    request_id, generation = offer["request"]["request_id"], offer["generation"]
    answer = {"kind": "approval", "choice": "once"}
    missing = await tui.request(
        "server_request.answer", {"session_id": sid, "request_id": request_id, "answer": answer}
    )
    assert error_of(missing) == INVALID_PARAMS.code
    assert missing["error"]["data"]["path"] == "generation"
    base = {"session_id": sid, "request_id": request_id, "answer": answer}
    stale = await tui.request("server_request.answer", {**base, "generation": generation - 1})
    assert stale["result"]["accepted"] is False
    taken = await tui.request("server_request.answer", {**base, "generation": generation})
    assert taken["result"]["accepted"] is True
    settled = await cli.until(lambda f: f.get("method") == "request.settled")
    assert settled["params"]["request_id"] == request_id
    assert settled["params"]["outcome"] == "answered"
    await cli.until(done(sid))
    await tui.until(done(sid))
    await cli.close()
    await tui.close()
    await r.close()


@aio
async def test_an_unrenderable_request_refused_by_its_only_viewer_settles_at_once(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    r = await rig(
        scope,
        store,
        tmp_path,
        tool_batch(("confirm", {})),
        text_turn("skipped"),
        tools={"confirm": confirm},
    )
    cli = await r.attached()
    sid = (await cli.request("session.create", {}))["result"]["session_id"]
    await cli.request("prompt.submit", {"session_id": sid, "text": "clean"})
    offer = (await cli.until(lambda f: f.get("method") == "server_request"))["params"]
    request_id = offer["request"]["request_id"]
    await cli.send_json(
        {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "no card"}}
    )
    settled = await cli.until(lambda f: f.get("method") == "request.settled")
    assert settled["params"]["outcome"] == "undeliverable"
    await cli.until(done(sid))
    await cli.close()
    await r.close()


@aio
async def test_interrupt_and_steer_name_their_generation(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    holder = Holder()
    r = await rig(scope, store, tmp_path, tool_batch(("hold", {})), tools={"hold": holder})
    client = await r.attached()
    sid = (await client.request("session.create", {}))["result"]["session_id"]
    await client.request("prompt.submit", {"session_id": sid, "text": "work"})
    await eventually(lambda: holder.entered)
    missing = await client.request("session.interrupt", {"session_id": sid})
    assert error_of(missing) == INVALID_PARAMS.code
    stale = await client.request("session.interrupt", {"session_id": sid, "generation": 0})
    assert stale["result"]["interrupted"] is False and holder.entered == 1
    steer = await client.request(
        "session.steer", {"session_id": sid, "text": "faster", "generation": 0}
    )
    assert steer["result"]["queued"] is False
    busy = await client.request(
        "prompt.submit", {"session_id": sid, "text": "more", "when_busy": "reject"}
    )
    assert error_of(busy) == SESSION_BUSY.code
    hit = await client.request("session.interrupt", {"session_id": sid, "generation": 1})
    assert hit["result"]["interrupted"] is True
    await client.until(done(sid))
    await client.close()
    await r.close()


@aio
async def test_submit_answers_real_generations_and_carries_unattended(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    holder = Holder()
    r = await rig(
        scope,
        store,
        tmp_path,
        tool_batch(("hold", {})),
        text_turn("done"),
        text_turn("next"),
        tools={"hold": holder},
    )
    client = await r.attached()
    sid = (await client.request("session.create", {}))["result"]["session_id"]
    first = await client.request(
        "prompt.submit", {"session_id": sid, "text": "work", "unattended": True}
    )
    assert first["result"]["status"] == "streaming" and first["result"]["generation"] == 1
    row = ledger.get(store, first["result"]["turn_id"])
    assert row is not None and row.payload.get("unattended") is True
    await eventually(lambda: holder.entered)
    queued = await client.request(
        "prompt.submit", {"session_id": sid, "text": "later", "when_busy": "queue"}
    )
    # queued behind the running turn: that turn's generation (the documented "running turn")
    assert queued["result"]["status"] == "queued" and queued["result"]["generation"] == 1
    later = ledger.get(store, queued["result"]["turn_id"])
    assert later is not None and "unattended" not in later.payload
    holder.gate.set()
    qid = queued["result"]["turn_id"]
    await client.until(
        lambda f: (
            f.get("method") == "admission.state"
            and f["params"]["admission_id"] == qid
            and f["params"]["state"] == "terminal"
        )
    )
    moves = [e for e in events_of(client, sid) if e["type"] == "admission.state"]
    assert [m["state"] for m in moves if m["admission_id"] == qid] == [
        "queued",
        "started",
        "terminal",
    ]
    await client.close()
    await r.close()


@aio
async def test_submit_carries_single_query_into_the_admission(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    """W47 g (D-16.487): ``single_query`` (an attached finite ``chat -q``) is kept with the
    input, so the daemon's turn runs ``single_query``; a later plain input does not carry it."""
    r = await rig(scope, store, tmp_path, text_turn("done"), text_turn("next"))
    client = await r.attached()
    sid = (await client.request("session.create", {}))["result"]["session_id"]
    first = await client.request(
        "prompt.submit", {"session_id": sid, "text": "work", "single_query": True}
    )
    row = ledger.get(store, first["result"]["turn_id"])
    assert row is not None and row.payload.get("single_query") is True
    await client.until(done(sid))
    later = await client.request("prompt.submit", {"session_id": sid, "text": "more"})
    plain = ledger.get(store, later["result"]["turn_id"])
    assert plain is not None and "single_query" not in plain.payload
    await client.close()
    await r.close()


@aio
async def test_a_queued_input_is_cancelled_by_admission_and_a_started_one_is_not(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    holder = Holder()
    r = await rig(
        scope, store, tmp_path, tool_batch(("hold", {})), text_turn("done"), tools={"hold": holder}
    )
    client = await r.attached("acp")
    other = await r.attached("tui")
    sid = (await client.request("session.create", {}))["result"]["session_id"]
    first = await client.request("prompt.submit", {"session_id": sid, "text": "work"})
    running = first["result"]["turn_id"]
    await eventually(lambda: holder.entered)
    queued = await client.request(
        "prompt.submit", {"session_id": sid, "text": "later", "when_busy": "queue"}
    )
    qid = queued["result"]["turn_id"]
    foreign = await other.request("admission.cancel", {"session_id": sid, "admission_id": qid})
    assert error_of(foreign) == SESSION_NOT_FOUND.code  # not a session this connection attached
    unknown = await client.request("admission.cancel", {"session_id": sid, "admission_id": "nope"})
    assert error_of(unknown) == ADMISSION_NOT_FOUND.code
    started = await client.request("admission.cancel", {"session_id": sid, "admission_id": running})
    assert started["result"] == {"cancelled": False, "state": "started", "generation": 1}
    assert holder.entered == 1 and not holder.gate.is_set()  # the running turn is untouched
    dropped = await client.request("admission.cancel", {"session_id": sid, "admission_id": qid})
    assert dropped["result"]["cancelled"] is True and dropped["result"]["state"] == "terminal"
    again = await client.request("admission.cancel", {"session_id": sid, "admission_id": qid})
    assert again["result"]["cancelled"] is True and again["result"]["outcome"] == "cancelled"
    row = ledger.get(store, qid)
    assert row is not None and (row.state, row.outcome) == ("terminal", "cancelled")
    holder.gate.set()
    await client.until(done(sid))
    assert (await r.host.wait(running)).outcome == "completed"
    usage = await client.request("session.usage", {"session_id": sid})
    numbers = usage["result"]["usage"]
    assert numbers["api_calls"] >= 1 and numbers["output_tokens"] >= 0
    foreign_usage = await other.request("session.usage", {"session_id": sid})
    assert error_of(foreign_usage) == SESSION_NOT_FOUND.code
    await client.close()
    await other.close()
    await r.close()


@aio
async def test_a_connection_reaches_only_sessions_it_attached(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    r = await rig(scope, store, tmp_path, text_turn("ok"))
    owner = await r.attached()
    other = await r.attached("tui")
    sid = (await owner.request("session.create", {}))["result"]["session_id"]
    calls: list[tuple[str, dict[str, Any]]] = [
        ("prompt.submit", {"session_id": sid, "text": "hi"}),
        ("session.interrupt", {"session_id": sid, "generation": 1}),
        ("events.since", {"session_id": sid, "cursor": 0}),
        ("server_request.decline", {"session_id": sid, "request_id": "srq-000000000000"}),
    ]
    for method, params in calls:
        assert error_of(await other.request(method, params)) == SESSION_NOT_FOUND.code, method
    joined = await other.request("session.subscribe", {"session_id": sid})
    assert joined["result"]["session_id"] == sid
    caught = await other.request(
        "events.since", {"session_id": sid, "epoch": r.host.replay_epoch, "cursor": 0}
    )
    assert caught["result"]["last_seq"] == len(caught["result"]["events"])
    await owner.close()
    await other.close()
    await r.close()


@aio
async def test_closing_the_socket_mid_turn_only_detaches(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    holder = Holder()
    r = await rig(
        scope,
        store,
        tmp_path,
        tool_batch(("hold", {})),
        text_turn("finished"),
        tools={"hold": holder},
    )
    client = await r.attached()
    sid = (await client.request("session.create", {}))["result"]["session_id"]
    turn = (await client.request("prompt.submit", {"session_id": sid, "text": "go"}))["result"]
    await eventually(lambda: holder.entered)
    await client.close()
    assert r.host.state(sid).viewers == () and r.endpoint.connections == {}
    holder.gate.set()
    finished = await r.host.wait(turn["turn_id"])
    assert finished.outcome == "completed"
    await r.close()


# -- limits ---------------------------------------------------------------------------------------


@aio
async def test_a_slow_reader_is_closed_without_stalling_other_viewers(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    limits = WsLimits(write_timeout=0.2)
    r = await rig(scope, store, tmp_path, text_turn("a", "b", "c"), text_turn("d"), limits=limits)
    slow = await r.attached("tui")
    fast = await r.attached("cli")
    sid = (await fast.request("session.create", {}))["result"]["session_id"]
    await slow.request("session.subscribe", {"session_id": sid})
    slow.pause()
    turn = (await fast.request("prompt.submit", {"session_id": sid, "text": "go"}))["result"]
    await fast.until(done(sid))
    assert (await r.host.wait(turn["turn_id"])).outcome == "completed"
    closed = await slow.closed_with()
    assert closed.code == CLOSE_SEND_DEADLINE
    await slow.wait_done()
    assert r.host.state(sid).viewers == tuple(r.endpoint.connections)  # only the fast one
    assert len(r.endpoint.connections) == 1
    again = await fast.request("prompt.submit", {"session_id": sid, "text": "again"})
    await fast.until(done(sid))
    assert again["result"]["turn_id"]
    await fast.close()
    await r.close()


@aio
async def test_the_257th_waiting_request_closes_the_socket_1013(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    r = await rig(scope, store, tmp_path)
    gate = asyncio.Event()
    entered: list[int] = []

    async def slow_history(conn: object, params: SessionHistoryParams) -> SessionHistoryResult:
        entered.append(1)
        await gate.wait()
        raise AssertionError("never answered")

    r.endpoint.table.add("session.history", slow_history)  # a test-only slow handler
    client = await r.attached()
    for n in range(256):
        await client.send_json(
            {
                "jsonrpc": "2.0",
                "id": 1000 + n,
                "method": "session.history",
                "params": {"session_id": "s"},
            }
        )
    await eventually(lambda: len(entered) == 256)
    pong = await client.request("gateway.ping")  # the read loop still answers pings
    assert pong["result"]["pong"] is True
    await client.send_json(
        {"jsonrpc": "2.0", "id": 9999, "method": "session.history", "params": {"session_id": "s"}}
    )
    closed = await client.closed_with()
    assert closed.code == CLOSE_TRY_AGAIN
    await client.wait_done()
    assert r.endpoint.connections == {}
    await r.close()


@aio
async def test_binary_and_oversized_messages_close_the_socket(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    r = await rig(scope, store, tmp_path, limits=WsLimits(max_message_bytes=1000))
    big = await r.attached()
    await big.send_text('{"jsonrpc":"2.0","id":1,"method":"gateway.ping","pad":"' + "x" * 2000)
    assert (await big.closed_with()).code == CLOSE_TOO_BIG
    binary = await r.attached()
    await binary.send_bytes(b"\x00\x01")
    assert (await binary.closed_with()).code == CLOSE_UNSUPPORTED
    broken = await r.attached()
    await broken.send_text("{not json")
    answer = await broken.recv()
    assert answer["id"] is None and answer["error"]["code"] == -32700
    await broken.close()
    await r.close()


@aio
async def test_a_silent_client_is_closed_after_the_idle_limit(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    r = await rig(scope, store, tmp_path, limits=WsLimits(idle_timeout=0.5))
    talker = await r.attached()
    silent = await r.attached("tui")
    for _ in range(8):
        await real_pause(0.1)
        assert (await talker.request("gateway.ping"))["result"]["pong"] is True
    assert (await silent.closed_with()).code == CLOSE_GOING_AWAY
    assert len(r.endpoint.connections) == 1  # pings count as activity
    await talker.close()
    await r.close()


@aio
async def test_stopping_the_endpoint_closes_every_socket_1001(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    r = await rig(scope, store, tmp_path)
    one, two = await r.attached(), await r.attached("tui")
    await r.endpoint.stop()
    for client in (one, two):
        assert (await client.closed_with()).code == CLOSE_GOING_AWAY
    late = r.client()
    await late.open()
    assert (await late.closed_with()).code == CLOSE_GOING_AWAY
    await r.host.aclose()


@aio
async def test_logs_carry_no_ticket_params_or_query(
    scope: Scope, store: StoreHandle, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    r = await rig(scope, store, tmp_path, text_turn("ok"))
    ticket = r.ticket()
    client = await r.attached(ticket=ticket, query=b"token=" + r.ticket().encode())
    sid = (await client.request("session.create", {}))["result"]["session_id"]
    words = "pass" + "phrase-" + "4417"
    await client.request("prompt.submit", {"session_id": sid, "text": words})
    await client.until(done(sid))
    await client.close()
    await r.close()
    assert ticket not in caplog.text and words not in caplog.text
    assert "token=" not in caplog.text
    assert loggable_url("ws://me:pw@127.0.0.1:9/ws?ticket=x#f") == "ws://127.0.0.1:9/ws"


@aio
async def test_resume_answers_the_daemon_cursor(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    r = await rig(scope, store, tmp_path, text_turn("one"))
    first = await r.attached()
    sid = (await first.request("session.create", {}))["result"]["session_id"]
    await first.request("prompt.submit", {"session_id": sid, "text": "hi"})
    await first.until(done(sid))
    second = await r.attached("tui")
    resumed = await second.request("session.resume", {"session_id": sid})
    while not any(n.get("method") == "session.info" for n in second.notes):
        await second.until(lambda f: f.get("method") == "session.info")
    # Then presence and the header (W48-h).
    assert resumed["result"]["last_seq"] == r.host.event_log.last_seq(sid) - 2
    closed = await second.request("session.close", {"session_id": sid})
    assert closed["result"]["closed"] is True
    assert sid in r.host.hosted()  # closing from a client only detaches
    await first.close()
    await second.close()
    await r.close()


def test_the_endpoint_serves_the_attach_methods() -> None:
    from indus.gateway.ws.methods import served

    assert {"session.subscribe", "session.unsubscribe", "sessions.watch", "events.since"} <= set(
        served()
    )
    assert "gateway.ping" in served() and "prompt.submit" in served()


def test_ws_closed_double_has_a_code() -> None:
    assert WsClosed(4401, "x").code == 4401
