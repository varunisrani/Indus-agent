"""Strict test lookups for the ledgers: is a cited test really collected, and not skipped?

The everyday ledger check (``ledger.KnownTests``) parses files. The release gate asks more:

* Python references are resolved against ``pytest --collect-only`` run in a subprocess over the
  cited files, with ``collect_plugin`` recording each item's skip reason in this environment, the
  modules that skipped as a whole, and collection errors.
* TypeScript references (``file::title`` or ``file > describe > title``) are resolved against a
  scan of the file's ``describe`` / ``it`` / ``test`` / ``bench`` calls: ``.skip``, ``.todo``,
  ``.fixme``, an ``x`` prefix, or a skipped enclosing ``describe`` make a test skipped.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from tooling.checks.common import DEFAULT_ROOT
from tooling.checks.ledger import KnownTests, Ledger, split_ref

PY_SUFFIXES = (".py",)
TS_SUFFIXES = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
COLLECT_TIMEOUT_S = 1800


# --------------------------------------------------------------------------- Python


@dataclass
class Collected:
    items: dict[str, str | None] = field(default_factory=dict)  # node id -> skip reason
    skipped: dict[str, str] = field(default_factory=dict)  # collector node id -> reason
    errors: dict[str, str] = field(default_factory=dict)  # collector node id -> message
    python_files: list[str] = field(default_factory=lambda: ["test_*.py", "*_test.py"])
    problem: str | None = None  # pytest itself could not run


def collect_python(
    root: Path, files: Iterable[str], timeout: float = COLLECT_TIMEOUT_S
) -> Collected:
    """Run ``pytest --collect-only`` over ``files`` (relative to ``root``) and read the record."""
    wanted = sorted({f for f in files if (root / f).is_file()})
    if not wanted:
        return Collected()
    with tempfile.TemporaryDirectory(prefix="indus-collect-") as tmp:
        out = Path(tmp) / "collected.json"
        env = dict(os.environ)
        env["INDUS_CHECKS_COLLECT_OUT"] = str(out)
        paths = [str(DEFAULT_ROOT), env.get("PYTHONPATH", "")]
        env["PYTHONPATH"] = os.pathsep.join(p for p in paths if p)
        cmd = [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "tooling.checks.collect_plugin",
            "--continue-on-collection-errors",
            "--rootdir",
            str(root),
            *wanted,
        ]
        try:
            run = subprocess.run(
                cmd,
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return Collected(problem=f"pytest --collect-only did not finish: {exc}")
        if not out.is_file():
            tail = (run.stderr or run.stdout).strip().splitlines()[-3:]
            return Collected(problem=f"pytest --collect-only failed: {' | '.join(tail)}")
        data = json.loads(out.read_text(encoding="utf-8"))
    return Collected(
        items=dict(data.get("items", {})),
        skipped=dict(data.get("skipped", {})),
        errors=dict(data.get("errors", {})),
        python_files=list(data.get("python_files") or ["test_*.py", "*_test.py"]),
    )


def python_status(ref: str, found: Collected) -> str | None:
    """None when ``ref`` names a collected, runnable test; else why not."""
    path, names = split_ref(ref)
    nodeid = "::".join([path, *names])
    base = path.rsplit("/", 1)[-1]
    if not any(fnmatch.fnmatch(base, pattern) for pattern in found.python_files):
        patterns = " ".join(found.python_files)
        return f"`{ref}` is not collected by pytest ({base} does not match {patterns})"
    for collector in (path, *(f"{path}::{'::'.join(names[:i])}" for i in range(1, len(names)))):
        if collector in found.errors:
            return f"`{ref}`: collection error in {collector}: {found.errors[collector]}"
        if collector in found.skipped:
            return f"`{ref}` is skipped here ({collector}: {found.skipped[collector]})"
    if nodeid in found.items:
        reason = found.items[nodeid]
        return None if reason is None else f"`{ref}` is skipped here ({reason})"
    matches = [k for k in found.items if k.startswith((nodeid + "[", nodeid + "::"))]
    if not matches:
        return f"`{ref}` is not collected by pytest"
    if all(found.items[k] is not None for k in matches):
        return f"`{ref}` is skipped here ({found.items[matches[0]]})"
    return None


# --------------------------------------------------------------------------- TypeScript

_CALL = re.compile(
    r"(?<![\w$.])(?P<base>describe|suite|it|test|bench|xit|xdescribe|xtest)"
    r"(?P<mods>(?:\s*\.\s*[A-Za-z_]\w*)*)"
    r"(?P<args>\s*\([^()]*\))?"
    r"\s*\(\s*(?P<quote>['\"`])"
)
_SKIP_MODS = frozenset({"skip", "todo", "fixme"})
_NOT_A_TEST = frozenset(
    {"step", "use", "extend", "info", "beforeEach", "afterEach", "beforeAll", "afterAll", "fail"}
)


@dataclass(frozen=True)
class TsCase:
    title: str
    chain: tuple[str, ...]  # enclosing describe titles, outermost first
    kind: str  # "describe" or "test"
    skipped: bool
    pattern: str | None = None  # a template title: its ``${…}`` holes match anything

    def matches(self, title: str) -> bool:
        if self.pattern is None:
            return self.title == title
        return re.fullmatch(self.pattern, title, re.DOTALL) is not None


def _skip_expr(text: str, j: int) -> int:
    """Index just past the ``}`` closing a template hole whose body starts at ``j``."""
    depth, n = 1, len(text)
    while j < n:
        c = text[j]
        if c in "'\"`":
            j, _ = _skip_string(text, j)
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return n


def _skip_string(text: str, i: int) -> tuple[int, list[str]]:
    """``text[i]`` is a quote: the index just past its closing quote, and the literal parts
    (one for a plain string; for a template literal, the text between its ``${…}`` holes)."""
    quote, n = text[i], len(text)
    parts = [""]
    j = i + 1
    while j < n:
        c = text[j]
        if c == "\\" and j + 1 < n:
            parts[-1] += text[j + 1]
            j += 2
            continue
        if c == quote:
            return j + 1, parts
        if c == "\n" and quote != "`":
            return j, parts
        if quote == "`" and text.startswith("${", j):
            j = _skip_expr(text, j + 2)
            parts.append("")
            continue
        parts[-1] += c
        j += 1
    return n, parts


def mask(text: str) -> str:
    """The text with comments blanked and string contents replaced, positions kept.

    Quotes stay in place so string starts can be found; newlines are kept.
    """
    out = list(text)
    i, n = 0, len(text)

    def blank(a: int, b: int, fill: str = " ") -> None:
        for k in range(a, b):
            if out[k] != "\n":
                out[k] = fill

    while i < n:
        c = text[i]
        if c == "/" and text.startswith("//", i):
            end = text.find("\n", i)
            end = n if end < 0 else end
            blank(i, end)
            i = end
        elif c == "/" and text.startswith("/*", i):
            end = text.find("*/", i + 2)
            end = n if end < 0 else end + 2
            blank(i, end)
            i = end
        elif c in "'\"`":
            end, _ = _skip_string(text, i)
            closed = end - 1 > i and text[end - 1] == c
            blank(i + 1, end - 1 if closed else end, "x")
            i = max(end, i + 1)
        else:
            i += 1
    return "".join(out)


def _close_paren(masked: str, open_at: int) -> int:
    depth = 0
    for k in range(open_at, len(masked)):
        if masked[k] == "(":
            depth += 1
        elif masked[k] == ")":
            depth -= 1
            if depth == 0:
                return k
    return len(masked)


def ts_cases(text: str) -> list[TsCase]:
    masked = mask(text)
    raw: list[tuple[int, int, str, str, bool, str | None]] = []
    # start, end, kind, title, skipped, title pattern
    for m in _CALL.finditer(masked):
        base = m.group("base")
        mods = [x.strip() for x in m.group("mods").split(".") if x.strip()]
        if any(x in _NOT_A_TEST for x in mods):
            continue
        is_describe = base in ("describe", "suite", "xdescribe") or "describe" in mods
        skipped = base.startswith("x") or any(x in _SKIP_MODS for x in mods)
        quote_at = m.end() - 1
        _, parts = _skip_string(text, quote_at)
        title = "${…}".join(parts)
        pattern = ".*".join(re.escape(p) for p in parts) if len(parts) > 1 else None
        open_at = masked.rfind("(", m.start(), quote_at)
        end = _close_paren(masked, open_at)
        kind = "describe" if is_describe else "test"
        raw.append((m.start(), end, kind, title, skipped, pattern))
    cases: list[TsCase] = []
    for start, _end, kind, title, skipped, pattern in raw:
        outer = [r for r in raw if r[2] == "describe" and r[0] < start < r[1]]
        outer.sort(key=lambda r: r[0])
        chain = tuple(r[3] for r in outer)
        hidden = skipped or any(r[4] for r in outer)
        cases.append(TsCase(title, chain, kind, hidden, pattern))
    return cases


def _chain_matches(want: list[str], chain: tuple[str, ...]) -> bool:
    it = iter(chain)
    return all(any(w == c for c in it) for w in want)


def ts_status(root: Path, ref: str, cache: dict[str, list[TsCase]]) -> str | None:
    path, names = split_ref(ref)
    if path not in cache:
        try:
            cache[path] = ts_cases((root / path).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            cache[path] = []
    title, want = names[-1], names[:-1]
    found = [c for c in cache[path] if c.matches(title) and _chain_matches(want, c.chain)]
    tests = [c for c in found if c.kind == "test"] or found
    if not tests:
        return f"`{ref}`: no describe/it/test call with that title in {path}"
    if all(c.skipped for c in tests):
        return f"`{ref}` is skipped (.skip/.todo/x-prefix or a skipped describe)"
    return None


# --------------------------------------------------------------------------- both


def strict_problems(root: Path, ledger: Ledger, name: str, known: KnownTests) -> list[str]:
    """Problems for test references that parse but are not collected, or are skipped here."""
    refs: list[tuple[str, int, str]] = []  # (row id, line, ref)
    for row in ledger.rows.values():
        for cov in row.coverage:
            if cov.kind == "test" and known.check(cov.value) is None:
                refs.append((row.id, cov.line, cov.value))
    py = [r for r in refs if split_ref(r[2])[0].endswith(PY_SUFFIXES)]
    ts = [r for r in refs if split_ref(r[2])[0].endswith(TS_SUFFIXES)]
    out: list[str] = []
    if py:
        found = collect_python(root, (split_ref(r[2])[0] for r in py))
        if found.problem:
            return [f"{name}: {found.problem}"]
        for rid, line, ref in py:
            why = python_status(ref, found)
            if why:
                out.append(f"{name}:{line}: {rid}: {why}")
    cache: dict[str, list[TsCase]] = {}
    for rid, line, ref in ts:
        why = ts_status(root, ref, cache)
        if why:
            out.append(f"{name}:{line}: {rid}: {why}")
    return out
