"""Release checklist lint (P25-20): the bug-bash triage in docs/internals/release-checklist.md.

The triage files every open item under must (``RM<n>``), should (``RS<n>``) or later (``RL<n>``).
Ids are permanent: assigned once, never renumbered, never reused. The checklist records the
highest id handed out in each series on one line,

    **Ids assigned:** RM1–RM12 · RS1–RS30 · RL1–RL20

and every id from 1 to that mark must appear exactly once across the tables — Must-fix, Should-fix,
Later, Done in this wave, Withdrawn — so an id can leave a bucket only by moving to another table,
never by disappearing (which is what would let it be reused). Also checked:

- each table heading carries its row count, ``### Should-fix (30)``, and the count is right;
- every triage row has an Item, a Source and a Why; Done rows name what closed them and a commit
  cell; Withdrawn rows say why;
- an ``RM`` id in the Should-fix or Later table carries the owner's approval with its date (the
  only way a must becomes a should);
- every D-id in the plan's "After v1.0" list (``docs/plan/18-phase-wise-build-plan.md`` §4) appears
  in the checklist's "After v1.0" section.

A root without the checklist (the guards' fixture trees) is a note, not a failure.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from tooling.checks.common import Context, Result

NAME = "checklist"
CHECKLIST = Path("docs/internals/release-checklist.md")
PLAN = Path("docs/plan/18-phase-wise-build-plan.md")

SERIES = ("RM", "RS", "RL")
ID = re.compile(r"^R[MSL][1-9]\d*$")
MARKS_LINE = re.compile(r"^\s*\**Ids assigned:?\**:?\s*(?P<rest>.*)$", re.IGNORECASE)
MARK = re.compile(r"\b(?P<series>R[MSL])1\s*[–-]\s*(?P=series)(?P<top>[1-9]\d*)\b")
HEADING = re.compile(r"^(?P<hashes>#{2,3})\s+(?P<text>.*?)\s*$")
COUNTED = re.compile(r"^(?P<title>.*?)\s*\((?P<count>\d+)\)\s*$")
SEPARATOR = re.compile(r"^\|?\s*:?-{2,}")
CELL_SPLIT = re.compile(r"(?<!\\)\|")
APPROVAL = re.compile(r"owner\b.*\b\d{4}-\d{2}-\d{2}\b|\b\d{4}-\d{2}-\d{2}\b.*\bowner\b", re.I)
D_ID = re.compile(r"\bD-16\.\d+\b")

# Table title (lower case, without the count) -> (kind, the columns that must not be empty).
TABLES: dict[str, tuple[str, tuple[str, ...]]] = {
    "must-fix for 1.0": ("must", ("Item", "Source", "Why")),
    "should-fix": ("should", ("Item", "Source", "Why")),
    "later": ("later", ("Item", "Source", "Why")),
    "done in this wave": ("done", ("Item", "What closed it", "Commit")),
    "withdrawn": ("withdrawn", ("Item", "Why withdrawn")),
}
MOVED_DOWN = frozenset({"should", "later"})


@dataclass
class TriageRow:
    id: str
    table: str
    cells: list[str]
    line: int


@dataclass
class Parsed:
    rows: list[TriageRow] = field(default_factory=list)
    marks: dict[str, int] = field(default_factory=dict)
    marks_line: int = 0
    problems: list[str] = field(default_factory=list)
    after_text: str = ""
    tables_seen: set[str] = field(default_factory=set)


def _cells(line: str) -> list[str]:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|") and not body.endswith("\\|"):
        body = body[:-1]
    return [c.strip() for c in CELL_SPLIT.split(body)]


def parse(text: str, name: str = str(CHECKLIST)) -> Parsed:
    out = Parsed()
    table: str | None = None  # kind of the open counted table
    columns: tuple[str, ...] = ()
    declared = 0
    heading_line = 0
    count_rows = 0
    in_after = False
    after: list[str] = []
    in_fence = False

    def close_table() -> None:
        if table is not None and count_rows != declared:
            out.problems.append(
                f"{name}:{heading_line}: heading says {declared} rows, the table has {count_rows}"
            )

    for no, raw in enumerate(text.splitlines(), 1):
        if raw.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        h = HEADING.match(raw)
        if h:
            close_table()
            table, count_rows = None, 0
            title = h.group("text")
            in_after = len(h.group("hashes")) == 2 and "after v1.0" in title.lower()
            counted = COUNTED.match(title)
            key = (counted.group("title") if counted else title).strip().lower()
            if key in TABLES:
                kind, columns = TABLES[key]
                if kind in out.tables_seen:
                    out.problems.append(f"{name}:{no}: second '{title}' table")
                out.tables_seen.add(kind)
                if counted is None:
                    out.problems.append(f"{name}:{no}: heading '{title}' has no (count)")
                    declared = -1
                else:
                    declared = int(counted.group("count"))
                table, heading_line = kind, no
            continue
        if in_after:
            after.append(raw)
        m = MARKS_LINE.match(raw)
        if m and not out.marks_line:
            out.marks_line = no
            for mark in MARK.finditer(m.group("rest")):
                out.marks[mark.group("series")] = int(mark.group("top"))
            continue
        if table is None or not raw.lstrip().startswith("|"):
            continue
        if SEPARATOR.match(raw.strip()):
            continue
        cells = _cells(raw)
        first = cells[0].strip("*` ")
        if first == "#":
            continue  # the header row
        count_rows += 1
        if not ID.match(first):
            out.problems.append(f"{name}:{no}: row without an RM/RS/RL id: {cells[0]!r}")
            continue
        row = TriageRow(first, table, cells, no)
        out.rows.append(row)
        for index, column in enumerate(columns, start=1):
            if index >= len(cells) or not cells[index]:
                out.problems.append(f"{name}:{no}: {first}: empty {column}")
    close_table()
    out.after_text = "\n".join(after)
    return out


def plan_after_ids(text: str) -> list[str]:
    """D-ids listed in the plan's "After v1.0" section (a level-2 heading)."""
    found: list[str] = []
    inside = False
    for raw in text.splitlines():
        h = HEADING.match(raw)
        if h and len(h.group("hashes")) == 2:
            inside = "after v1.0" in h.group("text").lower()
            continue
        if inside:
            for d in D_ID.findall(raw):
                if d not in found:
                    found.append(d)
    return found


