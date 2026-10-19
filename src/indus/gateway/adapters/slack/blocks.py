"""Block Kit payloads for the Slack adapter (P15-35).

Approval cards and clarify questions are a ``section`` with the text and ``actions`` blocks of
buttons. Each button's ``action_id`` and ``value`` are our callback data (``apv:<id>:<choice>`` /
``clr:<id>:<n>``), so a press comes back already in the shape the chat prompts parse; action ids
must be unique inside one actions block, which ours are.

``sanitize`` is the last step before any ``blocks`` payload leaves: Slack rejects the whole message
(``invalid_blocks``) for one oversized text or empty block, so texts are clamped to their caps,
empty blocks are dropped and the list is cut at the 50-block maximum.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, cast

from indus.gateway.adapters.base import ApprovalPrompt, ClarifyPrompt
from indus.gateway.outbound.formatting import SLACK_MAX_BLOCKS, SLACK_MAX_SECTION_TEXT

__all__ = [
    "BUTTONS_PER_BLOCK",
    "BUTTON_LABEL_MAX",
    "HEADER_TEXT_MAX",
    "approval_blocks",
    "button",
    "clarify_blocks",
    "sanitize",
    "section",
]

type Block = dict[str, Any]

BUTTONS_PER_BLOCK = 5
BUTTON_LABEL_MAX = 75
HEADER_TEXT_MAX = 150
_ELLIPSIS = "…"


def _clamp(text: str, limit: int) -> str:
    """``text`` cut to ``limit`` characters, the cut marked with an ellipsis."""
    if len(text) <= limit:
        return text
    return text[: limit - len(_ELLIPSIS)] + _ELLIPSIS


def section(text: str) -> Block:
    """A mrkdwn ``section`` (text clamped to the section cap)."""
    return {
        "type": "section",
        "text": {"type": "mrkdwn", "text": _clamp(text, SLACK_MAX_SECTION_TEXT)},
    }


def button(label: str, data: str, *, style: str = "") -> Block:
    """A button whose action id and value are ``data``; ``style`` primary/danger or none."""
    element: Block = {
        "type": "button",
        "text": {"type": "plain_text", "text": _clamp(label.strip() or "?", BUTTON_LABEL_MAX)},
        "action_id": data,
        "value": data,
    }
    if style in ("primary", "danger"):
        element["style"] = style
    return element


def _action_rows(buttons: Sequence[Block]) -> list[Block]:
    return [
        {"type": "actions", "elements": list(buttons[n : n + BUTTONS_PER_BLOCK])}
        for n in range(0, len(buttons), BUTTONS_PER_BLOCK)
    ]


def approval_blocks(prompt: ApprovalPrompt, text: str, data: Iterable[str]) -> list[Block]:
    """The approval card: ``text`` (already mrkdwn) and one button per action, ``data`` being
    the callback for each action in order."""
    buttons = [
        button(action.label, payload, style=action.style)
        for action, payload in zip(prompt.actions, data, strict=True)
    ]
    return [section(text), *_action_rows(buttons)]


def clarify_blocks(prompt: ClarifyPrompt, text: str, data: Iterable[str]) -> list[Block]:
    """The clarify question: ``text`` and one button per choice (rows of five), cut so the whole
    card stays within the block cap."""
    buttons = [button(label, payload) for label, payload in zip(prompt.choices, data, strict=True)]
    room = (SLACK_MAX_BLOCKS - 1) * BUTTONS_PER_BLOCK
    return [section(text), *_action_rows(buttons[:room])]


def _mapping(obj: object) -> Mapping[str, Any] | None:
    return cast(Mapping[str, Any], obj) if isinstance(obj, Mapping) else None


def _text_of(obj: object) -> str:
    found = _mapping(obj)
    value = found.get("text") if found is not None else None
    return value if isinstance(value, str) else ""


def _clamped_text_obj(obj: Mapping[str, Any], limit: int) -> dict[str, Any]:
    out = dict(obj)
    text = _text_of(obj)
    if len(text) > limit:
        out["text"] = _clamp(text, limit)
    return out


def _clamped_element(element: object) -> object:
    found = _mapping(element)
    if found is not None and found.get("type") in ("mrkdwn", "plain_text"):
        return _clamped_text_obj(found, SLACK_MAX_SECTION_TEXT)
    return element


def _clean(block: Mapping[str, Any]) -> Block | None:
    kind = block.get("type")
    out: Block = dict(block)
    text_obj = _mapping(block.get("text"))
    if kind == "section":
        has_body = bool(block.get("fields")) or bool(block.get("accessory"))
        if text_obj is not None and _text_of(text_obj).strip():
            out["text"] = _clamped_text_obj(text_obj, SLACK_MAX_SECTION_TEXT)
        elif not has_body:
            return None
        else:
            out.pop("text", None)
    elif kind == "header":
        if text_obj is None or not _text_of(text_obj).strip():
            return None
        out["text"] = _clamped_text_obj(text_obj, HEADER_TEXT_MAX)
    elif kind == "context":
        elements = block.get("elements")
        if not isinstance(elements, list) or not elements:
            return None
        out["elements"] = [_clamped_element(e) for e in cast(list[object], elements)]
    elif kind in ("actions", "rich_text"):
        if not block.get("elements"):
            return None
    elif kind == "table":
        if not block.get("rows"):
            return None
    elif not isinstance(kind, str) or not kind:
        return None
    return out


def sanitize(blocks: object) -> list[Block] | None:
    """``blocks`` made safe to send (see the module notes), or None when nothing is left — the
    caller then sends its text alone. Never raises."""
    if not isinstance(blocks, list | tuple):
        return None
    out: list[Block] = []
    for block in cast(Sequence[object], blocks):
        found = _mapping(block)
        cleaned = _clean(found) if found is not None else None
        if cleaned is not None:
            out.append(cleaned)
    return out[:SLACK_MAX_BLOCKS] or None
