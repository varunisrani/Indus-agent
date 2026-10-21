"""Acceptance map: every P0/P1 feature in the inventory has an ACC id backed by a real test.

Inventory: table rows ``| A1 | Feature | P0 | ... |`` inside the area sections (``## A. …``) of
docs/plan/03-feature-inventory.md; a mixed tier ``P0 → P1`` is gated by its first tier, ``—`` is
dropped.
Map: rows ``- **ACC-A1-1** summary`` in docs/internals/acceptance-map.md, with coverage lines as in
the traps ledger. Features 1.0 leaves out are listed after a ``## Known gaps`` heading as
``- **A42** what is left out (D-16.41)``, each citing a §2.8 D-id of 16-what-we-change.md.

``--strict`` (the release gate) also fails on Review lines, on ACC ids whose feature is no longer
in the inventory, and on cited tests that ``pytest --collect-only`` (or, for TypeScript, a scan of
the spec file) does not find or finds skipped (``tooling.checks.collect``).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from tooling.checks import collect, ledger
from tooling.checks.common import Context, Result

NAME = "acceptance"
INVENTORY = Path("docs/plan/03-feature-inventory.md")
MAP = Path("docs/internals/acceptance-map.md")
DECISIONS = Path("docs/plan/16-what-we-change.md")
GAPS_HEADING = re.compile(r"^##\s+Known gaps\b", re.IGNORECASE)
GAP_ROW = re.compile(r"^[-*]\s+\*\*(?P<id>[^*\s]+)\*\*\s*(?P<text>.*)$")
D_ID = re.compile(r"\bD-16\.\d+\b")
FEATURE_ID = re.compile(r"^[A-Z]{1,3}\d{1,3}[a-z]?$")
TIER = re.compile(r"^\**(?P<first>P[0-3]|—)\**(?:\s*(?:→|->)\s*\**(?:P[0-3]|—)\**)*(?:\s*\(.*\))?$")
AREA_HEADING = re.compile(r"^##\s+([A-Z])\.\s")
DROPPED = "—"
ACC_ID = re.compile(r"^ACC-(?P<feature>[A-Z]{1,3}\d{1,3}[a-z]?)-(?P<n>\d+)$")
GATED = ("P0", "P1")
HEADER = """# Acceptance map

Each P0/P1 feature of `docs/plan/03-feature-inventory.md` has at least one acceptance id
`ACC-<feature>-<n>`. Coverage lines are a test reference in backticks or
`Review: <why> (<task id>)`; `--strict` (release) accepts test references only.
Checked by `python -m tooling.checks acceptance`.
"""


def _short(text: str, limit: int = 100) -> str:
    """A one-line row summary: the feature text cut at a word boundary."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:") + " …"


def inventory(root: Path) -> dict[str, tuple[str, str, int]]:
    """Feature id -> (name, gated tier, line).

    Only rows inside area sections (``## <Letter>. …``) count, so tier-count tables elsewhere are
    never read. The tier is the first tier of a cell like ``P0 → P1``; ``—`` (dropped) is kept as
    the tier "—"; a row without a recognisable tier gets "".
    """
    out: dict[str, tuple[str, str, int]] = {}
    path = root / INVENTORY
    area = ""
    for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.startswith("#"):
            h = AREA_HEADING.match(line)
            if h:
                area = h.group(1)
            elif line.startswith("## ") or line.startswith("# "):
                area = ""
            continue
        if not area or not line.lstrip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        fid = cells[0].strip("*` ")
        if not FEATURE_ID.match(fid) or not fid.startswith(area):
            continue
        tier = next((m.group("first") for c in cells[1:] if (m := TIER.match(c))), "")
        name = cells[1] if len(cells) > 1 else ""
        out.setdefault(fid, (name, tier, no))
    return out


def not_in_1_0(root: Path) -> set[str]:
    """D-ids of the "Not in 1.0" table (§2.8) of 16-what-we-change.md."""
    path = root / DECISIONS
    if not path.is_file():
        return set()
    out: set[str] = set()
    inside = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("#"):
            inside = bool(re.match(r"^#{2,4}\s+2\.8\b", line))
            continue
        if inside and line.lstrip().startswith("|"):
            first = line.strip().strip("|").split("|")[0]
            out.update(D_ID.findall(first))
    return out


def split_gaps(text: str) -> tuple[str, list[tuple[int, str]]]:
    """The map with its ``## Known gaps`` section blanked (line numbers kept), and that
    section's numbered lines."""
    lines = text.splitlines()
    gaps: list[tuple[int, str]] = []
    inside = False
    for i, line in enumerate(lines):
        if line.startswith("#"):
            inside = bool(GAPS_HEADING.match(line))
            if inside:
                lines[i] = ""
            continue
        if inside:
            gaps.append((i + 1, line))
            lines[i] = ""
    return "\n".join(lines) + "\n", gaps


