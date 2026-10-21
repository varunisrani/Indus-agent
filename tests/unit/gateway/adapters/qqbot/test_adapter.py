"""The QQ Bot adapter (P16-20) over the fake QQ API and gateway."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import socket
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.config.schema import PlatformSection
from indus.foundation.scope import Scope
from indus.gateway.adapters.base import (
    AdapterConnectError,
    ApprovalAction,
    ApprovalPrompt,
    ClarifyPrompt,
    approval_callback,
)
from indus.gateway.adapters.qqbot import create_adapter
from indus.gateway.adapters.qqbot.adapter import (
    QQBotAdapter,
    QQBotMarkdownAdapter,
    QQBotOptions,
    parse_target,
)
from indus.gateway.adapters.qqbot.client import ROUTES, SANDBOX_BASE
from indus.gateway.adapters.qqbot.gateway import INTENTS, MAX_ATTEMPTS
from indus.gateway.config import AdapterConfig, PlatformSettings
from indus.gateway.core.events import Attachment, InboundEvent, MessageKind
from indus.gateway.core.send import ErrorKind
from tests.fakes.clock import FakeClock, hold_for_off_loop
from tests.fakes.platform.core import FailureKind, FakeChatPlatform
from tests.fakes.platform.qqbot_wire import GUILD, HEARTBEAT_MS, MEDIA_HOST, QQWire

ANA = "U-ana"
GROUP = "group:G1"


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("real network used in a QQ Bot adapter test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield


def run[T](coro: Awaitable[T]) -> T:
    return asyncio.run(coro)  # pyright: ignore[reportArgumentType]


async def drive[T](clock: FakeClock, awaitable: Awaitable[T], *, step: float = 0.5) -> T:
    task = asyncio.ensure_future(awaitable)
    for _ in range(4000):
        await clock.run_until_idle()
        # Uploads read and hash files on a worker thread: give it real time before moving on.
        await asyncio.wait({task}, timeout=0.002)
        await hold_for_off_loop(clock)  # and never move the clock while one is still awaited
        if task.done():
            return task.result()
        clock.advance(step)
    task.cancel()
    raise AssertionError("never finished on the fake clock")


async def settle(clock: FakeClock, seconds: float = 4.0, step: float = 1.0) -> None:
    for _ in range(int(seconds / step)):
        await clock.advance_and_settle(step)


async def until(clock: FakeClock, test: Callable[[], bool], limit: float = 600.0) -> None:
    start = clock.monotonic()
    while not test():
        if clock.monotonic() - start > limit:
            raise AssertionError("condition never held on the fake clock")
        await hold_for_off_loop(clock)  # never move the clock under an awaited off-loop call
        await clock.advance_and_settle(0.5)


class Rig:
    def __init__(
        self,
        *,
        sink: bool = True,
        send_only: bool = False,
        options: QQBotOptions | None = None,
        cls: type[QQBotAdapter] = QQBotAdapter,
    ) -> None:
        self.clock = FakeClock(datetime(2026, 1, 1, 9, 0, tzinfo=UTC))
        self.platform = FakeChatPlatform("qqbot", clock=self.clock)
        self.wire = QQWire(self.platform)
        self.adapter = cls(
            clock=self.clock,
            app_id=self.wire.app_id,
            secret=self.wire.secret,
            options=options,
            transport=self.wire.transport(),
            opener=self.wire.opener,
            send_only=send_only,
        )
        self.events: list[InboundEvent] = []
        self.fatal: list[tuple[str, str]] = []
        self.adapter.set_fatal_listener(lambda code, message: self.fatal.append((code, message)))
        if sink:
            self.adapter.set_sink(self.sink)

    async def sink(self, event: InboundEvent) -> None:
        self.events.append(event)

    async def connect(self) -> None:
        await drive(self.clock, self.adapter.connect())
        await settle(self.clock, 2)

    async def close(self) -> None:
        await drive(self.clock, self.adapter.disconnect())

    @property
    def gateway(self) -> Any:
        assert self.adapter.gateway is not None
        return self.adapter.gateway


# -- the row's Verify ------------------------------------------------------------------------------


def test_resume_after_a_drop_keeps_the_sequence() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        identify = rig.wire.client_frames[0]
        assert identify["op"] == 2 and identify["d"]["intents"] == INTENTS
        assert identify["d"]["shard"] == [0, 1]
        assert identify["d"]["token"] == f"QQBot {rig.wire.token}"
        rig.platform.dm(ANA, "one")
        rig.platform.dm(ANA, "two")
        await settle(rig.clock)
        last = rig.gateway.seq
        assert last is not None and last >= 3  # READY + two messages
        rig.wire.drop_socket()
        await until(rig.clock, lambda: bool(rig.wire.resume_frames))
        resume = rig.wire.resume_frames[0]
        assert resume["seq"] == last and resume["session_id"] == "session-1"
        assert rig.wire.identify_count == 1 and len(rig.wire.sockets) == 2
        assert rig.gateway.waits == [2.0]
        rig.platform.dm(ANA, "three")
        await settle(rig.clock)
        assert [e.text for e in rig.events] == ["one", "two", "three"]
        await rig.close()

    run(main())


def test_the_daily_upload_limit_is_not_retried(tmp_path: Path) -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        path = tmp_path / "a.png"
        path.write_bytes(b"png!")
        rig.wire.daily_limit = True
        result = await drive(rig.clock, rig.adapter.send_image(ANA, str(path)))
        assert (
            not result.ok and not result.retryable and "daily upload limit" in (result.error or "")
        )
        prepares = [c for c in rig.wire.calls if c.path.endswith("/upload_prepare")]
        assert len(prepares) == 1
        await rig.close()

    run(main())


# -- the gateway session --------------------------------------------------------------------------


def test_heartbeats_follow_the_hello_interval_and_a_missing_ack_replaces_the_socket() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        beat = HEARTBEAT_MS / 1000 * 0.8
        assert rig.gateway.heartbeat_s == pytest.approx(beat)
        await until(rig.clock, lambda: len(rig.wire.heartbeats) == 1)
        assert rig.wire.heartbeats == [rig.gateway.seq]
        rig.wire.ack_heartbeats = False
        await until(rig.clock, lambda: len(rig.wire.sockets) == 2)
        await until(rig.clock, lambda: bool(rig.wire.resume_frames))
        assert rig.fatal == []
        await rig.close()

    run(main())


def test_server_requests_op7_and_op9() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        rig.wire.push({"op": 7})
        await until(rig.clock, lambda: len(rig.wire.resume_frames) == 1)
        rig.wire.push({"op": 9, "d": True})
        await until(rig.clock, lambda: len(rig.wire.resume_frames) == 2)
        rig.wire.push({"op": 9, "d": False})
        await until(rig.clock, lambda: rig.wire.identify_count == 2)
        assert len(rig.wire.resume_frames) == 2
        await rig.close()

    run(main())


def test_close_codes() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        rig.clock.advance(10)
        rig.wire.drop_socket(4004)
        await until(rig.clock, lambda: rig.wire.tokens_issued == 2)
        rig.clock.advance(10)
        rig.wire.drop_socket(4006)
        await until(rig.clock, lambda: rig.wire.identify_count == 2)
        rig.clock.advance(10)
        rig.wire.drop_socket(4008)
        await until(rig.clock, lambda: len(rig.wire.sockets) == 4)
        assert rig.gateway.waits[-1] == 60.0
        rig.clock.advance(10)
        rig.wire.drop_socket(4914)
        await until(rig.clock, lambda: bool(rig.fatal))
        assert rig.fatal[0][0] == "qqbot_closed" and "offline" in rig.fatal[0][1]
        await settle(rig.clock, 120, 10)
        assert len(rig.wire.sockets) == 4 and not rig.adapter.listening
        await rig.close()

    run(main())


def test_three_quick_closes_in_a_row_are_fatal() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        for n in range(3):
            rig.wire.drop_socket(4009)
            await until(rig.clock, lambda n=n: len(rig.wire.sockets) > n + 1 or bool(rig.fatal))
        assert rig.fatal and rig.fatal[0][0] == "qqbot_quick_disconnect"
        await rig.close()

    run(main())


def test_a_hundred_failed_attempts_are_fatal() -> None:
    async def main() -> None:
        rig = Rig()
        rig.wire.socket_failures = [OSError("refused") for _ in range(MAX_ATTEMPTS)]
        await rig.connect()
        await until(rig.clock, lambda: bool(rig.fatal), limit=10 * 3600)
        assert rig.fatal[0][0] == "qqbot_reconnect"
        assert len(rig.wire.socket_urls) == MAX_ATTEMPTS
        assert rig.gateway.waits[:6] == [2.0, 5.0, 10.0, 30.0, 60.0, 60.0]
        await rig.close()

    run(main())


def test_without_httpx_ws_the_listener_is_fatal_with_the_install_hint() -> None:
    if importlib.util.find_spec("httpx_ws") is not None:
        pytest.skip("httpx-ws is installed")

    async def main() -> None:
        rig = Rig(sink=False)
        rig.adapter._opener_factory = __import__(  # pyright: ignore[reportPrivateUsage]
            "indus.gateway.adapters.qqbot.client", fromlist=["httpx_ws_opener"]
        ).httpx_ws_opener
        rig.adapter.set_sink(rig.sink)
        await rig.connect()
        await settle(rig.clock)
        assert rig.fatal and rig.fatal[0][0] == "missing_dependency"
        assert "indus-agent[qqbot]" in rig.fatal[0][1]
        sent = await drive(rig.clock, rig.adapter.send(ANA, "sending still works"))
        assert sent.ok
        await rig.close()

    run(main())


# -- inbound --------------------------------------------------------------------------------------


def test_chat_kinds_ids_mentions_and_dedupe() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        rig.platform.dm(ANA, "private")
        rig.platform.group(GROUP, "M-ben", "/status", mention=True)
        rig.platform.group("channel:C9", "1234", "in a channel", mention=True, user_name="Ben")
        rig.platform.group("dm:" + GUILD, "1234", "guild dm")  # rendered by its dm: prefix
        rig.platform.bot_message("channel:C9", "4321", "beep")
        await settle(rig.clock)
        rig.wire.dispatch(
            "C2C_MESSAGE_CREATE", {"id": "m-x", "content": "again", "author": {"user_openid": ANA}}
        )
        rig.wire.dispatch(
            "C2C_MESSAGE_CREATE", {"id": "m-x", "content": "again", "author": {"user_openid": ANA}}
        )
        rig.wire.dispatch(
            "C2C_MESSAGE_CREATE", {"id": "m-y", "content": "  ", "author": {"user_openid": ANA}}
        )
        rig.wire.dispatch("FORUM_THREAD_CREATE", {"id": "z"})
        rig.wire.push({"op": 99})
        await settle(rig.clock)
        texts = [e.text for e in rig.events]
        assert texts == ["private", "/status", "in a channel", "guild dm", "beep", "again"]
        dm, group, channel, guild_dm, bot, _ = rig.events
        assert dm.source.chat_id == ANA and dm.source.chat_type == "dm" and dm.user_id == ANA
        assert group.source.chat_id == GROUP and group.source.chat_type == "group"
        assert group.user_id == "M-ben" and group.metadata == {"mentioned": True}
        assert group.command == "status"
        assert channel.source.chat_id == "channel:C9" and channel.source.chat_type == "channel"
        assert channel.metadata == {"mentioned": True} and channel.source.user_name == "Ben"
        assert channel.source.guild_id == GUILD
        assert guild_dm.source.chat_id == "dm:" + GUILD and guild_dm.source.chat_type == "dm"
        assert bot.source.is_bot
        assert dm.timestamp is not None
        await rig.close()

    run(main())


def test_attachments_voice_transcripts_and_the_fetcher() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        rig.wire.media_files["pic.jpg"] = b"JPEG"
        rig.wire.dispatch(
            "C2C_MESSAGE_CREATE",
            {
                "id": "v1",
                "content": "",
                "author": {"user_openid": ANA},
                "attachments": [
                    {
                        "content_type": "voice",
                        "url": "//x.qq.com/v.silk",
                        "asr_refer_text": "hello from voice",
                    },
                ],
            },
        )
        rig.platform.photo(ANA, ANA, "pic.jpg", caption="look")
        await settle(rig.clock)
        voice, photo = rig.events
        assert voice.kind is MessageKind.VOICE and voice.text == "hello from voice"
        assert voice.metadata == {"transcript": "hello from voice"}
        assert voice.media[0].ref == "https://x.qq.com/v.silk"
        assert voice.media[0].mime == "audio/silk"
        assert photo.kind is MessageKind.PHOTO and photo.text == "look"
        assert photo.media[0].ref == f"https://{MEDIA_HOST}/download/pic.jpg"
        data = b"".join([c async for c in rig.adapter.fetch_media(photo.media[0])])
        assert data == b"bytes of pic.jpg"
        for url in (
            "https://evil.example/pic.jpg",
            f"http://{MEDIA_HOST}/download/pic.jpg",
            "https://qq.com.evil.example/x",
        ):
            with pytest.raises(ValueError):
                _ = [c async for c in rig.adapter.fetch_media(Attachment(ref=url))]
        assert not any("evil" in str(c.path) for c in rig.wire.calls)
        await rig.close()

    run(main())


def test_button_presses_are_acknowledged_and_handed_on() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        data = approval_callback("req1", "once")
        rig.platform.button_press(ANA, ANA, data)
        rig.platform.button_press(GROUP, "M-ben", data, chat_kind="group")
        await settle(rig.clock)
        c2c, group = rig.events
        assert c2c.metadata == {"button": data} and c2c.source.chat_id == ANA
        assert group.source.chat_id == GROUP and group.user_id == "M-ben"
        assert len(rig.wire.acks) == 2
        await rig.close()

    run(main())


# -- outbound -------------------------------------------------------------------------------------


def test_passive_replies_name_the_message_with_a_growing_seq() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        rig.platform.dm(ANA, "question")
        await settle(rig.clock)
        (question,) = rig.events
        long = " ".join(f"w{n:05d}" for n in range(1200))  # about 8,400 characters
        sent = await drive(rig.clock, rig.adapter.send(ANA, long, reply_to=question.message_id))
        assert sent.ok and len(sent.ids) >= 3
        pieces = rig.platform.ops_of("send")
        assert {op.extra["msg_id"] for op in pieces} == {question.message_id}
        assert [op.extra["msg_seq"] for op in pieces] == list(range(1, len(pieces) + 1))
        assert all(op.extra["msg_type"] == 0 for op in pieces)
        rig.clock.advance(301)
        await drive(rig.clock, rig.adapter.send(ANA, "much later"))
        assert rig.platform.ops_of("send")[-1].extra["msg_id"] is None
        await rig.close()

    run(main())


def test_guild_bodies_markdown_and_errors() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        await drive(rig.clock, rig.adapter.send("channel:C9", "hi channel"))
        body = rig.wire.calls_to("POST", "/channels/C9/messages")[-1].json()
        assert body == {"content": "hi channel"}
        await drive(rig.clock, rig.adapter.send("dm:G5", "hi dm"))
        assert rig.wire.calls_to("POST", "/dms/G5/messages")
        bad = await drive(rig.clock, rig.adapter.send("group:../x", "hi"))
        assert bad.kind is ErrorKind.NOT_FOUND
        rig.platform.fail(FailureKind.RATE_LIMIT, op="send", retry_after=7)
        limited = await drive(rig.clock, rig.adapter.send(GROUP, "x"))
        assert limited.kind is ErrorKind.RATE_LIMITED and limited.retry_after == 7
        await rig.close()

        md = Rig(cls=QQBotMarkdownAdapter)
        await md.connect()
        await drive(md.clock, md.adapter.send(GROUP, "**bold**"))
        body = md.wire.calls_to("POST", "/v2/groups/G1/messages")[-1].json()
        assert body["msg_type"] == 2 and body["markdown"] == {"content": "**bold**"}
        assert md.adapter.capabilities.markup == "markdown"
        await md.close()

    run(main())


def test_typing_only_in_c2c_with_a_fresh_message_and_at_most_every_50_seconds() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        await drive(rig.clock, rig.adapter.typing_start(ANA))
        assert rig.platform.ops_of("typing") == []  # no inbound message yet
        rig.platform.dm(ANA, "hi")
        rig.platform.group(GROUP, "M-ben", "hi", mention=True)
        await settle(rig.clock)
        for chat in (ANA, ANA, GROUP):
            await drive(rig.clock, rig.adapter.typing_start(chat))
        assert [op.chat_id for op in rig.platform.ops_of("typing")] == [ANA]
        rig.clock.advance(51)
        await drive(rig.clock, rig.adapter.typing_start(ANA))
        assert len(rig.platform.ops_of("typing")) == 2
        typing = [c for c in rig.wire.calls if c.json() and c.json().get("msg_type") == 6]
        assert typing[0].json()["input_notify"] == {"input_type": 1, "input_second": 60}
        await rig.close()

    run(main())


def test_keyboards_for_approvals_and_choices_text_in_guild_chats() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        prompt = ApprovalPrompt(
            "r1",
            "k",
            "Run it?",
            "Run it? Reply /approve or /deny.",
            tuple(ApprovalAction(c, c) for c in ("once", "session", "always", "deny")),
        )
        card = await drive(rig.clock, rig.adapter.send_approval(GROUP, prompt))
        message = rig.platform.message(card.message_id or "")
        assert message is not None
        assert [b.data for b in message.buttons] == [
            approval_callback("r1", c) for c in ("once", "session", "always", "deny")
        ]
        keyboard = rig.wire.calls_to("POST", "/v2/groups/G1/messages")[-1].json()["keyboard"]
        first = keyboard["content"]["rows"][0]["buttons"][0]
        assert first["action"]["type"] == 1 and first["action"]["permission"] == {"type": 2}
        text = await drive(rig.clock, rig.adapter.send_approval("channel:C9", prompt))
        guild = rig.platform.message(text.message_id or "")
        assert guild is not None and "/approve" in guild.text and guild.buttons == ()
        choices = ClarifyPrompt("c1", "k", "Pick", tuple(f"choice {n}" for n in range(7)))
        asked = await drive(rig.clock, rig.adapter.send_clarify(ANA, choices))
        rows = rig.wire.calls_to("POST", f"/v2/users/{ANA}/messages")[-1].json()["keyboard"]
        assert [len(r["buttons"]) for r in rows["content"]["rows"]] == [5, 2]
        assert asked.ok
        await rig.close()

    run(main())


def test_media_upload_and_send(tmp_path: Path) -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        rig.platform.dm(ANA, "send me the report")
        await settle(rig.clock)
        path = tmp_path / "report.pdf"
        path.write_bytes(b"%PDF report")
        sent = await drive(
            rig.clock, rig.adapter.send_document(ANA, str(path), caption="here it is")
        )
        assert sent.ok
        (op,) = rig.platform.ops_of("media")
        assert op.extra == {"kind": "document", "ref": "report.pdf"} and op.text == "here it is"
        assert op.reply_to == rig.events[0].message_id
        stored = next(u for k, u in rig.wire.uploads.items() if k.startswith("fi-"))
        assert stored["data"] == b"%PDF report" and stored["file_type"] == 4
        refused = await drive(rig.clock, rig.adapter.send_image("channel:C9", str(path)))
        assert refused.unsupported
        big = QQBotOptions(max_media_bytes=3)
        rig.adapter.options = big
        too_big = await drive(rig.clock, rig.adapter.send_image(ANA, str(path)))
        assert not too_big.ok and "limit" in (too_big.error or "")
        await rig.close()

    run(main())


def test_a_401_refreshes_the_token_once() -> None:
    async def main() -> None:
        rig = Rig(sink=False)
        await rig.connect()
        rig.wire.token = "".join(["qq", "tok", "-0002"])
        sent = await drive(rig.clock, rig.adapter.send(ANA, "after rotation"))
        assert sent.ok and rig.wire.tokens_issued == 2
        await rig.close()

    run(main())


# -- lifecycle ------------------------------------------------------------------------------------


def test_send_only_never_opens_the_gateway() -> None:
    async def main() -> None:
        rig = Rig(send_only=True)
        await rig.connect()
        assert not rig.adapter.listening and rig.wire.socket_urls == []
        await drive(rig.clock, rig.adapter.send(GROUP, "deploy done"))
        await rig.close()
        assert [op.kind for op in rig.platform.ops] == ["connect", "send", "disconnect"]
        assert not rig.wire.calls_to("GET", "/gateway")

    run(main())


def test_connect_problems() -> None:
    async def main() -> None:
        rig = Rig()
        rig.wire.secret = "something-else"
        with pytest.raises(AdapterConnectError) as caught:
            await rig.connect()
        assert not caught.value.retryable and caught.value.code == "qqbot_auth"
        empty = QQBotAdapter(clock=rig.clock, app_id="", secret="")
        with pytest.raises(AdapterConnectError) as caught:
            await empty.connect()
        assert caught.value.code == "config" and "QQ_APP_ID" in str(caught.value)
        rig = Rig()
        rig.platform.fail_connect(FailureKind.CONNECT_TIMEOUT)
        with pytest.raises(AdapterConnectError) as caught:
            await rig.connect()
        assert caught.value.retryable

    run(main())


def test_create_adapter_reads_secrets_and_settings(scope: Scope) -> None:
    secrets: Any = scope.secrets
    secrets.set("QQ_APP_ID", "102000009")
    secrets.set("QQ_CLIENT_SECRET", "s" * 24)
    section = PlatformSection.model_validate({"markdown": True, "sandbox": "yes"})
    settings = PlatformSettings("qqbot", True, "credentials", section)
    adapter = create_adapter(AdapterConfig("qqbot", settings, scope, scope.clock, None))
    assert isinstance(adapter, QQBotMarkdownAdapter) and adapter.app_id == "102000009"
    assert adapter.options.sandbox and adapter.options.markdown
    assert "s" * 24 not in adapter.scrub("leak " + "s" * 24)
    assert repr(adapter) == "<QQBotMarkdownAdapter app 102000009 disconnected>"
    plain = create_adapter(
        AdapterConfig(
            "qqbot",
            PlatformSettings("qqbot", True, "x", PlatformSection()),
            scope,
            scope.clock,
            None,
            send_only=True,
        )
    )
    assert type(plain) is QQBotAdapter and plain.send_only
    assert SANDBOX_BASE.startswith("https://sandbox.")


def test_secrets_never_reach_logs_errors_or_reprs(caplog: pytest.LogCaptureFixture) -> None:
    async def main() -> tuple[str, str]:
        rig = Rig()
        await rig.connect()
        rig.platform.fail(FailureKind.FORBIDDEN, op="send")
        failed = await drive(rig.clock, rig.adapter.send(ANA, "x"))
        assert failed.kind is ErrorKind.FORBIDDEN
        rig.wire.drop_socket(4004)
        await settle(rig.clock, 10)
        await rig.close()
        for item in (repr(rig.adapter), failed.error or "", repr(rig.adapter.client)):
            assert rig.wire.secret not in item and rig.wire.token not in item
        return rig.wire.secret, rig.wire.token

    with caplog.at_level(logging.DEBUG):
        secret, token = run(main())
    for record in caplog.records:
        text = record.getMessage()
        assert secret not in text and token not in text


def test_targets_and_routes() -> None:
    assert parse_target("U1").kind == "c2c" and parse_target("c2c:U1").id == "U1"
    assert parse_target("group:G1").kind == "group" and parse_target("dm:G").kind == "dm"
    assert parse_target("channel:1").kind == "channel"
    for bad in ("", "group:", "group:a/b", "a b", "x" * 200):
        with pytest.raises(ValueError):
            parse_target(bad)
    used = QQBotAdapter.vendor_names["indus.gateway.adapters.qqbot.client:QQClient"]
    assert {"send_message", "files_call", "ack_interaction"} <= set(used)
    assert "send_c2c" in ROUTES and "upload_prepare" in ROUTES
    frames = json.dumps({"op": 2})
    assert json.loads(frames)["op"] == 2


def test_a_long_qq_rate_limit_is_handed_over_without_sleeping() -> None:
    """R06.3: QQ's stated wait above the inline cap comes back typed; nothing sleeps it."""

    async def main() -> None:
        rig = Rig(sink=False)
        await rig.connect()
        rig.platform.fail(FailureKind.RATE_LIMIT, op="send", retry_after=3600, times=5)
        before = rig.clock.monotonic()
        result = await rig.adapter.send_with_retry(GROUP, "busy", rng=lambda: 0.0)
        assert result.is_flood and result.retry_after == 3600
        assert rig.clock.monotonic() == before
        assert len(rig.wire.calls_to("POST", "/v2/groups/G1/messages")) == 1
        await rig.close()

    run(main())
