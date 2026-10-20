"""The WeCom smart-robot adapter over the fake socket (P16-17): the row's Verify cases
(prefix-stable stream frames, a missed heartbeat reconnects, duplicate ids ignored) and the rules
the shared contract suite does not reach."""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import socket
from collections.abc import Awaitable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.config.schema import PlatformSection
from indus.foundation.scope import make_scope
from indus.gateway.adapters.base import AdapterConnectError
from indus.gateway.adapters.wecom import create_adapter
from indus.gateway.adapters.wecom.adapter import ChatWindow, WeComAdapter, WeComOptions
from indus.gateway.adapters.wecom.stream import MAX_FRAME_BYTES, SAFE_AGE_S, cut_to_bytes
from indus.gateway.config import AdapterConfig, PlatformSettings
from indus.gateway.core.events import InboundEvent, MessageKind
from indus.gateway.core.send import ErrorKind
from tests.fakes.clock import FakeClock
from tests.fakes.platform.core import FailureKind, FakeChatPlatform
from tests.fakes.platform.wecom_wire import WeComWire

ANA = "ana"
GROUP = "wrGroup01"
HAS_CRYPTO = importlib.util.find_spec("cryptography") is not None


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("real network used in a WeCom adapter test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield


def _clock() -> FakeClock:
    return FakeClock(datetime(2026, 1, 1, 9, 0, tzinfo=UTC))


async def drive[T](clock: FakeClock, awaitable: Awaitable[T], *, step: float = 0.5) -> T:
    task = asyncio.ensure_future(awaitable)
    for _ in range(4000):
        await clock.run_until_idle()
        if task.done():
            return task.result()
        clock.advance(step)
    task.cancel()
    raise AssertionError("never finished on the fake clock")


async def settle(clock: FakeClock) -> None:
    await clock.run_until_idle()


def run(coro: Awaitable[Any]) -> Any:
    return asyncio.run(coro)  # pyright: ignore[reportArgumentType]


class Rig:
    def __init__(
        self, *, options: WeComOptions | None = None, max_len: int = 4000, **kw: Any
    ) -> None:
        self.clock = _clock()
        # Streams hold up to 20,480 bytes; ordinary messages 4,000 characters.
        platform = FakeChatPlatform("wecom", clock=self.clock, max_len=max_len)
        self.wire = WeComWire(platform)
        self.adapter = self.wire.adapter(options=options or WeComOptions(), **kw)
        self.events: list[InboundEvent] = []
        self.lost: list[tuple[str, str]] = []
        self.adapter.set_sink(self.sink)
        self.adapter.set_fatal_listener(lambda code, message: self.lost.append((code, message)))

    async def sink(self, event: InboundEvent) -> None:
        self.events.append(event)

    async def connect(self) -> None:
        await drive(self.clock, self.adapter.connect())

    async def inbound_dm(self, text: str = "hello") -> str:
        item = self.wire.platform.dm(ANA, text)
        await settle(self.clock)
        return item.message_id

    async def close(self) -> None:
        await drive(self.clock, self.adapter.disconnect())

    def cmds(self) -> list[str]:
        return [str(f.get("cmd")) for f in self.wire.frames]


# -- native streams (R06.12) ----------------------------------------------------------------------


def test_every_stream_frame_is_a_prefix_of_the_next() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        await rig.inbound_dm("tell me")
        stream = rig.adapter.open_stream(ANA)
        assert stream is not None
        for text in ("Hel", "Hello", "Hello wor"):
            assert await stream.push(text)
            await settle(rig.clock)
        assert await stream.push("Bye") is False  # rewrites sent text: skipped
        await settle(rig.clock)
        final = await drive(rig.clock, stream.finish("Hello world"))
        assert final.ok and final.message_id == stream.stream_id
        frames = rig.wire.streams[stream.stream_id]
        contents = [c for c, _ in frames]
        assert contents == ["Hel", "Hello", "Hello wor", "Hello world"]
        assert all(b.startswith(a) for a, b in zip(contents, contents[1:], strict=False))
        assert [f for _, f in frames] == [False, False, False, True]
        assert rig.wire.platform.texts(ANA) == ["Hello world"]  # the stream is the message
        assert await stream.push("Hello world!") is False
        await rig.close()

    run(main())


def test_unanswered_interims_are_skipped_and_the_final_carries_the_text() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        await rig.inbound_dm()
        stream = rig.adapter.open_stream(ANA)
        assert stream is not None
        for n in range(1, 6):
            assert await stream.push("x" * n)  # no time for WeCom to answer in between
        final = await drive(rig.clock, stream.finish("x" * 6))
        assert final.ok
        contents = [c for c, _ in rig.wire.streams[stream.stream_id]]
        assert contents[0] == "x" and contents[-1] == "x" * 6 and len(contents) < 6
        assert all(b.startswith(a) for a, b in zip(contents, contents[1:], strict=False))
        await rig.close()

    run(main())


def test_an_identical_final_still_differs_and_long_finals_spill_over() -> None:
    async def main() -> None:
        rig = Rig(max_len=MAX_FRAME_BYTES)
        await rig.connect()
        await rig.inbound_dm()
        same = rig.adapter.open_stream(ANA)
        assert same is not None
        await same.push("done")
        await settle(rig.clock)
        assert (await drive(rig.clock, same.finish("done"))).ok
        assert rig.wire.streams[same.stream_id][-1] == ("done​", True)
        await rig.inbound_dm("again")
        long = rig.adapter.open_stream(ANA)
        assert long is not None
        text = "字" * 8000  # 24,000 UTF-8 bytes
        assert await long.push(text)  # too big for a frame: held for the final
        result = await drive(rig.clock, long.finish(text))
        assert result.ok and len(result.ids) == 2
        last = rig.wire.streams[long.stream_id][-1][0]
        assert len(last.encode()) <= MAX_FRAME_BYTES and text.startswith(last)
        assert last + rig.wire.platform.texts(ANA)[-1] == text
        await rig.close()

    run(main())
    assert cut_to_bytes("é" * 3, 5) == "éé"


def test_an_old_stream_refuses_its_final_so_the_caller_sends_normally() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        await rig.inbound_dm()
        stream = rig.adapter.open_stream(ANA)
        assert stream is not None
        await stream.push("thinking")
        # Keep the socket alive while the stream ages.
        for _ in range(int(SAFE_AGE_S // 10) + 1):
            await rig.clock.advance_and_settle(10.0)
        result = await drive(rig.clock, stream.finish("thinking done"))
        assert result.unsupported and not stream.finished
        await rig.close()

    run(main())


def test_no_stream_without_an_inbound_message_or_when_turned_off() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        assert rig.adapter.open_stream(ANA) is None
        off = Rig(options=WeComOptions(native_stream=False))
        await off.connect()
        await off.inbound_dm()
        assert off.adapter.open_stream(ANA) is None
        await rig.close()
        await off.close()

    run(main())


# -- the socket ------------------------------------------------------------------------------------


def test_a_missed_heartbeat_reconnects() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        assert len(rig.wire.sockets) == 1
        await rig.clock.advance_and_settle(30.0)
        assert rig.cmds().count("ping") == 1 and len(rig.wire.sockets) == 1
        rig.wire.muted.add("ping")
        await rig.clock.advance_and_settle(30.0)  # the next ping goes unanswered
        await rig.clock.advance_and_settle(15.0)  # ... for 15 s: the socket is given up
        await rig.clock.advance_and_settle(2.0)  # first backoff step
        await rig.clock.advance_and_settle(1.0)
        assert len(rig.wire.sockets) == 2 and rig.wire.sockets[0].closed
        assert rig.cmds().count("aibot_subscribe") == 2
        assert rig.adapter.socket_opens == 2
        rig.wire.muted.clear()
        await rig.inbound_dm("after the reconnect")
        assert [e.text for e in rig.events] == ["after the reconnect"]
        await rig.close()

    run(main())


def test_duplicate_message_ids_are_ignored() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        body = {
            "msgid": "dup-1",
            "chattype": "single",
            "from": {"userid": ANA},
            "msgtype": "text",
            "text": {"content": "once"},
        }
        rig.wire.callback(body, chat_id=ANA)
        rig.wire.callback(body, chat_id=ANA)
        await settle(rig.clock)
        assert [e.text for e in rig.events] == ["once"]
        await rig.close()

    run(main())


def test_a_takeover_reconnects_once_then_two_within_ten_minutes_are_fatal() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        rig.wire.take_over()
        await settle(rig.clock)
        await rig.clock.advance_and_settle(59.0)
        assert len(rig.wire.sockets) == 1
        await rig.clock.advance_and_settle(1.5)
        assert len(rig.wire.sockets) == 2 and rig.lost == []
        rig.wire.take_over()
        await settle(rig.clock)
        await rig.clock.advance_and_settle(120.0)
        assert [code for code, _ in rig.lost] == ["wecom_replaced"] and len(rig.wire.sockets) == 2
        await rig.close()

    run(main())


def test_a_refused_subscription_is_a_fatal_connect_error() -> None:
    async def main() -> None:
        clock = _clock()
        wire = WeComWire(clock=clock)
        adapter = WeComAdapter(clock=clock, bot_id=wire.bot_id, secret="wrong", opener=wire.open)
        with pytest.raises(AdapterConnectError) as raised:
            await drive(clock, adapter.connect())
        assert raised.value.code == "auth_failed" and not raised.value.retryable
        assert not adapter.connected and wire.sockets[0].closed
        blank = WeComAdapter(clock=clock, bot_id="", secret="")
        with pytest.raises(AdapterConnectError, match="WECOM_BOT_ID and WECOM_SECRET"):
            await drive(clock, blank.connect())
        missing = WeComAdapter(
            clock=clock,
            bot_id=wire.bot_id,
            secret=wire.secret,
            opener=_missing_httpx_ws,
        )
        with pytest.raises(AdapterConnectError) as gone:
            await drive(clock, missing.connect())
        assert gone.value.code == "missing_dependency" and "indus-agent[wecom]" in str(gone.value)

    run(main())


def _missing_httpx_ws(client: Any, url: str) -> Any:
    raise ModuleNotFoundError(
        "the WeCom socket needs httpx-ws: pip install 'indus-agent[wecom]'", name="httpx_ws"
    )


# -- inbound ---------------------------------------------------------------------------------------


def test_group_mentions_are_cut_and_policies_apply() -> None:
    async def main() -> None:
        rig = Rig(options=WeComOptions(dm_policy="disabled"))
        await rig.connect()
        rig.wire.platform.group(GROUP, ANA, "/approve", mention=True)
        rig.wire.platform.dm(ANA, "a DM")
        await settle(rig.clock)
        assert [(e.text, e.source.chat_type) for e in rig.events] == [("/approve", "group")]
        assert rig.events[0].metadata["mentioned"] is True
        assert rig.events[0].source.chat_id == GROUP and rig.events[0].user_id == ANA
        assert rig.adapter.access_policy("dm") == "disabled"
        await rig.close()

    run(main())
    allow = WeComAdapter(
        clock=_clock(),
        bot_id="b",
        secret="s",
        options=WeComOptions(dm_policy="allowlist", allow_from=frozenset({"ana"})),
    )
    assert allow.allows_dm("Ana") and not allow.allows_dm("ben")


@pytest.mark.skipif(not HAS_CRYPTO, reason="needs the cryptography package (wecom extra)")
def test_inbound_media_is_downloaded_and_decrypted() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        rig.wire.platform.document(ANA, ANA, ref="notes.txt")
        rig.wire.platform.photo(ANA, ANA, caption="look")
        rig.wire.platform.voice(ANA, ANA)
        await settle(rig.clock)
        doc, photo, voice = rig.events
        assert doc.kind is MessageKind.DOCUMENT and photo.kind is MessageKind.PHOTO
        assert photo.text == "look" and voice.kind is MessageKind.VOICE and not voice.media
        data = b"".join([c async for c in rig.adapter.fetch_media(doc.media[0])])
        assert data == b"bytes of notes.txt"
        rig.clock.advance(301)
        with pytest.raises(LookupError):
            [c async for c in rig.adapter.fetch_media(doc.media[0])]
        await rig.close()

    run(main())


def test_quotes_become_reply_context() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        quote = {"msgtype": "text", "text": {"content": "the earlier words"}}
        body = {"chattype": "single", "from": {"userid": ANA}, "msgtype": "text"}
        rig.wire.callback(
            {**body, "msgid": "q1", "text": {"content": "why?"}, "quote": quote}, chat_id=ANA
        )
        rig.wire.callback(
            {**body, "msgid": "q2", "text": {"content": ""}, "quote": quote}, chat_id=ANA
        )
        await settle(rig.clock)
        got = [(e.text, e.reply_to_text) for e in rig.events]
        assert got == [("why?", "the earlier words"), ("the earlier words", None)]
        await rig.close()

    run(main())


def test_a_lost_subscription_drops_the_reply_anchors() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        await rig.inbound_dm()
        failed = rig.adapter._failure({"errcode": 846609, "errmsg": "not subscribed"})  # pyright: ignore[reportPrivateUsage]
        assert failed.retryable and rig.adapter.open_stream(ANA) is None
        await rig.close()

    run(main())


def test_a_send_only_adapter_takes_no_callbacks() -> None:
    async def main() -> None:
        rig = Rig(send_only=True)
        await rig.connect()
        await rig.inbound_dm("ignored")
        assert rig.events == []
        assert (await drive(rig.clock, rig.adapter.send(GROUP, "deploy done"))).ok
        await rig.close()

    run(main())


# -- outbound --------------------------------------------------------------------------------------


def test_replies_are_passive_when_possible_else_proactive() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        message_id = await rig.inbound_dm("question")
        req = next(iter(r for r, chat in rig.wire.req_chat.items() if chat == ANA))
        first = await drive(rig.clock, rig.adapter.send(ANA, "answer", reply_to=message_id))
        second = await drive(rig.clock, rig.adapter.send(ANA, "more"))
        assert (first.message_id, second.message_id) == (f"{req}:1", f"{req}:2")
        assert rig.cmds()[-2:] == ["aibot_respond_msg", "aibot_respond_msg"]
        cold = await drive(rig.clock, rig.adapter.send(GROUP, "cron output"))
        assert cold.ok and rig.cmds()[-1] == "aibot_send_msg"
        assert rig.wire.frames[-1]["body"]["chat_type"] == 0
        assert rig.wire.platform.message(cold.message_id or "") is not None
        # The reply anchor expired: WeCom says so and the adapter writes to the chat instead.
        rig.wire.req_chat.pop(req)
        late = await drive(rig.clock, rig.adapter.send(ANA, "much later"))
        assert late.ok and rig.cmds()[-2:] == ["aibot_respond_msg", "aibot_send_msg"]
        assert rig.wire.frames[-1]["body"]["chat_type"] == 1
        await rig.close()

    run(main())


def test_thirty_messages_a_minute_per_chat() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        for n in range(30):
            assert (await drive(rig.clock, rig.adapter.send(GROUP, f"m{n}"))).ok
        limited = await drive(rig.clock, rig.adapter.send(GROUP, "one too many"))
        assert limited.kind is ErrorKind.RATE_LIMITED and limited.retry_after is not None
        assert 0 < limited.retry_after <= 60
        assert (await drive(rig.clock, rig.adapter.send("other", "elsewhere"))).ok
        await rig.close()

    run(main())
    window = ChatWindow(_clock(), limit=2, period=60.0)
    assert window.take("a") == 0 and window.take("a") == 0 and window.take("a") > 0


def test_send_failures_are_classified() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        rig.wire.platform.fail(FailureKind.READ_TIMEOUT, op="send")
        lost = await drive(rig.clock, rig.adapter.send(GROUP, "x"))
        assert lost.maybe_delivered and not lost.retryable
        rig.wire.platform.fail(FailureKind.CONNECT_TIMEOUT, op="send")
        never = await drive(rig.clock, rig.adapter.send(GROUP, "x"))
        assert never.retryable and never.kind is ErrorKind.TRANSIENT
        await rig.close()

    run(main())


def test_media_goes_up_in_chunks_with_wecom_limits(tmp_path: Path) -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        big = tmp_path / "report.pdf"
        big.write_bytes(b"r" * (600 * 1024))
        result = await drive(rig.clock, rig.adapter.send_document(GROUP, str(big), caption="hi"))
        assert result.ok and len(result.ids) == 2
        chunks = [f for f in rig.wire.frames if f["cmd"] == "aibot_upload_media_chunk"]
        assert [c["body"]["chunk_index"] for c in chunks] == [0, 1]
        init = next(f for f in rig.wire.frames if f["cmd"] == "aibot_upload_media_init")
        assert init["body"]["type"] == "file" and init["body"]["total_chunks"] == 2
        voice = tmp_path / "note.ogg"
        voice.write_bytes(b"o" * 100)
        assert (await drive(rig.clock, rig.adapter.send_voice(GROUP, str(voice)))).ok
        inits = [f for f in rig.wire.frames if f["cmd"] == "aibot_upload_media_init"]
        assert inits[-1]["body"]["type"] == "file"  # WeCom voice is AMR only
        huge = tmp_path / "huge.bin"
        huge.write_bytes(b"h" * (20 * 1024 * 1024 + 1))
        refused = await drive(rig.clock, rig.adapter.send_document(GROUP, str(huge)))
        assert not refused.ok and refused.kind is ErrorKind.TOO_LONG
        tiny = tmp_path / "tiny.txt"
        tiny.write_bytes(b"ab")
        assert not (await drive(rig.clock, rig.adapter.send_document(GROUP, str(tiny)))).ok
        await rig.close()

    run(main())


# -- settings and secrets --------------------------------------------------------------------------


def test_create_adapter_reads_secrets_and_options(tmp_path: Path) -> None:
    env = {"WECOM_BOT_ID": "bot-from-env", "WECOM_SECRET": "q" * 30}
    scope = make_scope(tmp_path / "home", env=env, clock=_clock())
    section = PlatformSection.model_validate(
        {"extra": {"dm_policy": "allowlist", "allow_from": "Ana, ben", "native_stream": "false"}}
    )
    settings = PlatformSettings("wecom", True, "credentials", section)
    adapter = create_adapter(AdapterConfig("wecom", settings, scope, scope.clock))
    assert adapter.options.dm_policy == "allowlist" and adapter.options.allow_from == {"ana", "ben"}
    assert adapter.options.native_stream is False
    assert "q" * 30 not in adapter.scrub("secret " + "q" * 30)
    assert repr(adapter) == "<WeComAdapter disconnected>"


def test_the_secret_never_reaches_the_logs(caplog: pytest.LogCaptureFixture) -> None:
    secret: list[str] = []

    async def main() -> None:
        rig = Rig()
        secret.append(rig.wire.secret)
        await rig.connect()
        rig.wire.drop_socket()
        await rig.clock.advance_and_settle(1.0)
        await rig.clock.advance_and_settle(3.0)
        rig.wire.platform.fail(FailureKind.FORBIDDEN, op="send")
        failed = await drive(rig.clock, rig.adapter.send(GROUP, f"leak {rig.wire.secret}"))
        assert failed.kind is ErrorKind.FORBIDDEN and rig.wire.secret not in (failed.error or "")
        await rig.close()

    with caplog.at_level(logging.DEBUG):
        run(main())
    assert "socket closed" in caplog.text
    assert secret[0] not in caplog.text
