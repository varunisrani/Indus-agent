"""P22-06: durable batch rows, tombstones and the resume scan."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from indus.research.batch_io import (
    BatchRow,
    DiscardReason,
    Tombstone,
    append_durable,
    batch_files,
    batch_path,
    entry_prompt_text,
    read_batch_file,
    remaining,
    scan_done_prompts,
)


def row(index: int, prompt: str) -> BatchRow:
    return BatchRow(
        prompt_index=index,
        conversations=[{"from": "system", "value": "s"}, {"from": "human", "value": prompt}],
        metadata={"batch": 0},
        completed=True,
        partial=False,
        api_calls=1,
        tool_stats={"terminal": {"count": 0, "success": 0, "failure": 0}},
        tool_error_counts={"terminal": 0},
    )


def test_fsync_happens_before_the_completion_callback(tmp_path: Path) -> None:
    events: list[str] = []
    path = batch_path(tmp_path, 0)

    def spy_fsync(fd: int) -> None:
        events.append("fsync")

    append_durable(path, row(0, "q").record(), fsync=spy_fsync)
    events.append("complete")
    assert events == ["fsync", "complete"]
    assert json.loads(path.read_text())["prompt_index"] == 0


def test_a_crash_before_the_fsync_leaves_the_prompt_not_done(tmp_path: Path) -> None:
    completed: list[int] = []
    path = batch_path(tmp_path, 0)

    def crash(fd: int) -> None:
        raise OSError("killed before the disk caught up")

    def worker_step(index: int) -> None:
        append_durable(path, row(index, "q").record(), fsync=crash)
        completed.append(index)  # only reached after a durable write

    with pytest.raises(OSError):
        worker_step(7)
    assert completed == []


def test_tombstoned_prompt_is_not_run_again(tmp_path: Path) -> None:
    append_durable(
        batch_path(tmp_path, 1),
        Tombstone(4, DiscardReason.NO_REASONING, "  dropped question ").record(),
    )
    append_durable(batch_path(tmp_path, 1), row(5, "kept question").record())
    done = scan_done_prompts(tmp_path, lambda _t: None)
    assert done == {"dropped question", "kept question"}
    pairs = [(3, "fresh"), (4, "dropped question"), (5, "kept question ")]
    left, skipped = remaining(pairs, done, lambda p: p)
    assert left == [(3, "fresh")] and skipped == [4, 5]


def test_torn_last_line_is_skipped_with_a_warning_and_next_append_is_clean(
    tmp_path: Path,
) -> None:
    path = batch_path(tmp_path, 2)
    append_durable(path, row(0, "one").record())
    with open(path, "ab") as handle:
        handle.write(b'{"prompt_index": 1, "conversa')  # the process died mid-write
    warnings: list[str] = []
    assert [e["prompt_index"] for e in read_batch_file(path, warnings.append)] == [0]
    assert warnings == ["batch_2.jsonl line 2: an unfinished last line (torn write); skipped"]
    append_durable(path, row(2, "three").record())
    warnings.clear()
    got = [e["prompt_index"] for e in read_batch_file(path, warnings.append)]
    assert got == [0, 2]
    assert warnings == ["batch_2.jsonl line 2: not valid JSON; skipped"]


def test_scan_reads_every_batch_file_and_both_shapes(tmp_path: Path) -> None:
    (tmp_path / "batch_10.jsonl").write_text(
        json.dumps({"messages": [{"role": "user", "content": "chat shape"}]}) + "\n"
    )
    (tmp_path / "batch_2.jsonl").write_text(
        json.dumps({"conversations": [{"from": "human", "value": "sharegpt shape"}]})
        + "\n"
        + json.dumps({"prompt": "failed one", "failed": True})
        + "\n[1, 2]\n"
    )
    (tmp_path / "batch_x.jsonl").write_text(json.dumps({"prompt": "not a batch file"}) + "\n")
    assert [p.name for p in batch_files(tmp_path)] == ["batch_2.jsonl", "batch_10.jsonl"]
    assert scan_done_prompts(tmp_path, lambda _t: None) == {"chat shape", "sharegpt shape"}


def test_entry_prompt_text() -> None:
    assert entry_prompt_text({"prompt": " p "}) == "p"
    assert entry_prompt_text({"conversations": [{"from": "system", "value": "s"}]}) == ""
    assert entry_prompt_text({"conversations": "nope"}) == ""


def test_records_have_the_documented_fields() -> None:
    assert set(row(0, "q").record()) == {
        "prompt_index",
        "conversations",
        "metadata",
        "completed",
        "partial",
        "api_calls",
        "tool_stats",
        "tool_error_counts",
        "reasoning",
    }
    tomb = Tombstone(1, DiscardReason.NO_REASONING, "q").record()
    assert tomb == {"prompt_index": 1, "discarded": "no_reasoning", "prompt": "q"}
