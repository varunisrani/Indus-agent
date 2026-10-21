"""The Feishu / Lark adapter over the fake platform (P16-14): the row's Verify cases (batching,
webhook decryption, the Lark domain) and the rules the shared contract suite does not reach."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from collections.abc import Awaitable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.config.schema import PlatformSection
from indus.foundation.scope import Scope
from indus.gateway.adapters.base import AdapterConnectError, ApprovalAction, ApprovalPrompt
from indus.gateway.adapters.feishu import create_adapter
from indus.gateway.adapters.feishu.adapter import (
    FeishuAdapter,
    FeishuOptions,
    parse_resource_ref,
)
from indus.gateway.adapters.feishu.batching import BatchSettings
from indus.gateway.adapters.feishu.client import ROUTES as CLIENT_ROUTES
from indus.gateway.config import AdapterConfig, PlatformSettings
from indus.gateway.core.events import InboundEvent, MessageKind
from indus.gateway.core.send import ErrorKind
from indus.gateway.core.session_keys import build_session_key
from indus.gateway.delivery import send_command
from indus.gateway.registry import PlatformEntry, PlatformRegistry
from tests.fakes.clock import FakeClock, quiesce
from tests.fakes.platform.core import FailureKind
from tests.fakes.platform.feishu_wire import (
    BOT_OPEN_ID,
    LARK_URL,
    ROUTES,
    SERVICE_ID,
    FeishuWire,
)

ANA = "ou_ana"
BEN = "ou_ben"
GROUP = "oc_group1"


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("real network used in a Feishu adapter test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield


def _clock() -> FakeClock:
    return FakeClock(datetime(2026, 1, 1, 9, 0, tzinfo=UTC))


async def drive[T](clock: FakeClock, awaitable: Awaitable[T], *, step: float = 0.5) -> T:
    task = asyncio.ensure_future(awaitable)
    for _ in range(4000):
        await quiesce(clock)  # never move the clock under an awaited off-loop call
        if task.done():
            return task.result()
        clock.advance(step)
    task.cancel()
    raise AssertionError("never finished on the fake clock")


async def settle(clock: FakeClock) -> None:
    """Let queued work run without moving the clock."""
    for _ in range(5):
        await clock.run_until_idle()


async def pump(clock: FakeClock, rounds: int = 3) -> None:
    for _ in range(rounds):
        await clock.advance_and_settle(1.0)


def run(coro: Awaitable[Any]) -> Any:
    return asyncio.run(coro)  # pyright: ignore[reportArgumentType]


class Rig:
    def __init__(
        self,
        *,
        options: FeishuOptions | None = None,
        webhook: bool = False,
        listen: bool = True,
        **wire_kw: Any,
    ) -> None:
        self.clock = _clock()
        self.wire = FeishuWire(clock=self.clock, **wire_kw)
        self.wire.user_names.update({ANA: "Ana", BEN: "Ben"})
        self.adapter = (
            self.wire.webhook_adapter(options=options)
            if webhook
            else self.wire.adapter(options=options)
        )
        self.events: list[InboundEvent] = []
        self.fatal: list[tuple[str, str]] = []
        self.adapter.set_fatal_listener(lambda code, message: self.fatal.append((code, message)))
        if listen:
            self.adapter.set_sink(self.sink)

    async def sink(self, event: InboundEvent) -> None:
        self.events.append(event)

    async def connect(self) -> None:
        await drive(self.clock, self.adapter.connect())
        await settle(self.clock)

    async def close(self) -> None:
        await drive(self.clock, self.adapter.disconnect())

    @property
    def socket(self) -> Any:
        return self.wire.sockets[-1]


# -- the row's Verify ------------------------------------------------------------------------------


def test_nine_rapid_messages_make_two_batches() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        for n in range(9):
            rig.wire.platform.dm(ANA, f"part {n}")
        await settle(rig.clock)
        # The ninth message would pass eight: the first eight go at once, the ninth waits.
        assert [e.metadata.get("batched") for e in rig.events] == [8]
        await pump(rig.clock)
        assert len(rig.events) == 2
        first, second = rig.events
        assert first.text == "\n".join(f"part {n}" for n in range(8))
        assert first.metadata["batched"] == 8
        assert second.text == "part 8"
        assert first.message_id != second.message_id
        await rig.close()

    run(main())


def test_the_quiet_window_restarts_with_each_message_and_respects_the_char_cap() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        p = rig.wire.platform
        p.dm(ANA, "one")
        await settle(rig.clock)
        await rig.clock.advance_and_settle(0.4)
        p.dm(ANA, "two")
        await settle(rig.clock)
        await rig.clock.advance_and_settle(0.4)
        assert rig.events == []  # 0.8 s since the first, 0.4 s since the newest
        await rig.clock.advance_and_settle(0.3)
        assert [e.text for e in rig.events] == ["one\ntwo"]
        p.dm(ANA, "a" * 2500)
        p.dm(ANA, "b" * 2500)
        await pump(rig.clock)
        assert [len(e.text) for e in rig.events[1:]] == [2500, 2500]
        await rig.close()

    run(main())


def test_batches_keep_senders_threads_and_order_apart() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        p = rig.wire.platform
        p.add_chat(GROUP, "group", "Team")
        p.group(GROUP, ANA, "ana says", mention=True)
        p.group(GROUP, BEN, "ben says", mention=True)
        p.thread(GROUP, "omt_t1", ANA, "in a thread", mention=True)
        p.group(GROUP, ANA, "/status", mention=True)  # a command flushes Ana's batch first
        await pump(rig.clock)
        texts = [e.text for e in rig.events]
        assert texts.index("ana says") < texts.index("/status")
        assert sorted(texts) == sorted(["ana says", "ben says", "in a thread", "/status"])
        thread = next(e for e in rig.events if e.text == "in a thread")
        assert thread.source.chat_type == "thread" and thread.source.thread_id == "omt_t1"
        group = next(e for e in rig.events if e.text == "ana says")
        assert group.source.chat_name == "Team" and group.source.user_name == "Ana"
        assert group.metadata["mentioned"] is True
        await rig.close()

    run(main())


def test_photos_within_the_media_window_become_one_event() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        p = rig.wire.platform
        p.photo(ANA, ANA, "a.jpg", caption="first")
        p.photo(ANA, ANA, "b.jpg")
        p.document(ANA, ANA, "notes.txt")
        await pump(rig.clock)
        photos = [e for e in rig.events if e.kind is MessageKind.PHOTO]
        documents = [e for e in rig.events if e.kind is MessageKind.DOCUMENT]
        assert len(photos) == 1 and len(photos[0].media) == 2 and photos[0].text == "first"
        assert len(documents) == 1
        # The intake downloads through the registered fetcher.
        fetchers: dict[str, Any] = {}

        class Seat:
            def register_fetcher(self, platform: str, fetcher: Any) -> Any:
                fetchers[platform] = fetcher
                return lambda: fetchers.pop(platform)

        undo = rig.adapter.register_fetchers(Seat())
        assert undo is not None
        chunks = [c async for c in fetchers["feishu"](documents[0].media[0])]
        assert b"".join(chunks) == b"bytes of notes.txt"
        assert parse_resource_ref(documents[0].media[0].ref) is not None
        undo()
        await rig.close()

    run(main())


def test_the_lark_domain_is_selected_by_config(scope: Scope) -> None:
    section = PlatformSection.model_validate({"extra": {"domain": "lark"}})
    settings = PlatformSettings("feishu", True, "enabled in config", section)
    adapter = create_adapter(AdapterConfig("feishu", settings, scope, scope.clock, None))
    assert adapter.options.domain == "lark" and adapter.options.locale == "en_us"

    async def main() -> None:
        rig = Rig(options=FeishuOptions(domain="lark"), domain="lark")
        await rig.connect()
        assert (await drive(rig.clock, rig.adapter.send(ANA, "hello"))).ok
        assert set(rig.wire.hosts) == {"open.larksuite.com"}
        assert rig.adapter.client is not None and rig.adapter.client.base_url == LARK_URL
        await rig.close()

    run(main())


def test_an_unknown_domain_or_mode_is_a_configuration_error() -> None:
    async def main() -> None:
        for options in (
            FeishuOptions(domain="larkish"),
            FeishuOptions(connection_mode="polling"),
        ):
            rig = Rig(options=options)
            with pytest.raises(AdapterConnectError) as caught:
                await drive(rig.clock, rig.adapter.connect())
            assert caught.value.retryable is False and caught.value.code == "config"
        clock = _clock()
        missing = FeishuAdapter(clock=clock, app_id="cli_x", app_secret="")
        with pytest.raises(AdapterConnectError) as caught:
            await drive(clock, missing.connect())
        assert caught.value.code == "config" and "FEISHU_APP_SECRET" in str(caught.value)

    run(main())


def test_webhook_mode_decrypts_and_checks_every_layer() -> None:
    async def main() -> None:
        rig = Rig(webhook=True)
        await rig.connect()
        wire = rig.wire
        p = wire.platform
        item = p.dm(ANA, "hello webhook")
        p.drain_inbound()
        found = wire.event_for(item)
        assert found is not None
        body = found[1]
        good = wire.signed(body)
        answer = await wire.deliver(good)
        assert answer.status_code == 200
        await pump(rig.clock)
        assert [e.text for e in rig.events] == ["hello webhook"]
        # The same event id again: answered, never a second event.
        assert (await wire.deliver(wire.signed(body))).status_code == 200
        # Bad signature, stale timestamp, wrong token, plain body: refused.
        bad = wire.signed({**body, "header": {**body["header"], "event_id": "x1"}})
        forged = bad.__class__(bad.path, bad.body, {**bad.headers, "x-lark-signature": "0" * 64})
        assert (await wire.deliver(forged)).status_code == 401
        stale = wire.signed(
            {**body, "header": {**body["header"], "event_id": "x2"}}, timestamp="1000"
        )
        assert (await wire.deliver(stale)).status_code == 401
        wrong = {**body, "header": {**body["header"], "event_id": "x3", "token": "nope"}}
        assert (await wire.deliver(wire.signed(wrong))).status_code == 401
        plain = wire.signed(
            {**body, "header": {**body["header"], "event_id": "x4"}}, encrypted=False
        )
        assert (await wire.deliver(plain)).status_code == 400
        unsigned = wire.signed({**body, "header": {**body["header"], "event_id": "x5"}})
        headers = {k: v for k, v in unsigned.headers.items() if k != "x-lark-signature"}
        assert (
            await wire.deliver(unsigned.__class__(unsigned.path, unsigned.body, headers))
        ).status_code == 401
        await pump(rig.clock)
        assert [e.text for e in rig.events] == ["hello webhook"]
        await rig.close()

    run(main())


def test_the_url_challenge_is_echoed_only_with_the_token() -> None:
    async def main() -> None:
        rig = Rig(webhook=True)
        await rig.connect()
        wire = rig.wire
        challenge = {
            "challenge": "c-123",
            "token": wire.verification_token,
            "type": "url_verification",
        }
        signed = wire.signed(challenge)
        headers = {k: v for k, v in signed.headers.items() if not k.startswith("x-lark")}
        answer = await wire.deliver(signed.__class__(signed.path, signed.body, headers))
        assert answer.status_code == 200 and answer.json() == {"challenge": "c-123"}
        forged = wire.signed({**challenge, "token": "other"})
        answer = await wire.deliver(forged)
        assert answer.status_code == 401 and "c-123" not in answer.text
        await rig.close()

    run(main())


def test_webhook_mode_needs_the_verification_token() -> None:
    async def main() -> None:
        clock = _clock()
        wire = FeishuWire(clock=clock)
        adapter = wire.adapter(
            options=FeishuOptions(connection_mode="webhook"), serve=False, encrypt_key="k" * 8
        )
        with pytest.raises(AdapterConnectError) as caught:
            await drive(clock, adapter.connect())
        assert caught.value.code == "config" and "FEISHU_VERIFICATION_TOKEN" in str(caught.value)

    run(main())


# -- the long connection --------------------------------------------------------------------------


def test_every_data_frame_is_acknowledged_and_pings_follow_the_server_interval() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        rig.wire.platform.dm(ANA, "hi")
        await pump(rig.clock)
        frames = rig.socket.frames_sent()
        acks = [f for f in frames if f["method"] == 1]
        assert len(acks) == 1
        assert acks[0]["headers"]["type"] == "event" and "biz_rt" in acks[0]["headers"]
        assert json.loads(acks[0]["payload"]) == {"code": 200}
        assert not [f for f in frames if f["headers"].get("type") == "ping"]
        await rig.clock.advance_and_settle(120.0)
        pings = [f for f in rig.socket.frames_sent() if f["headers"].get("type") == "ping"]
        assert len(pings) == 1 and pings[0]["service"] == SERVICE_ID and pings[0]["method"] == 0
        await rig.close()

    run(main())


def test_an_event_in_parts_becomes_one_event() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        rig.wire.split_parts = 3
        rig.wire.platform.dm(ANA, "split " * 50)
        await pump(rig.clock)
        assert len(rig.events) == 1 and rig.events[0].text == ("split " * 50).strip()
        assert len([f for f in rig.socket.frames_sent() if f["method"] == 1]) == 1
        await rig.close()

    run(main())


def test_a_lost_link_reopens_with_backoff() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        assert rig.adapter.link_opens == 1
        rig.wire.drop_link()
        await settle(rig.clock)
        await rig.clock.advance_and_settle(1.9)
        assert rig.adapter.link_opens == 1
        await rig.clock.advance_and_settle(0.2)
        assert rig.adapter.link_opens == 2
        rig.wire.platform.dm(ANA, "after the drop")
        await pump(rig.clock)
        assert [e.text for e in rig.events] == ["after the drop"]
        await rig.close()

    run(main())


def test_connect_tries_the_first_link_three_times() -> None:
    async def main() -> None:
        rig = Rig()
        rig.wire.socket_failures = [OSError("refused"), OSError("refused")]
        await rig.connect()
        assert len(rig.wire.socket_urls) == 3 and rig.adapter.connected
        await rig.close()

        rig = Rig()
        rig.wire.socket_failures = [OSError("refused")] * 3
        with pytest.raises(AdapterConnectError) as caught:
            await drive(rig.clock, rig.adapter.connect())
        assert caught.value.retryable is True and "3 attempts" in str(caught.value)
        assert not rig.adapter.connected

    run(main())


def test_refused_credentials_are_fatal_at_connect_and_while_running() -> None:
    async def main() -> None:
        rig = Rig()
        rig.wire.app_secret = "different"
        with pytest.raises(AdapterConnectError) as caught:
            await drive(rig.clock, rig.adapter.connect())
        assert caught.value.retryable is False and caught.value.code == "auth_failed"

        rig = Rig()
        await rig.connect()
        rig.wire.app_secret = "rotated"
        rig.wire.drop_link()
        await pump(rig.clock, 5)
        assert [code for code, _ in rig.fatal] == ["feishu_auth"]
        await rig.close()

    run(main())


def test_a_missing_extra_is_a_clear_connect_error() -> None:
    async def main() -> None:
        rig = Rig()
        rig.wire.socket_failures = [ModuleNotFoundError("no httpx_ws", name="httpx_ws")]
        with pytest.raises(AdapterConnectError) as caught:
            await drive(rig.clock, rig.adapter.connect())
        assert caught.value.code == "missing_dependency" and "indus-agent[feishu]" in str(
            caught.value
        )

    run(main())


def test_the_tenant_token_is_refreshed_before_expiry_and_after_an_invalid_answer() -> None:
    async def main() -> None:
        rig = Rig()
        rig.wire.token_ttl = 900
        await rig.connect()
        assert len(rig.wire.tokens) == 1
        assert (await drive(rig.clock, rig.adapter.send(ANA, "one"))).ok
        assert len(rig.wire.tokens) == 1
        await rig.clock.advance_and_settle(700.0)  # within five minutes of expiry
        assert (await drive(rig.clock, rig.adapter.send(ANA, "two"))).ok
        assert len(rig.wire.tokens) == 2
        rig.wire.retired.add(rig.wire.tokens[-1])
        assert (await drive(rig.clock, rig.adapter.send(ANA, "three"))).ok
        assert len(rig.wire.tokens) == 3
        assert rig.wire.platform.texts(ANA) == ["one", "two", "three"]
        await rig.close()

    run(main())


def test_events_before_the_sink_wait_for_it() -> None:
    async def main() -> None:
        rig = Rig(listen=False)
        await rig.connect()
        rig.wire.platform.dm(ANA, "early")
        await pump(rig.clock)
        assert rig.events == []
        rig.adapter.set_sink(rig.sink)
        await settle(rig.clock)
        assert [e.text for e in rig.events] == ["early"]
        await rig.close()

    run(main())


# -- inbound shapes --------------------------------------------------------------------------------


def test_mentions_are_resolved_and_the_bots_own_messages_dropped() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        content = {"text": "@_user_1 ask @_user_2 and @_all"}
        mentions = [
            {"key": "@_user_1", "id": {"open_id": BOT_OPEN_ID}, "name": "IndusBot"},
            {"key": "@_user_2", "id": {"open_id": BEN}, "name": "Ben"},
        ]
        body = _message_body(rig.wire, GROUP, ANA, "text", content, mentions=mentions)
        rig.wire.push_event("event", body)
        own = _message_body(rig.wire, GROUP, BOT_OPEN_ID, "text", {"text": "echo"}, mid="om_own")
        rig.wire.push_event("event", own)
        await pump(rig.clock)
        assert [e.text for e in rig.events] == ["ask @Ben and @all"]
        assert rig.events[0].metadata["mentioned"] is True
        assert rig.events[0].source.user_id_alt == "on_" + ANA
        await rig.close()

    run(main())


def test_rich_text_posts_become_markdown_and_attachments() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        post = {
            "zh_cn": {
                "title": "Report",
                "content": [
                    [
                        {"tag": "text", "text": "see", "style": ["bold"]},
                        {"tag": "a", "text": " docs", "href": "https://example.org"},
                    ],
                    [{"tag": "code_block", "language": "PYTHON", "text": "x  =  1\n"}],
                    [{"tag": "img", "image_key": "img_k1"}],
                ],
            }
        }
        rig.wire.files["img_k1"] = ("k1.png", b"png", "image/png")
        rig.wire.push_event("event", _message_body(rig.wire, ANA, ANA, "post", post))
        await pump(rig.clock)
        (event,) = rig.events
        assert event.kind is MessageKind.PHOTO and len(event.media) == 1
        assert event.text == "Report\n**see**[docs](https://example.org)\n```python\nx  =  1\n```"
        await rig.close()

    run(main())


# -- outbound --------------------------------------------------------------------------------------


def test_markdown_goes_as_a_post_in_every_piece_and_plain_as_text() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        long = "**Title**\n\n" + " ".join(f"word{n}" for n in range(2500))
        assert (await drive(rig.clock, rig.adapter.send(ANA, long))).ok
        bodies = [c.json() for c in rig.wire.calls_to("POST", "/open-apis/im/v1/messages")]
        assert len(bodies) >= 2 and {b["msg_type"] for b in bodies} == {"post"}
        assert (await drive(rig.clock, rig.adapter.send(ANA, "**x**", meta={"plain": True}))).ok
        assert (
            rig.wire.calls_to("POST", "/open-apis/im/v1/messages")[-1].json()["msg_type"] == "text"
        )
        await rig.close()

    run(main())


def test_a_reply_to_a_withdrawn_message_becomes_a_new_message() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        result = await drive(rig.clock, rig.adapter.send(ANA, "answer", reply_to="om_gone"))
        assert result.ok
        assert rig.wire.platform.texts(ANA) == ["answer"]
        assert rig.wire.calls_to("POST", "/open-apis/im/v1/messages/om_gone/reply")
        await rig.close()

    run(main())


def test_thread_replies_stay_in_the_thread() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        p = rig.wire.platform
        p.add_chat(GROUP, "group", "Team")
        inbound = p.thread(GROUP, "omt_t9", ANA, "question", mention=True)
        await pump(rig.clock)
        (event,) = rig.events
        meta = {"thread_id": event.source.thread_id}
        assert (await drive(rig.clock, rig.adapter.send(GROUP, "answer", meta=meta))).ok
        call = rig.wire.calls[-1]
        assert call.path == f"/open-apis/im/v1/messages/{inbound.message_id}/reply"
        assert call.json()["reply_in_thread"] is True
        assert build_session_key(event.source) != build_session_key(
            event.source.model_copy(update={"thread_id": None, "chat_type": "group"})
        )
        await rig.close()

    run(main())


def test_an_approval_card_press_and_close() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        prompt = ApprovalPrompt(
            request_id="req7",
            session_key="k",
            text="Run `rm -rf build`?",
            fallback_text="Run it? /approve or /deny",
            actions=(ApprovalAction("Allow", "once", "primary"), ApprovalAction("Deny", "deny")),
        )
        sent = await drive(rig.clock, rig.adapter.send_approval(ANA, prompt))
        assert sent.ok and sent.message_id
        card = rig.wire.cards[sent.message_id]
        assert card["header"]["template"] == "orange"
        rig.wire.platform.button_press(ANA, ANA, "apv:req7:once", message_id=sent.message_id)
        await pump(rig.clock)
        (press,) = rig.events
        assert press.metadata["button"] == "apv:req7:once"
        assert press.metadata["button_message_id"] == sent.message_id
        assert press.source.user_id == ANA and press.source.chat_type == "dm"
        closed = await drive(
            rig.clock, rig.adapter.edit(ANA, sent.message_id, "Approved once", final=True)
        )
        assert closed.ok
        message = rig.wire.platform.message(sent.message_id)
        assert message is not None and message.buttons == () and message.text == "Approved once"
        await rig.close()

    run(main())


def test_send_errors_are_classified() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        rig.wire.platform.fail(FailureKind.FORBIDDEN, op="send")
        result = await drive(rig.clock, rig.adapter.send(GROUP, "x"))
        assert result.kind is ErrorKind.FORBIDDEN and result.chat_unreachable
        rig.wire.app_secret = "rotated"
        rig.wire.retired.update(rig.wire.tokens)
        result = await drive(rig.clock, rig.adapter.send(GROUP, "x"))
        assert not result.ok and rig.adapter.is_final_failure(result)
        assert not result.chat_unreachable
        await rig.close()

    run(main())


def test_media_uploads_and_processing_reactions(tmp_path: Path) -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        image = tmp_path / "chart.png"
        image.write_bytes(b"\x89PNG data")
        voice = tmp_path / "note.ogg"
        voice.write_bytes(b"OggS data")
        assert (await drive(rig.clock, rig.adapter.send_image(ANA, str(image), caption="look"))).ok
        assert (await drive(rig.clock, rig.adapter.send_voice(ANA, str(voice)))).ok
        uploads = rig.wire.calls_to("POST", "/open-apis/im/v1/files")
        assert len(uploads) == 1 and b"opus" in uploads[0].body
        kinds = [
            c.json()["msg_type"] for c in rig.wire.calls_to("POST", "/open-apis/im/v1/messages")
        ]
        assert kinds == ["post", "audio"]
        await drive(rig.clock, rig.adapter.processing_started(ANA, "om_x"))
        await drive(rig.clock, rig.adapter.processing_finished(ANA, "om_x", ok=False))
        assert [(a, e) for a, _m, e in rig.wire.reactions if a == "add"] == [
            ("add", "Typing"),
            ("add", "CrossMark"),
        ]
        assert any(a == "remove" for a, _m, _e in rig.wire.reactions)
        await rig.close()

    run(main())


# -- send-only, routes and secrets ---------------------------------------------------------------


def test_indus_send_builds_a_send_only_adapter_that_opens_nothing(scope: Scope) -> None:
    wire = FeishuWire(clock=scope.clock)  # pyright: ignore[reportArgumentType]
    secrets: Any = scope.secrets
    secrets.set("FEISHU_APP_ID", wire.app_id)
    secrets.set("FEISHU_APP_SECRET", wire.app_secret)
    built: list[FeishuAdapter] = []

    def factory(config: AdapterConfig) -> FeishuAdapter:
        adapter = create_adapter(config, transport=wire.transport(), opener=wire.open)
        built.append(adapter)
        return adapter

    entry = PlatformEntry(name="feishu", label="Feishu", factory=factory, check_fn=lambda: True)
    settings = PlatformSettings("feishu", True, "credentials", PlatformSection())
    found = send_command.build_adapter(scope, "feishu", settings, PlatformRegistry([entry]))
    assert found is built[0] and built[0].send_only
    adapter = built[0]

    async def main() -> None:
        await adapter.connect()
        assert (await adapter.send(ANA, "deploy done")).ok
        await adapter.disconnect()

    run(main())
    assert wire.socket_urls == [] and not wire.calls_to("POST", "/callback/ws/endpoint")
    assert not wire.calls_to("GET", "/open-apis/bot/v3/info")
    assert [op.kind for op in wire.platform.ops] == ["connect", "send", "disconnect"]


def test_the_client_routes_are_served_by_the_fake() -> None:
    assert set(CLIENT_ROUTES.items()) <= set(ROUTES.items())


def test_secrets_never_show_in_logs_errors_or_repr(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="httpx")
    caplog.set_level(logging.DEBUG, logger="httpcore")

    async def main() -> list[str]:
        rig = Rig(webhook=False)
        await rig.connect()
        rig.wire.platform.fail(FailureKind.FORBIDDEN, op="send")
        failed = await drive(rig.clock, rig.adapter.send(GROUP, "x"))
        rig.wire.drop_link()
        await pump(rig.clock, 4)
        secrets = [
            rig.wire.app_secret,
            *rig.wire.tokens,
            *(url.split("?", 1)[1] for url in rig.wire.socket_urls),
        ]
        texts = [repr(rig.adapter), repr(rig.adapter.client), str(failed.error)]
        for secret in secrets:
            assert secret not in rig.adapter.scrub(f"x {secret} y")
        await rig.close()
        return [*secrets, *texts]

    found = run(main())
    secrets, texts = found[:-3], found[-3:]
    logged = caplog.text + "\n".join(texts)
    for secret in secrets:
        assert secret not in logged


def test_options_read_extra_and_env() -> None:
    options = FeishuOptions.read(
        {"text_batch_max_messages": "3", "reactions": "off", "connection_mode": "WEBHOOK"},
        {"FEISHU_DOMAIN": "lark"},
    )
    assert options.domain == "lark" and options.connection_mode == "webhook"
    assert options.batching == BatchSettings(text_max_messages=3)
    assert options.reactions is False


def _message_body(
    wire: FeishuWire,
    chat_id: str,
    sender: str,
    message_type: str,
    content: dict[str, Any],
    *,
    mentions: list[dict[str, Any]] | None = None,
    mid: str = "om_hand1",
) -> dict[str, Any]:
    return {
        "schema": "2.0",
        "header": wire._header("im.message.receive_v1"),  # pyright: ignore[reportPrivateUsage]
        "event": {
            "sender": {"sender_id": {"open_id": sender, "union_id": "on_" + sender}},
            "message": {
                "message_id": mid,
                "chat_id": chat_id,
                "chat_type": "p2p" if chat_id.startswith("ou_") else "group",
                "message_type": message_type,
                "content": json.dumps(content),
                "mentions": mentions or [],
            },
        },
    }


def test_a_reply_carries_the_text_it_answers() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        sent = await drive(rig.clock, rig.adapter.send(ANA, "the plan is ready"))
        assert sent.message_id is not None
        rig.wire.platform.dm(ANA, "looks good", reply_to=sent.message_id)
        await pump(rig.clock)
        (event,) = rig.events
        assert event.reply_to_id == sent.message_id
        assert event.reply_to_text == "the plan is ready"
        await rig.close()

    run(main())


def test_the_webhook_takes_json_only() -> None:
    async def main() -> None:
        rig = Rig(webhook=True)
        await rig.connect()
        callback = rig.wire.signed({"schema": "2.0", "header": {}, "event": {}})
        form = callback.__class__(
            callback.path, callback.body, {**callback.headers, "content-type": "text/plain"}
        )
        assert (await rig.wire.deliver(form)).status_code == 415
        await rig.close()

    run(main())


def test_the_real_opener_speaks_to_an_asgi_server(caplog: pytest.LogCaptureFixture) -> None:
    """The production path (httpx-ws) against an in-process ASGI app: no socket involved. The
    connection ticket reaches the server inside the transport only."""
    pytest.importorskip("httpx_ws")
    from httpx_ws.transport import (  # pyright: ignore[reportMissingImports]
        ASGIWebSocketTransport,  # pyright: ignore[reportUnknownVariableType]
    )
    from starlette.applications import Starlette
    from starlette.routing import WebSocketRoute

    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="httpx")
    from starlette.websockets import WebSocket

    from indus.gateway.adapters.feishu.client import httpx_ws_opener
    from tests.fakes.platform.feishu_wire import (
        _frame,  # pyright: ignore[reportPrivateUsage]
        read_frame,
    )

    seen: dict[str, Any] = {}

    async def link(websocket: WebSocket) -> None:
        seen["ticket"] = websocket.query_params.get("ticket")
        await websocket.accept()
        body = {
            "schema": "2.0",
            "header": {"event_id": "e1", "event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_id": {"open_id": ANA}, "sender_type": "user"},
                "message": {
                    "message_id": "om_real1",
                    "chat_id": ANA,
                    "chat_type": "p2p",
                    "message_type": "text",
                    "content": json.dumps({"text": "over a real socket"}),
                },
            },
        }
        headers = {"type": "event", "message_id": "m1", "sum": "1", "seq": "0"}
        await websocket.send_bytes(_frame(1, headers, json.dumps(body).encode(), seq=1))
        seen["ack"] = read_frame(await websocket.receive_bytes())
        await websocket.receive_bytes()  # stay open until the client goes

    app = Starlette(routes=[WebSocketRoute("/ws/v2", link)])

    async def main() -> list[InboundEvent]:
        clock = _clock()
        wire = FeishuWire(clock=clock)
        got: list[InboundEvent] = []
        async with ASGIWebSocketTransport(app) as transport:  # pyright: ignore[reportUnknownVariableType]
            adapter = wire.adapter(
                opener=httpx_ws_opener(transport=transport),  # pyright: ignore[reportUnknownArgumentType]
                options=FeishuOptions(batching=BatchSettings(text_delay_s=0.0)),
            )

            async def sink(event: InboundEvent) -> None:
                got.append(event)

            adapter.set_sink(sink)
            await adapter.connect()
            for _ in range(200):
                if got and "ack" in seen:
                    break
                await asyncio.sleep(0.01)  # noqa: TID251 - a real socket, not the fake clock
            await adapter.disconnect()
        return got

    got = run(main())
    assert [e.text for e in got] == ["over a real socket"]
    assert seen["ticket"] and seen["ack"]["headers"]["type"] == "event"
    assert seen["ticket"] not in caplog.text
    assert json.loads(seen["ack"]["payload"]) == {"code": 200}
