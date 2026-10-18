"""The persona file: ``<home>/PERSONA.md`` of the build's profile becomes the prompt's identity.

Only the profile home is looked at, never the working directory, so a repository cannot replace
who the agent is. A missing, empty, whitespace-only, unreadable or too-slow file gives ``None`` and
the builder uses the built-in identity. The text is used verbatim after screening and capping; a
scanner hit is logged but the file still loads, because the user wrote it (same trust class as
the config file).
"""

from __future__ import annotations

from pathlib import Path

from indus.core.prompt.context_files import ReadSettings, read_bounded, screen, truncate

__all__ = ["PERSONA_FILE", "load_persona", "persona_path"]

PERSONA_FILE = "PERSONA.md"


def persona_path(home: Path) -> Path:
    return home / PERSONA_FILE


async def load_persona(home: Path, *, reads: ReadSettings, cap: int) -> str | None:
    """The persona text for the profile at ``home``, or None to use the built-in identity."""
    path = persona_path(home)
    text = await read_bounded(path, reads)
    if text is None:
        return None
    text = text.strip()
    if not text:
        return None
    kept = screen(text, PERSONA_FILE, user_authored=True)
    return truncate(kept, name=PERSONA_FILE, cap=cap, read_path=str(path))
