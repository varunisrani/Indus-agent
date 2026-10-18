"""Run the guard checks: ``python -m tooling.checks [options] [names...]``.

Every named check runs (default: all), even after a failure. The exit code is the worst one:
0 clean, 1 violations, 2 setup problem.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
import time
from pathlib import Path

from tooling.checks import NAMES
from tooling.checks.common import DEFAULT_ROOT, SETUP, Context, Result, resolve_ref

STATUS = {0: "ok", 1: "FAIL", 2: "SETUP"}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m tooling.checks", description=__doc__)
    p.add_argument("names", nargs="*", help=f"checks to run (default: all of {', '.join(NAMES)})")
    p.add_argument("--require-ref", action="store_true", help="a missing reference is exit 2")
    p.add_argument("--ref", help="reference checkout (default: $INDUS_REF, then the pinned path)")
    p.add_argument("--root", default=str(DEFAULT_ROOT), help="repository root to check")
    p.add_argument("--json", action="store_true", help="print one JSON object")
    p.add_argument("--closest", type=int, default=0, metavar="N", help="rank top N similarity runs")
    p.add_argument("--strict", action="store_true", help="acceptance: Review rows also fail")
    p.add_argument("--body-file", help="prbody: file holding the pull request body")
    p.add_argument(
        "--write-skeleton",
        action="store_true",
        help="traps/acceptance: create or extend the ledger with missing ids",
    )
    return p


def run_check(name: str, ctx: Context) -> Result:
    started = time.monotonic()
    try:
        module = importlib.import_module(f"tooling.checks.{name}")
        result: Result = module.run(ctx)
    except Exception as exc:  # a crashing check is a setup problem, never a silent pass
        result = Result(name)
        result.setup(f"check crashed: {type(exc).__name__}: {exc}")
    result.seconds = time.monotonic() - started
    return result


def print_report(results: list[Result], worst: int) -> None:
    for r in results:
        if not (r.violations or r.warnings or r.notes):
            continue
        print(f"== {r.name}")
        for line in r.notes:
            print(f"  note: {line}")
        for line in r.warnings:
            print(f"  warning: {line}")
        for line in r.violations:
            print(f"  {STATUS[r.code]}: {line}")
    print()
    print(f"{'check':<12} {'status':<7} {'issues':>6} {'time':>8}")
    for r in results:
        status = STATUS[r.code]
        if r.code == 0 and r.warnings:
            status = "warn"
        print(f"{r.name:<12} {status:<7} {len(r.violations):>6} {r.seconds:>7.1f}s")
    print(f"result: {STATUS[worst]} (exit {worst})")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_intermixed_args(argv)
    names = args.names or list(NAMES)
    unknown = [n for n in names if n not in NAMES]
    if unknown:
        print(f"unknown check(s): {', '.join(unknown)}; known: {', '.join(NAMES)}", file=sys.stderr)
        return SETUP
    ctx = Context(
        root=Path(args.root).resolve(),
        ref=resolve_ref(args.ref),
        require_ref=args.require_ref,
        as_json=args.json,
        closest=max(0, args.closest),
        strict=args.strict,
        body_file=args.body_file,
        write_skeleton=args.write_skeleton,
    )
    results = [run_check(n, ctx) for n in names]
    worst = max((r.code for r in results), default=0)
    if args.json:
        print(json.dumps({"code": worst, "checks": [r.to_json() for r in results]}, indent=2))
    else:
        print_report(results, worst)
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
