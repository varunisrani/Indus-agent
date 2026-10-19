"""Durable batch files, tombstones and the resume scan (P22-06).

A finished prompt is one JSON line in ``batch_<N>.jsonl`` of the run directory. The order of
effects is fixed (R10.1): the line is written, flushed and fsynced; only when ``append_durable``
returns normally may the caller mark the prompt complete, and only after that does the parent
write the checkpoint. A crash anywhere before the fsync leaves the prompt not done, so the next run
pays for it once more but never loses an acknowledged sample.

A prompt dropped on purpose (``DiscardReason``; ``no_reasoning`` today) still leaves a line, a
``Tombstone``, so a resumed run does not run it again (R10.2); the merge leaves tombstones out.

``scan_done_prompts`` reads every batch file and collects the trimmed prompt text of every row and
tombstone; ``remaining`` drops the dataset rows whose prompt is in that set (content-based resume,
on top of the checkpoint's indices). A line that is not JSON is skipped with a warning (the last
line of a file without its newline is reported as a torn write).
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

__all__ = [
    "BATCH_GLOB",
    "BatchRow",
    "DiscardReason",
    "Tombstone",
    "append_durable",
    "batch_files",
    "batch_path",
    "entry_prompt_text",
    "read_batch_file",
    "remaining",
    "scan_done_prompts",
]

BATCH_GLOB = "batch_*.jsonl"
_BATCH_NAME = re.compile(r"^batch_(\d+)\.jsonl$")

type Warn = Callable[[str], None]


class DiscardReason(StrEnum):
    """Why a finished prompt was dropped on purpose (each one leaves a tombstone)."""

    NO_REASONING = "no_reasoning"


@dataclass(frozen=True)
class BatchRow:
    prompt_index: int
    conversations: list[dict[str, str]]
    metadata: Mapping[str, Any]
    completed: bool
    partial: bool
    api_calls: int
    tool_stats: Mapping[str, Mapping[str, int]]
    tool_error_counts: Mapping[str, int]
    reasoning: Mapping[str, int] = field(default_factory=dict[str, int])

    def record(self) -> dict[str, Any]:
        return {
            "prompt_index": self.prompt_index,
            "conversations": self.conversations,
            "metadata": dict(self.metadata),
            "completed": self.completed,
            "partial": self.partial,
            "api_calls": self.api_calls,
            "tool_stats": {k: dict(v) for k, v in self.tool_stats.items()},
            "tool_error_counts": dict(self.tool_error_counts),
            "reasoning": dict(self.reasoning),
        }


@dataclass(frozen=True)
class Tombstone:
    prompt_index: int
    discarded: DiscardReason
    prompt: str

    def record(self) -> dict[str, Any]:
        return {
            "prompt_index": self.prompt_index,
            "discarded": str(self.discarded),
            "prompt": self.prompt,
        }


def batch_path(run_dir: Path, batch: int) -> Path:
    return run_dir / f"batch_{batch}.jsonl"


def batch_files(run_dir: Path) -> list[Path]:
    """Every ``batch_<N>.jsonl`` in ``run_dir``, by N."""
    found: list[tuple[int, Path]] = []
    for path in run_dir.glob(BATCH_GLOB):
        match = _BATCH_NAME.match(path.name)
        if match is not None and path.is_file():
            found.append((int(match.group(1)), path))
    return [path for _, path in sorted(found)]


def _ends_open(handle: Any) -> bool:
    """True when the file has bytes and the last one is not a newline (a torn write)."""
    handle.seek(0, os.SEEK_END)
    size = handle.tell()
    if size == 0:
        return False
    handle.seek(size - 1)
    return handle.read(1) != b"\n"


def append_durable(
    path: Path, record: Mapping[str, Any], *, fsync: Callable[[int], None] = os.fsync
) -> None:
    """Append ``record`` as one line and fsync it (see the module notes). An exception means the
    line may not be on disk: the caller must not count the prompt as done."""
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with open(path, "a+b") as handle:
        prefix = b"\n" if _ends_open(handle) else b""
        handle.write(prefix + line.encode("utf-8"))
        handle.flush()
        fsync(handle.fileno())


def read_batch_file(path: Path, warn: Warn) -> Iterator[dict[str, Any]]:
    """The JSON objects of one batch file; bad lines are skipped with a warning."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        warn(f"cannot read {path.name}: {exc.strerror or exc}")
        return
    lines = data.split(b"\n")
    torn_last = bool(lines and lines[-1])
    for number, raw in enumerate(lines, 1):
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except ValueError:
            last = number == len(lines) and torn_last
            what = "an unfinished last line (torn write)" if last else "not valid JSON"
            warn(f"{path.name} line {number}: {what}; skipped")
            continue
        if isinstance(value, dict):
            yield cast(dict[str, Any], value)


def entry_prompt_text(entry: Mapping[str, Any]) -> str:
    """The prompt of a row or tombstone: flat ``prompt``, else the first human/user turn of
    ``conversations`` or ``messages`` (``value`` or ``content``), trimmed."""
    flat = entry.get("prompt")
    if isinstance(flat, str) and flat.strip():
        return flat.strip()
    for key in ("conversations", "messages"):
        turns = entry.get(key)
        if not isinstance(turns, list):
            continue
        for turn in cast(list[Any], turns):
            if not isinstance(turn, Mapping):
                continue
            item = cast(Mapping[str, Any], turn)
            if (item.get("from") or item.get("role")) in ("human", "user"):
                text = item.get("value", item.get("content"))
                if isinstance(text, str) and text.strip():
                    return text.strip()
    return ""


def scan_done_prompts(run_dir: Path, warn: Warn) -> set[str]:
    """Trimmed prompt texts of every row and tombstone in the run's batch files."""
    done: set[str] = set()
    for path in batch_files(run_dir):
        for entry in read_batch_file(path, warn):
            if entry.get("failed"):
                continue
            text = entry_prompt_text(entry)
            if text:
                done.add(text)
    return done


def remaining[T](
    pairs: Sequence[tuple[int, T]], done: set[str], prompt_of: Callable[[T], str]
) -> tuple[list[tuple[int, T]], list[int]]:
    """``(pairs still to run, indices skipped)`` given the done prompt texts."""
    left: list[tuple[int, T]] = []
    skipped: list[int] = []
    for index, row in pairs:
        if prompt_of(row).strip() in done:
            skipped.append(index)
        else:
            left.append((index, row))
    return left, skipped
