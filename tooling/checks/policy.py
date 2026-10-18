"""Read guard policy from the base branch: ``python -m tooling.checks.policy --base <git ref>``.

On a pull request the guards must judge the change with the rules that were in force before it
(R10.22). This copies every policy file as it is at ``<git ref>`` over the working tree, so a PR
that widens an allow-list is still judged by the old one. A policy file the base does not have yet
keeps the PR's copy (a brand-new guard has nothing older to read) and is reported.
"""

from __future__ import annotations

import argparse
import fnmatch
import subprocess
import sys
from pathlib import Path

from tooling.checks.common import DEFAULT_ROOT

POLICY_DIR = "tooling/checks"
POLICY_PATTERNS = (
    "*_allow.json",
    "banned_patterns.json",
    "pins_meta.json",
    "reference.json",
)


def is_policy(path: str) -> bool:
    parent, _, name = path.rpartition("/")
    return parent == POLICY_DIR and any(fnmatch.fnmatch(name, p) for p in POLICY_PATTERNS)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=False)


def base_files(root: Path, base: str) -> list[str]:
    out = _git(root, "ls-tree", "--name-only", f"{base}:{POLICY_DIR}")
    if out.returncode != 0:
        raise RuntimeError(f"cannot list {POLICY_DIR} at {base}: {out.stderr.decode().strip()}")
    names = out.stdout.decode().split()
    return sorted(p for p in (f"{POLICY_DIR}/{n}" for n in names) if is_policy(p))


def head_files(root: Path) -> list[str]:
    folder = root / POLICY_DIR
    if not folder.is_dir():
        return []
    paths = (f"{POLICY_DIR}/{p.name}" for p in folder.iterdir() if p.is_file())
    return sorted(p for p in paths if is_policy(p))


def restore(root: Path, base: str) -> list[str]:
    """Overlay base-branch policy files onto root; one report line per policy file."""
    report: list[str] = []
    from_base = base_files(root, base)
    for path in from_base:
        blob = _git(root, "show", f"{base}:{path}")
        if blob.returncode != 0:
            raise RuntimeError(f"cannot read {path} at {base}: {blob.stderr.decode().strip()}")
        target = root / path
        before = target.read_bytes() if target.is_file() else None
        target.write_bytes(blob.stdout)
        if before is None:
            report.append(f"{path}: restored from {base} (the change deleted it)")
        elif before != blob.stdout:
            report.append(f"{path}: replaced by the {base} copy (the change edits it)")
        else:
            report.append(f"{path}: unchanged")
    for path in head_files(root):
        if path not in from_base:
            report.append(f"{path}: new in this change, kept (judged from base from the next one)")
    return report


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m tooling.checks.policy", description=__doc__)
    p.add_argument("--base", required=True, help="git ref or commit of the base branch")
    p.add_argument("--root", default=str(DEFAULT_ROOT), help="repository root")
    args = p.parse_args(argv)
    try:
        lines = restore(Path(args.root).resolve(), args.base)
    except RuntimeError as exc:
        print(f"policy: {exc}", file=sys.stderr)
        return 2
    for line in lines:
        print(f"policy: {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
