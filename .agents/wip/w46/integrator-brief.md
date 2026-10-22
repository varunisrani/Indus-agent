# Waves 45 + 46 integrator brief

Worktree: scratchpad/verify25, detached at main AFTER wave 44 (P25-20..24) landed. D-ids: S45 used D-16.970..975 and P45
D-16.980..983 (rows already in their patches). Wave-46 decisions are labelled W46-<slice>-<n> in the patches' notes:
give them D-16.990..999 and then D-16.1000.. as needed (one row each in §2.7), and add §3.1 range rows for D-16.970..989 and
the wave-46 range. Read .agents/wip/w45/README.md and .agents/wip/w46/README.md in full first (merge notes per patch).

1. Apply with `git apply --3way`, in this order, resolving conflicts by keeping both sides' intent:
   w45/w45-s45.patch, w45/w45-p45.patch, then w46/w46a, w46b (+ w46b-notices.patch), w46c (if parked), w46d, w46e, w46f,
   w46g, w46h, w46k. Known overlaps:
   - S45 × P45 in service/lsp_host.py and cli/commands/lsp.py: P45's `_manager_for(scope)` must use S45's per-profile tier
     (`_TIERS` keyed by profile home); `diagnostics.release_folder` must release in every tier. Add a test that restart and
     release reach the right profile's tier on a two-profile host.
   - S45 × w46a in core/untrusted.py (S45 adds wrap(); a adds Taint/fence(tainted=)/fence_as) and threat-model.md rows.
   - w46d × w46e in apps/desktop (PaneShell.tsx, customPages.tsx, channels.ts/schemas.ts vs Shell.tsx, main.tsx).
   - w46g × w44 in .github/workflows/ci.yml; w46b lockfiles (uv.lock, pnpm-lock.yaml, pyproject, package.json) — after
     applying, `uv lock --check` and `pnpm install --frozen-lockfile --offline` must accept them; never re-lock by hand.
2. release.yml additions from w46k (see its README section): build apps/web, tar to indus-web-${VERSION}.tar.gz into the
   upload dir before `publish.py upload`, include it in the artefact scan; add the rehearsal-log row. Pin with a test in the
   release workflow tests.
3. Docs: w46h's bots-and-peers.md and lsp.md need a paragraph each for P45 (message_agent in daemon bot chat; `indus lsp
   restart` reaching the daemon). Threat model: SEC-4/14/15/18 fixed — make the header line and the finding table agree; the
   doc test must pass. D-16.878: message_agent and suggestion offers done. Release checklist (landed with w44): move the
   rows these waves closed (RM2, RM4, RM8, RM21; RS4, RS6, RS7, RS9 (CI step added, first run pending), RS10, RS11, RS16,
   RS19; F-2..F-5 = RS23..RS26; RM28 SEC-14 and RM29 SEC-15 closed by S45) to "Done in this wave" with "this integration"; the `checklist` guard must pass.
4. Triage `tests/integration/gateway/multiplex/test_room_two_members.py` (flaky on main per w46f: 2 of 4 runs fail) and
   `test_boot_recovery::test_a_crash_left_turn_pauses_the_conversation_until_reset` (failed under load for P45): find the
   root cause on a quiet run and fix it (never skip or loosen a test); if you cannot, record exactly what you saw.
5. Full gate (short pytest basetemp `/tmp/i46pt`, deleted after each run): uv sync --locked; ruff check + format --check;
   pyright; lint-imports; codegen --check; build/pins/install build.py --check; mkdocs build --strict; gen_reference --check;
   notices --check; `INDUS_STRESS_SECONDS=10 pytest -m "not perf"` then perf alone; `INDUS_RELEASE_LANE=1 pytest
   tests/e2e/upgrade`; tooling.checks --require-ref; pnpm lint / fmt / -r typecheck / check:ipc / gen:bridge-types /
   dump:slash / test; Electron E2E under xvfb (Electron 44.5.1 zip checked against SHASUMS256); web Playwright CI=true.
   Delete build output and the Electron binary when done.
6. LEAD-NOTES section "Waves 45 + 46 landing notes (integrator)" ending with a phase-table status line. Stage everything with
   `git add -A`. Never commit, never stash.

Rules (ENV RULE):
- `export S=/tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/int46` as ONE statement, then a SECOND
  statement: `export HOME=$S/home XDG_CACHE_HOME=$S/home/.cache UV_CACHE_DIR=/tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/uv-cache-shared npm_config_cache=$S/home/.npm INDUS_HOME=$S/indus`.
  Repeat both in every Bash call. TMPDIR unset. Never run `indus dashboard` by hand. Never use `pnpm exec` or `npx` in a way
  that can auto-install: run `pnpm install --frozen-lockfile --offline` first (store scratchpad/pnpm-store-p20; if metadata
  is missing, stop and report rather than going online).
- No new packages; no credential-shaped literals; no real network or cloud calls; never pkill by pattern; never git stash;
  never write under the real ~, /root or /. Clean room.
Report: apply/conflicts, fixes (D-ids), flake triage, checklist changes, full gate counts per lane, follow-ups.
