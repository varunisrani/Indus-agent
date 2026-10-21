"""Compare, classify, report (P25-09).

Puts one flow's product capture next to the reference capture of the same flow (made by the owner
in the REA sandbox, kept in ``~/rea-lab``) and lists the divergences, aligned by exact step id:

- ``step-only-in-<side>``: a step id one capture has and the other lacks;
- ``step-failed-in-<side>``: a step that did not complete on that side;
- ``role-only-in-<side>``: a role (a structural kind: an accessibility role, a terminal card or
  prompt, a protocol message key) seen at a ``wait_for`` step on one side only. Only ``wait_for``
  steps are compared: right after an action each app races its own update. Accessible names and
  text are never compared, because our copy differs on purpose;
- ``page-errors``: uncaught page errors on a side.

Duplicate step ids and different origins are alignment failures: the flow is not compared.
Each divergence is then classified with ``parity/ledger/rules.json``: **intended** (a rule cites a
D-id), **reference gap** (the step is listed as one the reference cannot do) or **unclassified**.
REA's own step-aligned comparison (``compare_web_captures``) is run through its MCP tools on the
same two captures; this summary is ours and is what the ledger reads.

Usage::

    python scripts/parity/compare.py PRODUCT_CAPTURE REFERENCE_CAPTURE [--rules FILE]

exits 1 while anything is unclassified or the alignment fails.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
RULES = ROOT / "parity" / "ledger" / "rules.json"
SIDES = ("indus", "reference")
KINDS = (
    "step-only-in-indus",
    "step-only-in-reference",
    "step-failed-in-indus",
    "step-failed-in-reference",
    "role-only-in-indus",
    "role-only-in-reference",
    "page-errors",
)
_ARIA_ROLE = re.compile(r"""^\s*-\s+['"]?([a-z][a-z-]*)\b""")
_NOT_ROLES = {"text", "img"}  # free text is copy, not structure


@dataclass(frozen=True)
class Step:
    step_id: str
    action: str
    status: str
    roles: frozenset[str] | None  # None: not a wait_for step, or nothing structural recorded


@dataclass
class Capture:
    flow: str
    side: str
    steps: list[Step]
    origin: str | None = None
    page_errors: list[str] = field(default_factory=list[str])


def aria_roles(text: str) -> frozenset[str]:
    """Roles in an accessibility snapshot (``- button "Send"`` gives ``button``); names dropped."""
    found = {m.group(1) for line in text.splitlines() if (m := _ARIA_ROLE.match(line))}
    return frozenset(found - _NOT_ROLES)


def terminal_roles(observed: Mapping[str, Any]) -> frozenset[str]:
    roles = {f"card:{c.get('kind')}" for c in observed.get("cards") or []}
    if observed.get("prompts"):
        roles.add("prompt")
    for path in observed.get("shape") or []:
        roles.add(f"key:{path}")
    return frozenset(roles)


def _roles_of(action: str, observed: Mapping[str, Any] | None) -> frozenset[str] | None:
    if action != "wait_for" or not observed:
        return None
    if isinstance(observed.get("accessibility"), str):
        return aria_roles(observed["accessibility"])
    if any(k in observed for k in ("cards", "prompts", "shape")):
        return terminal_roles(observed)
    return None


def load_capture(data: Mapping[str, Any], side: str | None = None) -> Capture:
    """Our capture format, or a REA ``capture_browser_scenario`` result (wrapped or not)."""
    if "normalized_result" in data:
        data = data["normalized_result"]
    if data.get("format", "").startswith("indus-parity-capture"):
        steps = [
            Step(
                str(s.get("step_id")),
                str(s.get("action")),
                str(s.get("status")),
                _roles_of(str(s.get("action")), s.get("observed")),
            )
            for s in data.get("steps", [])
        ]
        return Capture(
            str(data.get("flow", "")),
            side or str(data.get("side", "indus")),
            steps,
            data.get("origin"),
            [str(e) for e in data.get("page_errors", [])],
        )
    steps = []
    for raw in data.get("steps", []):
        if raw.get("step_id") == "scenario_start":
            continue
        accessibility = ((raw.get("artifacts") or {}).get("accessibility") or {}).get("value") or {}
        observed = {"accessibility": accessibility["text"]} if "text" in accessibility else None
        action = str(raw.get("action"))
        steps.append(
            Step(
                str(raw.get("step_id")), action, str(raw.get("status")), _roles_of(action, observed)
            )
        )
    items = (data.get("events") or {}).get("items") or []
    scenario = data.get("scenario") or {}
    return Capture(
        str(data.get("flow", "")),
        side or "reference",
        steps,
        scenario.get("start_origin"),
        [str(e.get("message")) for e in items if e.get("kind") == "page-error"],
    )


@dataclass(frozen=True)
class Divergence:
    kind: str
    step: str | None
    subject: str
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "step": self.step,
            "subject": self.subject,
            "detail": self.detail,
        }


def duplicates(steps: Iterable[Step]) -> list[str]:
    counts = Counter(s.step_id for s in steps)
    return sorted(k for k, n in counts.items() if n > 1)


def alignment_failures(ours: Capture, theirs: Capture) -> list[str]:
    out = [
        f"duplicate step id {d!r} in the {c.side} capture"
        for c in (ours, theirs)
        for d in duplicates(c.steps)
    ]
    if ours.origin and theirs.origin and ours.origin != theirs.origin:
        out.append(f"origins differ: {ours.origin} vs {theirs.origin}")
    return out


