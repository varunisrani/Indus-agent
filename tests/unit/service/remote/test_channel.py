"""P17-16: the Python attach channel — tickets per dial, heartbeat, reconnect, transport swaps.

Two kinds of far end: the daemon's real WebSocket endpoint over a ``DaemonSessionHost`` on the fake
model, reached in-loop through the ASGI double (``wire.Rig``), and scripted transports driven by a
fake clock for the liveness timings. The real ``wsproto`` client is exercised against a uvicorn
listener on loopback at the end.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.service.remote import channel as channel_mod
from indus.service.remote.channel import (
    Channel,
    ChannelLimits,
    Dialed,
    DialRefused,
    Grant,
    NotSent,
    RpcError,
    TransportLost,
    grant_for,
)
from indus.service.ws_auth import SUBPROTOCOL, TICKET_PROTOCOL_PREFIX
from indus.state import registry
from indus.state.registry import StoreHandle
from tests.fakes.agent import eventually
from tests.fakes.clock import FakeClock, checkpoint
from tests.fakes.model import text_turn, tool_batch
from tests.fakes.session_host import Holder
from tests.unit.backends._aio import aio
from tests.unit.service.remote.wire import AsgiTransport, ScriptedTransport, rig


@pytest.fixture
def store(scope: Scope) -> Iterator[StoreHandle]:
    handle = registry.open_unshared(scope)
    yield handle
    handle.close()


def scripted(clock: FakeClock, transports: list[ScriptedTransport], **limits: Any) -> Channel:
    """A channel whose every dial makes a new scripted transport that is ready at once."""

    async def dial() -> Dialed:
        transport = ScriptedTransport()
        transport.ready()
        transports.append(transport)
        return Dialed(transport, "p1", "i1")

    return Channel(dial, clock=clock, limits=ChannelLimits(**limits))


async def settle(clock: FakeClock, seconds: float, step: float = 1.0) -> None:
    """Advance the fake clock in steps, letting the channel's tasks run between them."""
    left = seconds
    while left > 0:
        move = min(step, left)
        await clock.advance_and_settle(move)
        left -= move


# -- liveness -------------------------------------------------------------------------------------


@aio
async def test_a_server_streaming_without_pongs_stays_connected(fake_clock: FakeClock) -> None:
    transports: list[ScriptedTransport] = []
    channel = scripted(fake_clock, transports)
    await channel.start()
    live = transports[0]
    for second in range(120):
        live.push({"jsonrpc": "2.0", "method": "message.delta", "params": {"n": second}})
        await settle(fake_clock, 1.0)
    assert live.closed is None and channel.connected and len(transports) == 1
    assert len(live.pings()) == 8  # every 15 s, never more than 8 unanswered
    await channel.close()


@aio
async def test_a_silent_server_is_closed_at_45_s(fake_clock: FakeClock) -> None:
    transports: list[ScriptedTransport] = []
    states: list[str] = []
    channel = scripted(fake_clock, transports)
    channel.on_state(lambda state, info: states.append(state))
    await channel.start()
    await settle(fake_clock, 44.0)
    assert transports[0].closed is None and len(transports[0].pings()) == 2
    await settle(fake_clock, 1.0)
    assert transports[0].closed == 1001 and "reconnecting" in states
    await settle(fake_clock, 1.0)  # first redial after 1 s
    assert len(transports) == 2 and channel.connected
    await channel.close()


@aio
async def test_pongs_keep_the_ping_count_down(fake_clock: FakeClock) -> None:
    transports: list[ScriptedTransport] = []
    channel = scripted(fake_clock, transports)
    await channel.start()
    live = transports[0]
    for _ in range(12):
        await settle(fake_clock, 15.0)
        ping = live.pings()[-1]
        live.push({"jsonrpc": "2.0", "id": ping["id"], "result": {"pong": True}})
        await checkpoint()
    await fake_clock.run_until_idle()
    assert channel.outstanding_pings == 0 and live.closed is None
    await channel.close()


@aio
async def test_without_a_heartbeat_a_quiet_server_is_left_alone(fake_clock: FakeClock) -> None:
    async def dial() -> Dialed:
        transport = ScriptedTransport()
        transport.ready(heartbeat=False)
        made.append(transport)
        return Dialed(transport, "p1")

    made: list[ScriptedTransport] = []
    channel = Channel(dial, clock=fake_clock)
    await channel.start()
    await settle(fake_clock, 120.0, step=5.0)
    assert made[0].closed is None and made[0].pings() == []
    await channel.close()


# -- reconnect ------------------------------------------------------------------------------------


