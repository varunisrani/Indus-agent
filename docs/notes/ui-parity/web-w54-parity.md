# Task: Web parity fixes from the wave-53 side-by-side (W54-W1) Phase: ui-parity Owner: builder w54w1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `web/src/pages/AnalyticsPage.tsx`
(the daily token chart, to see how it lays out its bars), `web/src/pages/SessionsPage.tsx` (header
and filter-row buttons: which are outlined, which set capitals), `web/src/pages/ProfilesPage.tsx`,
`CronPage.tsx`, `PairingPage.tsx`, `McpPage.tsx` (call sites that add `uppercase` to a button) — read
and compared only, nothing copied.
Evidence: the owner's captures of the running reference web dashboard and of ours at the landing
tree (wave-53 compare kit), slugs `sessions`, `session-detail`, `sessions-search`, `sessions-prune`,
`empty-state`, `chat-exchange`, `analytics`, `cron`, `cron-create`, `mcp`, `channels`, `webhooks`,
`pairing`, `profiles`, `system-console`, `error-state`, `theme-menu` (light and dark), and the gap
list written from them. None are in the repo.

What it does (own words): closes the web lines of the wave-53 gap list. Buttons go back to mixed case
with the wide typewriter tracking; the page actions the reference sets in capitals keep them. The
daily token chart draws one slot per calendar day of the period, so one busy day is one bar instead
of a block filling the chart. Sessions opens on the history list (Overview is one click away). The
search field draws its focus on its frame only, so the clear cross sits inside it. Dialogs dim the
page with a neutral grey veil in both themes. A failed cron load is a banner at the top of the page
that says what failed, with the server's words as details. The admin console's light theme no longer
shows a black strip under the terminal. "Import sessions" works: it lists the other coding tools'
conversations the server finds and imports one. "Clear pending" on Pairing is always a live button.

Inputs → outputs: `/api/analytics/usage` (days with a session only) → a chart over every day of the
period; `/api/session-import/{candidates,import}` (existing routes, P24-28) → the import dialog;
`/api/pairing/clear-pending` unchanged. Nothing new on the server.

Files: `apps/web/src/ui/controls.tsx` (Button `caps` default, ErrorState fill), `index.css`
(overlay token, focus ring in the base layer), `pages/AnalyticsPage.tsx` (+ test),
`pages/SessionsPage.tsx` (+ test), `pages/sessions/ImportDialog.tsx` (new), `lib/api.ts`,
`lib/restTypes.ts`, `testing/mockApi.ts`, `pages/CronPage.tsx`, `pages/PairingPage.tsx` (+ test),
`pages/ProfilesPage.tsx`, `pages/cron/JobModal.tsx`, `pages/mcp/AddServerModal.tsx`,
`console/ConsoleModal.tsx`, `App.test.tsx` (axe test waits for the history list),
`e2e/dashboard.spec.ts`, this note and
`web-foundation.md` (the reverted D-16.1208 line and the overlay row).

Edge cases / failure modes:
- Chart: no ISO day in the answer (all zero) → the period still ends today; a server whose day is
  already tomorrow → the period ends on its last day; a listed day before the period (zone skew) →
  the period stretches back to it, so no listed day is dropped; a tiny day keeps a 1 px bar.
- Import: nothing found → one line naming the tools looked for; a file imported before → "Already
  imported" on its row (409 `already_imported`); any other failure → a notice in the dialog.
- Clear pending with nobody waiting → a toast says so; no request is sent.

Our design differences / decisions:
- W54-W1-1: buttons are mixed case by default (reverts the W53 all-caps default, D-16.1208). The
  reference screenshots decide it: Sessions' "Prune old sessions" / "Import sessions", every dialog's
  "Cancel" / "Prune", and the error banner's "Retry" are mixed case with wide tracking; the header
  actions "CREATE", "ADD SERVER", "RESTART GATEWAY", "NEW SUBSCRIPTION", "CLEAR PENDING", "BUILD" are
  capitals. Those already carried `className="uppercase"` from wave 52 except Profiles, which now
  passes `caps`.
- W54-W1-2: Sessions opens on History; the view switch reads History | Overview.
- W54-W1-3: the daily chart fills the period with zero days (`chartDays`), bars at 80 % of the series
  colour (full on hover), a base rule under the bars.
- W54-W1-4: `--indus-overlay` is black at 60 % in both themes (measured from the reference captures:
  the light page goes from (232,242,253) to (93,97,101)). The reference's form panels (New cron job,
  Add MCP server) instead lay a pale veil of the page colour (85 %) over the page, as do its model
  picker and console: those four pass `overlayClassName="bg-canvas/85"`.
- W54-W1-5: the global `:focus-visible` outline moves into `@layer base`; unlayered, it beat every
  `outline-none` utility, which drew a second box inside the search field and left its clear cross
  looking outside.
- W54-W1-6: Cron's failed load is a top-of-page ErrorState ("Could not load the cron jobs…",
  details = the server's message); ErrorState loses its pink fill (border only, as the reference).
- W54-W1-7: the console's screen element takes the terminal colour, so the fit's leftover space
  under the last row is not black on light.
- W54-W1-8: "Clear pending" stays enabled; with nobody waiting it says so in a toast.
- W54-W1-9: "Import sessions" opens a dialog over the existing session-import routes.
- Not taken: the chat-exchange tool row (it is drawn by the TUI in `apps/tui`, outside this slice);
  theme presets 4 → 8 (not cheap: each preset needs its own checked contrast); Webhooks "New route"
  stays disabled while the receiver is off (the reference's "New subscription" is disabled there too).

Tests we will write:
- Analytics: one listed day in a 30-day period draws 30 slots with one bar; gap filling, server day
  past ours, month end and leap day (`chartDays`).
- Sessions: opens on History, Overview reachable; import lists, imports, refuses a second import,
  and says when nothing is found.
- Pairing: Clear pending stays live after clearing and sends nothing more.

## Decisions for the lead
- D-16.1208 is reverted by W54-W1-1; the lead gives it the next D-id in §3.2.

Compare checklist (filled after the rewrite; reopened the reference captures and the files above):
- [x] analytics chart (test: `AnalyticsPage.test.tsx::draws one slot per day of the period…`)
- [x] sessions land on the history list (test: `SessionsPage.test.tsx::opens on History…`)
- [x] import sessions live (test: `SessionsPage.test.tsx::import`)
- [x] button case — decided from the screenshots, see W54-W1-1
- [x] search field focus and clear cross; dialog scrim; error banner; console strip — re-shot
- [x] Clear pending live (test: `PairingPage.test.tsx::clears pending requests after confirming`)
- [x] session-detail: our transcript already folds tool calls into a row and prints the tool output
  block like the reference; the wave-53 capture expanded a session without tools. Re-shot on the
  session with a terminal call.
- Deliberate differences: see W54-W1-4 (picker and console veil), Webhooks "New route" gating.
- Not taken: chat-exchange tool row (apps/tui), theme presets.

## Status

Done (2026-10-25), uncommitted in the w54w1 worktree. Web only; nothing in `packages/ui`.
