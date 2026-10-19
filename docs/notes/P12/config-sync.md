# Task: Config sync (P12-26) Phase: 12 Owner: wave-8 builder R4

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `ui-tui/src/app/useConfigSync.ts`
(whole: keys, normalisers, poll, MCP revision handshake), test names of
`ui-tui/src/__tests__/useConfigSync.test.ts`. Research `05-tui.md` R05.18. Ours:
`docs/notes/P12/bridge-methods.md` (`config.get full|mtime`, `reload.mcp`),
`src/indus/tui_bridge/methods_config.py` `_client_settings` (keys sent). Evidence: none.

What it does (own words): once the bridge is ready and a session exists, the renderer reads every
display setting with one `config.get {key: full}` and applies it to the UI store: busy input mode
(a missing or unknown value means `queue` in the TUI, R05.18), status bar (on/off/top/bottom and the
field list), busy indicator, details mode and sections, reasoning visibility, streaming, timestamps,
inline diffs, bells, mouse preset and paste thresholds. Then every 5 s it reads `config.get {key:
mtime}`; when the stamp changes it reads `full` again and re-applies. When the MCP revision in the
settings changes it asks the bridge to reload MCP servers (`reload.mcp`). Errors are quiet: a failed
call keeps the settings already applied and the next poll tries again.

Inputs → outputs: `config.get` results → `uiStore.patch(...)` + extras (`bellOnPrompt`,
`bellOnComplete`, `statusBarFields`, `detailSections`, `mcpRevision`); MCP revision change →
`reload.mcp {session_id}`.

Files: `apps/tui/src/config/configSync.ts`, `apps/tui/src/config/configSync.test.ts`.

Edge cases / failure modes:
- first mtime unknown (null) → baseline set on the first non-null stamp, no reload.
- poll fails → nothing changes; a later success with a new stamp re-applies.
- `full` fails → old settings kept (no snap back to defaults).
- polls never overlap (the next one is armed after the previous settles), timers come from an
  injected clock, `stop()` cancels.
- results for a session that is no longer current are dropped (destination gate).
- a reload that fails leaves the revision unaccepted so the next poll retries it.

Our design differences / decisions:
- our bridge sends flat keys (`busy`, `show_reasoning`, `indicator`, `details`, `statusbar`, `mouse`,
  `verbose`, `theme`); the normalisers also accept the long names (`busy_input_mode`, ...) so a
  later bridge can add keys without a client change.
- the MCP revision is read from `mcp_revision` in the `full` payload; absent means "never reload".

Tests we will write: fake clock: mtime change at 5 s re-applies; failing poll keeps settings then
heals; missing busy key → queue; normalisers table; MCP revision change reloads once.

## Decisions for the lead
- The bridge's `full` payload sends `busy` filled with the schema default (`interrupt`), so the TUI
  cannot tell "unset" from "set to interrupt". Bridge follow-up: send null when the key is not in
  the file (then R05.18's queue default applies).
- Bridge follow-up: add `bell_on_prompt`, `mcp_revision`, paste thresholds to `full`.

Compare checklist (filled after the rewrite; reopened `useConfigSync.ts`):
- [x] one `full` read on start, memoised (test: `apps/tui/src/config/configSync.test.ts::reads full once on start, even if started twice`)
- [x] 5 s poll re-applies on a stamp change (test: `configSync.test.ts::re-applies when the stamp changes at the 5 s poll`)
- [x] failing poll keeps settings and heals; a failed `full` does not advance the stamp (test:
  `configSync.test.ts::a failing poll keeps the old settings, then heals`)
- [x] missing busy key → queue (test: `configSync.test.ts::a missing busy key applies queue`, `::normalisers`)
- [x] MCP revision change reloads once; failure retried on later polls (test:
  `configSync.test.ts::an MCP revision change reloads once; a failed reload is retried`)
- [x] late `full` after a session switch dropped (test: `configSync.test.ts::drops a full read that lands after the session changed`)
- Deliberate differences: mouse absent → off (our schema default is off; the reference treats absent
  as all). Polls are chained on the injected clock instead of a fixed interval, so they never overlap.
- Not taken: language sync, voice record key, battery, compact and focus views, destructive-confirm
  setting (not in our settings or owned elsewhere).

## Status

Done (2026-10-19). P12-27 starts it after `gateway.ready` and hands `extras.bellOnPrompt` to the
server-request cards.
