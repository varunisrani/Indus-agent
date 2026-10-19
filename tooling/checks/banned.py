"""Banned identifiers: reference-only names must not appear in product files.

Patterns live in ``banned_patterns.json`` as ``{pattern, hint, flags}`` entries; append to it to
ban more names. Earlier entries win when two patterns match at the same spot.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from tooling.checks.common import CHECKS_DIR, Context, Result, read_text, rel, walk_files

NAME = "banned"
PATTERNS_FILE = CHECKS_DIR / "banned_patterns.json"
SCAN_DIRS = ("src", "apps", "packages", "scripts", "tests", "tooling", "skills")
EXEMPT = (
    "tooling/checks/banned_patterns.json",
    "tooling/checks/reference.json",
    "tooling/checks/tests/",
)
FLAG_NAMES = {"IGNORECASE": re.IGNORECASE, "I": re.IGNORECASE, "MULTILINE": re.MULTILINE}


@dataclass(frozen=True)
class Pattern:
    regex: re.Pattern[str]
    hint: str


def load_patterns(path: Path = PATTERNS_FILE) -> tuple[list[Pattern], list[str]]:
    """Return (patterns, problems)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [], [f"{path.name}: cannot read: {exc}"]
    if not isinstance(data, list):
        return [], [f"{path.name}: must be a JSON list"]
    out: list[Pattern] = []
    problems: list[str] = []
    for n, item in enumerate(data):
        if not isinstance(item, dict) or not item.get("pattern") or not item.get("hint"):
            problems.append(f"{path.name}[{n}]: needs non-empty 'pattern' and 'hint'")
            continue
        flags = 0
        for f in item.get("flags", []) or []:
            if f not in FLAG_NAMES:
                problems.append(f"{path.name}[{n}]: unknown flag {f!r}")
            flags |= FLAG_NAMES.get(f, 0)
        try:
            out.append(Pattern(re.compile(item["pattern"], flags), str(item["hint"])))
        except re.error as exc:
            problems.append(f"{path.name}[{n}]: bad regex: {exc}")
    return out, problems


def scan_text(text: str, patterns: list[Pattern]) -> list[tuple[int, int, str, str]]:
    """(line, col, match, hint) for every banned match; one report per position."""
    found: list[tuple[int, int, str, str]] = []
    for no, line in enumerate(text.splitlines(), 1):
        taken: list[tuple[int, int]] = []
        for pat in patterns:
            for m in pat.regex.finditer(line):
                if any(s <= m.start() < e for s, e in taken):
                    continue
                taken.append((m.start(), max(m.end(), m.start() + 1)))
                found.append((no, m.start() + 1, m.group(), pat.hint))
    found.sort()
    return found


def run(ctx: Context, patterns_file: Path = PATTERNS_FILE) -> Result:
    result = Result(NAME)
    patterns, problems = load_patterns(patterns_file)
    for p in problems:
        result.setup(p)
    if problems:
        return result
    scanned = 0
    for top in SCAN_DIRS:
        for path in walk_files(ctx.root / top):
            relpath = rel(path, ctx.root)
            if relpath.startswith(EXEMPT):
                continue
            for _, col, match, hint in scan_text(relpath, patterns):
                result.fail(f"{relpath}: path contains {match!r} (col {col}); {hint}")
            text = read_text(path)
            if text is None:
                continue
            scanned += 1
            for line, col, match, hint in scan_text(text, patterns):
                result.fail(f"{relpath}:{line}:{col}: {match!r}; {hint}")
    result.notes.append(f"scanned {scanned} text files with {len(patterns)} patterns")
    return result


if __name__ == "__main__":
    from tooling.checks.__main__ import main

    raise SystemExit(main([NAME, *sys.argv[1:]]))
