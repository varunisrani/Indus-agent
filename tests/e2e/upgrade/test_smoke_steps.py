"""P25-23: the smoke script's own steps pass on a source install of a locally built release.

The post-release smoke scripts work from public downloads, so CI cannot run them whole before a
release exists. This test runs every step that follows their install step — the very functions,
sourced from the script — on a stable release built and installed the way ``world.py`` does it,
in the installers' default layout under an isolated HOME (the layout the scripts expect). The
desktop steps need the release's packages and are skipped (``--desktop skip``). A step that would
fail on the owner's machine for a reason in our code fails here first.

Release lane only (``release`` marker, ``INDUS_RELEASE_LANE=1``).
"""

from __future__ import annotations

import os
import signal
import sys

from scripts.release.versioning import next_stable
from tests.e2e.upgrade.world import RELEASE_LANE_MARKS, ROOT, Rehearsal

SMOKE = ROOT / "scripts" / "release" / "smoke"
SCRIPT = SMOKE / ("macos.sh" if sys.platform == "darwin" else "linux.sh")
STEPS = (
    "version", "setup", "turn", "approval", "resume", "doctor", "dashboard-gate",
    "dashboard-login", "licences", "uninstall",
)  # fmt: skip
# Sources the script (it runs nothing when sourced), then runs the steps after the install.
DRIVER = r"""
set -u
source "$1"
smoke_parse --tag "$2" --desktop skip
SMOKE_WORK="$3"
SMOKE_LOGS="$3/logs"
smoke_isolate
trap smoke_cleanup EXIT
shift 3
for step in "$@"; do
    "step_${step//-/_}"
done
smoke_cleanup
trap - EXIT
printf '%s' "$SMOKE_RESULTS" >"$SMOKE_WORK/results.txt"
"""

pytestmark = RELEASE_LANE_MARKS


def test_the_smoke_steps_pass_on_a_local_source_install(smoke_world: Rehearsal) -> None:
    world = smoke_world
    version = next_stable(None)
    world.release(str(version))
    world.install("stable")
    done = world.run(
        ["bash", "-c", DRIVER, "smoke", str(SCRIPT), f"v{version}", str(world.base), *STEPS],
        timeout=1800,
    )
    leftover = world.gateway_pid()
    if leftover is not None:
        os.kill(leftover, signal.SIGTERM)
    assert leftover is None, "the smoke left a gateway daemon running"
    results: dict[str, tuple[str, str]] = {}
    for line in (world.base / "results.txt").read_text(encoding="utf-8").splitlines():
        step, status, *detail = line.split(maxsplit=2)
        results[step] = (status, detail[0] if detail else "")
    assert list(results) == list(STEPS), done.stdout + done.stderr
    # A release built here has no apps/ folder, so the dashboard has no pages to build: the API
    # gate still works, the page with the loopback token does not (a real release checkout builds
    # its pages with pnpm on first launch, when pnpm and Node are there).
    status, detail = results.pop("dashboard-login")
    assert status == "PASS" or "without a session token" in detail, detail
    failed = {step: row for step, row in results.items() if row[0] != "PASS"}
    assert not failed, f"{failed}\n{done.stdout}{done.stderr}"
    assert done.returncode == 0, done.stdout + done.stderr
    assert not world.checkout.exists() and not world.launcher.exists()
    assert (world.home / "state.db").is_file()
