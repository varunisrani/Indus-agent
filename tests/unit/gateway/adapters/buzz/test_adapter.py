"""The Buzz adapter (P16-29) over the fake relay, with the fake signer."""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from indus.foundation.config.schema import PlatformSection
from indus.foundation.scope import Scope
from indus.gateway.adapters._identity import hex_to_npub
from indus.gateway.adapters.base import AdapterConnectError
from indus.gateway.adapters.buzz import create_adapter
from indus.gateway.adapters.buzz.adapter import (
    BAD_CAP,
    DISCOVER_EVERY,
    FETCH_LIMIT,
    MEDIA_REF,
    MEMBERS_TTL,
    SEEN_CAP,
    BuzzAdapter,
    BuzzOptions,
    thread_of,
)
from indus.gateway.config import AdapterConfig, PlatformSettings
from indus.gateway.core.authz import Authorizer, PlatformPolicy
from indus.gateway.core.events import Attachment, InboundEvent, MessageKind, Source
from indus.gateway.core.send import ErrorKind
from indus.gateway.intake.media import MediaRejected
from tests.contract.gateway.buzz_case import GROUP, USER, build_adapter
from tests.fakes.clock import FakeClock
from tests.fakes.platform.buzz_wire import BOT_KEY, BuzzRelay, FakeNostrCrypto, dm_conversation
from tests.fakes.platform.core import FailureKind, FakeChatPlatform

ANA = "a1" * 32
BO = "b0" * 32


class Rig:
    def __init__(self, *, require_auth: bool = True, **kw: Any) -> None:
        self.clock = FakeClock(datetime(2026, 3, 1, 9, 0, tzinfo=UTC))
        self.platform = FakeChatPlatform("buzz", clock=self.clock, max_len=16000)
        self.platform.add_chat(GROUP, "group", "general")
        self.relay = BuzzRelay(self.platform, require_auth=require_auth)
        self.adapter = build_adapter(self.relay, self.clock, **kw)
        self.events: list[InboundEvent] = []

        async def sink(event: InboundEvent) -> None:
            self.events.append(event)

        self.adapter.set_sink(sink)

    async def step(self, seconds: float = 1.0, times: int = 1) -> None:
        for _ in range(times):
            await self.clock.advance_and_settle(seconds)

    async def settle(self, awaitable: Awaitable[Any]) -> Any:
        task = asyncio.ensure_future(awaitable)
        for _ in range(400):
            await self.clock.run_until_idle()
            if task.done():
                return task.result()
            self.clock.advance(0.5)
        raise AssertionError("never finished")

    def post(
        self,
        author: str,
        text: str,
        *,
        conv: str = GROUP,
        kind: int = 9,
        tags: list[list[str]] | None = None,
        name: str = "",
        bot: bool = False,
        at: int | None = None,
    ) -> dict[str, Any]:
        self.relay.profiles[author] = (name, bot)
        stamp = at if at is not None else self.relay.now()
        event = self.relay.sign_as(author, kind, [["h", conv], *(tags or [])], text, stamp)
        self.relay.chats.append(event)
        return event

    def requests_of(self, kind: int) -> list[dict[str, Any]]:
        return [r for r in self.relay.requests if r.get("kinds") == [kind]]


def run(main: Callable[[Rig], Awaitable[None]], **kw: Any) -> None:
    async def outer() -> None:
        rig = Rig(**kw)
        try:
            await main(rig)
        finally:
            await rig.adapter.disconnect()

    asyncio.run(outer())


# -- allowlists ------------------------------------------------------------------------------------


