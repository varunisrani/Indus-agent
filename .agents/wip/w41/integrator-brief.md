# Integrate P25 wave 41 (w41-d1 docs + notices, w41-g1 gates + perf, w41-q1 parity) on main (P24 landed)

Worktree: /tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/verify21 (from main 1c29df1). Do NOT commit.
ENV RULE: `export S=/tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/int41` as ONE statement, then
`export HOME=$S/home XDG_CACHE_HOME=$S/home/.cache UV_CACHE_DIR=$S/home/.cache/uv npm_config_cache=$S/home/.npm` as a SECOND
statement before the first uv/pnpm command. TMPDIR unset. pytest ALWAYS `--basetemp=/tmp/i41pt` (short; delete after each run).
Never write under the real ~, /root or /. Never pkill by pattern, never git stash. Wave-42 wiring builders run in parallel on
gateway/daemon, core/loop, tools, apps/desktop, apps/tui — do not touch their areas.

Patches (base 34a3315, before P24): .agents/wip/w41/w41-g1.patch, w41-q1.patch, w41-d1.patch — apply in that order with
`git apply --3way`. Read .agents/wip/w41/README.md in full first (each section lists what to do after P24 lands), the notes
in docs/notes/P25/, docs/plan/phases/P25-hardening-parity-release.md, AGENTS.md, HANDOFF.md.

Steps:
1. Apply; resolve unions (acceptance-map.md, traps-ledger.md — P24's lines for R06.26 / R08.11 win; ci.yml; pyproject dev group;
   .gitignore; similarity/licence allow files).
2. Online: `uv lock` (D1's mkdocs dev pins) + `uv sync --locked`; then the D1 post-P24 order: `uv sync --all-extras` (in a
   throwaway venv under $S if needed), `python scripts/release/notices.py refresh` → review new rows (P24's sherpa-onnx,
   sherpa-onnx-core etc.) → `python scripts/release/notices.py` → `python scripts/docs/gen_reference.py`; then `--check` both.
3. CI wiring G1 owes: web bundle check after the web build, `pnpm --filter @indus/tui bench`, `tooling.checks acceptance traps
   --strict` in the RELEASE gate only (not the PR gate — it fails by design until people act), the artefact sweep step;
   parity `ledger.py --check` + `runner.py --all --dry-run` in CI.
4. Re-run G1's acceptance/traps checks on the merged tree (P24 added rows); keep the default checks green; report the strict
   counts.
5. D-ids D-16.830 … D-16.859 in docs/plan/16-what-we-change.md §2.7 (+ §3.1 range row) from the three slices' decisions;
   LEAD-NOTES section "P25 wave 41 landing notes (integrator)".
6. Full gate: uv sync --locked; ruff; format; pyright; lint-imports; codegen --check; pins + build checks; `mkdocs build --strict`;
   `INDUS_STRESS_SECONDS=10 uv run pytest -q -p no:cacheprovider -rf -m "not perf" --basetemp=/tmp/i41pt` (background + log);
   perf alone; tooling.checks --require-ref; pnpm lint / fmt / -r typecheck / test. Root-cause anything red (3x alone).
7. Scans; `git add -A`; report (files beyond patches, notices/ref regen results, CI wiring, strict counts, D-ids, gate counts).
