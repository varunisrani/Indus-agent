# Task: Web Analytics, Models, Logs (W52-b) Phase: ui-parity Owner: builder w52b

## Status

Done (2026-10-24). Analytics, Models (with the Switch model and Auxiliary tasks dialogs) and Logs are
rebuilt on the foundation; re-captured in Clear Blue (light) and Deep Teal (dark) and set beside the
reference in a side-by-side PDF (builder's scratch, not in the repo). Shared pieces added to the
foundation's files: `CardHeader` `subtitle`/`below`, `PeriodButtons`, `RefreshButton`, `PageSpinner`
(`ui/controls.tsx`), `ui/DataTable.tsx`, `ui/ModelPickerDialog.tsx`, `lib/useTokenAnalyticsFlag.ts`.

Read: reference screenshots (Evidence, not in the repo) `analytics--{light,dark}`, `models--{light,dark}`,
`logs--{light,dark}`, `model-picker--dark` (dialog frame only); the wave-52 spec for this slice; the
foundation note `web-foundation.md`. Reference source was not opened for this rewrite (the spec's
earlier Read covered `web/src/pages/{Analytics,Models,Logs}Page` for structure and states only).
Ours: `apps/web/src/pages/{Analytics,Models,Logs}Page.tsx` and tests, `lib/{api,restTypes,format}.ts`,
`ui/*`, `shell/PageHeader.tsx`, `App.tsx`, `e2e/dashboard.spec.ts`.

What it does (own words): three dashboard pages get the reference look. Analytics shows token
totals as a dotted-leader list, a stacked daily bar chart and a sortable daily table; Models shows
a settings card (main model, side tasks) with dialogs, a usage figure list and a card per provider;
Logs shows a filter summary in the header, an auto-refresh switch, a wrapping row of joined filter
strips and a tall log card that opens scrolled to the newest line.

Inputs → outputs: `GET /api/analytics/usage?days=` → stat list, chart, table; `GET /api/model/
{info,options,auxiliary}` + `POST /api/model/set` → settings rows, dialogs, provider cards;
`GET /api/logs` → log card. The `dashboard.show_token_analytics` flag (config) gates token figures.

Edge cases: flag off (explanation card on Analytics, sessions-only list on Models); no usage in range
(empty card); failed query (error state with Retry); no providers (empty card); unknown `?file=`
(falls back to agent); a day string that is not `YYYY-MM-DD` is shown as given.

## Decisions

- W52-b-1: the fifth Analytics stat row is "Est. cost" (we record no API-call count).
- W52-b-2: no per-model or top-skills tables on Analytics (no data; the reference hides them when
  empty); the separate cost chart is dropped, cost shows in the stat list and the bar tooltip.
- W52-b-3: the flag check moves into `lib/useTokenAnalyticsFlag.ts`, used by App, Analytics, Models.
  Analytics with the flag off shows our explanation and fetches no usage.
- W52-b-4: Logs auto-refresh defaults OFF (lead decision); the polling test turns it on first.
- W52-b-5: the Switch model dialog is a shared `ui/ModelPickerDialog.tsx` (slice a's chat picker
  had not landed): provider list on the left, a model id field on the right (our API cannot list a
  provider's models). The apply button is "Switch" as in the reference; the Models test follows.
- W52-b-6: Models row 2 shows our providers as cards in the reference's model-card frame (no
  per-model usage exists); the Mixture-of-agents row is omitted (no such feature).
- W52-b-7: side tasks dialog: per-row Change opens today's Provider/Model editor; "Reset all to
  auto" confirms, then sets only the overridden tasks back to auto. No "Follow" button (no inherit
  data).

## Compare checklist (L = light, D = dark)

**Common**
- [x] L [x] D Page title in the display face at x≈263; the controls sit right after the title (not in the body).
- [x] L [x] D Cards are square with 1px borders in the right colour, no shadow, and the page fill. The gaps between
  cards are 22–24px.
- [x] L [x] D Period buttons are 28px tall, square, the active one filled (cream / blue), the others outlined.
- [x] L [x] D The refresh icon is ghost and muted, and turns into a spinner while loading.