def diverge(ours: Capture, theirs: Capture) -> list[Divergence]:
    """Divergences between two aligned captures, in our step order then theirs."""
    mine = {s.step_id: s for s in ours.steps}
    other = {s.step_id: s for s in theirs.steps}
    out: list[Divergence] = []
    for capture, there in ((ours, other), (theirs, mine)):
        for step in capture.steps:
            if step.step_id not in there:
                out.append(Divergence(f"step-only-in-{capture.side}", step.step_id, step.step_id))
    for step in [*ours.steps, *[s for s in theirs.steps if s.step_id not in mine]]:
        a, b = mine.get(step.step_id), other.get(step.step_id)
        for side, own in (("indus", a), ("reference", b)):
            if own is not None and own.status != "completed":
                out.append(
                    Divergence(f"step-failed-in-{side}", own.step_id, own.step_id, own.status)
                )
        if (
            a is not None
            and b is not None
            and a.action == "wait_for"
            and a.status == b.status == "completed"
            and a.roles is not None
            and b.roles is not None
        ):
            for role in sorted(a.roles - b.roles):
                out.append(Divergence("role-only-in-indus", a.step_id, role))
            for role in sorted(b.roles - a.roles):
                out.append(Divergence("role-only-in-reference", a.step_id, role))
    for capture in (ours, theirs):
        if capture.page_errors:
            out.append(
                Divergence(
                    "page-errors", None, capture.side, f"{len(capture.page_errors)} uncaught"
                )
            )
    return out


@dataclass(frozen=True)
class Rules:
    intended: list[dict[str, Any]]
    gaps: list[dict[str, Any]]

    @classmethod
    def load(cls, path: Path = RULES) -> Rules:
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(list(data.get("intended", [])), list(data.get("gaps", [])))


def classify(flow: str, divergence: Divergence, rules: Rules) -> dict[str, Any]:
    """The divergence with its class: intended (and the cite), reference-gap, or unclassified."""
    entry = divergence.as_dict()
    for gap in rules.gaps:
        if gap.get("scenario") == flow and divergence.step in (gap.get("steps") or []):
            return {**entry, "class": "reference-gap", "why": gap.get("why")}
    for rule in rules.intended:
        if (
            rule.get("scenario") in (flow, "*")
            and rule.get("kind") == divergence.kind
            and rule.get("subject") in (divergence.subject, "*")
        ):
            return {**entry, "class": "intended", "cite": rule.get("cite"), "why": rule.get("why")}
    return {**entry, "class": "unclassified"}


def compare_flow(flow: str, ours: Capture, theirs: Capture, rules: Rules) -> dict[str, Any]:
    failures = alignment_failures(ours, theirs)
    if failures:
        return {"flow": flow, "aligned": False, "alignment_failures": failures, "divergences": []}
    items = [classify(flow, d, rules) for d in diverge(ours, theirs)]
    counts = Counter(item["class"] for item in items)
    return {
        "flow": flow,
        "aligned": True,
        "alignment_failures": [],
        "counts": dict(sorted(counts.items())),
        "divergences": items,
    }


def unclassified(result: Mapping[str, Any]) -> int:
    if not result.get("aligned", False):
        return 1
    return sum(1 for d in result["divergences"] if d["class"] == "unclassified")


def reference_capture(ref_dir: Path, flow: str) -> Path | None:
    for name in (f"{flow}.ref.capture.json", f"{flow}.capture.json"):
        if (ref_dir / name).is_file():
            return ref_dir / name
    return None


def compare_all(
    product_dir: Path, ref_dir: Path, out_dir: Path, flows: Sequence[str], rules_path: Path = RULES
) -> int:
    """Compare every flow that has both captures; write ``<flow>.compare.json`` and
    ``<flow>.divergences.json``; return how many divergences are unclassified."""
    rules = Rules.load(rules_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    left = 0
    for flow in flows:
        ours_path = product_dir / f"{flow}.capture.json"
        theirs_path = reference_capture(ref_dir, flow)
        if not ours_path.is_file() or theirs_path is None:
            continue
        ours = load_capture(json.loads(ours_path.read_text(encoding="utf-8")), "indus")
        theirs = load_capture(json.loads(theirs_path.read_text(encoding="utf-8")), "reference")
        result = compare_flow(flow, ours, theirs, rules)
        (out_dir / f"{flow}.compare.json").write_text(
            json.dumps({k: v for k, v in result.items() if k != "divergences"}, indent=2) + "\n",
            encoding="utf-8",
        )
        (out_dir / f"{flow}.divergences.json").write_text(
            json.dumps(result["divergences"], indent=2) + "\n", encoding="utf-8"
        )
        left += unclassified(result)
    return left


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare one flow's product and reference captures."
    )
    parser.add_argument("product", type=Path)
    parser.add_argument("reference", type=Path)
    parser.add_argument("--rules", type=Path, default=RULES)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    ours_data = json.loads(args.product.read_text(encoding="utf-8"))
    ours = load_capture(ours_data, "indus")
    theirs = load_capture(json.loads(args.reference.read_text(encoding="utf-8")), "reference")
    flow = ours.flow or args.product.name.split(".")[0]
    result = compare_flow(flow, ours, theirs, Rules.load(args.rules))
    print(json.dumps(result, indent=2))
    return 1 if unclassified(result) else 0


__all__ = [
    "KINDS",
    "Capture",
    "Divergence",
    "Rules",
    "Step",
    "alignment_failures",
    "aria_roles",
    "classify",
    "compare_all",
    "compare_flow",
    "diverge",
    "load_capture",
    "main",
    "unclassified",
]

if __name__ == "__main__":
    if __package__ in (None, ""):
        sys.path.insert(0, str(ROOT))
    raise SystemExit(main())
