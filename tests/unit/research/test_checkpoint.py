"""P22-05: the checkpoint bound to the dataset hash."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from indus.research.checkpoint import (
    CHECKPOINT_NAME,
    BatchStat,
    Checkpoint,
    DatasetChanged,
    load_checkpoint,
    open_checkpoint,
    save_checkpoint,
)

NOW = datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    path = tmp_path / "runs" / "r1"
    path.mkdir(parents=True)
    return path


def opened(run_dir: Path, warnings: list[str], **values: object) -> Checkpoint:
    args: dict[str, object] = {
        "run_name": "r1",
        "dataset_hash": "h1",
        "distribution": "default",
        "seed": 0,
        "now": NOW,
        "warn": warnings.append,
        **values,
    }
    return open_checkpoint(run_dir, **args)  # type: ignore[arg-type]


def saved(run_dir: Path, **changes: object) -> Checkpoint:
    cp = Checkpoint("r1", "h1", "default", 0, completed={3, 1, 2})
    cp.batch_stats["0"] = BatchStat(3, 0, 1)
    for key, value in changes.items():
        setattr(cp, key, value)
    save_checkpoint(run_dir, cp, now=NOW)
    return cp


def test_missing_file_is_an_empty_checkpoint(run_dir: Path) -> None:
    warnings: list[str] = []
    cp = opened(run_dir, warnings)
    assert cp.completed == set() and cp.batch_stats == {} and warnings == []


def test_round_trip(run_dir: Path) -> None:
    saved(run_dir)
    data = json.loads((run_dir / CHECKPOINT_NAME).read_text())
    assert data["format"] == 1
    assert data["completed"] == [1, 2, 3]
    assert data["batch_stats"] == {"0": {"processed": 3, "skipped": 0, "discarded": 1}}
    assert data["updated_at"] == "2026-10-19T12:00:00+00:00"
    cp = opened(run_dir, [])
    assert cp.completed == {1, 2, 3}
    assert cp.batch_stats["0"].discarded == 1


def test_changed_dataset_is_refused_naming_both_hashes(run_dir: Path) -> None:
    saved(run_dir)
    with pytest.raises(DatasetChanged) as caught:
        opened(run_dir, [], dataset_hash="h2")
    assert "h1" in str(caught.value) and "h2" in str(caught.value)
    assert "--new-dataset" in str(caught.value)


def test_new_dataset_archives_the_old_run(run_dir: Path) -> None:
    saved(run_dir)
    (run_dir / "batch_0.jsonl").write_text('{"prompt_index": 0}\n')
    warnings: list[str] = []
    cp = opened(run_dir, warnings, dataset_hash="h2", new_dataset=True)
    assert cp.completed == set() and cp.dataset_hash == "h2"
    archive = run_dir / "previous-20261019T120000Z"
    assert sorted(p.name for p in archive.iterdir()) == ["batch_0.jsonl", CHECKPOINT_NAME]
    assert sorted(p.name for p in run_dir.iterdir()) == ["previous-20261019T120000Z"]
    assert any("previous-" in w for w in warnings)
    # a second archive in the same second gets its own folder; earlier archives stay put
    save_checkpoint(run_dir, cp, now=NOW)
    opened(run_dir, [], dataset_hash="h3", new_dataset=True)
    assert (run_dir / "previous-20261019T120000Z-2" / CHECKPOINT_NAME).exists()
    assert (archive / "batch_0.jsonl").exists()


def test_new_dataset_with_the_same_hash_keeps_everything(run_dir: Path) -> None:
    saved(run_dir)
    cp = opened(run_dir, [], new_dataset=True)
    assert cp.completed == {1, 2, 3}


def test_corrupt_checkpoint_warns_and_keeps_a_copy(run_dir: Path) -> None:
    (run_dir / CHECKPOINT_NAME).write_text("{not json", encoding="utf-8")
    warnings: list[str] = []
    cp = opened(run_dir, warnings)
    assert cp.completed == set()
    kept = run_dir / f"{CHECKPOINT_NAME}.bad-20261019T120000Z"
    assert kept.read_text() == "{not json"
    assert len(warnings) == 1 and kept.name in warnings[0]
    (run_dir / CHECKPOINT_NAME).write_text('{"format": 1}', encoding="utf-8")
    opened(run_dir, warnings)
    assert (run_dir / f"{kept.name}-2").exists()


def test_another_run_name_starts_empty(run_dir: Path) -> None:
    saved(run_dir)
    warnings: list[str] = []
    cp = opened(run_dir, warnings, run_name="r2")
    assert cp.completed == set() and cp.run_name == "r2"
    assert warnings and "'r1'" in warnings[0]


def test_changed_sampling_settings_only_warn(run_dir: Path) -> None:
    saved(run_dir)
    warnings: list[str] = []
    cp = opened(run_dir, warnings, seed=5, distribution="minimal")
    assert cp.completed == {1, 2, 3}
    assert len(warnings) == 2


def test_atomic_write_survives_a_crash_before_the_rename(run_dir: Path) -> None:
    saved(run_dir)
    before = (run_dir / CHECKPOINT_NAME).read_bytes()
    cp = Checkpoint("r1", "h1", "default", 0, completed=set(range(50)))

    def crash(_src: Path, _dst: Path) -> None:
        raise OSError("power cut")

    with pytest.raises(OSError):
        save_checkpoint(run_dir, cp, now=NOW, replace=crash)
    assert (run_dir / CHECKPOINT_NAME).read_bytes() == before
    assert load_checkpoint(
        run_dir,
        run_name="r1",
        dataset_hash="h1",
        distribution="default",
        seed=0,
        stamp="x",
        warn=lambda _t: None,
    ).completed == {1, 2, 3}
    # litter from a writer that died for real is cleared by the next save
    (run_dir / f".{CHECKPOINT_NAME}.999.tmp").write_text("half")
    save_checkpoint(run_dir, cp, now=NOW)
    assert [p.name for p in run_dir.iterdir()] == [CHECKPOINT_NAME]


def test_save_creates_the_run_dir(run_dir: Path) -> None:
    target = run_dir / "nested" / "run"
    save_checkpoint(target, Checkpoint("r1", "h1", "default", 0), now=NOW)
    assert (target / CHECKPOINT_NAME).is_file()
