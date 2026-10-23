## w50e (desktop identity follow-ups) — 12 files, base 7eee12d, clean scan
- Runtime identity from the build stamp's channel (`electron/identity.ts`): canary = `Indus Canary`, Linux launcher id
  `ai.indus.desktop.canary` (own .desktop + icon; stable untouched); staged `desktopName` per channel.
- Windows updates: `verifyUpdateCodeSignature: true`, `win.signtoolOptions.publisherName` from
  `packaging/msix/identity.json`; `dist-win.mjs` refuses a SIGNED build while the publisher is `OWNER-TO-FILL`.
- Decisions W50-e-1..4 (update owner on Windows stays `manual` until a signed run). Integrator: update the D-16.1122 row
  and W49-b-6 text (Windows verification now on).
- Builder gate: biome, tsc, desktop vitest 1676 passed, tooling.checks ok, pytest contract/packaging/ci/release 2741 passed.
## w50a (attached config.set + /status queued count) — 18 files, base 7eee12d, clean scan
- Daemon serves `config.set` (shared checks in `service/config_writes.py`; session switches in `service/daemon_config.py`);
  `/yolo`, `/reasoning`, `/fast` are shared server commands; daemon `/status` shows `Queued messages: N`.
- No protocol change (config.set was already declared); codegen/twins unchanged.
- Decisions W50-a-1..7. Note: attached `/verbose` changes the reported mode but the daemon does not yet trim tool events
  per viewer (follow-up). Deferred model switch applies at next turn start on the daemon (bridge: at turn end).
- Merge with w50b: adjacent hunks in `daemon_slash.py` (after `skills`) and `picker_reads.py` (DAEMON_REFUSED above
  DAEMON_QUICK_EXEC) — keep both sides. Ledger: close D-16.549; D-16.1094 text; RS30/RS33 and P17/ws-endpoint.md wording.
- Builder gate: ruff, pyright 0, lint-imports, codegen/twins --check, tooling.checks ok; pytest contract/service/
  tui_bridge/gateway/integration 5315 passed, 0 failed.
## w50d (Termux follow-ups) — 9 files, base 7eee12d, clean scan
- `indus update`: new `apt` install kind from the `.install_method` stamp (wins over `.git`); refusal message
  `pkg upgrade indus-agent` (package name is indus-agent, W50-d-1); exit 1 like other managed kinds.
- `scripts/termux/repo.py`: `validate` (Release/index/pool/stanza/payload checks, collected) and `sign` commands, exit 4 on
  an invalid tree; `build.sh` stages now `... pack repo validate sign` (11).
- Decisions W50-d-1..5. Follow-ups: `indus uninstall` apt kind; dashboard `_UPDATE_HINTS` apt entry; termux.yml exit-4
  annotation.
- Builder gate: ruff, pyright 0, lint-imports, twins --check, tooling.checks ok; pytest release/cli/ci/contract/install
  4252 passed, 0 failed.
## w50b (daemon shell.exec + quick exec) — 19 files, base 7eee12d, clean scan
- `service/host_shell.py`: shared shell checks/runner/output cleaning (bridge delegates, unchanged behaviour); daemon
  serves `shell.exec` (named session must be attached by this connection, else SESSION_NOT_FOUND); quick `exec` commands
  served on the daemon (`DAEMON_QUICK_EXEC` removed).
- Protocol: `shell.exec` row gains SESSION_NOT_FOUND + session_id doc; generated TS/schema/field_docs/reference regenerated.
  security_allow.json shell exception moved to host_shell.py (dated 2026-10-23). Threat model B-SHELL-1/B-DAEMON-5 proofs.
- Decisions W50-b-1..4. Owner/lead question: should attached `!command` ever go through the sandbox backend (W50-b-2).
- Merge with w50a: picker_reads.py, test_ws_tui_session.py, test_ws_tui_reads.py, methods.py docstring, B-DAEMON-5;
  REGENERATE codegen + field_docs + gen_reference after both are applied (never hand-merge generated files).
- Builder gate: ruff, pyright 0, lint-imports, codegen/twins --check, tooling.checks ok, tsc/biome/vitest protocol+tui
  801 passed; pytest 6339 passed then the 2 pinned tests updated (affected set 85 passed).
## w50f (start-up twins, RM25) — 13 files, base 7eee12d, clean scan
- Config: `defaults.json` twin (keyed by sha256 of schema.py + versions.py), models `defer_build`, `validate()` checks only
  changed top-level settings (warnings identical to the whole-model check on 25 inputs). Generator
  `foundation/config/defaults_twin.py --check`.
- Skills: `shipped_frontmatter.json` twin keyed by the front-matter block's own sha256 (edited/user skills always parse).
  Generator `learning/skills/frontmatter_twin.py --check`. Both in the `generated_twins` guard; biome skips both JSONs.
- Measured: schema import 199 → 131 ms; shipped-skill parse 20 → 1 ms; one-shot first byte within box noise (RM25: judge on
  a quiet runner). Perf budgets unchanged.
- Decisions W50-f-1..3. Note: any edit to schema.py (even comments)/versions.py/shipped SKILL.md needs a twin regen.
  REPL pump test (`test_keys.py::test_feed_items_reach_the_screen_through_the_pump`) flaked once under load ~13.
- Builder gate: ruff, pyright 0, lint-imports, all --check 0, biome, tooling.checks ok; pytest 15326 passed, 1 load flake.
## w50c (Windows plan() foreground + starter race) — 5 files, base 7eee12d, clean scan
- `terminal.py`: `plan(foreground_only=…)` true only on native Windows host-local backends: trailing `&` refused with a
  foreground hint (WSL2 / Docker / SSH), over-limit timeout capped to `terminal.max_foreground_timeout` with
  `timeout_capped`, guidance texts from `WINDOWS_GUIDANCE`; POSIX and Windows sandbox backends unchanged.
- `test_windows_rl26.py`: starter test waits on the shell's and child's pid lines (no sleep); the `GroupJobs` stand-in
  recounts the pipe before calling it closed (second race). 20x: 20/20, 220/220 under CPU load, plan tests 2000/2000.
- docs/user/getting-started/windows.md "Foreground only". Decisions W50-c-1..4. Follow-up: windows-local real-child lane.
- Builder gate: ruff, pyright 0, lint-imports, all --check, mkdocs --strict, tooling.checks ok; pytest 4965 passed, 0 failed.
