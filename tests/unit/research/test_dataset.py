"""P22-01: the dataset loader and the dataset hash."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from indus.research.dataset import (
    DatasetError,
    chunk,
    dataset_hash,
    load_dataset,
    warning_lines,
)

DATASETS = Path(__file__).resolve().parents[2] / "fixtures" / "research" / "datasets"


def write(path: Path, *lines: str) -> Path:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_valid_rows_keep_their_index_and_extra_keys() -> None:
    data = load_dataset(DATASETS / "valid.jsonl")
    assert [(r.index, r.prompt) for r in data.rows] == [
        (0, "What is 2 + 2?"),
        (1, "Name a prime above 10."),
        (2, "List three rivers."),
    ]
    assert data.rows[1].data["topic"] == "math"
    assert data.skipped == ()
    assert data.pairs()[2][0] == 2


def test_bad_lines_are_warned_with_their_line_numbers() -> None:
    data = load_dataset(DATASETS / "mixed.jsonl")
    assert [r.prompt for r in data.rows] == ["first good", "second good"]
    assert [r.index for r in data.rows] == [0, 1]
    assert [w.split(":")[0] for w in data.skipped] == [
        "line 3",
        "line 4",
        "line 5",
        "line 6",
        "line 7",
    ]
    assert "not valid JSON" in data.skipped[0]
    assert "not a JSON object" in data.skipped[1]
    assert "no 'prompt' field" in data.skipped[2]
    assert "non-empty text" in data.skipped[3]


def test_only_twenty_warnings_then_a_count(tmp_path: Path) -> None:
    path = write(tmp_path / "d.jsonl", *(["oops"] * 25), json.dumps({"prompt": "ok"}))
    data = load_dataset(path)
    lines = warning_lines(data.skipped)
    assert len(lines) == 21
    assert lines[0] == "warning: line 1: not valid JSON (Expecting value); skipped"
    assert lines[-1] == "warning: … and 5 more line(s) skipped"


def test_image_and_cwd_fields() -> None:
    rows = load_dataset(DATASETS / "images.jsonl").rows
    assert (rows[0].image, rows[0].cwd) == ("python:3.13-slim", "/work")
    assert (rows[1].image, rows[1].cwd) == ("alpine:3.20", None)
    assert rows[2].image is None


def test_empty_and_missing_files_are_dataset_errors(tmp_path: Path) -> None:
    with pytest.raises(DatasetError, match="not found"):
        load_dataset(tmp_path / "nope.jsonl")
    empty = write(tmp_path / "empty.jsonl", "", "   ")
    with pytest.raises(DatasetError, match="no usable rows"):
        load_dataset(empty)
    junk = write(tmp_path / "junk.jsonl", '{"q": 1}', "[]")
    with pytest.raises(DatasetError, match="no usable rows"):
        load_dataset(junk)
    binary = tmp_path / "bin.jsonl"
    binary.write_bytes(b'{"prompt": "\xff\xfe"}\n')
    with pytest.raises(DatasetError, match="not UTF-8"):
        load_dataset(binary)


def test_hash_is_stable_across_whitespace_and_key_order(tmp_path: Path) -> None:
    a = write(tmp_path / "a.jsonl", '{"prompt": "x", "k": 1}', '{"prompt": "y"}')
    b = write(tmp_path / "b.jsonl", '  {"k":1,   "prompt":"x"}  ', "", '{ "prompt" : "y" }')
    assert load_dataset(a).hash == load_dataset(b).hash


def test_hash_changes_with_a_prompt_or_a_moved_row(tmp_path: Path) -> None:
    base = load_dataset(write(tmp_path / "a.jsonl", '{"prompt": "x"}', '{"prompt": "y"}')).hash
    edited = load_dataset(write(tmp_path / "b.jsonl", '{"prompt": "x"}', '{"prompt": "z"}')).hash
    moved = load_dataset(write(tmp_path / "c.jsonl", '{"prompt": "y"}', '{"prompt": "x"}')).hash
    assert len({base, edited, moved}) == 3


def test_max_samples_keeps_the_first_rows_and_changes_the_hash() -> None:
    full = load_dataset(DATASETS / "valid.jsonl")
    two = load_dataset(DATASETS / "valid.jsonl", max_samples=2)
    assert [r.index for r in two.rows] == [0, 1]
    assert two.hash != full.hash
    # even a limit above the row count is part of what was asked for
    big = load_dataset(DATASETS / "valid.jsonl", max_samples=99)
    assert len(big.rows) == 3
    assert big.hash != full.hash
    assert big.hash == dataset_hash(full.rows, 99)


def test_chunk_keeps_order() -> None:
    assert chunk([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]
    with pytest.raises(ValueError):
        chunk([1], 0)
