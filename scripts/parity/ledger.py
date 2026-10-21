"""Obligation ledger for the parity pass (P25-08).

``parity/ledger/obligations.json`` holds the observable claims (``OBL-<AREA>-<nnn>``, our words),
each proven by the completion of named steps of one scenario. ``manifest.json`` binds every
obligation to exactly one owner (module and symbol) and the product tests that check the same
claim. ``rules.json`` holds the intended-difference rules (each citing a D-id or a section of
``16-what-we-change.md``) and the reference gaps (steps the reference cannot do, with a reason).

``self_test`` checks the ledger against the tree (discovered, never from a fixed list): every
scenario proves an obligation, every named step exists, every owner exports its symbol, every test
id resolves, every rule cites something that exists, every gap says why. ``statuses`` reads the
captures and comparisons and gives each obligation one status:

- **verified**: its steps completed on the product, and the reference matches or differs only
  where intended;
- **open**: the reference capture is owed, or divergences on its steps are not classified yet;
- **blocked**: verified on the product, but a recorded reference gap covers one of its steps;
- **failed**: one of its steps did not complete on the product;
- **not run**: no product capture (the flow could not run here).

Usage::

    python scripts/parity/ledger.py --check            # self-test, exit 1 on a problem
    python scripts/parity/ledger.py --ledger [--cache DIR] [--ref DIR] [--out DIR]
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.parity import compare
from scripts.parity.runner import CACHE_DIR, FLOWS, SCENARIO_DIR, load_scenario, step_ids

ROOT = Path(__file__).resolve().parents[2]
LEDGER_DIR = ROOT / "parity" / "ledger"
WHAT_WE_CHANGE = ROOT / "docs" / "plan" / "16-what-we-change.md"
OBLIGATION_ID = re.compile(r"^OBL-[A-Z]+-\d{3}$")
STATUSES = ("verified", "open", "blocked", "failed", "not run")
_D_ID = re.compile(r"^D-16\.\d+$")
_SECTION = re.compile(r"^16 §(\d+(?:\.\d+)*)$")


@dataclass(frozen=True)
class Ledger:
    obligations: list[dict[str, Any]]
    bindings: list[dict[str, Any]]
    intended: list[dict[str, Any]]
    gaps: list[dict[str, Any]]

    @classmethod
    def load(cls, directory: Path = LEDGER_DIR) -> Ledger:
        def read(name: str) -> dict[str, Any]:
            return json.loads((directory / name).read_text(encoding="utf-8"))

        rules = read("rules.json")
        return cls(
            list(read("obligations.json").get("obligations", [])),
            list(read("manifest.json").get("bindings", [])),
            list(rules.get("intended", [])),
            list(rules.get("gaps", [])),
        )


# -- resolving owners, tests and citations -------------------------------------------------------


def python_exports(path: Path) -> set[str]:
    """Top-level names a Python module defines (functions, classes, assignments)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def module_file(module: str, root: Path = ROOT) -> Path | None:
    """The file of a dotted module under ``src/`` or the repo root, or a TS path as given."""
    if "/" in module:
        path = root / module
        return path if path.is_file() else None
    parts = module.split(".")
    for base in (root / "src", root):
        candidate = base.joinpath(*parts)
        for path in (candidate.with_suffix(".py"), candidate / "__init__.py"):
            if path.is_file():
                return path
    return None


def exports_symbol(module: str, symbol: str, root: Path = ROOT) -> bool:
    path = module_file(module, root)
    if path is None:
        return False
    if path.suffix == ".py":
        return symbol in python_exports(path)
    text = path.read_text(encoding="utf-8")
    name = re.escape(symbol)
    declared = re.search(
        rf"^export\s+(?:default\s+)?(?:async\s+)?(?:function|const|let|class|interface|type|enum)\s+{name}\b",
        text,
        re.M,
    )
    listed = re.search(rf"^export\s*\{{[^}}]*\b{name}\b[^}}]*\}}", text, re.M)
    return bool(declared or listed)


def test_resolves(test_id: str, root: Path = ROOT) -> bool:
    """``path::name`` (Python: a ``def`` of that name) or ``path > title`` (a test titled so)."""
    if "::" in test_id:
        path_text, _, rest = test_id.partition("::")
        path = root / path_text
        if path.suffix != ".py" or not path.is_file():
            return False
        name = rest.split("::")[-1].split("[")[0]
        return (
            re.search(rf"^\s*(?:async\s+)?def\s+{re.escape(name)}\b", path.read_text("utf-8"), re.M)
            is not None
        )
    if " > " in test_id:
        path_text, _, title = test_id.partition(" > ")
        path = root / path_text
        if not path.is_file():
            return False
        text = path.read_text(encoding="utf-8")
        last = title.split(" > ")[-1]
        return any(f"{q}{last}{q}" in text for q in ('"', "'", "`"))
    return False


