"""Feishu / Lark message content, in and out (P16-14).

Inbound, a message's ``content`` is a JSON string whose shape depends on ``message_type``.
`parse_content` turns it into readable text plus the resources (images, files, voice, video) the
intake can download, and resolves mention placeholders: ``@_user_1`` style keys are replaced by
``@Name`` from the event's ``mentions`` list, the bot's own mention is removed (and reported as
``mentioned``), ``@_all`` becomes ``@all`` (and counts as a mention of the bot).

Outbound, `outbound_content` picks ``text`` or a ``post`` with ``md`` rows: Markdown renders only
inside a post, and a fenced code block gets a row of its own so the text after it stays visible.
Cards (`card`) carry approval and clarify buttons; the button value holds our callback data under
``indus``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, cast

__all__ = [
    "BUTTON_KEY",
    "Parsed",
    "Resource",
    "button_data",
    "card",
    "card_text",
    "has_markdown",
    "outbound_content",
    "parse_content",
    "media_post",
    "post_content",
    "post_rows",
    "text_content",
]

BUTTON_KEY = "indus"
ResourceKind = Literal["image", "file", "audio", "media"]

_PLACEHOLDER = re.compile(r"@_user_\d+|@_all")
_SPACES = re.compile(r"[ \t]+")
_FENCE = re.compile(r"^\s*```")
_MARKDOWN = re.compile(
    r"^\s{0,3}#{1,6}\s"  # heading
    r"|^\s*(?:[-*+]|\d+[.)])\s+\S"  # list item
    r"|^\s*>"  # quote
    r"|```"  # fence
    r"|`[^`\n]+`"  # inline code
    r"|\*\*[^*\n]+\*\*|__[^_\n]+__"  # bold
    r"|~~[^~\n]+~~"  # strike
    r"|\[[^\]\n]+\]\([^)\s]+\)"  # link
    r"|^\|.*\|\s*$",  # table row
    re.MULTILINE,
)
_TEXT_KEYS = ("text", "content", "title")


@dataclass(frozen=True)
class Resource:
    """A downloadable part of a message (``GET messages/{id}/resources/{key}?type=``)."""

    kind: ResourceKind
    key: str
    name: str = ""

    @property
    def request_type(self) -> str:
        return "image" if self.kind == "image" else "file"


@dataclass
class Parsed:
    text: str = ""
    resources: list[Resource] = field(default_factory=list[Resource])
    mentioned: bool = False
    sticker: bool = False


def _object(value: object) -> dict[str, Any]:
    return dict(cast(Mapping[str, Any], value)) if isinstance(value, Mapping) else {}


def _list(value: object) -> list[Any]:
    return list(cast(Sequence[Any], value)) if isinstance(value, list) else []


def _load(raw: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
    except ValueError:
        return {"text": raw}
    return _object(loaded)


@dataclass(frozen=True)
class _Mention:
    name: str
    open_id: str
    is_self: bool


def _mentions(raw: object, bot_open_id: str) -> dict[str, _Mention]:
    found: dict[str, _Mention] = {}
    for item in _list(raw):
        entry = _object(item)
        key = str(entry.get("key") or "")
        if not key:
            continue
        ident = entry.get("id")
        open_id = (
            str(_object(ident).get("open_id") or "")
            if isinstance(ident, Mapping)
            else str(ident or "")
        )
        name = str(entry.get("name") or "").strip()
        found[key] = _Mention(name, open_id, bool(bot_open_id) and open_id == bot_open_id)
    return found


class _Text:
    """Placeholder resolution with the mention state it found."""

    def __init__(self, mentions: Mapping[str, _Mention]) -> None:
        self.mentions = mentions
        self.mentioned = False

    def resolve(self, text: str) -> str:
        def swap(match: re.Match[str]) -> str:
            key = match.group(0)
            if key == "@_all":
                self.mentioned = True
                return "@all"
            found = self.mentions.get(key)
            if found is None:
                return ""
            if found.is_self:
                self.mentioned = True
                return ""
            return f"@{found.name or found.open_id or 'someone'}"

        return _PLACEHOLDER.sub(swap, text)

    def at(self, element: Mapping[str, Any]) -> str:
        """A post ``at`` element (its ``user_id`` holds the placeholder key or ``all``)."""
        key = str(element.get("user_id") or "")
        if key in ("@_all", "all"):
            self.mentioned = True
            return "@all"
        found = self.mentions.get(key)
        if found is not None:
            if found.is_self:
                self.mentioned = True
                return ""
            return f"@{found.name or found.open_id or 'someone'}"
        name = str(element.get("user_name") or "").strip()
        return f"@{name}" if name else ""


def _styled(element: Mapping[str, Any], text: str) -> str:
    styles = {str(s) for s in _list(element.get("style"))}
    if not text.strip():
        return text
    if "bold" in styles:
        text = f"**{text}**"
    if "italic" in styles:
        text = f"*{text}*"
    if "lineThrough" in styles:
        text = f"~~{text}~~"
    return text


def _post_body(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The locale block of a post (``{"title", "content"}``), whichever locale it uses."""
    if isinstance(payload.get("content"), list):
        return dict(payload)
    nested = _object(payload.get("post")) or dict(payload)
    for locale in ("zh_cn", "en_us", "ja_jp"):
        block = _object(nested.get(locale))
        if isinstance(block.get("content"), list):
            return block
    for value in nested.values():
        block = _object(value)
        if isinstance(block.get("content"), list):
            return block
    return {}


