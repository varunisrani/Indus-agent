"""Fencing tool output that an outsider controls (indirect prompt injection, A31).

Web, browser and MCP tools return text written by whoever runs the page or server. Such a result
(at least ``MIN_CHARS`` long) is wrapped in a fence that tells the model it is data. Every
occurrence of the fence word inside the content is defused first, in any letter case, so the
content cannot close the fence early or forge one. There is deliberately no "already fenced"
shortcut: a fence found in the content is the attacker's, and wrapping again is harmless.

Multimodal results are lists of parts: each text part is fenced on its own, image parts are kept.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, cast

from indus.core import loop_texts as texts
from indus.tools.budget import MCP_PREFIX

__all__ = ["MIN_CHARS", "UNTRUSTED_NAMES", "UNTRUSTED_PREFIXES", "fence", "is_untrusted"]

MIN_CHARS = 32
UNTRUSTED_NAMES = frozenset({"web_search", "web_extract"})
UNTRUSTED_PREFIXES = ("browser_", MCP_PREFIX)

_WORD = re.compile(re.escape(texts.FENCE_WORD), re.IGNORECASE)
_SOURCE_UNSAFE = re.compile(r"[^A-Za-z0-9_.:-]")


def is_untrusted(tool_name: str | None) -> bool:
    return bool(tool_name) and (
        tool_name in UNTRUSTED_NAMES or str(tool_name).startswith(UNTRUSTED_PREFIXES)
    )


def _fence_text(tool_name: str, text: str) -> str:
    if len(text) < MIN_CHARS:
        return text
    body = _WORD.sub(texts.FENCE_WORD_DEFUSED, text)
    source = _SOURCE_UNSAFE.sub("_", tool_name)
    opening = texts.FENCE_OPEN.format(word=texts.FENCE_WORD, source=source)
    closing = texts.FENCE_CLOSE.format(word=texts.FENCE_WORD)
    return f"{opening}\n{texts.FENCE_PREAMBLE}\n\n{body}\n{closing}"


def fence(tool_name: str, content: Any) -> Any:
    """``content`` fenced when ``tool_name`` returns outside data; otherwise unchanged."""
    if not is_untrusted(tool_name):
        return content
    if isinstance(content, str):
        return _fence_text(tool_name, content)
    if isinstance(content, list):
        out: list[Any] = []
        for part in cast(list[Any], content):
            if isinstance(part, Mapping):
                item = cast(Mapping[str, Any], part)
                text = item.get("text")
                if item.get("type") == "text" and isinstance(text, str):
                    out.append({**item, "text": _fence_text(tool_name, text)})
                    continue
            out.append(part)
        return out
    return content
