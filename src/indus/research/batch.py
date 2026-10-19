"""Batch orchestration: the parent process of ``indus research batch`` (P22-08).

``BatchRunner.run`` in order:

1. every setting resolved against ``DEFAULTS`` (the one defaults table; the effective config
   printed before anything runs says what each value ended up as, R10.4);
2. the dataset (``DatasetError`` → exit 2), the distribution (unknown → 2), the route (resolved
   once here so the effective config names the model actually used; a failure → 1);
3. ``<runs root>/<run name>/`` and its checkpoint, bound to the dataset hash (a different dataset
   → 2 unless ``new_dataset``); prompts whose index the checkpoint has are skipped, and with
   ``resume`` so are prompts whose text is already in a batch file (content scan);
4. the rest in chunks of ``batch_size`` on a spawn process pool of ``workers``. Results come back
   in any order; after each finished batch the parent merges its completed indices, records its
   stats and rewrites the checkpoint atomically (a write error only warns). Only the parent writes
   the checkpoint, and only after the worker wrote and fsynced the rows (R10.1);
5. Ctrl-C → the pool is terminated and joined, the checkpoint saved, exit 130; a worker that died
   or raised → the same, exit 1;
6. after the pool: final checkpoint, every ``batch_*.jsonl`` merged into ``trajectories.jsonl``
   (tombstones, bad lines, non-objects and rows naming a tool outside the registry left out,
   R10.5), ``statistics.json`` and a summary table.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import Executor, Future, ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC
from pathlib import Path
from typing import Any, cast

from indus.foundation.scope import Scope
from indus.research import distributions
from indus.research.batch_io import batch_files, read_batch_file, remaining, scan_done_prompts
from indus.research.checkpoint import (
    BatchStat,
    Checkpoint,
    DatasetChanged,
    open_checkpoint,
    save_checkpoint,
)
from indus.research.dataset import Dataset, DatasetError, chunk, load_dataset
from indus.research.dataset import warning_lines as dataset_warnings
from indus.research.stats import Reasoning, Tally, merge_tallies
from indus.research.worker import (
    BatchJob,
    BatchOutcome,
    WorkerSettings,
    init_worker,
    process_batch,
    registered_tools,
)

__all__ = [
    "DEFAULTS",
    "EXIT_DONE",
    "EXIT_FAILED",
    "EXIT_INTERRUPTED",
    "EXIT_USAGE",
    "BatchRunner",
    "BatchSettings",
    "MergeReport",
    "merge_batches",
    "tool_rates",
]

EXIT_DONE = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130

RUNS_ROOT_KEY = "research.runs_root"
TRAJECTORIES = "trajectories.jsonl"
STATISTICS = "statistics.json"
PROVIDER_DEFAULT = "provider default"
PREVIEW = 60
_RUN_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

DEFAULTS: Mapping[str, Any] = {
    "batch_size": 10,
    "distribution": distributions.DEFAULT_DISTRIBUTION,
    "max_turns": 10,
    "workers": 4,
    "seed": 0,
    "runs_root": "./research-runs",
    "reasoning_effort": None,
    "max_samples": None,
}
"""Every default of a batch run, in one place (the CLI and the effective config read it)."""

type Out = Callable[[str], None]
type ExecutorFactory = Callable[[int, WorkerSettings], Executor]
type Task = Callable[[BatchJob], BatchOutcome]


@dataclass(frozen=True)
class BatchSettings:
    """What the caller asked for; ``None`` means "use the default" (``DEFAULTS``)."""

    dataset: Path
    run_name: str
    batch_size: int | None = None
    distribution: str | None = None
    model: str | None = None
    provider: str | None = None
    base_url: str | None = None
    max_turns: int | None = None
    workers: int | None = None
    resume: bool = False
    new_dataset: bool = False
    seed: int | None = None
    reasoning_effort: str | None = None
    prefill: tuple[Mapping[str, Any], ...] = ()
    ephemeral_system_prompt: str | None = None
    routing: Mapping[str, Any] | None = None
    max_samples: int | None = None
    verbose: bool = False
    runs_root: Path | None = None


class _Stop(Exception):
    def __init__(self, code: int, message: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _default(value: Any, key: str) -> Any:
    return DEFAULTS[key] if value is None else value


def _spawn_executor(workers: int, settings: WorkerSettings) -> Executor:
    return ProcessPoolExecutor(
        max_workers=workers,
        mp_context=multiprocessing.get_context("spawn"),
        initializer=init_worker,
        initargs=(settings,),
    )


def _terminate(executor: Executor) -> None:
    """Stop the pool now: pending batches cancelled, running workers terminated and joined."""
    executor.shutdown(wait=False, cancel_futures=True)
    if not isinstance(executor, ProcessPoolExecutor):
        return
    stop = getattr(executor, "terminate_workers", None)  # Python 3.14+
    if callable(stop):
        stop()
        return
    processes = cast(
        Mapping[int, Any],
        getattr(executor, "_processes", None) or {},
    )
    for process in list(processes.values()):
        try:
            process.terminate()
        except Exception:
            continue
    for process in list(processes.values()):
        try:
            process.join(5)
        except Exception:
            continue


# -- merge and statistics --------------------------------------------------------------------------


@dataclass
class MergeReport:
    files: int = 0
    kept: int = 0
    tombstones: int = 0
    dropped: int = 0
    corrupt: list[str] = field(default_factory=list[str])


def merge_batches(run_dir: Path, registered: Iterable[str], warn: Out) -> MergeReport:
    """Every batch file → ``trajectories.jsonl`` (see the module notes)."""
    known = set(registered)
    report = MergeReport()
    target = run_dir / TRAJECTORIES
    temp = run_dir / f".{TRAJECTORIES}.tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        for path in batch_files(run_dir):
            report.files += 1
            bad: list[str] = []
            for entry in read_batch_file(path, bad.append):
                if entry.get("discarded"):
                    report.tombstones += 1
                    continue
                stats = entry.get("tool_stats")
                names = list(cast(Mapping[str, Any], stats)) if isinstance(stats, Mapping) else []
                strange = [n for n in names if n not in known]
                if strange or not isinstance(entry.get("conversations"), list):
                    report.dropped += 1
                    what = f"tool {strange[0][:50]!r}" if strange else "no conversations"
                    report.corrupt.append(f"{path.name}: prompt {entry.get('prompt_index')}")
                    warn(f"dropped a corrupt sample from {path.name} ({what})")
                    continue
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
                report.kept += 1
            report.dropped += len(bad)
            for line in bad:
                warn(line)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, target)
    return report


def tool_rates(tallies: Mapping[str, Tally]) -> dict[str, dict[str, float | int]]:
    """Per tool: counts plus success and failure rates in percent (2 dp, 0.0 without calls)."""
    out: dict[str, dict[str, float | int]] = {}
    for name in sorted(tallies):
        tally = tallies[name]
        judged = tally.success + tally.failure
        out[name] = {
            **tally.as_dict(),
            "success_rate": round(tally.success / judged * 100, 2) if judged else 0.0,
            "failure_rate": round(tally.failure / judged * 100, 2) if judged else 0.0,
        }
    return out


# -- the runner ------------------------------------------------------------------------------------


@dataclass
class _Totals:
    tallies: dict[str, Tally] = field(default_factory=dict[str, Tally])
    reasoning: Reasoning = field(default_factory=Reasoning)
    processed: int = 0
    discarded: int = 0
    failed: int = 0
    bad_arguments: int = 0
    batches_done: int = 0


class BatchRunner:
    """One batch run (see the module notes)."""

    def __init__(
        self,
        scope: Scope,
        settings: BatchSettings,
        *,
        out: Out,
        err: Out,
        executor_factory: ExecutorFactory = _spawn_executor,
        task: Task = process_batch,
        resolve_route: Callable[[Scope, BatchSettings], Any] | None = None,
    ) -> None:
        self.scope = scope
        self.settings = settings
        self.out = out
        self.err = err
        self._executor_factory = executor_factory
        self._task = task
        self._resolve_route = resolve_route or _resolve_route
        self.totals = _Totals()
        self.checkpoint: Checkpoint | None = None
        self.run_dir: Path | None = None
        self.effective: dict[str, Any] = {}

    def warn(self, text: str) -> None:
        self.err(f"warning: {text}")

    # -- setup ---------------------------------------------------------------------------------

    def _runs_root(self) -> Path:
        if self.settings.runs_root is not None:
            return self.settings.runs_root
        config = self.scope.config
        value = config.get(RUNS_ROOT_KEY) if config is not None else None
        return Path(str(value or DEFAULTS["runs_root"])).expanduser()

    def _dataset(self) -> Dataset:
        s = self.settings
        try:
            dataset = load_dataset(s.dataset, max_samples=s.max_samples)
        except DatasetError as exc:
            raise _Stop(EXIT_USAGE, str(exc)) from None
        for line in dataset_warnings(dataset.skipped):
            self.err(line)
        return dataset

    def _check_values(self) -> tuple[int, int, int, int, str]:
        s = self.settings
        if not _RUN_NAME.match(s.run_name):
            raise _Stop(EXIT_USAGE, f"run name {s.run_name!r}: use letters, digits, '.', '-', '_'")
        batch_size = int(_default(s.batch_size, "batch_size"))
        workers = int(_default(s.workers, "workers"))
        max_turns = int(_default(s.max_turns, "max_turns"))
        seed = int(_default(s.seed, "seed"))
        for name, value in (("batch size", batch_size), ("workers", workers)):
            if value < 1:
                raise _Stop(EXIT_USAGE, f"{name} must be at least 1")
        if max_turns < 1:
            raise _Stop(EXIT_USAGE, "max turns must be at least 1")
        distribution = str(_default(s.distribution, "distribution"))
        try:
            distributions.get(distribution)
        except distributions.UnknownDistribution as exc:
            raise _Stop(EXIT_USAGE, str(exc)) from None
        return batch_size, workers, max_turns, seed, distribution

    # -- run -----------------------------------------------------------------------------------

    def run(self) -> int:
        try:
            return self._run()
        except _Stop as stop:
            if stop.message:
                self.err(f"research batch: {stop.message}")
            return stop.code

    def _run(self) -> int:
        s = self.settings
        batch_size, workers, max_turns, seed, distribution = self._check_values()
        dataset = self._dataset()
        try:
            route = self._resolve_route(self.scope, s)
        except Exception as exc:
            raise _Stop(EXIT_FAILED, f"no model to run with: {exc}") from None
        run_dir = self._runs_root() / s.run_name
        try:
            run_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise _Stop(EXIT_FAILED, f"cannot create {run_dir}: {exc.strerror or exc}") from None
        self.run_dir = run_dir
        try:
            checkpoint = open_checkpoint(
                run_dir,
                run_name=s.run_name,
                dataset_hash=dataset.hash,
                distribution=distribution,
                seed=seed,
                now=self.scope.clock.now(),
                warn=self.warn,
                new_dataset=s.new_dataset,
            )
        except DatasetChanged as exc:
            raise _Stop(EXIT_USAGE, str(exc)) from None
        self.checkpoint = checkpoint
        left = [(i, row) for i, row in dataset.pairs() if i not in checkpoint.completed]
        by_content: list[int] = []
        if s.resume and left:
            done = scan_done_prompts(run_dir, self.warn)
            left, by_content = remaining(left, done, lambda row: row.prompt)
            checkpoint.completed.update(by_content)  # durable rows of this dataset: done
        registered = registered_tools(self.scope.clock)
        settings = self._worker_settings(route, run_dir, distribution, seed, max_turns, registered)
        self._print_effective(
            dataset, route, run_dir, batch_size, workers, max_turns, seed, distribution
        )
        images = sum(1 for _, row in left if row.image)
        if images:
            self.warn(
                f"{images} prompt(s) name a container image; each runs in its own sandbox "
                "(backends without images ignore it with a warning)"
            )
        skipped = len(dataset.rows) - len(left)
        self.out(
            f"{len(left)} prompt(s) to run, {skipped} already done"
            + (f" ({len(by_content)} found in the batch files)" if by_content else "")
        )
        started = time.monotonic()
        code = EXIT_DONE
        if left:
            jobs = [BatchJob(n, tuple(items)) for n, items in enumerate(chunk(left, batch_size))]
            code = self._pool(jobs, workers, settings)
        self._save()
        if code != EXIT_DONE:
            return code
        report = merge_batches(run_dir, registered, self.warn)
        self._write_statistics(dataset, route, report, batch_size, distribution, seed, started)
        self._summary(report)
        return EXIT_DONE

    def _worker_settings(
        self,
        route: Any,
        run_dir: Path,
        distribution: str,
        seed: int,
        max_turns: int,
        registered: Sequence[str],
    ) -> WorkerSettings:
        from indus.tools import toolsets
        from indus.tools.builtin import load_builtins
        from indus.tools.registry import ToolRegistry

        registry = ToolRegistry(clock=self.scope.clock)
        load_builtins(registry)
        s = self.settings
        return WorkerSettings(
            home=str(self.scope.home),
            env=self.scope.env.to_dict(),
            run_dir=str(run_dir.resolve()),
            run_name=s.run_name,
            model=str(getattr(route, "model", "") or "") or s.model,
            provider=str(getattr(route, "provider", "") or "") or s.provider,
            base_url=s.base_url,
            distribution=distribution,
            seed=seed,
            max_iterations=max_turns,
            reasoning_effort=s.reasoning_effort,
            prefill=tuple(dict(m) for m in s.prefill),
            ephemeral_system_prompt=s.ephemeral_system_prompt,
            routing=dict(s.routing) if s.routing else None,
            registered=tuple(registered),
            valid_toolsets=tuple(toolsets.toolset_names(registry, self.scope)),
            verbose=s.verbose,
        )

    def _pool(self, jobs: list[BatchJob], workers: int, settings: WorkerSettings) -> int:
        executor = self._executor_factory(min(workers, len(jobs)), settings)
        total = len(jobs)
        try:
            futures: list[Future[BatchOutcome]] = [executor.submit(self._task, j) for j in jobs]
            for future in as_completed(futures):
                self._record(future.result(), total)
        except KeyboardInterrupt:
            self.err("interrupted: stopping the workers")
            _terminate(executor)
            return EXIT_INTERRUPTED
        except Exception as exc:
            self.err(f"research batch: a worker failed: {type(exc).__name__}: {exc}")
            _terminate(executor)
            return EXIT_FAILED
        executor.shutdown(wait=True)
        return EXIT_DONE

    def _record(self, outcome: BatchOutcome, total: int) -> None:
        checkpoint = self.checkpoint
        assert checkpoint is not None
        checkpoint.completed.update(outcome.completed)
        checkpoint.batch_stats[str(outcome.batch)] = BatchStat(
            outcome.processed, outcome.skipped, outcome.discarded
        )
        t = self.totals
        merge_tallies(t.tallies, outcome.tool_stats)
        t.reasoning.add(
            Reasoning(
                outcome.reasoning.get("total_assistant_turns", 0),
                outcome.reasoning.get("turns_with_reasoning", 0),
                outcome.reasoning.get("turns_without_reasoning", 0),
            )
        )
        t.processed += outcome.processed
        t.discarded += outcome.discarded
        t.failed += outcome.failed
        t.bad_arguments += outcome.bad_arguments
        t.batches_done += 1
        self._save()
        self.out(
            f"batch {outcome.batch} done ({t.batches_done}/{total}): {outcome.processed} run, "
            f"{len(outcome.completed)} saved, {outcome.discarded} discarded, "
            f"{outcome.failed} failed"
        )

    def _save(self) -> None:
        if self.checkpoint is None or self.run_dir is None:
            return
        try:
            save_checkpoint(self.run_dir, self.checkpoint, now=self.scope.clock.now())
        except OSError as exc:
            self.warn(f"the checkpoint could not be written: {exc.strerror or exc}")

    # -- reporting -----------------------------------------------------------------------------

    def _print_effective(
        self,
        dataset: Dataset,
        route: Any,
        run_dir: Path,
        batch_size: int,
        workers: int,
        max_turns: int,
        seed: int,
        distribution: str,
    ) -> None:
        s = self.settings
        prompt = s.ephemeral_system_prompt or ""
        shown = prompt if len(prompt) <= PREVIEW else prompt[:PREVIEW] + "…"
        routing = ", ".join(f"{k}={v}" for k, v in (s.routing or {}).items()) or "none"
        values: list[tuple[str, Any]] = [
            ("dataset", f"{dataset.path} ({len(dataset.rows)} prompts, hash {dataset.hash[:12]})"),
            ("run dir", run_dir),
            ("model", f"{getattr(route, 'model', '?')} via {getattr(route, 'provider', '?')}"),
            ("base url", getattr(route, "base_url", "") or "provider default"),
            ("distribution", distribution),
            ("batch size", batch_size),
            ("workers", workers),
            ("max turns", max_turns),
            ("seed", seed),
            ("reasoning effort", s.reasoning_effort or PROVIDER_DEFAULT),
            ("prefill", f"{len(s.prefill)} message(s)" if s.prefill else "none"),
            ("ephemeral system prompt", repr(shown) if prompt else "none"),
            ("provider routing", routing),
            ("max samples", s.max_samples if s.max_samples is not None else "all"),
            ("resume", "yes (content scan)" if s.resume else "no (checkpoint indices only)"),
            ("new dataset", "yes" if s.new_dataset else "no"),
        ]
        self.effective = {name: value for name, value in values}
        self.out("effective config:")
        for name, value in values:
            self.out(f"  {name + ':':<26}{value}")

    def _write_statistics(
        self,
        dataset: Dataset,
        route: Any,
        report: MergeReport,
        batch_size: int,
        distribution: str,
        seed: int,
        started: float,
    ) -> None:
        assert self.run_dir is not None
        t = self.totals
        data: dict[str, Any] = {
            "run_name": self.settings.run_name,
            "dataset": str(dataset.path),
            "dataset_hash": dataset.hash,
            "distribution": distribution,
            "seed": seed,
            "model": getattr(route, "model", None),
            "total_prompts": len(dataset.rows),
            "batch_size": batch_size,
            "batches_this_run": t.batches_done,
            "prompts_run": t.processed,
            "failed": t.failed,
            "discarded": t.discarded,
            "unparseable_arguments": t.bad_arguments,
            "completed_at": self.scope.clock.now().astimezone(UTC).isoformat(),
            "duration_seconds": round(time.monotonic() - started, 2),
            "tool_statistics": tool_rates(t.tallies),
            "reasoning_statistics": t.reasoning.as_dict(),
            "merged": {
                "kept": report.kept,
                "tombstones": report.tombstones,
                "dropped": report.dropped,
                "files": report.files,
            },
        }
        path = self.run_dir / STATISTICS
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def _summary(self, report: MergeReport) -> None:
        t = self.totals
        rates = tool_rates(t.tallies)
        self.out("")
        self.out(f"prompts run this time: {t.processed} ({t.failed} failed, retried next run)")
        self.out(f"samples in {TRAJECTORIES}: {report.kept} (from {report.files} batch file(s))")
        if report.dropped:
            self.out(f"left out as corrupt: {report.dropped}")
        if rates:
            self.out(f"{'tool':<28}{'calls':>7}{'ok':>7}{'failed':>8}{'ok %':>9}")
            ordered = sorted(rates.items(), key=lambda item: (-int(item[1]["count"]), item[0]))
            for name, row in ordered:
                self.out(
                    f"{name:<28}{row['count']:>7}{row['success']:>7}{row['failure']:>8}"
                    f"{row['success_rate']:>8.1f}%"
                )
        else:
            self.out("no tool calls this run")
        r = t.reasoning
        if r.total:
            share = round(r.with_reasoning / r.total * 100, 1)
            self.out(f"assistant turns with reasoning: {r.with_reasoning}/{r.total} ({share}%)")
        if t.discarded:
            self.out(f"discarded without reasoning: {t.discarded}")
        assert self.run_dir is not None
        self.out(f"results: {self.run_dir}")


def _resolve_route(scope: Scope, settings: BatchSettings) -> Any:
    from indus.providers.route import resolve

    return resolve(
        scope, model=settings.model, provider=settings.provider, base_url=settings.base_url
    )
