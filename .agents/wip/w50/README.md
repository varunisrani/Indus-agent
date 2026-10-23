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
