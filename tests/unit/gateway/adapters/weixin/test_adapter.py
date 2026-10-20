"""The Weixin adapter over the fake iLink service (P16-19): the row's Verify cases and the rules the
shared contract suite does not reach."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import socket
import stat
import threading
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.config.schema import PlatformSection
from indus.foundation.scope import make_scope
from indus.gateway.adapters.base import AdapterConnectError
from indus.gateway.adapters.weixin import create_adapter
from indus.gateway.adapters.weixin import media as wxmedia
from indus.gateway.adapters.weixin.adapter import (
    NO_TOKEN,
    ContextTokens,
    WeixinAdapter,
    WeixinOptions,
    prepare_text,
)
from indus.gateway.adapters.weixin.api import ROUTES as CLIENT_ROUTES
from indus.gateway.config import AdapterConfig, PlatformSettings
from indus.gateway.core.events import Attachment, InboundEvent, MessageKind
from indus.gateway.core.send import ErrorKind, SendResult
from tests.fakes.clock import FakeClock
from tests.fakes.platform.weixin_wire import BOT_ACCOUNT, BOT_NAME, ROUTES, WeixinWire, fake_token

ANA = "wxid_ana01"
BEN = "wxid_ben02"
ROOM = "5520001@chatroom"
VECTORS = (
    Path(__file__).parents[4] / "fixtures" / "gateway" / "crypto_vectors" / "weixin_media.json"
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("real network used in a Weixin adapter test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield


def run(coro: Awaitable[Any]) -> Any:
    return asyncio.run(coro)  # pyright: ignore[reportArgumentType]


async def drive[T](clock: FakeClock, awaitable: Awaitable[T]) -> T:
    task = asyncio.ensure_future(awaitable)
    for _ in range(2000):
        await asyncio.to_thread(threading.Event().wait, 0.001)
        await clock.run_until_idle()
        if task.done():
            return task.result()
        clock.advance(0.5)
    task.cancel()
    raise AssertionError("never finished on the fake clock")


async def settle(clock: FakeClock, rounds: int = 6, step: float = 1.0) -> None:
    for _ in range(rounds):
        await asyncio.to_thread(threading.Event().wait, 0.005)  # the io pool's file work
        await clock.advance_and_settle(step)


async def until(clock: FakeClock, ready: Callable[[], bool], seconds: float = 10.0) -> None:
    """Wait in real time (the fake clock does not move) until ``ready()``: the adapter's file
    writes run on the io pool and end with an fsync, which a loaded disk can hold for a while."""
    for _ in range(int(seconds / 0.005)):
        if ready():
            return
        await asyncio.to_thread(threading.Event().wait, 0.005)
        await clock.run_until_idle()
    raise AssertionError("the adapter's state never settled")


class Rig:
    def __init__(
        self,
        *,
        options: WeixinOptions | None = None,
        state_dir: Path | None = None,
        tokens: ContextTokens | None = None,
        send_only: bool = False,
        listen: bool = True,
        wire: WeixinWire | None = None,
    ) -> None:
        self.clock = (
            wire.clock if wire is not None else FakeClock(datetime(2026, 1, 1, 9, tzinfo=UTC))
        )
        self.wire = wire or WeixinWire(clock=self.clock)
        self.adapter = WeixinAdapter(
            clock=self.clock,
            token=self.wire.token,
            account_id=BOT_ACCOUNT,
            options=options,
            tokens=tokens,
            state_dir=state_dir,
            transport=self.wire.transport(),
            send_only=send_only,
        )
        self.events: list[InboundEvent] = []
        self.fatal: list[tuple[str, str]] = []
        self.adapter.set_fatal_listener(lambda code, text: self.fatal.append((code, text)))
        if listen:
            self.adapter.set_sink(self._sink)

    async def _sink(self, event: InboundEvent) -> None:
        self.events.append(event)

    async def connect(self) -> None:
        await self.adapter.connect()
        await settle(self.clock, 2)

    async def close(self) -> None:
        await self.adapter.disconnect()

    async def send(self, chat_id: str, text: str) -> SendResult:
        """Send while the fake clock moves (sends to one chat are spaced 1.5 s apart)."""
        return await drive(self.clock, self.adapter.send(chat_id, text))

    async def send_image(self, chat_id: str, path: str, **kw: Any) -> SendResult:
        return await drive(self.clock, self.adapter.send_image(chat_id, path, **kw))

    def polls(self) -> list[dict[str, Any]]:
        return [b for _, p, b in self.wire.requests if p.endswith("/getupdates")]


def dm(wire: WeixinWire, user: str, text: str, message_id: str, **extra: Any) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "from_user_id": user,
        "to_user_id": BOT_ACCOUNT,
        "message_type": 1,
        "context_token": wire.token_for(extra.pop("chat", user)),
        "item_list": [{"type": 1, "text_item": {"text": text}}],
        **extra,
    }


# -- the row's Verify ------------------------------------------------------------------------------


def test_a_reply_without_a_stored_context_token_is_refused_clearly() -> None:
    async def main() -> None:
        rig = Rig(listen=False)
        await rig.connect()
        result = await rig.send(ANA, "hello?")
        assert not result.ok and result.error == NO_TOKEN
        assert "message the bot first" in NO_TOKEN
        assert rig.adapter.is_final_failure(result) and not result.retryable
        assert not result.chat_unreachable
        assert not [p for _, p, _ in rig.wire.requests if p.endswith("/sendmessage")]
        # Once the person writes, the reply carries their conversation token.
        rig.adapter.set_sink(rig._sink)  # pyright: ignore[reportPrivateUsage]
        rig.wire.push_message(dm(rig.wire, ANA, "hi bot", "m1"))
        await settle(rig.clock)
        assert [e.text for e in rig.events] == ["hi bot"]
        assert (await rig.send(ANA, "hello Ana")).ok
        assert rig.wire.sent[-1]["context_token"] == rig.wire.token_for(ANA)
        assert rig.wire.sent[-1]["message_type"] == 2 and rig.wire.sent[-1]["message_state"] == 2
        await rig.close()

    run(main())


def test_media_crypto_matches_the_known_answer_vectors() -> None:
    vectors = json.loads(VECTORS.read_text(encoding="utf-8"))
    key = bytes.fromhex(vectors["key_hex"])
    assert wxmedia.parse_key(vectors["key_b64_raw"]) == key
    assert wxmedia.parse_key(vectors["key_b64_hex"]) == key
    assert wxmedia.parse_key(vectors["key_hex"]) == key
    assert wxmedia.key_for_api(key) == vectors["key_b64_hex"]
    for case in vectors["cases"]:
        plain = bytes.fromhex(case["plain_hex"])
        cipher = bytes.fromhex(case["cipher_hex"])
        assert wxmedia.encrypt(plain, key) == cipher, case["name"]
        assert wxmedia.decrypt(cipher, key) == plain, case["name"]
        assert len(cipher) == wxmedia.padded_size(len(plain))
    with pytest.raises(wxmedia.MediaKeyError):
        wxmedia.parse_key("c2hvcnQ=")
    with pytest.raises(wxmedia.MediaKeyError):
        wxmedia.decrypt(b"x" * 15, key)


def test_media_round_trip_through_the_cdn(tmp_path: Path) -> None:
    async def main() -> None:
        rig = Rig(tokens=ContextTokens(seed={ANA: "unused"}), listen=False)
        rig.adapter.tokens = ContextTokens(seed={ANA: rig.wire.token_for(ANA)})
        await rig.connect()
        photo = tmp_path / "board.png"
        photo.write_bytes(b"\x89PNG whiteboard photo")
        sent = await rig.send_image(ANA, str(photo), caption="the board")
        assert sent.ok and len(sent.ids) == 2  # caption first, then the image
        [(to, clear, payload)] = rig.wire.delivered_media
        assert to == ANA and clear == b"\x89PNG whiteboard photo"
        grant = next(iter(rig.wire.uploads.values()))
        assert grant["media_type"] == 1 and grant["rawsize"] == len(clear)
        assert grant["filesize"] == wxmedia.padded_size(len(clear))
        assert len(grant["aeskey"]) == 32  # hex in getuploadurl
        stored = rig.wire.cdn[payload["media"]["encrypt_query_param"]]
        assert b"whiteboard" not in stored and len(stored) == grant["filesize"]
        assert len(wxmedia.parse_key(payload["media"]["aes_key"])) == 16
        # The bot token never goes to the CDN.
        cdn_headers = [h for h in rig.wire.headers if "authorization" not in h]
        assert cdn_headers and all("ilink" not in str(dict(h)) for h in cdn_headers)

        # Inbound: a photo is fetched from the CDN and decrypted for the intake.
        rig.adapter.set_sink(rig._sink)  # pyright: ignore[reportPrivateUsage]
        param = rig.wire.store_media(b"a photo from Ana")
        message = dm(rig.wire, ANA, "", "m-photo")
        message["item_list"] = [
            {
                "type": 2,
                "image_item": {
                    "media": {"encrypt_query_param": param, "aes_key": rig.wire.media_key_b64}
                },
            }
        ]
        rig.wire.push_message(message)
        await settle(rig.clock)
        [event] = rig.events
        assert event.kind is MessageKind.PHOTO and event.media[0].mime == "image/jpeg"
        assert event.media[0].ref.startswith("weixin-media://")
        chunks = [c async for c in rig.adapter.fetch_media(event.media[0])]
        assert b"".join(chunks) == b"a photo from Ana"
        with pytest.raises(Exception, match="no longer known"):
            [c async for c in rig.adapter.fetch_media(Attachment(ref="weixin-media://999"))]
        await rig.close()

    run(main())


def test_inbound_media_urls_outside_the_tencent_cdn_are_refused() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        message = dm(rig.wire, ANA, "", "m-evil")
        message["item_list"] = [
            {
                "type": 4,
                "file_item": {"media": {"full_url": "https://evil.example/x"}, "file_name": "x"},
            },
        ]
        rig.wire.push_message(message)
        await settle(rig.clock)
        assert rig.events == []
        assert wxmedia.check_cdn_url("https://mmbiz.qpic.cn/a.jpg")
        for bad in (
            "http://mmbiz.qpic.cn/a.jpg",
            "https://user:pw@mmbiz.qpic.cn/a",
            "https://qq.com.evil/",
        ):
            with pytest.raises(wxmedia.MediaKeyError):
                wxmedia.check_cdn_url(bad)
        await rig.close()

    run(main())


# -- the poll --------------------------------------------------------------------------------------


def test_the_cursor_is_saved_after_dispatch_and_resumed_after_a_restart(tmp_path: Path) -> None:
    async def main() -> None:
        state = tmp_path / "state"
        rig = Rig(state_dir=state)
        await rig.connect()
        rig.wire.push_message(dm(rig.wire, ANA, "first", "m1"))
        await settle(rig.clock)
        # The context token and then the cursor are written (fsync'd) on the io pool.
        await until(rig.clock, lambda: bool(rig.events) and (state / "cursor.json").exists())
        assert [e.text for e in rig.events] == ["first"]
        saved = json.loads((state / "cursor.json").read_text())
        assert saved["get_updates_buf"]
        assert stat.S_IMODE((state / "cursor.json").stat().st_mode) == 0o600
        assert stat.S_IMODE(state.stat().st_mode) == 0o700
        mtime = (state / "cursor.json").stat().st_mtime_ns
        await settle(rig.clock, 3, step=36.0)  # idle polls echo the same cursor
        assert (state / "cursor.json").stat().st_mtime_ns == mtime
        await rig.close()

        again = Rig(state_dir=state, wire=rig.wire)
        polled = len(again.polls())
        await again.connect()
        await until(again.clock, lambda: len(again.polls()) > polled)
        assert again.polls()[-1]["get_updates_buf"] == saved["get_updates_buf"]
        # The conversation token survived the restart too.
        assert again.adapter.tokens.get(ANA) == rig.wire.token_for(ANA)
        assert stat.S_IMODE((state / "context_tokens.json").stat().st_mode) == 0o600
        await again.close()

    run(main())


def test_duplicates_own_messages_and_empty_messages_are_dropped() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        rig.wire.push_message(dm(rig.wire, ANA, "once", "m1"))
        rig.wire.push_message(dm(rig.wire, ANA, "once again", "m1"))  # same id
        rig.wire.push_message(dm(rig.wire, ANA, "once", "m2"))  # same text, new id
        rig.wire.push_message(dm(rig.wire, BOT_ACCOUNT, "our own echo", "m3"))
        empty = dm(rig.wire, ANA, "", "m4")
        empty["item_list"] = []
        rig.wire.push_message(empty)
        await settle(rig.clock)
        assert [e.text for e in rig.events] == ["once"]
        rig.clock.advance(61.0)
        rig.wire.push_message(dm(rig.wire, ANA, "once", "m5"))  # a minute later: a new message
        await settle(rig.clock)
        assert [e.text for e in rig.events] == ["once", "once"]
        await rig.close()

    run(main())


def test_groups_are_off_by_default_and_need_a_mention_when_on() -> None:
    async def main() -> None:
        off = Rig()
        await off.connect()
        off.wire.push_message(dm(off.wire, ANA, "@IndusBot hi", "g1", room_id=ROOM, chat=ROOM))
        await settle(off.clock)
        assert off.events == []
        await off.close()

        rig = Rig(
            options=WeixinOptions(
                group_policy="allowlist",
                group_allow_from=frozenset({ROOM}),
                mention_names=(BOT_NAME,),
            )
        )
        await rig.connect()
        rig.wire.push_message(
            dm(rig.wire, ANA, f"@{BOT_NAME} what's new?", "g2", room_id=ROOM, chat=ROOM)
        )
        rig.wire.push_message(dm(rig.wire, BEN, "just chatting", "g3", room_id=ROOM, chat=ROOM))
        rig.wire.push_message(
            dm(rig.wire, BEN, "elsewhere", "g4", room_id="999@chatroom", chat="999@chatroom")
        )
        await settle(rig.clock)
        texts = {e.text: e for e in rig.events}
        assert set(texts) == {"what's new?", "just chatting"}
        named = texts["what's new?"]
        assert named.source.chat_type == "group" and named.source.chat_id == ROOM
        assert named.metadata.get("mentioned") is True and named.user_id == ANA
        assert "mentioned" not in texts["just chatting"].metadata  # the core's mention gate decides
        # The group's token was stored for the group chat (replies go to the room).
        assert rig.adapter.tokens.get(ROOM) == rig.wire.token_for(ROOM)
        await rig.close()

    run(main())


def test_voice_quotes_and_bot_messages() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        param = rig.wire.store_media(b"silk audio")
        voice = dm(rig.wire, ANA, "", "v1")
        voice["item_list"] = [
            {
                "type": 3,
                "voice_item": {
                    "media": {"encrypt_query_param": param, "aes_key": rig.wire.media_key_b64},
                    "text": "transcript from Tencent",
                },
            }
        ]
        transcript_only = dm(rig.wire, ANA, "", "v2")
        transcript_only["item_list"] = [{"type": 3, "voice_item": {"text": "only a transcript"}}]
        quote = dm(rig.wire, ANA, "and this?", "q1")
        quote["item_list"][0]["ref_msg"] = {
            "title": "Ben",
            "message_item": {"type": 1, "text_item": {"text": "the earlier point"}},
        }
        bot = dm(rig.wire, "wxid_robot", "beep", "b1", message_type=2)
        for message in (voice, transcript_only, quote, bot):
            rig.wire.push_message(message)
        await settle(rig.clock)
        first, second, third, fourth = rig.events
        assert (
            first.kind is MessageKind.VOICE
            and first.text == ""
            and first.media[0].mime == "audio/silk"
        )
        assert second.kind is MessageKind.TEXT and "only a transcript" in second.text
        assert second.text.startswith("[voice message, Weixin transcript]")
        assert third.reply_to_text == "Ben | the earlier point" and third.text == "and this?"
        assert fourth.source.is_bot
        await rig.close()

    run(main())


def test_poll_errors_back_off_pause_on_an_expired_session_and_stop_on_a_refused_token() -> None:
    async def main() -> None:
        import httpx

        rig = Rig()
        rig.wire.poll_errors = [
            httpx.Response(200, json={"ret": -1, "errmsg": "busy"}),
            httpx.Response(200, json={"ret": -14, "errmsg": "session timeout"}),
        ]
        await rig.connect()
        await settle(rig.clock, 4)  # the -1 retries after 2 s and meets the -14
        after_expiry = len(rig.polls())
        assert after_expiry == 2
        await settle(rig.clock, 3, step=60.0)  # 3 minutes later: still paused
        assert len(rig.polls()) == after_expiry
        await settle(rig.clock, 2, step=600.0)
        assert len(rig.polls()) > after_expiry
        rig.wire.token = "rotated"  # the service no longer knows the token
        await settle(rig.clock, 3, step=36.0)
        assert [code for code, _ in rig.fatal] == ["weixin_auth"]
        assert not rig.adapter.listening
        await rig.close()

    run(main())


# -- sending ---------------------------------------------------------------------------------------


def test_a_frequency_limit_pauses_sends_and_an_unready_session_is_final() -> None:
    async def main() -> None:
        rig = Rig(listen=False)
        rig.adapter.tokens = ContextTokens(seed={ANA: rig.wire.token_for(ANA)})
        await rig.connect()
        rig.wire.ret_queue.append({"ret": -2, "errmsg": "frequency limit"})
        limited = await rig.send(ANA, "one")
        assert limited.kind is ErrorKind.RATE_LIMITED and limited.retry_after == 3.0
        paused = await rig.send(ANA, "two")
        assert paused.kind is ErrorKind.RATE_LIMITED and paused.retry_after is not None
        assert len(rig.wire.sent) == 1  # the pause answered without calling iLink
        rig.clock.advance(31.0)
        assert (await rig.send(ANA, "three")).ok
        rig.wire.ret_queue.append({"ret": -2, "errmsg": "prepare failed"})
        unready = await rig.send(ANA, "four")
        assert unready.kind is not ErrorKind.RATE_LIMITED and rig.adapter.is_final_failure(unready)
        assert "message the bot first" in (unready.error or "")
        await rig.close()

    run(main())


def test_an_expired_session_on_send_drops_the_token() -> None:
    async def main() -> None:
        rig = Rig(listen=False)
        rig.adapter.tokens = ContextTokens(seed={ANA: "ctx-stale"})
        await rig.connect()
        expired = await rig.send(ANA, "hello")
        assert not expired.ok and rig.adapter.is_final_failure(expired)
        assert rig.adapter.tokens.get(ANA) is None
        again = await rig.send(ANA, "hello")
        assert again.error == NO_TOKEN
        assert len(rig.wire.sent) == 1  # never resent without a token
        await rig.close()

    run(main())


def test_long_replies_split_at_2000_characters_and_lines_wrap_for_copying() -> None:
    async def main() -> None:
        rig = Rig(listen=False)
        rig.adapter.tokens = ContextTokens(seed={ANA: rig.wire.token_for(ANA)})
        await rig.connect()
        text = "\n\n\n".join(
            " ".join(f"word{n}" for n in range(p * 100, p * 100 + 100)) for p in range(9)
        )
        result = await rig.send(ANA, text)
        assert result.ok and len(result.ids) >= 3
        sent = [m["item_list"][0]["text_item"]["text"] for m in rig.wire.sent]
        assert all(len(s) <= 2000 for s in sent)
        # Lines wrapped at 120 (a piece's last line may carry the chunker's "(i/n)" mark).
        assert all(len(line) <= 128 for s in sent for line in s.splitlines())
        words = [w for w in " ".join(sent).split() if not re.fullmatch(r"\(\d+/\d+\)", w)]
        assert words == text.split()
        assert "\n\n\n" not in "".join(sent)
        await rig.close()

    run(main())


def test_prepare_text_keeps_code_and_tables() -> None:
    long = "x " * 80
    text = f"intro\n\n\n\n```\n{'y' * 200}\n\n\n```\n| a | b |\n|---|---|\n| {'z' * 130} |\n{long}"
    out = prepare_text(text)
    assert "intro\n\n```" in out
    assert f"```\n{'y' * 200}\n\n\n```" in out
    assert f"| {'z' * 130} |" in out
    assert all(len(line) <= 120 for line in out.splitlines()[-2:])


def test_typing_uses_a_cached_ticket() -> None:
    async def main() -> None:
        rig = Rig(listen=False)
        await rig.connect()
        await rig.adapter.typing_start(ANA)
        await rig.adapter.typing_start(ANA)
        await rig.adapter.typing_stop(ANA)
        configs = [b for _, p, b in rig.wire.requests if p.endswith("/getconfig")]
        assert len(configs) == 2  # the connect probe + one ticket for Ana
        assert rig.wire.typing == [(ANA, 1), (ANA, 1), (ANA, 2)]
        await rig.close()

    run(main())


def test_send_only_never_polls_and_uses_the_stored_tokens(tmp_path: Path) -> None:
    async def main() -> None:
        wire = WeixinWire(clock=FakeClock(datetime(2026, 1, 1, 9, tzinfo=UTC)))
        state = tmp_path / "state"
        state.mkdir()
        (state / "context_tokens.json").write_text(json.dumps({ANA: wire.token_for(ANA)}))
        rig = Rig(state_dir=state, send_only=True, wire=wire)
        await rig.connect()
        await settle(rig.clock)
        assert rig.polls() == [] and not rig.adapter.listening
        assert (await rig.send(ANA, "from cron")).ok
        await rig.close()

    run(main())


# -- secrets and configuration ---------------------------------------------------------------------


def test_tokens_never_reach_logs_errors_or_reprs(caplog: pytest.LogCaptureFixture) -> None:
    async def main() -> Rig:
        rig = Rig(listen=False)
        context = rig.wire.token_for(ANA)
        rig.adapter.tokens = ContextTokens(seed={ANA: context})
        await rig.connect()
        rig.wire.ret_queue.append(
            {"ret": -1, "errmsg": f"bad context {context} for {rig.wire.token}"}
        )
        failed = await rig.send(ANA, "x")
        assert context not in (failed.error or "") and rig.wire.token not in (failed.error or "")
        assert context not in repr(rig.adapter) and context not in repr(rig.adapter.tokens)
        assert rig.wire.token not in repr(rig.adapter)
        await rig.close()
        return rig

    with caplog.at_level(logging.DEBUG):
        rig = run(main())
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert rig.wire.token not in text and rig.wire.token_for(ANA) not in text


def test_create_adapter_reads_the_profile(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 1, 1, 9, tzinfo=UTC))
    home = tmp_path / "home"
    scope = make_scope(
        home,
        env={
            "WEIXIN_TOKEN": fake_token(),
            "WEIXIN_ACCOUNT_ID": "bot/1@im.bot",
            "WEIXIN_GROUP_POLICY": "open",
        },
        clock=clock,
    )
    section = PlatformSection.model_validate(
        {
            "enabled": True,
            "extra": {
                "mention_names": "IndusBot,Indus",
                "cdn_base_url": "https://novac2c.cdn.weixin.qq.com/c2c/",
            },
        }
    )
    settings = PlatformSettings("weixin", True, "credentials", section)
    adapter = create_adapter(AdapterConfig("weixin", settings, scope, clock))
    assert adapter.account_id == "bot/1@im.bot"
    assert adapter.options.group_policy == "open"
    assert adapter.options.mention_names == ("Indus", "IndusBot")
    assert adapter._state_dir == home / "platforms" / "weixin" / "bot_1@im.bot"  # pyright: ignore[reportPrivateUsage]
    assert repr(adapter) == "<WeixinAdapter disconnected>"
    assert fake_token() not in adapter.scrub(f"token {fake_token()}")
    assert not adapter.send_only
    one_shot = create_adapter(AdapterConfig("weixin", settings, scope, clock, send_only=True))
    assert one_shot.send_only  # ``indus send``: no long poll

    class Seat:
        def __init__(self) -> None:
            self.registered: list[str] = []

        def register_fetcher(self, platform: str, fetcher: object) -> Any:
            self.registered.append(platform)
            return lambda: self.registered.remove(platform)

    seat = Seat()
    undo = adapter.register_fetchers(seat)
    assert seat.registered == ["weixin"] and undo is not None
    undo()
    assert seat.registered == []


def test_missing_credentials_fail_for_good() -> None:
    async def main() -> None:
        adapter = WeixinAdapter(
            clock=FakeClock(datetime(2026, 1, 1, tzinfo=UTC)), token="", account_id=""
        )
        with pytest.raises(Exception) as failed:
            await adapter.connect()
        assert getattr(failed.value, "retryable", True) is False
        assert "WEIXIN_TOKEN" in str(failed.value) and "WEIXIN_ACCOUNT_ID" in str(failed.value)

    run(main())


def test_the_fake_serves_every_client_route() -> None:
    assert dict(CLIENT_ROUTES) == ROUTES
    assert set(WeixinAdapter.vendor_names["indus.gateway.adapters.weixin.api:ILinkClient"]) == set(
        CLIENT_ROUTES
    )


def test_a_cdn_base_outside_tencents_https_hosts_is_refused_at_connect() -> None:
    async def main() -> None:
        wire = WeixinWire(clock=FakeClock(datetime(2026, 1, 1, 9, tzinfo=UTC)))
        for base in ("http://novac2c.cdn.weixin.qq.com/c2c", "https://cdn.example.com/c2c"):
            adapter = WeixinAdapter(
                clock=wire.clock,
                token=wire.token,
                account_id=BOT_ACCOUNT,
                cdn_base_url=base,
                transport=wire.transport(),
            )
            with pytest.raises(AdapterConnectError) as raised:
                await adapter.connect()
            assert raised.value.code == "config" and not raised.value.retryable
            assert "CDN base URL" in str(raised.value)
        assert wire.requests == []

    run(main())
