"""Similarity check: shared runs of code tokens, meaningful lines or prose words.

Gate: a shared run of 50 tokens, 6 lines or 12 words is a violation. ``--closest N`` probes with
smaller windows, extends every probe to its longest run, scores each run by run / gate size, and
merges runs that sit close together in the same pair of files into regions (P25-18); the N
regions with the highest total score are printed with line ranges on both sides.

``--closest N --report`` also writes ``docs/internals/similarity-report.md``: one row per region
with a verdict a person fills in (``independent``, ``protocol-mandated``, ``convention`` or
``fix``). Rows are matched across runs by a key (both paths and a hash of the strongest run), so
verdicts survive regeneration; rows that drop out of the top N move to an "earlier" table and keep
their verdict. In report mode a top-N region without a verdict, with an unknown verdict, or with
``fix`` is a violation: that is the release gate. Not pass/fail by score.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import pickle
import re
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
    our_end: int = 0  # last line of the run on our side (0: same as the first)
    ref_end: int = 0

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
                our_end=our_units[1][start + run - 1],
                ref_end=ref_units[1][min(b + run, len(ref_units[1])) - 1],
            )
        )
    hits.sort(key=lambda h: (-h.score, h.ours, h.our_line))
    return hits


# --------------------------------------------------------------------------- regions (P25-18)

REGION_GAP = 12  # lines: runs this close on both sides belong to one region
REPORT = Path("docs/internals/similarity-report.md")
VERDICTS = ("independent", "protocol-mandated", "convention", "fix")


@dataclass
class Region:
    ours: str
    ref: str
    our_start: int
    our_end: int
    ref_start: int
    ref_end: int
    hits: list[Hit] = field(default_factory=list[Hit])

    @property
    def score(self) -> float:
        return round(sum(h.score for h in self.hits), 3)

    @property
    def top(self) -> Hit:
        return max(self.hits, key=lambda h: (h.score, -h.our_line))

    @property
    def detectors(self) -> str:
        return ", ".join(sorted({h.detector for h in self.hits}))

    @property
    def key(self) -> str:
        digest = hashlib.sha1(self.top.excerpt.encode("utf-8")).hexdigest()[:10]
        return f"{self.ours} ~ {self.ref} ~ {digest}"

    def near(self, h: Hit) -> bool:
        return (
            h.ours == self.ours
            and h.ref == self.ref
            and h.our_line <= self.our_end + REGION_GAP
            and (h.our_end or h.our_line) >= self.our_start - REGION_GAP
            and h.ref_line <= self.ref_end + REGION_GAP
            and (h.ref_end or h.ref_line) >= self.ref_start - REGION_GAP
        )

    def add(self, h: Hit) -> None:
        self.hits.append(h)
        self.our_start = min(self.our_start, h.our_line)
        self.our_end = max(self.our_end, h.our_end or h.our_line)
        self.ref_start = min(self.ref_start, h.ref_line)
        self.ref_end = max(self.ref_end, h.ref_end or h.ref_line)

    def describe(self) -> str:
        return (
            f"{self.ours}:{self.our_start}-{self.our_end} ~ {self.ref}:{self.ref_start}-"
            f"{self.ref_end} [score {self.score:.2f}, {len(self.hits)} run(s), {self.detectors}]"
            f" {self.top.excerpt}"
        )

    def to_json(self) -> dict[str, object]:
        return {
            "key": self.key,
            "ours": f"{self.ours}:{self.our_start}-{self.our_end}",
            "ref": f"{self.ref}:{self.ref_start}-{self.ref_end}",
            "score": self.score,
            "runs": len(self.hits),
            "detectors": self.detectors,
            "excerpt": self.top.excerpt,
        }


def regions(hits: Iterable[Hit]) -> list[Region]:
    """Group runs into regions, highest total score first."""
    out: list[Region] = []
    for h in sorted(hits, key=lambda h: (h.ours, h.ref, h.our_line, h.ref_line)):
        home = next((r for r in reversed(out) if r.near(h)), None)
        if home is None:
            end, ref_end = h.our_end or h.our_line, h.ref_end or h.ref_line
            out.append(Region(h.ours, h.ref, h.our_line, end, h.ref_line, ref_end, [h]))
        else:
            home.add(h)
    out.sort(key=lambda r: (-r.score, r.ours, r.our_start))
    return out


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|").replace("`", "'")


def _cells(line: str) -> list[str]:
    body = line.strip()
    if not (body.startswith("|") and body.endswith("|")):
        return []
    parts = re.split(r"(?<!\\)\|", body[1:-1])
    return [p.strip().replace("\\|", "|") for p in parts]


def read_verdicts(path: Path) -> dict[str, tuple[str, str]]:
    """Key -> (verdict, note) from an existing report (both tables)."""
    out: dict[str, tuple[str, str]] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        cells = _cells(line)
        if len(cells) < 4 or cells[0] in ("#", "Key") or set(cells[0]) <= {"-", ":"}:
            continue
        key, verdict, note = cells[1], cells[-2], cells[-1]
        if " ~ " in key:
            out[key] = (verdict.strip().lower(), note)
    return out


def write_report(
    path: Path, top: list[Region], earlier: dict[str, tuple[str, str]], commit: str, n: int
) -> None:
    lines = [
        "# Similarity report",
        "",
        f"The {n} regions of our tree closest to the reference (`{commit}`), ranked by the n-gram "
        "score (sum of shared-run lengths over the gate size, for runs within "
        f"{REGION_GAP} lines of each other on both sides). Written by "
        f"`python -m tooling.checks similarity --require-ref --closest {n} --report`; a person "
        "reads each region and fills in **Verdict** (`independent`, `protocol-mandated`, "
        "`convention` or `fix`) and **Note**. A region without a verdict, or marked `fix`, fails "
        "the release gate. Not pass/fail by score. Rows are matched across runs by **Key**; edit "
        "only the last two columns.",
        "",
        "| # | Key | Ours | Reference | Score | Detectors | Excerpt (ours) | Verdict | Note |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(top, 1):
        verdict, note = earlier.get(r.key, ("", ""))
        lines.append(
            f"| {i} | {_cell(r.key)} | {_cell(f'{r.ours}:{r.our_start}-{r.our_end}')} | "
            f"{_cell(f'{r.ref}:{r.ref_start}-{r.ref_end}')} | {r.score:.2f} | {r.detectors} | "
            f"{_cell(r.top.excerpt)} | {_cell(verdict)} | {_cell(note)} |"
        )
    shown = {r.key for r in top}
    kept = {k: v for k, v in earlier.items() if k not in shown and (v[0] or v[1])}
    if kept:
        lines += [
            "",
            "## Reviewed earlier, not in the current top list",
            "",
            "| # | Key | Verdict | Note |",
            "|---|---|---|---|",
        ]
        for i, (key, (verdict, note)) in enumerate(sorted(kept.items()), 1):
            lines.append(f"| {i} | {_cell(key)} | {_cell(verdict)} | {_cell(note)} |")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def gate_report(top: list[Region], verdicts: dict[str, tuple[str, str]], result: Result) -> None:
    for i, r in enumerate(top, 1):
        verdict = verdicts.get(r.key, ("", ""))[0]
        where = f"{REPORT} row {i} ({r.ours}:{r.our_start}-{r.our_end})"
        if not verdict:
            result.fail(f"{where}: no verdict yet")
        elif verdict not in VERDICTS:
            result.fail(f"{where}: unknown verdict {verdict!r} (use {', '.join(VERDICTS)})")
        elif verdict == "fix":
            result.fail(f"{where}: marked fix: rewrite the region, then regenerate the report")


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
        ranked = regions(hits)
        best = ranked[: ctx.closest]
        result.data["regions"] = [r.to_json() for r in best]
        result.notes.append(
            f"closest {len(best)} of {len(ranked)} regions ({len(hits)} probe runs; "
            "a ranking for a person to read):"
        )
        result.notes.extend(f"  {i}. {r.describe()}" for i, r in enumerate(best, 1))
        if ctx.report:
            path = ctx.root / REPORT
            earlier = read_verdicts(path)
            write_report(path, best, earlier, ref_commit(ctx.ref), ctx.closest)
            result.notes.append(f"wrote {REPORT}")
            gate_report(best, earlier, result)
        return result
    result.data["hits"] = [h.to_json() for h in hits]
    for h in hits:
        if h.run >= GATE[h.detector]:
            result.fail(h.describe())
    return result


if __name__ == "__main__":
    from tooling.checks.__main__ import main

    raise SystemExit(main([NAME, *sys.argv[1:]]))
