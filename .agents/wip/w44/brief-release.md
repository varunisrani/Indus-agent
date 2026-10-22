# Wave 44 brief: P25-20..24 (release prep)

Base: main `e9d6c87`. Read in full first: docs/plan/phases/P25-hardening-parity-release.md (rows 20–24, Files, Traps,
Acceptance, Done when), AGENTS.md, HANDOFF.md (NEXT STEP + owner items), docs/plan/phases/LEAD-NOTES.md (P23 release
workflow + P25 wave 41 notes), docs/plan/18-phase-wise-build-plan.md ("After v1.0"), the P23 release workflow and
scripts under .github/workflows and scripts/release (if any), and .agents/wip/w43/README.md (security findings, parked).
Task notes go in docs/notes/P25/<slug>.md first (Status at the top). Clean room: never copy reference code.

Slices:
- R1 = P25-20 release checklist + bug-bash triage (docs/internals/release-checklist.md; triage every open item from task notes,
  HANDOFF, acceptance Known gaps, security findings SEC-1..11 + SEC2-1..4 (parked w43), the parity ledger into RM/RS/RL with
  permanent ids; owner-open items as safe placeholders, never invented values) + P25-21 release gates from a fresh clone
  (scripts/release/release_gates.sh, scripts/release/gates.txt, tests/unit/release/test_release_gates.py: the runner test uses
  a tiny local git repo and fake gate commands, never the real gates). D-ids D-16.920..934.
- R2 = P25-22 canary rehearsal: tests/e2e/upgrade/test_canary_to_stable.py automates the source-install half with two locally
  built releases (no network, no publishing; mark it for the release lane per the plan) + docs/internals/rehearsal-log.md as a
  template the owner fills (the real canary run needs the owner: signing secrets, GHCR) + P25-23 smoke and rollback kit
  (scripts/release/smoke/linux.sh, macos.sh, windows.ps1 using only public downloads at run time; docs/internals/rollback.md).
  shellcheck if available (do not install it system-wide); PSScriptAnalyzer is not available here: record that. D-ids
  D-16.935..949.
- R3 = P25-24 native Windows terminal backend, best effort (src/indus/backends/local_windows.py,
  tests/unit/backends/test_local_windows.py; the path-conversion table test runs everywhere; Windows-only behaviour gated
  behind the platforms("windows") marker; Windows guide section). D-ids D-16.950..959.

Rules (ENV RULE), for builder <x> in {r1, r2, r3}:
- `export S=/tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/<x>` as ONE statement, then a SECOND
  statement: `export HOME=$S/home XDG_CACHE_HOME=$S/home/.cache UV_CACHE_DIR=/tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/uv-cache-shared npm_config_cache=$S/home/.npm`.
  Repeat both in every Bash call. TMPDIR unset. Set INDUS_HOME to a temp dir before running `indus` by hand.
- pytest ALWAYS with `--basetemp=/tmp/<x>pt` (SHORT path; delete after each run). pnpm store scratchpad/pnpm-store-p20 with
  --frozen-lockfile (only if you need TS).
- No new packages; no credential-shaped literals (build fake tokens at runtime); no real network or cloud calls (R2's scripts
  may NAME public download URLs but tests must not fetch them); never pkill by pattern; never git stash; never write under
  the real ~, /root or /. Never commit. Delete build output when a step passes.
Before finishing: ruff check / format --check / pyright on your paths, lint-imports, pytest on the dirs you touched plus
tests/contract (short basetemp), tooling.checks --require-ref, mkdocs build --strict if you touch docs nav.
Report: files, task status, check results, decisions (D-ids), owner items, follow-ups.
