"""Acceptance map: every P0/P1 feature in the inventory has an ACC id backed by a real test.

Inventory: table rows ``| A1 | Feature | P0 | ... |`` inside the area sections (``## A. …``) of
docs/plan/03-feature-inventory.md; a mixed tier ``P0 → P1`` is gated by its first tier, ``—`` is
dropped.
Map: rows ``- **ACC-A1-1** summary`` in docs/internals/acceptance-map.md, with coverage lines as in
the traps ledger. ``--strict`` (release) also fails on Review lines.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from tooling.checks import ledger
from tooling.checks.common import Context, Result

NAME = "acceptance"
INVENTORY = Path("docs/plan/03-feature-inventory.md")
MAP = Path("docs/internals/acceptance-map.md")
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
    parsed = ledger.parse(path.read_text(encoding="utf-8"), str(MAP))
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
            result.warnings.append(f"{MAP}:{row.line}: {rid} names an unknown feature")
        covered.add(m.group("feature"))
    for fid, (_name, prio, line) in gated.items():
        if fid not in covered:
            result.fail(f"{fid} ({prio}, {INVENTORY}:{line}) has no ACC id in {MAP}")
    problems = ledger.check_rows(parsed, ledger.KnownTests(ctx.root), str(MAP), strict=ctx.strict)
    for _, problem in problems:
        result.fail(problem)
    return result


if __name__ == "__main__":
    from tooling.checks.__main__ import main

    raise SystemExit(main([NAME, *sys.argv[1:]]))
