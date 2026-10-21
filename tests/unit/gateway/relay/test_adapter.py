"""Relay adapters and ops (P24-09): the shared contract suite over the fake connector, op gating,
the profile round-trip and the router's resolution rule."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from indus.cron import delivery as cron_delivery
from indus.cron.targets import DeliveryTarget
from indus.foundation.scope import Scope
from indus.gateway.adapters.base import (
    Adapter,
    AdapterConnectError,
    ApprovalAction,
    ApprovalPrompt,
    ClarifyPrompt,
    approval_callback,
    clarify_callback,
)
from indus.gateway.config import HomeChannel
from indus.gateway.core.events import Attachment, InboundEvent, MessageKind
from indus.gateway.core.send import ErrorKind, SendResult
from indus.gateway.delivery.router import DeliveryRouter
from indus.gateway.outbound.chunker import utf16_units
from indus.gateway.outbound.sender import OutboundSender
from indus.gateway.relay.adapter import (
    RelayAdapter,
    RelayFront,
    RelayMediaClient,
    create_relay_adapter,
    resolve_transport,
    to_send_result,
)
from indus.gateway.relay.descriptor import Descriptor
from indus.gateway.relay.transport import RelaySettings, RelayTransport
from tests.contract.gateway.conftest import (
    AdapterCase,
    CaseEnv,
    SocketGuard,
    run_check,
    settle,
    socket_guard,  # noqa: F401  (the autouse no-network guard, used here too)
)
from tests.contract.gateway.test_adapter_contract import CHECKS, check_no_real_network
from tests.fakes.clock import FakeClock
from tests.fakes.platform.core import FakeChatPlatform
from tests.fakes.platform.relay_wire import (
    BASE_OPS,
    GATEWAY_ID,
    URL,
    RelayWire,
    descriptor_for,
    shared_secret,
)
from tests.unit.gateway.outbound.recording import RecordingAdapter, recording_adapter

# -- the contract case -----------------------------------------------------------------------------


def settings() -> RelaySettings:
    return RelaySettings(url=URL, gateway_id=GATEWAY_ID, secret=shared_secret())


def _env(clock: FakeClock) -> CaseEnv:
    platform = FakeChatPlatform("telegram", clock=clock, max_len=4096, len_fn=utf16_units)
    wire = RelayWire(platform, descriptors=[descriptor_for(platform, len_unit="utf16")])
    return CaseEnv(clock, platform, wire)


def build_front(env: CaseEnv) -> RelayFront:
    wire: RelayWire = env.wire
    transport = RelayTransport(settings(), clock=env.clock, opener=wire.open)
    media = RelayMediaClient(
        URL, GATEWAY_ID, shared_secret(), clock=env.clock, transport=wire.media_transport()
    )
    return RelayFront(transport, media=media)


def _build(env: CaseEnv) -> Adapter:
    wire: RelayWire = env.wire
    return create_relay_adapter(build_front(env), wire.descriptors[0], clock=env.clock)


async def _pump(env: CaseEnv, adapter: Adapter) -> None:
    env.wire.push_inbound()


RELAY_CASE = AdapterCase(
    "relay",
    _env,
    _build,
    user_id="200",
    group_id="300",
    thread_id="400",
    bot_id="500",
    pump=_pump,
)


@pytest.mark.parametrize("rule", sorted(CHECKS))
def test_relay_adapter_passes_the_contract_suite(rule: str) -> None:
    run_check(RELAY_CASE, CHECKS[rule])


def test_relay_adapter_uses_no_real_network(socket_guard: SocketGuard) -> None:  # noqa: F811
    check_no_real_network(RELAY_CASE, socket_guard)


# -- a rig for the op tests ------------------------------------------------------------------------


class Rig:
    def __init__(self, ops: tuple[str, ...] | None = None) -> None:
        self.clock = FakeClock(datetime(2026, 3, 1, 9, 0, tzinfo=UTC))
        self.platform = FakeChatPlatform("telegram", clock=self.clock, max_len=4096)
        descriptor = (
            descriptor_for(self.platform, ops=ops) if ops else descriptor_for(self.platform)
        )
        self.wire = RelayWire(self.platform, descriptors=[descriptor])
        self.env = CaseEnv(self.clock, self.platform, self.wire)
        self.front = build_front(self.env)
        self.adapter: RelayAdapter = create_relay_adapter(self.front, descriptor, clock=self.clock)
        self.events: list[InboundEvent] = []

        async def sink(event: InboundEvent) -> None:
            self.events.append(event)

        self.adapter.set_sink(sink)

    async def settle(self, awaitable: Awaitable[Any]) -> Any:
        return await settle(self.env, awaitable)

    def ops(self) -> list[str]:
        return [str(a["op"]) for a in self.wire.actions]


def run(main: Callable[[Rig], Awaitable[None]], **kw: Any) -> None:
    async def outer() -> None:
        rig = Rig(**kw)
        try:
            await rig.settle(rig.adapter.connect())
            await main(rig)
        finally:
            await rig.adapter.disconnect()

    asyncio.run(outer())


# -- capabilities and lifecycle --------------------------------------------------------------------


def test_each_fronted_platform_gets_its_own_adapter_class_and_capabilities() -> None:
    platform = FakeChatPlatform("discord")
    env = CaseEnv(FakeClock(datetime(2026, 1, 1, tzinfo=UTC)), platform, RelayWire(platform))
    front = build_front(env)
    one = create_relay_adapter(front, Descriptor("discord", max_len=2000), clock=env.clock)
    two = create_relay_adapter(front, Descriptor("telegram", len_unit="utf16"), clock=env.clock)
    assert (one.platform, one.capabilities.max_len) == ("discord", 2000)
    assert (two.platform, two.capabilities.len_fn) == ("telegram", utf16_units)
    assert one.capabilities.auth_upstream and not one.capabilities.splits_natively
    assert RelayAdapter.platform == "relay"


def test_a_platform_the_connector_does_not_front_fails_to_connect() -> None:
    async def main() -> None:
        rig = Rig()
        other = create_relay_adapter(rig.front, Descriptor("slack"), clock=rig.clock)
        with pytest.raises(AdapterConnectError) as caught:
            await rig.settle(other.connect())
        assert not caught.value.retryable and caught.value.code == "relay_not_fronted"
        assert rig.front.transport.state == "closed"

    asyncio.run(main())


def test_two_adapters_share_one_socket() -> None:
    async def main() -> None:
        clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
        tg = FakeChatPlatform("telegram", clock=clock)
        wire = RelayWire(tg, descriptors=[descriptor_for(tg), Descriptor("discord", max_len=2000)])
        env = CaseEnv(clock, tg, wire)
        front = build_front(env)
        a = create_relay_adapter(front, wire.descriptors[0], clock=clock)
        b = create_relay_adapter(front, wire.descriptors[1], clock=clock)
        await settle(env, a.connect())
        await settle(env, b.connect())
        assert wire.opens == 1 and {x.platform for x in front.adapters()} == {"telegram", "discord"}
        await a.disconnect()
        assert front.transport.live
        await b.disconnect()
        assert front.transport.state == "closed"

    asyncio.run(main())


# -- op gating -------------------------------------------------------------------------------------


def test_ops_not_advertised_are_capability_refusals_and_never_reach_the_wire(
    tmp_path: Path,
) -> None:
    photo = tmp_path / "p.jpg"
    photo.write_bytes(b"\xff\xd8 jpeg")

    async def main(rig: Rig) -> None:
        results = [
            await rig.settle(rig.adapter.send_image("5", str(photo))),
            await rig.settle(rig.adapter.react("5", "1", "👀")),
            await rig.settle(rig.adapter.create_thread("5", "handoff")),
            await rig.settle(rig.adapter.rename_thread("5", "9", "new name")),
        ]
        for result in results:
            assert not result.ok and result.unsupported, result
            assert result.kind is None  # a refusal, not a classified failure
        info = await rig.settle(rig.adapter.chat_info("5"))
        assert info.name == "5"
        assert rig.ops() == [] and not rig.wire.uploads

    run(main, ops=BASE_OPS)


def test_without_prompt_approvals_and_clarify_fall_back_to_text() -> None:
    prompt = ApprovalPrompt(
        "req9",
        "k",
        "Run it?",
        "Run it?\nReply /approve or /deny.",
        (ApprovalAction("Yes", "once"),),
    )

    async def main(rig: Rig) -> None:
        assert not rig.adapter.capabilities.supports_buttons
        result = await rig.settle(rig.adapter.send_approval("5", prompt))
        assert result.ok and rig.ops() == ["send"]
        assert "/approve" in rig.platform.texts("5")[0]
        clarify = ClarifyPrompt("c1", "k", "Which?", ("a", "b"), fallback_text="Which?\n1. a\n2. b")
        await rig.settle(rig.adapter.send_clarify("5", clarify))
        assert rig.platform.texts("5")[1].startswith("Which?\n1.")

    run(main, ops=BASE_OPS)


def test_an_edit_op_withdrawn_by_a_new_descriptor_is_refused() -> None:
    async def main(rig: Rig) -> None:
        sent = await rig.settle(rig.adapter.send("5", "draft"))
        assert rig.wire.socket is not None
        rig.wire.socket.push(
            {
                "type": "descriptor",
                "descriptor": Descriptor(
                    "telegram", supports_edit=True, ops=frozenset({"send"})
                ).to_wire(),
            }
        )
        await rig.clock.run_until_idle()
        result = await rig.settle(rig.adapter.edit("5", sent.message_id, "final", final=True))
        assert result.unsupported and rig.ops() == ["send"]

    run(main)


def test_long_text_is_refused_locally_as_too_long() -> None:
    async def main(rig: Rig) -> None:
        result = await rig.settle(rig.adapter.send("5", "x" * 4097))
        assert result.kind is ErrorKind.TOO_LONG and rig.ops() == []

    run(main)


# -- advertised ops --------------------------------------------------------------------------------


def test_media_goes_by_reference_after_a_signed_upload(tmp_path: Path) -> None:
    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"%PDF-1.7 tiny")

    async def main(rig: Rig) -> None:
        result = await rig.settle(
            rig.adapter.send_document("5", str(doc), caption="here", reply_to="3")
        )
        assert result.ok, result
        upload = rig.wire.uploads[0]
        assert upload["filename"] == "report.pdf" and upload["kind"] == "document"
        action = rig.wire.actions[-1]
        assert action["op"] == "send_media" and action["ref"] == upload["ref"]
        assert action["caption"] == "here" and action["reply_to"] == "3"
        assert rig.wire.media[upload["ref"]] == b"%PDF-1.7 tiny"
        media = rig.platform.ops_of("media")
        assert media and media[0].extra["ref"] == "report.pdf"

    run(main)


def test_media_over_25_mb_or_empty_is_refused_before_upload(tmp_path: Path) -> None:
    big = tmp_path / "big.bin"
    with big.open("wb") as fh:
        fh.truncate(25 * 1024 * 1024 + 1)
    empty = tmp_path / "empty.bin"
    empty.write_bytes(b"")

    async def main(rig: Rig) -> None:
        for path in (big, empty, tmp_path / "missing.bin"):
            result = await rig.settle(rig.adapter.send_file("5", str(path)))
            assert not result.ok and not result.retryable
        assert not rig.wire.uploads and rig.ops() == []

    run(main)


def test_a_bad_media_signature_is_refused_by_the_connector(tmp_path: Path) -> None:
    doc = tmp_path / "a.txt"
    doc.write_text("hi")

    async def main(rig: Rig) -> None:
        rig.wire.secret = "-".join(("other", "value"))
        result = await rig.settle(rig.adapter.send_file("5", str(doc)))
        assert not result.ok and not rig.wire.uploads

    run(main)


def test_prompt_buttons_carry_our_callback_data() -> None:
    prompt = ApprovalPrompt(
        "req1",
        "k",
        "Run it?",
        "fallback",
        (ApprovalAction("Allow once", "once", "primary"), ApprovalAction("Deny", "deny", "danger")),
    )

    async def main(rig: Rig) -> None:
        result = await rig.settle(rig.adapter.send_approval("5", prompt))
        assert result.ok
        action = rig.wire.actions[-1]
        assert action["op"] == "prompt" and action["kind"] == "approval"
        assert [o["data"] for o in action["options"]] == [
            approval_callback("req1", "once"),
            approval_callback("req1", "deny"),
        ]
        assert action["options"][0]["style"] == "primary"
        clarify = ClarifyPrompt("cq", "k", "Pick one", ("red", "blue"))
        await rig.settle(rig.adapter.send_clarify("5", clarify))
        options = rig.wire.actions[-1]["options"]
        assert [o["data"] for o in options] == [
            clarify_callback("cq", 0),
            clarify_callback("cq", 1),
        ]

    run(main)


def test_react_threads_follow_up_and_chat_info_round_trip() -> None:
    async def main(rig: Rig) -> None:
        rig.platform.add_chat("5", "group", "Ops room")
        assert (await rig.settle(rig.adapter.react("5", "1", "👀"))).ok
        assert (await rig.settle(rig.adapter.react("5", "1", "👀", remove=True))).ok
        created = await rig.settle(rig.adapter.create_thread("5", "  hand   off  "))
        assert created.ok and created.message_id == "900"
        assert rig.wire.threads[0]["name"] == "hand off"
        renamed = await rig.settle(rig.adapter.rename_thread("5", "900", "Renamed"))
        assert renamed.ok and rig.wire.threads[1]["thread_id"] == "900"
        assert not (await rig.settle(rig.adapter.rename_thread("5", "900", "   "))).ok
        done = await rig.settle(
            rig.adapter.follow_up("5", "discord.interaction", "done", session_key="s1")
        )
        assert done.ok and rig.wire.follow_ups[0]["session_key"] == "s1"
        info = await rig.settle(rig.adapter.chat_info("5"))
        assert (info.name, info.type) == ("Ops room", "group")
        reactions = rig.platform.ops_of("reaction")
        assert [r.text for r in reactions] == ["👀", ""]

    run(main)


# -- inbound and the profile round-trip ------------------------------------------------------------


def test_the_serving_profile_round_trips_in_outbound_metadata() -> None:
    async def main(rig: Rig) -> None:
        rig.wire.deliver(
            {"chat_id": "77", "user_id": "u1", "text": "hi", "profile": "work", "scope_id": "g1"}
        )
        await rig.clock.run_until_idle()
        assert rig.events[0].metadata["profile"] == "work"
        assert rig.events[0].source.scope_id == "g1"
        await rig.settle(rig.adapter.send("77", "reply"))
        await rig.settle(rig.adapter.typing_start("77"))
        await rig.settle(rig.adapter.follow_up("77", "k", "x", session_key="s"))
        for action in rig.wire.actions:
            assert action["meta"]["profile"] == "work", action
            assert action["meta"]["scope_id"] == "g1" and action["meta"]["user_id"] == "u1"
        await rig.settle(rig.adapter.send("78", "elsewhere"))
        assert "profile" not in rig.wire.actions[-1]["meta"]

    run(main)


def test_inbound_events_are_normalised_and_authorised_upstream() -> None:
    async def main(rig: Rig) -> None:
        rig.wire.deliver(
            {
                "chat_id": "300",
                "chat_type": "group",
                "user_id": "9",
                "user_name": "Ana",
                "text": "",
                "message_id": "41",
                "media": [{"ref": "relay-media:x1", "kind": "voice", "mime": "audio/ogg"}],
                "mentioned": True,
                "reply_to": "40",
            }
        )
        rig.wire.deliver({"text": "no chat"})
        rig.wire.deliver({"chat_id": "1", "chat_type": "weird", "thread_id": "5", "text": "t"})
        await rig.clock.run_until_idle()
        voice, thread = rig.events
        assert voice.kind is MessageKind.VOICE and voice.media == (
            Attachment(ref="relay-media:x1", mime="audio/ogg"),
        )
        assert voice.reply_to_id == "40" and voice.metadata["mentioned"] and voice.reply_expected
        assert thread.source.chat_type == "thread" and thread.source.thread_id == "5"
        assert rig.adapter.capabilities.auth_upstream

    run(main)


def test_inbound_media_downloads_through_the_signed_endpoint() -> None:
    async def main(rig: Rig) -> None:
        rig.wire.media["relay-media:in7"] = b"OggS..."
        assert rig.front.media is not None
        chunks = [c async for c in rig.front.media.download("relay-media:in7")]
        assert b"".join(chunks) == b"OggS..."
        with pytest.raises(ValueError):
            [c async for c in rig.front.media.download("https://elsewhere/x")]

    run(main)


def test_events_for_a_platform_without_an_adapter_are_dropped() -> None:
    async def main(rig: Rig) -> None:
        rig.wire.deliver({"chat_id": "1", "text": "for slack"}, platform="slack")
        await rig.clock.run_until_idle()
        assert rig.events == []

    run(main)


def test_a_revoked_relay_reports_fatal_to_its_adapters() -> None:
    async def main(rig: Rig) -> None:
        fatal: list[tuple[str, str]] = []
        rig.adapter.set_fatal_listener(lambda code, msg: fatal.append((code, msg)))
        rig.wire.revoke()
        rig.wire.drop()
        await rig.clock.run_until_idle()
        await rig.clock.advance_and_settle(1)
        assert fatal and fatal[0][0] == "relay_refused"

    run(main)


def test_interrupts_reach_the_front_callback() -> None:
    async def main(rig: Rig) -> None:
        seen: list[tuple[str, str, str | None]] = []

        async def on_interrupt(platform: str, chat: str, key: str | None) -> None:
            seen.append((platform, chat, key))

        rig.front.on_interrupt = on_interrupt
        rig.wire.interrupt("42")
        await rig.clock.run_until_idle()
        assert seen == [("telegram", "42", None)]

    run(main)


# -- result codes ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("result", "check"),
    [
        ({"ok": True, "message_id": 12}, lambda r: r.ok and r.message_id == "12"),
        ({"ok": True}, lambda r: r.ok and r.message_id is None),
        ({"ok": False, "code": "unsupported"}, lambda r: r.unsupported and r.kind is None),
        (
            {"ok": False, "code": "unavailable"},
            lambda r: r.kind is ErrorKind.TRANSIENT and r.retryable,
        ),
        ({"ok": False, "code": "timeout"}, lambda r: r.maybe_delivered and not r.retryable),
        (
            {"ok": False, "code": "rate_limited", "retry_after": 3},
            lambda r: r.kind is ErrorKind.RATE_LIMITED and r.retry_after == 3,
        ),
        ({"ok": False, "code": "chat_not_found"}, lambda r: r.chat_unreachable),
        (
            {"ok": False, "code": "message_not_found", "error": "forbidden words"},
            lambda r: r.kind is ErrorKind.NOT_FOUND and not r.chat_unreachable,
        ),
        ({"ok": False, "code": "declined"}, lambda r: r.kind is ErrorKind.FORBIDDEN),
        ({"ok": False, "code": "bad_format"}, lambda r: r.kind is ErrorKind.BAD_FORMAT),
        ({"ok": False, "code": "too_long"}, lambda r: r.kind is ErrorKind.TOO_LONG),
        ({"ok": False, "code": "???"}, lambda r: r.kind is ErrorKind.UNKNOWN and not r.retryable),
    ],
)
def test_result_codes_map_onto_the_send_classifier(
    result: dict[str, Any], check: Callable[[SendResult], bool]
) -> None:
    assert check(to_send_result(result))


# -- the router rule -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("native", "fronts", "expected"),
    [
        (True, True, "native"),
        (True, False, "native"),
        (False, True, "relay"),
        (False, False, None),
        (None, True, "relay"),
        (None, False, None),
    ],
)
def test_router_resolution_table(native: bool | None, fronts: bool, expected: str | None) -> None:
    assert resolve_transport(native, fronts) == expected


@pytest.fixture
def router(scope: Scope) -> Iterator[tuple[DeliveryRouter, dict[str, bool | None]]]:
    states: dict[str, bool | None] = {}
    built = DeliveryRouter(
        scope,
        homes=lambda name: HomeChannel(name, "-1"),
        native_state=lambda name: states.get(name),
    )
    yield built, states
    built.close()


def _sender(scope: Scope, name: str) -> OutboundSender:
    adapter = recording_adapter(name=name, markup="plain")
    return OutboundSender(adapter, scope=scope, platform=name, rng=lambda: 0.0)


def test_the_delivery_router_follows_the_rule(
    scope: Scope, router: tuple[DeliveryRouter, dict[str, bool | None]]
) -> None:
    built, states = router
    native, relay = _sender(scope, "telegram"), _sender(scope, "telegram")
    built.attach("telegram", relay, via_relay=True)
    assert built.sender_for("telegram") is relay  # not configured natively
    states["telegram"] = False
    assert built.sender_for("telegram") is relay  # explicitly disabled never shadows
    states["telegram"] = True
    assert built.sender_for("telegram") is None  # enabled native wins even while down
    built.attach("telegram", native)
    assert built.sender_for("telegram") is native
    assert built.attached() == ["telegram"]
    built.detach("telegram")
    assert cron_delivery.deliverer_for("telegram") is not None  # the relay still serves it
    states["telegram"] = None
    assert built.sender_for("telegram") is relay
    built.detach("telegram", via_relay=True)
    assert cron_delivery.deliverer_for("telegram") is None
    assert built.sender_for("telegram") is None


def test_a_relay_platform_is_delivered_to(
    scope: Scope, router: tuple[DeliveryRouter, dict[str, bool | None]]
) -> None:
    built, _ = router
    sender = _sender(scope, "discord")
    built.attach("discord", sender, via_relay=True)
    receipt = asyncio.run(built.send(DeliveryTarget("discord", "55"), "hello"))
    assert receipt.ok
    recorded = sender.adapter
    assert isinstance(recorded, RecordingAdapter)
    assert [op.text for op in recorded.ops if op.op == "send"] == ["hello"]


def test_delete_is_used_only_when_advertised() -> None:
    async def main(rig: Rig) -> None:
        assert rig.adapter.capabilities.supports_delete
        sent = await rig.settle(rig.adapter.send("5", "temporary"))
        assert sent.message_id is not None
        assert await rig.settle(rig.adapter.delete("5", sent.message_id))
        assert rig.platform.ops_of("delete")

    run(main)

    async def without(rig: Rig) -> None:
        assert not rig.adapter.capabilities.supports_delete
        assert not await rig.settle(rig.adapter.delete("5", "1"))
        assert "delete" not in rig.ops()

    run(without, ops=BASE_OPS)
