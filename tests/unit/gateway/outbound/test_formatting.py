"""P15-15: platform markup (golden cases plus a MarkdownV2 validator written for these tests)."""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from indus.gateway.outbound.chunker import split_message, utf16_units
from indus.gateway.outbound.formatting import (
    MARKDOWNV2_SPECIAL,
    SLACK_MAX_BLOCKS,
    clamp_embed_title,
    escape_markdownv2,
    formatter_for,
    markdownv2_marker,
    markdownv2_to_plain,
    slack_blocks,
    slack_stream_text,
    strip_markdown,
    tables_to_bullets,
    to_discord,
    to_markdownv2,
    to_mrkdwn,
)

CASES = Path(__file__).resolve().parents[3] / "fixtures" / "gateway" / "markdownv2_cases.json"


# -- a MarkdownV2 validator (Telegram Bot API formatting rules) ------------------------------------


def _scan_until(text: str, i: int, closer: str) -> int:
    """Index just past the unescaped ``closer`` starting at ``i``; raises when there is none or
    (inside pre) a lone backtick is unescaped."""
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text.startswith(closer, i):
            return i + len(closer)
        if closer == "```" and text[i] == "`":
            raise ValueError(f"unescaped backtick inside pre at {i}")
        i += 1
    raise ValueError(f"unclosed {closer!r}")


def mdv2_errors(text: str) -> str | None:
    """None when ``text`` parses as MarkdownV2, else the first problem found."""
    stack: list[str] = []
    i = 0
    try:
        while i < len(text):
            char = text[i]
            line_start = i == 0 or text[i - 1] == "\n"
            if char == "\\":
                if i + 1 >= len(text):
                    return "dangling backslash"
                i += 2
                continue
            if text.startswith("```", i):
                i = _scan_until(text, i + 3, "```")
                continue
            if char == "`":
                i = _scan_until(text, i + 1, "`")
                continue
            if char == "[":
                stack.append("link")
                i += 1
                continue
            if char == "]":
                if not stack or stack[-1] != "link" or text[i + 1 : i + 2] != "(":
                    return f"stray ] at {i}"
                stack.pop()
                end = i + 2
                while True:
                    if end >= len(text):
                        return "unclosed link url"
                    if text[end] == "\\":
                        end += 2
                        continue
                    if text[end] == ")":
                        break
                    end += 1
                i = end + 1
                continue
            if char == ">" and line_start:
                i += 1
                continue
            for marker, name in (
                ("||", "spoiler"),
                ("__", "underline"),
                ("*", "bold"),
                ("_", "italic"),
                ("~", "strike"),
            ):
                if text.startswith(marker, i):
                    if name in stack:
                        if stack[-1] != name:
                            return f"crossed {name} at {i}"
                        stack.pop()
                    else:
                        stack.append(name)
                    i += len(marker)
                    break
            else:
                if char in MARKDOWNV2_SPECIAL:
                    return f"unescaped {char!r} at {i}"
                i += 1
    except ValueError as exc:
        return str(exc)
    return None if not stack else f"unclosed {stack}"


def test_the_validator_rejects_broken_markdownv2() -> None:
    assert mdv2_errors("a.b") is not None
    assert mdv2_errors("*bold") is not None
    assert mdv2_errors("*a _b* c_") is not None
    assert mdv2_errors("```\na ` b\n```") is not None
    assert mdv2_errors("[x](a)b)") is not None
    assert mdv2_errors("*bold* _it_ `c` [l](u\\)) a\\.b") is None


# -- golden and property tests --------------------------------------------------------------------


