"""Send results, the error classifier and the retry policy (P15-04; R06.3, R06.4)."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from datetime import UTC, datetime
from typing import Any

import pytest

from indus.gateway.core.send import (
    DELIVERY_FAILED_NOTICE,
    ErrorKind,
    RetryPolicy,
    SendResult,
    classify_send_error,
    is_ambiguous_timeout,
    is_chat_level_not_found,
    send_with_retry,
)
from tests.fakes.clock import FakeClock


class ReadTimeout(Exception):  # named like the httpx class: the class name is part of the blob
    pass


class ConnectTimeout(Exception):
    pass


class RetryAfter(Exception):
    pass


class TimedOut(Exception):
    pass


K = ErrorKind

# (exception, text) → (kind, chat_level, ambiguous)
CASES: list[tuple[BaseException | None, str, ErrorKind, bool, bool]] = [
    (None, "Bad Request: message is too long", K.TOO_LONG, False, False),
    (None, "msg_too_long", K.TOO_LONG, False, False),
    (
        None,
        "Invalid Form Body: content: Must be 2000 or fewer in length.",
        K.TOO_LONG,
        False,
        False,
    ),
    (
        None,
        "Bad Request: can't parse entities: Can't find end of the entity",
        K.BAD_FORMAT,
        False,
        False,
    ),
    (None, "invalid_blocks", K.BAD_FORMAT, False, False),
    (None, 'Bad Request: unsupported start tag "x"', K.BAD_FORMAT, False, False),
    (None, "entity parse failure", K.BAD_FORMAT, False, False),
    (None, "Forbidden: bot was blocked by the user", K.FORBIDDEN, False, False),
    (None, "Forbidden: user is deactivated", K.FORBIDDEN, False, False),
    (None, "Bad Request: not enough rights to send text messages", K.FORBIDDEN, False, False),
    (None, "not_in_channel", K.FORBIDDEN, False, False),
    (None, "is_archived", K.FORBIDDEN, False, False),
    (None, "403 Forbidden (error code: 50001): Missing Access", K.FORBIDDEN, False, False),
    (None, "Cannot send messages to this user", K.FORBIDDEN, False, False),
    (PermissionError("nope"), "", K.FORBIDDEN, False, False),
    (None, "Bad Request: chat not found", K.NOT_FOUND, True, False),
    (None, "channel_not_found", K.NOT_FOUND, True, False),
    (None, "404 Not Found (error code: 10003): Unknown Channel", K.NOT_FOUND, True, False),
    (None, "Bad Request: message thread not found", K.NOT_FOUND, False, False),
    (None, "Bad Request: message to edit not found", K.NOT_FOUND, False, False),
    (None, "Bad Request: message to reply not found", K.NOT_FOUND, False, False),
    (None, "TOPIC_DELETED", K.NOT_FOUND, False, False),
    (None, "thread_not_found", K.NOT_FOUND, False, False),
    (None, "404 Not Found (error code: 10008): Unknown Message", K.NOT_FOUND, False, False),
    # Both readings: the sub-chat one wins, the chat stays alive.
    (None, "chat not found; message thread not found", K.NOT_FOUND, False, False),
    (None, "Too Many Requests: retry after 35", K.RATE_LIMITED, False, False),
    (RetryAfter("Flood control exceeded. Retry in 5 seconds"), "", K.RATE_LIMITED, False, False),
    (None, "ratelimited", K.RATE_LIMITED, False, False),
    (
        None,
        "429 Too Many Requests (error code: 0): You are being rate limited.",
        K.RATE_LIMITED,
        False,
        False,
    ),
    (ConnectTimeout("timed out while connecting"), "", K.UNKNOWN, False, True),
    (ConnectTimeout(""), "", K.TRANSIENT, False, False),
    (ConnectionRefusedError(111, "Connection refused"), "", K.TRANSIENT, False, False),
    (ConnectionResetError("peer"), "", K.TRANSIENT, False, False),
    (BrokenPipeError(), "", K.TRANSIENT, False, False),
    (None, "httpx.ConnectError: [Errno -2] Name or service not known", K.TRANSIENT, False, False),
    (None, "Cannot connect to host slack.com:443", K.TRANSIENT, False, False),
    (None, "Server disconnected", K.TRANSIENT, False, False),
    (ReadTimeout(""), "", K.UNKNOWN, False, True),
    (None, "httpx.WriteTimeout", K.UNKNOWN, False, True),
    (TimedOut("Timed out"), "", K.UNKNOWN, False, True),
    (TimeoutError(), "", K.UNKNOWN, False, True),
    (None, "NetworkError: httpx.ReadTimeout", K.UNKNOWN, False, True),  # ambiguity wins
    (None, "502 Bad Gateway", K.UNKNOWN, False, False),
    (None, "", K.UNKNOWN, False, False),
    (ValueError("weird"), "", K.UNKNOWN, False, False),
]


@pytest.mark.parametrize(("exc", "text", "kind", "chat_level", "ambiguous"), CASES)
def test_classifier_table(
    exc: BaseException | None, text: str, kind: ErrorKind, chat_level: bool, ambiguous: bool
) -> None:
    assert classify_send_error(exc, text) is kind
    assert is_chat_level_not_found(exc, text) is chat_level
    assert is_ambiguous_timeout(exc, text) is ambiguous
    result = SendResult.failure(text or None, exc=exc)
    assert result.kind is kind
    assert result.retryable is (kind in (K.TRANSIENT, K.RATE_LIMITED))
    assert result.maybe_delivered is (ambiguous and kind is K.UNKNOWN)
    assert result.chat_unreachable is (kind is K.FORBIDDEN or chat_level)


def test_table_has_thirty_rows_and_every_kind() -> None:
    assert len(CASES) >= 30
    assert {row[2] for row in CASES} == set(ErrorKind)


def test_result_helpers() -> None:
    ok = SendResult.sent("3", ("1", "2"))
    assert ok.ids == ("1", "2", "3") and ok.ok and not ok.chat_unreachable
    flood = SendResult.failure("x", retry_after=97 * 60)
    assert flood.kind is K.RATE_LIMITED and flood.is_flood and flood.retryable
    part = SendResult.failure("chat not found", delivered_ids=("a", "b"), remainder="tail")
    assert part.partial and part.ids == ("a", "b") and part.remainder == "tail"
    assert SendResult.failure("boom", remainder="ignored").remainder is None
    refused = SendResult.refused("editing")
    assert refused.unsupported and not refused.ok and refused.kind is None
    assert SendResult.skip().skipped and SendResult.skip().ok  # not a failure (P15-16)
    assert SendResult.failure().error == "send failed"


# -- retry ------------------------------------------------------------------------------------


class RecordingClock(FakeClock):
    def __init__(self) -> None:
        super().__init__(datetime(2026, 1, 1, tzinfo=UTC))
        self.sleeps: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        await super().sleep(seconds)


def drive(clock: RecordingClock, coro: Coroutine[Any, Any, SendResult]) -> SendResult:
    async def main() -> SendResult:
        task = asyncio.ensure_future(coro)
        for _ in range(200):
            await clock.run_until_idle()
            if task.done():
                return task.result()
            assert clock.pending == 1, "the retry loop waits only on the clock"
            clock.advance(clock.sleeps[-1])
        raise AssertionError("retry loop did not finish")

    return asyncio.run(main())


class Script:
    def __init__(self, *answers: SendResult | BaseException) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, bool]] = []

    async def __call__(self, text: str, plain: bool) -> SendResult:
        self.calls.append((text, plain))
        answer = self.answers.pop(0) if self.answers else SendResult.sent(f"id{len(self.calls)}")
        if isinstance(answer, BaseException):
            raise answer
        return answer


def run(script: Script, content: str = "hello", *, cap: float = 60.0, jitter: float = 0.5):
    clock = RecordingClock()
    policy = RetryPolicy(inline_wait_cap=cap)
    result = drive(
        clock, send_with_retry(script, content, clock=clock, policy=policy, rng=lambda: jitter)
    )
    return result, clock


CONNECT = SendResult.failure("ConnectError: connection refused")


def test_success_first_time_sleeps_never() -> None:
    script = Script(SendResult.sent("m1"))
    result, clock = run(script)
    assert result.ok and result.message_id == "m1"
    assert clock.sleeps == [] and script.calls == [("hello", False)]


def test_transient_backoff_then_success() -> None:
    script = Script(CONNECT, CONNECT, SendResult.sent("m3"))
    result, clock = run(script, jitter=0.5)
    assert result.ok and result.message_id == "m3"
    assert clock.sleeps == [2.5, 4.5]
    assert clock.monotonic() == pytest.approx(7.0)
    assert script.calls == [("hello", False)] * 3


def test_transient_exhausted_sends_one_notice() -> None:
    script = Script(CONNECT, CONNECT, CONNECT, SendResult.sent("notice"))
    result, clock = run(script)
    assert not result.ok and result.kind is K.TRANSIENT
    assert script.calls == [("hello", False)] * 3 + [(DELIVERY_FAILED_NOTICE, True)]
    assert len(clock.sleeps) == 2


def test_read_timeout_is_attempted_exactly_once() -> None:
    script = Script(ReadTimeout("read timed out"))
    result, clock = run(script)
    assert not result.ok and result.maybe_delivered and not result.retryable
    assert script.calls == [("hello", False)]
    assert clock.sleeps == []


def test_server_wait_replaces_one_backoff() -> None:
    flood = SendResult.failure("Too Many Requests: retry after 7", retry_after=7)
    script = Script(flood, CONNECT, SendResult.sent("ok"))
    result, clock = run(script, jitter=0.0)
    assert result.ok
    assert clock.sleeps == [7.0, 4.0]  # the wait replaced backoff 2; the second backoff stays


def test_long_flood_wait_is_handed_over_without_sleeping_or_notice() -> None:
    flood = SendResult.failure(exc=RetryAfter("Flood control"), retry_after=97 * 60)
    script = Script(flood)
    result, clock = run(script)
    assert result.is_flood and result.retry_after == 97 * 60
    assert clock.sleeps == [] and clock.monotonic() == 0.0
    assert script.calls == [("hello", False)]


def test_telegram_cap_refuses_a_six_second_wait() -> None:
    script = Script(SendResult.failure("retry after 6", retry_after=6))
    result, clock = run(script, cap=5.0)
    assert result.is_flood and clock.sleeps == []


def test_no_inline_sleep_ever_exceeds_the_cap() -> None:
    for wait in (None, 0.5, 3.0, 4.5, 5.0):
        for jitter in (0.0, 0.5, 0.999):
            first = SendResult.failure("flood", retry_after=wait)
            script = Script(first, first, first)
            _, clock = run(script, cap=5.0, jitter=jitter)
            assert clock.sleeps and max(clock.sleeps) <= 5.0, (wait, jitter, clock.sleeps)


def test_rate_limit_exhausted_sends_nothing_more() -> None:
    flood = SendResult.failure("Too Many Requests", retry_after=1)
    script = Script(flood, flood, flood)
    result, _ = run(script)
    assert result.kind is K.RATE_LIMITED
    assert script.calls == [("hello", False)] * 3  # no notice into the penalty


def test_flood_appearing_mid_loop_is_handed_over() -> None:
    script = Script(CONNECT, SendResult.failure("retry after 3600", retry_after=3600))
    result, clock = run(script)
    assert result.is_flood and len(clock.sleeps) == 1
    assert len(script.calls) == 2


def test_bad_format_falls_back_to_plain_text_once() -> None:
    script = Script(SendResult.failure("can't parse entities"), SendResult.sent("p"))
    result, clock = run(script)
    assert result.ok and script.calls == [("hello", False), ("hello", True)]
    assert clock.sleeps == []


def test_reclassification_after_each_attempt() -> None:
    script = Script(CONNECT, SendResult.failure("can't parse entities"), SendResult.sent("p"))
    result, _ = run(script)
    assert result.ok
    assert script.calls == [("hello", False), ("hello", False), ("hello", True)]


def test_timeout_after_a_retry_stops_everything() -> None:
    script = Script(CONNECT, ReadTimeout("x"))
    result, _ = run(script)
    assert result.maybe_delivered and len(script.calls) == 2


@pytest.mark.parametrize(
    "failure",
    [
        "Forbidden: bot was blocked by the user",
        "Bad Request: chat not found",
        "message is too long",
    ],
)
def test_unfixable_failures_are_not_resent_as_plain(failure: str) -> None:
    script = Script(SendResult.failure(failure))
    result, _ = run(script)
    assert not result.ok and script.calls == [("hello", False)]


def test_partial_split_resumes_only_the_remainder() -> None:
    first = SendResult.failure("ConnectError", delivered_ids=("c1", "c2"), remainder="third part")
    script = Script(first, SendResult.sent("c3"))
    result, _ = run(script, "first part second part third part")
    assert result.ok and result.ids == ("c1", "c2", "c3")
    assert script.calls == [("first part second part third part", False), ("third part", False)]


def test_failed_remainder_keeps_its_proof_for_the_next_retry() -> None:
    first = SendResult.failure("ConnectError", delivered_ids=("c1",), remainder="tail")
    script = Script(first, CONNECT, SendResult.sent("c2"))
    result, _ = run(script, "head tail")
    assert result.ok and result.ids == ("c1", "c2")
    assert [c[0] for c in script.calls] == ["head tail", "tail", "tail"]


def test_partial_without_remainder_is_never_resent_whole() -> None:
    first = SendResult.failure("ConnectError", delivered_ids=("c1",))
    script = Script(first)
    result, clock = run(script, "head tail")
    assert result.partial and result.ids == ("c1",)
    assert script.calls == [("head tail", False)] and clock.sleeps == []


def test_partial_format_failure_is_not_resent_as_plain() -> None:
    first = SendResult.failure("can't parse entities", delivered_ids=("c1",), remainder="t")
    script = Script(first)
    result, _ = run(script, "h t")
    assert result.partial and len(script.calls) == 1


def test_a_raising_attempt_is_classified() -> None:
    script = Script(ConnectionRefusedError("refused"), SendResult.sent("m"))
    result, clock = run(script)
    assert result.ok and len(clock.sleeps) == 1


def test_unsupported_and_final_failures_return_at_once() -> None:
    script = Script(SendResult.refused("editing"))
    result, _ = run(script)
    assert result.unsupported and len(script.calls) == 1

    async def main() -> SendResult:
        clock = RecordingClock()
        attempt = Script(CONNECT)
        return await send_with_retry(attempt, "x", clock=clock, is_final=lambda r: True)

    assert asyncio.run(main()).kind is K.TRANSIENT