def check_gaps(
    gaps: list[tuple[int, str]],
    features: dict[str, tuple[str, str, int]],
    allowed: set[str],
    result: Result,
) -> set[str]:
    """Validate the Known gaps rows; returns the features they cover."""
    covered: set[str] = set()
    for no, raw in gaps:
        m = GAP_ROW.match(raw)
        if not m:
            continue
        fid = m.group("id")
        cited = D_ID.findall(m.group("text"))
        where = f"{MAP}:{no}"
        if fid not in features:
            result.fail(f"{where}: Known gap {fid} is not a feature of {INVENTORY}")
            continue
        if not cited:
            result.fail(f"{where}: Known gap {fid} cites no D-16 id (§2.8 of {DECISIONS})")
            continue
        unknown = [d for d in cited if d not in allowed]
        if unknown:
            result.fail(
                f"{where}: Known gap {fid} cites {', '.join(unknown)}, not in §2.8 of {DECISIONS}"
            )
            continue
        covered.add(fid)
    return covered


def run(ctx: Context) -> Result:
    result = Result(NAME)
    if not (ctx.root / INVENTORY).is_file():
        result.notes.append(f"no inventory yet ({INVENTORY}); nothing to check")
        return result
    features = inventory(ctx.root)
    gated = {f: v for f, v in features.items() if v[1] in GATED}
    path = ctx.root / MAP
    if ctx.write_skeleton:
        rows = [(f"ACC-{f}-1", _short(name)) for f, (name, _, _) in gated.items()]
        existing = ledger.parse(path.read_text(encoding="utf-8"), "") if path.is_file() else None
        if existing is not None:
            have = {m.group("feature") for r in existing.rows if (m := ACC_ID.match(r))}
            rows = [r for r in rows if r[0].split("-")[1] not in have]
        added = ledger.append_rows(path, HEADER, rows, "phase TBD")
        result.notes.append(f"{MAP}: added {added} rows")
    dropped = sum(1 for v in features.values() if v[1] == DROPPED)
    result.notes.append(f"{len(features)} features, {len(gated)} at P0/P1, {dropped} dropped")
    no_prio = [f for f, v in features.items() if not v[1]]  # "—" (dropped) is a tier
    if no_prio:
        result.warnings.append(f"features without a P0–P3 priority: {', '.join(no_prio[:20])}")
    if not path.is_file():
        if gated:
            result.fail(
                f"{MAP} is missing; create it with"
                " `python -m tooling.checks.acceptance --write-skeleton`"
            )
        return result
    body, gaps = split_gaps(path.read_text(encoding="utf-8"))
    parsed = ledger.parse(body, str(MAP))
    for p in parsed.problems:
        result.fail(p)
    result.warnings.extend(parsed.warnings)
    covered: set[str] = set()
    for rid, row in parsed.rows.items():
        m = ACC_ID.match(rid)
        if not m:
            result.fail(f"{MAP}:{row.line}: {rid} is not an ACC-<feature>-<n> id")
            continue
        if m.group("feature") not in features:
            message = f"{MAP}:{row.line}: {rid} names a feature not in {INVENTORY}"
            if ctx.strict:
                result.fail(message)
            else:
                result.warnings.append(message)
        covered.add(m.group("feature"))
    gap_features = check_gaps(gaps, features, not_in_1_0(ctx.root), result)
    for fid, (_name, prio, line) in gated.items():
        if fid not in covered and fid not in gap_features:
            result.fail(f"{fid} ({prio}, {INVENTORY}:{line}) has no ACC id in {MAP}")
    known = ledger.KnownTests(ctx.root)
    problems = ledger.check_rows(parsed, known, str(MAP), strict=ctx.strict)
    for _, problem in problems:
        result.fail(problem)
    if ctx.strict:
        for problem in collect.strict_problems(ctx.root, parsed, str(MAP), known):
            result.fail(problem)
    reviews = sum(1 for r in parsed.rows.values() if any(c.kind == "review" for c in r.coverage))
    result.data = {
        "features": len(features),
        "gated": len(gated),
        "rows": len(parsed.rows),
        "review_rows": reviews,
        "known_gaps": sorted(gap_features),
    }
    return result


if __name__ == "__main__":
    from tooling.checks.__main__ import main

    raise SystemExit(main([NAME, *sys.argv[1:]]))