@aio
async def test_reconnect_backs_off_1_2_4_up_to_30_s_and_resets_on_ready(
    fake_clock: FakeClock,
) -> None:
    attempts: list[float] = []
    fail = True
    made: list[ScriptedTransport] = []

    async def dial() -> Dialed:
        attempts.append(fake_clock.monotonic())
        if fail and len(attempts) > 1:
            raise DialRefused("no_daemon")
        transport = ScriptedTransport()
        transport.ready()
        made.append(transport)
        return Dialed(transport, "p1")

    channel = Channel(dial, clock=fake_clock)
    await channel.start()
    start = fake_clock.monotonic()
    await channel.drop()
    await fake_clock.run_until_idle()
    await settle(fake_clock, 1 + 2 + 4 + 8 + 16 + 30 + 30, step=0.5)
    gaps = [round(b - a) for a, b in zip([start, *attempts[1:]], attempts[1:], strict=False)]
    assert gaps == [1, 2, 4, 8, 16, 30, 30]
    fail = False
    await settle(fake_clock, 30.0, step=0.5)
    assert channel.connected and len(made) == 2
    attempts.clear()
    await channel.drop()
    await fake_clock.run_until_idle()
    await settle(fake_clock, 1.0, step=0.5)
    assert len(attempts) == 1 and channel.connected  # the count started over after ready
    await channel.close()


@aio
async def test_a_terminal_dial_refusal_stops_the_redial(fake_clock: FakeClock) -> None:
    calls = 0

    async def dial() -> Dialed:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise DialRefused("unsafe_home", terminal=True)
        transport = ScriptedTransport()
        transport.ready()
        return Dialed(transport, "p1")

    channel = Channel(dial, clock=fake_clock)
    await channel.start()
    await channel.drop()
    await settle(fake_clock, 10.0)
    assert channel.state == "failed" and calls == 2
    assert channel.failure is not None and channel.failure.reason == "unsafe_home"
    with pytest.raises(NotSent):
        await channel.request("gateway.ping")
    await channel.close()


@aio
async def test_ready_for_another_profile_is_refused(fake_clock: FakeClock) -> None:
    async def dial() -> Dialed:
        transport = ScriptedTransport()
        transport.ready(profile_id="other")
        return Dialed(transport, "p1")

    channel = Channel(dial, clock=fake_clock)
    with pytest.raises(DialRefused) as refused:
        await channel.start()
    assert refused.value.reason == "profile_mismatch" and refused.value.terminal


@aio
async def test_a_server_that_does_not_echo_the_protocol_is_refused(fake_clock: FakeClock) -> None:
    async def dial() -> Dialed:
        transport = ScriptedTransport(subprotocol=None)
        transport.ready()
        return Dialed(transport, None)

    with pytest.raises(DialRefused) as refused:
        await Channel(dial, clock=fake_clock).start()
    assert refused.value.reason == "protocol_mismatch"


@aio
async def test_a_missing_ready_frame_times_out(fake_clock: FakeClock) -> None:
    async def dial() -> Dialed:
        return Dialed(ScriptedTransport(), None)

    channel = Channel(dial, clock=fake_clock)
    started = asyncio.ensure_future(channel.start())
    await fake_clock.run_until_idle()
    await settle(fake_clock, 15.0)
    with pytest.raises(DialRefused) as refused:
        await started
    assert refused.value.reason == "ready_timeout"


# -- requests -------------------------------------------------------------------------------------


@aio
async def test_a_swapped_transport_rejects_its_in_flight_calls(fake_clock: FakeClock) -> None:
    transports: list[ScriptedTransport] = []
    channel = scripted(fake_clock, transports)
    await channel.start()
    old = transports[0]
    call = asyncio.ensure_future(channel.request("session.resume", {"session_id": "s1"}))
    await fake_clock.run_until_idle()
    sent = [f for f in old.sent if f.get("method") == "session.resume"]
    assert len(sent) == 1 and isinstance(sent[0]["id"], int)
    await channel.drop()
    with pytest.raises(TransportLost, match="outcome is unknown"):
        await call
    await settle(fake_clock, 1.0)
    assert len(transports) == 2 and channel.connected
    # The old socket's late answer and close change nothing on the new one.
    old.push({"jsonrpc": "2.0", "id": sent[0]["id"], "result": {"session_id": "s1"}})
    await old.close(1006)
    await fake_clock.run_until_idle()
    assert channel.connected and transports[1].closed is None
    await channel.close()


@aio
async def test_a_request_while_disconnected_waits_for_the_next_ready_or_is_not_sent(
    fake_clock: FakeClock,
) -> None:
    transports: list[ScriptedTransport] = []
    channel = scripted(fake_clock, transports, request_timeout=5.0)
    await channel.start()
    await channel.drop()
    later = asyncio.ensure_future(channel.request("gateway.ping"))
    await fake_clock.run_until_idle()
    await settle(fake_clock, 1.0)
    frame = next(f for f in transports[1].sent if f.get("method") == "gateway.ping")
    transports[1].push({"jsonrpc": "2.0", "id": frame["id"], "result": {"pong": True}})
    assert (await later) == {"pong": True}
    await channel.close()
    with pytest.raises(NotSent):
        await channel.request("gateway.ping")


