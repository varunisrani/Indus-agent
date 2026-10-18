"""Server-sent events decoder (P03-07); framing per the WHATWG HTML standard."""

from __future__ import annotations

import random
from collections.abc import AsyncIterator

import httpx
import pytest

from indus.providers.sse import MAX_LINE_BYTES, SSEDecoder, SSEEvent, SSELineTooLong, aiter_sse
from indus.providers.types import KeepAlive
from tests.unit.providers._support import run


def decode(*chunks: bytes) -> list[object]:
    decoder = SSEDecoder()
    out: list[object] = []
    for chunk in chunks:
        out.extend(decoder.feed(chunk))
    out.extend(decoder.flush())
    return out


@pytest.mark.parametrize(
    ("chunks", "expected"),
    [
        ((b"data: a\r\n\r\n",), [SSEEvent("message", "a")]),
        ((b"data: a\n\n",), [SSEEvent("message", "a")]),
        ((b"data: a\r\r",), [SSEEvent("message", "a")]),
        # lone CR at the end of a chunk, LF at the start of the next: one line end
        (
            (b"data: a\r", b"\n\r", b"\ndata: b\n\n"),
            [SSEEvent("message", "a"), SSEEvent("message", "b")],
        ),
        ((b"data: one\ndata: two\n\n",), [SSEEvent("message", "one\ntwo")]),
        ((b"data:tight\n\n",), [SSEEvent("message", "tight")]),
        ((b"data:  two spaces\n\n",), [SSEEvent("message", " two spaces")]),
        ((b"event: ping\ndata: {}\n\n",), [SSEEvent("ping", "{}")]),
        ((b"\xef\xbb\xbfdata: x\n\n",), [SSEEvent("message", "x")]),
        ((b"\xef", b"\xbb", b"\xbfdata: x\n\n"), [SSEEvent("message", "x")]),
        ((b"retry: 10x\ndata: r\n\n",), [SSEEvent("message", "r")]),
        ((b"retry: 1500\ndata: r\n\n",), [SSEEvent("message", "r", retry=1500)]),
        (
            (b"id: 7\ndata: a\n\ndata: b\n\n",),
            [SSEEvent("message", "a", "7"), SSEEvent("message", "b", "7")],
        ),
        ((b"id: a\x00b\ndata: a\n\n",), [SSEEvent("message", "a")]),
        ((b"bogus: 1\ndata: a\n\n",), [SSEEvent("message", "a")]),
        ((b"event: x\n\n",), []),  # no data → nothing dispatched
        ((b"data\n\n",), [SSEEvent("message", "")]),  # field without colon, empty value
        ((b"data: [DONE]\n\n",), [SSEEvent("message", "[DONE]")]),
        ((b"data: tail-without-blank-line",), [SSEEvent("message", "tail-without-blank-line")]),
    ],
)
def test_framing_table(chunks: tuple[bytes, ...], expected: list[SSEEvent]) -> None:
    assert decode(*chunks) == expected


def test_comment_is_keepalive() -> None:
    assert decode(b": OPENROUTER PROCESSING\n\n", b":\n") == [
        KeepAlive("OPENROUTER PROCESSING"),
        KeepAlive(""),
    ]


def test_multibyte_split_across_chunks() -> None:
    raw = "data: héllo → 世界\n\n".encode()
    cut = raw.index("世".encode()) + 1
    assert decode(raw[:cut], raw[cut:]) == [SSEEvent("message", "héllo → 世界")]


def test_second_bom_is_not_stripped() -> None:
    events = decode(b"\xef\xbb\xbfdata: a\n\n\xef\xbb\xbfdata: b\n\n")
    assert events[0] == SSEEvent("message", "a")
    assert len(events) == 1  # "﻿data" is an unknown field name, ignored


def test_line_over_limit_raises() -> None:
    decoder = SSEDecoder()
    decoder.feed(b"data: " + b"a" * (MAX_LINE_BYTES - 6))  # exactly at the limit: fine
    with pytest.raises(SSELineTooLong):
        decoder.feed(b"a")


def test_line_at_limit_is_accepted() -> None:
    payload = b"a" * (MAX_LINE_BYTES - 6)
    events = decode(b"data: " + payload + b"\n\n")
    assert events == [SSEEvent("message", payload.decode())]


STREAM = (
    b": hello\r\n"
    b'event: message_start\r\ndata: {"a": 1}\r\n\r\n'
    b"data: line one\ndata: line two\n\n"
    b"id: 42\rretry: 300\rdata: \xc3\xa9t\xc3\xa9 \xe2\x9c\x93\r\r"
    b"data: [DONE]\n\n"
)


def _split(stream: bytes, cuts: list[int]) -> list[bytes]:
    points = [0, *sorted(set(cuts)), len(stream)]
    return [stream[a:b] for a, b in zip(points, points[1:], strict=False)]


def test_any_chunking_gives_the_same_events_seeded() -> None:
    whole = decode(STREAM)
    assert len(whole) == 5
    rng = random.Random(20261018)
    for _ in range(300):
        cuts = [rng.randrange(1, len(STREAM)) for _ in range(rng.randrange(1, 12))]
        assert decode(*_split(STREAM, cuts)) == whole
    assert decode(*[STREAM[i : i + 1] for i in range(len(STREAM))]) == whole


def test_any_chunking_gives_the_same_events_hypothesis() -> None:
    hypothesis = pytest.importorskip("hypothesis")
    st = pytest.importorskip("hypothesis.strategies")
    whole = decode(STREAM)

    @hypothesis.given(st.lists(st.integers(min_value=1, max_value=len(STREAM) - 1), max_size=20))
    def check(cuts: list[int]) -> None:
        assert decode(*_split(STREAM, cuts)) == whole

    check()


def test_aiter_sse_over_httpx_response() -> None:
    chunks = [b"data: a\n", b"\ndata: ", b"b\n\n"]

    async def main() -> list[object]:
        async def body() -> AsyncIterator[bytes]:
            for c in chunks:
                yield c

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=body())

        async with (
            httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client,
            client.stream("GET", "https://sse.fake.invalid/") as response,
        ):
            return [item async for item in aiter_sse(response)]

    assert run(main()) == [SSEEvent("message", "a"), SSEEvent("message", "b")]
