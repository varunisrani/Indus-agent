"""Conventions for A/B experiments on the agent (P22-15).

- **Results store** (``ResultsStore``): one ``results.jsonl`` per experiment. A cell is one arm ×
  one task × one repetition; its id is a hash of the three. A rerun skips completed cells. A
  startup crash (non-zero exit with empty output) is never recorded, so the cell runs again.
- **Arms** (``Arm``, ``check_arms``): two arms differ in exactly one thing: the code tree on
  ``PYTHONPATH`` or one config key. A second difference, or none, is refused.
- **Oracles** (``marker``, ``matches``, ``file_check``): tasks are scored by checks a program can
  make (a marker string or a regex in the final answer, a file on disk), never by asking the model
  how it did.
- **Trace metrics** (``trace_metrics``): model calls, tool calls, tool errors, retries (a call to
  the tool that just failed), result bytes and wall time, from session store rows.
- **Copies only** (``open_store_copy``): a session store is opened only when it is a copy under
  the experiment's run folder and is not the live store (different inode), read-only.
- **Observed vs modelled** (``Figure``, ``Report``): figures recomputed from recorded runs are
  ``observed``; figures from a replay or a model are ``modelled``. They are never added together,
  and a report prints them in separate sections.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from indus.research.stats import tool_succeeded

__all__ = [
    "Arm",
    "ArmError",
    "Cell",
    "Figure",
    "LiveStoreError",
    "MixedLabels",
    "Report",
    "ResultsStore",
    "RunOutcome",
    "Score",
    "TraceMetrics",
    "cell_id",
    "check_arms",
    "file_check",
    "marker",
    "matches",
    "open_store_copy",
    "read_session_rows",
    "total",
    "trace_metrics",
]

RESULTS_FILE = "results.jsonl"
CELL_ID_CHARS = 16
type Label = Literal["observed", "modelled"]


# -- cells and the results store -------------------------------------------------------------------


def cell_id(arm: str, task: str, repetition: int) -> str:
    raw = json.dumps([arm, task, repetition], separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:CELL_ID_CHARS]


@dataclass(frozen=True)
class Cell:
    arm: str
    task: str
    repetition: int

    @property
    def id(self) -> str:
        return cell_id(self.arm, self.task, self.repetition)


@dataclass(frozen=True)
class RunOutcome:
    """What one cell's run left: its exit code, its output, anything else to keep."""

    exit_code: int
    output: str
    data: Mapping[str, Any] = field(default_factory=dict[str, Any])

    @property
    def startup_crash(self) -> bool:
        return self.exit_code != 0 and not self.output.strip()


