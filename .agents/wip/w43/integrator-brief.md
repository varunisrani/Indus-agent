# Wave 43 integrator brief (P25-01..04 security)

Worktree: scratchpad/verify23 (detached at main `e9d6c87`, which includes P24 wiring wave 42 `226a5ae`).
D-ids: D-16.900..919 (add a §3.1 range row in docs/plan/16-what-we-change.md).

1. Apply `.agents/wip/w43/w43-sec1.patch` then `w43-sec2.patch` with `git apply --3way` (both built on 7a5463a, before
   P25-05..19 and wave 42 landed). Expect conflicts in tooling/checks/__init__.py (both add to NAMES: keep both),
   docs/internals/traps-ledger.md (both cite rows), acceptance map. Keep both sides.
2. Wave 42 switched on new surfaces after SEC1 wrote the threat model: multiplexed gateway (served-profile attach over its own
   WS, D-16.877), relay front, room loop, LSP servers (LspUpkeep, stop gates), message_agent + a2a_call, preview.act/read,
   desktop contribution registry + PluginsPage, `indus dashboard --console`, TUI interactive under CI. Re-check every
   threat-model row citing these (B-LSP-1, preview automation, served-profile attach, contributions); add rows where missing,
   each with a proof path the doc test checks. SEC-5 (message_agent answers not fenced): if the fix is the one-line
   UNTRUSTED_NAMES addition like SEC-3, do it with a test; otherwise record it.
3. Run `python -m tooling.checks security_static` on the merged tree. Fix real hits in wave-42 code, or allow-list with a dated
   reason (`2026-10-22: …`). Same for `deps_audit` (offline snapshot); do not refresh it over the network.
4. Full gate (short pytest basetemp `/tmp/i43pt`, deleted after each run): uv sync --locked; ruff check + format --check; pyright;
   lint-imports; `python -m indus.protocol.codegen --check`; build/pins checks; mkdocs build --strict; gen_reference --check
   (regenerate if stale); notices --check; `INDUS_STRESS_SECONDS=10 pytest -m "not perf"` then perf alone; tooling.checks
   --require-ref; pnpm lint / fmt / -r typecheck / check:ipc / gen:bridge-types / dump:slash / test; Electron E2E under xvfb
   (Electron 44.5.1 zip checked against SHASUMS256 — this is the FIRST run of SEC2's security-audit.spec.ts); web Playwright
   with CI=true (includes SEC2's auth-gates.spec.ts). Delete build output and the Electron binary when done.
5. Update docs/plan/phases/P25 task notes' Status for 01–04, LEAD-NOTES section "P25 wave 43 landing notes (integrator)" ending
   with a phase-table status line. Stage everything with `git add -A`. Never commit, never stash.

Rules (ENV RULE):
- `export S=/tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/int43` as ONE statement, then a SECOND
  statement: `export HOME=$S/home XDG_CACHE_HOME=$S/home/.cache UV_CACHE_DIR=/tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/uv-cache-shared npm_config_cache=$S/home/.npm`.
  Repeat both in every Bash call. TMPDIR unset. Set INDUS_HOME to a temp dir before running `indus` by hand.
- pnpm store: scratchpad/pnpm-store-p20, `--frozen-lockfile`.
- No new packages; no credential-shaped literals in tests (build fake tokens at runtime); no real network or cloud calls;
  never pkill by pattern (stop only your own pids); never git stash; never write under the real ~, /root or /.
- Clean room: never copy reference code.
- Disk: keep the build output small; delete it when a lane passes.
Report: apply/conflicts, fixes (with D-ids), threat-model rows added, guard hits and resolutions, full gate counts per lane,
P25 01–04 Done-when status, follow-ups.
