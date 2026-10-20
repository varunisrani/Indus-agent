"""The prepared-submission journal of an attached client (P17-17).

Before a submit leaves for the daemon, the remote host writes what it is about to send here: one
private file per submission under ``<home>/state/attach-journal/`` (directory 0700, files 0600,
written atomically) with the session, the text and the admission request id. The entry is retired
when the daemon accepts the submission (or refuses it, or it never left). When the connection is
lost after the submit went out, the entry stays, marked ``unknown``: nobody knows whether the
daemon admitted it, so it is never resent on its own (R09.11). The person may retry it — with the
same request id, which the daemon's ledger answers with the first admission if there was one — or
discard it.

Blocking file work: call it off the event loop (``run_blocking``).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import secrets
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Final, Literal, cast

from indus.foundation import fsio

__all__ = [
    "JOURNAL_DIR",
    "MAX_LISTED",
    "Journal",
    "Prepared",
    "journal_dir",
    "new_request_id",
]

log = logging.getLogger(__name__)

JOURNAL_DIR: Final = "attach-journal"
MAX_LISTED: Final = 256
_ID: Final = re.compile(r"^sub-[0-9a-f]{32}$")

type EntryState = Literal["prepared", "unknown"]


def journal_dir(home: Path) -> Path:
    return home / "state" / JOURNAL_DIR


def new_request_id() -> str:
    """A fresh admission request id (128 random bits)."""
    return "sub-" + secrets.token_hex(16)


@dataclass(frozen=True)
class Prepared:
    """One journal entry. ``text`` is the person's own message; it stays out of ``repr``."""

    request_id: str
    session_id: str
    text: str = field(repr=False)
    state: EntryState = "prepared"
    created_at: float = 0.0

    def to_json(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "session_id": self.session_id,
            "text": self.text,
            "state": self.state,
            "created_at": self.created_at,
        }

    @classmethod
    def from_json(cls, data: object) -> Prepared | None:
        if not isinstance(data, dict):
            return None
        body = cast(dict[str, object], data)
        request_id, session_id, text = (
            body.get("request_id"),
            body.get("session_id"),
            body.get("text"),
        )
        state, created = body.get("state"), body.get("created_at")
        if not isinstance(request_id, str) or not _ID.match(request_id):
            return None
        if not isinstance(session_id, str) or not session_id or not isinstance(text, str):
            return None
        if state not in ("prepared", "unknown"):
            return None
        when = (
            float(created)
            if isinstance(created, int | float) and not isinstance(created, bool)
            else 0.0
        )
        return cls(request_id, session_id, text, state, when)


class Journal:
    """The journal of one profile home (see the module notes)."""

    def __init__(self, home: Path, *, wall: Callable[[], float] = time.time) -> None:
        self.home = home
        self.path = journal_dir(home)
        self._wall = wall

    def _file(self, request_id: str) -> Path:
        if not _ID.match(request_id):
            raise ValueError("not a journal request id")
        return self.path / f"{request_id}.json"

    def _ensure_dir(self) -> None:
        self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
        with contextlib.suppress(OSError):
            os.chmod(self.path, 0o700)

    def _write(self, entry: Prepared) -> None:
        self._ensure_dir()
        fsio.atomic_write_json(self._file(entry.request_id), entry.to_json(), mode=0o600)

    def prepare(self, session_id: str, text: str, *, request_id: str | None = None) -> Prepared:
        """Write a new entry before the submit is sent."""
        entry = Prepared(request_id or new_request_id(), session_id, text, "prepared", self._wall())
        self._write(entry)
        return entry

    def mark_unknown(self, request_id: str) -> Prepared | None:
        entry = self.get(request_id)
        if entry is None:
            return None
        changed = replace(entry, state="unknown")
        self._write(changed)
        return changed

    def retire(self, request_id: str) -> bool:
        """Remove an entry; False when there was none."""
        try:
            self._file(request_id).unlink()
        except FileNotFoundError:
            return False
        except ValueError:
            return False
        return True

    def get(self, request_id: str) -> Prepared | None:
        try:
            path = self._file(request_id)
        except ValueError:
            return None
        return _read(path)

    def pending(self, session_id: str | None = None) -> list[Prepared]:
        """Entries still here (oldest first, at most ``MAX_LISTED``); foreign or unreadable files
        are skipped."""
        try:
            names = sorted(p for p in self.path.iterdir() if p.suffix == ".json")
        except OSError:
            return []
        found: list[Prepared] = []
        for path in names:
            entry = _read(path)
            if entry is None or (session_id is not None and entry.session_id != session_id):
                continue
            found.append(entry)
        found.sort(key=lambda e: (e.created_at, e.request_id))
        return found[:MAX_LISTED]


def _read(path: Path) -> Prepared | None:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            return None
        if info.st_mode & 0o077:
            log.warning("ignoring a journal entry other users can read: %s", path.name)
            return None
        data: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entry = Prepared.from_json(data)
    if entry is None or f"{entry.request_id}.json" != path.name:
        return None
    return entry