def _post_element(element: Mapping[str, Any], text: _Text, parsed: Parsed) -> str:
    tag = str(element.get("tag") or "")
    if tag == "text":
        return _styled(element, text.resolve(str(element.get("text") or "")))
    if tag == "a":
        label = str(element.get("text") or "").strip()
        href = str(element.get("href") or "").strip()
        if label and href:
            return f"[{label}]({href})"
        return label or href
    if tag == "at":
        return text.at(element)
    if tag == "img":
        key = str(element.get("image_key") or "")
        if key:
            parsed.resources.append(Resource("image", key))
        return ""
    if tag == "media":
        key = str(element.get("file_key") or "")
        if key:
            parsed.resources.append(Resource("media", key, str(element.get("file_name") or "")))
        return ""
    if tag == "emotion":
        return f":{element.get('emoji_type') or 'emoji'}:"
    if tag == "code_block":
        language = str(element.get("language") or "").lower()
        code = str(element.get("text") or "").rstrip("\n")
        return f"\n```{language}\n{code}\n```\n"
    if tag == "hr":
        return "\n---\n"
    if tag == "md":
        return text.resolve(str(element.get("text") or ""))
    return str(element.get("text") or "")


def _text_of_post(payload: Mapping[str, Any], text: _Text, parsed: Parsed) -> str:
    body = _post_body(payload)
    lines: list[str] = []
    title = str(body.get("title") or "").strip()
    if title:
        lines.append(title)
    for row in _list(body.get("content")):
        pieces = [_post_element(_object(e), text, parsed) for e in _list(row)]
        line = "".join(pieces)
        if line.strip():
            lines.append(line)
    return "\n".join(lines)


def _card_lines(value: object, out: list[str], depth: int = 0) -> None:
    if depth > 8 or len(out) >= 12:
        return
    if isinstance(value, Mapping):
        entry = cast(Mapping[str, Any], value)
        for key in ("content", "text"):
            found = entry.get(key)
            if isinstance(found, str) and found.strip():
                out.append(found.strip())
        for key, child in entry.items():
            if key not in ("content", "text") and isinstance(child, Mapping | list):
                _card_lines(child, out, depth + 1)
    elif isinstance(value, list):
        for child in cast(list[Any], value):
            _card_lines(child, out, depth + 1)


