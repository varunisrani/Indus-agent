"""Where the turn loop stores the transcript (R01.3).

``TranscriptStore`` is the small async seam the loop writes through: make sure the session row
exists (with its system prompt), append one row and wait until it is committed, replace the live
transcript after a repair, load it back. ``StateStore`` adapts the P02 store (writes go through
the writer thread with ``awrite``; reads run on a worker thread). ``MemoryStore`` keeps rows in a
list for sessions that are not saved and for tests.

The P02 ``messages`` table has no ``display_kind`` or ``scaffold`` column. Scaffold rows are never
stored, and a stored steer row is recognised again on load by its marker text.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Protocol

from indus.core.interrupts import is_steer_text
from indus.core.transcript import DISPLAY_STEER, TranscriptRow
from indus.state import messages, sessions
from indus.state.writer import TRANSCRIPT
from indus.tools.dispatch import run_sync

if TYPE_CHECKING:
    from indus.state.registry import StoreHandle

log = logging.getLogger(__name__)

__all__ = ["MemoryStore", "StateStore", "TranscriptStore", "row_from_store", "rows_from_store"]


class TranscriptStore(Protocol):
    async def ensure_session(
        self, session_id: str, *, source: str, system_prompt: str, fields: Mapping[str, Any]
    ) -> None:
        """Create the session row if missing; fill only its empty fields otherwise."""
        ...

    async def append(self, session_id: str, row: TranscriptRow) -> int:
        """Store ``row`` at the end of the live transcript; returns once it is committed."""
        ...

    async def replace(
        self, session_id: str, rows: Sequence[TranscriptRow], *, expect_ids: Sequence[int]
    ) -> list[int]:
        """Swap the live transcript for ``rows`` (soft: unchanged leading rows are kept)."""
        ...

    async def load(self, session_id: str) -> list[TranscriptRow]:
        """The live transcript, oldest first."""
        ...


def row_from_store(stored: Mapping[str, Any]) -> TranscriptRow:
    row = TranscriptRow.from_store(stored)
    if row.role == "user" and row.display_kind is None and is_steer_text(row.content):
        row.display_kind = DISPLAY_STEER
    return row


def rows_from_store(stored: Sequence[Mapping[str, Any]]) -> list[TranscriptRow]:
    return [row_from_store(r) for r in stored]


class StateStore:
    """``TranscriptStore`` over a P02 ``StoreHandle``."""

    def __init__(self, handle: StoreHandle) -> None:
        self.handle = handle

    def _now(self) -> float:
        return self.handle.scope.clock.now().timestamp()

    async def ensure_session(
        self, session_id: str, *, source: str, system_prompt: str, fields: Mapping[str, Any]
    ) -> None:
        values = {k: v for k, v in fields.items() if k in sessions.CREATE_FIELDS}
        values.setdefault("profile_id", self.handle.scope.profile_id)
        if system_prompt:
            values["system_prompt"] = system_prompt
        now = self._now()
        await self.handle.awrite(
            lambda conn: sessions.create_in(
                conn, session_id, source or "unknown", now=now, **values
            )
        )

    async def append(self, session_id: str, row: TranscriptRow) -> int:
        data = row.to_store()
        now = self._now()
        return await self.handle.awrite(
            lambda conn: messages.append_in(conn, session_id, data, now=now), budget=TRANSCRIPT
        )

    async def replace(
        self, session_id: str, rows: Sequence[TranscriptRow], *, expect_ids: Sequence[int]
    ) -> list[int]:
        data = [r.to_store() for r in rows]
        now = self._now()
        return await self.handle.awrite(
            lambda conn: messages.replace_in(
                conn, session_id, data, now=now, soft=True, expect_active_ids=list(expect_ids)
            ),
            budget=TRANSCRIPT,
        )

    async def load(self, session_id: str) -> list[TranscriptRow]:
        stored = await run_sync(lambda: messages.get(self.handle, session_id), None)
        return rows_from_store(stored)


class MemoryStore:
    """An in-process store: rows kept as copies, ids counted from 1."""

    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}
        self.rows: dict[str, list[TranscriptRow]] = {}
        self._next_id = 1

    async def ensure_session(
        self, session_id: str, *, source: str, system_prompt: str, fields: Mapping[str, Any]
    ) -> None:
        current = self.sessions.setdefault(session_id, {"source": source})
        if system_prompt and not current.get("system_prompt"):
            current["system_prompt"] = system_prompt
        for key, value in fields.items():
            current.setdefault(key, value)

    async def append(self, session_id: str, row: TranscriptRow) -> int:
        stored = row.clone()
        stored.row_id = self._next_id
        stored.scaffold = False
        self._next_id += 1
        self.rows.setdefault(session_id, []).append(stored)
        return stored.row_id

    async def replace(
        self, session_id: str, rows: Sequence[TranscriptRow], *, expect_ids: Sequence[int]
    ) -> list[int]:
        live = self.rows.get(session_id, [])
        if [r.row_id for r in live] != list(expect_ids):
            raise messages.TranscriptMoved(f"the live transcript of {session_id} changed")
        fresh: list[TranscriptRow] = []
        for row in rows:
            stored = row.clone()
            stored.row_id = self._next_id
            self._next_id += 1
            fresh.append(stored)
        self.rows[session_id] = fresh
        return [r.row_id for r in fresh if r.row_id is not None]

    async def load(self, session_id: str) -> list[TranscriptRow]:
        return [copy.deepcopy(r) for r in self.rows.get(session_id, [])]
