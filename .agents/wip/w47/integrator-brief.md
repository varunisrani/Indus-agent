# Wave 47 integrator brief

Worktree: scratchpad/verify26 (or verify25 moved), detached at main AFTER waves 45+46 landed. The 8 patches in
.agents/wip/w47/ were built on a LOCAL snapshot commit of the waves 45+46 integration (its hash is in each .base file;
the commit object exists in this container's repo). Read .agents/wip/w47/README.md in full first (merge notes per patch).
D-ids: wave-47 decisions are labelled W47-<slice>-<n>; give them the next free D-16 numbers after the waves 45+46 landing
(check §2.7 and §3.1 of docs/plan/16-what-we-change.md) and add a §3.1 range row.

1. Apply with `git apply --3way` in this order: w47c, w47f, w47a, w47b, w47e, w47d, w47h, w47g. Known overlaps:
   - packages/protocol/src/generated/* and docs/user/reference/protocol.md: w47a (HistoryMessage.arguments), w47h
     (groups.approve/watch/unwatch/event, method count 90→93), w47g (PromptSubmitParams.single_query). Resolve the Python
     sources (src/indus/protocol/*.py, tests/unit/protocol/*) by keeping all, then REGENERATE with
     `python -m indus.protocol.codegen` and `scripts/docs/gen_reference.py`; never hand-merge generated files.
   - docs/internals/threat-model.md (a, c, g, h rows), task notes (several "Wave 47" sections): keep all.
   - tui_bridge/methods_session.py (w47a display args vs w47c picker_reads), service/session_service.py (w47b),
     cli/attach.py (w47g removes w46f's one_shot_answer).
   If a hunk no longer applies because waves 45+46 changed differently after the snapshot, re-implement that hunk's
   intent by hand and say so in the report.
2. Text the builders left to the lead: D-16.549 (partly closed by w47c), D-16.554 / D-16.1010 / D-16.1025 and the
   dual-host-matrix note's cli.q.denies line (w47g), LEAD-NOTES "attached -q deny" follow-up (close), release checklist:
   move to Done what wave 47 closed (RS3 only once room UI lands — leave it open, note the daemon half; RS4-related
   picker rows, RL26 → partially done with refusals, F-3/F-4 follow-ups from w47f, the screenshot bugs), the checklist
   guard must pass.
3. Full gate (short basetemp `/tmp/i47pt`, deleted after each run): uv sync --locked; ruff check + format --check;
   pyright; lint-imports; codegen --check; build/pins/install build.py --check; mkdocs build --strict; gen_reference
   --check; notices --check; `INDUS_STRESS_SECONDS=10 pytest -m "not perf"` then perf alone; `INDUS_RELEASE_LANE=1
   pytest tests/e2e/upgrade`; tooling.checks --require-ref; pnpm lint / fmt / -r typecheck / check:ipc /
   gen:bridge-types / dump:slash / test; Electron E2E under xvfb (Electron 44.5.1 zip checked against SHASUMS256);
   web Playwright CI=true. Delete build output and the Electron binary after.
4. LEAD-NOTES section "Wave 47 landing notes (integrator)" ending with a phase-table status line. Stage with
   `git add -A`. Never commit, never stash.

Rules (ENV RULE): `export S=/tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/int47` as ONE
statement, then `export HOME=$S/home XDG_CACHE_HOME=$S/home/.cache UV_CACHE_DIR=/tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/uv-cache-shared npm_config_cache=$S/home/.npm INDUS_HOME=$S/indus`
as a SECOND statement, in every Bash call. TMPDIR unset. Never run `indus dashboard` by hand; never `pnpm run` /
`pnpm exec` / `npx` before node_modules is complete (they can auto-install from the network; hard-link node_modules from
another worktree with the same lockfile if offline install lacks metadata). No new packages; no credential-shaped
literals; no real network except the Electron zip through the proxy; never pkill by pattern; never git stash; never
write under the real ~, /root or /. The container may restart; when resumed, check what survived and continue.
Report: apply/conflicts, hand-reimplemented hunks, D-ids, checklist changes, full gate counts per lane, follow-ups.
