"""P22-08: batch orchestration with an in-process executor (the real pool is in
``tests/integration/research/test_batch_run.py``)."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from collections.abc import Callable, Sequence
from concurrent.futures import Executor, ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from indus.foundation.clock import SystemClock
from indus.foundation.scope import Scope
from indus.research import batch as batch_mod
from indus.research.batch import (
    EXIT_DONE,
    EXIT_FAILED,
    EXIT_INTERRUPTED,
    EXIT_USAGE,
    BatchRunner,
    BatchSettings,
    merge_batches,
    tool_rates,
)
from indus.research.batch_io import append_durable, batch_path
from indus.research.checkpoint import CHECKPOINT_NAME
from indus.research.stats import Tally
from indus.research.worker import BatchJob, BatchOutcome, Conversation, WorkerSettings, run_batch

ROUTE = SimpleNamespace(model="model-used", provider="fakeprov", base_url="http://fake.invalid/v1")


class Scripted:
    """Canned conversations: prompts containing "plain" have no reasoning, "tool" call
    read_file, "ghost" call a tool that does not exist."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    async def __call__(self, prompt: str, toolsets: Sequence[str], task_id: str) -> Conversation:
        self.asked.append(prompt)
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        name = "read_file" if "tool" in prompt else "teleport_user" if "ghost" in prompt else ""
        if name:
            call = {"id": "c1", "type": "function", "function": {"name": name, "arguments": "{}"}}
            messages.append(
                {"role": "assistant", "content": "", "reasoning": "r", "tool_calls": [call]}
            )
            messages.append({"role": "tool", "tool_call_id": "c1", "content": '{"error": "x"}'})
        reasoning = None if "plain" in prompt else "because"
        messages.append({"role": "assistant", "content": "answer", "reasoning": reasoning})
        return Conversation(messages, [], "completed", 2, "model-used")


class Harness:
    def __init__(self, scope: Scope, root: Path) -> None:
        self.scope = scope
        self.root = root
        self.runner = Scripted()
        self.out: list[str] = []
        self.err: list[str] = []
        self.executors: list[SpyExecutor] = []
        self.fail_with: BaseException | None = None

    def task(self, worker_settings: WorkerSettings) -> Callable[[BatchJob], BatchOutcome]:
        def run(job: BatchJob) -> BatchOutcome:
            if self.fail_with is not None:
                raise self.fail_with
            return asyncio.run(
                run_batch(self.scope, worker_settings, job, runner=self.runner, out=self.err.append)
            )

        return run

    def runner_for(self, settings: BatchSettings) -> BatchRunner:
        holder: dict[str, WorkerSettings] = {}

        def factory(workers: int, ws: WorkerSettings) -> Executor:
            holder["ws"] = ws
            spy = SpyExecutor(max_workers=1)
            self.executors.append(spy)
            return spy

        runner = BatchRunner(
            self.scope,
            settings,
            out=self.out.append,
            err=self.err.append,
            executor_factory=factory,
            task=lambda job: self.task(holder["ws"])(job),
            resolve_route=lambda _scope, _s: ROUTE,
        )
        return runner

    def settings(self, dataset: Path, **values: Any) -> BatchSettings:
        base: dict[str, Any] = {
            "dataset": dataset,
            "run_name": "r1",
            "batch_size": 5,
            "workers": 2,
            "runs_root": self.root,
            "distribution": "terminal_only",
        }
        base.update(values)
        return BatchSettings(**base)

    def run(self, dataset: Path, **values: Any) -> int:
        return self.runner_for(self.settings(dataset, **values)).run()

    @property
    def run_dir(self) -> Path:
        return self.root / "r1"


class SpyExecutor(ThreadPoolExecutor):
    shut: list[tuple[bool, bool]]

    def __init__(self, max_workers: int) -> None:
        super().__init__(max_workers=max_workers)
        self.shut = []

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        self.shut.append((wait, cancel_futures))
        super().shutdown(wait=wait, cancel_futures=cancel_futures)


@pytest.fixture
def harness(scope: Scope, tmp_path: Path) -> Harness:
    return Harness(dataclasses.replace(scope, clock=SystemClock()), tmp_path / "runs")


def dataset(tmp_path: Path, prompts: Sequence[str], name: str = "d.jsonl") -> Path:
    path = tmp_path / name
    path.write_text("".join(json.dumps({"prompt": p}) + "\n" for p in prompts), encoding="utf-8")
    return path


PROMPTS = [f"q{i} plain" if i == 3 else f"q{i} tool" if i % 4 == 0 else f"q{i}" for i in range(12)]


def test_a_run_writes_rows_checkpoint_merge_and_statistics(
    harness: Harness, tmp_path: Path
) -> None:
    code = harness.run(dataset(tmp_path, PROMPTS))
    assert code == EXIT_DONE, harness.err
    run_dir = harness.run_dir
    checkpoint = json.loads((run_dir / CHECKPOINT_NAME).read_text())
    assert checkpoint["completed"] == list(range(12))
    assert sorted(checkpoint["batch_stats"]) == ["0", "1", "2"]
    assert checkpoint["batch_stats"]["0"]["discarded"] == 1
    merged = (run_dir / "trajectories.jsonl").read_text().splitlines()
    assert sorted(json.loads(line)["prompt_index"] for line in merged) == [
        i for i in range(12) if i != 3
    ]
    stats = json.loads((run_dir / "statistics.json").read_text())
    assert stats["tool_statistics"]["read_file"] == {
        "count": 3,
        "success": 0,
        "failure": 3,
        "success_rate": 0.0,
        "failure_rate": 100.0,
    }
    assert stats["discarded"] == 1 and stats["merged"]["tombstones"] == 1
    assert stats["model"] == "model-used"
    out = "\n".join(harness.out)
    assert "effective config:" in out
    assert "model-used via fakeprov" in out
    assert "provider default" in out  # no reasoning flag: nothing sent (R10.4)
    assert out.index("effective config:") < out.index("batch 0 done")


