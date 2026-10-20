"""Markdown from the agent → the HTML subset Matrix clients render (P16-06).

Matrix messages carry a plain ``body`` and, optionally, ``format: org.matrix.custom.html`` with a
``formatted_body``. We write the HTML ourselves from the Markdown the sender hands us (tables were
already turned into code blocks by the ``markdown`` formatter), so every character of the input is
escaped unless it is markup we emit; raw HTML in the agent's text shows as text, never as tags.
Links keep only ``http``, ``https``, ``mailto`` and ``matrix`` URLs; anything else stays text.

Handled: fenced code (language kept as ``language-<x>`` when safe), inline code, ``**bold**`` /
``__bold__``, ``*italic*`` / ``_italic_``, ``~~strike~~``, links, headings, block quotes, bullet and
numbered lists, rules, and line breaks. User mentions (``@name:server``) outside code become
matrix.to pills; `mentioned_users` lists them for the ``m.mentions`` block.
"""

from __future__ import annotations

import re
from html import escape

__all__ = ["mentioned_users", "to_html"]

_FENCE = re.compile(r"^\s*```\s*([\w+.#-]*)\s*$")
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_QUOTE = re.compile(r"^>\s?(.*)$")
_BULLET = re.compile(r"^\s*[-*+]\s+(.+)$")
_NUMBERED = re.compile(r"^\s*\d{1,9}[.)]\s+(.+)$")
_RULE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_LANG = re.compile(r"^[A-Za-z0-9_+.#-]{1,32}$")
_SAFE_SCHEMES = ("http://", "https://", "mailto:", "matrix:")
# A Matrix user id: @localpart:server (with an optional port).
_MXID = re.compile(r"(?<![\w/@])(@[a-z0-9._=/+-]+:[A-Za-z0-9.-]+(?::\d{1,5})?)", re.IGNORECASE)
_INLINE = re.compile(
    r"(?P<code>`+)(?P<code_body>.+?)(?P=code)"
    r"|\[(?P<label>[^\]\n]+)\]\((?P<url>[^)\s]+)\)"
    r"|(?P<strong>\*\*|__)(?P<strong_body>.+?)(?P=strong)"
    r"|~~(?P<strike_body>.+?)~~"
    r"|(?<![\w*])\*(?P<em_star>[^*\s](?:[^*]*[^*\s])?)\*(?!\*)"
    r"|(?<![\w_])_(?P<em_under>[^_\s](?:[^_]*[^_\s])?)_(?![\w_])"
)


def _safe_url(url: str) -> str | None:
    cleaned = url.strip()
    if any(cleaned.lower().startswith(s) for s in _SAFE_SCHEMES):
        return cleaned
    return None


def _pills(text: str, pills: bool = True) -> str:
    """Escape ``text``, turning user ids into matrix.to links (never inside a link label)."""
    if not pills:
        return escape(text, quote=False)
    out: list[str] = []
    last = 0
    for match in _MXID.finditer(text):
        out.append(escape(text[last : match.start()], quote=False))
        user = match.group(1)
        out.append(f'<a href="https://matrix.to/#/{escape(user)}">{escape(user, quote=False)}</a>')
        last = match.end()
    out.append(escape(text[last:], quote=False))
    return "".join(out)


def _inline(text: str, pills: bool = True) -> str:
    out: list[str] = []
    last = 0
    for match in _INLINE.finditer(text):
        out.append(_pills(text[last : match.start()], pills))
        last = match.end()
        if match.group("code") is not None:
            out.append(f"<code>{escape(match.group('code_body').strip(), quote=False)}</code>")
        elif match.group("url") is not None:
            url = _safe_url(match.group("url"))
            label = _inline(match.group("label"), pills=False)
            if url is None:
                out.append(label + escape(f" ({match.group('url')})", quote=False))
            else:
                out.append(f'<a href="{escape(url)}">{label}</a>')
        elif match.group("strong_body") is not None:
            out.append(f"<strong>{_inline(match.group('strong_body'), pills)}</strong>")
        elif match.group("strike_body") is not None:
            out.append(f"<del>{_inline(match.group('strike_body'), pills)}</del>")
        else:
            body = match.group("em_star") or match.group("em_under") or ""
            out.append(f"<em>{_inline(body, pills)}</em>")
    out.append(_pills(text[last:], pills))
    return "".join(out)


def _flush_paragraph(lines: list[str], out: list[str]) -> None:
    if lines:
        out.append("<p>" + "<br>".join(_inline(line) for line in lines) + "</p>")
        lines.clear()


def to_html(markdown: str) -> str:
    """The HTML for ``markdown`` (see the module notes)."""
    lines = markdown.split("\n")
    out: list[str] = []
    paragraph: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        fence = _FENCE.match(line)
        if fence:
            _flush_paragraph(paragraph, out)
            lang = fence.group(1)
            end = i + 1
            while end < len(lines) and not _FENCE.match(lines[end]):
                end += 1
            body = escape("\n".join(lines[i + 1 : end]), quote=False)
            attr = f' class="language-{lang}"' if lang and _LANG.match(lang) else ""
            out.append(f"<pre><code{attr}>{body}</code></pre>")
            i = end + 1
            continue
        if not line.strip():
            _flush_paragraph(paragraph, out)
            i += 1
            continue
        if _RULE.match(line):
            _flush_paragraph(paragraph, out)
            out.append("<hr>")
            i += 1
            continue
        heading = _HEADING.match(line)
        if heading:
            _flush_paragraph(paragraph, out)
            level = len(heading.group(1))
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            i += 1
            continue
        if _QUOTE.match(line):
            _flush_paragraph(paragraph, out)
            quoted: list[str] = []
            while i < len(lines) and (found := _QUOTE.match(lines[i])):
                quoted.append(_inline(found.group(1)))
                i += 1
            out.append("<blockquote>" + "<br>".join(quoted) + "</blockquote>")
            continue
        matched = False
        for pattern, tag in ((_BULLET, "ul"), (_NUMBERED, "ol")):
            if pattern.match(line):
                _flush_paragraph(paragraph, out)
                items: list[str] = []
                while i < len(lines) and (item := pattern.match(lines[i])):
                    items.append(f"<li>{_inline(item.group(1))}</li>")
                    i += 1
                out.append(f"<{tag}>{''.join(items)}</{tag}>")
                matched = True
                break
        if matched:
            continue
        paragraph.append(line)
        i += 1
    _flush_paragraph(paragraph, out)
    if len(out) == 1 and out[0].startswith("<p>") and out[0].endswith("</p>"):
        return out[0][3:-4]  # one plain paragraph: no wrapper (clients add their own spacing)
    return "".join(out)


_CODE_SPANS = re.compile(r"```.*?```|`[^`\n]+`", re.DOTALL)


def mentioned_users(markdown: str) -> list[str]:
    """User ids written in ``markdown`` outside code, first spelling first, no repeats."""
    visible = _CODE_SPANS.sub(" ", markdown)
    return list(dict.fromkeys(m.group(1) for m in _MXID.finditer(visible)))
