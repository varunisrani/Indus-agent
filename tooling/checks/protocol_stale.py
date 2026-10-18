"""Protocol stale check: the generated TypeScript protocol matches the Python models (D-04.3).

Runs ``python -m indus.protocol.codegen --check`` in a child process against the checked root
(``PYTHONPATH=<root>/src``), so the tree under ``--root`` is judged, not an installed copy. A
root without the protocol package (the guards' own fixture trees) is skipped with a note.
"""

from __future__ import annotations

import os
import subprocess
import sys

from tooling.checks.common import Context, Result

NAME = "protocol_stale"
GENERATOR = "src/indus/protocol/codegen.py"
TIMEOUT_S = 120
MAX_LINES = 60
STALE_MARK = "protocol output is stale"


def run(ctx: Context) -> Result:
    result = Result(NAME)
    if not (ctx.root / GENERATOR).is_file():
        result.notes.append(f"no {GENERATOR} under the root; nothing to check")
        return result
    env = dict(os.environ)
    src = str(ctx.root / "src")
    env["PYTHONPATH"] = os.pathsep.join(p for p in (src, env.get("PYTHONPATH", "")) if p)
    command = [sys.executable, "-m", "indus.protocol.codegen", "--check", "--root", str(ctx.root)]
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
        result.setup(f"could not run the protocol generator: {exc}")
        return result
    if proc.returncode == 0:
        result.notes.append("generated protocol files are up to date")
        return result
    # An uncaught exception also exits 1, so a stale result is recognised by its closing line.
    if proc.returncode == 1 and STALE_MARK in proc.stdout:
        lines = proc.stdout.splitlines()
        shown = lines[:MAX_LINES]
        if len(lines) > MAX_LINES:
            shown.append(f"... {len(lines) - MAX_LINES} more diff lines")
        result.fail(
            "packages/protocol/src/generated is stale; run `uv run python -m"
            " indus.protocol.codegen`\n" + "\n".join(shown)
        )
        return result
    tail = (proc.stderr or proc.stdout).strip().splitlines()[-5:]
    result.setup(f"protocol generator exited {proc.returncode}: " + " | ".join(tail))
    return result


if __name__ == "__main__":
    from tooling.checks.__main__ import main

    raise SystemExit(main([NAME, *sys.argv[1:]]))
