"""The BlueBubbles adapter (P16-24) over the fake BlueBubbles server."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from indus.foundation.config.schema import PlatformSection
from indus.foundation.scope import Scope
from indus.gateway.adapters._http import ListenerError
from indus.gateway.adapters.base import AdapterConnectError
from indus.gateway.adapters.bluebubbles import adapter as bb
from indus.gateway.adapters.bluebubbles import create_adapter
from indus.gateway.adapters.bluebubbles.adapter import (
    ATTACHMENT_REF,
    BlueBubblesAdapter,
    BlueBubblesOptions,
    GuidCache,
    compile_patterns,
    mask_address,
    webhook_token,
)
from indus.gateway.config import AdapterConfig, PlatformSettings
from indus.gateway.core.events import Attachment, InboundEvent, MessageKind
from indus.gateway.core.send import ErrorKind
from indus.gateway.intake.media import MediaRejected
from tests.contract.gateway.bluebubbles_case import build_adapter
from tests.fakes.clock import FakeClock
from tests.fakes.platform.bluebubbles_wire import PASSWORD, BlueBubblesWire
from tests.fakes.platform.core import FailureKind, FakeChatPlatform, FakeInbound
from tests.fakes.platform.http_wire import Callback

ANA = "+15550000200"
DM = f"iMessage;-;{ANA}"
GROUP = "iMessage;+;chat300"


class Rig:
    def __init__(self, *, private_api: bool = True, **kw: Any) -> None:
        self.clock = FakeClock(datetime(2026, 3, 1, 9, 0, tzinfo=UTC))
        self.platform = FakeChatPlatform("bluebubbles", clock=self.clock, max_len=4000)
        self.wire = BlueBubblesWire(
            self.platform, private_api=private_api, helper_connected=private_api
        )
        self.adapter = build_adapter(self.wire, self.clock, **kw)
        self.events: list[InboundEvent] = []

        async def sink(event: InboundEvent) -> None:
            self.events.append(event)

        self.adapter.set_sink(sink)

    async def deliver(self, payload: dict[str, Any], *, query: str | None = None) -> httpx.Response:
        callback = self.wire.callback(payload)
        assert callback is not None
        if query is not None:
            callback = Callback(callback.path, callback.body, callback.headers, query=query)
        answer = await self.wire.deliver(callback)
        await self.adapter.listener.idle()
        return answer

    async def pump(self) -> None:
        await self.wire.pump()
        await self.adapter.listener.idle()


def run(main: Callable[[Rig], Awaitable[None]], **kw: Any) -> None:
    async def outer() -> None:
        rig = Rig(**kw)
        try:
            await main(rig)
        finally:
            await rig.adapter.disconnect()

    asyncio.run(outer())


def message(guid: str = "m1", text: str = "hi", **data: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "guid": guid,
        "text": text,
        "isFromMe": False,
        "handle": {"address": ANA},
        "chats": [{"guid": DM, "chatIdentifier": ANA}],
        "dateCreated": 1_772_355_600_000,
    }
    body.update(data)
    return {"type": "new-message", "data": body}


# -- webhook auth and replay -----------------------------------------------------------------------


def test_callback_without_the_right_token_is_refused_before_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compared: list[tuple[object, object]] = []
    real = bb.constant_time_equal

    def spy(offered: str | bytes, expected: str | bytes) -> bool:
        compared.append((offered, expected))
        return real(offered, expected)

    monkeypatch.setattr(bb, "constant_time_equal", spy)

    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        path = rig.adapter.listener_settings.path
        for query in ("", "token=nope", "token=" + "0" * 64, f"password={PASSWORD}"):
            answer = await rig.wire.deliver(Callback(path, b"not json at all", query=query))
            assert answer.status_code == 401
        assert len(compared) == 4
        assert all(expected == rig.adapter.token for _, expected in compared)
        assert rig.events == []

    run(main)


def test_a_replayed_webhook_is_ignored() -> None:
    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        first = await rig.deliver(message("m1", "hello"))
        again = await rig.deliver(message("m1", "hello"))
        assert first.json() == {"status": "ok"}
        assert again.json() == {"status": "duplicate"}
        assert [e.text for e in rig.events] == ["hello"]
        assert rig.events[0].source.chat_id == DM and rig.events[0].user_id == ANA

    run(main)


def test_edits_arrive_once_each_and_status_updates_are_dropped() -> None:
    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        await rig.deliver(message("m1", "hello"))
        status = message("m1", "hello", dateDelivered=1)
        status["type"] = "updated-message"
        await rig.deliver(status)
        edit = message("m1", "hello there", dateEdited=1_772_355_700_000)
        edit["type"] = "updated-message"
        await rig.deliver(edit)
        await rig.deliver(edit)
        assert [(e.text, e.metadata.get("edited")) for e in rig.events] == [
            ("hello", None),
            ("hello there", True),
        ]

    run(main)


def test_own_messages_tapbacks_and_other_events_are_dropped() -> None:
    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        await rig.deliver(message("m1", "mine", isFromMe=True))
        await rig.deliver(message("m2", "Loved “hi”", associatedMessageType="love"))
        await rig.deliver(message("m3", "Liked", associatedMessageType=2001))
        other = await rig.deliver({"type": "typing-indicator", "data": {"display": True}})
        assert other.json() == {"status": "ignored"}
        assert rig.events == []

    run(main)


def test_bad_payloads_are_400() -> None:
    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        callback = rig.wire.callback({"type": "new-message"})
        assert callback is not None
        garbage = Callback(callback.path, b"{nope", callback.headers, query=callback.query)
        assert (await rig.wire.deliver(garbage)).status_code == 400
        no_sender = message("m9", "x", handle={})
        assert (await rig.deliver(no_sender)).status_code == 400
        assert rig.events == []

    run(main)


# -- the password stays out of URLs and logs -------------------------------------------------------


def test_the_password_never_reaches_a_url_log_or_error(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.INFO, logger="httpx")

    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        url = rig.wire.webhooks[0]["url"]
        assert PASSWORD not in url
        assert url.endswith("?token=" + webhook_token(PASSWORD))
        assert rig.wire.calls[0].params["password"] == PASSWORD  # the server still gets it
        rig.platform.fail(FailureKind.READ_TIMEOUT, op="send")
        result = await rig.adapter.send(GROUP, "hello")
        assert not result.ok and result.maybe_delivered
        assert PASSWORD not in (result.error or "")
        assert PASSWORD not in repr(rig.adapter)

    run(main)
    assert caplog.records, "httpx logged nothing: the check would prove nothing"
    assert all(PASSWORD not in r.getMessage() for r in caplog.records)


# -- lifecycle -------------------------------------------------------------------------------------


def test_the_webhook_is_registered_once_and_removed_at_close() -> None:
    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        assert len(rig.wire.webhooks) == 1
        assert rig.wire.webhooks[0]["events"] == ["new-message", "updated-message"]
        await rig.adapter.disconnect()
        assert rig.wire.webhooks == []
        # A row left behind by a crash is reused, not duplicated.
        await rig.adapter.connect()
        rig.adapter.connected = False  # crash: no clean close
        await rig.adapter.listener.stop()
        await rig.adapter.connect()
        assert len(rig.wire.webhooks) == 1

    run(main)


def test_send_only_registers_nothing_and_listens_nowhere() -> None:
    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        assert rig.wire.webhooks == [] and not rig.adapter.listener.accepting
        assert (await rig.adapter.send(GROUP, "from a script")).ok
        await rig.adapter.disconnect()
        assert [c.path for c in rig.wire.calls if c.method == "DELETE"] == []

    run(main, send_only=True)


def test_connect_errors() -> None:
    async def main(rig: Rig) -> None:
        empty = BlueBubblesAdapter(clock=rig.clock, server_url="", password="")
        with pytest.raises(AdapterConnectError) as config:
            await empty.connect()
        assert not config.value.retryable and "BLUEBUBBLES_SERVER_URL" in str(config.value)
        wrong = build_adapter(rig.wire, rig.clock)
        wrong.password = "x" * 12
        with pytest.raises(AdapterConnectError) as auth:
            await wrong.connect()
        assert not auth.value.retryable and auth.value.code == "bluebubbles_auth"
        rig.platform.fail_connect(FailureKind.CONNECT_TIMEOUT)
        with pytest.raises(AdapterConnectError) as down:
            await rig.adapter.connect()
        assert down.value.retryable
        ftp = BlueBubblesAdapter(clock=rig.clock, server_url="ftp://mac", password="p" * 8)
        with pytest.raises(AdapterConnectError):
            await ftp.connect()

    run(main)


def test_create_adapter_reads_the_secret_store_and_listener_keys(scope: Scope) -> None:
    secrets: Any = scope.secrets
    secrets.set("BLUEBUBBLES_SERVER_URL", "192.168.1.10:1234/")
    secrets.set("BLUEBUBBLES_PASSWORD", PASSWORD)
    section = PlatformSection.model_validate(
        {"extra": {"webhook_port": 9001, "send_read_receipts": False, "mention_patterns": "bot"}}
    )
    settings = PlatformSettings("bluebubbles", True, "enabled in config", section)
    adapter = create_adapter(AdapterConfig("bluebubbles", settings, scope, scope.clock, None))
    assert adapter.server_url == "http://192.168.1.10:1234"
    assert adapter.listener_settings.port == 9001
    assert adapter.options.send_read_receipts is False
    assert [p.pattern for p in adapter.options.mention_patterns] == ["bot"]
    assert not adapter.send_only
    one_shot = AdapterConfig("bluebubbles", settings, scope, scope.clock, None, send_only=True)
    assert create_adapter(one_shot).send_only
    public = PlatformSection.model_validate({"extra": {"webhook_host": "0.0.0.0"}})
    with pytest.raises(ListenerError):
        create_adapter(
            AdapterConfig(
                "bluebubbles",
                PlatformSettings("bluebubbles", True, "x", public),
                scope,
                scope.clock,
                None,
            )
        )


# -- chat GUIDs ------------------------------------------------------------------------------------


def test_guid_cache_evicts_the_least_recently_used_address() -> None:
    cache = GuidCache(size=3)
    for n in range(3):
        cache.put(f"+1555000000{n}", f"iMessage;-;+1555000000{n}")
    assert cache.get("+15550000000") is not None  # now the most recent
    cache.put("+15550000009", "iMessage;-;+15550000009")
    assert "+15550000001" not in cache and "+15550000000" in cache and len(cache) == 3
    assert bb.GUID_CACHE_SIZE == 500 and GuidCache().size == 500
    cache.put("Ana@Example.com", "iMessage;-;ana@example.com")
    assert cache.get("ana@example.com") == "iMessage;-;ana@example.com"


def test_addresses_resolve_by_exact_identifier_and_are_cached() -> None:
    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        rig.platform.add_chat(ANA, "dm", "Ana")
        rig.platform.add_chat("iMessage;+;chat300", "group", "Team")
        assert await rig.adapter.resolve_chat(GROUP) == GROUP  # a GUID passes through
        assert rig.wire.chat_queries == 0
        assert await rig.adapter.resolve_chat(ANA) == DM
        assert await rig.adapter.resolve_chat(ANA) == DM
        assert rig.wire.chat_queries == 1
        # A participant of a group is never matched to that group.
        assert await rig.adapter.resolve_chat("+15550000999") is None

    run(main)


def test_an_inbound_dm_teaches_the_cache_its_chat() -> None:
    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        await rig.deliver(message("m1", "hello"))
        assert (await rig.adapter.send(ANA, "hi back")).ok
        assert rig.wire.chat_queries == 0
        assert rig.platform.texts(ANA) == ["hi back"]

    run(main)


def test_an_unknown_address_needs_the_private_api_for_a_new_chat() -> None:
    async def with_api(rig: Rig) -> None:
        await rig.adapter.connect()
        result = await rig.adapter.send("+15550000777", "first contact")
        assert result.ok
        assert rig.platform.texts("+15550000777") == ["first contact"]
        assert rig.adapter.guids.get("+15550000777") == "iMessage;-;+15550000777"

    async def without(rig: Rig) -> None:
        await rig.adapter.connect()
        result = await rig.adapter.send("+15550000777", "first contact")
        assert result.kind is ErrorKind.NOT_FOUND and result.chat_unreachable
        assert "+155****0777" in (result.error or "")

    run(with_api)
    run(without, private_api=False)


# -- outbound --------------------------------------------------------------------------------------


def test_paragraphs_become_bubbles_without_markers() -> None:
    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        result = await rig.adapter.send(GROUP, "first part\n\n\nsecond part\n  \nthird")
        assert rig.platform.texts(GROUP) == ["first part", "second part", "third"]
        assert len(result.ids) == 3
        long = " ".join(f"w{n:05d}" for n in range(1500))
        await rig.adapter.send(GROUP, long)
        pieces = rig.platform.texts(GROUP)[3:]
        assert len(pieces) == 3 and all(len(p) <= 4000 for p in pieces)
        assert " ".join(pieces).split() == long.split()  # no "(i/n)" markers

    run(main)


def test_one_bubble_when_paragraph_splitting_is_off() -> None:
    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        await rig.adapter.send(GROUP, "one\n\ntwo")
        assert rig.platform.texts(GROUP) == ["one\n\ntwo"]

    run(main, options=BlueBubblesOptions(split_paragraphs=False))


def test_a_send_that_stops_part_way_returns_the_remainder() -> None:
    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        rig.platform.fail(FailureKind.CONNECT_TIMEOUT, op="send", after=1)
        result = await rig.adapter.send(GROUP, "alpha\n\nbeta\n\ngamma")
        assert not result.ok and result.retryable and len(result.ids) == 1
        assert result.remainder == "beta\n\ngamma"

    run(main)


def test_replies_quote_only_with_the_private_api() -> None:
    async def with_api(rig: Rig) -> None:
        await rig.adapter.connect()
        await rig.adapter.send(GROUP, "answer", reply_to="m1")
        sent = rig.wire.calls_to("POST", "/api/v1/message/text")[-1].json()
        assert sent["selectedMessageGuid"] == "m1" and sent["method"] == "private-api"

    async def without(rig: Rig) -> None:
        await rig.adapter.connect()
        await rig.adapter.send(GROUP, "answer", reply_to="m1")
        sent = rig.wire.calls_to("POST", "/api/v1/message/text")[-1].json()
        assert "selectedMessageGuid" not in sent and "method" not in sent
        await rig.adapter.typing_start(GROUP)
        assert rig.platform.ops_of("typing") == []

    run(with_api)
    run(without, private_api=False)


def test_files_go_as_attachments_with_the_caption_after(tmp_path: Path) -> None:
    note = tmp_path / "note.m4a"
    note.write_bytes(b"\x00\x00\x00\x18ftypM4A ")

    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        result = await rig.adapter.send_voice(GROUP, str(note), caption="listen")
        assert result.ok and len(result.ids) == 2
        media = rig.platform.ops_of("media")[0]
        assert media.extra["kind"] == "voice" and media.extra["ref"] == "note.m4a"
        assert rig.platform.texts(GROUP)[-1] == "listen"
        missing = await rig.adapter.send_document(GROUP, str(tmp_path / "gone.pdf"))
        assert not missing.ok

    run(main)


def test_processing_tapbacks() -> None:
    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        await rig.adapter.processing_started(GROUP, "m1")
        await rig.adapter.processing_finished(GROUP, "m1", ok=False)
        assert [r["reaction"] for r in rig.wire.reactions] == ["emphasize", "-emphasize", "dislike"]

    run(main)


# -- inbound details -------------------------------------------------------------------------------


def test_group_wake_words_set_mentioned_and_are_stripped() -> None:
    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        group = [{"guid": GROUP, "chatIdentifier": "chat300", "displayName": "Team"}]
        await rig.deliver(message("g1", "Indus, what time is it?", chats=group))
        await rig.deliver(message("g2", "lunch anyone?", chats=group))
        await rig.deliver(message("g3", "Indus agent: /status", chats=group))
        mentioned = [(e.text, e.metadata.get("mentioned")) for e in rig.events]
        assert mentioned == [("what time is it?", True), ("lunch anyone?", None), ("/status", True)]
        assert rig.events[0].source.chat_type == "group"
        assert rig.events[0].source.chat_name == "Team"
        assert rig.events[2].command == "status"

    run(main)


def test_mention_patterns_parse_lists_json_and_text() -> None:
    assert [p.pattern for p in compile_patterns('["^hey bot", "assistant"]')] == [
        "^hey bot",
        "assistant",
    ]
    assert [p.pattern for p in compile_patterns("a\nb, c")] == ["a", "b", "c"]
    assert [p.pattern for p in compile_patterns(["(unclosed", "ok"])] == ["ok"]
    assert len(compile_patterns(None)) == 2


def test_attachments_are_fetched_lazily_and_capped() -> None:
    class Seat:
        def __init__(self) -> None:
            self.fetchers: dict[str, Any] = {}

        def register_fetcher(self, platform: str, fetcher: Any) -> Callable[[], None]:
            self.fetchers[platform] = fetcher
            return lambda: self.fetchers.pop(platform, None)

    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        seat = Seat()
        undo = rig.adapter.register_fetchers(seat)
        assert undo is not None and "bluebubbles" in seat.fetchers
        rig.platform.photo(ANA, ANA, caption="look")
        await rig.pump()
        event = rig.events[-1]
        assert event.kind is MessageKind.PHOTO and event.text == "look"
        assert rig.wire.calls_to("GET", "/api/v1/attachment") == []  # nothing fetched yet
        data = b"".join([c async for c in rig.adapter.fetch_attachment(event.media[0])])
        assert data.startswith(b"\xff\xd8\xff")
        with pytest.raises(MediaRejected):
            async for _ in rig.adapter.fetch_attachment(Attachment(ref="https://x/y")):
                pass
        with pytest.raises(MediaRejected):
            async for _ in rig.adapter.fetch_attachment(Attachment(ref=ATTACHMENT_REF + "nope")):
                pass
        undo()
        assert seat.fetchers == {}

    run(main)


def test_read_receipts_follow_the_setting() -> None:
    async def on(rig: Rig) -> None:
        await rig.adapter.connect()
        await rig.deliver(message("m1", "hello"))
        assert rig.wire.reads == [DM]

    async def off(rig: Rig) -> None:
        await rig.adapter.connect()
        await rig.deliver(message("m1", "hello"))
        assert rig.wire.reads == []

    run(on)
    run(off, options=BlueBubblesOptions(send_read_receipts=False))


def test_masking_and_quotes_of_our_own_messages() -> None:
    assert mask_address("+15551234567") == "+155****4567"
    assert mask_address("ana@example.com") == "a***@example.com"

    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        sent = await rig.adapter.send(DM, "our message")
        assert sent.message_id is not None
        await rig.deliver(message("m5", "replying", threadOriginatorGuid=sent.message_id))
        assert rig.events[-1].reply_to_id == sent.message_id
        assert rig.events[-1].reply_to_is_own

    run(main)


def test_wire_inbound_items_reach_the_adapter() -> None:
    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        item: FakeInbound = rig.platform.dm(ANA, "hello there", user_name="Ana")
        await rig.pump()
        assert rig.events[0].message_id == item.message_id

    run(main)
