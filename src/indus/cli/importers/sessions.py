"""Session import from other coding-agent CLIs, as the command line sees it (P24-28).

The mapping itself lives in ``indus.service.session_import`` so the dashboard and desktop route
(``web.routes.session_import``) and the CLI share one implementation without importing each other
(the layer rule: ``cli`` and ``web`` are siblings). This module adds what a terminal needs: a
candidate table, a preview rendered as lines, and an import that names the new session.
"""

from __future__ import annotations

from pathlib import Path

from indus.service.session_import import (
    KINDS,
    Candidate,
    Imported,
    ImportRefused,
    Mapped,
    SourceKind,
    candidates,
    import_session,
    map_file,
    preview,
    resolve_candidate,
    roots,
    source_kind,
)
from indus.state.registry import StoreHandle

__all__ = [
    "KINDS",
    "Candidate",
    "ImportRefused",
    "Imported",
    "Mapped",
    "SourceKind",
    "candidate_lines",
    "candidates",
    "import_file",
    "import_session",
    "map_file",
    "preview",
    "preview_lines",
    "resolve_candidate",
    "roots",
    "source_kind",
]

_LABELS: dict[str, str] = {"claude-code": "Claude Code", "codex": "Codex"}


def candidate_lines(home_dir: Path, *, kind: SourceKind | None = None) -> list[str]:
    """One line per importable session: source, title and the folder it ran in."""
    rows = candidates(home_dir, kind=kind)
    if not rows:
        return ["No Claude Code or Codex sessions were found."]
    return [f"{_LABELS[row.kind]:<12} {row.title}  ({row.cwd or '?'})  {row.path}" for row in rows]


def preview_lines(path: Path, kind: SourceKind, *, width: int = 100) -> list[str]:
    """The mapped messages as ``role: text`` lines, each cut to ``width``."""
    mapped = preview(path, kind)
    lines = [f"{mapped.title} — {len(mapped.messages)} messages"]
    for row in mapped.to_dict()["messages"]:
        text = str(row.get("content") or "").replace("\n", " ")
        if row.get("tool"):
            text = f"[{row['tool']}] {text}".rstrip()
        lines.append(f"{row['role']:>9}: {text}"[:width])
    if mapped.skipped:
        lines.append(f"({mapped.skipped} lines were not understood and are left out)")
    return lines


def import_file(store: StoreHandle, home_dir: Path, kind: str, raw_path: str) -> Imported:
    """Import one listed session file into ``store`` (refuses files outside the tools' roots)."""
    path = resolve_candidate(home_dir, kind, raw_path)
    return import_session(store, path, source_kind(kind))
