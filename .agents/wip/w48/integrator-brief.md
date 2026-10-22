# Wave 48 integrator brief

Worktree: scratchpad/verify26, detached at main (check out main first). Read .agents/wip/w48/README.md in full first.
D-ids: give the W48-<slice>-<n> labels the next free D-16 numbers after D-16.1069 (one §2.7 row each) and add a §3.1 range row.

1. Apply with `git apply --3way` in this order: w48f, w48e, w48g, w48d, w48b, w48h, w48c, w48a.
   - w48g is relative to LOCAL commit 34c34d1 (= 1b6e711 + w48e) and w48h to LOCAL 0992fb4 (= 1b6e711 + w48b); both commits
     exist in this repo, so 3-way works. Apply e before g and b before h.
   - Generated files: packages/protocol/src/generated/* and docs/user/reference/protocol.md change in b and h — resolve the
     Python sources, then REGENERATE with `python -m indus.protocol.codegen` and `scripts/docs/gen_reference.py`; never
     hand-merge generated files. Also regenerate w48g's twins after everything is applied:
     `python -m indus.providers.pricing_twin` and `python -m indus.protocol.field_docs` (both have --check), and wire
     both --check commands into the gate (tooling.checks or the CI job that runs codegen --check) with a test.
   - Expected overlaps: gateway/ws/methods.py and service/picker_reads.py (b, h), tui_bridge/methods_slash.py (b, h),
     threat-model.md rows (a, b, h), task notes, the dual-host allow-list (b, h removed entries), core/tool_round.py (c).
     If a hunk no longer applies, re-implement its intent and say so.
2. Docs/text: D-16.549 (now only config.set), D-16.550 (closed by w48h), D-16.1040 (W48-b-3), D-16.1056 / RS3 (desktop
   room view done; TUI/web room views are new surfaces — record as an owner decision, W48-a-2), the release checklist rows
   these close (RS3 desktop part, the minor UI follow-ups from the screenshots, RL26 additions, the katex/sprintf advisories),
   pypdf re-lock date 2026-10-27 (W48-f-3), RM25 perf note (W48-e/g). `checklist` guard must pass.
3. node_modules: hard-link from scratchpad/wt-w48f (`cp -al`; its node_modules match the NEW lockfile from w48f, vite 8.3.3);
   remove verify26's old node_modules first (only verify26's — never another worktree's). `pnpm install --frozen-lockfile
   --offline` may now work with wt-w48f's store under scratchpad/w48f? — do not go online; if offline install fails, use
   the hard-linked tree and run tools from node_modules/.bin.
4. Full gate (short basetemp `/tmp/i48pt`, deleted after each run): uv sync --locked; ruff check + format --check; pyright;
   lint-imports; codegen --check; the two twin --checks; build/pins/install build.py --check; mkdocs build --strict;
   gen_reference --check; notices --check; `INDUS_STRESS_SECONDS=10 pytest -m "not perf"` then perf alone (report
   oneshot_first_token honestly; do not change the budget); `INDUS_RELEASE_LANE=1 pytest tests/e2e/upgrade`;
   tooling.checks --require-ref; biome check + format, tsc in every project, check:ipc, gen:bridge-types --check,
   dump:slash --check, root vitest (CI=true); Electron E2E under xvfb (Electron 44.5.1 zip through the proxy, verified
   against SHASUMS256); web Playwright CI=true. Delete build output and the Electron binary after.
5. LEAD-NOTES section "Wave 48 landing notes (integrator)" ending with a phase-table status line. Stage with `git add -A`.
   Never commit, never stash.

Rules (ENV RULE): `export S=/tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/int48` as ONE
statement, then `export HOME=$S/home XDG_CACHE_HOME=$S/home/.cache UV_CACHE_DIR=/tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/uv-cache-shared npm_config_cache=$S/home/.npm INDUS_HOME=$S/indus`
as a SECOND statement, in every Bash call. TMPDIR unset. Never run `indus dashboard` by hand; never `pnpm run` /
`pnpm exec` / `npx`. No new packages; no credential-shaped literals; no real network except the Electron zip through the
proxy; never pkill by pattern; never git stash; never write under the real ~, /root or /. The container may restart; when
resumed, check what survived and continue.
Report: apply/conflicts, hand-reimplemented hunks, D-ids, checklist changes, full gate counts per lane, follow-ups.
