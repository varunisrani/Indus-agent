# pyright: strict
"""What one task has read and written, for the read loop breaker and the stale-write rule.

One ``TaskReads`` per (profile home, task id) (R02.8), kept in the process. A *version* is the
sha256 of a file's bytes. It holds:

- ``dedup``: (path, offset, limit) → version last served in full, and ``served`` — the keys served
  in full since the last compaction boundary. An identical read of an unchanged file that was
  served in this generation gets a short stub instead of the content (R02.15).
- ``stub_hits``: stubs given per key; the second stub in a row becomes a refusal.
- ``last_key`` / ``consecutive``: identical reads in a row (warn at 3, refuse at 4). Any other
  tool call resets it (``note_other_tool``).
- ``baselines``: path → version this task has seen in full and unredacted (one page, or pages
  that together cover every line of that version), wrote itself, or patched from such a baseline.
  ``write_file`` needs one to replace an existing file (R02.14). Terminal writes never add one.
- ``blind_patches``: path → version a patch wrote without a baseline (for the refusal wording).
- ``coverage``: per path, the line ranges read at one version.
- ``not_found``: path → (time, answer) for 60 s.

Every container is capped (oldest entries go first).
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Literal

from indus.foundation.scope import Scope

__all__ = [
    "BASELINE_CAP",
    "DEDUP_CAP",
    "HISTORY_CAP",
    "NOT_FOUND_TTL_S",
    "RANGES_CAP",
    "ReadKey",
    "TaskReads",
    "compaction_boundary",
    "note_other_tool",
    "reset_all",
    "tracker_for",
]

HISTORY_CAP = 500
DEDUP_CAP = 1000
BASELINE_CAP = 1000
RANGES_CAP = 256
NOT_FOUND_CAP = 500
NOT_FOUND_TTL_S = 60.0
PATCH_FAILURE_CAP = 64
TASKS_CAP = 256
WARN_AT = 3
BLOCK_AT = 4

type ReadKey = tuple[str, int, int]
type DedupVerdict = Literal["stub", "block"]


def _trim[K, V](table: OrderedDict[K, V], cap: int) -> None:
    while len(table) > cap:
        table.popitem(last=False)


def _put[K, V](table: OrderedDict[K, V], key: K, value: V, cap: int) -> None:
    table.pop(key, None)
    table[key] = value
    _trim(table, cap)


@dataclass
class _Coverage:
    version: str
    ranges: list[tuple[int, int]] = field(default_factory=lambda: [])
    redacted: bool = False


class TaskReads:
    """The read and write record of one task (see the module notes). Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.last_key: ReadKey | None = None
        self.consecutive = 0
        self.history: OrderedDict[ReadKey, None] = OrderedDict()
        self.dedup: OrderedDict[ReadKey, str] = OrderedDict()
        self.served: OrderedDict[ReadKey, None] = OrderedDict()
        self.stub_hits: OrderedDict[ReadKey, int] = OrderedDict()
        self.baselines: OrderedDict[str, str] = OrderedDict()
        self.blind_patches: OrderedDict[str, str] = OrderedDict()
        self.coverage: OrderedDict[str, _Coverage] = OrderedDict()
        self.not_found: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self.patch_failures: OrderedDict[str, int] = OrderedDict()

    # -- the loop breaker --------------------------------------------------------------------------

    def dedup_verdict(self, key: ReadKey, version: str) -> tuple[DedupVerdict | None, int]:
        """``stub`` / ``block`` for a repeat of a read served in full this generation, with the
        number of times the region has now been asked for; ``(None, 0)`` to read for real."""
        with self._lock:
            if self.dedup.get(key) != version or key not in self.served:
                return None, 0
            hits = self.stub_hits.get(key, 0) + 1
            _put(self.stub_hits, key, hits, DEDUP_CAP)
            return ("block" if hits >= 2 else "stub"), hits + 1

    def record_read(
        self,
        key: ReadKey,
        version: str,
        *,
        first_line: int,
        last_line: int,
        total_lines: int,
        whole: bool,
        redacted: bool,
    ) -> int:
        """Bookkeeping after content was served; returns the consecutive count for ``key``."""
        path = key[0]
        with self._lock:
            self.stub_hits.pop(key, None)
            _put(self.history, key, None, HISTORY_CAP)
            if self.last_key == key:
                self.consecutive += 1
            else:
                self.last_key, self.consecutive = key, 1
            count = self.consecutive
            if count >= BLOCK_AT:
                self.dedup.pop(key, None)
                self.served.pop(key, None)
            else:
                _put(self.dedup, key, version, DEDUP_CAP)
                _put(self.served, key, None, DEDUP_CAP)
            complete = self.baselines.get(path) == version
            if not complete:
                complete = self._covered(
                    path, version, first_line, last_line, total_lines, redacted
                )
                complete = complete or (whole and not redacted)
            if complete:
                _put(self.baselines, path, version, BASELINE_CAP)
                self.blind_patches.pop(path, None)
            else:
                self.baselines.pop(path, None)
            return count

    def _covered(
        self, path: str, version: str, first: int, last: int, total: int, redacted: bool
    ) -> bool:
        entry = self.coverage.get(path)
        if entry is None or entry.version != version or len(entry.ranges) >= RANGES_CAP:
            entry = _Coverage(version)
        entry.redacted = entry.redacted or redacted
        merged: list[tuple[int, int]] = []
        for start, end in sorted([*entry.ranges, (first, last)]):
            if merged and start <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        entry.ranges = merged
        _put(self.coverage, path, entry, BASELINE_CAP)
        whole = total > 0 and merged[0][0] <= 1 and merged[0][1] >= total
        return whole and not entry.redacted

    def note_other_tool(self) -> None:
        with self._lock:
            self.last_key, self.consecutive = None, 0
            self.stub_hits.clear()
            self.not_found.clear()

    def compaction_boundary(self) -> None:
        """After compression the next read of each region returns full content again."""
        with self._lock:
            self.served.clear()
            self.stub_hits.clear()

    # -- not found ---------------------------------------------------------------------------------

    def remember_missing(self, path: str, now: float, answer: str) -> None:
        with self._lock:
            _put(self.not_found, path, (now, answer), NOT_FOUND_CAP)

    def cached_missing(self, path: str, now: float) -> str | None:
        with self._lock:
            hit = self.not_found.get(path)
            if hit is None:
                return None
            if now - hit[0] > NOT_FOUND_TTL_S:
                del self.not_found[path]
                return None
            return hit[1]

    def forget_missing(self, path: str) -> None:
        with self._lock:
            self.not_found.pop(path, None)

    # -- writes ------------------------------------------------------------------------------------

    def baseline(self, path: str) -> str | None:
        with self._lock:
            return self.baselines.get(path)

    def set_baseline(self, path: str, version: str) -> None:
        with self._lock:
            _put(self.baselines, path, version, BASELINE_CAP)
            self.blind_patches.pop(path, None)

    def drop_baseline(self, path: str) -> None:
        with self._lock:
            self.baselines.pop(path, None)

    def note_blind_patch(self, path: str, version: str) -> None:
        with self._lock:
            self.baselines.pop(path, None)
            _put(self.blind_patches, path, version, BASELINE_CAP)

    def blind_patch(self, path: str) -> str | None:
        with self._lock:
            return self.blind_patches.get(path)

    def after_write(self, path: str) -> None:
        """Forget served regions and misses of ``path`` so the next read shows the new bytes."""
        with self._lock:
            for key in [k for k in self.dedup if k[0] == path]:
                del self.dedup[key]
                self.served.pop(key, None)
                self.stub_hits.pop(key, None)
            self.not_found.pop(path, None)
            self.coverage.pop(path, None)
            self.last_key, self.consecutive = None, 0

    def patch_failed(self, path: str) -> int:
        with self._lock:
            count = self.patch_failures.get(path, 0) + 1
            _put(self.patch_failures, path, count, PATCH_FAILURE_CAP)
            return count

    def patch_succeeded(self, path: str) -> None:
        with self._lock:
            self.patch_failures.pop(path, None)


