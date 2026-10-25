# Wave 55 (lead: Agent 2) — UI parity follow-ups

## w55f1.patch — TUI tool rows, desktop switcher, Preview width (F1), base a06a405 — LANDED with D-16.1273
- Gate (builder): desktop vitest 181 / 1818; TUI vitest 54 / 794; TUI e2e 12 passed / 2 skipped (goldens regenerated,
  reviewed, re-verified); Electron E2E 43 / 2 skipped (security 12/12 with Preview 420); tsc + biome clean; tooling.checks ok.
- Slip: the web kit started `indus --tui` with a stale TUI bundle, so cli/tui_launch.py ran a pnpm build that reinstalled
  the worktree's node_modules; restored from $S/land, all checks re-run after. Rebuild apps/tui/dist after any TUI change
  before using the web kit.
