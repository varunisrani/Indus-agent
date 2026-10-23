# Wave 49 integrator brief (the last 4 planned tasks)

Worktree: scratchpad/verify26, detached at main (check out main first). Read .agents/wip/w49/README.md in full first (merge
notes per patch) and AGENTS.md. D-ids: give the W49-<slice>-<n> labels the next free D-16 numbers after D-16.1109 (one §2.7
row each) and add a §3.1 range row.

1. Apply with `git apply --3way` in this order: w49a (mac signing), w49b (win/linux), w49d (msix), w49c (termux).
   Shared files — keep every slice's part: apps/desktop/electron-builder.config.cjs (mac section + afterExtract/afterSign
   from a; win/linux sections from b), apps/desktop/scripts/after-extract.mjs STAMPERS (darwin from a, win32 from b),
   builderConfig.test.ts, .github/workflows/release.yml (desktop-mac from a, desktop-win/linux from b), tests/unit/ci/
   test_release_workflow.py, docs/internals/traps-ledger.md (R08.22 and the Windows rows). If a hunk no longer applies,
   re-implement its intent and say so.
2. Secret names must agree across a, b and d (Windows: WIN_CERTIFICATE + WIN_CERTIFICATE_PASSWORD or the
   AZURE_TRUSTED_SIGNING_* group + INDUS_AZURE_SIGNING_DLIB; mac: MAC_CERTIFICATE, MAC_CERTIFICATE_PASSWORD, APPLE_*).
   Add an off-by-default opt-in lane mechanism if needed (claims.py JOB_LANES / DEFAULT_LANES, test_publish.py, gate needs)
   so the optional `termux` lane (calls termux.yml) and a `desktop-msix` lane can exist without running by default; pin
   with tests. Reword release.yml's shared desktop-lanes comment (it says no secrets are passed).
3. Text: phase table P23 → 23/23 built (signing runs owed to the owner), P24 → 47/47 built (21/22 built; first lane runs
   owed); release checklist RM14 → code done, owner secrets + first signed runs owed (list every secret name, incl. the
   APPLE_API_* route and TERMUX_APT_*); §0 secret list; RL8/RL9 and acceptance Known gaps S5 → partial delivery;
   ACC-S6-1 still owed. `checklist` guard must pass. LEAD-NOTES "Wave 49 landing notes (integrator)" ending with a
   phase-table status line.
4. node_modules: hard-link from /home/user/indus-agent/node_modules (`cp -al`; remove only verify26's own first).
   Full gate (short basetemp `/tmp/i49pt`, deleted after each run): uv sync --locked; ruff check + format --check; pyright;
   lint-imports; codegen and twin --checks; install build.py --check; pins; mkdocs build --strict; gen_reference --check;
   notices --check; `INDUS_STRESS_SECONDS=10 pytest -m "not perf"` then perf alone; `INDUS_RELEASE_LANE=1 pytest
   tests/e2e/upgrade`; tooling.checks --require-ref; biome check + format, tsc in every project, check:ipc,
   gen:bridge-types --check, dump:slash --check, root vitest (CI=true), `node --test scripts/release/msix.test.mjs`;
   Electron E2E under xvfb (Electron 44.5.1 zip through the proxy, verified against SHASUMS256); web Playwright CI=true.
   Delete build output and the Electron binary after.
5. Stage with `git add -A`. Never commit, never stash.

Rules (ENV RULE): `export S=/tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/int49` as ONE
statement, then `export HOME=$S/home XDG_CACHE_HOME=$S/home/.cache UV_CACHE_DIR=/tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/uv-cache-shared npm_config_cache=$S/home/.npm INDUS_HOME=$S/indus`
as a SECOND statement, in every Bash call. TMPDIR unset. Never run `indus dashboard` by hand; never `pnpm run` / `pnpm exec`
/ `npx`. No real signing, no secrets, no credential-shaped literals; no network except the Electron zip through the proxy;
no docker runs; never pkill by pattern; never git stash; never write under the real ~, /root or /. The container may
restart; when resumed, check what survived and continue.
Report: apply/conflicts, hand-reimplemented hunks, lane mechanism, D-ids, checklist changes, full gate counts per lane,
owner items (all secret names), follow-ups.
