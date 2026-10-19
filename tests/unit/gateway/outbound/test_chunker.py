"""P15-14: chunking (seeded-random property tables; ``hypothesis`` is not installed)."""

from __future__ import annotations

import random

import pytest

from indus.gateway.outbound.chunker import (
    BARE_LIMIT,
    Chunked,
    LenFn,
    close_fences,
    code_points,
    fence_state,
    plain_marker,
    prefix_within,
    split_message,
    utf16_units,
)
from indus.gateway.outbound.chunker import _open_inline_tick as open_inline_tick

LEN_FNS: list[LenFn] = [code_points, utf16_units]
WORDS = ["alpha", "beta", "gamma", "δέλτα", "数据", "x", "longerwordwithoutspaces", "🙂", "𝔘𝔫𝔦"]


def _paragraph(rng: random.Random) -> str:
    parts: list[str] = []
    for _ in range(rng.randint(1, 30)):
        roll = rng.random()
        if roll < 0.08:
            parts.append(f"`{rng.choice(WORDS)} {rng.choice(WORDS)}`")
        elif roll < 0.12:
            parts.append("\\" + rng.choice("._*"))
        else:
            parts.append(rng.choice(WORDS))
    return " ".join(parts)


def _fence(rng: random.Random) -> str:
    lang = rng.choice(["py", "js", ""])
    lines = [
        "    " * rng.randint(0, 2) + " ".join(rng.choice(WORDS) for _ in range(rng.randint(1, 9)))
        for _ in range(rng.randint(1, 12))
    ]
    return "```" + lang + "\n" + "\n".join(lines) + "\n```"


def _document(rng: random.Random) -> str:
    blocks = [
        _fence(rng) if rng.random() < 0.35 else _paragraph(rng) for _ in range(rng.randint(1, 8))
    ]
    return rng.choice(["\n", "\n\n"]).join(blocks)


def _strip_decoration(chunked: Chunked, index: int) -> tuple[str, str | None]:
    """The source text a piece carries, rebuilt by removing marker, closer and reopen prefix."""
    chunk = chunked.chunks[index]
    text = chunk.text
    total = len(chunked.chunks)
    if total > 1:
        marker = plain_marker(index + 1, total)
        assert text.endswith(marker)
        text = text[: -len(marker) - 1]
    carry = fence_state(chunked.source[: chunk.start])
    if carry is not None:
        prefix = f"```{carry}\n"
        assert text.startswith(prefix), (prefix, text[:20])
        text = text[len(prefix) :]
    if fence_state(chunked.source[chunk.start : chunk.end], carry) is not None:
        assert text.endswith("\n```")
        text = text[: -len("\n```")]
    return text, carry


def _check(chunked: Chunked, limit: int, len_fn: LenFn) -> None:
    source = chunked.source
    assert chunked.chunks[0].start == 0
    assert chunked.chunks[-1].end == len(source)
    for i, chunk in enumerate(chunked.chunks):
        assert len_fn(chunk.text) <= limit, (limit, chunk.text)
        body, _ = _strip_decoration(chunked, i)
        assert body == source[chunk.start : chunk.end]
        if i:
            gap = source[chunked.chunks[i - 1].end : chunk.start]
            assert gap.strip() == "", repr(gap)
        unmarked = chunk.text
        if len(chunked.chunks) > 1:
            unmarked = unmarked[: -len(plain_marker(i + 1, len(chunked.chunks))) - 1]
        assert fence_state(unmarked) is None, "every piece closes its own fences"
        if _longest_inline_span(source) <= limit // 4:
            # a span longer than a quarter of a piece cannot always be kept whole
            assert open_inline_tick(unmarked, None) == -1, "no unpaired backtick"


def _longest_inline_span(source: str) -> int:
    longest = 0
    for line in source.split("\n"):
        if line.strip().startswith("```"):
            continue
        parts = line.split("`")
        for k in range(1, len(parts), 2):
            longest = max(longest, len(parts[k]) + 2)
    return longest


@pytest.mark.parametrize("len_fn", LEN_FNS, ids=["code_points", "utf16"])
def test_pieces_fit_the_limit_under_both_length_functions(len_fn: LenFn) -> None:
    for seed in range(250):
        rng = random.Random(seed)
        text = _document(rng)
        limit = rng.choice([BARE_LIMIT, 40, 64, 100, 160, 400])
        chunked = split_message(text, limit, len_fn=len_fn)
        _check(chunked, limit, len_fn)


@pytest.mark.parametrize("len_fn", LEN_FNS, ids=["code_points", "utf16"])
def test_tiny_limits_terminate(len_fn: LenFn) -> None:
    for seed in range(60):
        text = _document(random.Random(seed))
        for limit in range(1, 21):
            chunked = split_message(text, limit, len_fn=len_fn)
            assert "".join(chunked.texts) == text
            for chunk in chunked.chunks:
                assert len_fn(chunk.text) <= limit or len(chunk.text) == 1


