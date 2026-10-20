"""Markdown → WhatsApp text markup (P16-04).

WhatsApp renders ``*bold*``, ``_italic_``, ``~strike~``, ``` ``` ``` blocks and `` `code` ``
spans, but not Markdown's doubled markers, headings or links. ``to_whatsapp`` rewrites one piece
of agent Markdown (already cut by the chunker, so its fences are balanced):

- fenced blocks and inline code spans pass through untouched;
- ``# Heading`` lines become a bold line (bold markers inside the heading are dropped);
- ``**x**`` / ``__x__`` → ``*x*``; ``*x*`` → ``_x_``; ``_x_`` stays; ``~~x~~`` → ``~x~``;
- ``[label](url)`` → ``label (url)`` (just the URL when the label is the URL).

Bullets written ``* item`` are left alone (a marker followed by a space never opens emphasis).
The rewrite never makes a piece longer, so the chunker's cut still fits after it.
"""

from __future__ import annotations

import re

__all__ = ["to_whatsapp"]

_FENCE = re.compile(r"^\s*```")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")

# One alternation, scanned left to right; the first group that matches names the construct.
_INLINE = re.compile(
    r"(?P<code>`[^`\n]+`)"
    r"|\[(?P<label>[^\]\n]+)\]\((?P<url>[^)\s]+)\)"
    r"|\*\*(?P<bold>\S(?:.*?\S)?)\*\*"
    r"|__(?P<bold2>\S(?:.*?\S)?)__"
    r"|~~(?P<strike>\S(?:.*?\S)?)~~"
    r"|(?<![\w*])\*(?P<italic>[^\s*](?:[^*\n]*?[^\s*])?)\*(?![\w*])"
)


def _inline(text: str) -> str:
    def swap(match: re.Match[str]) -> str:
        if match.group("code") is not None:
            return match.group("code")
        if match.group("label") is not None:
            label, url = match.group("label"), match.group("url")
            return url if label.strip() == url else f"{label} ({url})"
        for name in ("bold", "bold2"):
            inner = match.group(name)
            if inner is not None:
                return f"*{_inline(inner).strip('*')}*"
        if match.group("strike") is not None:
            return f"~{_inline(match.group('strike'))}~"
        return f"_{match.group('italic')}_"

    return _INLINE.sub(swap, text)


def _heading(text: str) -> str:
    inner = _inline(text).strip()
    while len(inner) > 1 and inner[0] == "*" and inner[-1] == "*":
        inner = inner[1:-1].strip()
    return f"*{inner}*" if inner else ""


def to_whatsapp(markdown: str) -> str:
    """``markdown`` in WhatsApp's markup (see the module notes)."""
    if not markdown:
        return markdown
    out: list[str] = []
    in_fence = False
    for line in markdown.split("\n"):
        if _FENCE.match(line):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        heading = _HEADING.match(line)
        out.append(_heading(heading.group(1)) if heading else _inline(line))
    return "\n".join(out)