def test_an_npub_allowlist_entry_authorises_the_matching_hex_pubkey() -> None:
    npub = hex_to_npub(ANA)
    clock = FakeClock(datetime(2026, 3, 1, tzinfo=UTC))
    adapter = BuzzAdapter(
        clock=clock,
        relay_url="https://r.test",
        private_key=BOT_KEY,
        allowlist=[npub, "  ", "not-a-key"],
    )
    assert adapter.resolved_allowlist_ids() == {ANA}
    like: Any = adapter  # the authorizer's adapter protocol (``AdapterLike``)
    env = {"INDUS_BUZZ_ALLOWED_USERS": npub}
    authorizer = Authorizer(
        policy_for=lambda p: PlatformPolicy(p, allowlist_env="INDUS_BUZZ_ALLOWED_USERS"),
        env=env.get,
        adapter_for=lambda _p: like,
    )

    def source(user: str) -> Source:
        return Source(platform="buzz", chat_id="dm-1", user_id=user, user_id_alt=hex_to_npub(user))

    assert authorizer.check(source(ANA)).granted
    assert not authorizer.check(source(BO)).granted
    # A config allowlist (no env list) matches the npub through the alternate id.
    config_only = Authorizer(
        policy_for=lambda p: PlatformPolicy(p, allowlist_env="INDUS_BUZZ_ALLOWED_USERS"),
        env=lambda _n: None,
        extra_for=lambda _p: {"allow_from": [npub]},
        adapter_for=lambda _p: like,
    )
    assert config_only.check(source(ANA)).granted
    assert not config_only.check(source(BO)).granted


def test_inbound_events_carry_the_hex_pubkey_and_its_npub() -> None:
    async def main(rig: Rig) -> None:
        await rig.settle(rig.adapter.connect())
        rig.post(ANA, "hello all", name="Ana")
        await rig.step(1.0, 2)
        event = rig.events[0]
        assert event.user_id == ANA and event.source.user_id_alt == hex_to_npub(ANA)
        assert event.source.user_name == "Ana" and event.source.chat_name == "general"

    run(main)


# -- what is and is not answered -------------------------------------------------------------------


def test_non_chat_kinds_and_our_own_events_are_ignored() -> None:
    async def main(rig: Rig) -> None:
        await rig.settle(rig.adapter.connect())
        rig.post(ANA, "+", kind=7)
        rig.post(ANA, "a long-form post", kind=30023)
        rig.post(rig.adapter.pubkey, "said by us")
        rig.post(ANA, "a chat message")
        await rig.step(1.0, 2)
        assert [e.text for e in rig.events] == ["a chat message"]

    run(main)


def test_signatures_are_verified_on_inbound_events(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="indus.gateway.adapters.buzz.adapter")

    async def main(rig: Rig) -> None:
        await rig.settle(rig.adapter.connect())
        forged = rig.post(ANA, "pay me")
        forged["content"] = "pay me more"  # the id no longer hashes the content
        stolen = rig.post(ANA, "trust me")
        stolen["sig"] = FakeNostrCrypto.sign_as(BO, bytes.fromhex(stolen["id"])).hex()
        rig.post(ANA, "genuine")
        await rig.step(1.0, 3)
        assert [e.text for e in rig.events] == ["genuine"]
        dropped = [r for r in caplog.records if "bad id or signature" in r.getMessage()]
        assert len(dropped) == 2  # once each, not once per poll

    run(main)


def test_history_is_seeded_not_answered_and_events_are_deduped() -> None:
    async def main(rig: Rig) -> None:
        rig.post(ANA, "said before we came", at=rig.relay.now() - 60)
        await rig.settle(rig.adapter.connect())
        rig.post(ANA, "new")
        await rig.step(1.0, 5)  # ``since`` is inclusive: "new" comes back on every poll
        assert [e.text for e in rig.events] == ["new"]
        state = rig.adapter.channels[GROUP]
        for n in range(SEEN_CAP + 100):
            state.remember(f"{n:064x}", ANA)
        assert len(state.seen) == SEEN_CAP and len(state.authors) == SEEN_CAP

    run(main)


def test_polls_ask_for_50_chat_events_since_the_mark_at_the_configured_pace() -> None:
    async def main(rig: Rig) -> None:
        await rig.settle(rig.adapter.connect())
        before = len(rig.requests_of(9))
        await rig.step(1.0, 3)
        assert len(rig.requests_of(9)) == before  # 4 s not up yet
        await rig.step(1.0)
        polls = rig.requests_of(9)[before:]
        assert len(polls) == 1
        assert polls[0]["limit"] == FETCH_LIMIT and polls[0]["#h"] == [GROUP]

    run(main, options=BuzzOptions(channels=(GROUP,)))


