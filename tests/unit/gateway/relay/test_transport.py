"""The relay connector transport (P24-08) against the fake connector (``relay_wire``)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from indus.gateway.outbound.chunker import code_points, utf16_units
from indus.gateway.relay.descriptor import (
    BASE_OPS,
    DEFAULT_MAX_LEN,
    Descriptor,
    DescriptorError,
)
from indus.gateway.relay.transport import (
    NonceMemory,
    RelayConnectError,
    RelaySettings,
    RelayTransport,
    backoff_delays,
    dial_url,
    make_hello,
    media_base_url,
    sign_hello,
    verify_hello,
)
from tests.fakes.clock import FakeClock
from tests.fakes.platform.core import FakeChatPlatform
from tests.fakes.platform.relay_wire import (
    GATEWAY_ID,
    URL,
    FakeRelaySocket,
    RelayWire,
    descriptor_for,
    shared_secret,
)

START = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)


def wire_descriptor(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"contract_version": 1, "platform": "telegram", "label": "Telegram"}
    base.update(fields)
    return base


# -- the descriptor --------------------------------------------------------------------------------


def test_max_len_zero_reads_as_4096() -> None:
    assert Descriptor.parse(wire_descriptor(max_len=0)).max_len == DEFAULT_MAX_LEN == 4096


@pytest.mark.parametrize("raw", [-5, "lots", None, True, 2.5, [3]])
def test_a_bad_max_len_reads_as_4096(raw: object) -> None:
    assert Descriptor.parse(wire_descriptor(max_len=raw)).max_len == 4096


def test_a_good_descriptor_is_read_field_by_field() -> None:
    d = Descriptor.parse(
        wire_descriptor(
            max_len=2000,
            len_unit="utf16",
            supports_draft=True,
            supports_edit=True,
            supports_threads=True,
            markdown_dialect="MarkdownV2",
            block_formatting=True,
            ops=["send", "edit", "prompt", "react", "brand_new_op"],
            unknown_future_field={"x": 1},
        )
    )
    assert (d.platform, d.label, d.max_len, d.len_unit) == ("telegram", "Telegram", 2000, "utf16")
    assert d.supports_draft and d.supports_edit and d.supports_threads and d.block_formatting
    assert d.markup == "markdownv2"
    assert d.supports("prompt") and d.supports("react") and not d.supports("send_media")
    caps = d.capabilities()
    assert caps.max_len == 2000 and caps.len_fn is utf16_units
    assert caps.supports_edit and caps.supports_buttons and caps.auth_upstream
    assert not caps.splits_natively and caps.dm_is_private


def test_missing_ops_mean_the_base_set_and_new_ops_need_advertising() -> None:
    d = Descriptor.parse(wire_descriptor())
    assert d.ops == BASE_OPS == {"send", "edit", "typing", "follow_up"}
    assert not d.supports("send_media") and not d.supports("prompt")
    assert Descriptor.parse(wire_descriptor(ops=[])).ops == BASE_OPS
    assert Descriptor.parse(wire_descriptor(ops="send")).ops == BASE_OPS


def test_an_unknown_length_unit_reads_as_the_stricter_utf16() -> None:
    assert Descriptor.parse(wire_descriptor(len_unit="bytes")).len_fn is utf16_units
    assert Descriptor.parse(wire_descriptor(len_unit="chars")).len_fn is code_points


def test_edits_need_both_the_flag_and_the_op() -> None:
    no_op = Descriptor.parse(wire_descriptor(supports_edit=True, ops=["send", "typing"]))
    assert not no_op.capabilities().supports_edit
    no_flag = Descriptor.parse(wire_descriptor(supports_edit=False))
    assert not no_flag.capabilities().supports_edit


@pytest.mark.parametrize(
    "raw",
    [
        wire_descriptor(contract_version=2),
        wire_descriptor(contract_version=None),
        wire_descriptor(contract_version=True),
        wire_descriptor(platform="Telegram"),
        wire_descriptor(platform="../etc"),
        wire_descriptor(platform=""),
        ["not", "an", "object"],
    ],
)
def test_unreadable_descriptors_are_refused(raw: object) -> None:
    with pytest.raises(DescriptorError):
        Descriptor.parse(raw)


def test_descriptor_round_trips_through_the_wire_form() -> None:
    d = descriptor_for(FakeChatPlatform("telegram", max_len=4096), len_unit="utf16")
    assert Descriptor.parse(d.to_wire()) == d


# -- signing ---------------------------------------------------------------------------------------


def test_the_hello_is_an_hmac_over_gateway_timestamp_and_nonce() -> None:
    secret = shared_secret()
    hello = make_hello("gw-1", secret, now=START, nonce="ab" * 16)
    assert hello["ts"] == int(START.timestamp()) and hello["nonce"] == "ab" * 16
    assert hello["sig"] == sign_hello(secret, "gw-1", hello["ts"], "ab" * 16)
    assert len(hello["sig"]) == 64 and secret not in json.dumps(hello)
    assert sign_hello(secret, "gw-1", hello["ts"], "cd" * 16) != hello["sig"]


def test_verify_refuses_bad_signature_stale_time_and_replayed_nonce() -> None:
    secret = shared_secret()
    seen = NonceMemory()
    good = make_hello("gw-1", secret, now=START, nonce="01" * 16)
    assert verify_hello(good, secret, now=START, seen=seen) is None
    assert verify_hello(good, secret, now=START, seen=seen) == "nonce already used"
    wrong = make_hello("gw-1", "-".join(("other", "value")), now=START, nonce="02" * 16)
    assert verify_hello(wrong, secret, now=START, seen=seen) == "bad signature"
    stale = make_hello("gw-1", secret, now=START - timedelta(seconds=301), nonce="03" * 16)
    assert verify_hello(stale, secret, now=START, seen=seen) == "timestamp outside the window"
    edge = make_hello("gw-1", secret, now=START - timedelta(seconds=300), nonce="04" * 16)
    assert verify_hello(edge, secret, now=START, seen=seen) is None
    assert verify_hello({**good, "v": 2}, secret, now=START, seen=seen) is not None


def test_urls() -> None:
    assert dial_url("https://relay.example") == "wss://relay.example/relay"
    assert dial_url("http://127.0.0.1:8080/") == "ws://127.0.0.1:8080/relay"
    assert dial_url("wss://relay.example/relay") == "wss://relay.example/relay"
    assert media_base_url("https://relay.example") == "https://relay.example/relay/media"
    with pytest.raises(ValueError):
        dial_url("ftp://relay.example")


def test_backoff_doubles_from_one_to_a_sixty_second_ceiling() -> None:
    gen = backoff_delays()
    assert [next(gen) for _ in range(9)] == [1, 2, 4, 8, 16, 32, 60, 60, 60]


# -- the rig ---------------------------------------------------------------------------------------


class Rig:
    def __init__(self, **wire_kw: Any) -> None:
        self.clock = FakeClock(START)
        self.platform = FakeChatPlatform("telegram", clock=self.clock, max_len=4096)
        self.wire = RelayWire(self.platform, **wire_kw)
        self.settings = RelaySettings(url=URL, gateway_id=GATEWAY_ID, secret=shared_secret())
        self.transport = RelayTransport(self.settings, clock=self.clock, opener=self.opener)
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.dials: list[float] = []
        self.fatal: list[tuple[str, str]] = []
        self.fail_events = False

        async def on_event(platform: str, event: Mapping[str, Any]) -> None:
            if self.fail_events:
                raise RuntimeError("handler broke")
            self.events.append((platform, dict(event)))

        self.transport.set_event_handler(on_event)
        self.transport.set_fatal_handler(lambda code, msg: self.fatal.append((code, msg)))

    @contextlib.asynccontextmanager
    async def opener(self, url: str) -> AsyncIterator[FakeRelaySocket]:
        self.dials.append(self.clock.monotonic())
        async with self.wire.open(url) as socket:
            yield socket

    async def settle(self, awaitable: Awaitable[Any], step: float = 0.5) -> Any:
        task = asyncio.ensure_future(awaitable)
        for _ in range(2000):
            await self.clock.run_until_idle()
            if task.done():
                return task.result()
            self.clock.advance(step)
        raise AssertionError("never finished")

    async def idle(self) -> None:
        await self.clock.run_until_idle()

    async def step(self, seconds: float) -> None:
        await self.clock.advance_and_settle(seconds)


def run(main: Callable[[Rig], Awaitable[None]], **kw: Any) -> None:
    async def outer() -> None:
        rig = Rig(**kw)
        try:
            await main(rig)
        finally:
            await rig.transport.stop()

    asyncio.run(outer())


# -- handshake -------------------------------------------------------------------------------------


def test_start_authenticates_and_collects_descriptors() -> None:
    async def main(rig: Rig) -> None:
        await rig.settle(rig.transport.start())
        assert rig.transport.live and rig.transport.fronts("telegram")
        hello = rig.wire.hellos[0]
        assert hello["gateway_id"] == GATEWAY_ID and not rig.wire.refusals
        assert shared_secret() not in json.dumps(hello)
        assert rig.transport.descriptor("telegram") == rig.wire.descriptors[0]
        await rig.settle(rig.transport.start())  # idempotent while running
        assert rig.wire.opens == 1

    run(main)


def test_a_fake_connector_with_max_len_zero_gives_4096() -> None:
    raw = wire_descriptor(platform="discord", max_len=0, len_unit="chars", ops=["send"])

    async def main(rig: Rig) -> None:
        await rig.settle(rig.transport.start())
        found = rig.transport.descriptor("discord")
        assert found is not None and found.max_len == 4096
        assert found.capabilities().max_len == 4096

    run(main, raw_descriptors=[raw])


def test_a_refused_descriptor_leaves_only_the_good_platforms() -> None:
    bad = wire_descriptor(platform="slack", contract_version=7)

    async def main(rig: Rig) -> None:
        await rig.settle(rig.transport.start())
        assert rig.transport.fronts("telegram") and not rig.transport.fronts("slack")

    run(main, raw_descriptors=[bad])


def test_bad_hmac_is_rejected_by_the_fake_and_start_is_not_retryable() -> None:
    async def main(rig: Rig) -> None:
        rig.wire.revoke()
        with pytest.raises(RelayConnectError) as caught:
            await rig.settle(rig.transport.start())
        assert not caught.value.retryable
        assert rig.wire.refusals == ["bad signature"]
        assert shared_secret() not in str(caught.value)
        assert not rig.transport.live

    run(main)


def test_a_refused_socket_is_a_retryable_start_failure() -> None:
    async def main(rig: Rig) -> None:
        rig.wire.refuse = True
        with pytest.raises(RelayConnectError) as caught:
            await rig.settle(rig.transport.start())
        assert caught.value.retryable
        rig.wire.refuse = False
        await rig.settle(rig.transport.start())
        assert rig.transport.live

    run(main)


def test_no_ready_within_the_connect_timeout_fails_the_start() -> None:
    async def main(rig: Rig) -> None:
        rig.wire.descriptors = []
        original = rig.wire.handle

        async def silent(socket: FakeRelaySocket, frame: dict[str, Any]) -> None:
            if frame.get("type") != "hello":
                await original(socket, frame)

        rig.wire.handle = silent  # type: ignore[method-assign]
        task = asyncio.ensure_future(rig.transport.start())
        await rig.idle()
        await rig.step(29)
        assert not task.done()
        await rig.step(2)
        with pytest.raises(RelayConnectError):
            await task

    run(main)


def test_missing_settings_are_not_retryable() -> None:
    async def main(rig: Rig) -> None:
        empty = RelayTransport(
            RelaySettings(url=URL, gateway_id=GATEWAY_ID, secret=""), clock=rig.clock
        )
        with pytest.raises(RelayConnectError) as caught:
            await empty.start()
        assert not caught.value.retryable and "INDUS_RELAY_SECRET" in str(caught.value)
        bad_url = RelayTransport(
            RelaySettings(url="ftp://x", gateway_id=GATEWAY_ID, secret=shared_secret()),
            clock=rig.clock,
        )
        with pytest.raises(RelayConnectError):
            await bad_url.start()

    run(main)


def test_settings_repr_hides_the_secret() -> None:
    settings = RelaySettings(url=URL, gateway_id=GATEWAY_ID, secret=shared_secret())
    assert shared_secret() not in repr(settings)


# -- requests --------------------------------------------------------------------------------------


def test_a_request_is_answered_by_its_result() -> None:
    async def main(rig: Rig) -> None:
        await rig.settle(rig.transport.start())
        result = await rig.settle(
            rig.transport.request("telegram", {"op": "send", "chat_id": "5", "text": "hi"})
        )
        assert result["ok"] is True and result["message_id"]
        assert rig.platform.texts("5") == ["hi"]
        assert rig.wire.actions[0]["_platform"] == "telegram"

    run(main)


def test_an_unanswered_request_times_out_as_maybe_delivered() -> None:
    async def main(rig: Rig) -> None:
        await rig.settle(rig.transport.start())
        rig.wire.mute = True
        task = asyncio.ensure_future(
            rig.transport.request("telegram", {"op": "send", "chat_id": "5", "text": "hi"})
        )
        await rig.idle()
        await rig.step(29)
        assert not task.done()
        await rig.step(2)
        result = task.result()
        assert result["ok"] is False and result["code"] == "timeout"

    run(main)


def test_a_request_while_disconnected_is_definitely_not_sent() -> None:
    async def main(rig: Rig) -> None:
        result = await rig.transport.request("telegram", {"op": "send", "chat_id": "5"})
        assert result == {
            "ok": False,
            "code": "unavailable",
            "error": "relay: the relay is not connected (connection refused)",
        }
        assert not rig.wire.actions

    run(main)


def test_a_dropped_socket_fails_waiting_requests_as_maybe_delivered() -> None:
    async def main(rig: Rig) -> None:
        await rig.settle(rig.transport.start())
        rig.wire.mute = True
        task = asyncio.ensure_future(rig.transport.request("telegram", {"op": "typing"}))
        await rig.idle()
        rig.wire.drop()
        await rig.idle()
        assert task.done() and task.result()["code"] == "timeout"

    run(main)


def test_interrupt_frames_reach_the_handler_and_ours_reach_the_connector() -> None:
    seen: list[tuple[str, str, str | None]] = []

    async def main(rig: Rig) -> None:
        async def on_interrupt(platform: str, chat_id: str, key: str | None) -> None:
            seen.append((platform, chat_id, key))

        rig.transport.set_interrupt_handler(on_interrupt)
        await rig.settle(rig.transport.start())
        rig.wire.interrupt("42", session_key="k1")
        await rig.idle()
        assert seen == [("telegram", "42", "k1")]
        assert await rig.transport.interrupt("telegram", "42", reason="stop")
        assert rig.wire.interrupts == [
            {"type": "interrupt", "platform": "telegram", "chat_id": "42", "reason": "stop"}
        ]

    run(main)


def test_malformed_and_unknown_frames_are_skipped(caplog: pytest.LogCaptureFixture) -> None:
    async def main(rig: Rig) -> None:
        await rig.settle(rig.transport.start())
        assert rig.wire.socket is not None
        rig.wire.socket.push("not json {")
        rig.wire.socket.push("[1, 2]")
        rig.wire.socket.push({"type": "from_the_future"})
        rig.wire.deliver({"chat_id": "1", "text": "still here"})
        await rig.idle()
        assert rig.transport.live and [e["text"] for _, e in rig.events] == ["still here"]

    caplog.set_level(logging.WARNING)
    run(main)
    assert "not JSON" in caplog.text


# -- reconnect -------------------------------------------------------------------------------------


def test_reconnect_backoff_doubles_from_one_to_sixty_and_resets_after_success() -> None:
    async def main(rig: Rig) -> None:
        await rig.settle(rig.transport.start())
        rig.wire.refuse = True
        rig.wire.drop()
        await rig.idle()
        for _ in range(8):
            await rig.step(60)
        assert list(rig.transport.recent_delays)[:8] == [1, 2, 4, 8, 16, 32, 60, 60]
        gaps = [round(b - a, 3) for a, b in zip(rig.dials, rig.dials[1:], strict=False)]
        assert gaps[1:4] == [60.0, 60.0, 60.0]  # the refused dials, one per step
        rig.wire.refuse = False
        await rig.step(60)
        assert rig.transport.live
        rig.transport.recent_delays.clear()
        rig.wire.drop()
        await rig.idle()
        await rig.step(1)
        assert list(rig.transport.recent_delays) == [1], rig.transport.recent_delays
        assert rig.transport.live, (rig.dials, rig.clock.monotonic())

    run(main)


def test_a_drop_redials_after_one_second_and_keeps_working() -> None:
    async def main(rig: Rig) -> None:
        await rig.settle(rig.transport.start())
        rig.wire.drop()
        await rig.idle()
        assert not rig.transport.live
        await rig.step(0.9)
        assert rig.wire.opens == 1
        await rig.step(0.2)
        assert rig.wire.opens == 2 and rig.transport.live
        result = await rig.settle(
            rig.transport.request("telegram", {"op": "typing", "chat_id": "1"})
        )
        assert result["ok"] is True

    run(main)


def test_revoked_credentials_after_success_are_fatal_and_never_retried() -> None:
    async def main(rig: Rig) -> None:
        await rig.settle(rig.transport.start())
        rig.wire.revoke()
        rig.wire.drop()
        await rig.idle()
        await rig.step(1)
        assert rig.fatal and rig.fatal[0][0] == "relay_refused"
        assert rig.transport.state == "refused"
        opens = rig.wire.opens
        await rig.step(120)
        assert rig.wire.opens == opens

    run(main)


def test_stop_closes_the_socket_and_stops_redialling() -> None:
    async def main(rig: Rig) -> None:
        await rig.settle(rig.transport.start())
        await rig.transport.stop()
        assert rig.platform.ops_of("disconnect") and rig.transport.state == "closed"
        await rig.step(120)
        assert rig.wire.opens == 1

    run(main)


def test_shared_use_starts_once_and_stops_with_the_last_user() -> None:
    async def main(rig: Rig) -> None:
        await rig.settle(rig.transport.acquire())
        await rig.settle(rig.transport.acquire())
        assert rig.wire.opens == 1
        await rig.transport.release()
        assert rig.transport.live
        await rig.transport.release()
        assert rig.transport.state == "closed"

    run(main)


# -- dormant mode ----------------------------------------------------------------------------------


def test_dormant_buffering_replays_in_order_and_acks_each_once() -> None:
    async def main(rig: Rig) -> None:
        await rig.settle(rig.transport.start())
        assert await rig.settle(rig.transport.go_dormant())
        assert rig.transport.state == "dormant" and rig.wire.socket is None
        for n in range(5):
            rig.wire.deliver({"chat_id": "7", "text": f"m{n}"})
        assert len(rig.wire.buffer) == 5
        await rig.step(300)
        assert rig.wire.opens == 1  # no re-dial while dormant
        await rig.transport.wake()
        await rig.step(0)
        await rig.idle()
        assert rig.transport.live and rig.wire.opens == 2
        assert [e["text"] for _, e in rig.events] == [f"m{n}" for n in range(5)]
        assert rig.wire.acks == ["b1", "b2", "b3", "b4", "b5"] and not rig.wire.buffer

    run(main)


def test_a_failing_handler_leaves_the_event_unacked_for_replay() -> None:
    async def main(rig: Rig) -> None:
        await rig.settle(rig.transport.start())
        assert await rig.settle(rig.transport.go_dormant())
        rig.wire.deliver({"chat_id": "7", "text": "keep me"})
        rig.fail_events = True
        await rig.transport.wake()
        await rig.idle()
        assert rig.wire.acks == [] and len(rig.wire.buffer) == 1
        rig.fail_events = False
        rig.wire.drop()
        await rig.idle()
        await rig.step(1)
        assert [e["text"] for _, e in rig.events] == ["keep me"] and rig.wire.acks == ["b1"]

    run(main)


def test_a_replay_already_handled_is_acked_but_not_delivered_twice() -> None:
    async def main(rig: Rig) -> None:
        await rig.settle(rig.transport.start())
        assert rig.wire.socket is not None
        frame = {
            "type": "event",
            "platform": "telegram",
            "event": {"text": "once"},
            "buffer_id": "x9",
        }
        rig.wire.socket.push(frame)
        rig.wire.socket.push(frame)
        await rig.idle()
        assert [e["text"] for _, e in rig.events] == ["once"]
        assert rig.wire.acks == ["x9", "x9"]

    run(main)


def test_go_dormant_without_an_ack_stays_connected() -> None:
    async def main(rig: Rig) -> None:
        await rig.settle(rig.transport.start())
        original = rig.wire.handle

        async def no_ack(socket: FakeRelaySocket, frame: dict[str, Any]) -> None:
            if frame.get("type") != "idle":
                await original(socket, frame)

        rig.wire.handle = no_ack  # type: ignore[method-assign]
        assert await rig.settle(rig.transport.go_dormant()) is False
        assert rig.transport.live and rig.transport.state == "live"

    run(main)


def test_go_dormant_when_not_connected_is_false() -> None:
    async def main(rig: Rig) -> None:
        assert await rig.transport.go_dormant() is False

    run(main)


def test_stop_lets_an_operation_already_sent_be_answered() -> None:
    async def main(rig: Rig) -> None:
        await rig.settle(rig.transport.start())
        rig.wire.mute = True
        pending = asyncio.ensure_future(
            rig.transport.request("telegram", {"op": "send", "chat_id": "5", "text": "last"})
        )
        await rig.idle()
        assert rig.wire.socket is not None
        socket = rig.wire.socket
        stopping = asyncio.ensure_future(rig.transport.stop())
        await rig.idle()
        assert not stopping.done()
        sent = socket.sent[-1]
        socket.push({"type": "result", "id": sent["id"], "result": {"ok": True, "message_id": "9"}})
        await rig.idle()
        assert pending.result() == {"ok": True, "message_id": "9"}
        await rig.settle(stopping)
        assert rig.transport.state == "closed"

    run(main)


def test_stop_gives_up_on_an_unanswered_operation_after_two_seconds() -> None:
    async def main(rig: Rig) -> None:
        await rig.settle(rig.transport.start())
        rig.wire.mute = True
        pending = asyncio.ensure_future(rig.transport.request("telegram", {"op": "typing"}))
        await rig.idle()
        stopping = asyncio.ensure_future(rig.transport.stop())
        await rig.idle()
        await rig.step(1.9)
        assert not stopping.done()
        await rig.step(0.2)
        assert stopping.done() and pending.result()["ok"] is False

    run(main)
