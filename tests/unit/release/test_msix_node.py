"""P24-22: run the MSIX packaging tests (``scripts/release/msix.test.mjs``, ``node --test``).

The packaging script is JavaScript and its tests use Node's own runner, outside the pnpm
workspaces that vitest covers; running them here puts them in the Python gate. They need only Node
(no Windows tools, no network): makeappx and signtool are recorders inside the tests.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
TESTS = ROOT / "scripts" / "release" / "msix.test.mjs"
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_msix_packaging_suite_passes(tmp_path: Path) -> None:
    assert NODE is not None
    env = {
        key: value for key, value in os.environ.items() if not key.startswith(("WIN_", "AZURE_"))
    }
    env["TMPDIR"] = str(tmp_path)
    run = subprocess.run(  # noqa: S603 - fixed argv, our own test file
        [NODE, "--test", str(TESTS)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert run.returncode == 0, run.stdout[-4000:] + run.stderr[-2000:]
    assert "# fail 0" in run.stdout
