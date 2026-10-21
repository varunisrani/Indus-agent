# Task: Performance budgets, TypeScript (P25-13) Phase: 25 Owner: wave-41 builder G1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): none — our budgets (D-04.2, the
P18-29 chunk plan, the P20 packaged smoke).
Evidence: none.

What it does (own words): three checks read their numbers from `tooling/perf/budgets.toml`
through one small reader, `tooling/perf/read-budgets.mjs` (the TOML subset the file keeps to;
anything else is an error, and `tests/perf` pins the subset from the Python side).

- `apps/web/scripts/check-bundle.mjs [dist]`: on a production build, the JS `index.html` loads up
  front (module scripts + modulepreloads, base path ignored) must total ≤ `web.initial_js`
  (350 kB gzip), and every other chunk ≤ `web.route_chunk` (200 kB gzip). gzip level 9,
  1 kB = 1,000 bytes. Exit 1 names each file over budget; exit 2 when there is no build.
- `apps/tui/bench/stream-fps.bench.ts` (`pnpm --filter @indus/tui bench`, own vitest config, real
  timers): the real `App` rendered by Ink (`maxFps: 60` as in `entry.tsx`) through our output
  gate into a fake terminal. (1) Replays the recorded `long-reply` fixture (2,000 deltas, ~10,000
  tokens) at its recorded pace and counts frames (DEC 2026 frame starts) while it streams:
  fps ≥ `tui.stream_fps.min_fps` (30). (2) A drag-resize of 100 events (bursts of ten every 10 ms)
  paints ≤ `tui.resize_storm.max_frames` (10) frames.
- `apps/desktop/e2e/startup-budget.spec.ts` (e2e lane): three cold starts of the packaged Linux
  build against the fake backend, spawn → connected window with an enabled composer; the median
  must be ≤ `desktop.cold_start.limit_ms` (3,000 ms). Skips with the reason when there is no
  packaged build (CI's desktop-package job makes it) or no Electron/display.

Files: `apps/web/scripts/check-bundle.mjs`, `apps/web/scripts/check-bundle.test.mjs`,
`apps/tui/bench/stream-fps.bench.ts`, `apps/tui/bench/vitest.config.ts`, `apps/tui/package.json`
(`bench` script), `apps/tui/tsconfig.json` (`bench` included), `apps/desktop/e2e/startup-budget.spec.ts`,
`apps/desktop/e2e/vitest.config.ts` (includes the spec), `tooling/perf/read-budgets.mjs` + `.d.mts`.

Measured here (wave-41 container, Node 22): web build initial JS 118.5 kB gzip (4 files), largest
other chunk 121.0 kB gzip; TUI stream 36.5–37.6 fps over three runs; resize storm 5–6 frames;
desktop not measured (no Electron binary or packaged build in this container).

Tests: the bundle check on synthetic builds (both budgets read from the file; entry + preloads
are initial under a base path; a build within budget passes; an oversized route chunk fails and
is named; initial JS over budget fails though every file is small; no build is a setup problem).

## Decisions for the lead
- The TUI bench is a vitest file run by its own config (not `vitest bench`): bench mode cannot
  assert, and the check must fail the lane.
- Desktop cold start is a vitest e2e spec like the rest of the P20 lane (no Playwright test
  runner there); "interactive" = connection badge open + an enabled composer textarea.
- CI wiring (left to the lead, `ci.yml` is shared): after the web build,
  `node apps/web/scripts/check-bundle.mjs`; in the TUI job, `pnpm --filter @indus/tui bench`.

## Status

Done (2026-10-21); desktop budget unmeasured here.