def _cases() -> list[dict[str, str]]:
    return json.loads(CASES.read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", _cases(), ids=lambda c: c["name"])
def test_golden_markdownv2_cases(case: dict[str, str]) -> None:
    rendered = to_markdownv2(case["markdown"])
    assert rendered == case["markdownv2"]
    assert mdv2_errors(rendered) is None
    assert strip_markdown(case["markdown"]) == case["plain"]


def test_every_special_character_is_escaped() -> None:
    every = "".join(sorted(MARKDOWNV2_SPECIAL))
    escaped = escape_markdownv2(every)
    assert escaped == "".join("\\" + c for c in every)
    assert mdv2_errors(escaped) is None


TOKENS = [
    "word",
    "x.y",
    "a-b",
    "1+1=2",
    "(paren)",
    "{brace}",
    "#tag",
    "50%!",
    "a|b",
    "C:\\dir",
    "snake_case",
    "2*3",
    "~",
    "**bold words**",
    "*it*",
    "_it_",
    "~~gone~~",
    "||hide||",
    "`code`",
    "`a\\b`",
    "[label](https://e.com/a_(b))",
    "[x]",
    "> not quote",
    "🙂",
    "数据",
    "**unclosed",
    "_dangling",
    "``",
    "![img](https://e.com/i.png)",
]


def _random_markdown(rng: random.Random) -> str:
    lines: list[str] = []
    for _ in range(rng.randint(1, 12)):
        roll = rng.random()
        words = " ".join(rng.choice(TOKENS) for _ in range(rng.randint(1, 10)))
        if roll < 0.1:
            lines.append("```" + rng.choice(["", "py"]) + "\n" + words + " ` \\ ``\n```")
        elif roll < 0.18:
            lines.append("| h1 | h2 |\n|---|---|\n| " + words + " | v |")
        elif roll < 0.25:
            lines.append("# " + words)
        elif roll < 0.32:
            lines.append("> " + words)
        elif roll < 0.4:
            lines.append("- " + words)
        elif roll < 0.45:
            lines.append("3. " + words)
        else:
            lines.append(words)
    return "\n".join(lines)


def test_markdownv2_output_always_parses() -> None:
    for seed in range(400):
        markdown = _random_markdown(random.Random(seed))
        rendered = to_markdownv2(markdown)
        assert mdv2_errors(rendered) is None, (markdown, rendered, mdv2_errors(rendered))


def test_chunks_of_markdownv2_output_parse() -> None:
    simple = ["word", "x.y", "1+1=2", "*it*", "**bold**", "`code`", "[l](https://e.com)", "🙂"]
    for seed in range(120):
        rng = random.Random(seed)
        markdown = "\n".join(
            " ".join(rng.choice(simple) for _ in range(rng.randint(1, 25)))
            for _ in range(rng.randint(1, 10))
        )
        if rng.random() < 0.3:
            markdown += "\n```py\n" + "\n".join("print('a.b')" for _ in range(30)) + "\n```"
        rendered = to_markdownv2(markdown)
        pieces = split_message(rendered, 120, len_fn=utf16_units, marker=markdownv2_marker)
        for piece in pieces.texts:
            assert utf16_units(piece) <= 120
            assert mdv2_errors(piece) is None, (piece, mdv2_errors(piece))


def test_escaping_can_push_a_reply_into_two_pieces() -> None:
    markdown = "a." * 2000  # 4,000 units raw, 6,000 escaped
    assert len(split_message(markdown, 4096).texts) == 1
    assert len(split_message(to_markdownv2(markdown), 4096, marker=markdownv2_marker).texts) == 2


def test_plain_fallback_strips_markers_and_escapes() -> None:
    rendered = to_markdownv2("**Done** with _x_: see [docs](https://e.com/a) and `a.b`. 1+1=2!")
    assert markdownv2_to_plain(rendered) == (
        "Done with x: see docs (https://e.com/a) and a.b. 1+1=2!"
    )
    assert markdownv2_to_plain(to_markdownv2("```py\nx = 'a\\\\b'\n```")) == "x = 'a\\\\b'"
    formatter = formatter_for("markdownv2")
    assert formatter.plain(formatter.format("*it*")) == "it"


def test_tables_become_bullets_outside_fences_only() -> None:
    table = "| Name | Age |\n|---|---|\n| Ann | 30 |"
    assert tables_to_bullets(table) == "**Ann**\n• Age: 30"
    fenced = "```\n" + table + "\n```"
    assert tables_to_bullets(fenced) == fenced
    assert tables_to_bullets("a | b\nnot a table") == "a | b\nnot a table"


# -- Slack ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("markdown", "mrkdwn"),
    [
        ("**bold** and *it* and ~~x~~", "*bold* and _it_ and ~x~"),
        ("**done!**", "*done!\u200b*"),
        ("[docs](https://e.com/a?b=1|2)", "<https://e.com/a?b=1%7C2|docs>"),
        ("a < b & c > d", "a &lt; b &amp; c &gt; d"),
        ("hey <!channel> and <!here>", "hey &lt;!channel&gt; and &lt;!here&gt;"),
        ("ping <@U123ABC> in <#C42|general>", "ping <@U123ABC> in <#C42|general>"),
        ("# Head", "*Head*"),
        ("```python\nif a<b: pass\n```", "```\nif a&lt;b: pass\n```"),
        ("- one\n- two", "• one\n• two"),
    ],
)
def test_mrkdwn_conversion(markdown: str, mrkdwn: str) -> None:
    assert to_mrkdwn(markdown) == mrkdwn


