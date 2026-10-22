"""Generated twins check: the JSON twins the startup path reads match their sources (W48-g).

Two files ship as generated twins so the one-shot path skips slow parsing at import:
``src/indus/providers/pricing_table.json`` (from ``pricing_table.yaml``) and
``src/indus/protocol/field_docs.json`` (the field docstrings of the wire models). Each generator
has a ``--check`` mode; this guard runs both in a child process against the checked root
(``PYTHONPATH=<root>/src``), so the tree under ``--root`` is judged, not an installed copy. A root
without a generator (the guards' own fixture trees) skips that twin with a note.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import NamedTuple

from tooling.checks.common import Context, Result

NAME = "generated_twins"
TIMEOUT_S = 120
STALE_MARK = "is stale"


class Twin(NamedTuple):
    module: str
    generator: str
    output: str


TWINS = (
    Twin(
        "indus.providers.pricing_twin",
        "src/indus/providers/pricing_twin.py",
        "src/indus/providers/pricing_table.json",
    ),
    Twin(
        "indus.protocol.field_docs",
        "src/indus/protocol/field_docs.py",
        "src/indus/protocol/field_docs.json",
    ),
)


def _check_one(ctx: Context, twin: Twin, env: dict[str, str], result: Result) -> None:
    if not (ctx.root / twin.generator).is_file():
        result.notes.append(f"no {twin.generator} under the root; nothing to check")
        return
    command = [sys.executable, "-m", twin.module, "--check"]
    try:
        proc = subprocess.run(
            command,
            cwd=ctx.root,
            env=env,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        result.setup(f"could not run {twin.module}: {exc}")
        return
    if proc.returncode == 0:
        result.notes.append(f"{twin.output} is up to date")
        return
    # An uncaught exception also exits 1, so a stale twin is recognised by its message.
    if proc.returncode == 1 and STALE_MARK in proc.stdout:
        result.fail(f"{twin.output} is stale; run `uv run python -m {twin.module}`")
        return
    tail = (proc.stderr or proc.stdout).strip().splitlines()[-5:]
    result.setup(f"{twin.module} exited {proc.returncode}: " + " | ".join(tail))


def run(ctx: Context) -> Result:
    result = Result(NAME)
    env = dict(os.environ)
    src = str(ctx.root / "src")
    env["PYTHONPATH"] = os.pathsep.join(p for p in (src, env.get("PYTHONPATH", "")) if p)
    for twin in TWINS:
        _check_one(ctx, twin, env, result)
    return result


if __name__ == "__main__":
    from tooling.checks.__main__ import main

    raise SystemExit(main([NAME, *sys.argv[1:]]))
