"""P15-16: the stream consumer (fake clock throughout)."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from indus.gateway.outbound.filters import redact_final
from indus.gateway.outbound.stream import (
    StreamConsumer,
    StreamOutcome,
    StreamSettings,
    streaming_default,
)
from indus.protocol.events import ExitReason, MessageDelta, ToolStarted, TurnDone
from tests.fakes.clock import FakeClock
from tests.unit.gateway.outbound.recording import RecordingAdapter, fail, recording_adapter

CURSOR = StreamSettings().cursor


def _clock() -> FakeClock:
    return FakeClock(datetime(2026, 1, 1, tzinfo=UTC))


def _run(
    adapter: RecordingAdapter,
    clock: FakeClock,
    script: Callable[[StreamConsumer], Awaitable[None]],
    **kwargs: Any,
) -> StreamOutcome:
    async def main() -> StreamOutcome:
        consumer = StreamConsumer(adapter, "chat", clock=clock, **kwargs)
        task = asyncio.create_task(consumer.run())
        await script(consumer)
        return await task

    return asyncio.run(main())


async def _say(consumer: StreamConsumer, clock: FakeClock, text: str) -> None:
    consumer.delta(text)
    await clock.run_until_idle()


def test_streaming_defaults_per_platform() -> None:
    assert streaming_default("telegram")
    assert not streaming_default("discord")
    assert not streaming_default("slack")
    assert not streaming_default("matrix")


def test_edits_follow_interval_and_threshold() -> None:
    clock = _clock()
    adapter = recording_adapter(clock=clock)

    async def script(c: StreamConsumer) -> None:
        await _say(c, clock, "Hello there ")  # first text: shown at once
        clock.advance(0.1)
        await _say(c, clock, "more ")  # 5 new code points, 0.1 s: wait
        assert len(adapter.ops) == 1
        await clock.advance_and_settle(0.6)  # 0.7 s since the first send: still waiting
        assert len(adapter.ops) == 1
        await clock.advance_and_settle(0.1)  # 0.8 s: edit
        assert len(adapter.ops) == 2
        await _say(c, clock, "x" * 30 + " ")  # 31 new code points: edit without waiting
        assert len(adapter.ops) == 3
        c.finish()
        await clock.run_until_idle()

    outcome = _run(adapter, clock, script)
    send, edit1, edit2, final = adapter.ops
    assert (send.op, send.at, send.text) == ("send", 0.0, "Hello there" + CURSOR)
    assert (edit1.op, edit1.text) == ("edit", "Hello there more" + CURSOR)
    assert edit1.at == pytest.approx(0.8) and edit2.at == pytest.approx(0.8)
    assert final.text == "Hello there more " + "x" * 30
    assert outcome.final_delivered and outcome.message_ids == ["m1"]


def test_every_non_final_send_is_marked_interim() -> None:
    clock = _clock()
    adapter = recording_adapter(clock=clock, max_len=80)

    async def script(c: StreamConsumer) -> None:
        for i in range(40):
            await _say(c, clock, f"word{i} ")
            await clock.advance_and_settle(1.0)
            if i == 20:
                c.tool_boundary()
                await clock.run_until_idle()
        c.finish()

    _run(adapter, clock, script)
    assert len(adapter.ops) > 5
    *interim, final = adapter.ops
    assert all(op.interim and not op.final for op in interim)
    assert final.final and not final.interim


def test_overflow_seals_and_continues_in_a_new_message() -> None:
    clock = _clock()
    adapter = recording_adapter(clock=clock, max_len=60)
    words = [f"w{i:03d}" for i in range(60)]

    async def script(c: StreamConsumer) -> None:
        for word in words:
            await _say(c, clock, word + " ")
            await clock.advance_and_settle(1.0)
        c.finish()

    outcome = _run(adapter, clock, script)
    assert all(len(op.text) <= 60 for op in adapter.ops)
    visible = adapter.visible()
    assert len(visible) == len(outcome.message_ids) > 3
    assert not any(text.endswith(CURSOR) for text in visible)
    assert " ".join(visible).split() == words


def test_overflow_carries_an_open_fence() -> None:
    clock = _clock()
    adapter = recording_adapter(clock=clock, max_len=80)
    code = "".join(f"line_{i} = {i}\n" for i in range(30))

    async def script(c: StreamConsumer) -> None:
        await _say(c, clock, "```py\n")
        for line in code.splitlines(keepends=True):
            await _say(c, clock, line)
            await clock.advance_and_settle(1.0)
        await _say(c, clock, "```\nDone.")
        c.finish()

    _run(adapter, clock, script)
    visible = adapter.visible()
    assert len(visible) > 2
    for text in visible[:-1]:
        assert text.startswith("```py\n") and text.endswith("\n```")
    assert visible[-1].endswith("Done.")


def test_without_edit_support_each_sealed_segment_is_one_message() -> None:
    clock = _clock()
    adapter = recording_adapter(clock=clock, supports_edit=False)

    async def script(c: StreamConsumer) -> None:
        for word in ["Let", "me", "look", "that", "up", "for", "you", ""]:
            await _say(c, clock, word + " ")
            await clock.advance_and_settle(1.0)
        assert adapter.ops == []
        c.tool_boundary()
        await clock.run_until_idle()
        await _say(c, clock, "Found it: 42.")
        c.finish()

    _run(adapter, clock, script)
    assert [op.op for op in adapter.ops] == ["send", "send"]
    assert adapter.ops[0].text == "Let me look that up for you"
    assert adapter.ops[1].text == "Found it: 42." and adapter.ops[1].final


def test_approval_seals_and_resumes_below() -> None:
    clock = _clock()
    adapter = recording_adapter(clock=clock)

    async def script(c: StreamConsumer) -> None:
        await _say(c, clock, "I need to run a command ")
        sealed = c.approval_boundary()
        await clock.run_until_idle()
        assert await sealed
        assert adapter.ops[-1].op == "edit"
        assert adapter.ops[-1].text == "I need to run a command"
        await _say(c, clock, "It worked, all good.")
        c.finish()

    outcome = _run(adapter, clock, script)
    assert [op.op for op in adapter.ops] == ["send", "edit", "send"]
    assert adapter.ops[-1].final and outcome.message_ids == ["m1", "m2"]


def test_three_floods_stop_edits_and_final_sends_tail() -> None:
    clock = _clock()
    adapter = recording_adapter(clock=clock)
    adapter.edit_results.extend(fail("rate_limited", retry_after=3.0) for _ in range(3))

    async def script(c: StreamConsumer) -> None:
        await _say(c, clock, "First part is here ")
        for word in ["alpha", "beta", "gamma", "delta", "epsilon"]:
            await _say(c, clock, word + " ")
            await clock.advance_and_settle(31)
        c.finish()

    outcome = _run(adapter, clock, script)
    *refused, unstick = adapter.of("edit")
    assert len(refused) == 3
    assert [round(b.at - a.at, 1) for a, b in zip(refused, refused[1:], strict=False)] == [31, 31]
    assert unstick.text == "First part is here" and unstick.interim  # cursor taken off once
    last = adapter.ops[-1]
    assert last.op == "send" and last.final
    assert last.text == "alpha beta gamma delta epsilon"
    assert outcome.final_delivered


def test_flood_backoff_is_capped_at_30_seconds() -> None:
    clock = _clock()
    adapter = recording_adapter(clock=clock)
    adapter.edit_results.extend([fail("rate_limited", retry_after=500.0)])

    async def script(c: StreamConsumer) -> None:
        await _say(c, clock, "Opening words here ")
        await _say(c, clock, "next ")
        await clock.advance_and_settle(1.0)
        assert len(adapter.of("edit")) == 1  # refused
        await _say(c, clock, "more ")
        await clock.advance_and_settle(29.5)
        assert len(adapter.of("edit")) == 1
        await clock.advance_and_settle(0.6)
        assert len(adapter.of("edit")) == 2
        c.finish()

    _run(adapter, clock, script)


def test_final_edits_the_stream_message_instead_of_sending_again() -> None:
    clock = _clock()
    adapter = recording_adapter(clock=clock)

    async def script(c: StreamConsumer) -> None:
        await _say(c, clock, "The answer is 42 ")
        await clock.advance_and_settle(1.0)
        c.finish("The answer is 42 because of maths.\n\n_Sources: memory_")

    outcome = _run(adapter, clock, script)
    assert len(adapter.of("send")) == 1
    assert adapter.ops[-1].op == "edit" and adapter.ops[-1].final
    assert adapter.visible() == ["The answer is 42 because of maths.\n\n_Sources: memory_"]
    assert outcome.final_message_id == "m1"


def test_unrelated_final_after_sealed_heads_goes_below() -> None:
    clock = _clock()
    adapter = recording_adapter(clock=clock, max_len=40)

    async def script(c: StreamConsumer) -> None:
        for i in range(12):
            await _say(c, clock, f"part{i} ")
            await clock.advance_and_settle(1.0)
        c.finish("Completely different answer.")

    _run(adapter, clock, script)
    assert adapter.visible()[-1] == "Completely different answer."


def test_partial_tokens_are_held_and_secrets_masked() -> None:
    clock = _clock()
    adapter = recording_adapter(clock=clock)
    secret = "sk-" + "proj" + "A1b2C3d4" * 6

    async def script(c: StreamConsumer) -> None:
        await _say(c, clock, "Your key is ")
        for i in range(0, len(secret), 7):
            await _say(c, clock, secret[i : i + 7])
            await clock.advance_and_settle(1.0)
        await _say(c, clock, " keep it safe.")
        c.finish()

    _run(adapter, clock, script, redact=lambda text: redact_final(text, None))
    for op in adapter.ops:
        assert secret[:12] not in op.text, op.text
    assert adapter.ops[0].text == "Your key is" + CURSOR


def test_silence_token_is_never_shown() -> None:
    clock = _clock()
    adapter = recording_adapter(clock=clock)

    async def script(c: StreamConsumer) -> None:
        for piece in ["NO", "_RE", "PLY"]:
            await _say(c, clock, piece)
            await clock.advance_and_settle(1.0)
        c.finish()

    outcome = _run(adapter, clock, script)
    assert adapter.ops == [] and outcome.silenced and not outcome.final_delivered


def test_session_events_drive_the_stream() -> None:
    clock = _clock()
    adapter = recording_adapter(clock=clock)

    def ev(seq: int, cls: type, **fields: object) -> object:
        return cls(session_id="s", seq=seq, turn_id="t1", **fields)

    async def script(c: StreamConsumer) -> None:
        c.feed(ev(1, MessageDelta, text="Checking the files now "))
        await clock.run_until_idle()
        c.feed(ev(3, ToolStarted, call_id="c1", name="read_file"))
        c.feed(ev(4, MessageDelta, text="All files look fine."))
        c.feed(MessageDelta(session_id="s", seq=5, turn_id="other", text="NOT MINE"))
        c.feed(ev(6, TurnDone, reason=ExitReason.COMPLETED))

    _run(adapter, clock, script, turn_id="t1")
    assert adapter.visible() == ["Checking the files now", "All files look fine."]


def test_markdownv2_frames_are_escaped_and_bad_format_falls_back() -> None:
    clock = _clock()
    adapter = recording_adapter(clock=clock, markup="markdownv2")
    adapter.edit_results.append(fail("bad_format", "can't parse entities"))

    async def script(c: StreamConsumer) -> None:
        await _say(c, clock, "Version 1.2 is **out** ")
        c.finish()

    _run(adapter, clock, script)
    assert adapter.ops[0].text == "Version 1\\.2 is *out*" + CURSOR
    refused, plain = adapter.ops[1:]
    assert refused.text == "Version 1\\.2 is *out*"
    assert plain.text == "Version 1.2 is out" and plain.final


def test_cancel_leaves_the_message_without_cursor() -> None:
    clock = _clock()
    adapter = recording_adapter(clock=clock)

    async def main() -> None:
        consumer = StreamConsumer(adapter, "chat", clock=clock)
        task = asyncio.create_task(consumer.run())
        await _say(consumer, clock, "Working on it ")
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert adapter.visible() == ["Working on it "] or adapter.visible() == ["Working on it"]
    assert not adapter.ops[-1].text.endswith(CURSOR)


def test_first_send_failure_stops_edits_and_final_goes_out_once() -> None:
    clock = _clock()
    adapter = recording_adapter(clock=clock)
    adapter.send_results.append(fail("transient", "connection refused"))

    async def script(c: StreamConsumer) -> None:
        await _say(c, clock, "Something to say ")
        await clock.advance_and_settle(1.0)
        await _say(c, clock, "and more.")
        c.finish()

    outcome = _run(adapter, clock, script)
    assert [op.op for op in adapter.ops] == ["send", "send"]
    assert adapter.ops[-1].text == "Something to say and more." and outcome.final_delivered
