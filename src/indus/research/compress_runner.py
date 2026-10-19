"""``indus research compress``: the compressor over files and folders (P22-13).

File mode: ``x.jsonl`` → ``x_compressed.jsonl`` (or ``--output``), metrics in
``<output stem>_metrics.json``, rejects in ``<output stem>.rejects.jsonl``. Folder mode: every
``*.jsonl`` of ``DIR`` → ``DIR_compressed/`` (or ``--output``) with the same names, metrics in
``<output dir>/<metrics.file>``, rejects in ``<output dir>.rejects.jsonl`` beside the folder (a
later run over the output folder never reads it as data). The suffix comes from
``output.suffix``.

``--sample-percent P`` (0 < P ≤ 100) keeps ``max(1, floor(n · P / 100))`` entries of each file,
drawn with ``--seed`` (default 42) and kept in input order. ``--dry-run`` reads and samples,
says what it would do, and writes nothing.

Every entry runs under a semaphore of ``processing.max_concurrent_requests`` and its own
``processing.per_trajectory_timeout``. An entry that times out is **not** silently dropped
(R10.8): it goes to the rejects file as ``{source_file, line, reason: "timeout", entry}`` and is
counted. Any other failure keeps the original entry in the output and counts it failed. Output
keeps the input order of each file. Entries without a ``conversations`` list pass through as they
are. With ``processing.skip_under_target: false``, entries already under the target are left out
of the output (a set of compressed samples only); by default they pass through unchanged. The
rejects file is always written, empty when nothing timed out. The summariser is made inside the
running event loop (``make_summariser``), so a client never outlives its loop.
"""

from __future__ import annotations

import asyncio
import json
import math
import random
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from indus.foundation.fsio import atomic_write_json, atomic_write_text
from indus.research.compress_config import CompressionConfig, TokenCounter
from indus.research.compressor import Compressor, Summariser, TrajectoryMetrics

__all__ = [
    "EXIT_FAILED",
    "EXIT_OK",
    "EXIT_USAGE",
    "Aggregate",
    "RunPaths",
    "RunSettings",
    "UsageError",
    "plan_paths",
    "read_jsonl",
    "run",
    "sample_count",
]

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
DEFAULT_SEED = 42
REJECTS_SUFFIX = ".rejects.jsonl"

type Out = Callable[[str], None]
type Mode = Literal["file", "folder"]


class UsageError(ValueError):
    """Bad input for a compress run (CLI exit 2)."""


@dataclass(frozen=True)
class RunSettings:
    input: Path
    output: Path | None = None
    sample_percent: float | None = None
    seed: int = DEFAULT_SEED
    dry_run: bool = False


@dataclass(frozen=True)
class RunPaths:
    mode: Mode
    sources: tuple[Path, ...]
    outputs: Mapping[Path, Path]
    metrics: Path
    rejects: Path


def _strip_jsonl(path: Path) -> Path:
    return path.with_suffix("") if path.suffix == ".jsonl" else path


def plan_paths(settings: RunSettings, config: CompressionConfig) -> RunPaths:
    """Where every file goes (see the module notes); ``UsageError`` for a missing input."""
    source = settings.input
    suffix = config.output.suffix
    if source.is_file():
        output = settings.output or source.with_name(f"{source.stem}{suffix}.jsonl")
        stem = _strip_jsonl(output)
        return RunPaths(
            "file",
            (source,),
            {source: output},
            stem.with_name(f"{stem.name}_metrics.json"),
            stem.with_name(f"{stem.name}{REJECTS_SUFFIX}"),
        )
    if source.is_dir():
        folder = settings.output or source.with_name(f"{source.name}{suffix}")
        files = tuple(sorted(source.glob("*.jsonl")))
        if not files:
            raise UsageError(f"no .jsonl files in {source}")
        return RunPaths(
            "folder",
            files,
            {f: folder / f.name for f in files},
            folder / config.metrics.file,
            folder.with_name(f"{folder.name}{REJECTS_SUFFIX}"),
        )
    raise UsageError(f"input not found: {source}")


# -- reading and sampling --------------------------------------------------------------------------