@aio
async def test_method_budgets_and_rpc_errors(fake_clock: FakeClock) -> None:
    limits = ChannelLimits()
    assert limits.timeout_for("prompt.submit") == 1800.0
    assert limits.timeout_for("server_request.answer") == 330.0
    assert limits.timeout_for("session.compress") == 660.0
    assert limits.timeout_for("session.create") == 120.0
    transports: list[ScriptedTransport] = []
    channel = scripted(fake_clock, transports)
    await channel.start()
    live = transports[0]
    slow = asyncio.ensure_future(channel.request("session.create", {}))
    await fake_clock.run_until_idle()
    for _ in range(7):  # 105 s, the pings answered so the socket stays up
        await settle(fake_clock, 15.0)
        live.push({"jsonrpc": "2.0", "id": live.pings()[-1]["id"], "result": {"pong": True}})
        await fake_clock.run_until_idle()
    await settle(fake_clock, 14.0)
    assert not slow.done()
    await settle(fake_clock, 1.0)
    with pytest.raises(TransportLost, match="did not answer session.create"):
        await slow
    failing = asyncio.ensure_future(channel.request("session.resume", {"session_id": "x"}))
    await fake_clock.run_until_idle()
    frame = live.sent[-1]
    nested = {"path": "session_id", "raw": {"deep": "dropped"}}
    live.push(
        {
            "jsonrpc": "2.0",
            "id": frame["id"],
            "error": {"code": -32002, "message": "gone", "data": nested},
        }
    )
    with pytest.raises(RpcError) as error:
        await failing
    assert error.value.code == -32002 and error.value.data == {"path": "session_id"}
    await channel.close()


@aio
async def test_an_unanswerable_server_request_gets_32601_at_once(fake_clock: FakeClock) -> None:
    transports: list[ScriptedTransport] = []
    channel = scripted(fake_clock, transports)
    seen: list[tuple[str, dict[str, Any]]] = []
    channel.on_notification(lambda method, params: seen.append((method, dict(params))))
    await channel.start()
    live = transports[0]
    live.push({"jsonrpc": "2.0", "id": "srq-0123456789ab", "method": "window.read", "params": {}})
    live.push({"jsonrpc": "2.0", "method": "message.delta", "params": {"text": "x"}})
    await fake_clock.run_until_idle()
    answer = next(f for f in live.sent if f.get("id") == "srq-0123456789ab")
    assert answer["error"]["code"] == -32601
    assert seen == [("message.delta", {"text": "x"})]
    await channel.close()


# -- against the real endpoint --------------------------------------------------------------------


@aio
async def test_each_reconnect_mints_a_new_ticket(
    scope: Scope, store: StoreHandle, tmp_path: Path, fake_clock: FakeClock
) -> None:
    r = await rig(scope, store, tmp_path)
    channel = Channel(r.dial, clock=fake_clock)
    await channel.start()
    assert channel.ready_params["instance_id"] == r.book.instance_id
    assert (await channel.request("gateway.ping"))["pong"] is True
    for _ in range(3):
        await r.last.kill()
        await eventually(lambda: channel.state == "reconnecting")
        await settle(fake_clock, 1.0)
        await eventually(lambda: channel.connected)
    assert r.dials == 4 and len(set(r.tickets)) == 4
    assert (await channel.request("gateway.ping"))["pong"] is True
    await channel.close()
    # A dialer that reused a spent ticket would be refused by the endpoint (4401).
    r.reuse_first_ticket = True
    again = Channel(r.dial, clock=fake_clock)
    with pytest.raises(DialRefused):
        await again.start()
    await r.close()


@aio
async def test_a_turn_streams_through_the_channel_and_an_interrupt_names_its_generation(
    scope: Scope, store: StoreHandle, tmp_path: Path, fake_clock: FakeClock
) -> None:
    holder = Holder()
    r = await rig(
        scope,
        store,
        tmp_path,
        tool_batch(("hold", {})),
        text_turn("stopped"),
        tools={"hold": holder},
    )
    channel = Channel(r.dial, clock=fake_clock)
    frames: list[tuple[str, dict[str, Any]]] = []
    channel.on_notification(lambda method, params: frames.append((method, dict(params))))
    await channel.start()
    sid = (await channel.request("session.create", {}))["session_id"]
    await channel.request("prompt.submit", {"session_id": sid, "text": "go"})
    await eventually(lambda: holder.entered)
    started = next(p for m, p in frames if m == "turn.started")
    stale = await channel.request(
        "session.interrupt", {"session_id": sid, "generation": started["generation"] - 1}
    )
    assert stale["interrupted"] is False
    hit = await channel.request(
        "session.interrupt", {"session_id": sid, "generation": started["generation"]}
    )
    assert hit["interrupted"] is True
    await eventually(lambda: any(m == "turn.done" for m, _ in frames))
    seqs = [p["seq"] for _, p in frames if "seq" in p]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    await channel.close()
    await r.close()


