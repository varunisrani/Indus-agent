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
