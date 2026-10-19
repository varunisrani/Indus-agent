"""``indus research sample``: draw long trajectories from datasets and compress them (P22-15).

The datasets come from a list the user gives (``--dataset``, repeatable, or config
``research.sample_datasets``); there is no default dataset, and an empty list is a usage error
(exit 2). Each dataset's rows (objects with a ``conversations`` list) are tagged with their
source and counted with the compressor's tokenizer; rows with at least ``min_tokens`` tokens
(16000) form one pool, from which ``samples`` (2500) are drawn with ``seed`` (42), or all of them
when the pool is smaller. The draw is written in batch files to ``<out>/<name>.parts/input``,
compressed in folder mode (P22-13) into ``<out>/<name>.parts/compressed`` and merged into one
file, ``<out>/<name>.jsonl``.

Datasets load through an injected loader; the default uses Hugging Face ``datasets``
(``load_dataset(name, split="train")``, the optional ``research`` extra), imported only when used.
"""

from __future__ import annotations

import json
import random
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from indus.foundation.fsio import atomic_write_text
from indus.research.compress_config import TokenCounter

__all__ = [
    "DEFAULT_MIN_TOKENS",
    "DEFAULT_NAME",
    "DEFAULT_SAMPLES",
    "SampleError",
    "SampleSettings",
    "dataset_list",
    "draw",
    "hf_loader",
    "run_sample",
]

DEFAULT_MIN_TOKENS = 16000
DEFAULT_SAMPLES = 2500
DEFAULT_SEED = 42
DEFAULT_NAME = "compressed_samples"
BATCH_SIZE = 100
CONFIG_KEY = "research.sample_datasets"
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2

type Out = Callable[[str], None]
type Loader = Callable[[str], Iterable[Mapping[str, Any]]]


class SampleError(ValueError):
    """The sample run cannot start (CLI exit 2)."""


@dataclass(frozen=True)
class SampleSettings:
    datasets: tuple[str, ...]
    out_dir: Path
    name: str = DEFAULT_NAME
    samples: int = DEFAULT_SAMPLES
    min_tokens: int = DEFAULT_MIN_TOKENS
    seed: int = DEFAULT_SEED
    batch_size: int = BATCH_SIZE


def dataset_list(given: Sequence[str], configured: object) -> tuple[str, ...]:
    """``--dataset`` names, else the configured list; neither → ``SampleError``."""
    names = [n.strip() for n in given if n.strip()]
    if not names and isinstance(configured, list):
        names = [str(n).strip() for n in cast(list[Any], configured) if str(n).strip()]
    if not names:
        raise SampleError(
            "no datasets to sample from: pass --dataset NAME (repeatable) or set "
            f"{CONFIG_KEY} in the config; there is no default dataset"
        )
    return tuple(dict.fromkeys(names))


def hf_loader(name: str) -> list[dict[str, Any]]:
    """Every row of a Hugging Face dataset's ``train`` split."""
    try:
        from datasets import load_dataset  # pyright: ignore[reportMissingImports]
    except ImportError as exc:
        raise SampleError(
            "loading datasets needs the 'datasets' package: install the research extra"
        ) from exc
    data: Any = load_dataset(name, split="train")  # pyright: ignore[reportUnknownVariableType]
    return [dict(cast(Mapping[str, Any], row)) for row in data]


def _tokens(entry: Mapping[str, Any], counter: TokenCounter) -> int:
    turns = entry.get("conversations")
    if not isinstance(turns, list):
        return 0
    total = 0
    for turn in cast(list[Any], turns):
        if isinstance(turn, Mapping):
            value = cast(Mapping[str, Any], turn).get("value")
            if isinstance(value, str):
                total += counter.count(value)
    return total


def draw(
    settings: SampleSettings, loader: Loader, counter: TokenCounter, out: Out
) -> list[dict[str, Any]]:
    """The pooled rows over the threshold, then the seeded draw."""
    pool: list[dict[str, Any]] = []
    for name in settings.datasets:
        rows = list(loader(name))
        kept = 0
        for row in rows:
            tokens = _tokens(row, counter)
            if tokens >= settings.min_tokens:
                pool.append({**dict(row), "sampled_from": name, "sampled_tokens": tokens})
                kept += 1
        out(f"{name}: {kept} of {len(rows)} rows have at least {settings.min_tokens} tokens")
    if len(pool) <= settings.samples:
        out(f"using all {len(pool)} rows over the threshold")
        return pool
    rng = random.Random(settings.seed)
    picked = sorted(rng.sample(range(len(pool)), settings.samples))
    out(f"drew {settings.samples} of {len(pool)} rows (seed {settings.seed})")
    return [pool[i] for i in picked]


def _lines(rows: Iterable[Any]) -> str:
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)


def write_batches(rows: Sequence[Mapping[str, Any]], folder: Path, size: int) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for number, start in enumerate(range(0, len(rows), max(1, size))):
        path = folder / f"batch_{number:04d}.jsonl"
        atomic_write_text(path, _lines(rows[start : start + size]))
        paths.append(path)
    return paths


def merge(folder: Path, target: Path) -> int:
    rows: list[Any] = []
    for path in sorted(folder.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    atomic_write_text(target, _lines(rows))
    return len(rows)


def run_sample(
    settings: SampleSettings,
    *,
    loader: Loader,
    counter: Callable[[], TokenCounter],
    compress: Callable[[Path, Path], int],
    out: Out = print,
    err: Out = print,
) -> int:
    """One sample run (see the module notes); returns the exit code. ``compress(input folder,
    output folder)`` runs the compressor in folder mode."""
    try:
        tokens = counter()
    except Exception as exc:
        err(f"research sample: {exc}")
        return EXIT_USAGE
    try:
        rows = draw(settings, loader, tokens, out)
    except SampleError as exc:
        err(f"research sample: {exc}")
        return EXIT_USAGE
    except Exception as exc:
        err(f"research sample: loading the datasets failed: {type(exc).__name__}: {exc}")
        return EXIT_FAILED
    if not rows:
        err(f"research sample: no row has {settings.min_tokens} tokens or more; nothing to do")
        return EXIT_FAILED
    work = settings.out_dir / f"{settings.name}.parts"
    source, compressed = work / "input", work / "compressed"
    write_batches(rows, source, settings.batch_size)
    code = compress(source, compressed)
    if code:
        return code
    target = settings.out_dir / f"{settings.name}.jsonl"
    count = merge(compressed, target)
    out(f"merged {count} compressed rows into {target}")
    return EXIT_OK