def test_astral_characters_are_never_split() -> None:
    text = "🙂" * 5000
    for limit in (1, 2, 3, 33, 4096):
        chunked = split_message(text, limit, len_fn=utf16_units)
        for chunk in chunked.chunks:
            body = chunk.text.split(" (")[0]
            assert set(body) <= {"🙂"}
            assert body.encode("utf-16-le").decode("utf-16-le") == body
        total_emoji = sum(len(c.text.split(" (")[0]) for c in chunked.chunks)
        assert total_emoji == 5000


def test_utf16_counts_astral_characters_twice() -> None:
    assert utf16_units("a🙂") == 3
    assert code_points("a🙂") == 2
    assert prefix_within("🙂🙂🙂", 3, utf16_units) == 1
    assert prefix_within("🙂🙂🙂", 4, utf16_units) == 2
    # 4,096 UTF-16 units of emoji are 2,048 emoji: one Telegram message holds no more.
    pieces = split_message("🙂" * 2049, 4096, len_fn=utf16_units)
    assert len(pieces.chunks) == 2
    assert all(utf16_units(t) <= 4096 for t in pieces.texts)


def test_text_that_fits_is_one_piece() -> None:
    assert split_message("hello", 5).texts == ["hello"]
    assert split_message("", 5).texts == [""]
    with pytest.raises(ValueError):
        split_message("x", 0)


def test_cut_prefers_newline_then_space() -> None:
    text = "a" * 30 + "\n" + "b" * 30 + " " + "c" * 30
    pieces = split_message(text, 50).texts
    assert pieces[0] == "a" * 30 + " (1/3)"
    assert pieces[1] == "b" * 30 + " (2/3)"
    assert pieces[2] == "c" * 30 + " (3/3)"
    early_newline = "a\n" + "b" * 25 + " " + "c" * 25
    first = split_message(early_newline, 45).texts[0]
    assert first == "a\n" + "b" * 25 + " (1/2)"


def test_fence_is_closed_and_reopened_with_its_language() -> None:
    code = "\n".join(f"    line {i} = value_{i}" for i in range(40))
    text = "Intro text.\n```python\n" + code + "\n```\nOutro."
    chunked = split_message(text, 200)
    texts = chunked.texts
    assert len(texts) > 2
    assert texts[0].rstrip().endswith("```\n" + plain_marker(1, len(texts)))
    assert texts[1].startswith("```python\n    line")
    # indentation inside the reopened fence survives
    assert "\n    line" in texts[1]
    _check(chunked, 200, code_points)


def test_marker_after_a_closer_sits_on_its_own_line() -> None:
    text = "```\n" + "\n".join("x" * 20 for _ in range(10)) + "\n```"
    first = split_message(text, 80).texts[0]
    assert first.endswith("\n```\n(1/" + first.rsplit("/", 1)[1])


def test_cut_backs_off_before_an_open_inline_code_span() -> None:
    text = "word " * 12 + "`some code that is long` tail " * 3
    chunked = split_message(text, 80)
    for i in range(len(chunked.chunks)):
        body, _ = _strip_decoration(chunked, i)
        assert body.count("`") % 2 == 0


def test_backslash_escape_is_not_separated_from_its_character() -> None:
    text = "\\." * 200
    for chunk in split_message(text, 41).chunks:
        body = chunk.text.split(" (")[0]
        assert body.count("\\") == body.count(".")


def test_remainder_resumes_from_the_first_undelivered_piece() -> None:
    text = " ".join(f"word{i}" for i in range(200))
    chunked = split_message(text, 100)
    assert chunked.remainder(0) == text
    assert chunked.remainder(len(chunked.chunks)) == ""
    rest = chunked.remainder(2)
    assert text.endswith(rest)
    assert rest.startswith(chunked.source[chunked.chunks[2].start : chunked.chunks[2].end])


def test_many_pieces_widen_the_marker_reserve() -> None:
    text = " ".join("w" * 30 for _ in range(400))
    chunked = split_message(text, 40)
    assert len(chunked.chunks) > 99
    assert all(len(t) <= 40 for t in chunked.texts)
    assert chunked.texts[-1].endswith(f"({len(chunked.chunks)}/{len(chunked.chunks)})")


def test_custom_marker_is_used() -> None:
    texts = split_message("a " * 100, 60, marker=lambda i, n: f"\\({i}/{n}\\)").texts
    assert texts[0].endswith(" \\(1/" + texts[0].rsplit("/", 1)[1])


def test_close_fences_for_interim_frames() -> None:
    assert close_fences("text") == "text"
    assert close_fences("```py\nx = 1") == "```py\nx = 1\n```"
    assert close_fences("see `code") == "see `code`"