def test_slack_table_becomes_a_table_block() -> None:
    blocks = slack_blocks("Intro\n\n| a | b |\n|---|---|\n| 1 | |\n\nOutro **x**")
    assert blocks is not None
    kinds = [b["type"] for b in blocks]
    assert kinds == ["section", "table", "section"]
    table = blocks[1]
    assert table["rows"][0] == [
        {"type": "raw_text", "text": "a"},
        {"type": "raw_text", "text": "b"},
    ]
    assert table["rows"][1][1]["text"] == " "
    assert blocks[2]["text"]["text"] == "Outro *x*"
    assert slack_blocks("no table here") is None


def _table(rows: int, cols: int, cell: str = "v") -> str:
    header = "| " + " | ".join(f"h{c}" for c in range(cols)) + " |"
    sep = "|" + "---|" * cols
    body = ["| " + " | ".join(cell for _ in range(cols)) + " |" for _ in range(rows)]
    return "\n".join([header, sep, *body])


def test_slack_table_limits_fall_back_to_code() -> None:
    fits = slack_blocks(_table(99, 20))
    assert fits is not None and fits[0]["type"] == "table"
    for markdown in (_table(100, 2), _table(3, 21), _table(10, 10, "x" * 201)):
        blocks = slack_blocks(markdown)
        assert blocks is not None
        assert all(b["type"] == "section" for b in blocks)
        assert blocks[0]["text"]["text"].startswith("```")
    second = slack_blocks(_table(2, 2) + "\n\n" + _table(2, 2))
    assert second is not None
    assert [b["type"] for b in second] == ["table", "section"]


def test_slack_block_cap_returns_none() -> None:
    many = "\n\n".join(["x" * 2999] * (SLACK_MAX_BLOCKS + 1)) + "\n\n" + _table(1, 2)
    assert slack_blocks(many) is None


def test_slack_streams_take_standard_markdown() -> None:
    assert slack_stream_text("**x** [a](b)") == "**x** [a](b)"


# -- Discord and plain ----------------------------------------------------------------------------


def test_discord_keeps_markdown_and_boxes_tables() -> None:
    assert to_discord("**x** _y_ `z`") == "**x** _y_ `z`"
    assert to_discord("| a | b |\n|---|---|\n| 1 | 2 |") == "```\na | b\n--+--\n1 | 2\n```"
    assert clamp_embed_title("t" * 300) == "t" * 255 + "…"
    assert clamp_embed_title("short") == "short"


def test_plain_strip() -> None:
    assert strip_markdown("# T\n**b** *i* [l](https://u) `c`\n- x") == "T\nb i l (https://u) c\n• x"
    assert formatter_for("unknown").markup == "plain"
