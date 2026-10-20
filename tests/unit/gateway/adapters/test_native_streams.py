"""P16-33: native draft streaming — Telegram drafts and Slack streams over their wire fakes.

The streams are driven the way the stream consumer's native branch will drive them (proposed to
the lead): ``push`` gets the raw reply so far up to its last whitespace, ``finish`` the declared
final. Recorded frame sequences live in ``tests/fixtures/gateway/native_stream_frames/``; set
``INDUS_RECORD_FRAMES=1`` to write them again from the fakes.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from indus.foundation.config.schema import PlatformSection
from indus.foundation.scope import Scope
from indus.gateway.adapters._native import FloodGate, native_stream_enabled
from indus.gateway.adapters.slack import create_adapter as create_slack
from indus.gateway.adapters.slack.adapter import MAX_LEN as SLACK_MAX_LEN
from indus.gateway.adapters.slack.adapter import SlackAdapter
from indus.gateway.adapters.slack.streams import SlackStream
from indus.gateway.adapters.telegram import create_adapter as create_telegram
from indus.gateway.adapters.telegram.adapter import TelegramAdapter, TelegramOptions
from indus.gateway.adapters.telegram.bot_api import BotApi
from indus.gateway.adapters.telegram.drafts import DRAFT_MAX_UNITS, DraftStream
from indus.gateway.config import AdapterConfig, PlatformSettings, load_gateway_settings
from indus.gateway.core.send import SendResult
from indus.gateway.outbound.chunker import utf16_units
from tests.contract.gateway.slack_case import wire_sdk
from tests.fakes.clock import FakeClock
from tests.fakes.platform.core import FailureKind, FakeChatPlatform
from tests.fakes.platform.slack_wire import SlackPlatform, SlackWire
from tests.fakes.platform.telegram_wire import TelegramWire
from tests.unit.cli.commands.cmdrun import write_settings

FRAMES = Path(__file__).resolve().parents[3] / "fixtures" / "gateway" / "native_stream_frames"
START = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
CURSOR = "▉"
BOT = "bot-" + "token-" + "x" * 12  # built at run time, not Slack-shaped
APP = "app-" + "token-" + "y" * 12
TG_TOKEN = "".join(["55501", "234", ":", "Ab", "c-" * 16, "d"])
DM = "4242"
CHANNEL = "C1"
THREAD = "1700000000.000005"

# A reply that opens a code fence mid-stream: an edit transport would close it in every frame.
DELTAS = [
    "Here is ",
    "the **fix** you",
    " asked for:\n\n```py",
    "thon\nprint(",
    "'hi')\n",
    "```\n\nThat",
    " is all",
    ".",
]
STREAMED = "".join(DELTAS)
FINAL = STREAMED + "\n\n_Sources: notes.md_"  # added after streaming: rides the final


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def views(deltas: list[str]) -> list[str]:
    """What the consumer pushes: the reply so far up to its last whitespace, once each."""
    shown: list[str] = []
    text = ""
    for delta in deltas:
        text += delta
        cut = max(text.rfind(" "), text.rfind("\n"))
        if cut >= 0 and (not shown or shown[-1] != text[: cut + 1]):
            shown.append(text[: cut + 1])
    return shown


def assert_prefix_stable(frames: list[str]) -> None:
    for earlier, later in zip(frames, frames[1:], strict=False):
        assert later.startswith(earlier), (earlier, later)
    assert all(CURSOR not in frame for frame in frames)


def recorded(name: str, frames: dict[str, Any]) -> dict[str, Any]:
    path = FRAMES / f"{name}.json"
    if os.environ.get("INDUS_RECORD_FRAMES") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(frames, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return json.loads(path.read_text(encoding="utf-8"))


# -- rigs ------------------------------------------------------------------------------------------


class TgRig:
    """A Telegram adapter on its wire fake, marked connected (no polling)."""

    def __init__(self, *, native: bool = True) -> None:
        self.clock = FakeClock(START)
        self.platform = FakeChatPlatform(
            "telegram", clock=self.clock, max_len=4096, len_fn=utf16_units, bot_user_id="1000"
        )
        self.wire = TelegramWire(self.platform)
        self.bot = self.wire.bot
        self.adapter = TelegramAdapter(
            cast(BotApi, self.bot), clock=self.clock, options=TelegramOptions(native_stream=native)
        )
        self.adapter.connected = True

    def stream(self, chat: str = DM, **meta: Any) -> DraftStream:
        stream = self.adapter.open_stream(chat, reply_to="77", meta=meta)
        assert stream is not None
        return stream

    def called(self, name: str) -> list[dict[str, Any]]:
        return [kw for n, kw in self.bot.calls if n == name]

    def frames(self) -> list[str]:
        return [text for _, _, text, _ in self.bot.drafts]

    async def push(self, stream: DraftStream, text: str) -> bool:
        """A frame a little later (the chat's 1 s send slot has passed)."""
        self.clock.advance(1.0)
        return await stream.push(text)

    async def settle[T](self, awaitable: Awaitable[T]) -> T:
        """Await while moving the fake clock past the send slot's waits."""
        task = asyncio.ensure_future(awaitable)
        for _ in range(1000):
            await self.clock.run_until_idle()
            if task.done():
                return task.result()
            self.clock.advance(0.5)
        task.cancel()
        raise AssertionError("never finished on the fake clock")

    async def finish(self, stream: DraftStream, text: str) -> SendResult:
        return await self.settle(stream.finish(text))


def slack_settings() -> PlatformSettings:
    return PlatformSettings(
        "slack",
        True,
        "credentials",
        PlatformSection(),
        credentials={"SLACK_BOT_TOKEN": BOT, "SLACK_APP_TOKEN": APP},
    )


class SlackRig:
    def __init__(self, *, native: bool = True) -> None:
        self.clock = FakeClock(START)
        self.platform = SlackPlatform(
            "slack", clock=self.clock, max_len=SLACK_MAX_LEN, bot_user_id="U0BOT"
        )
        self.wire = SlackWire(self.platform)
        self.adapter = SlackAdapter(
            clock=self.clock,
            bot_token=BOT,
            app_token=APP,
            settings=slack_settings(),
            sdk=wire_sdk(self.wire),
            native_stream=native,
        )

    async def up(self) -> None:
        await self.adapter.connect()

    async def down(self) -> None:
        await self.adapter.disconnect()

    def stream(self, **meta: Any) -> SlackStream:
        stream = self.adapter.open_stream(CHANNEL, reply_to=THREAD, meta={"user_id": "U9", **meta})
        assert stream is not None
        return stream

    def called(self, name: str) -> list[dict[str, Any]]:
        return [kw for n, kw in self.wire.client.calls if n == name]

    def frames(self) -> list[str]:
        """The stream messages' text after each frame (one reply, so cumulative)."""
        out: list[str] = []
        text = ""
        for op, _, piece in self.wire.stream_log:
            if op == "stop":
                break
            text += piece
            out.append(text)
        return out


async def drive(stream: Any, deltas: list[str], clock: FakeClock | None = None) -> None:
    for shown in views(deltas):
        if clock is not None:
            clock.advance(1.0)
        assert await stream.push(shown)


# -- the phase row's Verify column -----------------------------------------------------------------


def test_recorded_telegram_draft_frames_are_prefix_stable() -> None:
    rig = TgRig()

    async def main() -> Any:
        stream = rig.stream()
        await drive(stream, DELTAS, rig.clock)
        return await rig.finish(stream, FINAL)

    result = run(main())
    frames = rig.frames()
    record = recorded("telegram_drafts", {"deltas": DELTAS, "final": FINAL, "frames": frames})
    assert frames == record["frames"]
    assert_prefix_stable(frames)
    # the fence opened mid-stream stays open in the frame (no per-tick fence closing)
    assert any(f.count("```") == 1 for f in frames)
    assert all("\\" not in f for f in frames)  # plain text: no MarkdownV2 escaping in frames
    assert {draft_id for _, draft_id, _, _ in rig.bot.drafts} == {rig.bot.drafts[0][1]}
    # the final is one real message holding the augmentation, sent once
    (sent,) = rig.called("send_message")
    assert sent["parse_mode"] == "MarkdownV2" and "Sources" in sent["text"]
    assert result.ok and result.message_id == rig.platform.messages(DM)[0].message_id


def test_recorded_slack_stream_frames_are_prefix_stable() -> None:
    rig = SlackRig()

    async def main() -> Any:
        await rig.up()
        stream = rig.stream()
        await drive(stream, DELTAS)
        result = await stream.finish(FINAL)
        await rig.down()
        return result

    result = run(main())
    frames = rig.frames()
    record = recorded("slack_stream", {"deltas": DELTAS, "final": FINAL, "frames": frames})
    assert frames == record["frames"]
    assert_prefix_stable(frames)
    assert any(f.count("```") == 1 for f in frames)
    assert "**fix**" in frames[-1]  # standard Markdown in the stream, never mrkdwn
    (start,) = rig.called("chat_startStream")
    assert start["thread_ts"] == THREAD and start["recipient_user_id"] == "U9"
    assert start["recipient_team_id"] == "T1"
    # the augmentation rides the seal; the stream is the reply: nothing posted besides it
    (stop,) = rig.called("chat_stopStream")
    assert stop["markdown_text"] == FINAL[len(views(DELTAS)[-1]) :]
    assert rig.called("chat_postMessage") == [] and rig.called("chat_update") == []
    (message,) = rig.platform.messages(CHANNEL)
    assert message.text == FINAL and result.ok and result.message_id == start_ts(rig)


def start_ts(rig: SlackRig) -> str:
    return rig.wire.stream_log[0][1]


def test_no_duplicate_final_after_seal() -> None:
    tg = TgRig()
    slack = SlackRig()

    async def main() -> None:
        stream = tg.stream()
        await drive(stream, DELTAS, tg.clock)
        first = await tg.finish(stream, FINAL)
        assert await tg.finish(stream, FINAL) == first  # the same final again: nothing sent
        assert not await stream.push(FINAL + " more")  # a finished stream takes no frames
        changed = await tg.finish(stream, FINAL + " (edited)")  # beside the sealed stream: edit
        assert changed == first
        await slack.up()
        s_stream = slack.stream()
        await drive(s_stream, DELTAS)
        s_first = await s_stream.finish(FINAL)
        assert await s_stream.finish(FINAL) == s_first
        assert await s_stream.finish(FINAL + " (edited)") == s_first
        await slack.down()

    run(main())
    assert len(tg.called("send_message")) == 1
    (edit,) = tg.called("edit_message_text")
    assert "edited" in edit["text"]
    assert len(slack.called("chat_stopStream")) == 1 and slack.called("chat_postMessage") == []
    (update,) = slack.called("chat_update")
    assert update["ts"] == start_ts(slack) and "edited" in update["text"]
    assert len(slack.platform.messages(CHANNEL)) == 1


def test_a_flood_during_a_stream_backs_off_without_sleeping() -> None:
    rig = TgRig()
    slack = SlackRig()
    texts = [f"word{n} " * (n + 1) for n in range(8)]
    growing = ["".join(texts[: n + 1]) for n in range(8)]

    async def telegram() -> None:
        stream = rig.stream()
        assert await rig.push(stream, growing[0])
        rig.platform.fail(FailureKind.RATE_LIMIT, op="draft", retry_after=7)
        rig.clock.advance(1.0)
        before = rig.clock.monotonic()
        assert await stream.push(growing[1])  # refused: held 7 s, not slept
        assert rig.clock.monotonic() == before
        assert await rig.push(stream, growing[2])  # inside the hold: no call
        assert len(rig.called("send_message_draft")) == 2
        rig.clock.advance(6)
        assert await stream.push(growing[3])
        assert rig.frames() == [growing[0], growing[3]]
        # three floods in a row stop interim frames; the final still goes out
        rig.platform.fail(FailureKind.RATE_LIMIT, op="draft", retry_after=1, times=3)
        for n in (4, 5, 6):
            rig.clock.advance(31)
            assert await stream.push(growing[n])
        assert stream.gate.stopped
        rig.clock.advance(60)
        assert await stream.push(growing[7])
        assert rig.frames() == [growing[0], growing[3]]
        assert (await rig.finish(stream, growing[7])).ok

    async def slack_side() -> None:
        await slack.up()
        stream = slack.stream()
        assert await stream.push(growing[0])
        slack.platform.fail(FailureKind.RATE_LIMIT, op="stream", retry_after=4)
        assert await stream.push(growing[1])
        assert await stream.push(growing[2])
        assert len(slack.called("chat_appendStream")) == 1
        slack.clock.advance(4)
        assert await stream.push(growing[3])
        assert slack.frames() == [growing[0], growing[3]]
        assert stream.gate.waits == [4.0]
        assert (await stream.finish(growing[7])).ok
        await slack.down()

    run(telegram())
    run(slack_side())
    assert rig.platform.messages(DM)[0].text.startswith("word0")
    (message,) = slack.platform.messages(CHANNEL)
    assert message.text == growing[7]


# -- Telegram drafts -------------------------------------------------------------------------------


def test_drafts_only_in_private_chats_and_only_when_switched_on() -> None:
    rig = TgRig()
    assert rig.adapter.open_stream("-100123") is None  # a group
    assert TgRig(native=False).adapter.open_stream(DM) is None
    rig.adapter.send_only = True
    assert rig.adapter.open_stream(DM) is None
    rig.adapter.send_only = False
    rig.adapter.connected = False
    assert rig.adapter.open_stream(DM) is None
    rig.adapter.connected = True
    one, two = rig.stream(), rig.stream()
    assert one.draft_id != two.draft_id and one.draft_id > 0
    assert one.one_message_per_turn is False


def test_a_rewriting_or_repeated_frame_is_skipped() -> None:
    rig = TgRig()

    async def main() -> None:
        stream = rig.stream()
        assert await rig.push(stream, "Hello there ")
        assert await rig.push(stream, "Hello there ")  # identical: no call
        assert await rig.push(stream, "Hullo there friend ")  # rewrites sent text: skipped
        assert await rig.push(stream, "Hello there friend ")

    run(main())
    assert rig.frames() == ["Hello there ", "Hello there friend "]


def test_draft_frames_share_the_chats_send_slot() -> None:
    rig = TgRig()

    async def main() -> None:
        stream = rig.stream()
        async with rig.adapter.slots.hold(DM):
            assert await stream.push("busy slot ")  # a send holds the slot: skipped
        assert await stream.push("busy slot later ")  # right after the send: not due yet
        assert await rig.push(stream, "busy slot later on ")

    run(main())
    assert rig.frames() == ["busy slot later on "]


def test_text_over_the_draft_cap_freezes_the_draft_and_the_final_splits() -> None:
    rig = TgRig()
    long = ("x" * 99 + " ") * 50  # 5,000 units

    async def main() -> Any:
        stream = rig.stream()
        assert await rig.push(stream, long[:3000])
        assert await rig.push(stream, long)
        assert stream.frozen
        assert await rig.push(stream, long + "y ")
        return await rig.finish(stream, long + "end")

    result = run(main())
    assert rig.frames() == [long[:3000]]
    assert utf16_units(rig.frames()[0]) <= DRAFT_MAX_UNITS
    assert result.ok and len(rig.called("send_message")) == 2  # the final split normally


def test_a_refused_draft_ends_the_stream_and_switches_drafts_off() -> None:
    rig = TgRig()
    rig.bot.refuse_drafts = "TEXTDRAFT_PEER_INVALID"

    async def main() -> Any:
        stream = rig.stream()
        assert not await rig.push(stream, "first words ")
        assert not await rig.push(stream, "first words more ")
        return await rig.finish(stream, "first words more and the end")

    result = run(main())
    assert result.ok and rig.adapter.drafts_off
    assert rig.adapter.open_stream(DM) is None  # streaming goes on by edits
    assert len(rig.called("send_message_draft")) == 1


def test_a_topic_refusal_turns_drafts_off_inside_topics_only() -> None:
    rig = TgRig()
    rig.bot.refuse_drafts = "message thread not found in this topic"

    async def main() -> None:
        stream = rig.stream(thread_id="12")
        assert stream.thread == 12
        assert not await rig.push(stream, "in a topic ")

    run(main())
    assert rig.adapter.topic_drafts_off and not rig.adapter.drafts_off
    assert rig.adapter.open_stream(DM, meta={"thread_id": "12"}) is None
    assert rig.adapter.open_stream(DM) is not None
    (frame,) = rig.called("send_message_draft")
    assert frame["message_thread_id"] == 12


def test_lost_frames_are_skipped_then_end_the_stream() -> None:
    rig = TgRig()
    rig.platform.fail(FailureKind.READ_TIMEOUT, op="draft", times=3)

    async def main() -> None:
        stream = rig.stream()
        assert await rig.push(stream, "one ")
        assert await rig.push(stream, "one two ")
        assert not await rig.push(stream, "one two three ")
        assert stream.dead

    run(main())
    assert not rig.adapter.drafts_off


def test_the_final_falls_back_to_plain_text_on_a_markup_refusal() -> None:
    rig = TgRig()
    rig.platform.fail(FailureKind.PARSE_ERROR, op="send")

    async def main() -> Any:
        stream = rig.stream()
        await drive(stream, DELTAS, rig.clock)
        return await rig.finish(stream, FINAL)

    result = run(main())
    first, second = rig.called("send_message")
    assert first["parse_mode"] == "MarkdownV2" and second["parse_mode"] is None
    assert result.ok and second["text"] == FINAL


def test_abandoning_a_draft_sends_nothing() -> None:
    rig = TgRig()

    async def main() -> None:
        stream = rig.stream()
        assert await rig.push(stream, "partial answer ")
        await stream.abandon()
        assert not await rig.push(stream, "partial answer more ")

    run(main())
    assert rig.called("send_message") == [] and rig.frames() == ["partial answer "]


# -- Slack streams ---------------------------------------------------------------------------------


def test_slack_streams_need_a_thread_the_switch_and_a_connection() -> None:
    rig = SlackRig()
    assert rig.adapter.open_stream(CHANNEL, reply_to=THREAD) is None  # not connected

    async def main() -> None:
        await rig.up()
        assert rig.adapter.open_stream(CHANNEL) is None  # no thread to stream into
        stream = rig.adapter.open_stream(CHANNEL, meta={"thread_id": THREAD})
        assert stream is not None and stream.one_message_per_turn
        await rig.down()

    run(main())
    off = SlackRig(native=False)

    async def off_main() -> None:
        await off.up()
        assert off.adapter.open_stream(CHANNEL, reply_to=THREAD) is None
        await off.down()

    run(off_main())


def test_slack_without_ai_streaming_switches_native_streams_off() -> None:
    rig = SlackRig()
    rig.wire.stream_refusal = "not_allowed"

    async def main() -> Any:
        await rig.up()
        stream = rig.stream()
        assert not await stream.push("hello there ")
        result = await stream.finish("hello there friend")
        late = rig.adapter.open_stream(CHANNEL, reply_to=THREAD)
        await rig.down()
        return result, late

    result, late = run(main())
    assert rig.adapter.native_off and late is None
    assert not result.ok and result.unsupported  # nothing on screen: the caller sends normally


def test_a_stream_slack_sealed_continues_in_a_fresh_one() -> None:
    rig = SlackRig()
    rig.wire.seal_after = 1

    async def main() -> Any:
        await rig.up()
        stream = rig.stream()
        await drive(stream, DELTAS)
        result = await stream.finish(FINAL)
        await rig.down()
        return stream, result

    stream, result = run(main())
    assert len(rig.called("chat_startStream")) > 1
    joined = "".join(m.text for m in rig.platform.messages(CHANNEL))
    assert joined == FINAL
    assert result.ok and result.message_id == stream.message_ids[-1]
    assert result.continuation_ids == tuple(stream.message_ids[:-1])


def test_a_final_unrelated_to_the_stream_seals_and_edits() -> None:
    rig = SlackRig()

    async def main() -> Any:
        await rig.up()
        stream = rig.stream()
        await drive(stream, DELTAS)
        result = await stream.finish("A *different* answer.")
        await rig.down()
        return result

    result = run(main())
    (stop,) = rig.called("chat_stopStream")
    assert stop.get("markdown_text") is None
    (update,) = rig.called("chat_update")
    assert update["text"] == "A _different_ answer."  # mrkdwn outside the stream
    assert result.ok and rig.called("chat_postMessage") == []


def test_a_failed_seal_is_settled_by_one_edit() -> None:
    rig = SlackRig()

    async def main() -> Any:
        await rig.up()
        stream = rig.stream()
        await drive(stream, DELTAS)
        rig.platform.fail(FailureKind.READ_TIMEOUT, op="stop", times=5)
        result = await stream.finish(FINAL)
        await rig.down()
        return result

    result = run(main())
    assert len(rig.called("chat_stopStream")) == 1  # something was being added: no retry
    (update,) = rig.called("chat_update")
    assert "Sources" in update["text"] and result.ok
    assert rig.called("chat_postMessage") == []


def test_a_failed_bare_seal_is_retried_once() -> None:
    rig = SlackRig()

    async def main() -> Any:
        await rig.up()
        stream = rig.stream()
        await drive(stream, DELTAS)
        rig.platform.fail(FailureKind.READ_TIMEOUT, op="stop", times=1)
        result = await stream.finish(views(DELTAS)[-1])
        await rig.down()
        return result

    result = run(main())
    assert len(rig.called("chat_stopStream")) == 2 and rig.called("chat_update") == []
    assert result.ok


def test_a_stream_past_the_cap_seals_what_fits_and_the_rest_follows() -> None:
    rig = SlackRig()
    paragraph = "word " * 1999 + "end\n"  # 10,000 characters
    whole = paragraph * 5

    async def main() -> Any:
        await rig.up()
        stream = rig.stream()
        assert await stream.push(paragraph * 3)
        assert await stream.push(whole)
        assert stream.full
        result = await stream.finish(whole)
        await rig.down()
        return result

    result = run(main())
    messages = rig.platform.messages(CHANNEL)
    assert len(messages) == 2 and all(len(m.text) <= SLACK_MAX_LEN for m in messages)
    assert messages[0].text.startswith(paragraph * 3)  # the stream kept what it showed
    assert (messages[0].text + " " + messages[1].text).rstrip() == whole.rstrip()
    (post,) = rig.called("chat_postMessage")
    assert post["thread_ts"] == THREAD
    assert result.ok and result.continuation_ids == (start_ts(rig),)


def test_a_frame_that_fails_after_the_message_exists_leaves_it_to_the_final() -> None:
    rig = SlackRig()

    async def main() -> Any:
        await rig.up()
        stream = rig.stream()
        assert await stream.push("first words ")
        rig.platform.fail(FailureKind.READ_TIMEOUT, op="stream")
        assert await stream.push("first words then ")
        assert stream.stalled
        assert await stream.push("first words then more ")  # no more frames
        result = await stream.finish("first words then more, and the end.")
        await rig.down()
        return result

    result = run(main())
    assert len(rig.called("chat_appendStream")) == 1
    (update,) = rig.called("chat_update")
    assert update["text"] == "first words then more, and the end." and result.ok


def test_abandoning_a_slack_stream_seals_it() -> None:
    rig = SlackRig()

    async def main() -> None:
        await rig.up()
        stream = rig.stream()
        assert await stream.push("partial answer ")
        await stream.abandon()
        assert not await stream.push("partial answer more ")
        await rig.down()

    run(main())
    (stop,) = rig.called("chat_stopStream")
    assert stop.get("markdown_text") is None
    assert rig.wire.open_streams == {}


# -- the switch ------------------------------------------------------------------------------------


def test_the_switch_is_display_platforms_native_stream(scope: Scope) -> None:
    assert not native_stream_enabled(scope, "telegram")
    secrets: Any = scope.secrets
    secrets.set("TELEGRAM_BOT_TOKEN", TG_TOKEN)
    secrets.set("SLACK_BOT_TOKEN", BOT)
    secrets.set("SLACK_APP_TOKEN", APP)
    write_settings(
        scope,
        "display:\n  platforms:\n    telegram:\n      native_stream: true\n"
        "    slack:\n      native_stream: true\n",
    )
    assert native_stream_enabled(scope, "telegram") and native_stream_enabled(scope, "slack")
    assert not native_stream_enabled(scope, "discord")
    settings = load_gateway_settings(scope)
    tg_settings, slack_cfg = settings.get("telegram"), settings.get("slack")
    assert tg_settings is not None and slack_cfg is not None
    telegram = create_telegram(AdapterConfig("telegram", tg_settings, scope, scope.clock))
    slack = create_slack(AdapterConfig("slack", slack_cfg, scope, scope.clock))
    assert telegram.options.native_stream
    assert isinstance(slack, SlackAdapter) and slack.native_stream
    write_settings(scope, "display:\n  platforms:\n    telegram:\n      native_stream: 'yes'\n")
    assert not native_stream_enabled(scope, "telegram")  # only a real true turns it on


def test_the_flood_gate_caps_and_resets() -> None:
    clock = FakeClock(START)
    gate = FloodGate(clock)
    gate.flood(None)
    gate.flood(None)
    assert gate.waits == [1.0, 2.0] and not gate.open()
    gate.passed()
    assert gate.strikes == 0
    gate.flood(500)
    assert gate.waits[-1] == 30.0
    clock.advance(30)
    assert gate.open()


@pytest.mark.parametrize("name", ["telegram_drafts", "slack_stream"])
def test_the_recorded_sequences_are_kept(name: str) -> None:
    record = json.loads((FRAMES / f"{name}.json").read_text(encoding="utf-8"))
    assert record["deltas"] == DELTAS and record["final"] == FINAL
    assert_prefix_stable(record["frames"])
