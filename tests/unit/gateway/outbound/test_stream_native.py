"""P16-33: the stream consumer's native branch (``Adapter.open_stream``), over a scripted native
stream and over the Telegram draft and Slack stream fakes (fake clock throughout)."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from indus.gateway.core.send import SendResult
from indus.gateway.outbound.stream import StreamConsumer, StreamOutcome
from tests.fakes.clock import FakeClock
from tests.unit.gateway.adapters.test_native_streams import (
    CHANNEL,
    DELTAS,
    DM,
    FINAL,
    SlackRig,
    TgRig,
    assert_prefix_stable,
)
from tests.unit.gateway.outbound.recording import RecordingAdapter, recording_adapter

_IDS = itertools.count(1)


@dataclass
class ScriptedStream:
    """A `NativeStream` that records what the consumer asks of it."""

    one_message_per_turn: bool = False
    give_up_after: int | None = None  # push answers False after this many frames
    final_result: SendResult | None = None
    frames: list[str] = field(default_factory=list[str])
    finals: list[str] = field(default_factory=list[str])
    abandoned: int = 0
    ids: Any = field(default_factory=lambda: _IDS)

    async def push(self, text: str) -> bool:
        if self.give_up_after is not None and len(self.frames) >= self.give_up_after:
            return False
        self.frames.append(text)
        return True

    async def finish(self, text: str) -> SendResult:
        self.finals.append(text)
        return self.final_result or SendResult.sent(f"native-{next(self.ids)}")

    async def abandon(self) -> None:
        self.abandoned += 1


class NativeAdapter(RecordingAdapter):
    """A recording adapter that offers a fresh scripted stream per ``open_stream`` call."""

    make: Callable[[], ScriptedStream]
    opened: list[ScriptedStream]
    explode: bool

    def open_stream(
        self,
        chat_id: str,
        *,
        reply_to: str | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> ScriptedStream | None:
        if self.explode:
            raise RuntimeError("the platform library broke")
        stream = self.make()
        self.opened.append(stream)
        return stream


def _clock() -> FakeClock:
    return FakeClock(datetime(2026, 1, 1, tzinfo=UTC))


def _adapter(clock: FakeClock, make: Callable[[], ScriptedStream] = ScriptedStream) -> Any:
    global _IDS
    _IDS = itertools.count(1)
    base = recording_adapter(clock=clock)
    attrs = {"capabilities": base.capabilities, "platform": base.platform}
    adapter: Any = type("NativeRecording", (NativeAdapter,), attrs)(clock=clock)
    adapter.make, adapter.opened, adapter.explode = make, [], False
    return adapter


def _run(
    adapter: RecordingAdapter,
    clock: FakeClock,
    script: Callable[[StreamConsumer], Awaitable[None]],
) -> StreamOutcome:
    async def main() -> StreamOutcome:
        consumer = StreamConsumer(adapter, "chat", clock=clock)
        task = asyncio.create_task(consumer.run())
        await script(consumer)
        return await task

    return asyncio.run(main())


async def _say(consumer: StreamConsumer, clock: FakeClock, text: str) -> None:
    consumer.delta(text)
    await clock.run_until_idle()
    await clock.advance_and_settle(1.0)


def test_frames_are_raw_prefixes_and_the_final_goes_through_the_stream() -> None:
    clock = _clock()
    adapter = _adapter(clock)

    async def script(c: StreamConsumer) -> None:
        await _say(c, clock, "Here is **bold** and `code` ")
        await _say(c, clock, "and more wor")
        c.finish("Here is **bold** and `code` and more words.")
        await clock.run_until_idle()

    outcome = _run(adapter, clock, script)
    (stream,) = adapter.opened
    assert stream.frames == [
        "Here is **bold** and `code` ",
        "Here is **bold** and `code` and more ",
    ]
    assert_prefix_stable(stream.frames)
    assert stream.finals == ["Here is **bold** and `code` and more words."]
    assert adapter.of("send", "edit") == []
    assert outcome.final_delivered and outcome.final_message_id == "native-1"


def test_a_tool_call_finishes_a_segment_stream_and_the_next_text_opens_another() -> None:
    clock = _clock()
    adapter = _adapter(clock)

    async def script(c: StreamConsumer) -> None:
        await _say(c, clock, "Let me look that up. ")
        c.tool_boundary()
        await clock.run_until_idle()
        await _say(c, clock, "Found it: three results. ")
        c.finish(None)
        await clock.run_until_idle()

    outcome = _run(adapter, clock, script)
    first, second = adapter.opened
    assert first.finals == ["Let me look that up."]
    assert second.finals == ["Found it: three results."]
    assert outcome.message_ids == ["native-1", "native-2"]
    assert outcome.final_delivered and adapter.of("send", "edit") == []


def test_a_whole_turn_stream_stays_open_across_tools_but_an_approval_seals_it() -> None:
    clock = _clock()
    adapter = _adapter(clock, lambda: ScriptedStream(one_message_per_turn=True))

    async def script(c: StreamConsumer) -> None:
        await _say(c, clock, "Checking the logs. ")
        c.tool_boundary()
        await clock.run_until_idle()
        await _say(c, clock, "The disk is full. ")
        sealed = c.approval_boundary()
        await clock.run_until_idle()
        assert await sealed
        await _say(c, clock, "Cleaned up. ")
        c.finish(None)
        await clock.run_until_idle()

    _run(adapter, clock, script)
    first, second = adapter.opened
    # one message for the turn: the tool call only adds a blank line, the approval seals it
    assert first.frames[-1] == "Checking the logs. \n\nThe disk is full. "
    assert_prefix_stable(first.frames)
    assert first.finals == ["Checking the logs. \n\nThe disk is full."]
    assert second.finals == ["Cleaned up."]


def test_a_stream_that_gives_up_hands_the_reply_to_edits() -> None:
    clock = _clock()
    adapter = _adapter(clock, lambda: ScriptedStream(give_up_after=1))

    async def script(c: StreamConsumer) -> None:
        await _say(c, clock, "First words here ")
        await _say(c, clock, "then the rest ")
        c.finish(None)
        await clock.run_until_idle()

    outcome = _run(adapter, clock, script)
    (stream,) = adapter.opened
    assert stream.frames == ["First words here "] and stream.finals == []
    assert adapter.of("send")  # edits from there on
    assert adapter.visible() == ["First words here then the rest"]
    assert outcome.final_delivered


def test_a_final_the_stream_cannot_deliver_is_sent_by_the_edit_path() -> None:
    clock = _clock()
    adapter = _adapter(
        clock, lambda: ScriptedStream(final_result=SendResult.failure("connect error: lost"))
    )

    async def script(c: StreamConsumer) -> None:
        await _say(c, clock, "Almost done ")
        c.finish("Almost done: here it is.")
        await clock.run_until_idle()

    outcome = _run(adapter, clock, script)
    (stream,) = adapter.opened
    assert stream.finals == ["Almost done: here it is."]
    assert adapter.visible() == ["Almost done: here it is."]
    assert outcome.final_delivered


def test_a_cancelled_turn_abandons_the_stream_and_sends_nothing() -> None:
    clock = _clock()
    adapter = _adapter(clock)

    async def main() -> None:
        consumer = StreamConsumer(adapter, "chat", clock=clock)
        task = asyncio.create_task(consumer.run())
        await _say(consumer, clock, "Working on it ")
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(main())
    (stream,) = adapter.opened
    assert stream.frames == ["Working on it "] and stream.abandoned == 1
    assert adapter.of("send", "edit") == []


def test_an_adapter_that_fails_to_open_a_stream_streams_by_edits() -> None:
    clock = _clock()
    adapter = _adapter(clock)
    adapter.explode = True

    async def script(c: StreamConsumer) -> None:
        await _say(c, clock, "Plain old edits ")
        c.finish(None)
        await clock.run_until_idle()

    outcome = _run(adapter, clock, script)
    assert adapter.opened == [] and adapter.visible() == ["Plain old edits"]
    assert outcome.final_delivered


# -- over the platform fakes (the builder's end-to-end check) -------------------------------------


async def _feed(consumer: StreamConsumer, clock: Any, deltas: list[str]) -> None:
    for delta in deltas:
        consumer.delta(delta)
        for _ in range(4):
            await clock.advance_and_settle(0.25)


def test_telegram_drafts_through_the_consumer() -> None:
    rig = TgRig()

    async def main() -> Any:
        consumer = StreamConsumer(rig.adapter, DM, clock=rig.clock, reply_to="77")
        task = asyncio.ensure_future(consumer.run())
        await _feed(consumer, rig.clock, ["Let me look ", "that up."])
        consumer.tool_boundary()
        await _feed(consumer, rig.clock, DELTAS)
        consumer.finish(FINAL)
        for _ in range(40):
            await rig.clock.advance_and_settle(0.5)
            if task.done():
                break
        return task.result()

    outcome = asyncio.run(main())
    by_draft: dict[int, list[str]] = {}
    for _, draft_id, text, _ in rig.bot.drafts:
        by_draft.setdefault(draft_id, []).append(text)
    assert len(by_draft) == 2  # one draft per segment
    for seq in by_draft.values():
        assert_prefix_stable(seq)
    sends = rig.called("send_message")
    assert len(sends) == 2 and "Sources" in sends[-1]["text"]
    assert rig.called("edit_message_text") == []
    assert outcome.final_delivered and rig.frames()


def test_slack_stream_through_the_consumer() -> None:
    rig = SlackRig()

    async def main() -> Any:
        await rig.up()
        consumer = StreamConsumer(
            rig.adapter, CHANNEL, clock=rig.clock, reply_to="1700000000.000005"
        )
        task = asyncio.ensure_future(consumer.run())
        await _feed(consumer, rig.clock, ["Let me look ", "that up."])
        consumer.tool_boundary()
        await _feed(consumer, rig.clock, DELTAS)
        consumer.finish(FINAL)
        for _ in range(40):
            await rig.clock.advance_and_settle(0.5)
            if task.done():
                break
        await rig.down()
        return task.result()

    outcome = asyncio.run(main())
    assert_prefix_stable(rig.frames())
    (message,) = rig.platform.messages(CHANNEL)
    assert message.text == "Let me look that up.\n\n" + FINAL  # one message for the whole turn
    assert rig.called("chat_postMessage") == [] and rig.called("chat_update") == []
    assert outcome.final_delivered
