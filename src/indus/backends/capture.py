# pyright: strict
"""Keeping a command's output: bounded for the model, whole for everyone else (P05-02).

The foreground terminal path shows the model at most ``tool_output.max_chars`` characters: the
first 40 % and the last 60 % of the stream, with a note in between. The moment the stream first
overflows, the full text (from its first character) is also written to
``<home>/cache/terminal-output/out-<ts>-<pid>-<id>.log`` so nothing is lost; the result then names
that file. Every internal caller — file reads through the shell, log reads, code-execution
requests — must use the unbounded capture: a cut there silently corrupts data (R02.2).
"""

from __future__ import annotations

import contextlib
import itertools
import logging
import os
import stat
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

__all__ = [
    "DEFAULT_MAX_CHARS",
    "SPILL_CAP_CHARS",
    "SPILL_MAX_AGE_S",
    "OutputCapture",
    "bounded_capture",
    "prune_spills",
    "spill_dir",
    "unbounded_capture",
]

log = logging.getLogger(__name__)

DEFAULT_MAX_CHARS = 50_000
SPILL_CAP_CHARS = 5_000_000
SPILL_MAX_AGE_S = 7 * 24 * 3600
HEAD_SHARE = 0.4

_SPILL_CAPPED_NOTE = "\n[full output file stopped at {cap:,} characters]\n"
_ids = itertools.count(1)


def _notice(omitted: int, total: int) -> str:
    return f"\n\n[... {omitted:,} of {total:,} characters left out here ...]\n\n"


def _open_private_exclusive(path: Path) -> TextIO:
    """Create ``path`` new (never through a symlink), owner-only, for text writing."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        return os.fdopen(fd, "w", encoding="utf-8", errors="replace", newline="")
    except BaseException:
        os.close(fd)
        raise


class OutputCapture:
    """Collects streamed text; bounded (``max_chars``) or unbounded (``None``). Thread-safe."""

    def __init__(self, max_chars: int | None, spill_path: Path | None = None) -> None:
        self.max_chars = None if max_chars is None else max(1, int(max_chars))
        self._lock = threading.Lock()
        self._total = 0
        self._spill_path = spill_path if self.max_chars is not None else None
        self._spill: TextIO | None = None
        self._spilled = 0
        self._spill_done = False
        self._spill_used = False
        self._all: list[str] = []  # unbounded only
        self._head_cap = 0 if self.max_chars is None else int(self.max_chars * HEAD_SHARE)
        self._tail_cap = 0 if self.max_chars is None else self.max_chars - self._head_cap
        self._head: list[str] = []
        self._head_len = 0
        self._tail: deque[str] = deque()
        self._tail_len = 0

    @property
    def bounded(self) -> bool:
        return self.max_chars is not None

    @property
    def total_chars(self) -> int:
        with self._lock:
            return self._total

    def append(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            if self.max_chars is None:
                self._all.append(text)
                self._total += len(text)
                return
            if self._spill_path is not None and (
                self._spill is not None or self._total + len(text) > self.max_chars
            ):
                self._tee(text)
            self._total += len(text)
            self._keep(text)

    def _keep(self, text: str) -> None:
        taken = 0
        if self._head_len < self._head_cap:
            taken = min(self._head_cap - self._head_len, len(text))
            self._head.append(text[:taken])
            self._head_len += taken
        rest = text[taken:]
        if not rest or self._tail_cap <= 0:
            return
        if len(rest) >= self._tail_cap:
            self._tail.clear()
            self._tail.append(rest[-self._tail_cap :])
            self._tail_len = self._tail_cap
            return
        self._tail.append(rest)
        self._tail_len += len(rest)
        while self._tail_len > self._tail_cap:
            extra = self._tail_len - self._tail_cap
            first = self._tail[0]
            if len(first) <= extra:
                self._tail.popleft()
                self._tail_len -= len(first)
            else:
                self._tail[0] = first[extra:]
                self._tail_len -= extra

    def _tee(self, text: str) -> None:
        if self._spill_done or self._spill_path is None:
            return
        try:
            if self._spill is None:
                self._spill_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                self._spill = _open_private_exclusive(self._spill_path)
                self._spill_used = True
                # Nothing was dropped yet: what is kept is the stream so far.
                backlog = "".join(self._head) + "".join(self._tail)
                self._spill.write(backlog)
                self._spilled = len(backlog)
            room = SPILL_CAP_CHARS - self._spilled
            if len(text) > room:
                self._spill.write(text[: max(0, room)])
                self._spill.write(_SPILL_CAPPED_NOTE.format(cap=SPILL_CAP_CHARS))
                self._spilled = SPILL_CAP_CHARS
                self._spill_done = True
                self._close_file()
            else:
                self._spill.write(text)
                self._spilled += len(text)
        except OSError as exc:  # a full or hostile disk never breaks the command
            log.warning("could not keep the full command output in %s: %s", self._spill_path, exc)
            self._spill_done = True
            self._close_file()

    def _close_file(self) -> None:
        if self._spill is not None:
            with contextlib.suppress(OSError):
                self._spill.close()
            self._spill = None

    def close_spill(self) -> str | None:
        """Close the full-output file; its path when one was written."""
        with self._lock:
            self._close_file()
            if self._spill_used and self._spill_path is not None:
                return str(self._spill_path)
            return None

    def render(self, *, suffix: str = "") -> str:
        """The kept text (with the cut note when something was left out) plus ``suffix``; for a
        bounded capture the whole result is at most ``max_chars`` long."""
        with self._lock:
            if self.max_chars is None:
                return "".join(self._all) + suffix
            limit = self.max_chars
            if len(suffix) >= limit:
                return suffix[-limit:]
            head = "".join(self._head)
            tail = "".join(self._tail)
            room = limit - len(suffix)
            if self._total <= room:
                return head + tail + suffix
            # Size the note for the largest number it can show, so the cut below never grows it.
            budget = max(0, room - len(_notice(self._total, self._total)))
            head_part = int(budget * HEAD_SHARE)
            tail_part = budget - head_part
            note = _notice(self._total - budget, self._total)
            kept_tail = tail[-tail_part:] if tail_part else ""
            return (head[:head_part] + note + kept_tail)[:room] + suffix


def spill_dir(home: Path) -> Path:
    return home / "cache" / "terminal-output"


def prune_spills(directory: Path, *, now: float, max_age_s: float = SPILL_MAX_AGE_S) -> int:
    """Delete ``out-*.log`` files older than ``max_age_s``; returns how many went."""
    removed = 0
    try:
        candidates = list(directory.glob("out-*.log"))
    except OSError:
        return 0
    for path in candidates:
        try:
            info = path.lstat()
            if stat.S_ISREG(info.st_mode) and now - info.st_mtime > max_age_s:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def bounded_capture(
    home: Path | None,
    max_chars: int = DEFAULT_MAX_CHARS,
    *,
    wall_time: Callable[[], float] | None = None,
) -> OutputCapture:
    """A capture for the model-facing foreground path; overflow goes to a spill file under
    ``home`` (none when ``home`` is None). Old spill files are pruned here."""
    if home is None:
        return OutputCapture(max_chars)
    now = (wall_time or time.time)()
    directory = spill_dir(home)
    if directory.is_dir():
        prune_spills(directory, now=now)
    name = f"out-{int(now)}-{os.getpid()}-{next(_ids):x}.log"
    return OutputCapture(max_chars, spill_path=directory / name)


def unbounded_capture() -> OutputCapture:
    """Every character kept: for internal readers."""
    return OutputCapture(None)