# -- the table of tasks ----------------------------------------------------------------------------

_TABLE_LOCK = threading.Lock()
_TABLE: OrderedDict[tuple[str, str], TaskReads] = OrderedDict()


def _key(scope: Scope | None, task_id: str | None) -> tuple[str, str]:
    return (str(scope.home) if scope is not None else "", task_id or "default")


def tracker_for(scope: Scope | None, task_id: str | None) -> TaskReads:
    key = _key(scope, task_id)
    with _TABLE_LOCK:
        found = _TABLE.get(key)
        if found is None:
            found = TaskReads()
        _put(_TABLE, key, found, TASKS_CAP)
        return found


def note_other_tool(scope: Scope | None, task_id: str | None) -> None:
    """Call after every tool other than ``read_file`` / ``search_files``."""
    with _TABLE_LOCK:
        found = _TABLE.get(_key(scope, task_id))
    if found is not None:
        found.note_other_tool()


def compaction_boundary(scope: Scope | None, task_id: str | None = None) -> None:
    """After context compression (P08): one task, or every task of the profile."""
    home = str(scope.home) if scope is not None else ""
    with _TABLE_LOCK:
        targets = [
            reads
            for (h, t), reads in _TABLE.items()
            if h == home and (task_id is None or t == task_id)
        ]
    for reads in targets:
        reads.compaction_boundary()


def reset_all() -> None:
    """Forget every task (tests)."""
    with _TABLE_LOCK:
        _TABLE.clear()
