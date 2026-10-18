"""The one-shot note channel: per-turn notes that ride the user message, never the prompt.

Some things the model must hear mid-conversation (a must-deliver note from a gateway, "you are
now on another interface", "these skills appeared since the prompt was built") cannot go into the
system prompt without changing its bytes and losing the provider's prefix cache (R01.1). They are
staged here, popped once by the turn prologue and appended to that turn's user message as its
``api_content`` sidecar, which is persisted so later replays send the same bytes.

Must-deliver notes accumulate in arrival order; the surface-switch note and the skills-index delta
hold one value each, a newer one replacing the older.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import Any, cast

__all__ = ["NoteChannel", "attach_notes"]


class NoteChannel:
    """Notes staged for the next turn; ``pop()`` hands them over exactly once."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._must_deliver: list[str] = []
        self._surface_switch = ""
        self._skills_delta = ""

    def stage_must_deliver(self, text: str) -> None:
        if text.strip():
            with self._lock:
                self._must_deliver.append(text.strip())

    def stage_surface_switch(self, text: str) -> None:
        with self._lock:
            self._surface_switch = text.strip()

    def stage_skills_delta(self, text: str) -> None:
        with self._lock:
            self._skills_delta = text.strip()

    @property
    def pending(self) -> bool:
        with self._lock:
            return bool(self._must_deliver or self._surface_switch or self._skills_delta)

    def pop(self) -> str:
        """All staged notes joined by blank lines (must-deliver, surface, skills); then cleared."""
        with self._lock:
            parts = [*self._must_deliver, self._surface_switch, self._skills_delta]
            self._must_deliver = []
            self._surface_switch = ""
            self._skills_delta = ""
        return "\n\n".join(part for part in parts if part)


def attach_notes(message: Mapping[str, Any], notes: str) -> dict[str, Any]:
    """A copy of the user ``message`` whose wire text carries ``notes`` after it.

    Text content: ``api_content`` becomes the existing sidecar (or the content), a blank line and
    the notes; ``content`` (what the user typed) is untouched. Multimodal list content cannot take
    a string sidecar, so the notes become one more text part on a copied list.
    """
    out = dict(message)
    if not notes.strip():
        return out
    content = out.get("content")
    if isinstance(content, list):
        parts = cast(list[Any], content)
        out["content"] = [*parts, {"type": "text", "text": notes}]
        return out
    base = out.get("api_content")
    if not isinstance(base, str):
        base = content if isinstance(content, str) else ""
    out["api_content"] = f"{base}\n\n{notes}" if base else notes
    return out
