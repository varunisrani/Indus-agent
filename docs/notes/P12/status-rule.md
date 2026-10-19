# Task: Status rule (P12-15) Phase: 12 Owner: wave-8 builder R2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `ui-tui/src/components/appChrome.tsx`
(`statusBarSegments`, `statusRuleWidths`, `StatusRule`, busy indicator width rules). Research
`05-tui.md` §3.7, §3.30. At Compare only: `ui-tui/src/app/useConfigSync.ts` (`normalizeStatusBarFields`). Evidence: none.

What it does (own words): a one-line rule above (or below) the input. Left: the busy indicator or
the idle status text, the model, the context read-out. More segments appear as the terminal gets
wider: below 72 cols the context is a compact token count; from 72 a context bar with percent;
duration from 76; compression count from 80; voice from 84 (hidden until voice exists); running
background tasks from 88; subagents from 92; cache-hit from 96; latency from 104; tokens/s from
110. Model and context always get room; lower-priority segments are dropped whole (never cut
mid-segment) when they do not fit what is left. The right edge shows the session title badge if
there is one, else the cwd label with the git branch; it yields first on a narrow terminal. A
YOLO warning badge shows when approvals are off. `display.status_bar.fields` hides named segments.
Position top / bottom / off. Busy indicator styles each use frames of equal display width so the
rule does not jitter (our own glyph sets).

Inputs → outputs: `StatusInput` (cols, busy, status text, model, usage numbers, duration, counts,
title, cwd, branch, yolo, field allow-list, indicator style, frame) → ordered segment list + right
label → `<StatusRule>` one Ink row.

Files: `apps/tui/src/components/StatusRule.tsx`, `apps/tui/src/components/statusSegments.ts`,
`apps/tui/src/components/statusSegments.test.ts`.

Edge cases / failure modes:
- Unknown context window → token total only; zero usage → no context segment.
- Width below the essentials → right label dropped first, then tail segments.
- Unknown field names are ignored; a missing or empty field list shows everything.
- Indicator style unknown → default style.

Our design differences / decisions:
- Our own indicator styles: `dots` (default), `bar`, `arc`, `ascii` with our own frames, plus a
  fixed-width verb list of our own words; equal widths checked by a test.
- Our own segment glyphs; no battery, focus badge, credits or spawn HUD (later / vendor).

Tests we will write:
- Table at 60, 72, 80, 96, 104, 110, 140 cols listing exactly the expected segment ids.
- Field allow-list respected; title beats cwd + branch; YOLO badge; segments dropped whole.
- Every indicator style has equal-width frames.

## Decisions for the lead
- Usage keys read from `uiStore.usage` (`UsageNumbers` from the protocol plus optional
  `compressions`, `cache_hit_pct`, `avg_latency_s`, `avg_tps`, `bg_tasks`, `subagents`); when the
  bridge does not send them the segment self-hides.
- `display.status_bar.fields` has no uiStore field in the contract; `StatusRule` takes `fields` as a
  prop (normalised by `readStatusFields`) until R1/R4 add it.

Compare checklist (filled after the rewrite; reopened `appChrome.tsx` and, for the field filter,
`app/useConfigSync.ts` `normalizeStatusBarFields`):
- [x] breakpoints 72 / 76 / 80 / 84 / 88 / 92 / 96 / 104 / 110 (tests:
  `statusSegments.test.ts::breakpoints > …`)
- [x] whole-segment dropping after the essentials and the right label (test: `drops tail segments
  whole …`); essentials kept at 20 cols (test: `keeps model and context …`)
- [x] title badge beats folder + branch; title field respected (test: `shows the title badge …`)
- [x] equal-width indicator frames (test: `busy indicator > uses equal-width frames …`)
- [x] gap found: `display.status_bar.fields` is an allow-list (null / empty = show everything), not
  a hide-list → fixed (`fields`, `readStatusFields`), pinned by `shows only the segments listed …`
- Deliberate differences: the context percent is an essential (the row: "model and context %
  always fit"), the bar itself is the tail segment; our own glyphs and short segment words; the
  indicator does not carry the elapsed time (the duration segment does).
- Not taken: battery, focus badge, credits notice / dev credits, live session count, idle clock,
  parked-subagent resume hint, spawn HUD, "estimated" context mark.

## Status

Done (2026-10-19). `components/StatusRule.tsx`, `components/statusSegments.ts`; 20 tests in
`statusSegments.test.ts`.