def read_jsonl(path: Path, warn: Out) -> list[tuple[int, Any]]:
    """``(line number, value)`` of every parseable non-blank line; others are warned about."""
    out: list[tuple[int, Any]] = []
    with open(path, encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                out.append((number, json.loads(line)))
            except json.JSONDecodeError as exc:
                warn(f"{path.name} line {number}: not valid JSON ({exc.msg}); skipped")
    return out


def sample_count(total: int, percent: float) -> int:
    if total <= 0:
        return 0
    return min(total, max(1, math.floor(total * percent / 100)))


def check_percent(percent: float | None) -> None:
    if percent is not None and not (0 < percent <= 100):
        raise UsageError(f"--sample-percent must be above 0 and at most 100, got {percent:g}")


def draw(
    entries: Sequence[tuple[int, Any]], percent: float | None, rng: random.Random
) -> list[tuple[int, Any]]:
    if percent is None:
        return list(entries)
    keep = sample_count(len(entries), percent)
    chosen = sorted(rng.sample(range(len(entries)), keep))
    return [entries[i] for i in chosen]


# -- metrics ---------------------------------------------------------------------------------------


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


@dataclass
class Aggregate:
    total: int = 0
    compressed: int = 0
    skipped_under_target: int = 0
    still_over_limit: int = 0
    failed: int = 0
    timed_out: int = 0
    passed_through: int = 0
    left_out_under_target: int = 0
    tokens_before: int = 0
    tokens_after: int = 0
    turns_before: int = 0
    turns_after: int = 0
    calls: int = 0
    errors: int = 0
    ratios: list[float] = field(default_factory=list[float])
    saved: list[int] = field(default_factory=list[int])
    removed: list[int] = field(default_factory=list[int])
    started: str = ""
    finished: str = ""
    seconds: float = 0.0
    tokenizer_fallbacks: int = 0

    def add(self, metrics: TrajectoryMetrics) -> None:
        self.tokens_before += metrics.tokens_before
        self.tokens_after += metrics.tokens_after
        self.turns_before += metrics.turns_before
        self.turns_after += metrics.turns_after
        self.calls += metrics.summarisation_calls
        self.errors += metrics.summarisation_errors
        self.skipped_under_target += int(metrics.skipped_under_target)
        self.still_over_limit += int(metrics.still_over_limit)
        if metrics.was_compressed:
            self.compressed += 1
            self.ratios.append(metrics.ratio)
            self.saved.append(metrics.tokens_saved)
            self.removed.append(metrics.turns_removed)

    def as_dict(self) -> dict[str, Any]:
        def mean(values: Sequence[float], empty: float) -> float:
            return sum(values) / len(values) if values else empty

        return {
            "summary": {
                "total": self.total,
                "compressed": self.compressed,
                "skipped_under_target": self.skipped_under_target,
                "still_over_limit": self.still_over_limit,
                "failed": self.failed,
                "timed_out": self.timed_out,
                "passed_through": self.passed_through,
                "left_out_under_target": self.left_out_under_target,
                "compression_rate": round(self.compressed / max(self.total, 1), 4),
            },
            "tokens": {
                "before": self.tokens_before,
                "after": self.tokens_after,
                "saved": self.tokens_before - self.tokens_after,
                "ratio": round(self.tokens_after / max(self.tokens_before, 1), 4),
            },
            "turns": {
                "before": self.turns_before,
                "after": self.turns_after,
                "removed": self.turns_before - self.turns_after,
            },
            "averages": {
                "compression_ratio": round(mean(self.ratios, 1.0), 4),
                "tokens_saved": round(mean([float(s) for s in self.saved], 0.0), 1),
                "turns_removed": round(mean([float(r) for r in self.removed], 0.0), 2),
            },
            "summarisation": {
                "calls": self.calls,
                "errors": self.errors,
                "success_rate": round(1 - self.errors / max(self.calls, 1), 4),
            },
            "tokenizer": {"fallbacks": self.tokenizer_fallbacks},
            "timings": {
                "started": self.started,
                "finished": self.finished,
                "seconds": round(self.seconds, 2),
            },
        }


# -- one run ---------------------------------------------------------------------------------------


@dataclass
class _Item:
    source: Path
    line: int
    entry: Any
    status: Literal["kept", "left_out", "timeout"] = "kept"
    result: Any = None


async def _one(
    item: _Item,
    compressor: Compressor,
    config: CompressionConfig,
    gate: asyncio.Semaphore,
    totals: Aggregate,
    err: Out,
) -> None:
    entry = item.entry
    item.result = entry
    turns = entry.get("conversations") if isinstance(entry, dict) else None
    if not isinstance(turns, list):
        totals.passed_through += 1
        return
    async with gate:
        try:
            out, metrics = await asyncio.wait_for(
                compressor.compress(cast(list[Mapping[str, Any]], turns)),
                timeout=config.processing.per_trajectory_timeout,
            )
        except TimeoutError:
            item.status = "timeout"
            totals.timed_out += 1
            totals.failed += 1
            err(
                f"{item.source.name} line {item.line}: over "
                f"{config.processing.per_trajectory_timeout:g} s; written to the rejects file"
            )
            return
        except Exception as exc:
            totals.failed += 1
            err(
                f"{item.source.name} line {item.line}: kept unchanged ({type(exc).__name__}: {exc})"
            )
            return
    totals.add(metrics)
    if metrics.skipped_under_target and not config.processing.skip_under_target:
        item.status = "left_out"
        totals.left_out_under_target += 1
        return
    result = dict(cast(Mapping[str, Any], entry), conversations=out)
    if metrics.was_compressed and config.metrics.per_trajectory:
        result["compression_metrics"] = metrics.as_dict()
    item.result = result


async def _run_items(
    items: list[_Item],
    config: CompressionConfig,
    counter: TokenCounter,
    make_summariser: Callable[[], Summariser],
    totals: Aggregate,
    err: Out,
    rng: random.Random | None,
) -> None:
    compressor = Compressor(config, counter, make_summariser(), rng=rng)
    gate = asyncio.Semaphore(config.processing.max_concurrent_requests)
    await asyncio.gather(*(_one(i, compressor, config, gate, totals, err) for i in items))


def _lines(values: Sequence[Any]) -> str:
    return "".join(json.dumps(v, ensure_ascii=False) + "\n" for v in values)


def run(
    settings: RunSettings,
    config: CompressionConfig,
    *,
    counter: Callable[[], TokenCounter],
    make_summariser: Callable[[], Summariser],
    out: Out = print,
    err: Out = print,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    rng: random.Random | None = None,
) -> int:
    """One compress run (see the module notes); returns the exit code. ``counter`` loads the
    tokenizer (only when something is to be written: a dry run never loads it)."""
    try:
        check_percent(settings.sample_percent)
        paths = plan_paths(settings, config)
    except UsageError as exc:
        err(f"research compress: {exc}")
        return EXIT_USAGE
    picker = random.Random(settings.seed)
    items: list[_Item] = []
    for source in paths.sources:
        entries = read_jsonl(source, lambda line: err(f"research compress: {line}"))
        kept = draw(entries, settings.sample_percent, picker)
        if settings.sample_percent is not None:
            out(f"{source.name}: sampled {len(kept)} of {len(entries)} entries")
        items.extend(_Item(source, number, entry) for number, entry in kept)
    out(f"{len(items)} entries from {len(paths.sources)} file(s) ({paths.mode} mode)")
    if settings.dry_run:
        for source in paths.sources:
            out(f"would write {paths.outputs[source]}")
        out(f"would write {paths.metrics} and {paths.rejects}")
        out("dry run: nothing written")
        return EXIT_OK
    try:
        tokens = counter()
    except Exception as exc:
        err(f"research compress: {exc}")
        return EXIT_USAGE
    totals = Aggregate(total=len(items), started=_iso(now()))
    began = time.monotonic()
    asyncio.run(_run_items(items, config, tokens, make_summariser, totals, err, rng))
    totals.seconds = time.monotonic() - began
    totals.finished = _iso(now())
    totals.tokenizer_fallbacks = tokens.fallbacks
    try:
        _write(paths, items, totals, config)
    except OSError as exc:
        err(f"research compress: cannot write the output: {exc}")
        return EXIT_FAILED
    _report(totals, paths, out)
    return EXIT_OK


def _write(
    paths: RunPaths, items: list[_Item], totals: Aggregate, config: CompressionConfig
) -> None:
    for source in paths.sources:
        target = paths.outputs[source]
        target.parent.mkdir(parents=True, exist_ok=True)
        kept = [i.result for i in items if i.source == source and i.status == "kept"]
        atomic_write_text(target, _lines(kept))
    rejects = [
        {"source_file": str(i.source), "line": i.line, "reason": "timeout", "entry": i.entry}
        for i in items
        if i.status == "timeout"
    ]
    paths.rejects.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(paths.rejects, _lines(rejects))
    if config.metrics.enabled:
        atomic_write_json(paths.metrics, totals.as_dict())


def _report(totals: Aggregate, paths: RunPaths, out: Out) -> None:
    data = totals.as_dict()
    summary, tokens = data["summary"], data["tokens"]
    out(
        f"compressed {summary['compressed']} of {summary['total']} "
        f"(under target {summary['skipped_under_target']}, still over "
        f"{summary['still_over_limit']}, failed {summary['failed']}, timed out "
        f"{summary['timed_out']})"
    )
    out(f"tokens {tokens['before']} → {tokens['after']} (ratio {tokens['ratio']})")
    for target in dict.fromkeys(paths.outputs.values()):
        out(f"wrote {target}")
    out(f"metrics: {paths.metrics}")
    out(f"rejects: {paths.rejects} ({summary['timed_out']} entries)")
