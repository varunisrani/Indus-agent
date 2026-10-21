"""Traps ledger: every trap named in the plan has a row in docs/internals/traps-ledger.md.

Trap ids come from the plan's "traps" sections: research specs use ``R<nn>.<n>`` (section
"Hard parts / traps"), phase docs use ``T<nn>.<n>``. Only ids in a definition position (start of
a list item or table row, bold or in backticks) count, so cross-references do not create ids.

``python -m tooling.checks.traps --write-skeleton`` creates the ledger, or appends rows for ids it
lacks, each with ``Review: not yet planned``.

Review lines (P25-11): every ``Review: <why> (<owner>)`` must name an owner task (``P<nn>`` or
``P<nn>-<n>``) and a reason. The skeleton placeholder ``not yet planned`` is not a reason: it is a
warning in the everyday check and a failure with ``--strict``. With ``--strict`` (the release
gate) only reviews the lead accepted pass — ``Review: accepted by the lead <YYYY-MM-DD>: <why>
(<owner>)``, for traps that cannot be tested — and cited tests must be collected and not skipped
(``tooling.checks.collect``). Rows without any test reference are listed as "unpinned".
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from tooling.checks import collect, ledger
from tooling.checks.common import Context, Result, rel

NAME = "traps"
LEDGER = Path("docs/internals/traps-ledger.md")
PLAN = Path("docs/plan")
HEADING = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<text>.*)$")
DEFINITION = re.compile(
    r"^\s*(?:\d+[.)]|[-*]|\|)\s*(?:\*\*|`)(?P<id>[RT]\d{2}\.\d+[a-z]?)\b(?P<rest>.*)$"
)
TRAP_ID = re.compile(r"^[RT]\d{2}\.\d+[a-z]?$")
RESEARCH = PLAN / "research"
OWNER = re.compile(r"\((?P<inside>[^()]*)\)\s*$")
TASK_ID = re.compile(r"\bP\d{2}(?:-\d+[a-z]?)?\b")
PLACEHOLDER = re.compile(r"^not yet planned\b", re.IGNORECASE)
ACCEPTED = re.compile(
    r"^accepted by the lead (?P<date>\d{4}-\d{2}-\d{2})\s*:\s*(?P<why>.*)$", re.IGNORECASE
)
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


def review_problem(why: str, *, strict: bool) -> tuple[str, str] | None:
    """(severity, message) for one Review line, or None when it is acceptable here.

    Severity is "fail" or "warn"."""
    owner = OWNER.search(why)
    if owner is None or not TASK_ID.search(owner.group("inside")):
        return "fail", "Review line names no owner task (P<nn> or P<nn>-<n>)"
    reason = why[: owner.start()].strip()
    accepted = ACCEPTED.match(reason)
    if accepted:
        if not accepted.group("why").strip():
            return "fail", "accepted review gives no reason"
        return None
    if not reason or PLACEHOLDER.match(reason):
        return ("fail" if strict else "warn"), "Review placeholder 'not yet planned' (no reason)"
    if strict:
        return "fail", "Review row (strict mode): only reviews accepted by the lead pass"
    return None


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
    known = ledger.KnownTests(ctx.root)
    for _, problem in ledger.check_rows(parsed, known, str(LEDGER)):
        result.fail(problem)
    for rid, row in parsed.rows.items():
        for cov in row.coverage:
            if cov.kind != "review":
                continue
            found = review_problem(cov.value, strict=ctx.strict)
            if found is None:
                continue
            severity, message = found
            line = f"{LEDGER}:{cov.line}: {rid}: {message}"
            if severity == "fail":
                result.fail(line)
            else:
                result.warnings.append(line)
    if ctx.strict:
        for problem in collect.strict_problems(ctx.root, parsed, str(LEDGER), known):
            result.fail(problem)
    research = sorted(t for t, (where, _) in traps.items() if where.startswith(f"{RESEARCH}/"))
    reviews = sum(1 for r in parsed.rows.values() if any(c.kind == "review" for c in r.coverage))
    unpinned = sorted(
        rid for rid, r in parsed.rows.items() if not any(c.kind == "test" for c in r.coverage)
    )
    result.notes.append(f"{len(research)} research R-ids; {len(unpinned)} rows without a test")
    if unpinned:
        result.notes.append(f"unpinned: {', '.join(unpinned)}")
    result.data = {
        "traps": len(traps),
        "research_ids": len(research),
        "rows": len(parsed.rows),
        "review_rows": reviews,
        "unpinned": unpinned,
    }
    return result


if __name__ == "__main__":
    from tooling.checks.__main__ import main

    raise SystemExit(main([NAME, *sys.argv[1:]]))