def known_citations(path: Path = WHAT_WE_CHANGE) -> tuple[set[str], set[str]]:
    """(D-ids with a row, section numbers with a heading) in the what-we-change document."""
    text = path.read_text(encoding="utf-8")
    ids = set(re.findall(r"^\|\s*(D-16\.\d+)\s*\|", text, re.M))
    sections = set(re.findall(r"^#+\s+(\d+(?:\.\d+)*)\b", text, re.M))
    return ids, sections


def citation_problem(cite: Any, ids: set[str], sections: set[str]) -> str | None:
    text = str(cite or "").strip()
    if _D_ID.match(text):
        return None if text in ids else f"cites {text}, which has no row in 16-what-we-change.md"
    found = _SECTION.match(text)
    if found:
        return None if found.group(1) in sections else f"cites {text}, which is not a section"
    return f"cite {text!r} is neither a D-id nor '16 §<n>'"


# -- self-test -------------------------------------------------------------------------------------


def self_test(
    ledger: Ledger,
    *,
    scenarios: Path = SCENARIO_DIR,
    root: Path = ROOT,
    what_we_change: Path = WHAT_WE_CHANGE,
    flows: Sequence[str] | None = None,
) -> list[str]:
    """Every problem with the ledger (empty when it is sound)."""
    problems: list[str] = []
    flow_names = list(flows) if flows is not None else [f.flow for f in FLOWS]
    steps_by_flow: dict[str, set[str]] = {}
    for flow in flow_names:
        try:
            steps_by_flow[flow] = set(step_ids(load_scenario(flow, scenarios)))
        except (OSError, ValueError) as exc:
            problems.append(f"{flow}: scenario unreadable ({exc})")
    seen: Counter[str] = Counter(str(o.get("id")) for o in ledger.obligations)
    for oid, count in seen.items():
        if count > 1:
            problems.append(f"{oid}: listed {count} times")
    for ob in ledger.obligations:
        oid = str(ob.get("id"))
        if not OBLIGATION_ID.match(oid):
            problems.append(f"{oid}: id must look like OBL-<AREA>-<nnn>")
        for key in ("title", "family", "scenario"):
            if not str(ob.get(key) or "").strip():
                problems.append(f"{oid}: no {key}")
        if not isinstance(ob.get("required"), bool):
            problems.append(f"{oid}: required must be true or false")
        flow = str(ob.get("scenario"))
        if flow not in steps_by_flow:
            problems.append(f"{oid}: scenario {flow!r} does not exist")
            continue
        steps = ob.get("steps") or []
        if not steps:
            problems.append(f"{oid}: names no steps")
        for step in steps:
            if step not in steps_by_flow[flow]:
                problems.append(f"{oid}: step {step!r} is not in {flow}")
    proven = {str(o.get("scenario")) for o in ledger.obligations}
    for flow in flow_names:
        if flow not in proven:
            problems.append(f"{flow}: proves no obligation")
    ids = set(seen)
    bound: Counter[str] = Counter(str(b.get("obligation")) for b in ledger.bindings)
    for oid in sorted(ids):
        if bound[oid] != 1:
            problems.append(f"{oid}: {bound[oid]} manifest bindings (exactly one wanted)")
    for binding in ledger.bindings:
        oid = str(binding.get("obligation"))
        if oid not in ids:
            problems.append(f"manifest binds unknown obligation {oid}")
        owner = binding.get("owner") or {}
        module, symbol = str(owner.get("module", "")), str(owner.get("symbol", ""))
        if not module or not symbol:
            problems.append(f"{oid}: owner needs a module and a symbol")
        elif not exports_symbol(module, symbol, root):
            problems.append(f"{oid}: {module} does not export {symbol}")
        tests = binding.get("tests") or []
        if not tests:
            problems.append(f"{oid}: no tests")
        for test in tests:
            if not test_resolves(str(test), root):
                problems.append(f"{oid}: test {test!r} does not resolve")
    d_ids, sections = known_citations(what_we_change)
    for index, rule in enumerate(ledger.intended):
        where = f"intended[{index}]"
        if rule.get("kind") not in compare.KINDS:
            problems.append(f"{where}: unknown kind {rule.get('kind')!r}")
        if rule.get("scenario") != "*" and rule.get("scenario") not in steps_by_flow:
            problems.append(f"{where}: unknown scenario {rule.get('scenario')!r}")
        if not str(rule.get("subject") or "").strip():
            problems.append(f"{where}: no subject")
        if not str(rule.get("why") or "").strip():
            problems.append(f"{where}: no why")
        found = citation_problem(rule.get("cite"), d_ids, sections)
        if found:
            problems.append(f"{where}: {found}")
    for index, gap in enumerate(ledger.gaps):
        where = f"gaps[{index}]"
        flow = str(gap.get("scenario"))
        if flow not in steps_by_flow:
            problems.append(f"{where}: unknown scenario {flow!r}")
        elif not gap.get("steps"):
            problems.append(f"{where}: names no steps")
        else:
            for step in gap["steps"]:
                if step not in steps_by_flow[flow]:
                    problems.append(f"{where}: step {step!r} is not in {flow}")
        if not str(gap.get("why") or "").strip():
            problems.append(f"{where}: a gap needs a reason")
    return problems