**Analytics**
- [x] L [x] D Row 1 has two equal columns, each 289px tall: the stat list on the left, "Daily token usage" on the right.
- [x] L [x] D Stat list: five rows at a 37px pitch, the value left in the display face, the label right in letter-spaced
  uppercase muted text, with a dotted leader between them. The fifth row is Est. cost (decision noted).
- [x] L [x] D The chart is 160px tall and fills the width. Input sits on top and output on the bottom, in the series
  colours at 70%. The legend has square swatches. The axis shows the first, middle and last dates in tertiary.
- [x] L [x] D The tooltip on bar hover shows the date, Input, Output and Total.
- [x] L [x] D Daily breakdown table: icon and title header, sortable columns with arrows, the series colours in the Input
  and Output columns, faint row dividers and the hover tint.
- [x] L [x] D The empty, error, loading and flag-off states each render as in §1.2.
- [x] L [x] D No per-model, skills or cost chart blocks are drawn (decision noted).

**Models**
- [x] L [x] D The "Model settings" card has the sliders icon, the uppercase title and the muted subtitle, and two tinted
  setting rows inset 16px with 12px between them.
- [x] L [x] D Main model row: the star in primary, the mono value with `current-model`, and a filled CHANGE button.
- [x] L [x] D Auxiliary row: the summary count and an outlined CONFIGURE button. The dialog shows the task rows,
  CHANGE opens the inline editor, and RESET ALL TO AUTO has a confirm.
- [x] L [x] D Switch model dialog: the reference frame (title, current line, the provider list on the left, the model id
  input on the right, then Cancel and Save).
- [x] L [x] D Stat card on the right: dotted-leader rows. With the flag off, it shows Total sessions and the note.
- [x] L [x] D Provider cards in a 3/2/1 grid with a 16px gap: mono uppercase title, id badge, state at top right, SIGN IN
  outlined, description, faint footer line. The main provider has the ring.
- [x] L [x] D The device sign-in flow works inside a card (code, link, status, Cancel/Close).

**Logs**
- [x] L [x] D The header badge "AGENT · ALL · ALL" updates with the filters, with the refresh icon after it.
- [x] L [x] D The end slot has "AUTO-REFRESH" with the square switch, and "Live" while it is on.
- [x] L [x] D The toolbar is horizontal and wraps: label left, joined square segments 28px tall, the active segment
  filled. LINES is on the second row at 1440 wide.
- [x] L [x] D The log card header shows the file icon and "<FILE>.LOG" in the display face. The body is mono 12px with
  about 19–20px lines, colours by level and the hover tint.
- [x] L [x] D The view opens scrolled to the newest line at the bottom, at least 400px tall and up to the viewport
  minus 220.
- [x] L [x] D The error strip and the empty line render correctly.
- [x] Unit tests (Analytics, Models, Logs, App), the e2e Logs test, and `tooling.checks --require-ref` all pass.

Deliberate differences (Compare): Analytics' fifth row is Est. cost and there is no per-model table
(W52-b-1/2); Models has two settings rows (no Mixture of agents) and provider cards where the reference
has per-model usage cards with "Use as" and a token bar (W52-b-6), and no "Models used" figure; the
Switch model dialog has a model id field instead of a model list and its "current" line is serif,
not mono (the foundation dialog's description voice); the stack's main provider ("custom") is not in
the provider catalogue, so no card carries the main ring in the shots (unit test covers the pill);
the log card head is the foundation's 54 px CardHeader (reference about 42 px) and log lines are on a
20 px pitch (reference about 19 px). Empty, error, loading and flag-off states are covered by unit
tests rather than shots.

## Follow-ups (backend; outside this slice)

- `/api/analytics/usage`: `api_calls` and `by_model[]` (API calls row, per-model table, "Models used",
  per-model usage cards with "Use as").
- A per-provider model listing (a real model list in the Switch model dialog).
- Effective/inherited routes for side tasks ("Follow <task>", effective-route text).
- Top skills analytics, if skills usage is ever recorded.

## Integration (wave 53)

W52-b-1, -2, -6 are D-16.1195, W52-b-3, -4, -5, -7 are D-16.1196. The Switch model dialog is slice a's look on this slice's `onApply` contract (D-16.1205); `ModelsPage.test.tsx` reads the CURRENT model row and the filter "Filter providers and models". `PageSpinner` is slice d's version (hidden label); `CardHeader` keeps `subtitle` and `below` (D-16.1206).