@aio
async def test_the_ticket_is_never_logged_nor_shown(
    scope: Scope,
    store: StoreHandle,
    tmp_path: Path,
    fake_clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    r = await rig(scope, store, tmp_path)
    channel = Channel(r.dial, clock=fake_clock)
    await channel.start()
    await r.last.kill()
    await settle(fake_clock, 1.0)
    await eventually(lambda: channel.connected)
    await channel.close()
    await r.close()
    text = caplog.text
    assert r.tickets and not any(ticket in text for ticket in r.tickets)
    grant = Grant("ws://127.0.0.1:9/ws", "t" * 43, "p", "i")
    assert "t" * 43 not in repr(grant)
    assert grant.subprotocols() == [SUBPROTOCOL, TICKET_PROTOCOL_PREFIX + "t" * 43]


def test_grant_for_refuses_without_a_daemon_and_on_an_unsafe_home(scope: Scope) -> None:
    with pytest.raises(DialRefused) as missing:
        grant_for(scope.home)
    assert missing.value.reason == "no_daemon" and not missing.value.terminal
    scope.home.chmod(0o777)
    try:
        with pytest.raises(DialRefused) as unsafe:
            grant_for(scope.home)
        assert unsafe.value.reason == "unsafe_home" and unsafe.value.terminal
    finally:
        scope.home.chmod(0o700)


@aio
async def test_the_home_dialer_recovers_a_missing_daemon_within_its_budget(
    scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    from indus.service.gateway_ensure import Endpoint, Ensured, RecoverBudget

    asked: list[str] = []
    grants = iter([None, Grant("ws://127.0.0.1:9/ws", "a" * 43, "p", "i")])

    def fake_grant(home: Path, **_: Any) -> Grant:
        grant = next(grants)
        if grant is None:
            raise DialRefused("no_daemon")
        return grant

    def fake_ensure(home: Path, env: Any, *, mode: str, budget: RecoverBudget) -> Ensured:
        asked.append(mode)
        assert budget.take()
        return Ensured("ready", endpoint=Endpoint("ws://127.0.0.1:9/ws", "p", "i", "0"))

    async def fake_connect(url: str, protocols: list[str]) -> Any:
        assert url == "ws://127.0.0.1:9/ws" and protocols[1].endswith("a" * 43)
        return ScriptedTransport()

    monkeypatch.setattr(channel_mod, "grant_for", fake_grant)
    monkeypatch.setattr(channel_mod, "ensure", fake_ensure)
    budget = RecoverBudget()
    dial = channel_mod.home_dialer(
        scope.home, env={"PATH": "/usr/bin"}, budget=budget, connect=fake_connect
    )
    dialed = await dial()
    assert asked == ["recover"] and dialed.profile_id == "p" and dialed.instance_id == "i"
    assert isinstance(dialed.transport, ScriptedTransport)


# -- the real wsproto client ----------------------------------------------------------------------


@aio
async def test_the_wsproto_client_attaches_to_a_real_loopback_listener(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    from indus.service.remote.ws_client import connect

    r = await rig(scope, store, tmp_path)
    url = await r.endpoint.start()
    try:
        ticket = r.book.mint("ws-interactive").ticket

        async def dial() -> Dialed:
            transport = await connect(url, [SUBPROTOCOL, TICKET_PROTOCOL_PREFIX + ticket])
            assert ticket not in repr(transport)
            return Dialed(transport, r.book.profile_id)

        channel = Channel(dial)
        await channel.start()
        assert (await channel.request("gateway.ping"))["pong"] is True
        sid = (await channel.request("session.create", {}))["session_id"]
        assert sid
        await channel.close()
        with pytest.raises(ValueError, match="loopback"):
            await connect("ws://example.com:80/ws", [SUBPROTOCOL])
        reused = Channel(dial)
        with pytest.raises(DialRefused):
            await reused.start()  # the ticket was spent by the first dial
    finally:
        await r.close()


def test_backoff_numbers() -> None:
    limits = ChannelLimits()
    assert [limits.backoff(n) for n in range(1, 9)] == [1, 2, 4, 8, 16, 30, 30, 30]
    assert isinstance(AsgiTransport, type)