def problems(parsed: Parsed, plan_text: str | None, name: str = str(CHECKLIST)) -> list[str]:
    out = list(parsed.problems)
    for kind in ("must", "should", "later", "done"):
        if kind not in parsed.tables_seen:
            out.append(f"{name}: no '{kind}' table")
    if not parsed.marks_line:
        out.append(f"{name}: no 'Ids assigned:' line with the high-water mark of each series")
    seen: dict[str, TriageRow] = {}
    for row in parsed.rows:
        if row.id in seen:
            out.append(
                f"{name}:{row.line}: {row.id} used again (first at line {seen[row.id].line}); "
                "ids are never reused"
            )
            continue
        seen[row.id] = row
        series, number = row.id[:2], int(row.id[2:])
        top = parsed.marks.get(series, 0)
        if number > top:
            out.append(
                f"{name}:{row.line}: {row.id} is above the assigned mark {series}{top}; "
                "raise the 'Ids assigned' line"
            )
        if series == "RM" and row.table in MOVED_DOWN and not APPROVAL.search(" ".join(row.cells)):
            out.append(
                f"{name}:{row.line}: {row.id} moved from must to {row.table} without the owner's "
                "dated written approval in the row"
            )
    for series in SERIES:
        for number in range(1, parsed.marks.get(series, 0) + 1):
            rid = f"{series}{number}"
            if rid not in seen:
                out.append(
                    f"{name}:{parsed.marks_line}: {rid} was assigned but is in no table "
                    "(move it to Withdrawn with a reason; never drop or reuse an id)"
                )
    if plan_text is not None:
        for d in plan_after_ids(plan_text):
            if not re.search(rf"\b{re.escape(d)}\b", parsed.after_text):
                out.append(f"{name}: After v1.0 lacks {d} (listed in {PLAN} §4)")
    return out


def run(ctx: Context) -> Result:
    result = Result(NAME)
    path = ctx.root / CHECKLIST
    if not path.is_file():
        result.notes.append(f"no {CHECKLIST} under the root; nothing to check")
        return result
    parsed = parse(path.read_text(encoding="utf-8"))
    plan = ctx.root / PLAN
    plan_text = plan.read_text(encoding="utf-8") if plan.is_file() else None
    if plan_text is None:
        result.notes.append(f"no {PLAN}; the After v1.0 list is not compared")
    for problem in problems(parsed, plan_text):
        result.fail(problem)
    by_kind = {kind: sum(1 for r in parsed.rows if r.table == kind) for kind, _ in TABLES.values()}
    result.notes.append(
        "triage: "
        + ", ".join(f"{kind} {by_kind.get(kind, 0)}" for kind in ("must", "should", "later"))
        + f"; done {by_kind.get('done', 0)}; withdrawn {by_kind.get('withdrawn', 0)}"
    )
    result.data = {"rows": by_kind, "marks": parsed.marks}
    return result


if __name__ == "__main__":
    from tooling.checks.__main__ import main

    raise SystemExit(main([NAME, *sys.argv[1:]]))
