"""Input history of the classic REPL: ``<home>/input_history`` (P06-04).

One JSON string per line, oldest first, so multi-line drafts survive. The file is rewritten
through ``fsio`` (atomic, owner-only) and keeps the newest ``LIMIT`` entries. A file that cannot
be read or written never stops the REPL: history is then kept in memory only.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from pathlib import Path

from prompt_toolkit.history import History

from indus.foundation import fsio

__all__ = ["HISTORY_FILE", "LIMIT", "InputHistory"]

log = logging.getLogger(__name__)

HISTORY_FILE = "input_history"
LIMIT = 1000


class InputHistory(History):
    """prompt_toolkit history backed by one owner-only file."""

    def __init__(self, path: Path, limit: int = LIMIT) -> None:
        super().__init__()
        self.path = path
        self.limit = limit
        self._entries: list[str] = []

    def _read(self) -> list[str]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError as exc:
            log.warning("could not read the input history: %s", type(exc).__name__)
            return []
        entries: list[str] = []
        for line in raw.splitlines():
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, str):
                entries.append(value)
        return entries[-self.limit :]

    def load_history_strings(self) -> Iterable[str]:
        self._entries = self._read()
        return list(reversed(self._entries))  # newest first, as prompt_toolkit expects

    def store_string(self, string: str) -> None:
        if self._entries and self._entries[-1] == string:
            return
        self._entries = [*self._entries, string][-self.limit :]
        text = "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in self._entries)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fsio.atomic_write_text(self.path, text, mode=0o600)
        except OSError as exc:
            log.warning("could not save the input history: %s", type(exc).__name__)
