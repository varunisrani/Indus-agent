"""What the CLI says when the first word is not one of its commands.

Always three lines on stderr, then exit 2:

1. ``'X' is not an `indus` command.`` (the group's full name for a nested group);
2. a hint: where a command went when it moved (``MOVED``), how to install the plugin that would
   add it (``plugin_hint``, a seam P13 fills), up to three close names, or a pointer to the list;
3. ``Run `indus --help` to see all commands.``
"""

from __future__ import annotations

import difflib
from collections.abc import Callable, Iterable, Mapping

__all__ = [
    "MAX_SUGGESTIONS",
    "MOVED",
    "SUGGESTION_CUTOFF",
    "set_plugin_hint",
    "unknown_command_lines",
]

MAX_SUGGESTIONS = 3
SUGGESTION_CUTOFF = 0.6

MOVED: Mapping[str, str] = {}
"""Commands that left this group: ``name -> where it lives now`` (one sentence)."""

type PluginHint = Callable[[str, str], str | None]

_plugin_hint: PluginHint | None = None


def set_plugin_hint(hint: PluginHint | None) -> None:
    """Install the catalogue lookup ``(group path, word) -> install sentence or None`` (P13)."""
    global _plugin_hint
    _plugin_hint = hint


def _plugin_line(path: str, word: str) -> str | None:
    if _plugin_hint is None:
        return None
    try:
        return _plugin_hint(path, word)
    except Exception:
        return None


def unknown_command_lines(path: str, word: str, known: Iterable[str]) -> list[str]:
    """The three lines for ``word`` typed where a command of the group ``path`` was expected."""
    names = list(known)
    first = f"'{word}' is not an `{path}` command."
    hint = _plugin_line(path, word)
    if hint is None and path == "indus":
        hint = MOVED.get(word)
    if hint is None:
        close = difflib.get_close_matches(word, names, n=MAX_SUGGESTIONS, cutoff=SUGGESTION_CUTOFF)
        if close:
            hint = f"Did you mean: {', '.join(close)}?"
    if hint is None:
        hint = "No command has a name close to that."
    return [first, hint, f"Run `{path} --help` to see all commands."]
