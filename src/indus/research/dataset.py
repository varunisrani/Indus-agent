"""The prompt dataset of a batch run, and its hash (P22-01).

A dataset is a JSONL file. A line that is a JSON object with a non-empty text ``prompt`` becomes a
``DatasetRow``; ``image`` (or ``container_image``) and ``cwd`` are optional, other keys ride along
in ``data``. Blank lines are skipped quietly; any other line that does not fit is skipped with a
warning naming its line number (``warning_lines`` shows the first 20, then a count). A missing or
unreadable file, or a file with no usable row, is a ``DatasetError`` (the CLI exits 2).

Rows keep their index among the kept rows; ``max_samples`` keeps the first N of them.

``dataset_hash`` fingerprints what a run will actually process: SHA-256 over the canonical JSON
(sorted keys, no spaces) of every kept row in order, one per line, then the ``max_samples`` value.
Reformatting the file leaves it unchanged; editing, adding, removing or moving a row changes it.
The checkpoint (P22-05) is bound to it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

__all__ = [
    "WARNING_LIMIT",
    "Dataset",
    "DatasetError",
    "DatasetRow",
    "canonical_json",
    "chunk",
    "dataset_hash",
    "load_dataset",
    "warning_lines",
]

WARNING_LIMIT = 20
IMAGE_KEYS = ("image", "container_image")


class DatasetError(Exception):
    """The dataset cannot be used (missing, unreadable, or nothing usable in it)."""


@dataclass(frozen=True)
class DatasetRow:
    index: int
    prompt: str
    image: str | None = None
    cwd: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict[str, Any])


@dataclass(frozen=True)
class Dataset:
    path: Path
    rows: tuple[DatasetRow, ...]
    max_samples: int | None
    hash: str
    skipped: tuple[str, ...] = ()

    def pairs(self) -> list[tuple[int, DatasetRow]]:
        return [(row.index, row) for row in self.rows]


def canonical_json(value: Any) -> str:
    """Sorted keys, no spaces, non-ASCII kept: the same object always gives the same text."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def dataset_hash(rows: Iterable[DatasetRow], max_samples: int | None) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(canonical_json(dict(row.data)).encode("utf-8"))
        digest.update(b"\n")
    digest.update(f"max_samples={'none' if max_samples is None else max_samples}".encode())
    return digest.hexdigest()


def _text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _row(index: int, data: dict[str, Any]) -> DatasetRow:
    image = next((found for key in IMAGE_KEYS if (found := _text(data.get(key)))), None)
    return DatasetRow(
        index=index,
        prompt=str(data["prompt"]),
        image=image.strip() if image is not None else None,
        cwd=_text(data.get("cwd")),
        data=data,
    )


def _problem(line: str) -> tuple[dict[str, Any] | None, str | None]:
    """The row object of one line, or the reason it is skipped."""
    try:
        value = json.loads(line)
    except json.JSONDecodeError as exc:
        return None, f"not valid JSON ({exc.msg})"
    except ValueError:
        return None, "not valid JSON"
    if not isinstance(value, dict):
        return None, "not a JSON object"
    data = cast(dict[str, Any], value)
    if "prompt" not in data:
        return None, "no 'prompt' field"
    if _text(data["prompt"]) is None:
        return None, "'prompt' is not a non-empty text"
    return data, None


def load_dataset(path: Path | str, *, max_samples: int | None = None) -> Dataset:
    """Read ``path`` (see the module notes)."""
    source = Path(path)
    try:
        raw = source.read_bytes()
    except FileNotFoundError:
        raise DatasetError(f"dataset file not found: {source}") from None
    except OSError as exc:
        raise DatasetError(f"cannot read the dataset {source}: {exc.strerror or exc}") from None
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DatasetError(f"{source} is not UTF-8 text (byte {exc.start})") from None
    rows: list[DatasetRow] = []
    skipped: list[str] = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        data, reason = _problem(line)
        if data is None:
            skipped.append(f"line {number}: {reason}; skipped")
            continue
        rows.append(_row(len(rows), data))
    if not rows:
        raise DatasetError(f"no usable rows in {source} (each line needs a JSON 'prompt')")
    if max_samples is not None:
        rows = rows[: max(0, max_samples)]
        if not rows:
            raise DatasetError(f"--max-samples {max_samples} leaves nothing to run")
    return Dataset(
        path=source,
        rows=tuple(rows),
        max_samples=max_samples,
        hash=dataset_hash(rows, max_samples),
        skipped=tuple(skipped),
    )


def warning_lines(skipped: Sequence[str], limit: int = WARNING_LIMIT) -> list[str]:
    """The first ``limit`` warnings, then one line counting the rest."""
    lines = [f"warning: {text}" for text in skipped[:limit]]
    rest = len(skipped) - limit
    if rest > 0:
        lines.append(f"warning: … and {rest} more line(s) skipped")
    return lines


def chunk[T](items: Sequence[T], size: int) -> list[list[T]]:
    """Consecutive slices of ``size`` (the last one may be shorter)."""
    if size < 1:
        raise ValueError("batch size must be at least 1")
    return [list(items[start : start + size]) for start in range(0, len(items), size)]
