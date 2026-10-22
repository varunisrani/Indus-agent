# Task: Release gates from a fresh clone (P25-21) Phase: 25 Owner: wave-44 builder R1

Status: Done (2026-10-22) for the runner, the gates file and the runner tests; the real run on the
candidate (P25-21 Verify, "real run … recorded in HANDOFF.md") is owed at the release commit.

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): none — no reference product file
applies. Process only: the clean-room-rebuild skill's reference 06 §3 (the release-candidate run
from a fresh clone, one line per gate, every gate runs after a failure) and reference 08 §5.
Evidence: none.

What it does (own words): `scripts/release/release_gates.sh <repo> <sha> <gates file>` clones the
repository into a new, empty work folder (no dependencies, caches, build output or ignored files),
checks out the candidate commit detached, and runs every gate in the gates file in order, each in
`bash -c` from the clone's root with stdin closed. A failing gate does not stop the run. For each
gate it writes one line `<gate> exit=<n> <s>s` to `<work>/gates.txt` and the gate's whole output to
`<work>/logs/<gate>.log`; `<work>/run.txt` records the repository, the commit and the times. It exits
0 when every gate exited 0, 1 when any failed, and 2 for a setup problem (bad arguments, a work
folder that is not empty, a clone or checkout that failed, an unreadable or malformed gates file)
before any gate runs.

`scripts/release/gates.txt` is the real list (P25-21): install, lint, format, types, unit,
contract, integration, security, upgrade (the release-lane rehearsal, `INDUS_RELEASE_LANE=1`), web e2e, TUI e2e, desktop e2e (packaged), clean room
`--require-ref`, acceptance and traps `--strict`, ACP protocol drift against the pinned schema, MCP
drift, bundle budget, perf (alone), docs build strict, notices fresh, dependency audit, parity
`--all --ledger`, similarity `--closest 20`, installer build fresh, Docker image tests, `nix flake
check`.

Inputs → outputs: repo (path or URL), sha, gates file; env `WORK_DIR` (default: a new folder made
by `mktemp -d`), plus whatever the gates read (`INDUS_REF`, `PARITY_REF`) → `gates.txt`, `run.txt`,
`logs/*.log`, `clone/`, exit code.

Files: `scripts/release/release_gates.sh`, `scripts/release/gates.txt`,
`tests/unit/release/test_release_gates.py`.

Edge cases / failure modes:
- a gate that fails, or a command that does not exist (127): recorded, the next gate runs;
- a gate that reads stdin: gets end of file, never hangs on the terminal;
- duplicate gate names, a bad name, a line without a command: refused before cloning (exit 2);
- an unknown or abbreviated sha: resolved with `rev-parse --verify <sha>^{commit}` in the clone;
  unknown → exit 2; the full sha is what `run.txt` records;
- a work folder that already holds files: refused (a fresh run never mixes with an old one);
- untracked or ignored files in a local source checkout never reach the clone (`git clone
  --no-local`, then checkout);
- gate names become file names: the name rule `[a-z0-9][a-z0-9_-]*` keeps them safe;
- an input the owner has to provide (reference captures): the gate uses `${PARITY_REF:?…}` and
  fails with that message instead of passing silently.

Our design differences / decisions:
- Gates file format and runner contract: D-16.926, D-16.927, D-16.931.
- The install gate syncs every extra (`uv sync --locked --all-extras`) like the release lane, so
  the strict acceptance and traps gates see no environment skips (D-16.928).
- Gates that need a Docker daemon, Nix or a display run where those exist; elsewhere they fail and
  the checklist says which CI lane stands in (D-16.930).
- The runner exports `GATE`, `LOG_DIR`, `CLONE_DIR` and `WORK_DIR` to each gate (Playwright output
  and per-gate temp folders go under `LOG_DIR`).

Tests we will write (`tests/unit/release/test_release_gates.py`, a tiny local repository and fake
gate commands, never the real gates):
- every gate runs after a failure, exit codes and durations recorded, summary lists every gate;
- per-gate logs hold the gate's output; the run exits 1 on a failure and 0 when all pass;
- the clone is fresh (an untracked file in the source is absent) and at the requested commit
  (abbreviated sha resolved);
- comments and blank lines ignored; duplicate or malformed names refused before cloning;
- unknown sha and a non-empty work folder are setup errors (exit 2);
- stdin is closed for gates; a missing command is exit 127 and the run goes on;
- the real `gates.txt` parses and names every gate P25-21 lists.

## Decisions for the lead
- D-16.926 – D-16.931.
- `deps_audit` and `tests/security` came with wave 43 (landed `4945b48`): the `audit` and
  `security` gates run them. Added at the wave 44 integration (D-16.961): the `upgrade` gate runs
  `tests/e2e/upgrade` with `INDUS_RELEASE_LANE=1` (P25-22's rehearsal, offline), pinned with the
  `security` and `audit` commands by
  `test_release_gates.py::test_the_real_gates_file_runs_the_security_audit_and_release_lane_suites`.

Compare checklist (filled after the rewrite; reopened skill reference 06 §3 and 08 §5):
- [x] clones into a new folder; runs every gate even after a failure
  (`tests/unit/release/test_release_gates.py::test_every_gate_runs_after_a_failure`)
- [x] one `<gate> exit=<n> <s>s` line per gate and a log per gate
  (`tests/unit/release/test_release_gates.py::test_summary_and_logs`)
- [x] the clone is fresh and at the requested commit
  (`tests/unit/release/test_release_gates.py::test_clone_is_fresh_and_at_the_commit`)
- Deliberate differences: the gates file is committed in the repository (the skill keeps it in the
  operator's scratch folder) so the list is reviewed like code; owner inputs through the
  environment.
- Not taken: parallel gates (one full suite at a time, reference 06 §1).

## Checks

- `bash -n` clean; shellcheck is not installed here (not installed system-wide on purpose): the
  script was written to its usual rules (quoted expansions, `--` before paths, no `eval`), to be
  checked when RS9 adds shellcheck to CI.
- Runner tests use `git` with no user or system config (`GIT_CONFIG_GLOBAL=/dev/null`,
  `GIT_CONFIG_NOSYSTEM=1`) and a home under the test's temp folder; no network.

## Status

Done (2026-10-22). Landed with wave 44 (integration on main `7d9a864`; D-16.961 adds the `upgrade`
gate; the runner's own tests pass). Not run: the real gates (they need the candidate commit, a machine with Docker,
Nix and a display, `INDUS_REF` and the owner's `PARITY_REF`); that run and its log in `HANDOFF.md`
belong to the release commit (checklist §1).