def test_poll_interval_default_and_floor() -> None:
    assert BuzzOptions.read({}, {}).poll_interval == 4.0
    assert BuzzOptions.read({"poll_interval": 0.2}, {}).poll_interval == 1.0
    assert BuzzOptions.read({}, {"BUZZ_POLL_INTERVAL": "9"}).poll_interval == 9.0
    assert BuzzOptions.read({"poll_interval": "x"}, {}).poll_interval == 4.0
    read = BuzzOptions.read({"channels": "a, b", "reply_in_thread": "false"}, {})
    assert read.channels == ("a", "b") and not read.reply_in_thread


def test_dm_discovery_runs_every_fifth_sweep_and_dms_start_from_their_beginning() -> None:
    async def main(rig: Rig) -> None:
        await rig.settle(rig.adapter.connect())
        member_queries = len(rig.requests_of(39002))
        rig.platform.add_chat(ANA, "dm", "Ana")
        conv = dm_conversation(ANA)
        rig.post(ANA, "first words", conv=conv, at=rig.relay.now() - 30)
        await rig.step(1.0, DISCOVER_EVERY - 1)
        assert len(rig.requests_of(39002)) == member_queries and rig.events == []
        await rig.step(1.0, 2)
        assert len(rig.requests_of(39002)) > member_queries
        assert [(e.text, e.source.chat_type) for e in rig.events] == [("first words", "dm")]
        assert rig.events[0].metadata == {}  # DMs need no mention

    run(main)


# -- mentions and threads --------------------------------------------------------------------------


def test_mentions_are_detected_and_a_leading_one_is_stripped() -> None:
    async def main(rig: Rig) -> None:
        await rig.settle(rig.adapter.connect())
        assert rig.adapter.display_name == "Indus"
        npub = hex_to_npub(rig.adapter.pubkey)
        rig.post(ANA, "@Indus /status")
        rig.post(ANA, f"{npub} what now?")
        rig.post(ANA, "hey", tags=[["p", rig.adapter.pubkey]])
        rig.post(ANA, "lunch? @Indusx")
        await rig.step(1.0, 2)
        got = [(e.text, e.metadata.get("mentioned")) for e in rig.events]
        assert got == [
            ("/status", True),
            ("what now?", True),
            ("hey", True),
            ("lunch? @Indusx", None),
        ]
        assert rig.events[0].command == "status"

    run(main)


def test_threads_and_replies_follow_nip10() -> None:
    root, parent = "aa" * 32, "bb" * 32
    marked = {"tags": [["e", root, "", "root"], ["e", parent, "", "reply"]]}
    assert thread_of(marked) == (root, parent)
    assert thread_of({"tags": [["e", root, "", "reply"]]}) == (root, root)
    assert thread_of({"tags": [["e", root], ["e", parent]]}) == (root, parent)
    assert thread_of({"tags": []}) == (None, None)

    async def main(rig: Rig) -> None:
        await rig.settle(rig.adapter.connect())
        top = rig.post(ANA, "top level")
        await rig.step(1.0, 2)
        first = rig.events[0]
        assert first.source.chat_type == "group"
        assert first.source.prospective_thread_id == top["id"]
        sent = await rig.settle(
            rig.adapter.send(GROUP, "answer", reply_to=top["id"], meta={"thread_id": top["id"]})
        )
        assert sent.ok
        tags = rig.relay.published[-1]["tags"]
        assert ["e", top["id"], "", "root"] in tags and ["h", GROUP] in tags
        rig.post(
            ANA,
            "follow-up",
            tags=[["e", top["id"], "", "root"], ["e", sent.message_id, "", "reply"]],
        )
        await rig.step(1.0, 2)
        follow = rig.events[-1]
        assert follow.source.chat_type == "thread" and follow.source.thread_id == top["id"]
        assert follow.reply_to_id == sent.message_id and follow.reply_to_is_own

    run(main)


