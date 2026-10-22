"""What a person sees of a stored conversation (the display projection), shared by the TUI's
in-process bridge (``session.resume`` / ``session.history``) and the daemon's ``/history`` (W48-h).

Multipart content is flattened (image parts as ``[image]``); system, scaffold and hidden rows are
dropped; a tool row carries its call's arguments, masked the way ``tool.started`` masks them.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any, cast

from indus.core.transcript import TranscriptRow
from indus.protocol.bridge import HistoryMessage, HistoryRole
from indus.safety.redaction import redact_arguments

__all__ = ["IMAGE_MARK", "display_messages", "flatten_content"]

IMAGE_MARK = "[image]"
_IMAGE_PARTS = frozenset({"image", "image_url", "input_image"})
_SHOWN = {"user": HistoryRole.USER, "assistant": HistoryRole.ASSISTANT, "tool": HistoryRole.TOOL}


def flatten_content(content: object) -> str:
    """Text of a stored message's content: parts joined, image parts as ``[image]``."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces: list[str] = []
        for part in cast(list[Any], content):
            if isinstance(part, str):
                pieces.append(part)
                continue
            if not isinstance(part, dict):
                continue
            entry = cast(dict[str, Any], part)
            kind = str(entry.get("type", ""))
            if kind in _IMAGE_PARTS:
                pieces.append(IMAGE_MARK)
            elif isinstance(entry.get("text"), str):
                pieces.append(str(entry["text"]))
        return "\n".join(piece for piece in pieces if piece)
    return str(content)


def _call_arguments(raw: object) -> dict[str, Any] | None:
    """A stored call's arguments as an object (stored as JSON text or already parsed), masked
    for display the way ``tool.started`` masks them; None when they are not an object."""
    value: object = raw
    if isinstance(raw, str):
        try:
            value = json.loads(raw) if raw.strip() else {}
        except ValueError:
            return None
    if not isinstance(value, dict):
        return None
    return redact_arguments(cast(dict[str, Any], value))


def display_messages(rows: Iterable[TranscriptRow]) -> list[HistoryMessage]:
    """The rows a person sees, in order (system, scaffold and hidden rows dropped). A tool row
    carries its call's arguments, so a transcript rebuilt from history shows the same summary and
    Arguments as the live rows did (W47-a)."""
    names: dict[str, str] = {}
    arguments: dict[str, object] = {}
    out: list[HistoryMessage] = []
    for row in rows:
        role = _SHOWN.get(row.role)
        if role is None or row.scaffold or row.display_kind == "hidden":
            continue
        for call in row.tool_calls or ():
            function = call.get("function")
            if isinstance(function, dict) and call.get("id"):
                spec = cast(dict[str, Any], function)
                names[str(call["id"])] = str(spec.get("name", ""))
                arguments[str(call["id"])] = spec.get("arguments")
        text = flatten_content(row.content)
        name = None
        args = None
        if role is HistoryRole.TOOL:
            call_id = row.tool_call_id or ""
            name = row.name or names.get(call_id) or "tool"
            if call_id in arguments:
                args = _call_arguments(arguments[call_id])
        elif not text.strip():
            continue  # an assistant row with only tool calls
        out.append(
            HistoryMessage(role=role, text=text, name=name, arguments=args, row_id=row.row_id)
        )
    return out
