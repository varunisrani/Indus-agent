"""Traps ledger: every trap named in the plan has a row in docs/internals/traps-ledger.md.

Trap ids come from the plan's "traps" sections: research specs use ``R<nn>.<n>`` (section
"Hard parts / traps"), phase docs use ``T<nn>.<n>``. Only ids in a definition position (start of
a list item or table row, bold or in backticks) count, so cross-references do not create ids.

``python -m tooling.checks.traps --write-skeleton`` creates the ledger, or appends rows for ids it
lacks, each with ``Review: not yet planned``.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from tooling.checks import ledger
from tooling.checks.common import Context, Result, rel

NAME = "traps"
LEDGER = Path("docs/internals/traps-ledger.md")
PLAN = Path("docs/plan")
HEADING = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<text>.*)$")
DEFINITION = re.compile(
    r"^\s*(?:\d+[.)]|[-*]|\|)\s*(?:\*\*|`)(?P<id>[RT]\d{2}\.\d+[a-z]?)\b(?P<rest>.*)$"
)
TRAP_ID = re.compile(r"^[RT]\d{2}\.\d+[a-z]?$")
HEADER = """# Traps ledger

Every trap from the plan (research specs `R<nn>.<n>`, phase docs `T<nn>.<n>`) has a row here.
Coverage lines under a row are either a test reference such as
`` `tests/unit/test_example.py::test_name` `` or `Review: <why> (<task id>)`.
Checked by `python -m tooling.checks traps`.
"""


def _summary(rest: str) -> str:
    """The short title after the id: the remainder of the bold span, or the following text."""
    if rest.startswith(("**", "`")):  # the id closed its own span: the title follows
        text = rest.lstrip("*`").strip(" |")
        text = text.split("|", 1)[0]
    else:  # **R01.1 Title.** — the title is the rest of the bold span
        text = rest.split("**", 1)[0]
    text = text.strip(" `*|").rstrip(".").strip()
    return text[:100]


def plan_traps(root: Path) -> dict[str, tuple[str, str]]:
    """Trap id -> (where defined, short summary)."""
    found: dict[str, tuple[str, str]] = {}
    plan = root / PLAN
    if not plan.is_dir():
        return found
    for path in sorted(plan.rglob("*.md")):
        level = 0  # heading level of the open traps section; 0 = not in one
        in_fence = False
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            h = HEADING.match(line)
            if h:
                depth = len(h.group("hashes"))
                if level and depth <= level:
                    level = 0
                if "trap" in h.group("text").lower():
                    level = depth
                continue
            if not level:
                continue
            m = DEFINITION.match(line)
            if m and m.group("id") not in found:
                found[m.group("id")] = (f"{rel(path, root)}:{no}", _summary(m.group("rest")))
    return found


def run(ctx: Context) -> Result:
    result = Result(NAME)
    traps = plan_traps(ctx.root)
    path = ctx.root / LEDGER
    if ctx.write_skeleton:
        rows = [(tid, summary) for tid, (_, summary) in traps.items()]
        added = ledger.append_rows(path, HEADER, rows, "P00-guards")
        result.notes.append(f"{LEDGER}: added {added} rows")
    result.notes.append(f"{len(traps)} trap ids in {PLAN}/")
    if not path.is_file():
        if traps:
            result.fail(
                f"{LEDGER} is missing; create it with"
                " `python -m tooling.checks.traps --write-skeleton`"
            )
        return result
    parsed = ledger.parse(path.read_text(encoding="utf-8"), str(LEDGER))
    for p in parsed.problems:
        result.fail(p)
    result.warnings.extend(parsed.warnings)
    for tid, (where, _) in traps.items():
        if tid not in parsed.rows:
            result.fail(f"{tid} ({where}) has no row in {LEDGER}")
    for rid, row in parsed.rows.items():
        if not TRAP_ID.match(rid):
            result.fail(f"{LEDGER}:{row.line}: {rid} is not a trap id (R<nn>.<n> or T<nn>.<n>)")
        elif rid not in traps:
            result.warnings.append(f"{LEDGER}:{row.line}: {rid} is not in the plan any more")
    for _, problem in ledger.check_rows(parsed, ledger.KnownTests(ctx.root), str(LEDGER)):
        result.fail(problem)
    reviews = sum(1 for r in parsed.rows.values() if any(c.kind == "review" for c in r.coverage))
    result.data = {"traps": len(traps), "rows": len(parsed.rows), "review_rows": reviews}
    return result


if __name__ == "__main__":
    from tooling.checks.__main__ import main

    raise SystemExit(main([NAME, *sys.argv[1:]]))