def test_replies_go_flat_when_threading_is_off() -> None:
    async def main(rig: Rig) -> None:
        await rig.settle(rig.adapter.connect())
        top = rig.post(ANA, "top level")
        await rig.step(1.0, 2)
        assert rig.events[0].source.prospective_thread_id is None
        await rig.settle(rig.adapter.send(GROUP, "flat", meta={"thread_id": top["id"]}))
        assert rig.relay.published[-1]["tags"] == [["h", GROUP]]

    run(main, options=BuzzOptions(channels=(GROUP,), poll_interval=1.0, reply_in_thread=False))


def test_mentions_out_use_the_member_list_cached_for_60_seconds() -> None:
    async def main(rig: Rig) -> None:
        await rig.settle(rig.adapter.connect())
        rig.relay.profiles[ANA] = ("Ana", False)
        members = rig.relay.extra
        listed = [["d", GROUP], ["p", ANA], ["p", BO]]
        members.append(rig.relay.sign_as(BO, 39002, listed, "", rig.relay.now()))
        rig.relay.profiles[BO] = ("Bo", False)
        await rig.settle(rig.adapter.send(GROUP, "thanks @Ana and @nobody"))
        assert ["p", ANA] in rig.relay.published[-1]["tags"]
        assert ["p", BO] not in rig.relay.published[-1]["tags"]
        asked = len([r for r in rig.requests_of(39002) if r.get("#d") == [GROUP]])
        await rig.settle(rig.adapter.send(GROUP, "again @Ana"))
        assert len([r for r in rig.requests_of(39002) if r.get("#d") == [GROUP]]) == asked
        rig.clock.advance(MEMBERS_TTL + 1)
        await rig.settle(rig.adapter.send(GROUP, "later @Ana"))
        assert len([r for r in rig.requests_of(39002) if r.get("#d") == [GROUP]]) == asked + 1

    run(main)


# -- the relay connection --------------------------------------------------------------------------


def test_the_auth_challenge_is_answered_with_a_signed_event() -> None:
    async def main(rig: Rig) -> None:
        await rig.settle(rig.adapter.connect())
        assert len(rig.relay.auths) == 1
        auth = rig.relay.auths[0]
        assert auth["kind"] == 22242 and auth["pubkey"] == rig.adapter.pubkey
        assert ["relay", "wss://community.buzz.test"] in auth["tags"]
        assert ["challenge", "challenge-1"] in auth["tags"]

    run(main)


def test_a_refused_identity_is_fatal() -> None:
    async def main(rig: Rig) -> None:
        rig.relay.accept_auth = False
        with pytest.raises(AdapterConnectError) as refused:
            await rig.settle(rig.adapter.connect())
        assert refused.value.code == "buzz_auth" and not refused.value.retryable

    run(main)


def test_a_relay_without_auth_works_too() -> None:
    async def main(rig: Rig) -> None:
        await rig.settle(rig.adapter.connect())
        assert rig.relay.auths == [] and rig.adapter.connected

    run(main, require_auth=False)


def test_a_restricted_channel_is_dropped_from_the_watch_list() -> None:
    async def main(rig: Rig) -> None:
        await rig.settle(rig.adapter.connect())
        rig.relay.restricted.add(GROUP)
        await rig.step(1.0, 2)
        assert GROUP not in rig.adapter.channels
        count = len(rig.requests_of(9))
        await rig.step(1.0, 3)
        assert len(rig.requests_of(9)) == count

    run(main)


def test_a_dropped_socket_reconnects_and_reauthenticates() -> None:
    async def main(rig: Rig) -> None:
        await rig.settle(rig.adapter.connect())
        rig.relay.drop()
        await rig.step(1.0, 4)
        assert rig.relay.opens == 2 and len(rig.relay.auths) == 2
        rig.post(ANA, "after the drop")
        await rig.step(1.0, 2)
        assert [e.text for e in rig.events] == ["after the drop"]

    run(main)


