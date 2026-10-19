"""The scriptable fake platform and its wire fakes behave as the adapter tests assume (P15-01)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from indus.gateway.core.send import ErrorKind, SendResult
from tests.fakes.clock import FakeClock
from tests.fakes.platform import FakePlatform
from tests.fakes.platform.core import (
    Button,
    FailureKind,
    FakeChatNotFound,
    FakeChatPlatform,
    FakeMessageNotFound,
    FakePlatformError,
    FakeRateLimited,
    FakeTooLong,
    make_error,
)
from tests.fakes.platform.discord_wire import DiscordWire, HTTPException, NotFound, View
from tests.fakes.platform.slack_wire import SlackApiError, SlackWire
from tests.fakes.platform.telegram_wire import (
    BadRequest,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    RetryAfter,
    TelegramWire,
    TimedOut,
)


def _clock() -> FakeClock:
    return FakeClock(datetime(2026, 1, 1, tzinfo=UTC))


@pytest.mark.parametrize("kind", list(FailureKind))
def test_every_failure_reads_as_its_kind(kind: FailureKind) -> None:
    error = make_error(kind, retry_after=9)
    assert error.kind is kind
    result = SendResult.failure(exc=error, retry_after=error.retry_after)
    expected = {
        FailureKind.CONNECT_TIMEOUT: ErrorKind.TRANSIENT,
        FailureKind.READ_TIMEOUT: ErrorKind.UNKNOWN,
        FailureKind.RATE_LIMIT: ErrorKind.RATE_LIMITED,
        FailureKind.CHAT_NOT_FOUND: ErrorKind.NOT_FOUND,
        FailureKind.THREAD_NOT_FOUND: ErrorKind.NOT_FOUND,
        FailureKind.FORBIDDEN: ErrorKind.FORBIDDEN,
        FailureKind.PARSE_ERROR: ErrorKind.BAD_FORMAT,
        FailureKind.TOO_LONG: ErrorKind.TOO_LONG,
    }[kind]
    assert result.kind is expected
    assert result.maybe_delivered is (kind is FailureKind.READ_TIMEOUT)
    assert result.chat_level is (kind is FailureKind.CHAT_NOT_FOUND)


def test_outbound_log_ids_and_times() -> None:
    async def main() -> FakeChatPlatform:
        clock = _clock()
        platform = FakeChatPlatform(clock=clock)
        await platform.connect()
        first = await platform.post("c", "one", buttons=(Button("Yes", "apv:r:once"),))
        clock.advance(2.5)
        second = await platform.post("c", "two", reply_to=first.message_id, thread_id="t")
        await platform.edit("c", first.message_id, "one!")
        await platform.typing("c")
        await platform.react("c", second.message_id, "eyes")
        assert await platform.delete("c", second.message_id)
        return platform

    platform = asyncio.run(main())
    kinds = [op.kind for op in platform.ops]
    assert kinds == ["connect", "send", "send", "edit", "typing", "reaction", "delete"]
    assert platform.ops[2].at == 2.5 and platform.ops[2].thread_id == "t"
    assert platform.texts("c") == ["one!"]
    message = platform.message("1")
    assert message is not None and message.buttons == (Button("Yes", "apv:r:once"),)
    assert message.edits == 1


def test_failures_are_scoped_counted_and_delayed() -> None:
    async def main() -> FakeChatPlatform:
        platform = FakeChatPlatform()
        platform.fail(
            FailureKind.RATE_LIMIT, op="send", chat_id="a", times=2, after=1, retry_after=3
        )
        await platform.post("a", "ok first")
        await platform.post("b", "other chat unaffected")
        for _ in range(2):
            with pytest.raises(FakeRateLimited) as info:
                await platform.post("a", "limited")
            assert info.value.retry_after == 3
        await platform.post("a", "ok again")
        platform.fail(FailureKind.FORBIDDEN)  # any op, not connect
        await platform.connect()
        with pytest.raises(FakePlatformError):
            await platform.typing("z")
        return platform

    platform = asyncio.run(main())
    assert platform.texts("a") == ["ok first", "ok again"]


def test_length_cap_edit_of_missing_message_and_removed_chat() -> None:
    async def main() -> None:
        platform = FakeChatPlatform(max_len=5)
        with pytest.raises(FakeTooLong):
            await platform.post("c", "too long")
        with pytest.raises(FakeMessageNotFound):
            await platform.edit("c", "99", "x")
        sent = await platform.post("c", "hi")
        with pytest.raises(FakeMessageNotFound):
            await platform.edit("other", sent.message_id, "x")
        platform.remove_chat("c")
        with pytest.raises(FakeChatNotFound):
            await platform.post("c", "hi")
        with pytest.raises(FakeChatNotFound):
            await platform.chat("c")

    asyncio.run(main())


def test_inbound_builders_queue_before_and_after_the_loop_starts() -> None:
    platform = FakeChatPlatform()
    dm = platform.dm("u1", "hi", user_name="Ana")
    platform.group("g", "u2", "/help", mention=True)

    async def main() -> list[str]:
        seen = [(await platform.receive_inbound()).text, (await platform.receive_inbound()).text]
        platform.thread("g", "t9", "u3", "threaded")
        platform.bot_message("g", "b1", "beep")
        platform.edited(dm, "hi (edited)")
        platform.voice("u1", "u1")
        platform.button_press("u1", "u1", "apv:r:deny", message_id="1")
        rest = platform.drain_inbound()
        assert platform.next_inbound() is None
        assert [i.event for i in rest] == ["message", "message", "edited", "message", "button"]
        assert rest[0].thread_id == "t9" and rest[0].chat_kind == "thread"
        assert rest[1].is_bot and rest[2].message_id == dm.message_id
        assert rest[3].media[0].kind == "voice" and rest[4].button_data == "apv:r:deny"
        updates = [dm.update_id, *(i.update_id for i in rest)]
        assert updates == sorted(updates) and len(set(updates)) == len(updates)
        return seen

    assert asyncio.run(main()) == ["hi", "/help"]
    assert dm.chat_kind == "dm" and dm.chat_id == "u1"


def test_p00_recorder_is_still_exported() -> None:
    platform = FakePlatform()
    assert asyncio.run(platform.send("c", "x")) == "m1"


# -- telegram -----------------------------------------------------------------------------------


def test_telegram_bot_sends_edits_and_translates_errors() -> None:
    async def main() -> TelegramWire:
        wire = TelegramWire()
        bot = wire.bot
        me = await bot.get_me()
        assert me.is_bot and wire.platform.connected
        markup = InlineKeyboardMarkup(((InlineKeyboardButton("Allow", "apv:r:once"),),))
        sent = await bot.send_message(100, "*hi*", parse_mode="MarkdownV2", reply_markup=markup)
        await bot.edit_message_text("done", chat_id=100, message_id=sent.message_id)
        await bot.send_chat_action(100, "typing", message_thread_id=7)
        wire.platform.fail(FailureKind.RATE_LIMIT, retry_after=97 * 60)
        with pytest.raises(RetryAfter) as flood:
            await bot.send_message(100, "again")
        assert flood.value.retry_after == 97 * 60
        wire.platform.fail(FailureKind.READ_TIMEOUT)
        with pytest.raises(TimedOut):
            await bot.send_message(100, "slow")
        wire.platform.fail(FailureKind.CHAT_NOT_FOUND)
        with pytest.raises(BadRequest, match="chat not found"):
            await bot.send_message(100, "gone")
        assert SendResult.failure(exc=flood.value).kind is ErrorKind.RATE_LIMITED
        return wire

    wire = asyncio.run(main())
    assert wire.platform.texts("100") == ["done"]
    message = wire.platform.message("1")
    assert message is not None and message.buttons[0].data == "apv:r:once"
    assert wire.platform.ops_of("typing")[0].thread_id == "7"


def test_telegram_utf16_cap() -> None:
    async def main() -> None:
        wire = TelegramWire()
        await wire.bot.send_message(1, "😀" * 2048)  # 4,096 UTF-16 units
        with pytest.raises(BadRequest, match="too long"):
            await wire.bot.send_message(1, "😀" * 2048 + "x")

    asyncio.run(main())


def test_telegram_updates_shape_and_reply_to_own_message_has_no_text() -> None:
    async def main() -> None:
        wire = TelegramWire()
        ours = await wire.bot.send_message(100, "rich answer")
        wire.platform.dm("100", "why?", reply_to=str(ours.message_id))
        wire.platform.thread("-1001", "55", "100", "topic text")
        wire.platform.photo("100", "100", caption="look")
        wire.platform.button_press("100", "100", "apv:r:once", message_id=str(ours.message_id))
        updates = await wire.bot.get_updates(offset=0, timeout=30)
        reply, topic, photo, press = updates
        assert reply.message is not None and reply.message.chat.type == "private"
        assert reply.message.reply_to_message is not None
        assert reply.message.reply_to_message.text is None
        assert topic.message is not None and topic.message.message_thread_id == 55
        assert topic.message.chat.is_forum and topic.message.chat.type == "supergroup"
        assert photo.message is not None and photo.message.caption == "look"
        assert photo.message.photo and photo.message.text is None
        assert press.callback_query is not None and press.callback_query.data == "apv:r:once"
        assert await wire.bot.get_updates() == []

    asyncio.run(main())


# -- discord ------------------------------------------------------------------------------------


def test_discord_channel_send_edit_typing_and_errors() -> None:
    async def main() -> DiscordWire:
        wire = DiscordWire()
        await wire.client.start("token-not-used")
        channel = wire.client.get_channel(300)
        assert channel is not None
        view = View()
        view.add_item(Button("Allow", "apv:r:once"))
        message = await channel.send("hello", view=view)
        async with channel.typing():
            await message.edit(content="hello!")
        await message.add_reaction("👀")
        thread = await message.create_thread("Indus", auto_archive_duration=1440)
        assert thread.kind == "thread" and thread.parent_id == 300
        wire.platform.fail(FailureKind.CHAT_NOT_FOUND)
        with pytest.raises(NotFound) as gone:
            await channel.send("x")
        assert SendResult.failure(exc=gone.value).chat_level
        wire.platform.fail(FailureKind.RATE_LIMIT)
        with pytest.raises(HTTPException) as limited:
            await channel.send("x")
        assert limited.value.status == 429
        with pytest.raises(HTTPException, match="2000 or fewer"):
            await channel.send("x" * 2001)
        return wire

    wire = asyncio.run(main())
    assert wire.platform.texts("300") == ["hello!"]
    assert wire.threads == [(1, "Indus", 1440)]


def test_discord_dispatches_messages_and_interactions() -> None:
    async def main() -> list[str]:
        wire = DiscordWire()
        seen: list[str] = []

        @wire.client.event
        async def on_message(message: object) -> None:
            seen.append(f"msg:{getattr(message, 'content', '')}")

        @wire.client.event
        async def on_interaction(interaction: object) -> None:
            seen.append(f"press:{getattr(interaction, 'data', {}).get('custom_id')}")

        wire.platform.group("300", "200", "hi bot", mention=True)
        wire.platform.thread("300", "400", "200", "in thread")
        wire.platform.button_press("300", "200", "apv:r:deny")
        assert await wire.dispatch_pending() == 3
        thread = wire.client.get_channel(400)
        assert thread is not None and thread.kind == "thread" and thread.parent_id == 300
        return seen

    assert asyncio.run(main()) == ["msg:hi bot", "msg:in thread", "press:apv:r:deny"]


# -- slack --------------------------------------------------------------------------------------


def test_slack_client_posts_updates_and_raises_api_errors() -> None:
    async def main() -> SlackWire:
        wire = SlackWire()
        handler = wire.handler("app-token-not-used")
        await handler.connect_async()
        auth = await wire.client.auth_test()
        assert auth["ok"] and auth["team_id"] == "T1"
        blocks = [{"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "Allow"},
             "action_id": "apv:r:once"},
        ]}]  # fmt: skip
        posted = await wire.client.chat_postMessage(channel="C1", text="hi", blocks=blocks)
        await wire.client.chat_update(channel="C1", ts=posted["ts"], text="hi!")
        await wire.client.assistant_threads_setStatus(
            channel_id="C1", thread_ts=posted["ts"], status="is thinking"
        )
        wire.platform.fail(FailureKind.RATE_LIMIT, retry_after=30)
        with pytest.raises(SlackApiError) as limited:
            await wire.client.chat_postMessage(channel="C1", text="again")
        assert limited.value.response["error"] == "ratelimited"
        assert limited.value.response.headers["Retry-After"] == "30"
        assert SendResult.failure(exc=limited.value).kind is ErrorKind.RATE_LIMITED
        wire.platform.fail(FailureKind.FORBIDDEN)
        with pytest.raises(SlackApiError) as forbidden:
            await wire.client.chat_postMessage(channel="C1", text="x")
        assert SendResult.failure(exc=forbidden.value).chat_unreachable
        await wire.client.chat_postEphemeral(channel="C1", user="U1", text="only you")
        return wire

    wire = asyncio.run(main())
    assert wire.platform.texts("C1") == ["hi!"]
    message = wire.platform.message("1")
    assert message is not None and message.buttons[0].data == "apv:r:once"
    assert wire.ephemeral == [("C1", "U1", "only you")]


def test_slack_events_mention_shares_ts_and_bot_subtype() -> None:
    async def main() -> list[tuple[str, dict[str, object]]]:
        wire = SlackWire()
        seen: list[tuple[str, dict[str, object]]] = []

        @wire.app.event("message")
        async def on_message(event: dict[str, object], say: object) -> None:
            seen.append(("message", event))

        @wire.app.event("app_mention")
        async def on_mention(event: dict[str, object], say: object) -> None:
            seen.append(("app_mention", event))

        @wire.app.action("*")
        async def on_action(body: dict[str, object], ack: object) -> None:
            seen.append(("action", body))

        wire.platform.add_chat("C9", "group", scope_id="T2")
        wire.platform.group("C9", "U1", "hey <@U0BOT>", mention=True)
        wire.platform.bot_message("C9", "B7", "beep")
        wire.platform.button_press("C9", "U1", "apv:r:once")
        await wire.dispatch_pending()
        return seen

    seen = asyncio.run(main())
    kinds = [k for k, _ in seen]
    assert kinds == ["message", "app_mention", "message", "action"]
    assert seen[0][1]["ts"] == seen[1][1]["ts"] and seen[0][1]["team"] == "T2"
    assert seen[2][1]["subtype"] == "bot_message" and "user" not in seen[2][1]