class ResultsStore:
    """``results.jsonl`` of one experiment (see the module notes)."""

    def __init__(self, folder: Path) -> None:
        self.path = folder / RESULTS_FILE

    def rows(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                value = json.loads(line)
            except ValueError:
                continue  # a torn last line: that cell runs again
            if isinstance(value, dict) and isinstance(value.get("cell"), str):
                out.append(cast(dict[str, Any], value))
        return out

    def done(self) -> set[str]:
        return {row["cell"] for row in self.rows()}

    def record(self, cell: Cell, outcome: RunOutcome) -> bool:
        """Append the cell's row; a startup crash is not recorded (False)."""
        if outcome.startup_crash:
            return False
        row = {
            "cell": cell.id,
            "arm": cell.arm,
            "task": cell.task,
            "repetition": cell.repetition,
            "exit_code": outcome.exit_code,
            "output": outcome.output,
            **dict(outcome.data),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return True

    def run(
        self,
        cells: Iterable[Cell],
        run_cell: Callable[[Cell], RunOutcome],
    ) -> tuple[list[Cell], list[Cell]]:
        """Run every cell not done yet; returns ``(recorded, crashed)``."""
        done = self.done()
        recorded: list[Cell] = []
        crashed: list[Cell] = []
        for cell in cells:
            if cell.id in done:
                continue
            outcome = run_cell(cell)
            if self.record(cell, outcome):
                recorded.append(cell)
                done.add(cell.id)
            else:
                crashed.append(cell)
        return recorded, crashed


def cells(arms: Sequence[str], tasks: Sequence[str], repetitions: int) -> list[Cell]:
    return [Cell(a, t, r) for r in range(repetitions) for t in tasks for a in arms]


# -- arms ------------------------------------------------------------------------------------------


class ArmError(ValueError):
    """Two arms that do not differ in exactly one thing."""


@dataclass(frozen=True)
class Arm:
    """One side of an experiment: a code tree on ``PYTHONPATH`` (None: the installed one) and
    config keys (dotted paths)."""

    name: str
    pythonpath: str | None = None
    config: Mapping[str, Any] = field(default_factory=dict[str, Any])

    def env(self, base: Mapping[str, str]) -> dict[str, str]:
        """``base`` with this arm's ``PYTHONPATH`` (the caller passes the scope's env view)."""
        out = dict(base)
        if self.pythonpath is not None:
            out["PYTHONPATH"] = self.pythonpath
        return out


def differences(a: Arm, b: Arm) -> list[str]:
    found: list[str] = []
    if a.pythonpath != b.pythonpath:
        found.append("PYTHONPATH")
    missing = object()
    for key in sorted(set(a.config) | set(b.config)):
        if a.config.get(key, missing) != b.config.get(key, missing):
            found.append(f"config {key}")
    return found


def check_arms(baseline: Arm, variant: Arm) -> str:
    """The one thing the arms vary; ``ArmError`` when it is not exactly one."""
    if baseline.name == variant.name:
        raise ArmError(f"both arms are named {baseline.name!r}")
    found = differences(baseline, variant)
    if not found:
        raise ArmError(f"arms {baseline.name!r} and {variant.name!r} do not differ")
    if len(found) > 1:
        raise ArmError(
            f"arms {baseline.name!r} and {variant.name!r} differ in {len(found)} things "
            f"({', '.join(found)}); an experiment varies exactly one"
        )
    return found[0]


# -- oracles ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Score:
    ok: bool
    detail: str = ""


type Oracle = Callable[[str, Path], Score]


def marker(text: str) -> Oracle:
    def score(answer: str, work: Path) -> Score:
        found = text in answer
        return Score(found, f"marker {text!r} {'found' if found else 'missing'}")

    return score


def matches(pattern: str, *, expect: str | None = None) -> Oracle:
    """The final answer matches ``pattern``; with ``expect``, its first group must equal it."""
    compiled = re.compile(pattern)

    def score(answer: str, work: Path) -> Score:
        hit = compiled.search(answer)
        if hit is None:
            return Score(False, f"no match for {pattern!r}")
        if expect is not None:
            got = hit.group(1) if compiled.groups else hit.group(0)
            return Score(got == expect, f"got {got!r}, expected {expect!r}")
        return Score(True, f"matched {hit.group(0)!r}")

    return score


def file_check(relative: str, *, contains: str | None = None) -> Oracle:
    """A file under the cell's work folder exists (and holds ``contains``)."""

    def score(answer: str, work: Path) -> Score:
        path = work / relative
        if not path.is_file():
            return Score(False, f"{relative} does not exist")
        if contains is not None:
            text = path.read_text(encoding="utf-8", errors="replace")
            ok = contains in text
            return Score(ok, f"{relative} {'holds' if ok else 'lacks'} {contains!r}")
        return Score(True, f"{relative} exists")

    return score


# -- the session store (copies only) ---------------------------------------------------------------


class LiveStoreError(ValueError):
    """The path is not a copy of the session store under the run folder."""


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
    except ValueError:
        return False
    return True


@contextmanager
def open_store_copy(path: Path, *, run_dir: Path, live: Path) -> Iterator[sqlite3.Connection]:
    """A read-only connection to ``path``; refused unless it lies under ``run_dir`` and is a
    different file from ``live`` (another inode, so not the live store or a link to it)."""
    if not _inside(path, run_dir):
        raise LiveStoreError(f"{path} is not under the run folder {run_dir}; copy the store there")
    if not path.is_file():
        raise LiveStoreError(f"{path} does not exist")
    if live.exists():
        mine, theirs = path.stat(), live.stat()
        if (mine.st_dev, mine.st_ino) == (theirs.st_dev, theirs.st_ino):
            raise LiveStoreError(f"{path} is the live session store, not a copy")
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        yield connection
    finally:
        connection.close()


def read_session_rows(connection: sqlite3.Connection, session_id: str) -> list[dict[str, Any]]:
    """The live message rows of one session, oldest first (``tool_calls`` decoded)."""
    connection.row_factory = sqlite3.Row
    found = connection.execute(
        "SELECT role, content, tool_call_id, tool_calls, tool_name, timestamp FROM messages "
        "WHERE session_id = ? AND active = 1 ORDER BY id",
        (session_id,),
    ).fetchall()
    rows: list[dict[str, Any]] = []
    for row in found:
        item = dict(row)
        raw = item.get("tool_calls")
        if isinstance(raw, str) and raw:
            try:
                item["tool_calls"] = json.loads(raw)
            except ValueError:
                item["tool_calls"] = None
        rows.append(item)
    return rows


# -- trace metrics ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class TraceMetrics:
    model_calls: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    retries: int = 0
    result_bytes: int = 0
    wall_seconds: float = 0.0

    def figures(self, label: Label = "observed") -> list[Figure]:
        return [
            Figure("model_calls", self.model_calls, label),
            Figure("tool_calls", self.tool_calls, label),
            Figure("tool_errors", self.tool_errors, label),
            Figure("retries", self.retries, label),
            Figure("result_bytes", self.result_bytes, label),
            Figure("wall_seconds", self.wall_seconds, label),
        ]


def _calls_of(row: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = row.get("tool_calls")
    if not isinstance(raw, list):
        return []
    return [cast(Mapping[str, Any], c) for c in cast(list[Any], raw) if isinstance(c, Mapping)]


def _call_name(call: Mapping[str, Any]) -> str:
    inner = call.get("function")
    spec = cast(Mapping[str, Any], inner) if isinstance(inner, Mapping) else call
    return str(spec.get("name") or "")


def trace_metrics(rows: Sequence[Mapping[str, Any]]) -> TraceMetrics:
    """Metrics of one session from its message rows (see the module notes)."""
    model_calls = tool_calls = errors = retries = size = 0
    names: dict[str, str] = {}
    last_failed: str | None = None
    stamps = [float(r["timestamp"]) for r in rows if isinstance(r.get("timestamp"), int | float)]
    for row in rows:
        role = row.get("role")
        if role == "assistant":
            model_calls += 1
            for call in _calls_of(row):
                tool_calls += 1
                name = _call_name(call)
                if name and name == last_failed:
                    retries += 1
                call_id = call.get("id")
                if isinstance(call_id, str):
                    names[call_id] = name
        elif role == "tool":
            content = row.get("content")
            text = content if isinstance(content, str) else json.dumps(content)
            size += len(text.encode("utf-8"))
            name = str(row.get("tool_name") or names.get(str(row.get("tool_call_id")), ""))
            if tool_succeeded(content):
                last_failed = None
            else:
                errors += 1
                last_failed = name
    wall = (max(stamps) - min(stamps)) if stamps else 0.0
    return TraceMetrics(model_calls, tool_calls, errors, retries, size, round(wall, 3))


# -- observed vs modelled --------------------------------------------------------------------------


class MixedLabels(ValueError):
    """Observed and modelled figures were about to be added together."""


@dataclass(frozen=True)
class Figure:
    name: str
    value: float
    label: Label

    def __post_init__(self) -> None:
        if self.label not in ("observed", "modelled"):
            raise ValueError(f"a figure is observed or modelled, not {self.label!r}")


def total(figures: Iterable[Figure]) -> Figure:
    """The sum of figures of one name and one label; mixing labels raises ``MixedLabels``."""
    items = list(figures)
    if not items:
        raise ValueError("nothing to add up")
    labels = {f.label for f in items}
    if len(labels) > 1:
        raise MixedLabels("observed and modelled figures are never added together")
    names = {f.name for f in items}
    if len(names) > 1:
        raise ValueError(f"figures of different names: {', '.join(sorted(names))}")
    return Figure(items[0].name, sum(f.value for f in items), items[0].label)


@dataclass
class Report:
    figures: list[Figure] = field(default_factory=list[Figure])

    def add(self, *figures: Figure) -> None:
        self.figures.extend(figures)

    def totals(self, label: Label) -> dict[str, float]:
        out: dict[str, float] = {}
        for name in dict.fromkeys(f.name for f in self.figures if f.label == label):
            out[name] = total(f for f in self.figures if f.label == label and f.name == name).value
        return out

    def render(self) -> str:
        lines: list[str] = []
        for label in ("observed", "modelled"):
            values = self.totals(label)
            if not values:
                continue
            lines.append(f"{label}:")
            lines.extend(f"  {name:<16}{value:g}" for name, value in values.items())
        return "\n".join(lines)