def parse_content(
    message_type: str, raw_content: str, mentions: object = None, *, bot_open_id: str = ""
) -> Parsed:
    """Readable text, resources and mention state of one message (see the module notes)."""
    payload = _load(raw_content)
    text = _Text(_mentions(mentions, bot_open_id))
    parsed = Parsed()
    kind = message_type.strip().lower()
    if kind == "text":
        body = text.resolve(str(payload.get("text") or ""))
    elif kind == "post":
        body = _text_of_post(payload, text, parsed)
    elif kind == "image":
        key = str(payload.get("image_key") or "")
        if key:
            parsed.resources.append(Resource("image", key))
        body = ""
    elif kind in ("file", "audio", "media"):
        key = str(payload.get("file_key") or "")
        if key:
            name = str(payload.get("file_name") or "")
            parsed.resources.append(Resource(cast(ResourceKind, kind), key, name))
        body = ""
    elif kind == "sticker":
        parsed.sticker = True
        body = "[sticker]"
    elif kind == "interactive":
        lines: list[str] = []
        _card_lines(payload, lines)
        body = "\n".join(["[card]", *lines[:12]])
    elif kind == "merge_forward":
        body = "[forwarded messages]"
    elif kind == "share_chat":
        body = f"[shared group chat {payload.get('chat_id') or ''}]".replace(" ]", "]")
    elif kind == "share_user":
        body = "[shared contact]"
    elif kind == "location":
        name = str(payload.get("name") or "a place")
        body = f"[location: {name} ({payload.get('latitude')}, {payload.get('longitude')})]"
    else:
        body = f"[{kind or 'unknown'} message]"
    parsed.text = _tidy(body)
    parsed.mentioned = text.mentioned
    return parsed


def _tidy(text: str) -> str:
    """Prose lines with runs of spaces collapsed and blank lines dropped; code blocks as written."""
    out: list[str] = []
    in_code = False
    for line in text.replace("\r\n", "\n").split("\n"):
        if _FENCE.match(line):
            in_code = not in_code
            out.append(line.strip())
            continue
        if in_code:
            out.append(line)
            continue
        cleaned = _SPACES.sub(" ", line).strip()
        if cleaned:
            out.append(cleaned)
    return "\n".join(out).strip()


# -- outbound ---------------------------------------------------------------------------------


def has_markdown(text: str) -> bool:
    return bool(_MARKDOWN.search(text))


def post_rows(markdown: str) -> list[list[dict[str, str]]]:
    """``md`` rows: prose and each fenced code block in rows of their own."""
    rows: list[list[dict[str, str]]] = []
    current: list[str] = []
    in_code = False

    def close() -> None:
        block = "\n".join(current)
        if block.strip():
            rows.append([{"tag": "md", "text": block}])
        current.clear()

    for line in markdown.split("\n"):
        fence = bool(_FENCE.match(line))
        if fence and not in_code:
            close()
        current.append(line)
        if fence:
            in_code = not in_code
            if not in_code:
                close()
    close()
    return rows or [[{"tag": "md", "text": markdown}]]


def post_content(markdown: str, *, locale: str = "zh_cn") -> str:
    return json.dumps({locale: {"content": post_rows(markdown)}}, ensure_ascii=False)


def text_content(text: str) -> str:
    return json.dumps({"text": text}, ensure_ascii=False)


def outbound_content(text: str, *, plain: bool, locale: str = "zh_cn") -> tuple[str, str]:
    """(``msg_type``, ``content``) for ``text``: a post when it carries Markdown."""
    if not plain and has_markdown(text):
        return "post", post_content(text, locale=locale)
    return "text", text_content(text)


def media_post(caption: str, media: Mapping[str, str], *, locale: str = "zh_cn") -> str:
    """A post with a caption and one image / media element below it."""
    rows: list[list[dict[str, str]]] = [*post_rows(caption), [dict(media)]]
    return json.dumps({locale: {"content": rows}}, ensure_ascii=False)


ButtonStyle = Literal["primary", "danger", "default"]


def card(
    text: str,
    *,
    title: str,
    template: str = "blue",
    buttons: Iterable[tuple[str, str, ButtonStyle]] = (),
) -> dict[str, Any]:
    """An interactive card: a header, the text as Markdown, and a row of buttons."""
    elements: list[dict[str, Any]] = [{"tag": "markdown", "content": text}]
    actions = [
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": label},
            "type": style,
            "value": {BUTTON_KEY: data},
        }
        for label, data, style in buttons
    ]
    if actions:
        elements.append({"tag": "action", "actions": actions})
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": template},
        "elements": elements,
    }


def card_text(card_json: Mapping[str, Any]) -> str:
    """The Markdown text of one of our cards."""
    for element in _list(card_json.get("elements")):
        entry = _object(element)
        if entry.get("tag") == "markdown":
            return str(entry.get("content") or "")
    return ""


def button_data(value: object) -> str | None:
    """Our callback data from a pressed button's ``value``; None for buttons we did not make."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    data = _object(value).get(BUTTON_KEY)
    return data if isinstance(data, str) and data else None