# -- statuses ------------------------------------------------------------------------------------


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def statuses(
    ledger: Ledger,
    *,
    product_dir: Path,
    ref_dir: Path | None,
    compare_dir: Path | None,
) -> dict[str, tuple[str, str]]:
    """``{obligation id: (status, reason)}`` from the captures and comparisons on disk."""
    out: dict[str, tuple[str, str]] = {}
    for ob in ledger.obligations:
        oid, flow = str(ob["id"]), str(ob["scenario"])
        steps = [str(s) for s in ob.get("steps", [])]
        capture = _json(product_dir / f"{flow}.capture.json")
        if capture is None or capture.get("status") in ("not_run", "blocked"):
            reason = "no product capture" if capture is None else str(capture.get("reason"))
            out[oid] = ("not run", reason)
            continue
        recorded = {str(s.get("step_id")): str(s.get("status")) for s in capture.get("steps", [])}
        bad = [s for s in steps if recorded.get(s) != "completed"]
        if bad:
            out[oid] = ("failed", f"{bad[0]}: {recorded.get(bad[0], 'missing')}")
            continue
        gap = next(
            (
                g
                for g in ledger.gaps
                if g.get("scenario") == flow and set(g.get("steps", [])) & set(steps)
            ),
            None,
        )
        if gap is not None:
            out[oid] = ("blocked", f"reference gap: {gap.get('why')}")
            continue
        if ref_dir is None or compare.reference_capture(ref_dir, flow) is None:
            out[oid] = ("open", "reference capture owed (owner's REA sandbox, ~/rea-lab)")
            continue
        divergences = _json(compare_dir / f"{flow}.divergences.json") if compare_dir else None
        summary = _json(compare_dir / f"{flow}.compare.json") if compare_dir else None
        if divergences is None or summary is None:
            out[oid] = ("open", "not compared yet")
            continue
        if not summary.get("aligned", False):
            out[oid] = (
                "open",
                "alignment failed: " + "; ".join(summary.get("alignment_failures", [])),
            )
            continue
        left = [
            d
            for d in divergences
            if d.get("class") == "unclassified"
            and (d.get("step") in steps or d.get("step") is None)
        ]
        if left:
            out[oid] = ("open", f"{len(left)} divergence(s) to classify")
            continue
        out[oid] = ("verified", "")
    return out


def report(
    cache: Path = CACHE_DIR, ref_dir: Path | None = None, compare_dir: Path | None = None
) -> str:
    """Counts per status, then one line per obligation."""
    ledger = Ledger.load()
    found = statuses(ledger, product_dir=cache / "indus", ref_dir=ref_dir, compare_dir=compare_dir)
    counts = Counter(status for status, _ in found.values())
    lines = ["ledger: " + ", ".join(f"{s} {counts.get(s, 0)}" for s in STATUSES)]
    for ob in ledger.obligations:
        status, reason = found[str(ob["id"])]
        lines.append(
            f"  {ob['id']:<15} {status:<9} {ob['scenario']}" + (f"  ({reason})" if reason else "")
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Parity obligation ledger: self-test and report.")
    parser.add_argument("--check", action="store_true", help="run the self-test")
    parser.add_argument("--ledger", action="store_true", help="print counts per status")
    parser.add_argument("--cache", type=Path, default=CACHE_DIR)
    parser.add_argument("--ref", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    code = 0
    if args.check or not args.ledger:
        problems = self_test(Ledger.load())
        for problem in problems:
            print(f"ledger: {problem}")
        print(f"ledger self-test: {'ok' if not problems else f'{len(problems)} problem(s)'}")
        code = 1 if problems else 0
    if args.ledger:
        print(report(args.cache, args.ref, args.out or args.cache / "compare"))
    return code


__all__ = [
    "STATUSES",
    "Ledger",
    "citation_problem",
    "exports_symbol",
    "known_citations",
    "main",
    "report",
    "self_test",
    "statuses",
    "test_resolves",
]

if __name__ == "__main__":
    raise SystemExit(main())
