# Wave 44 integrator brief (P25-20..24 release prep)

Worktree: scratchpad/verify24, detached at main AFTER wave 43 (P25-01..04 security) landed.
D-ids: the builders used D-16.920..931 (R1), 935..942 (R2), 950..958 (R3). Integration fixes: D-16.960..969. Add one §3.1
range row covering D-16.920..969 in docs/plan/16-what-we-change.md; copy R3's D-16.950..958 rows from
docs/notes/P25/windows-local.md into §2.7.

1. Apply `.agents/wip/w44/w44-r1.patch`, `w44-r2.patch`, `w44-r3.patch` in that order with `git apply --3way` (base bee44d9).
   Expected conflicts: tooling/checks/__init__.py NAMES (keep every check: checklist + w43's security_static + deps_audit),
   16-what-we-change.md (keep all rows, in id order), acceptance-map.md. Keep both sides.
2. Fix F-1 (from R2, must-fix): release.yml uploads only desktop-* artefacts, so install.sh / install.ps1 / install.cmd are
   never attached to a release while the install guide and install.cmd download them from releases/latest/download. Make the
   release workflow attach the installer scripts from the admitted commit; pin it with a test in tests/unit/ci (or where the
   workflow tests live). Smallest correct change.
3. Release checklist (R1, triage taken at bee44d9): wave 43 has now landed, so move RM1 ("land w43") and any must/should rows
   it closed to "Done in this wave" with the commit; add F-1 (Done, this integration) and F-2..F-5 as rows (F-3 dashboard pages
   on a clean script install and F-4 `--version` 0.0.0 on a source install: should, unless you judge them must with a reason).
   The `checklist` guard must pass.
4. `scripts/release/gates.txt` must list the gates that now exist (w43 audit + security) — run release_gates.sh's own unit
   tests; do NOT run the real gates file (that is the owner's, needs Docker, Nix, a display, PARITY_REF).
5. Full gate (short pytest basetemp `/tmp/i44pt`, deleted after each run): uv sync --locked; ruff check + format --check; pyright;
   lint-imports; codegen --check; build/pins checks; mkdocs build --strict; gen_reference --check; notices --check;
   `INDUS_STRESS_SECONDS=10 pytest -m "not perf"` then perf alone; `INDUS_RELEASE_LANE=1 pytest tests/e2e/upgrade` (offline);
   tooling.checks --require-ref; pnpm lint / fmt / -r typecheck / check:ipc / gen:bridge-types / dump:slash / test. The
   Electron and web Playwright lanes only if the slices touched TS or the web app (they did not; say so).
6. P25 task notes' Status for 20–24; LEAD-NOTES section "P25 wave 44 landing notes (integrator)" ending with a phase-table status
   line. Stage everything with `git add -A`. Never commit, never stash.

Rules (ENV RULE):
- `export S=/tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/int44` as ONE statement, then a SECOND
  statement: `export HOME=$S/home XDG_CACHE_HOME=$S/home/.cache UV_CACHE_DIR=/tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/uv-cache-shared npm_config_cache=$S/home/.npm`.
  Repeat both in every Bash call. TMPDIR unset. Set INDUS_HOME to a temp dir before running `indus` by hand.
- Never run `indus dashboard` by hand (its first launch builds the web app with pnpm and may fetch packages).
- pnpm store: scratchpad/pnpm-store-p20, `--frozen-lockfile`.
- No new packages; no credential-shaped literals; no real network or cloud calls; never pkill by pattern; never git stash;
  never write under the real ~, /root or /. Clean room. Delete build output when a lane passes.
Report: apply/conflicts, fixes (D-ids), checklist changes, full gate counts per lane, P25 20–24 Done-when status, owner
items, follow-ups.
