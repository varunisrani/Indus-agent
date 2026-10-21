"""Similarity check: shared runs of code tokens, meaningful lines or prose words.

Gate: a shared run of 50 tokens, 6 lines or 12 words is a violation. ``--closest N`` probes with
smaller windows, extends every probe to its longest run and ranks runs by run / gate size.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import pickle
import sys
import time
from array import array
from bisect import bisect_left
from collections.abc import Iterable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from tooling.checks import simtext
from tooling.checks.common import (
    CHECKS_DIR,
    Context,
    Result,
    missing_ref,
    read_text,
    ref_commit,
    rel,
    walk_files,
)

NAME = "similarity"
DETECTORS = ("tokens", "lines", "prose")
GATE = {"tokens": 50, "lines": 6, "prose": 12}
PROBE = {"tokens": 20, "lines": 3, "prose": 6}
MAX_BYTES = 2 * 1024 * 1024
ALLOW_FILE = CHECKS_DIR / "similarity_allow.json"

PRODUCT_DIRS = (
    "src",
    "apps",
    "packages",
    "tooling",
    "scripts",
    "tests",
    "skills",
    "docker",
    "nix",
    ".github",
)
PRODUCT_EXCLUDE = ("tooling/checks/tests/fixtures/",)
IDENT_FILTER_PREFIX = "docs/plan/"

LOCKFILES = frozenset(
    {
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "uv.lock",
        "poetry.lock",
        "pipfile.lock",
        "cargo.lock",
        "gemfile.lock",
        "composer.lock",
        "flake.lock",
        "bun.lock",
    }
)

MOD = (1 << 61) - 1
BASE = 1_000_003
HASH_BITS = 40
FID_BITS = 24
FID_MASK = (1 << FID_BITS) - 1
H40_MASK = (1 << HASH_BITS) - 1
BUCKETS = 256

_unit_memo: dict[str, int] = {}


# --------------------------------------------------------------------------- hashing


def unit_value(unit: str) -> int:
    v = _unit_memo.get(unit)
    if v is None:
        digest = hashlib.blake2b(unit.encode("utf-8", "replace"), digest_size=8).digest()
        v = int.from_bytes(digest, "little") % MOD
        if len(_unit_memo) < 2_000_000:
            _unit_memo[unit] = v
    return v


def window_hashes(units: list[str], size: int) -> list[int]:
    """Polynomial rolling hash of every window of ``size`` consecutive units."""
    n = len(units)
    if n < size:
        return []
    vals = [unit_value(u) for u in units]
    top = pow(BASE, size - 1, MOD)
    h = 0
    for v in vals[:size]:
        h = (h * BASE + v) % MOD
    out = [h]
    for i in range(size, n):
        h = ((h - vals[i - size] * top) * BASE + vals[i]) % MOD
        out.append(h)
    return out


def checker_hash() -> str:
    h = hashlib.blake2b(digest_size=6)
    for name in ("simtext.py", "similarity.py"):
        h.update((CHECKS_DIR / name).read_bytes())
    h.update(f"{sys.version_info[0]}.{sys.version_info[1]}".encode())
    return h.hexdigest()


# --------------------------------------------------------------------------- file selection


def is_lockfile(name: str) -> bool:
    low = name.lower()
    return low in LOCKFILES or low.endswith((".lock", ".min.js", ".min.css", ".map"))


def ref_files(ref: Path) -> list[str]:
    out = []
    for path in walk_files(ref):
        if is_lockfile(path.name) or simtext.kind_of(path.name) is None:
            continue
        try:
            if path.stat().st_size > MAX_BYTES:
                continue
        except OSError:
            continue
        out.append(rel(path, ref))
    return out


def product_files(root: Path) -> list[str]:
    out: list[str] = []
    for top in PRODUCT_DIRS:
        for path in walk_files(root / top):
            r = rel(path, root)
            if r.startswith(PRODUCT_EXCLUDE) or is_lockfile(path.name):
                continue
            if simtext.kind_of(r) is not None:
                out.append(r)
    for path in walk_files(root / "docs"):
        if path.suffix.lower() in (".md", ".mdx"):
            out.append(rel(path, root))
    for path in sorted(root.glob("*.md")):
        if path.is_file():
            out.append(rel(path, root))
    return out


def file_units(base: Path, relpath: str, product: bool = False) -> dict[str, simtext.Units]:
    path = base / relpath
    try:
        if path.stat().st_size > MAX_BYTES:
            return {}
    except OSError:
        return {}
    text = read_text(path)
    if text is None:
        return {}
    ident = product and relpath.startswith(IDENT_FILTER_PREFIX)
    return simtext.extract(relpath, text, ident_filter=ident)


# --------------------------------------------------------------------------- reference index


@dataclass
class Index:
    files: list[str]
    sizes: dict[str, int]
    tables: dict[str, array] = field(default_factory=dict)

    def candidates(self, detector: str, full_hash: int) -> Iterable[int]:
        table = self.tables.get(detector)
        if not table:
            return ()
        h40 = full_hash & H40_MASK
        lo = bisect_left(table, h40 << FID_BITS)
        out = []
        n = len(table)
        while lo < n and (table[lo] >> FID_BITS) == h40:
            out.append(table[lo] & FID_MASK)
            lo += 1
        return out


def _index_chunk(args: tuple[str, list[tuple[int, str]], dict[str, int]]) -> dict[str, list[bytes]]:
    base, items, sizes = args
    buckets = {d: [array("Q") for _ in range(BUCKETS)] for d in sizes}
    shift = 64 - 8
    root = Path(base)
    for fid, relpath in items:
        units = file_units(root, relpath)
        for det, size in sizes.items():
            got = units.get(det)
            if not got:
                continue
            hashes = window_hashes(got[0], size)
            if not hashes:
                continue
            b = buckets[det]
            for h in {x & H40_MASK for x in hashes}:
                v = (h << FID_BITS) | fid
                b[v >> shift].append(v)
    return {d: [a.tobytes() for a in arrs] for d, arrs in buckets.items()}


def build_index(ref: Path, sizes: dict[str, int], jobs: int) -> Index:
    files = ref_files(ref)
    if len(files) > FID_MASK:
        raise RuntimeError("reference has too many files for the index")
    items = list(enumerate(files))
    chunk = 150
    work = [(str(ref), items[i : i + chunk], sizes) for i in range(0, len(items), chunk)]
    merged = {d: [array("Q") for _ in range(BUCKETS)] for d in sizes}

    def absorb(part: dict[str, list[bytes]]) -> None:
        for det, blobs in part.items():
            for k, blob in enumerate(blobs):
                if blob:
                    merged[det][k].frombytes(blob)

    if jobs <= 1:
        for w in work:
            absorb(_index_chunk(w))
    else:
        with ProcessPoolExecutor(max_workers=jobs) as pool:
            for part in pool.map(_index_chunk, work):
                absorb(part)
    index = Index(files=files, sizes=dict(sizes))
    for det, arrs in merged.items():
        table = array("Q")
        for a in arrs:
            table.extend(array("Q", sorted(set(a))))
        index.tables[det] = table
    return index


def cache_dir() -> Path:
    env = os.environ.get("INDUS_CHECKS_CACHE")
    return Path(env).expanduser() if env else Path.home() / ".cache" / "indus-checks"


def load_index(ref: Path, sizes: dict[str, int], mode: str, notes: list[str]) -> Index:
    key = f"{ref_commit(ref)}-{checker_hash()}-{mode}"
    path = cache_dir() / f"{key}.pickle"
    if path.is_file():
        try:
            with path.open("rb") as fh:
                blob = pickle.load(fh)
            if blob.get("sizes") == sizes:
                idx = Index(files=blob["files"], sizes=blob["sizes"])
                for det, raw in blob["tables"].items():
                    t = array("Q")
                    t.frombytes(raw)
                    idx.tables[det] = t
                notes.append(f"reference index loaded from cache ({path.name})")
                return idx
        except (OSError, pickle.PickleError, EOFError, KeyError, ValueError):
            pass
    jobs = int(os.environ.get("INDUS_CHECKS_JOBS", "2") or "2")
    started = time.monotonic()
    idx = build_index(ref, sizes, jobs)
    notes.append(
        f"reference index built: {len(idx.files)} files in {time.monotonic() - started:.1f}s"
        f" ({', '.join(f'{d}={len(t)}' for d, t in idx.tables.items())})"
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".tmp{os.getpid()}")
        with tmp.open("wb") as fh:
            pickle.dump(
                {
                    "files": idx.files,
                    "sizes": idx.sizes,
                    "tables": {d: t.tobytes() for d, t in idx.tables.items()},
                },
                fh,
                protocol=pickle.HIGHEST_PROTOCOL,
            )
        os.replace(tmp, path)
    except OSError as exc:
        notes.append(f"could not write index cache: {exc}")
    return idx


# --------------------------------------------------------------------------- matching


@dataclass
class Hit:
    detector: str
    ours: str
    our_line: int
    ref: str
    ref_line: int
    run: int
    excerpt: str
    more: int = 0

    @property
    def score(self) -> float:
        return self.run / GATE[self.detector]

    def to_json(self) -> dict[str, object]:
        return {
            "detector": self.detector,
            "ours": f"{self.ours}:{self.our_line}",
            "ref": f"{self.ref}:{self.ref_line}",
            "run": self.run,
            "score": round(self.score, 3),
            "excerpt": self.excerpt,
            "more_refs": self.more,
        }

    def describe(self) -> str:
        more = f" (+{self.more} more)" if self.more else ""
        return (
            f"{self.ours}:{self.our_line} ~ {self.ref}:{self.ref_line}{more}"
            f" [{self.detector} run {self.run}, score {self.score:.2f}] {self.excerpt}"
        )


def _excerpt(detector: str, units: list[str]) -> str:
    sep = " | " if detector == "lines" else " "
    text = sep.join(units)
    return text if len(text) <= 200 else text[:197] + "..."


class RefFiles:
    """Lazy per-run cache of reference file units and window positions."""

    def __init__(self, ref: Path, index: Index) -> None:
        self.ref = ref
        self.index = index
        self._units: dict[int, dict[str, simtext.Units]] = {}
        self._pos: dict[tuple[int, str], dict[int, list[int]]] = {}

    def units(self, fid: int) -> dict[str, simtext.Units]:
        got = self._units.get(fid)
        if got is None:
            got = file_units(self.ref, self.index.files[fid])
            self._units[fid] = got
        return got

    def positions(self, fid: int, detector: str, size: int) -> dict[int, list[int]]:
        key = (fid, detector)
        got = self._pos.get(key)
        if got is None:
            got = {}
            units = self.units(fid).get(detector)
            if units:
                for j, h in enumerate(window_hashes(units[0], size)):
                    got.setdefault(h, []).append(j)
            self._pos[key] = got
        return got


def find_hits(
    root: Path,
    ref: Path,
    index: Index,
    files: list[str],
    max_candidates: int = 64,
) -> list[Hit]:
    refs = RefFiles(ref, index)
    grouped: dict[tuple[str, str, int, int], list[tuple[int, int]]] = {}
    for relpath in files:
        units = file_units(root, relpath, product=True)
        for det, size in index.sizes.items():
            got = units.get(det)
            if not got:
                continue
            ours = got[0]
            covered: dict[int, int] = {}  # fid -> our index up to which runs are known
            for i, h in enumerate(window_hashes(ours, size)):
                fids = index.candidates(det, h)
                for fid in list(fids)[:max_candidates]:
                    if covered.get(fid, -1) > i:
                        continue
                    pos = refs.positions(fid, det, size).get(h)
                    if not pos:
                        continue
                    theirs = refs.units(fid)[det][0]
                    for j in pos:
                        if theirs[j : j + size] != ours[i : i + size]:
                            continue
                        a, b = i, j
                        while a > 0 and b > 0 and ours[a - 1] == theirs[b - 1]:
                            a -= 1
                            b -= 1
                        end = i + size
                        tend = j + size
                        while end < len(ours) and tend < len(theirs) and ours[end] == theirs[tend]:
                            end += 1
                            tend += 1
                        grouped.setdefault((relpath, det, a, end - a), []).append((fid, b))
                        covered[fid] = max(covered.get(fid, -1), end - size + 1)
    hits: list[Hit] = []
    for (relpath, det, start, run), places in grouped.items():
        uniq = sorted(set(places))
        fid, b = uniq[0]
        ref_units = refs.units(fid)[det]
        our_units = file_units(root, relpath, product=True)[det]
        hits.append(
            Hit(
                detector=det,
                ours=relpath,
                our_line=our_units[1][start],
                ref=index.files[fid],
                ref_line=ref_units[1][b],
                run=run,
                excerpt=_excerpt(det, our_units[0][start : start + run]),
                more=len({f for f, _ in uniq}) - 1,
            )
        )
    hits.sort(key=lambda h: (-h.score, h.ours, h.our_line))
    return hits


# --------------------------------------------------------------------------- allow-list


def load_allow(path: Path = ALLOW_FILE) -> tuple[list[dict[str, str]], list[str]]:
    """Return (entries, problems)."""
    if not path.is_file():
        return [], []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [], [f"{path.name}: cannot read: {exc}"]
    if not isinstance(data, list):
        return [], [f"{path.name}: must be a JSON list"]
    entries, problems = [], []
    for n, item in enumerate(data):
        if (
            not isinstance(item, dict)
            or not str(item.get("path", "")).strip()
            or not str(item.get("reason", "")).strip()
        ):
            problems.append(f"{path.name}[{n}]: every entry needs a non-empty path and reason")
            continue
        entries.append({"path": str(item["path"]), "reason": str(item["reason"])})
    return entries, problems


def allowed(relpath: str, entries: list[dict[str, str]]) -> bool:
    return any(fnmatch.fnmatchcase(relpath, e["path"]) for e in entries)


# --------------------------------------------------------------------------- check entry


def run(ctx: Context, allow_path: Path = ALLOW_FILE) -> Result:
    result = Result(NAME)
    if missing_ref(ctx, result):
        return result
    assert ctx.ref is not None
    entries, problems = load_allow(allow_path)
    for p in problems:
        result.setup(p)
    if problems:
        return result
    closest = ctx.closest > 0
    sizes = PROBE if closest else GATE
    index = load_index(ctx.ref, dict(sizes), "closest" if closest else "gate", result.notes)
    files = product_files(ctx.root)
    skipped = [f for f in files if allowed(f, entries)]
    files = [f for f in files if f not in set(skipped)]
    hits = find_hits(ctx.root, ctx.ref, index, files, max_candidates=32 if closest else 256)
    result.notes.append(f"compared {len(files)} product files ({len(skipped)} allow-listed)")
    if closest:
        top = hits[: ctx.closest]
        result.data["closest"] = [h.to_json() for h in top]
        result.notes.append(f"closest {len(top)} of {len(hits)} probe runs (ranking, not a gate):")
        result.notes.extend(f"  {h.describe()}" for h in top)
        return result
    result.data["hits"] = [h.to_json() for h in hits]
    for h in hits:
        if h.run >= GATE[h.detector]:
            result.fail(h.describe())
    return result


if __name__ == "__main__":
    from tooling.checks.__main__ import main

    raise SystemExit(main([NAME, *sys.argv[1:]]))