def test_dropped_event_ids_are_remembered_only_up_to_a_cap() -> None:
    clock = FakeClock(datetime(2026, 3, 1, tzinfo=UTC))
    adapter = BuzzAdapter(clock=clock, relay_url="https://r.test", private_key=BOT_KEY)
    for n in range(BAD_CAP * 3):
        adapter._note_bad({"id": f"{n:064x}"})  # pyright: ignore[reportPrivateUsage]
    assert 0 < len(adapter._bad) <= BAD_CAP  # pyright: ignore[reportPrivateUsage]


def test_send_only_watches_nothing() -> None:
    async def main(rig: Rig) -> None:
        await rig.settle(rig.adapter.connect())
        assert rig.adapter.channels == {} and rig.requests_of(9) == []
        assert (await rig.settle(rig.adapter.send(GROUP, "from a script"))).ok
        assert (await rig.settle(rig.adapter.send(USER, "to a person"))).ok
        assert rig.platform.texts(USER) == ["to a person"]

    run(main, send_only=True)


# -- publishing ------------------------------------------------------------------------------------


def test_publish_refusals_are_classified() -> None:
    async def main(rig: Rig) -> None:
        await rig.settle(rig.adapter.connect())
        rig.platform.fail(FailureKind.RATE_LIMIT, op="send", retry_after=7)
        limited = await rig.settle(rig.adapter.send(GROUP, "x"))
        assert limited.kind is ErrorKind.RATE_LIMITED and limited.retry_after == 7
        rig.platform.fail(FailureKind.FORBIDDEN, op="send")
        blocked = await rig.settle(rig.adapter.send(GROUP, "x"))
        assert blocked.kind is ErrorKind.FORBIDDEN
        rig.platform.fail(FailureKind.READ_TIMEOUT, op="send")
        silent = await rig.settle(rig.adapter.send(GROUP, "x"))
        assert silent.maybe_delivered and not silent.retryable
        refusal = rig.adapter._refusal("restricted: you are muted")  # pyright: ignore[reportPrivateUsage]
        assert refusal.kind is ErrorKind.FORBIDDEN

    run(main)


def test_a_pubkey_target_goes_to_its_dm_conversation() -> None:
    async def main(rig: Rig) -> None:
        await rig.settle(rig.adapter.connect())
        npub = hex_to_npub(USER)
        assert (await rig.settle(rig.adapter.send(npub, "hello by npub"))).ok
        assert rig.relay.published[-1]["tags"][0] == ["h", dm_conversation(USER)]
        assert rig.platform.texts(USER) == ["hello by npub"]
        assert rig.adapter.channels[dm_conversation(USER)].chat_type == "dm"

    run(main)


# -- configuration and secrets ---------------------------------------------------------------------


def test_config_errors_and_the_key_never_shows(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)

    async def main(rig: Rig) -> None:
        clock = rig.clock
        for relay, key, word in (
            ("", BOT_KEY, "BUZZ_RELAY_URL"),
            ("https://r.test", "", "BUZZ_PRIVATE_KEY"),
            ("http://r.test", BOT_KEY, "https"),
            ("https://r.test", "nsec1nope", "nsec"),
        ):
            adapter = BuzzAdapter(
                clock=clock, relay_url=relay, private_key=key, crypto=FakeNostrCrypto()
            )
            with pytest.raises(AdapterConnectError) as bad:
                await adapter.connect()
            assert not bad.value.retryable and word in str(bad.value)
        local = BuzzAdapter(
            clock=clock,
            relay_url="http://127.0.0.1:7777",
            private_key=BOT_KEY,
            crypto=FakeNostrCrypto(),
            opener=rig.relay.open,
        )
        await rig.settle(local.connect())
        assert local.ws_url() == "ws://127.0.0.1:7777"
        await local.disconnect()
        await rig.settle(rig.adapter.connect())
        assert BOT_KEY not in repr(rig.adapter)
        assert rig.adapter.scrub(f"key={BOT_KEY}") != f"key={BOT_KEY}"

    run(main)
    assert all(BOT_KEY not in r.getMessage() for r in caplog.records)


