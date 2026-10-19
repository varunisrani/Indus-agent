"""Platform markup for agent replies (P15-15).

The agent writes ordinary Markdown. One small renderer turns it into each platform's markup:

- Telegram MarkdownV2 (``to_markdownv2``): every special character outside markup we emit is
  escaped, code is kept verbatim with only ``\\`` and backticks escaped inside it, link URLs
  escape only ``)`` and ``\\``, and tables become a bold heading plus bullets per row. The output
  is valid MarkdownV2 by construction; ``markdownv2_to_plain`` is the once-only fallback for a
  piece Telegram still refuses.
- Slack mrkdwn (``to_mrkdwn``) for normal sends, Block Kit (``slack_blocks``) when the reply holds a
  table, and plain Markdown for native streams (``slack_stream_text``).
- Discord (``to_discord``): native Markdown; tables become aligned code blocks.
- Plain text (``strip_markdown``) for SMS and every fallback.

The sender formats first and chunks after (escaping can push a reply over a limit, R06.11).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from indus.gateway.outbound.chunker import Marker, plain_marker

__all__ = [
    "MARKDOWNV2_SPECIAL",
    "SLACK_MAX_BLOCKS",
    "SLACK_MAX_SECTION_TEXT",
    "SLACK_MAX_TABLE_CHARS",
    "SLACK_MAX_TABLE_COLUMNS",
    "SLACK_MAX_TABLE_ROWS",
    "Formatter",
    "Markup",
    "clamp_embed_title",
    "escape_markdownv2",
    "formatter_for",
    "markdownv2_marker",
    "markdownv2_to_plain",
    "slack_blocks",
    "slack_stream_text",
    "strip_markdown",
    "tables_to_bullets",
    "to_discord",
    "to_markdownv2",
    "to_mrkdwn",
]

type Markup = Literal["markdownv2", "mrkdwn", "markdown", "plain"]

MARKDOWNV2_SPECIAL = frozenset("_*[]()~`>#+-=|{}.!\\")
_MARKDOWN_ESCAPABLE = frozenset("\\`*_{}[]()#+-.!|~<>")

SLACK_MAX_BLOCKS = 50
SLACK_MAX_SECTION_TEXT = 3000
SLACK_MAX_TABLE_ROWS = 100
SLACK_MAX_TABLE_COLUMNS = 20
SLACK_MAX_TABLE_CHARS = 20_000
DISCORD_EMBED_TITLE = 256

_FENCE_LINE = re.compile(r"^\s*```+\s*([^\s`]*)")
_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)+\|?\s*$")
_HEADER = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_QUOTE = re.compile(r"^\s{0,3}>\s?(.*)$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_ORDERED = re.compile(r"^(\s*)(\d{1,9})[.)]\s+(.*)$")
_RULE = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$")
_LEAD_IN_FENCE = re.compile(r"^(.*?\S)\s*```([A-Za-z0-9_+\-]*)\s*$")
_LANG_SAFE = re.compile(r"[^A-Za-z0-9_+\-]")
_SLACK_ENTITY = re.compile(r"<(?:[@#][A-Z0-9][^<>\s|]*(?:\|[^<>]*)?|(?:https?|mailto):[^<>\s]+)>")


# -- shared Markdown reading -----------------------------------------------------------------------


def _split_cells(row: str) -> list[str]:
    body = row.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|") and not body.endswith("\\|"):
        body = body[:-1]
    cells: list[str] = []
    current: list[str] = []
    i = 0
    while i < len(body):
        if body[i] == "\\" and body[i + 1 : i + 2] == "|":
            current.append("|")
            i += 2
            continue
        if body[i] == "|":
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(body[i])
        i += 1
    cells.append("".join(current).strip())
    return cells


@dataclass
class _Table:
    header: list[str]
    rows: list[list[str]]
    lines: list[str]


type _Segment = str | _Table


def _segments(markdown: str) -> list[_Segment]:
    """``markdown`` split into plain line runs and pipe tables (tables inside fences ignored)."""
    lines = markdown.split("\n")
    out: list[_Segment] = []
    plain: list[str] = []
    in_fence = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if _FENCE_LINE.match(line):
            in_fence = not in_fence
        elif (
            not in_fence
            and "|" in line
            and i + 1 < len(lines)
            and _TABLE_SEPARATOR.match(lines[i + 1])
        ):
            end = i + 2
            while end < len(lines) and "|" in lines[end] and lines[end].strip():
                end += 1
            header = _split_cells(line)
            if len(header) >= 2:
                if plain:
                    out.append("\n".join(plain))
                    plain = []
                rows = [_split_cells(row) for row in lines[i + 2 : end]]
                out.append(_Table(header, rows, lines[i:end]))
                i = end
                continue
        plain.append(line)
        i += 1
    if plain or not out:
        out.append("\n".join(plain))
    return out


def _bullet_groups(table: _Table) -> str:
    groups: list[str] = []
    for number, row in enumerate(table.rows, start=1):
        cells = row + [""] * (len(table.header) - len(row))
        heading = cells[0] or f"Row {number}"
        lines = [f"**{heading}**"]
        values = cells[1 : len(table.header)]
        pairs = zip(table.header[1:], values, strict=True)
        lines += [f"• {name}: {value}" for name, value in pairs if value]
        groups.append("\n".join(lines))
    return "\n\n".join(groups)


def tables_to_bullets(markdown: str) -> str:
    """Every pipe table outside fences rewritten as a bold heading plus ``• header: value``
    bullets per row (for markups without tables)."""
    return "\n".join(
        seg if isinstance(seg, str) else _bullet_groups(seg) for seg in _segments(markdown)
    )


def _aligned(table: _Table) -> str:
    rows = [table.header, *table.rows]
    width = max(len(r) for r in rows)
    padded = [r + [""] * (width - len(r)) for r in rows]
    sizes = [max(len(r[c]) for r in padded) for c in range(width)]
    lines = [" | ".join(r[c].ljust(sizes[c]) for c in range(width)).rstrip() for r in padded]
    lines.insert(1, "-+-".join("-" * s for s in sizes))
    return "\n".join(lines)


def _tables_to_code(markdown: str) -> str:
    return "\n".join(
        seg if isinstance(seg, str) else "```\n" + _aligned(seg) + "\n```"
        for seg in _segments(markdown)
    )


# -- the renderer ----------------------------------------------------------------------------------


class _Style:
    """How one markup writes each construct. Subclasses override what differs."""

    def text(self, raw: str) -> str:
        return raw

    def code(self, raw: str) -> str:
        return raw

    def pre(self, lang: str, body: str) -> str:
        return body

    def link(self, label: str, url: str) -> str:
        return label if label == url else f"{label} ({url})"

    def wrap(self, style: str, inner: str) -> str:
        return inner

    def header(self, inner: str) -> str:
        return inner

    def quote(self, inner: str) -> str:
        return inner

    def bullet(self, indent: str, inner: str) -> str:
        return f"{indent}• {inner}"

    def ordered(self, indent: str, number: str, inner: str) -> str:
        return f"{indent}{number}. {inner}"

    def rule(self) -> str:
        return "──────────"

    link_label_formatted = False


class _MarkdownV2(_Style):
    _MARKS = {"bold": "*", "italic": "_", "strike": "~", "spoiler": "||"}

    def text(self, raw: str) -> str:
        return escape_markdownv2(raw)

    def code(self, raw: str) -> str:
        return "`" + _escape_code(raw) + "`"

    def pre(self, lang: str, body: str) -> str:
        return "```" + lang + "\n" + _escape_code(body) + "\n```"

    def link(self, label: str, url: str) -> str:
        return f"[{label}]({url.replace(chr(92), chr(92) * 2).replace(')', chr(92) + ')')})"

    def wrap(self, style: str, inner: str) -> str:
        mark = self._MARKS[style]
        return mark + inner + mark

    def header(self, inner: str) -> str:
        return f"*{inner}*" if inner else ""

    def quote(self, inner: str) -> str:
        return ">" + inner

    def ordered(self, indent: str, number: str, inner: str) -> str:
        return f"{indent}{number}\\. {inner}"

    link_label_formatted = True


class _Mrkdwn(_Style):
    _MARKS = {"bold": "*", "italic": "_", "strike": "~", "spoiler": ""}

    def text(self, raw: str) -> str:
        return _slack_escape(raw)

    def code(self, raw: str) -> str:
        return "`" + _slack_escape(raw) + "`"

    def pre(self, lang: str, body: str) -> str:
        return "```\n" + _slack_escape(body) + "\n```"

    def link(self, label: str, url: str) -> str:
        safe_url = url.replace("|", "%7C").replace(">", "%3E").replace("<", "%3C")
        return f"<{safe_url}|{label}>" if label else f"<{safe_url}>"

    def wrap(self, style: str, inner: str) -> str:
        mark = self._MARKS[style]
        if style == "bold" and inner and not inner[-1].isalnum():
            inner += "​"  # Slack misses a closing * that follows punctuation
        return mark + inner + mark

    def header(self, inner: str) -> str:
        return f"*{inner}*" if inner else ""

    def quote(self, inner: str) -> str:
        return "> " + inner


_PLAIN = _Style()
_MDV2 = _MarkdownV2()
_MRKDWN = _Mrkdwn()

# delimiter, style, single-character (needs flanking checks)
_EMPHASIS = (
    ("**", "bold", False),
    ("__", "bold", False),
    ("~~", "strike", False),
    ("||", "spoiler", False),
    ("*", "italic", True),
    ("_", "italic", True),
)


def _escape_code(raw: str) -> str:
    return raw.replace("\\", "\\\\").replace("`", "\\`")


def escape_markdownv2(text: str) -> str:
    """``text`` with every MarkdownV2 special character backslash-escaped."""
    return "".join("\\" + c if c in MARKDOWNV2_SPECIAL else c for c in text)


def _slack_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _backtick_run(text: str, at: int) -> int:
    end = at
    while end < len(text) and text[end] == "`":
        end += 1
    return end - at


def _code_close(text: str, start: int, run: int) -> int:
    """Index of the backtick run of length ``run`` closing a span opened before ``start``."""
    i = start
    while i < len(text):
        if text[i] == "`":
            length = _backtick_run(text, i)
            if length == run:
                return i
            i += length
            continue
        i += 1
    return -1


def _find_close(text: str, start: int, delim: str, single: bool) -> int:
    i = start
    while i < len(text):
        char = text[i]
        if char == "\\":
            i += 2
            continue
        if char == "`":
            run = _backtick_run(text, i)
            close = _code_close(text, i + run, run)
            i = close + run if close >= 0 else i + run
            continue
        if text.startswith(delim, i):
            if single:
                doubled = text[i + 1 : i + 2] == delim or (i > start and text[i - 1] == delim)
                flanked = not text[i - 1].isspace()
                word_after = delim == "_" and text[i + 1 : i + 2].isalnum()
                if not doubled and flanked and not word_after:
                    return i
            else:
                return i
        i += 1
    return -1


def _match_link(text: str, at: int) -> tuple[str, str, int] | None:
    """``[label](url)`` starting at ``at``: label, url, end offset; None when it is not one."""
    depth = 0
    i = at
    while i < len(text):
        char = text[i]
        if char == "\\":
            i += 2
            continue
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                break
        i += 1
    else:
        return None
    label = text[at + 1 : i]
    if not label.strip() or text[i + 1 : i + 2] != "(":
        return None
    depth = 0
    j = i + 1
    while j < len(text):
        char = text[j]
        if char.isspace():
            return None
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                url = text[i + 2 : j]
                return (label, url, j + 1) if url else None
        j += 1
    return None


def _inline(text: str, style: _Style, active: frozenset[str] = frozenset()) -> str:
    out: list[str] = []
    i = 0
    while i < len(text):
        char = text[i]
        if char == "\\" and text[i + 1 : i + 2] in _MARKDOWN_ESCAPABLE and i + 1 < len(text):
            out.append(style.text(text[i + 1]))
            i += 2
            continue
        if char == "`":
            run = _backtick_run(text, i)
            close = _code_close(text, i + run, run)
            if close > i + run:
                inner = text[i + run : close]
                if run > 1 and inner.startswith(" ") and inner.endswith(" "):
                    inner = inner[1:-1]
                out.append(style.code(inner))
                i = close + run
            else:
                out.append(style.text("`" * run))
                i += run
            continue
        if char == "[" and "link" not in active:
            link = _match_link(text, i)
            if link is not None:
                label, url, end = link
                if style.link_label_formatted:
                    shown = _inline(label, style, active | {"link"})
                else:
                    shown = _inline(
                        label, _PLAIN if style is not _MRKDWN else _SlackLabel(), active
                    )
                out.append(style.link(shown, url))
                i = end
                continue
        matched = False
        for delim, name, single in _EMPHASIS:
            if not text.startswith(delim, i):
                continue
            start = i + len(delim)
            if single:
                if start >= len(text) or text[start].isspace():
                    continue
                if delim == "_" and i > 0 and text[i - 1].isalnum():
                    continue
            close = _find_close(text, start, delim, single)
            if close > start:
                inner = _inline(text[start:close], style, active | {name})
                # the same style inside itself (bold in a header) is flattened, never nested
                wrapped = inner if name in active else style.wrap(name, inner)
                if out and wrapped and out[-1].endswith(wrapped[0]) and wrapped[0] in "*_~|":
                    out.append("​")  # `_a__b_` would read as underline: keep entities apart
                out.append(wrapped)
                i = close + len(delim)
                matched = True
                break
        if matched:
            continue
        out.append(style.text(char))
        i += 1
    return "".join(out)


class _SlackLabel(_Style):
    def text(self, raw: str) -> str:
        return _slack_escape(raw).replace("|", "¦")


def _render(markdown: str, style: _Style) -> str:
    lines = markdown.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        fence = _FENCE_LINE.match(line)
        lead_in = None if fence else _LEAD_IN_FENCE.match(line)
        if lead_in and ("```" in lead_in.group(1) or lead_in.group(1).count("`") % 2):
            lead_in = None  # the backticks close an inline span opened earlier on the line
        if lead_in:
            out.append(_inline(lead_in.group(1), style))
            fence = lead_in
        if fence:
            lang = _LANG_SAFE.sub("", fence.group(fence.lastindex or 1))
            end = i + 1
            while end < len(lines) and not _FENCE_LINE.match(lines[end]):
                end += 1
            out.append(style.pre(lang, "\n".join(lines[i + 1 : end])))
            i = end + 1
            continue
        if _RULE.match(line):
            out.append(style.rule())
        elif match := _HEADER.match(line):
            out.append(style.header(_inline(match.group(1), style, frozenset({"bold"}))))
        elif match := _QUOTE.match(line):
            out.append(style.quote(_inline(match.group(1), style)))
        elif match := _BULLET.match(line):
            out.append(style.bullet(match.group(1), _inline(match.group(2), style)))
        elif match := _ORDERED.match(line):
            out.append(
                style.ordered(match.group(1), match.group(2), _inline(match.group(3), style))
            )
        else:
            out.append(_inline(line, style))
        i += 1
    return "\n".join(out)


# -- Telegram --------------------------------------------------------------------------------------


def to_markdownv2(markdown: str) -> str:
    """Telegram MarkdownV2 for ``markdown`` (valid by construction)."""
    if not markdown:
        return markdown
    return _render(tables_to_bullets(markdown), _MDV2)


def markdownv2_marker(index: int, total: int) -> str:
    return escape_markdownv2(plain_marker(index, total))


def markdownv2_to_plain(text: str) -> str:
    """The plain-text form of a MarkdownV2 piece: escapes undone, entity markers removed,
    links shown as ``label (url)``."""
    out: list[str] = []
    i = 0
    at_line_start = True
    while i < len(text):
        char = text[i]
        if char == "\\" and i + 1 < len(text):
            out.append(text[i + 1])
            i += 2
            at_line_start = False
            continue
        if text.startswith("```", i):
            newline = text.find("\n", i)
            close = text.find("```", newline if newline >= 0 else i + 3)
            body_start = newline + 1 if newline >= 0 else i + 3
            body_end = close if close >= 0 else len(text)
            out.append(_unescape(text[body_start:body_end]).rstrip("\n"))
            i = body_end + 3 if close >= 0 else len(text)
            continue
        if char == "`":
            close = _unescaped(text, i + 1, "`")
            end = close if close >= 0 else len(text)
            out.append(_unescape(text[i + 1 : end]))
            i = end + 1
            continue
        if char == "[":
            close = _unescaped(text, i + 1, "]")
            if close >= 0 and text[close + 1 : close + 2] == "(":
                url_end = _unescaped(text, close + 2, ")")
                if url_end >= 0:
                    label = markdownv2_to_plain(text[i + 1 : close])
                    url = _unescape(text[close + 2 : url_end])
                    out.append(label if label == url else f"{label} ({url})")
                    i = url_end + 1
                    continue
        if char == ">" and at_line_start:
            i += 1
            continue
        if text.startswith("||", i):
            i += 2
            continue
        if char in "*_~":
            i += 1
            continue
        out.append(char)
        at_line_start = char == "\n"
        i += 1
    return "".join(out)


def _unescaped(text: str, start: int, target: str) -> int:
    i = start
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == target:
            return i
        i += 1
    return -1


def _unescape(text: str) -> str:
    return re.sub(r"\\(.)", r"\1", text, flags=re.DOTALL)


# -- Slack -----------------------------------------------------------------------------------------


def _protect_slack_entities(markdown: str) -> tuple[str, dict[str, str]]:
    stash: dict[str, str] = {}

    def keep(match: re.Match[str]) -> str:
        key = f"\x00{len(stash)}\x00"
        stash[key] = match.group(0)
        return key

    return _SLACK_ENTITY.sub(keep, markdown), stash


def to_mrkdwn(markdown: str) -> str:
    """Slack mrkdwn: ``*bold*``, ``_italic_``, ``~strike~``, ``<url|label>``, ``&<>`` escaped,
    user/channel mentions and links in Slack's own ``<…>`` form kept, broadcasts defused, tables
    as code blocks."""
    if not markdown:
        return markdown
    protected, stash = _protect_slack_entities(_tables_to_code(markdown))
    rendered = _render(protected, _MRKDWN)
    for key, value in stash.items():
        rendered = rendered.replace(key, value)
    return rendered


def slack_stream_text(markdown: str) -> str:
    """Native Slack streams take standard Markdown unchanged."""
    return markdown


def _section_texts(text: str) -> list[str]:
    pieces: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > SLACK_MAX_SECTION_TEXT:
            if current:
                pieces.append(current)
                current = ""
            pieces.append(line[:SLACK_MAX_SECTION_TEXT])
            line = line[SLACK_MAX_SECTION_TEXT:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > SLACK_MAX_SECTION_TEXT:
            pieces.append(current)
            candidate = line
        current = candidate
    if current.strip():
        pieces.append(current)
    return [p for p in pieces if p.strip()]


def _section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _table_block(table: _Table) -> dict[str, Any] | None:
    rows = [table.header, *table.rows]
    width = max(len(r) for r in rows)
    if len(rows) > SLACK_MAX_TABLE_ROWS or width > SLACK_MAX_TABLE_COLUMNS:
        return None
    padded = [r + [""] * (width - len(r)) for r in rows]
    if sum(len(cell) for row in padded for cell in row) > SLACK_MAX_TABLE_CHARS:
        return None
    return {
        "type": "table",
        "rows": [[{"type": "raw_text", "text": cell or " "} for cell in row] for row in padded],
    }


def slack_blocks(markdown: str) -> list[dict[str, Any]] | None:
    """Block Kit blocks for a reply that holds a table, or None (no table, or more than
    ``SLACK_MAX_BLOCKS`` blocks: the caller sends ``to_mrkdwn`` text instead). One table becomes a
    native ``table`` block when it fits the limits; any other table is a code block."""
    segments = _segments(markdown)
    if not any(isinstance(seg, _Table) for seg in segments):
        return None
    blocks: list[dict[str, Any]] = []
    table_used = False
    for seg in segments:
        if isinstance(seg, _Table):
            block = None if table_used else _table_block(seg)
            if block is not None:
                blocks.append(block)
                table_used = True
                continue
            seg = "```\n" + _aligned(seg) + "\n```"
        rendered = to_mrkdwn(seg) if seg.strip() else ""
        blocks.extend(_section(text) for text in _section_texts(rendered))
    return blocks if len(blocks) <= SLACK_MAX_BLOCKS else None


# -- Discord and plain -----------------------------------------------------------------------------


def to_discord(markdown: str) -> str:
    """Discord renders Markdown natively; tables become aligned code blocks."""
    return _tables_to_code(markdown) if markdown else markdown


def clamp_embed_title(text: str, limit: int = DISCORD_EMBED_TITLE) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def strip_markdown(markdown: str) -> str:
    """Plain text: markers removed, links as ``label (url)``, code kept, tables as bullets."""
    if not markdown:
        return markdown
    return _render(tables_to_bullets(markdown), _PLAIN)


def _mrkdwn_to_plain(text: str) -> str:
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


# -- per-markup bundle -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Formatter:
    """What the sender needs for one markup: the converter, the plain fallback of an already
    formatted piece, and the chunk marker in that markup."""

    markup: Markup
    format: Callable[[str], str]
    plain: Callable[[str], str]
    marker: Marker


def _identity(text: str) -> str:
    return text


_FORMATTERS: dict[str, Formatter] = {
    "markdownv2": Formatter("markdownv2", to_markdownv2, markdownv2_to_plain, markdownv2_marker),
    "mrkdwn": Formatter("mrkdwn", to_mrkdwn, _mrkdwn_to_plain, plain_marker),
    "markdown": Formatter("markdown", to_discord, _identity, plain_marker),
    "plain": Formatter("plain", strip_markdown, _identity, plain_marker),
}


def formatter_for(markup: str) -> Formatter:
    """The formatter for ``markup``; unknown names get plain text."""
    return _FORMATTERS.get(markup, _FORMATTERS["plain"])
