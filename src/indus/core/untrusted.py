"""Fencing tool output that an outsider controls (indirect prompt injection, A31).

Web, browser and MCP tools return text written by whoever runs the page or server, the Feishu
document and comment tools (P16-15) text written by whoever edits the document, ``a2a_call``
(P24-10) the answer another organisation's agent wrote, ``message_agent`` (P24-07) the reply of
another bot (a peer gateway's, or a local profile's that may have read outside text) and
``drive_preview`` (P24-25) what a page in the desktop preview shows. Such a result
(at least ``MIN_CHARS`` long) is wrapped in a fence that tells the model it is data. Every
occurrence of the fence word inside the content is defused first, in any letter case, so the
content cannot close the fence early or forge one. There is deliberately no "already fenced"
shortcut: a fence found in the content is the attacker's, and wrapping again is harmless.

Multimodal results are lists of parts: each text part is fenced on its own, image parts are kept.
``wrap`` is the same fence for callers that frame outside text themselves, at any length (the
group-room transcript, wave 45).

Laundering (SEC-4, wave 46): a tool of our own can pass outside text on under its own name. A
script run by ``execute_code`` may call ``web_extract`` over its RPC and print the page, and a
``delegate_task`` child may summarise a page it read. Those results are *tainted*: the tool records
the call id in the turn's ``Taint`` (offered to handlers as the ``taint`` keyword; an inline tool
gets the ``TurnState``), and the tool round fences a tainted result whatever its tool's name. The
script itself still gets plain JSON answers; only what reaches the model is fenced. ``Taint.read``
says the turn saw outside text at all (an untrusted tool ran, or a tainted result came back), so
the parent of a delegated child knows to fence the child's summary.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, cast

from indus.core import loop_texts as texts
from indus.tools.budget import MCP_PREFIX

__all__ = [
    "MIN_CHARS",
    "UNTRUSTED_NAMES",
    "UNTRUSTED_PREFIXES",
    "Taint",
    "fence",
    "fence_as",
    "is_untrusted",
    "wrap",
]

MIN_CHARS = 32
UNTRUSTED_NAMES = frozenset(
    {"web_search", "web_extract", "a2a_call", "message_agent", "drive_preview"}
)
UNTRUSTED_PREFIXES = ("browser_", MCP_PREFIX, "feishu_", "yuanbao_")

_WORD = re.compile(re.escape(texts.FENCE_WORD), re.IGNORECASE)
_SOURCE_UNSAFE = re.compile(r"[^A-Za-z0-9_.:-]")


def is_untrusted(tool_name: str | None) -> bool:
    return bool(tool_name) and (
        tool_name in UNTRUSTED_NAMES or str(tool_name).startswith(UNTRUSTED_PREFIXES)
    )


def wrap(source: str, text: str) -> str:
    """``text`` inside the fence, whatever its length: for outside data a caller frames itself
    (a room member's reply in another member's prompt, SEC-15). ``source`` names who wrote it."""
    body = _WORD.sub(texts.FENCE_WORD_DEFUSED, text)
    label = _SOURCE_UNSAFE.sub("_", source)
    opening = texts.FENCE_OPEN.format(word=texts.FENCE_WORD, source=label)
    closing = texts.FENCE_CLOSE.format(word=texts.FENCE_WORD)
    return f"{opening}\n{texts.FENCE_PREAMBLE}\n\n{body}\n{closing}"


def _fence_text(tool_name: str, text: str) -> str:
    return text if len(text) < MIN_CHARS else wrap(tool_name, text)


class Taint:
    """One turn's laundering record (see the module notes)."""

    def __init__(self) -> None:
        self._calls: set[str] = set()
        self.read = False

    def untrusted(self, tool_name: str) -> bool:
        """Whether ``tool_name``'s output is outside text (for tools below ``core``)."""
        return is_untrusted(tool_name)

    def mark(self, call_id: str | None) -> None:
        """The result of call ``call_id`` carries outside text under a trusted tool's name."""
        if call_id:
            self._calls.add(call_id)

    def __contains__(self, call_id: object) -> bool:
        return call_id in self._calls


def fence(tool_name: str, content: Any, *, tainted: bool = False) -> Any:
    """``content`` fenced when ``tool_name`` returns outside data or the call is ``tainted``;
    otherwise unchanged."""
    if not tainted and not is_untrusted(tool_name):
        return content
    return fence_as(tool_name, content)


def fence_as(source: str, content: Any) -> Any:
    """``content`` fenced as outside data from ``source``, whatever its name."""
    if isinstance(content, str):
        return _fence_text(source, content)
    if isinstance(content, list):
        out: list[Any] = []
        for part in cast(list[Any], content):
            if isinstance(part, Mapping):
                item = cast(Mapping[str, Any], part)
                text = item.get("text")
                if item.get("type") == "text" and isinstance(text, str):
                    out.append({**item, "text": _fence_text(source, text)})
                    continue
            out.append(part)
        return out
    return content
