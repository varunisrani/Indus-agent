"""The batch checkpoint, bound to the dataset hash (P22-05).

``<run dir>/checkpoint.json`` records which prompt indices are done and what each batch did.
Indices only mean something for the dataset they were counted on, so the file carries the dataset
hash: a run whose dataset hash differs is refused (``DatasetChanged``, the CLI exits 2) instead of
skipping rows of another file (R10.3, D-16.31). ``open_checkpoint(..., new_dataset=True)`` moves
the old run directory's contents to ``previous-<stamp>/`` and starts fresh instead.

Loading: a missing file is an empty checkpoint; an unreadable one is empty too, with a warning, and
the bad file is kept as ``checkpoint.json.bad-<stamp>``; a file for another run name is ignored.

Only the parent process writes it, atomically: a temp file in the same directory is written,
flushed and fsynced, then renamed over the old one (and the directory fsynced, best effort). A
crash at any point leaves either the old file or the new one, never a torn one.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

__all__ = [
    "CHECKPOINT_NAME",
    "FORMAT",
    "BatchStat",
    "Checkpoint",
    "DatasetChanged",
    "archive_run",
    "load_checkpoint",
    "open_checkpoint",
    "save_checkpoint",
    "stamp_of",
]

FORMAT = 1
CHECKPOINT_NAME = "checkpoint.json"
ARCHIVE_PREFIX = "previous-"

type Warn = Callable[[str], None]


class DatasetChanged(Exception):
    """The run directory belongs to another dataset (another hash)."""

    def __init__(self, run_dir: Path, old: str, new: str) -> None:
        self.run_dir = run_dir
        self.old = old
        self.new = new
        super().__init__(
            f"the dataset changed since run {run_dir.name!r} started (checkpoint hash {old}, "
            f"dataset hash now {new}); use another --run-name, or --new-dataset to archive the "
            "old run and start again"
        )


@dataclass
class BatchStat:
    processed: int = 0
    skipped: int = 0
    discarded: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"processed": self.processed, "skipped": self.skipped, "discarded": self.discarded}


@dataclass
class Checkpoint:
    run_name: str
    dataset_hash: str
    distribution: str
    seed: int
    completed: set[int] = field(default_factory=set[int])
    batch_stats: dict[str, BatchStat] = field(default_factory=dict[str, BatchStat])
    updated_at: str | None = None

    def record(self) -> dict[str, Any]:
        return {
            "format": FORMAT,
            "run_name": self.run_name,
            "dataset_hash": self.dataset_hash,
            "distribution": self.distribution,
            "seed": self.seed,
            "completed": sorted(self.completed),
            "batch_stats": {
                key: self.batch_stats[key].as_dict()
                for key in sorted(self.batch_stats, key=_batch_order)
            },
            "updated_at": self.updated_at,
        }


def _batch_order(key: str) -> tuple[int, str]:
    return (int(key), key) if key.isdigit() else (1 << 62, key)


def stamp_of(now: datetime) -> str:
    return now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _parse(data: object) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    found = cast(dict[str, Any], data)
    if found.get("format") != FORMAT:
        raise ValueError(f"unknown checkpoint format {found.get('format')!r}")
    for key, kind in (("run_name", str), ("dataset_hash", str), ("completed", list)):
        if not isinstance(found.get(key), kind):
            raise ValueError(f"'{key}' is missing or of the wrong type")
    return found


def _stats(raw: object) -> dict[str, BatchStat]:
    out: dict[str, BatchStat] = {}
    if not isinstance(raw, dict):
        return out
    for key, value in cast(dict[str, Any], raw).items():
        if isinstance(value, dict):
            item = cast(dict[str, Any], value)
            out[str(key)] = BatchStat(
                int(item.get("processed", 0)),
                int(item.get("skipped", 0)),
                int(item.get("discarded", 0)),
            )
    return out


def _free_name(path: Path) -> Path:
    if not path.exists():
        return path
    n = 2
    while path.with_name(f"{path.name}-{n}").exists():
        n += 1
    return path.with_name(f"{path.name}-{n}")


def load_checkpoint(
    run_dir: Path,
    *,
    run_name: str,
    dataset_hash: str,
    distribution: str,
    seed: int,
    stamp: str,
    warn: Warn,
) -> Checkpoint:
    """The checkpoint of ``run_dir`` for this run (see the module notes)."""
    fresh = Checkpoint(run_name, dataset_hash, distribution, seed)
    path = run_dir / CHECKPOINT_NAME
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return fresh
    except OSError as exc:
        warn(f"cannot read {path}: {exc.strerror or exc}; starting an empty checkpoint")
        return fresh
    try:
        data = _parse(json.loads(text))
        completed = {int(i) for i in cast(list[Any], data["completed"])}
    except (ValueError, TypeError) as exc:
        keep = _free_name(path.with_name(f"{CHECKPOINT_NAME}.bad-{stamp}"))
        with contextlib.suppress(OSError):
            path.replace(keep)
        warn(f"checkpoint {path} is unreadable ({exc}); kept as {keep.name}, starting empty")
        return fresh
    if data["run_name"] != run_name:
        warn(f"checkpoint {path} belongs to run {data['run_name']!r}; starting empty")
        return fresh
    if data["dataset_hash"] != dataset_hash:
        raise DatasetChanged(run_dir, str(data["dataset_hash"]), dataset_hash)
    for key, now_value in (("distribution", distribution), ("seed", seed)):
        if key in data and data[key] != now_value:
            warn(f"this run uses {key} {now_value!r}; the checkpoint was made with {data[key]!r}")
    updated = data.get("updated_at")
    return Checkpoint(
        run_name,
        dataset_hash,
        distribution,
        seed,
        completed=completed,
        batch_stats=_stats(data.get("batch_stats")),
        updated_at=updated if isinstance(updated, str) else None,
    )


def archive_run(run_dir: Path, stamp: str) -> Path:
    """Move everything in ``run_dir`` (earlier archives excepted) to ``previous-<stamp>/``."""
    target = _free_name(run_dir / f"{ARCHIVE_PREFIX}{stamp}")
    target.mkdir(parents=True)
    for entry in sorted(run_dir.iterdir()):
        if entry == target or entry.name.startswith(ARCHIVE_PREFIX):
            continue
        shutil.move(str(entry), str(target / entry.name))
    return target


def open_checkpoint(
    run_dir: Path,
    *,
    run_name: str,
    dataset_hash: str,
    distribution: str,
    seed: int,
    now: datetime,
    warn: Warn,
    new_dataset: bool = False,
) -> Checkpoint:
    """``load_checkpoint``; with ``new_dataset`` a hash mismatch archives the run and starts
    fresh instead of raising ``DatasetChanged``."""
    stamp = stamp_of(now)
    values: Mapping[str, Any] = {
        "run_name": run_name,
        "dataset_hash": dataset_hash,
        "distribution": distribution,
        "seed": seed,
        "stamp": stamp,
        "warn": warn,
    }
    try:
        return load_checkpoint(run_dir, **values)
    except DatasetChanged:
        if not new_dataset:
            raise
    moved = archive_run(run_dir, stamp)
    warn(f"--new-dataset: the earlier run's files were moved to {moved}")
    return Checkpoint(run_name, dataset_hash, distribution, seed)


def _fsync_dir(path: Path) -> None:
    if os.name == "nt":
        return
    with contextlib.suppress(OSError):
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def save_checkpoint(
    run_dir: Path,
    checkpoint: Checkpoint,
    *,
    now: datetime,
    replace: Callable[[Path, Path], None] = os.replace,
) -> Path:
    """Write ``checkpoint`` atomically (see the module notes); returns its path."""
    checkpoint.updated_at = now.astimezone(UTC).isoformat()
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / CHECKPOINT_NAME
    temp = run_dir / f".{CHECKPOINT_NAME}.{os.getpid()}.tmp"
    payload = json.dumps(checkpoint.record(), indent=2, ensure_ascii=False) + "\n"
    with open(temp, "w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        replace(temp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            temp.unlink()
        raise
    _fsync_dir(run_dir)
    for stale in run_dir.glob(f".{CHECKPOINT_NAME}.*.tmp"):  # left by a crashed writer
        with contextlib.suppress(OSError):
            stale.unlink()
    return path