def test_rerun_skips_checkpoint_indices_and_resume_adds_the_content_scan(
    harness: Harness, tmp_path: Path
) -> None:
    data = dataset(tmp_path, PROMPTS)
    assert harness.run(data) == EXIT_DONE
    harness.runner.asked.clear()
    assert harness.run(data) == EXIT_DONE
    assert harness.runner.asked == []  # same dataset: indices honoured without --resume
    # rows written by a killed run that never reached the checkpoint
    (harness.run_dir / CHECKPOINT_NAME).unlink()
    assert harness.run(data, resume=True) == EXIT_DONE
    assert harness.runner.asked == []
    checkpoint = json.loads((harness.run_dir / CHECKPOINT_NAME).read_text())
    assert checkpoint["completed"] == list(range(12))


def test_changed_dataset_is_refused_and_new_dataset_archives(
    harness: Harness, tmp_path: Path
) -> None:
    assert harness.run(dataset(tmp_path, PROMPTS)) == EXIT_DONE
    edited = dataset(tmp_path, [*PROMPTS[:5], "q5 edited", *PROMPTS[6:]], name="e.jsonl")
    assert harness.run(edited) == EXIT_USAGE
    assert any("dataset changed" in line for line in harness.err)
    harness.runner.asked.clear()
    assert harness.run(edited, new_dataset=True) == EXIT_DONE
    assert len(harness.runner.asked) == 12
    assert [p.name for p in harness.run_dir.glob("previous-*")]


def test_interrupt_terminates_the_pool_and_saves_the_checkpoint(
    harness: Harness, tmp_path: Path
) -> None:
    harness.fail_with = KeyboardInterrupt()
    assert harness.run(dataset(tmp_path, PROMPTS)) == EXIT_INTERRUPTED
    assert harness.executors[-1].shut[0] == (False, True)
    assert (harness.run_dir / CHECKPOINT_NAME).exists()
    assert not (harness.run_dir / "trajectories.jsonl").exists()


def test_a_failing_worker_ends_the_run_with_1(harness: Harness, tmp_path: Path) -> None:
    harness.fail_with = RuntimeError("worker died")
    assert harness.run(dataset(tmp_path, PROMPTS)) == EXIT_FAILED
    assert any("worker died" in line for line in harness.err)
    assert harness.executors[-1].shut[0] == (False, True)


def test_usage_errors(harness: Harness, tmp_path: Path) -> None:
    data = dataset(tmp_path, PROMPTS)
    assert harness.run(tmp_path / "missing.jsonl") == EXIT_USAGE
    assert harness.run(data, distribution="nope") == EXIT_USAGE
    assert harness.run(data, run_name="../escape") == EXIT_USAGE
    assert harness.run(data, batch_size=0) == EXIT_USAGE

    def broken(_scope: Scope, _s: BatchSettings) -> Any:
        raise RuntimeError("no key for that provider")

    runner = harness.runner_for(harness.settings(data))
    runner._resolve_route = broken  # pyright: ignore[reportPrivateUsage]
    assert runner.run() == EXIT_FAILED
    assert harness.runner.asked == []


def test_runs_root_comes_from_the_config(harness: Harness, tmp_path: Path) -> None:
    from tests.fakes.agent import DictConfig

    harness.scope = harness.scope.with_config(
        DictConfig({"research": {"runs_root": str(tmp_path / "configured")}})
    )
    assert harness.run(dataset(tmp_path, PROMPTS[:2]), runs_root=None) == EXIT_DONE
    assert (tmp_path / "configured" / "r1" / "trajectories.jsonl").exists()


def test_merge_drops_tombstones_bad_lines_and_corrupt_rows(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    good = {"prompt_index": 0, "conversations": [], "tool_stats": {"terminal": {"count": 1}}}
    ghost = {"prompt_index": 1, "conversations": [], "tool_stats": {"teleport_user": {}}}
    append_durable(batch_path(run_dir, 0), good)
    append_durable(batch_path(run_dir, 0), {"prompt_index": 2, "discarded": "no_reasoning"})
    append_durable(batch_path(run_dir, 1), ghost)
    with open(batch_path(run_dir, 1), "a", encoding="utf-8") as handle:
        handle.write("not json\n[1, 2]\n")
    warnings: list[str] = []
    report = merge_batches(run_dir, ["terminal"], warnings.append)
    assert (report.files, report.kept, report.tombstones, report.dropped) == (2, 1, 1, 2)
    lines = (run_dir / "trajectories.jsonl").read_text().splitlines()
    assert [json.loads(line)["prompt_index"] for line in lines] == [0]
    assert any("teleport_user" in w for w in warnings)


def test_tool_rates_round_to_two_places() -> None:
    rates = tool_rates({"a": Tally(3, 2, 1), "b": Tally(0, 0, 0)})
    assert rates["a"]["success_rate"] == 66.67 and rates["a"]["failure_rate"] == 33.33
    assert rates["b"]["success_rate"] == 0.0


def test_defaults_table_is_the_single_source() -> None:
    assert batch_mod.DEFAULTS["workers"] == 4
    assert batch_mod.DEFAULTS["max_turns"] == 10
    assert batch_mod.DEFAULTS["distribution"] == "default"
    assert batch_mod.DEFAULTS["reasoning_effort"] is None
