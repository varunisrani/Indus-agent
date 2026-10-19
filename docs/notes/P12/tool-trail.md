# Task: Tool trail, reasoning and todo panel (P12-14) Phase: 12 Owner: wave-8 builder R2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `ui-tui/src/components/thinking.tsx`
(`ToolTrail`, `Thinking`, chevrons, floating alert backstop), `ui-tui/src/domain/details.ts`
(section modes and defaults), `ui-tui/src/components/todoPanel.tsx`. Research `05-tui.md` §3.14,
§3.15, §3.30. Evidence: none.

What it does (own words): while a turn runs, every running tool shows one row: a spinner, a verb
for that tool ("reading", "running"…) from our own table, a short argument preview and the elapsed
time. When the tool finishes the row collapses into a trail line: a done / failed mark, the label
and the duration (under 10 s one decimal, under 60 s whole seconds, else `Xm Ys`). A
`tool.generating` notice adds "drafting <name>…". With inline diffs on, a diff becomes its own
segment. Verbose output under a trail line is cut client-side to 800 chars / 12 lines. Three
sections plus activity are each hidden / collapsed / expanded: a global mode plus per-section
overrides; out of the box thinking and tools are expanded, subagents follow the global mode, and
activity is hidden. `/details` cycles or sets the global mode; `/details <section> <mode|reset>`
sets one section. When every section is hidden, warnings and errors still show as a floating
alert. The todo panel lists `{ id, content, status, parent? }` items with a done count, nested by
parent, collapsible. Subagents show as indented tool rows.

Inputs → outputs: turnStore (`activeTools`, `pendingTools`, `trail`, `activity`, `reasoning`,
`todos`) + uiStore `detailsMode` + section overrides → styled lines / Ink rows.

Files: `apps/tui/src/components/ToolTrail.tsx`, `Reasoning.tsx`, `TodoPanel.tsx`, `details.ts`,
`ToolTrail.test.tsx`, `details.test.ts`.

Edge cases / failure modes:
- Unknown tool → fallback verb "working"; names with a `mcp__server__tool` shape use the last part.
- Duration 0, negative, NaN → `0.0s`; rounding that crosses a unit carries over (9.96 s → `10s`, 59.6 s → `1m 0s`).
- Trail longer than 8 keeps the newest 8; activity keeps 8 and shows the last 4.
- Verbose text: cut to the tail, never mid-line when a newline is near, with an omitted note.
- `/details` with a bad mode or section returns an error string, state unchanged.
- Todos: unknown status → pending; missing id → index; parent cycles / unknown parents → top level.

Our design differences / decisions:
- Our own verb table, glyphs and spinner frames (D-16.23).
- Section overrides live in a small store in `details.ts` (`detailsStore`), global mode stays in
  uiStore `detailsMode`; `/details` parsing is a pure function R4's slash dispatch calls.
- No subagent accordion or spawn tree (J18 later); no token tallies on chevrons yet.

Tests we will write:
- Duration formatting table; trail keeps 8; verbose cap 800 / 12.
- Section resolution: override wins over global; defaults; `/details` cycle, set, section set, reset,
  errors.
- All hidden → only the floating alert (errors / warnings) shows; nothing when there are none.
- Active tool row shows verb + elapsed; drafting line; todo panel counts, nesting, collapse.

## Decisions for the lead
- `detailsStore` (sections + "set by command" flag) could fold into uiStore at merge; R4's
  `/details` should call `applyDetailsCommand`.
- Rows may carry `depth` (subagent nesting) as an optional extra on `ToolRow` (`TrailRow` type); R1
  can add the field to the store type at merge.
- The live plan panel's fold state is `todoView` (vanilla store) in `TodoPanel.tsx`.

Compare checklist (filled after the rewrite; reopened `thinking.tsx` (`Thinking`, `ToolTrail`,
backstop), `domain/details.ts`, `todoPanel.tsx`):
- [x] section defaults thinking/tools expanded, activity hidden, subagents follow global; precedence
  override → command global → default → global (tests: `details.test.ts::section resolution > …`)
- [x] all-hidden backstop shows the last 2 non-info activity items, nothing when only info
  (test: `ToolTrail.test.tsx::detail sections > still surfaces warnings …`)
- [x] activity lane shows its last 4 entries (test: `lists the last activity entries …`)
- [x] live elapsed time and trail duration format (tests: `durations`, `shows running tools …`)
- [x] drafting row for tools still being written (test: `adds a drafting line …`)
- [x] todo panel: done count, nesting by parent, fold (tests: `todo panel > …`)
- [x] `/details` grammar: mode, `cycle`, `<section> <mode|cycle|reset>` (tests: `/details > …`)
- Deliberate differences: collapsed tools show one summary line with the running tool instead of a
  closed chevron; collapsed reasoning previews the latest words, not the first; no token tallies;
  our own verbs, spinner frames and glyphs.
- Not taken: subagent accordion / spawn tree and heat colours (J18), "analyzing tool output"
  marker, MoA reference panels, click / shift-click expand-all (mouse lands with P12-27),
  "N pending" note on archived plans.

## Status

Done (2026-10-19). `components/{ToolTrail,Reasoning,TodoPanel}.tsx`, `components/details.ts`,
`components/toolVerbs.ts`; 39 tests (`ToolTrail.test.tsx` 29, `details.test.ts` 10). R4's
`/details` calls `applyDetailsCommand`; P12-26 config sync feeds `readOverrides` into
`detailsStore`.
