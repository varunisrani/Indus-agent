# Integrate wave 42 (P24 owed wiring, D-16.799): w42-wa (gateway/daemon), w42-wb (agent loop + tools), w42-wc (UI surfaces)

Worktree: /tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/verify22 (from main f59f7dd; P24 + P25-05..19
landed). Do NOT commit. ENV RULE: `export S=/tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/int42` as
ONE statement, then `export HOME=$S/home XDG_CACHE_HOME=$S/home/.cache UV_CACHE_DIR=$S/home/.cache/uv npm_config_cache=$S/home/.npm
ELECTRON_CACHE=$S/electron-cache` as a SECOND statement before the first uv/pnpm command. TMPDIR unset. pytest ALWAYS
`--basetemp=/tmp/i42pt` (short; delete after each run). Never write under the real ~, /root or /. Never pkill by pattern, never
git stash. Security builders SEC1/SEC2 run in parallel (tests/security, tooling/checks guards, apps security tests) — do not
touch their areas.

Patches (all base 3de5486): .agents/wip/w42/w42-wa.patch, w42-wb.patch, w42-wc.patch — apply in that order with `git apply
--3way`. Read .agents/wip/w42/README.md in full first (each section lists decisions, owed items and shared files), the P24
notes' "Wiring (wave 42 …)" sections, LEAD-NOTES "P24 waves 37–39 landing notes", AGENTS.md, HANDOFF.md.

Steps:
1. Apply; resolve unions — gateway/daemon/process.py (WA host/multiplex/parts + WB registry/transport/origin/lsp lines),
   protocol models + TS codegen (WB preview.act/read + WC voice.listen: regenerate ONLY with `python -m indus.protocol.codegen`;
   update the method-count test once), tests/unit/protocol samples, acceptance-map rows, P24 notes, schema.
2. Root-cause and fix the pre-existing order-dependent failure test_a_cli_pass_can_deliver_to_the_own_bot_chat
   (DeliveryRouter's self-chat deliverer leaks across tests when tests/unit/gateway runs first) — product or fixture fix,
   pinned by a test.
3. Lead decisions to apply: D-16.542 — let the attach WebSocket accept a SERVED profile's own scope so served profiles get
   attach (CLI/TUI) when multiplexed, with a test; if it ripples widely, record instead. Mark D-16.799 items done/partial in
   docs/plan/16-what-we-change.md and flip D-16.800 (sherpa default applied). Check browser golden prompts / tool arrays (WB
   left unverified) — regenerate with the repo's tool if they changed.
4. Re-run the three demos: two profiles on one gateway, the two-member room, the LSP stop gate. Then the P24 "Done when" list:
   say which items hold now.
5. D-ids D-16.860 … D-16.889 (§2.7 + §3.1 range row) from the three builders' decisions. LEAD-NOTES section "P24 wiring wave 42
   landing notes (integrator)"; phase-table-ready P24 status line.
6. Full gate: uv sync --locked; ruff; format; pyright; lint-imports; codegen --check; pins + build checks; mkdocs --strict;
   gen_reference --check (regenerate reference pages if the new commands/tools changed them); notices --check;
   `INDUS_STRESS_SECONDS=10 uv run pytest -q -p no:cacheprovider -rf -m "not perf" --basetemp=/tmp/i42pt` (background + log);
   perf alone; tooling.checks --require-ref; pnpm lint / fmt / -r typecheck / check-ipc / gen-bridge-types --check /
   dump-slash --check / test; Electron E2E lane under xvfb (Electron 44.5.1 zip vs SHASUMS256; delete it after) and web
   Playwright. Root-cause anything red (3x alone).
7. Scans; `git add -A`; delete build output. Report: conflicts, fixes, D-16.542 outcome, demos, Done-when, D-ids, gate counts.
