"""How a tool call looks in an editor: its ACP kind, title, start content and result (P19-10).

``KINDS`` names an ACP tool kind for every built-in tool (a test walks the registry and the core
list, so a new tool cannot go unmapped); MCP tools, plugin tools and anything unknown are ``other``.

The builders return plain JSON (the vendor's field names) so the projection and the history
replay can send them as they are:

- ``title(name, args)``: ``name: <preview>`` from the argument that says what the call is about
  (command, path, pattern, query, URL), at most 80 characters;
- ``start_content(name, args)``: edits as diffs (``write_file`` new text, ``patch`` old → new),
  ``terminal`` as ``$ command``, code in a fence, nothing for reads and searches (title and
  location say it), other arguments as JSON;
- ``result_content(name, result)``: the result text cut at ``MAX_OUTPUT`` characters
  (``read_file`` output in a fence so line numbers stay literal);
- ``locations(args)``: the file a call works on.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Final

__all__ = [
    "ABANDONED_TEXT",
    "KINDS",
    "MAX_OUTPUT",
    "cut",
    "fenced",
    "kind_of",
    "locations",
    "result_content",
    "start_content",
    "text_content",
    "title",
]

MAX_OUTPUT: Final = 5000
"""Characters of a tool's output shown in the editor."""
_MAX_ARGS: Final = 1200
_MAX_TITLE: Final = 80
ABANDONED_TEXT: Final = "This tool call ended without a result (blocked, denied or interrupted)."

_GROUPS: Final[dict[str, tuple[str, ...]]] = {
    "read": (
        "read_file",
        "skill_view",
        "skills_list",
        "browser_snapshot",
        "browser_vision",
        "browser_get_images",
        "browser_console",
        "vision_analyze",
    ),
    "edit": ("write_file", "patch", "skill_manage"),
    "search": ("search_files", "session_search"),
    "execute": (
        "terminal",
        "process_manage",
        "execute_code",
        "browser_click",
        "browser_type",
        "browser_scroll",
        "browser_press",
        "browser_back",
        "browser_dialog",
        "browser_cdp",
        "delegate_task",
        "image_generate",
        "text_to_speech",
        "video_generate",
        "video_edit",
        "video_extend",
        "computer_use",
    ),
    "fetch": ("web_search", "web_extract", "browser_navigate"),
    "think": ("_thinking",),
    "other": ("todo_list", "memory", "clarify", "cronjob_manage"),
}

KINDS: Final[Mapping[str, str]] = {name: kind for kind, names in _GROUPS.items() for name in names}
"""Tool name → ACP tool kind (``read``, ``edit``, ``search``, ``execute``, ``fetch``, ``think``,
``other``)."""


def kind_of(name: str) -> str:
    """The ACP kind of a tool; ``other`` for MCP, plugin and unknown tools."""
    return KINDS.get(name, "other")


# -- small pieces ----------------------------------------------------------------------------------


def text_content(text: str) -> dict[str, Any]:
    """One ``content`` tool-call item holding text."""
    return {"type": "content", "content": {"type": "text", "text": text}}


def _diff(path: str, old: str | None, new: str) -> dict[str, Any]:
    entry: dict[str, Any] = {"type": "diff", "path": path, "newText": new}
    if old is not None:
        entry["oldText"] = old
    return entry


def cut(text: str, limit: int = MAX_OUTPUT) -> str:
    """``text`` at most ``limit`` characters, with a tail saying how long it was."""
    if len(text) <= limit:
        return text
    tail = f"\n… ({len(text):,} characters in all)"
    return text[: max(0, limit - len(tail))] + tail


def fenced(text: str, language: str = "") -> str:
    """``text`` in a Markdown fence longer than any backtick run inside it."""
    longest = 0
    run = 0
    for char in text:
        run = run + 1 if char == "`" else 0
        longest = max(longest, run)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{language}\n{text}\n{fence}"


def _arg(args: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list) and value:
            return ", ".join(str(v) for v in value)  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    return ""


def _json(args: Mapping[str, Any]) -> str:
    try:
        return json.dumps(dict(args), indent=2, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(dict(args))


# -- titles, content, locations -----------------------------------------------------------------

_PREVIEW_KEYS: Final[tuple[str, ...]] = (
    "command",
    "path",
    "pattern",
    "query",
    "url",
    "urls",
    "image_url",
    "name",
    "action",
    "prompt",
    "goal",
)


def title(name: str, args: Mapping[str, Any]) -> str:
    """``name: preview`` (one line, at most 80 characters of preview), else the bare name."""
    preview = " ".join(_arg(args, *_PREVIEW_KEYS).split())
    if not preview:
        return name or "tool"
    if len(preview) > _MAX_TITLE:
        preview = preview[: _MAX_TITLE - 1] + "…"
    return f"{name}: {preview}"


def start_content(name: str, args: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    """What the editor shows while the call runs (see the module notes); None for nothing."""
    path = _arg(args, "path")
    if name == "write_file" and path:
        content = args.get("content")
        return [_diff(path, None, content if isinstance(content, str) else "")]
    if name == "patch" and path:
        old, new = args.get("old_string"), args.get("new_string")
        if isinstance(old, str) and isinstance(new, str):
            return [_diff(path, old, new)]
    if name == "terminal":
        return [text_content(f"$ {_arg(args, 'command')}")]
    if name == "execute_code":
        code = _arg(args, "code")
        return [text_content(fenced(cut(code, _MAX_ARGS), "python"))] if code else None
    if kind_of(name) in ("read", "search") or not args:
        return None
    return [text_content(cut(_json(args), _MAX_ARGS))]


def locations(args: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The file a call works on (``path``, with ``offset``/``line`` as the line)."""
    path = args.get("path")
    if not isinstance(path, str) or not path:
        return []
    entry: dict[str, Any] = {"path": path}
    line = args.get("line", args.get("offset"))
    if isinstance(line, int) and not isinstance(line, bool) and line >= 0:
        entry["line"] = line
    return [entry]


def result_content(name: str, result: str) -> list[dict[str, Any]] | None:
    """The completed call's output for the editor; None when it produced no text."""
    if not result.strip():
        return None
    if name == "read_file":
        return [text_content(fenced(cut(result, MAX_OUTPUT - 16)))]
    return [text_content(cut(result))]
