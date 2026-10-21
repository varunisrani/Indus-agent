"""A fake ``systemd-run`` (P24-43), written by us: it appends its argv as one JSON line to the
file named by ``FAKE_SYSTEMD_RUN_LOG`` and then becomes the command after ``--`` (as the real one
does with ``--scope``).

``FAKE_SYSTEMD_RUN_REJECT_EXPAND=1`` makes it refuse ``--expand-environment`` like systemd < 254;
``FAKE_SYSTEMD_RUN_FAIL=1`` makes every call fail like a missing user manager.

``install(bin_dir)`` writes an executable ``systemd-run`` shim into ``bin_dir`` that runs this file
with the current interpreter.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_SHIM = """#!/bin/sh
exec {python} {script} "$@"
"""


def install(bin_dir: Path) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "systemd-run"
    shim.write_text(_SHIM.format(python=sys.executable, script=Path(__file__).resolve()))
    shim.chmod(0o755)
    return shim


def read_log(path: Path) -> list[list[str]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def main(argv: list[str]) -> int:
    log = os.environ.get("FAKE_SYSTEMD_RUN_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(argv) + "\n")
    if os.environ.get("FAKE_SYSTEMD_RUN_FAIL") == "1":
        sys.stderr.write("Failed to connect to bus: No medium found\n")
        return 1
    if os.environ.get("FAKE_SYSTEMD_RUN_REJECT_EXPAND") == "1" and any(
        a.startswith("--expand-environment") for a in argv
    ):
        sys.stderr.write("systemd-run: unrecognized option '--expand-environment=no'\n")
        return 1
    if "--" not in argv:
        sys.stderr.write("systemd-run: no command\n")
        return 1
    command = argv[argv.index("--") + 1 :]
    os.execvp(command[0], command)
    return 127  # pragma: no cover - execvp does not return


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
