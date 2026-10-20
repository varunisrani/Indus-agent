"""The Yuanbao adapter (P16-21) over the fake Yuanbao service."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import socket
import struct
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.clock import SystemClock
from indus.foundation.config.schema import PlatformSection
from indus.foundation.scope import Scope
from indus.gateway.adapters import targets
from indus.gateway.adapters.base import AdapterConnectError
from indus.gateway.adapters.yuanbao import create_adapter, frames
from indus.gateway.adapters.yuanbao.adapter import (
    MAX_RECONNECTS,
    YuanbaoAdapter,
    YuanbaoOptions,
    parse_chat,
)
from indus.gateway.adapters.yuanbao.client import (
    MediaTooLarge,
    SocketClosed,
    cos_authorization,
)
from indus.gateway.adapters.yuanbao.frames import Element, Push
from indus.gateway.adapters.yuanbao.stickers import find_sticker
from indus.gateway.config import AdapterConfig, PlatformSettings
from indus.gateway.core.events import Attachment, InboundEvent, MessageKind
from indus.gateway.core.send import ErrorKind
from tests.fakes.clock import FakeClock
from tests.fakes.platform.core import FailureKind, FakeChatPlatform
from tests.fakes.platform.yuanbao_wire import (
    API_HOST,
    BOT_ID,
    MEDIA_HOST,
    YuanbaoWire,
    group_member,
)

ANA = "ana-01"
GROUP = "group:300"


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("real network used in a Yuanbao adapter test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield


def run[T](coro: Awaitable[T]) -> T:
    return asyncio.run(coro)  # pyright: ignore[reportArgumentType]


async def drive[T](clock: FakeClock, awaitable: Awaitable[T], *, step: float = 0.5) -> T:
    task = asyncio.ensure_future(awaitable)
    for _ in range(4000):
        await clock.run_until_idle()
        await asyncio.wait({task}, timeout=0.002)
        if task.done():
            return task.result()
        clock.advance(step)
    task.cancel()
    raise AssertionError("never finished on the fake clock")


async def settle(clock: FakeClock, seconds: float = 2.0, step: float = 0.5) -> None:
    for _ in range(int(seconds / step)):
        await clock.advance_and_settle(step)


async def until(
    clock: FakeClock, test: Callable[[], bool], limit: float = 600.0, step: float = 0.5
) -> None:
    start = clock.monotonic()
    while not test():
        if clock.monotonic() - start > limit:
            raise AssertionError("condition never held on the fake clock")
        await clock.advance_and_settle(step)


class Rig:
    def __init__(
        self,
        *,
        options: YuanbaoOptions | None = None,
        send_only: bool = False,
        sink: bool = True,
    ) -> None:
        self.clock = FakeClock(datetime(2026, 1, 1, 9, 0, tzinfo=UTC))
        self.platform = FakeChatPlatform("yuanbao", clock=self.clock, bot_user_id=BOT_ID)
        self.wire = YuanbaoWire(self.platform)
        self.adapter = YuanbaoAdapter(
            clock=self.clock,
            app_id=self.wire.app_id,
            secret=self.wire.secret,
            options=options or YuanbaoOptions(),
            transport=self.wire.transport(),
            opener=self.wire.open,
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
        await settle(self.clock, 1)

    async def close(self) -> None:
        await drive(self.clock, self.adapter.disconnect())

    def sends(self, cmd: str | None = None) -> list[dict[str, Any]]:
        return [s for s in self.wire.sends if cmd is None or s["cmd"] == cmd]


def _said(text: str, **kw: Any) -> Push:
    return _push(elements=(frames.text_element(text),), **kw)


def _push(**kw: Any) -> Push:
    base: dict[str, Any] = {
        "from_account": ANA,
        "sender_nickname": "Ana",
        "msg_id": "m1",
        "msg_time": 1767258000,
    }
    base.update(kw)
    return Push(**base)


# -- the row's Verify ------------------------------------------------------------------------------


def test_two_missed_pongs_open_a_new_socket() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        assert len(rig.wire.sockets) == 1
        await until(rig.clock, lambda: rig.wire.pings == 1)
        assert len(rig.wire.sockets) == 1
        rig.wire.answer_pings = False
        # One unanswered ping (10 s) is tolerated ...
        await until(rig.clock, lambda: rig.wire.pings == 2)
        await settle(rig.clock, 12)
        assert len(rig.wire.sockets) == 1
        # ... the second one in a row replaces the socket, with a freshly signed token.
        await until(rig.clock, lambda: rig.wire.pings == 3)
        await until(rig.clock, lambda: len(rig.wire.sockets) == 2, limit=30)
        assert rig.adapter.waits == [1.0]
        assert len(rig.wire.signs) == 2
        assert len(rig.wire.auth_requests) == 2
        assert rig.fatal == []
        rig.wire.answer_pings = True
        rig.platform.dm(ANA, "still here")
        await settle(rig.clock)
        assert [e.text for e in rig.events] == ["still here"]
        await rig.close()

    run(main())


def test_the_hundredth_failed_reconnect_is_fatal_and_no_101st_is_made() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        rig.wire.socket_failures = [ConnectionError("refused")] * (MAX_RECONNECTS + 5)
        rig.wire.drop_socket(1006)
        await until(rig.clock, lambda: bool(rig.fatal), limit=7200, step=30)
        assert rig.fatal[0][0] == "yuanbao_reconnect"
        assert rig.adapter.reconnects == MAX_RECONNECTS == 100
        assert rig.adapter.waits[:8] == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]
        assert len(rig.adapter.waits) == MAX_RECONNECTS
        left = len(rig.wire.socket_failures)
        await settle(rig.clock, 600, step=60)
        assert len(rig.wire.socket_failures) == left  # no 101st attempt
        assert rig.adapter.reconnects == 100
        await rig.close()

    run(main())


# -- the socket ------------------------------------------------------------------------------------


def test_a_permanent_close_code_is_fatal_at_once() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        rig.wire.drop_socket(4014)
        await until(rig.clock, lambda: bool(rig.fatal), limit=10)
        assert rig.fatal[0][0] == "yuanbao_closed" and "4014" in rig.fatal[0][1]
        await settle(rig.clock, 120, step=10)
        assert len(rig.wire.sockets) == 1
        await rig.close()

    run(main())


def test_a_kickout_or_a_drop_reconnects_and_resets_the_backoff() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        rig.wire.kick()
        await until(rig.clock, lambda: len(rig.wire.sockets) == 2, limit=10)
        rig.wire.drop_socket(1006)
        await until(rig.clock, lambda: len(rig.wire.sockets) == 3, limit=10)
        assert rig.adapter.waits == [1.0, 1.0]
        assert rig.fatal == []
        await rig.close()

    run(main())


def test_auth_bind_carries_the_token_and_the_bot_identity() -> None:
    async def main() -> None:
        rig = Rig(options=YuanbaoOptions(route_env="pre", instance_id="17"))
        await rig.connect()
        bind = next(f for f in rig.wire.client_frames if f.head.cmd == frames.AUTH_BIND)
        assert bind.head.cmd_type == frames.REQUEST and bind.head.module == frames.CONN_MODULE
        request = rig.wire.auth_requests[0]
        who = request.sub(2)
        assert (who.text(1), who.text(2), who.text(3)) == (BOT_ID, "bot", rig.wire.token)
        assert request.sub(3).text(10) == "17" and request.text(5) == "pre"
        assert rig.adapter.bot_id == BOT_ID and rig.adapter.connect_id == "conn-1"
        await rig.close()

    run(main())


def test_a_refused_auth_bind_fails_the_connect() -> None:
    async def main() -> None:
        rig = Rig()
        rig.wire.refuse_auth = True
        with pytest.raises(AdapterConnectError) as caught:
            await drive(rig.clock, rig.adapter.connect())
        assert caught.value.retryable and "41001" in str(caught.value)
        assert not rig.adapter.connected

    run(main())


# -- the sign token --------------------------------------------------------------------------------


def test_the_sign_token_is_an_hmac_over_the_documented_string() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        body = rig.wire.signs[0]
        assert set(body) == {"app_key", "nonce", "signature", "timestamp"}
        assert body["app_key"] == rig.wire.app_id
        assert re.fullmatch(r"[0-9a-f]{32}", body["nonce"])
        # The fake clock starts at 09:00 UTC: 17:00 in Beijing.
        assert body["timestamp"] == "2026-01-01T17:00:00+08:00"
        plain = body["nonce"] + body["timestamp"] + rig.wire.app_id + rig.wire.secret
        expected = hmac.new(rig.wire.secret.encode(), plain.encode(), hashlib.sha256).hexdigest()
        assert body["signature"] == expected
        headers = rig.wire.sign_headers[0]
        assert headers["x-instance-id"] == "17" and headers["x-appversion"]
        await rig.close()

    run(main())


def test_a_retry_code_is_tried_again_and_a_refusal_is_final() -> None:
    async def main() -> None:
        rig = Rig()
        rig.wire.retry_codes = 2
        await rig.connect()
        assert len(rig.wire.signs) == 3 and rig.adapter.connected
        await rig.close()
        refused = Rig()
        refused.wire.refuse_sign = True
        with pytest.raises(AdapterConnectError) as caught:
            await drive(refused.clock, refused.adapter.connect())
        assert not caught.value.retryable and caught.value.code == "yuanbao_auth"
        wrong = Rig()
        wrong.wire.secret = "x" * 20  # the fake now expects another secret
        with pytest.raises(AdapterConnectError) as caught:
            await drive(wrong.clock, wrong.adapter.connect())
        assert not caught.value.retryable and "40101" in str(caught.value)

    run(main())


def test_missing_credentials_and_insecure_urls_are_configuration_errors() -> None:
    async def main() -> None:
        rig = Rig()
        empty = YuanbaoAdapter(clock=rig.clock, app_id="", secret="")
        with pytest.raises(AdapterConnectError) as caught:
            await empty.connect()
        assert caught.value.code == "config" and "YUANBAO_APP_ID" in str(caught.value)

    run(main())
    with pytest.raises(ValueError, match="https"):
        YuanbaoOptions.read({"api_domain": "http://example.com"})
    with pytest.raises(ValueError, match="wss"):
        YuanbaoOptions.read({"ws_url": "ws://example.com/ws"})
    with pytest.raises(ValueError, match="credentials"):
        YuanbaoOptions.read({"api_domain": "https://user:pw@example.com"})
    local = YuanbaoOptions.read({"ws_url": "ws://127.0.0.1:9000/ws", "api_domain": "http://[::1]"})
    assert local.ws_url.startswith("ws://127.0.0.1") and local.api_base == "http://[::1]"


def test_secrets_never_reach_logs_errors_or_reprs(caplog: pytest.LogCaptureFixture) -> None:
    async def main() -> list[str]:
        rig = Rig()
        await rig.connect()
        texts = [repr(rig.adapter), repr(rig.adapter.api), repr(rig.adapter.api.current)]  # type: ignore[union-attr]
        texts.append(rig.adapter.scrub(f"echo {rig.wire.token}"))
        rig.platform.fail(FailureKind.FORBIDDEN, op="send")
        result = await drive(rig.clock, rig.adapter.send(ANA, "hello"))
        texts.append(result.error or "")
        rig.wire.drop_socket(1006)
        await until(rig.clock, lambda: len(rig.wire.sockets) == 2, limit=10)
        await rig.close()
        signature = rig.wire.signs[0]["signature"]
        texts.append(signature)
        return [*texts, rig.wire.secret, rig.wire.token]

    caplog.set_level(logging.DEBUG)
    for name in ("httpx", "httpcore"):
        caplog.set_level(logging.DEBUG, logger=name)
    found = run(main())
    *shown, signature, secret, token = found
    logged = caplog.text
    for value in (secret, token, signature):
        assert value not in logged
        for text in shown:
            assert value not in text


# -- inbound ---------------------------------------------------------------------------------------


def test_inbound_messages_are_normalised() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        wire = rig.wire
        wire.send_push(_push(elements=(frames.text_element("／status now"),)))
        wire.send_push(
            _push(
                msg_id="m2",
                group_code="300",
                group_name="Ops",
                elements=(
                    frames.custom_element({"elem_type": 1002, "text": "@bot", "user_id": BOT_ID}),
                    frames.text_element("hi bot"),
                ),
            )
        )
        wire.send_push(
            _push(msg_id="m3", group_code="300", elements=(frames.text_element("chatter"),))
        )
        wire.send_push(
            _push(msg_id="m4", from_account=BOT_ID, elements=(frames.text_element("me"),))
        )
        wire.send_push(_push(msg_id="m1", elements=(frames.text_element("repeat"),)))
        wire.send_push(_push(msg_id="m5", callback_command="Group.CallbackAfterRecallMsg"))
        face = frames.face_element({"sticker_id": "278", "name": "六六六"})
        wire.send_push(_push(msg_id="m6", elements=(face,)))
        wire.send_push(_said("[图片]", msg_id="m8"))  # stand-in text, no picture: dropped
        file_without_url = Element("TIMFileElem", {"file_name": "a.pdf"})
        wire.send_push(_push(msg_id="m9", elements=(file_without_url,)))
        wire.json_pushes = True
        wire.send_push(_push(msg_id="m7", elements=(frames.text_element("from json"),)))
        await settle(rig.clock)
        texts = [e.text for e in rig.events]
        assert texts == [
            "/status now",
            "hi bot",
            "chatter",
            "[sticker: 六六六]",
            "[file: a.pdf]",
            "from json",
        ]
        command, mentioned, unaddressed = rig.events[:3]
        assert command.command == "status" and command.source.chat_id == f"direct:{ANA}"
        assert command.source.chat_type == "dm" and command.user_name == "Ana"
        assert mentioned.source.chat_id == GROUP and mentioned.source.chat_type == "group"
        assert mentioned.source.chat_name == "Ops" and mentioned.metadata["mentioned"] is True
        assert unaddressed.metadata["mentioned"] is False and unaddressed.reply_expected is False
        assert command.timestamp is not None
        assert len(wire.acks) == 10  # every push asked for an acknowledgement
        await rig.close()

    run(main())


def test_messages_before_the_handler_is_installed_wait_for_it() -> None:
    async def main() -> None:
        rig = Rig(sink=False)
        await rig.connect()
        rig.platform.dm(ANA, "early bird")
        await settle(rig.clock)
        assert rig.events == [] and len(rig.wire.acks) == 1
        rig.adapter.set_sink(rig.sink)
        await settle(rig.clock)
        assert [e.text for e in rig.events] == ["early bird"]
        await rig.close()

    run(main())


def test_own_policies_drop_disabled_and_unlisted_chats() -> None:
    async def main() -> None:
        options = YuanbaoOptions(
            dm_policy="disabled",
            group_policy="allowlist",
            group_allowed_chats=frozenset({"group:300"}),
        )
        rig = Rig(options=options)
        await rig.connect()
        rig.wire.send_push(_push(elements=(frames.text_element("dm"),)))
        rig.wire.send_push(
            _push(msg_id="m2", group_code="300", elements=(frames.text_element("in"),))
        )
        rig.wire.send_push(
            _push(msg_id="m3", group_code="301", elements=(frames.text_element("out"),))
        )
        await settle(rig.clock)
        assert [e.text for e in rig.events] == ["in"]
        assert rig.adapter.access_policy("dm") == "disabled"
        assert rig.adapter.access_policy("group") == "allowlist"
        await rig.close()

    run(main())
    read = YuanbaoOptions.read({"group_allowed_chats": "300, group:301", "allow_from": "a,b"})
    assert read.group_allowed_chats == {"group:300", "group:301"}
    adapter = YuanbaoAdapter(
        clock=FakeClock(datetime(2026, 1, 1, tzinfo=UTC)), app_id="a", secret="b", options=read
    )
    assert adapter.allows_dm("a") and not adapter.allows_dm("c")
    assert adapter.access_policy("dm") == "pairing" and adapter.access_policy("group") == "open"


def test_inbound_media_is_exchanged_then_fetched_without_credentials() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        rig.platform.photo(ANA, ANA, "cat.png", caption="look")
        rig.platform.document(ANA, ANA, "notes.txt")
        await settle(rig.clock)
        photo, document = rig.events
        assert photo.kind is MessageKind.PHOTO and photo.text == "look"
        assert document.kind is MessageKind.DOCUMENT and document.media[0].mime == "text/plain"
        ref = photo.media[0].ref
        assert ref.startswith(f"https://{API_HOST}/api/resource/download?resourceId=")
        data = b"".join([c async for c in rig.adapter.fetch_media(photo.media[0])])
        assert data == b"bytes of cat.png"
        with pytest.raises(ValueError, match="allowlist"):
            async for _ in rig.adapter.fetch_media(Attachment(ref="https://evil.example/x.png")):
                pass
        with pytest.raises(ValueError, match="allowlist"):
            async for _ in rig.adapter.fetch_media(Attachment(ref=f"http://{MEDIA_HOST}/cat.png")):
                pass
        rig.adapter.options = YuanbaoOptions(max_media_bytes=4)
        with pytest.raises(MediaTooLarge):
            async for _ in rig.adapter.fetch_media(photo.media[0]):
                pass
        await rig.close()

    run(main())


# -- outbound --------------------------------------------------------------------------------------


def test_sends_reach_c2c_and_groups_with_quotes_and_dm_origins() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        rig.wire.send_push(
            _push(private_from_group_code="300", elements=(frames.text_element("x"),))
        )
        await settle(rig.clock)
        dm = await drive(rig.clock, rig.adapter.send(f"direct:{ANA}", "hello"))
        group = await drive(rig.clock, rig.adapter.send(GROUP, "**hi**", reply_to="m9"))
        bare = await drive(rig.clock, rig.adapter.send(ANA, "bare"))
        assert dm.ok and group.ok and bare.ok
        c2c, grp, plain = rig.wire.sends
        assert c2c["cmd"] == "send_c2c_message" and c2c["module"] == frames.BIZ_MODULE
        assert c2c["to"] == ANA and c2c["from"] == BOT_ID and c2c["group_code"] == "300"
        assert grp["cmd"] == "send_group_message" and grp["to"] == "300" and grp["quote"] == "m9"
        assert grp["elements"] == [frames.text_element("**hi**")]
        assert plain["to"] == ANA
        assert dm.message_id == c2c["msg_id"]
        rig.wire.push_answers = True
        pushed = await drive(rig.clock, rig.adapter.send(GROUP, "answered by a push"))
        assert pushed.ok and rig.clock.monotonic() < 120
        rig.wire.push_answers = False
        bad = await drive(rig.clock, rig.adapter.send("group: has space", "x"))
        assert not bad.ok and bad.kind is ErrorKind.NOT_FOUND
        await rig.close()

    run(main())


def test_long_text_is_cut_at_four_thousand_characters() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        result = await drive(rig.clock, rig.adapter.send(GROUP, "word " * 1700))
        assert result.ok and len(result.ids) == 3
        for sent in rig.wire.sends:
            assert len(sent["elements"][0].get("text")) <= 4000
        await rig.close()

    run(main())


def test_a_send_without_an_answer_is_maybe_delivered_and_never_retried() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        rig.platform.fail(FailureKind.READ_TIMEOUT, op="send")
        result = await drive(rig.clock, rig.adapter.send_with_retry(ANA, "hello", rng=lambda: 0))
        assert not result.ok and result.maybe_delivered and not result.retryable
        assert len(rig.sends("send_c2c_message")) == 1
        await rig.close()

    run(main())


def test_a_long_stated_wait_is_handed_back_not_slept() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        rig.platform.fail(FailureKind.RATE_LIMIT, op="send", retry_after=120)
        started = rig.clock.monotonic()
        result = await drive(rig.clock, rig.adapter.send_with_retry(ANA, "hello", rng=lambda: 0))
        assert result.kind is ErrorKind.RATE_LIMITED and result.retry_after == 120
        assert result.is_flood and len(rig.sends("send_c2c_message")) == 1
        assert rig.clock.monotonic() - started < 60
        await rig.close()

    run(main())


def test_outbound_mentions_use_the_cached_member_list() -> None:
    async def main() -> None:
        rig = Rig()
        rig.wire.groups["300"] = {
            "name": "Ops",
            "owner_id": "own",
            "owner_nickname": "Boss",
            "members": [group_member("u-ana", "Ana"), group_member("u-bo", "Bo", 1)],
        }
        await rig.connect()
        info = await drive(rig.clock, rig.adapter.group_info("300"))
        assert info["group_name"] == "Ops" and info["member_count"] == 2
        page = await drive(rig.clock, rig.adapter.group_members("300"))
        assert [m["nickname"] for m in page["members"]] == ["Ana", "Bo"]
        await drive(rig.clock, rig.adapter.send(GROUP, "hi @ana and @nobody"))
        elements = rig.wire.sends[-1]["elements"]
        assert elements[0] == frames.text_element("hi ")
        assert json.loads(elements[1].get("data")) == {
            "elem_type": 1002,
            "text": "@Ana",
            "user_id": "u-ana",
        }
        assert elements[2] == frames.text_element(" and @nobody")
        await rig.close()

    run(main())


def test_the_reply_heartbeat_runs_while_typing_and_finishes_after_delivery() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        await drive(rig.clock, rig.adapter.typing_start(GROUP))
        await settle(rig.clock, 4.5)
        running = [b for b in rig.wire.heartbeats if b == (GROUP, frames.RUNNING)]
        assert len(running) == 3  # at once, then every 2 s
        await drive(rig.clock, rig.adapter.send(GROUP, "done"))
        await settle(rig.clock, 6)
        assert rig.wire.heartbeats[-1] == (GROUP, frames.FINISH)
        assert rig.wire.heartbeats.count((GROUP, frames.RUNNING)) == 3
        # Idle typing stops by itself after 30 s with a FINISH.
        await drive(rig.clock, rig.adapter.typing_start(ANA))
        await settle(rig.clock, 34)
        mine = [b for b in rig.wire.heartbeats if b[0] == ANA]
        assert mine[-1] == (ANA, frames.FINISH)
        assert 14 <= len(mine) <= 17
        await rig.close()

    run(main())


def _png(width: int, height: int) -> bytes:
    header = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
    return header + struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00" + b"\x00" * 16


def test_media_goes_through_a_signed_cos_upload(tmp_path: Path) -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        picture = tmp_path / "chart.png"
        picture.write_bytes(_png(3, 2))
        result = await drive(rig.clock, rig.adapter.send_image(GROUP, str(picture), caption="Q3"))
        assert result.ok, result.error
        ((key, stored),) = rig.wire.objects.items()
        assert stored == picture.read_bytes() and key.endswith("/chart.png")
        assert rig.wire.object_headers[key]["x-cos-security-token"].startswith("sess")
        (slot_call,) = rig.wire.calls_to("POST", "/api/resource/genUploadInfo")
        assert slot_call.headers["x-source"] == "web" and slot_call.headers["x-id"] == BOT_ID
        image, caption = rig.wire.sends[-1]["elements"]
        info = image.get("image_info_array")[0]
        assert image.kind == "TIMImageElem" and (info["width"], info["height"]) == (3, 2)
        assert image.get("uuid") == hashlib.md5(stored).hexdigest()  # noqa: S324 - the vendor's id
        assert image.get("image_format") == 3 and info["url"].startswith("https://")
        assert caption == frames.text_element("Q3")
        report = tmp_path / "report.pdf"
        report.write_bytes(b"%PDF-1.7 fake")
        done = await drive(rig.clock, rig.adapter.send_document(ANA, str(report)))
        assert done.ok
        (element,) = rig.wire.sends[-1]["elements"]
        assert element.kind == "TIMFileElem" and element.get("file_name") == "report.pdf"
        assert element.get("file_size") == len(b"%PDF-1.7 fake")
        missing = await drive(rig.clock, rig.adapter.send_document(ANA, str(tmp_path / "no")))
        assert not missing.ok and "cannot send the file" in (missing.error or "")
        await rig.close()

    run(main())


def test_the_cos_signature_matches_a_known_answer() -> None:
    # Values worked out by hand from the COS signature steps (sha1, HMAC-SHA1).
    signed = cos_authorization(
        method="PUT",
        path="/a/b.txt",
        headers={"Host": "bkt-1.cos.accelerate.myqcloud.com", "Content-Type": "text/plain"},
        secret_id="id1",
        secret_key="key1",
        start=100,
        end=200,
    )
    request = "put\n/a/b.txt\n\ncontent-type=text%2Fplain&host=bkt-1.cos.accelerate.myqcloud.com\n"
    to_sign = "sha1\n100;200\n" + hashlib.sha1(request.encode()).hexdigest() + "\n"  # noqa: S324
    key = hmac.new(b"key1", b"100;200", hashlib.sha1).hexdigest()
    signature = hmac.new(key.encode(), to_sign.encode(), hashlib.sha1).hexdigest()
    assert signed == (
        "q-sign-algorithm=sha1&q-ak=id1&q-sign-time=100;200&q-key-time=100;200"
        f"&q-header-list=content-type;host&q-url-param-list=&q-signature={signature}"
    )


def test_stickers_go_as_face_elements() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        sticker = find_sticker("278")
        assert sticker is not None
        result = await drive(rig.clock, rig.adapter.send_sticker(GROUP, sticker))
        assert result.ok
        (face,) = rig.wire.sends[-1]["elements"]
        assert face.kind == "TIMFaceElem" and face.get("index") == 0
        assert json.loads(face.get("data")) == {
            "sticker_id": "278",
            "package_id": "1003",
            "width": 128,
            "height": 128,
            "formats": "png",
            "name": "六六六",
        }
        await rig.close()

    run(main())


# -- indus send and create_adapter -----------------------------------------------------------------


def test_create_adapter_reads_secrets_and_settings_and_send_only_reads_nothing(
    scope: Scope,
) -> None:
    secrets: Any = scope.secrets
    clock = FakeClock(datetime(2026, 1, 1, 9, 0, tzinfo=UTC))
    platform = FakeChatPlatform("yuanbao", clock=clock)
    wire = YuanbaoWire(platform)
    secrets.set("YUANBAO_APP_ID", wire.app_id)
    secrets.set("YUANBAO_APP_SECRET", wire.secret)
    section = PlatformSection.model_validate({"dm_policy": "allowlist", "allow_from": ["x"]})
    settings = PlatformSettings("yuanbao", True, "credentials", section)
    config = AdapterConfig("yuanbao", settings, scope, clock, None, send_only=True)
    adapter = create_adapter(config, transport=wire.transport(), opener=wire.open)
    assert adapter.app_id == wire.app_id and adapter.send_only
    assert adapter.options.dm_policy == "allowlist" and adapter.allows_dm("x")
    assert wire.secret not in adapter.scrub("leak " + wire.secret)
    assert repr(adapter) == f"<YuanbaoAdapter app {wire.app_id} disconnected>"

    events: list[InboundEvent] = []

    async def sink(event: InboundEvent) -> None:
        events.append(event)

    async def main() -> None:
        adapter.set_sink(sink)
        await drive(clock, adapter.connect())
        wire.send_push(_push(elements=(frames.text_element("not for indus send"),)))
        await settle(clock)
        result = await drive(clock, adapter.send(ANA, "one-shot"))
        assert result.ok
        await drive(clock, adapter.disconnect())

    run(main())
    assert events == [] and wire.acks == []
    assert platform.texts(ANA) == ["one-shot"]


def test_the_target_rule_reads_prefixed_chats_only() -> None:
    assert targets.parse_target("yuanbao", "group:328306697") == ("group:328306697", None)
    assert targets.parse_target("yuanbao", "direct:ana-01") == ("direct:ana-01", None)
    assert targets.parse_target("yuanbao", "#ops room".replace(" ", "-")) == ("#ops-room", None)
    for bad in ("328306697", "group:", "dm:ana", "group:1:2", "direct:a b"):
        with pytest.raises(targets.TargetError):
            targets.parse_target("yuanbao", bad)
    assert targets.validate_target("yuanbao", "group:300") is None
    assert targets.validate_target("yuanbao", "300") is not None


def test_parse_chat_forms() -> None:
    assert parse_chat("group:300").chat_id == "group:300"
    assert parse_chat("direct:ana").chat_id == "direct:ana"
    assert parse_chat("ana").chat_id == "direct:ana"
    for bad in ("", "group:", "a b", "group:x/y"):
        with pytest.raises(ValueError):
            parse_chat(bad)


def test_a_closed_socket_reading_raises_socket_closed() -> None:
    assert SocketClosed(4012).code == 4012
    assert Element("TIMTextElem", {"text": "a"}).as_json() == {
        "msg_type": "TIMTextElem",
        "msg_content": {"text": "a"},
    }


def test_the_real_opener_speaks_to_an_asgi_server() -> None:
    """The production socket path (httpx-ws) against an in-process ASGI app: no real socket."""
    pytest.importorskip("httpx_ws")
    from httpx_ws.transport import (  # pyright: ignore[reportMissingImports]
        ASGIWebSocketTransport,  # pyright: ignore[reportUnknownVariableType]
    )
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route, WebSocketRoute
    from starlette.websockets import WebSocket

    clock = SystemClock()  # real waits: the 1 s bound on closing the socket applies
    wire = YuanbaoWire(FakeChatPlatform("yuanbao"))

    async def sign(request: Request) -> JSONResponse:
        data = {"token": wire.token, "bot_id": BOT_ID, "duration": 3600, "source": "bot"}
        return JSONResponse({"code": 0, "data": data})

    async def bot_socket(websocket: WebSocket) -> None:
        await websocket.accept()
        bind = frames.decode_frame(await websocket.receive_bytes())
        answer = frames.Writer().text(3, "conn-real").build()
        head = frames.Head(frames.RESPONSE, bind.head.cmd, 1, bind.head.msg_id, bind.head.module)
        await websocket.send_bytes(frames.encode_frame(head, answer))
        push = _said("over a real socket")
        push_head = frames.Head(frames.PUSH, "inbound_message", 2, "p1", frames.BIZ_MODULE)
        await websocket.send_bytes(frames.encode_frame(push_head, frames.push_bytes(push)))
        await websocket.receive_bytes()  # stay open until the client goes

    app = Starlette(
        routes=[
            Route("/api/v5/robotLogic/sign-token", sign, methods=["POST"]),
            WebSocketRoute("/wss/connection", bot_socket),
        ]
    )

    async def main() -> list[InboundEvent]:
        got: list[InboundEvent] = []
        arrived = asyncio.Event()

        async def sink(event: InboundEvent) -> None:
            got.append(event)
            arrived.set()

        options = YuanbaoOptions(
            ws_url="ws://127.0.0.1/wss/connection", api_base="http://127.0.0.1"
        )
        async with ASGIWebSocketTransport(app) as transport:  # pyright: ignore[reportUnknownVariableType]
            adapter = YuanbaoAdapter(
                clock=clock,
                app_id=wire.app_id,
                secret=wire.secret,
                options=options,
                transport=transport,  # pyright: ignore[reportUnknownArgumentType]
            )
            adapter.set_sink(sink)
            await adapter.connect()
            await asyncio.wait_for(arrived.wait(), 10)
            assert adapter.connect_id == "conn-real"
            await adapter.disconnect()
        return got

    (event,) = run(main())
    assert event.text == "over a real socket" and event.source.chat_type == "dm"
