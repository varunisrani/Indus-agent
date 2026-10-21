"""Performance budgets (P25-12): the harness the perf tests measure through.

Numbers come from ``tooling/perf/budgets.toml``; ``tooling/perf/baseline.json`` holds medians
recorded on a clean runner. ``Meter.run`` takes ``runs`` samples of a workload (a workload may
return its own number, for metrics like "p99 of this run"; otherwise the meter times it), and
``judge`` compares the budget's metric with the limit and with the baseline plus the allowed
regression. Every failure message starts with the budget name.

Recording a baseline: ``INDUS_PERF_RECORD=<path of a task note that explains why>`` makes the
session write the medians it measured into ``baseline.json`` after the run (only with a note
that exists).
"""

from __future__ import annotations

import json
import os
import statistics
import time
import tomllib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
BUDGETS = ROOT / "tooling" / "perf" / "budgets.toml"
BASELINE = ROOT / "tooling" / "perf" / "baseline.json"
RECORD_ENV = "INDUS_PERF_RECORD"

Workload = Callable[[], "float | None"]


@dataclass(frozen=True)
class Budget:
    name: str
    limit_ms: float
    metric: str
    runs: int
    regression: float
    source: str


def load_budgets(path: Path = BUDGETS) -> dict[str, Budget]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    settings = data.get("settings", {})
    out: dict[str, Budget] = {}
    for name, row in data.get("python", {}).items():
        out[name] = Budget(
            name=name,
            limit_ms=float(row["limit_ms"]),
            metric=str(row.get("metric", "median")),
            runs=int(row.get("runs", settings.get("runs", 5))),
            regression=float(row.get("regression", settings.get("regression", 0.10))),
            source=str(row.get("source", "")),
        )
    return out


def load_baseline(path: Path = BASELINE) -> dict[str, float]:
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k: float(v) for k, v in (data.get("budgets") or {}).items()}


def metric(budget: Budget, samples: list[float]) -> float:
    if budget.metric == "median":
        return statistics.median(samples)
    if budget.metric == "max":
        return max(samples)
    raise ValueError(f"{budget.name}: unknown metric {budget.metric!r}")


def judge(budget: Budget, samples: list[float], baseline: dict[str, float]) -> list[str]:
    """Problems with ``samples`` (milliseconds) against the budget and the baseline."""
    value = metric(budget, samples)
    shown = ", ".join(f"{s:.1f}" for s in samples)
    problems: list[str] = []
    if value > budget.limit_ms:
        problems.append(
            f"{budget.name}: {budget.metric} {value:.1f} ms is over the budget of "
            f"{budget.limit_ms:g} ms ({budget.source}); samples [{shown}] ms"
        )
    base = baseline.get(budget.name)
    if base is not None and value > base * (1 + budget.regression):
        problems.append(
            f"{budget.name}: {budget.metric} {value:.1f} ms is more than "
            f"{budget.regression:.0%} over the recorded baseline of {base:.1f} ms; "
            f"samples [{shown}] ms"
        )
    return problems


@dataclass
class Meter:
    """Measures workloads and keeps the medians for an optional baseline recording."""

    budgets: dict[str, Budget]
    baseline: dict[str, float]
    clock: Callable[[], float] = time.perf_counter
    measured: dict[str, float] = field(default_factory=dict)

    def sample(self, workload: Workload) -> float:
        started = self.clock()
        own = workload()
        return float(own) if own is not None else (self.clock() - started) * 1000.0

    def run(self, name: str, workload: Workload, *, runs: int | None = None) -> list[float]:
        budget = self.budgets[name]
        samples = [self.sample(workload) for _ in range(runs or budget.runs)]
        self.measured[name] = metric(budget, samples)
        print(
            f"\nPERF {name}: {budget.metric}={self.measured[name]:.1f}ms "
            f"limit={budget.limit_ms:g}ms samples={[round(s, 1) for s in samples]}"
        )
        return samples

    def check(self, name: str, workload: Workload, *, runs: int | None = None) -> None:
        samples = self.run(name, workload, runs=runs)
        problems = judge(self.budgets[name], samples, self.baseline)
        assert not problems, "\n".join(problems)


def record_baseline(measured: dict[str, float], note: Path, path: Path = BASELINE) -> None:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    budgets = dict(data.get("budgets") or {})
    budgets.update({k: round(v, 1) for k, v in measured.items()})
    data.update(
        {
            "runner": os.environ.get("RUNNER_NAME") or os.uname().nodename,
            "recorded": datetime.now(UTC).date().isoformat(),
            "record_note": str(note),
            "budgets": dict(sorted(budgets.items())),
        }
    )
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


@pytest.fixture(scope="session")
def perf_meter() -> Iterator[Meter]:
    meter = Meter(load_budgets(), load_baseline())
    yield meter
    note = os.environ.get(RECORD_ENV)
    if note and meter.measured:
        path = Path(note)
        if not path.is_absolute():
            path = ROOT / path
        if not path.is_file():
            raise pytest.UsageError(f"{RECORD_ENV} must name an existing note, not {note!r}")
        record_baseline(
            meter.measured, path.relative_to(ROOT) if path.is_relative_to(ROOT) else path
        )