def test_missing_libraries_are_fatal_with_the_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "coincurve", None)

    async def main() -> None:
        clock = FakeClock(datetime(2026, 3, 1, tzinfo=UTC))
        adapter = BuzzAdapter(clock=clock, relay_url="https://r.test", private_key=BOT_KEY)
        with pytest.raises(AdapterConnectError) as missing:
            await adapter.connect()
        assert missing.value.code == "missing_dependency" and "indus-agent[buzz]" in str(
            missing.value
        )

    asyncio.run(main())


def test_create_adapter_reads_the_secret_store_and_allowlist(scope: Scope) -> None:
    secrets: Any = scope.secrets
    secrets.set("BUZZ_PRIVATE_KEY", BOT_KEY)
    secrets.set("BUZZ_RELAY_URL", "https://community.buzz.test")
    secrets.set("INDUS_BUZZ_ALLOWED_USERS", hex_to_npub(ANA))
    section = PlatformSection.model_validate(
        {"extra": {"channels": [GROUP], "allow_from": [BO.upper()]}}
    )
    settings = PlatformSettings("buzz", True, "enabled in config", section)
    adapter = create_adapter(AdapterConfig("buzz", settings, scope, scope.clock, None))
    assert adapter.relay_url == "https://community.buzz.test"
    assert adapter.options.channels == (GROUP,)
    assert adapter.resolved_allowlist_ids() == {ANA, BO}
    assert BOT_KEY not in repr(adapter)
    one_shot = AdapterConfig("buzz", settings, scope, scope.clock, None, send_only=True)
    assert create_adapter(one_shot).send_only


# -- inbound media ---------------------------------------------------------------------------------


def test_imeta_files_are_fetched_with_their_checks() -> None:
    async def main(rig: Rig) -> None:
        await rig.settle(rig.adapter.connect())
        rig.platform.photo(GROUP, ANA, caption="look", chat_kind="group")
        rig.relay.store_inbound()
        await rig.step(1.0, 2)
        event = rig.events[-1]
        assert event.kind is MessageKind.PHOTO and event.media[0].ref.startswith(MEDIA_REF)

        async def fetch(ref: str) -> bytes:
            return b"".join([c async for c in rig.adapter.fetch_media(Attachment(ref=ref))])

        assert await fetch(event.media[0].ref) == b"data-photo.jpg"
        digest, size, url = event.media[0].ref[len(MEDIA_REF) :].split(":", 2)
        bad = [
            f"{MEDIA_REF}{'0' * 64}:{size}:{url}",  # checksum does not match
            f"{MEDIA_REF}{digest}:{int(size) + 1}:{url}",  # size does not match
            f"{MEDIA_REF}{digest}:{size}:{url.replace('https://', 'http://')}",
            f"{MEDIA_REF}{digest}:{size}:https://elsewhere.test/media/x",
            f"{MEDIA_REF}{digest}:{size}:https://user:pw@community.buzz.test/media/x",
            "https://community.buzz.test/media/x",
        ]
        for ref in bad:
            with pytest.raises(MediaRejected):
                await fetch(ref)

    run(main)


def test_audio_kinds() -> None:
    kind_of = BuzzAdapter._kind_of  # pyright: ignore[reportPrivateUsage]
    assert kind_of("audio/ogg") is MessageKind.VOICE
    assert kind_of("audio/mpeg") is MessageKind.AUDIO
    assert kind_of("video/mp4") is MessageKind.VIDEO
    assert kind_of("application/pdf") is MessageKind.DOCUMENT


def test_a_relay_that_stops_answering_is_reconnected() -> None:
    async def main(rig: Rig) -> None:
        await rig.settle(rig.adapter.connect())
        real = rig.relay.handle

        async def mute(socket: Any, message: list[Any]) -> None:
            if message[0] != "REQ":
                await real(socket, message)

        rig.relay.handle = mute  # type: ignore[method-assign]
        await rig.step(1.0, 20)
        assert rig.relay.opens >= 2
        rig.relay.handle = real  # type: ignore[method-assign]
        await rig.step(1.0, 70)
        rig.post(ANA, "back again")
        await rig.step(1.0, 3)
        assert [e.text for e in rig.events] == ["back again"]

    run(main)
