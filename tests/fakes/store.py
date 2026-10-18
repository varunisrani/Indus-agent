"""A transcript store wrapper that records commit order and fails on demand (P04-16).

``RecordingStore(inner)`` forwards to any ``TranscriptStore`` (the P02 adapter or ``MemoryStore``)
and appends one entry to ``log`` per committed write, named by its write point:

- ``session`` — the session row;
- ``user`` — a user row (``steer`` for a steer row);
- ``calls`` — an assistant row with tool calls;
- ``tool`` — a tool result row;
- ``final`` — an assistant row without tool calls.

``fail_at(point, nth=1, exc=...)`` makes the ``nth`` write at ``point`` raise instead of writing:
an ``Exception`` models a store error the loop must handle; ``Crash`` (a ``BaseException``)
models the process dying there — nothing after it runs. ``shared`` is an extra list other fakes
(tool side effects) append to, so one list shows the global order.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from indus.core.persist import TranscriptStore
from indus.core.transcript import TranscriptRow


class Crash(BaseException):
    """The process died at this point (never caught by the loop)."""


def point_of(row: TranscriptRow) -> str:
    if row.role == "user":
        return "steer" if row.is_steer else "user"
    if row.role == "assistant":
        return "calls" if row.tool_calls else "final"
    return row.role


@dataclass
class RecordingStore:
    inner: TranscriptStore
    log: list[str] = field(default_factory=list[str])
    shared: list[str] | None = None
    _fail: dict[str, tuple[int, BaseException]] = field(
        default_factory=dict[str, tuple[int, BaseException]]
    )
    _seen: dict[str, int] = field(default_factory=dict[str, int])

    def fail_at(self, point: str, nth: int = 1, exc: BaseException | None = None) -> None:
        self._fail[point] = (nth, exc if exc is not None else OSError(f"disk error at {point}"))

    def _check(self, point: str) -> None:
        self._seen[point] = self._seen.get(point, 0) + 1
        planned = self._fail.get(point)
        if planned is not None and planned[0] == self._seen[point]:
            raise planned[1]

    def _record(self, point: str) -> None:
        self.log.append(point)
        if self.shared is not None:
            self.shared.append(f"stored:{point}")

    async def ensure_session(
        self, session_id: str, *, source: str, system_prompt: str, fields: Mapping[str, Any]
    ) -> None:
        self._check("session")
        await self.inner.ensure_session(
            session_id, source=source, system_prompt=system_prompt, fields=fields
        )
        self._record("session")

    async def append(self, session_id: str, row: TranscriptRow) -> int:
        point = point_of(row)
        self._check(point)
        ident = await self.inner.append(session_id, row)
        self._record(point)
        return ident

    async def replace(
        self, session_id: str, rows: Sequence[TranscriptRow], *, expect_ids: Sequence[int]
    ) -> list[int]:
        self._check("replace")
        ids = await self.inner.replace(session_id, rows, expect_ids=expect_ids)
        self._record("replace")
        return ids

    async def load(self, session_id: str) -> list[TranscriptRow]:
        return await self.inner.load(session_id)
