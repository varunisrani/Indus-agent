"""Shared parser for the Markdown ledgers (traps ledger, acceptance map).

A row is ``- **ID** summary``. The indented lines under it are its coverage, each either a test
reference in backticks (``path/to/test_x.py::test_name`` or ``path::Class::test_name``) or
``Review: <why> (<task id>)``.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

ROW = re.compile(r"^[-*]\s+\*\*(?P<id>[^*\s]+)\*\*\s*(?P<summary>.*)$")
TEST_REF = re.compile(r"^`(?P<ref>[^`]+)`")
REVIEW = re.compile(r"^Review:\s*(?P<why>.*)$")
TASK_SUFFIX = re.compile(r"\([^()]+\)\s*$")


@dataclass
class Coverage:
    kind: str  # "test" or "review"
    value: str
    line: int


@dataclass
class Row:
    id: str
    summary: str
    line: int
    coverage: list[Coverage] = field(default_factory=list)


@dataclass
class Ledger:
    rows: dict[str, Row] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def parse(text: str, name: str) -> Ledger:
    ledger = Ledger()
    current: Row | None = None
    in_fence = False
    for no, raw in enumerate(text.splitlines(), 1):
        if raw.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = ROW.match(raw)
        if m:
            rid = m.group("id")
            current = Row(rid, m.group("summary").strip(), no)
            if rid in ledger.rows:
                ledger.problems.append(f"{name}:{no}: duplicate row {rid}")
            else:
                ledger.rows[rid] = current
            continue
        if not raw.strip():
            continue
        if not raw[0].isspace():
            current = None  # headings and prose end a row
            continue
        if current is None:
            continue
        body = raw.strip()
        if body[:2] in ("- ", "* "):
            body = body[2:].strip()
        t = TEST_REF.match(body)
        r = REVIEW.match(body)
        if t:
            current.coverage.append(Coverage("test", t.group("ref").strip(), no))
        elif r:
            why = r.group("why").strip()
            if not why:
                ledger.problems.append(f"{name}:{no}: {current.id}: Review line needs a reason")
            elif not TASK_SUFFIX.search(why):
                ledger.warnings.append(f"{name}:{no}: {current.id}: Review line has no (task id)")
            current.coverage.append(Coverage("review", why, no))
        else:
            ledger.problems.append(
                f"{name}:{no}: {current.id}: coverage line must be `test ref` or 'Review: ...'"
            )
    return ledger


class KnownTests:
    """Answers "does this test exist?" by parsing test files with ``ast`` (no test run)."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._trees: dict[str, ast.Module | None] = {}

    def _tree(self, relpath: str) -> ast.Module | None:
        if relpath not in self._trees:
            try:
                src = (self.root / relpath).read_text(encoding="utf-8")
                self._trees[relpath] = ast.parse(src, filename=relpath)
            except (OSError, SyntaxError, ValueError):
                self._trees[relpath] = None
        return self._trees[relpath]

    def check(self, ref: str) -> str | None:
        """None when the referenced test exists, else the reason it does not."""
        parts = [p.strip() for p in ref.split("::")]
        if len(parts) < 2 or not all(parts):
            return f"`{ref}` is not of the form path::test_name"
        relpath, names = parts[0], [re.sub(r"\[.*\]$", "", n) for n in parts[1:]]
        path = self.root / relpath
        if not path.is_file():
            return f"test file {relpath} does not exist"
        if not relpath.endswith(".py"):
            text = path.read_text(encoding="utf-8", errors="replace")
            missing = [n for n in names if n not in text]
            return f"{relpath}: no {missing[0]!r}" if missing else None
        tree = self._tree(relpath)
        if tree is None:
            return f"test file {relpath} does not parse"
        body: list[ast.stmt] = tree.body
        for i, name in enumerate(names):
            found = next(
                (
                    node
                    for node in body
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                    and node.name == name
                ),
                None,
            )
            if found is None:
                return f"{relpath}: no {'::'.join(names[: i + 1])}"
            body = found.body if isinstance(found, ast.ClassDef) else []
        return None


def check_rows(
    ledger: Ledger, tests: KnownTests, name: str, strict: bool = False
) -> list[tuple[str, str]]:
    """(row id, problem) for rows without coverage, missing tests, and (strict) Review rows."""
    out: list[tuple[str, str]] = []
    for row in ledger.rows.values():
        if not row.coverage:
            out.append((row.id, f"{name}:{row.line}: {row.id} has no coverage line"))
        for cov in row.coverage:
            if cov.kind == "test":
                why = tests.check(cov.value)
                if why:
                    out.append((row.id, f"{name}:{cov.line}: {row.id}: {why}"))
            elif strict:
                out.append((row.id, f"{name}:{cov.line}: {row.id}: Review row (strict mode)"))
    return out


def append_rows(path: Path, header: str, rows: list[tuple[str, str]], task: str) -> int:
    """Create or extend a ledger with ``Review: not yet planned`` rows. Returns rows added."""
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    have = set(parse(existing, path.name).rows) if existing else set()
    new = [(rid, s) for rid, s in rows if rid not in have]
    if not new and existing:
        return 0
    chunks = [existing.rstrip("\n") + "\n\n"] if existing else [header.rstrip("\n") + "\n\n"]
    for rid, summary in new:
        chunks.append(f"- **{rid}** {summary}".rstrip() + "\n")
        chunks.append(f"  - Review: not yet planned ({task})\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(chunks), encoding="utf-8")
    return len(new)
