# Wave 55 (lead: Agent 2) — UI parity follow-ups

## w55f1.patch — TUI tool rows, desktop switcher, Preview width (F1), base a06a405 — LANDED with D-16.1273
- Gate (builder): desktop vitest 181 / 1818; TUI vitest 54 / 794; TUI e2e 12 passed / 2 skipped (goldens regenerated,
  reviewed, re-verified); Electron E2E 43 / 2 skipped (security 12/12 with Preview 420); tsc + biome clean; tooling.checks ok.
- Slip: the web kit started `indus --tui` with a stale TUI bundle, so cli/tui_launch.py ran a pnpm build that reinstalled
  the worktree's node_modules; restored from $S/land, all checks re-run after. Rebuild apps/tui/dist after any TUI change
  before using the web kit.

## w55f3.patch — desktop display preferences (F3), base 1359acc — PARKED, lead gate on F1 + F3 combined running
- 10 disabled Appearance rows made real as desktop prefs (intro splash, reopen last chat, session density, tab strip,
  chat backdrop, bubble opacity, text direction, timeline bars, tool call display, collapse thinking); defaults keep
  today's look. 10 rows stay disabled (6 need backend/features, hide code diffs needs a diff view, 3 renderer-only
  follow-ups: app actions side, file browser default, floating composer; vibe hearts).
- Gate (builder): desktop vitest 182 / 1825; Electron E2E 43 / 2 skipped; tsc 0; check-ipc + bridge ok; tooling.checks ok.
