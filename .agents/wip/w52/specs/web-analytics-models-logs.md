# W52 spec: web Analytics / Models / Logs (slice web-analytics-models-logs)

Status: spec ready for the wave-52 page builder (Read + Note done 2026-10-24; no code written).
Read: reference screenshots `$SP/compare/hermes/web/{analytics,models,logs}--{light,dark}.png` (+ `model-picker--dark.png`
for the dialog style only); reference source read for structure/states only (web/src/pages AnalyticsPage, ModelsPage,
LogsPage). Ours: `$SP/compare/indus/web/{analytics,models,models-side-tasks,logs}--*.png`, `apps/web/src/pages/{Analytics,
Models,Logs}Page.tsx` (+ tests), `lib/api.ts`, `lib/restTypes.ts`, `lib/format.ts`, `ui/controls.tsx`, `ui/Switch.tsx`,
`shell/PageHeader.tsx`, `App.tsx`, `e2e/dashboard.spec.ts`, backend `src/indus/web/routes/{analytics,models,logs}.py`.
Clean room: everything below is described in our words. No reference code, class names, CSS or long copy. Short labels
(1–4 words) may match.

All measurements come from the 1440x900 shots. `$SP` = `/tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad`.
Light = the reference "blue" preset, dark = the reference "teal" default. Hex values are sampled pixels. Use the wave-51
foundation tokens. The hex values are there to check that the tokens resolve correctly, so do not hard-code them.

---------------------------------------------------------------------------------------------------------------------
## 0. Shared frame (owned by the w51a foundation; listed here only to check alignment)

| Thing | Measured |
|---|---|
| Sidebar | x 0–239, 1px right border at x=239 (dark `#36443f`, light `#bad3fd`) |
| Page header | full width right of the sidebar, height 52, 1px bottom border at y=52 (same colours as the sidebar border). Same background as the page. |
| Page title | x=263, display face (wide geometric), title case, about 17–18px, bold. Glyph box y 19–32. |
| "After title" slot | controls sit right after the title, starting about 12px after it, vertically centred. **All three pages use this slot.** |
| "End" slot | right-aligned, 22px from the right edge (Logs only) |
| Content area | padding 24px left/right (content x 263–1407), first card top at y=75 (22px under the header border). Vertical gap between stacked cards is 22–23px (use the 24px spacing token). The page background is the page token for both the area and the cards. |
| Page bg | dark `#041c1c`, light `#e8f2fd` |
| Foreground / primary | dark `#ffe6cb` (cream), light `#0053fd` (blue). In light, primary and foreground are the same blue. |
| Card | transparent fill (same as the page), 1px square border (dark `#293935`, light `#c5dafd`), **no radius, no shadow** |
| Card header divider | 1px, same colour as the card border |
| Faint row divider (border at 50%) | dark `#162a28`, light `#d6e6fd` |
| Muted text | dark `#9a9585`–`#ccbda7`, light `#5d92fd`–`#2f73fd` |
| Tertiary text | dark `#a79f8d` at small sizes (thin glyphs read darker), light `#518bfd` |
| Series colours (charts) | input: dark `#ffe6cb`, light `#001934`. Output: dark `#34d399`, light `#0053fd`. Bars are drawn at 70% over the page, giving input bar dark `#b4a996` / light `#455a71` and output bar dark `#269c73` / light `#4582fe`. |

**Header after-title slot.** Today `PageHeader` has only an end slot (`HeaderActions`). If the foundation does not add an
after-title slot, add one the same way: a portal target placed right after the `<h1>`, with the end slot kept on the far
right and the `<h1>` no longer set to `flex-1`. Use it on all three pages. Other w52 specs name the same slot
differently: `HeaderTitleAddon` in web-sessions-chat and `HeaderAfterTitle` in web-cron-skills-plugins-mcp. Use the one
that lands first and do not add a second. Coordinate with w51a and the other w52 web builders.

**Font roles** (the foundation maps them to open fonts):
- **display**: wide and geometric. Used for the page title, card titles (uppercase) and stat values.
- **label**: letter-spaced uppercase, mono-like. Used for stat labels and row labels.
- **serif-small**: the small serif or pixel-serif face. Used for table cells, segment labels, legends and axis dates.
- **mono**: model ids and log lines.

Use whatever role names the foundation note (`docs/notes/ui-parity/web-foundation.md`) lists.

**Shared building blocks used by this slice.** Use them from the foundation if they exist. Otherwise build them in
`apps/web/src/ui/` and tell the other w52 builders.
1. **Period buttons**: three small square buttons, height 28 with a 1px border, 5–6px apart. Text is about 11px, bold
   mono-like, letter-spaced. The active button is filled: dark has a cream fill with page-coloured text; light has a blue
   `#0053fd` fill with text `#e8f2fd`. Inactive buttons are outlined, with the border and text in foreground and a
   transparent fill. The labels are lowercase "7d 30d 90d" on Analytics and uppercase "7D 30D 90D" on Models (the
   reference really does differ; matching it is optional).
2. **Icon refresh button**: ghost, a 16px circular-arrows icon in muted colour that turns foreground on hover. While
   loading, it shows a spinner and is disabled. Accessible name "Refresh".
3. **Stat list** (the "dotted leader" list): one row per stat with a 37px row pitch. The value sits on the left in the
   display face, about 13px, in foreground. The label sits on the right, uppercase, about 15px, letter-spaced about
   0.2em, in muted colour (dark `#9a9585`, light `#5d92fd`). Between them, a 1px dotted leader fills the space (dark
   `#273834`, light `#c9dcfd`), vertically centred, with about 12px gap at each end. Inside a card the padding is 24px
   top/bottom and 16px left/right.
4. **Card with icon title**: the header has 16px padding and is about 54px tall, with a divider under it. Inside it: an
   18–20px line icon in muted colour, an 8px gap, then the title in uppercase display face, about 17–18px, bold, in
   foreground (dark `#ffe6cb`, light `#0053fd`). Optional pieces: a subtitle right after the title (about 12px, muted,
   normal case), and a legend row under the title.
5. **Sortable table**: full width, serif-small face. The header row text is about 12px in muted colour. Each header cell
   holds the label, a 6px gap, then an icon. The active column shows a single arrow (down or up) in 80% foreground; the
   other columns show a double up/down arrow, 12px, in tertiary. Clicking a header toggles the direction; clicking a new
   column sorts it descending. Null values always sort last. The header row has a 1px border under it. Body rows are
   about 35px tall with a faint 1px divider and a hover tint (secondary colour at 20%). Cells are about 13–14px. The
   first column is left-aligned and the others are right-aligned.

---------------------------------------------------------------------------------------------------------------------
## 1. Analytics (`/analytics`)

### 1.1 Reference layout (1440x900)
- **Header after-title**: period buttons 7d / 30d / 90d at x 368–406, 411–458 and 463–508 (y 12–39), with 30d active,
  then the refresh icon at about x 522–535. There is no "Range" label, no badge, and nothing in the end slot.
- **Row 1** is a two-column grid with a 23px gap, top at y=75, both cards 289px tall (y 75–363):
  - **Left card (x 263–823)**: a stat list with five rows, centred at y 106, 143, 180, 217 and 254:
    1. Total tokens, e.g. "3.7K"
    2. Input, e.g. "3.4K"
    3. Output, e.g. "316"
    4. Total sessions, shown as "3 (~0.1/day avg)": the count, then the sessions per day over the selected range,
       uppercase as rendered
    5. API calls, e.g. "4"
    The rest of the card is empty space down to the shared row height.
  - **Right card (x 846–1407)**, titled "Daily token usage":
    - Bar-chart icon and title. A legend row under the title (y about 126): a 10px square swatch in the input series
      colour with the text "Input", then a swatch in the output series colour with "Output". The legend text is
      serif-small, about 11px, muted.
    - Header divider at y=149.
    - Body padding 16px. The chart is 160px tall (y 165–325). Each day is one flex column with a 2px gap between
      columns. Each column is a stacked bar with input at the top and output at the bottom; the bars are not rounded.
      A column has full width when there is only one day.
    - Axis labels under the chart, 8px below it: the first, middle and last day as "Oct 24", serif-small about 11px,
      tertiary, spread with space-between.
    - Hovering a bar shows a tooltip above it: a small card-coloured box with a border and a shadow, about 12px text,
      listing the date (medium weight), Input, Output and Total.
- **Daily breakdown card** (x 263–1407, y 386–540): trend-line icon and title. Header divider at y=440.
  - Sortable table, default sort day descending. Columns: Date (left), Sessions, Input, Output (right).
  - Cell colours: Sessions value muted, Input value in the input series colour, Output value in the output series
    colour.
  - Header row bottom border at y=489, first body row bottom border at y=524.
- **Per-model breakdown card** (y 564–718): chip/CPU icon and title.
  - Columns: Model (left, mono 12px, e.g. "demo/fake-model-1"), Sessions (muted), Tokens.
  - Tokens shows "input / output" in the two series colours, e.g. "3.4K / 316". Default sort is input tokens
    descending.
- A **Top skills** table also exists in the reference (columns skill, loads, edits, total, last used). It is hidden when
  there is no data and is not in the shot.
- Numbers use a compact format: at 1,000 or more, one decimal plus "K"; at 1,000,000 or more, one decimal plus "M";
  below 1,000, the raw number.

### 1.2 Reference states
| State | What it shows |
|---|---|
| Loading with no data yet | a centred spinner in primary colour with about 96px vertical padding; the refresh icon also spins |
| Error | a card holding one centred line of destructive text, about 14px |
| Empty (no day, no model, no skill) | a card with 48px vertical padding and, centred: the bar-chart icon at 32px and 40% opacity, a 14px medium line ("No usage data" style) and a 12px tertiary hint to start a session |
| Flag off (`dashboard.show_token_analytics` is not true) | a single explanatory card (48px padding, max width about 672px, muted 14px text, a display-face heading) saying the local token figures are a debug estimate, then how to turn the flag on in Config. The period buttons are hidden. |
| Plugin slots | top and bottom of the page |

### 1.3 Ours today (gaps)
- The "Range" label and the "7 days" style buttons sit in the page body instead of the header.
- The page has a plain `<dl>` of four totals (Input tokens, Output tokens, Cost, Sessions) with Intl formatting such as
  "10,010".
- The page has an SVG "Tokens per day" chart in our accent and success colours, centred at 640 viewBox width with empty
  sides.
- The page has a "Cost per day" chart.
- The page has no tables and no flag-off card.
- Rounded cards, the old palette and a sans face are all foundation work.

### 1.4 Data available vs missing
- `GET /api/analytics/usage?days=` returns:
  - `daily[]` with `day`, `input_tokens`, `output_tokens`, `cost`, `sessions`. It only includes days that have a
    session.
  - `totals` with `input_tokens`, `output_tokens`, `cost`, `sessions`.
- **Available**: total tokens (input + output), input, output, sessions, sessions per day (sessions / `days`), daily
  rows, and cost.
- **Missing** (do not add backend in this slice; list them as follow-ups):
  - API-call count
  - per-model rows
  - top skills
- Decisions for the builder (record them as `W52-<x>-n`):
  1. **The fifth stat row is "Est. cost"** (`formatCost(totals.cost)`) in place of API calls. It is our data and keeps
     the five-row rhythm.
  2. **No per-model card and no skills card.** The reference hides these tables when they are empty, so leaving them
     out matches what the reference shows for no data. Do not draw a placeholder.
  3. **Drop the separate "Cost per day" chart.** Cost appears in the stat list. Optionally add a Cost line to the bar
     tooltip and/or a fifth right-aligned "Cost" column in the daily table, but only if the layout stays clean.
     Default: tooltip only.
  4. **Flag-off card**: read the flag the way `App.tsx` does. Move `analyticsFlag()` and the `["shell","config",scope]`
     query into a small exported hook such as `useTokenAnalyticsFlag()` in `lib/` and use it in App, Analytics and
     Models. When the flag is off, show our own short explanation (our words, 2–3 sentences, with a link to `/config`)
     and do not fetch usage. Today the route is reachable even though the nav item is hidden.

### 1.5 Build notes
- `AnalyticsPage.tsx`:
  - Put the period buttons and the refresh button in the after-title slot.
  - Layout: a two-column grid at ≥1024px (`lg`) and one column below it. Under the grid, the Daily table card.
  - The chart should be HTML or flex divs, or SVG with `preserveAspectRatio="none"` filling the width. Keep
    `role="img"` and an aria-label on the chart container.
  - Hover tooltip: CSS `group-hover` or a state-driven box. Keep the native `<title>` too if it is easy, for screen
    readers.
- Empty and error: use the foundation's empty/error components with the reference arrangement from 1.2.
- `lib/format.ts`: add `formatTokens(n)` (compact K/M) and `formatShortDay("YYYY-MM-DD")` (local "Mon D").
  `formatCost` is already ours; keep it.

---------------------------------------------------------------------------------------------------------------------
## 2. Models (`/models`)

### 2.1 Reference layout (1440x900)

**Header after-title**: period buttons "7D 30D 90D" (x 346–384, 390–435, 442–487, y 12–39, uppercase) and the refresh
icon at about x 502–513. The period drives the usage figures on the page.

**Row 1**: a two-column grid (≥1024px) with a 23px gap; both cards are y 75–324 (250px).

**Left card, "Model settings"** (x 263–823):
- Header 46px tall (divider at y=121): a sliders icon at 16px, muted; the title "MODEL SETTINGS" in display face, about
  16px, bold, uppercase; and the subtitle "applies to new sessions" right after it, 12px, muted, normal case.
- Body: 12px top padding and 16px sides, holding three stacked setting rows 12px apart (y 133–181, 193–241, 254–301).
- Each row:
  - Box: x 279–807 (inset 16px), 1px border (dark `#192d2a`, light `#d4e4fd`), filled with muted at 20% (dark
    `#081f1e`, light `#e4effd`), padding 12px horizontal and 8px vertical, about 48px tall.
  - Left side, line 1: a 12px icon, an 8px gap, then the label in label face, uppercase, about 12px, medium weight,
    letter-spaced, in foreground.
  - Left side, line 2: mono 12px in muted colour, truncated.
  - Right side: a small button, about 24–26px tall, uppercase bold letter-spaced text about 11px.
- The three rows:
  1. **Main model**. The star icon is in primary colour. Value: "provider · model" (e.g. "custom · demo-fake-model").
     The button is **filled**: dark has a cream fill with dark text, light has a blue fill with light text. Label
     "CHANGE", x 722–795.
  2. **Auxiliary tasks**. Chip icon in tertiary. Value: "12 tasks · all auto", or "N overrides · M auto" when there are
     overrides. **Outlined** button "CONFIGURE", x 697–795.
  3. **Mixture of agents**. Brain icon. Value: a references count and the aggregator model. Outlined button
     "CONFIGURE".

**Right card** (x 846–1407): a stat list with six rows at a 37px pitch from y=106: Models used, Total tokens, Input,
Output, Est. cost, Total sessions. With the token flag off, it shows only Models used and Total sessions, plus a small
tertiary paragraph (12px, relaxed line height) saying the figures are hidden and naming the config flag.

**Row 2: model usage cards.** A grid with 1 column, 2 columns at ≥768px and 3 at ≥1280px, with a 16px gap. In the shot
there is one card, x 263–633 (371 wide), y 347–577. The main model's card gets a 1px primary ring at 40%.

Card header (y 347–431, divider at 431; padding 16px, 12px under the title line):
- Left, line 1:
  - "#1" in mono 12px tertiary
  - an 8px gap, then the short model name (the part after the vendor prefix), mono bold, about 13px, uppercase,
    letter-spaced, foreground, truncated
  - optional pill "★ main": primary at 15% fill, primary text
  - optional pill "aux · task": purple at 10% fill, purple text
- Left, line 2: a secondary badge with the provider name (about 11px serif-small, square). It has a muted fill (dark
  `#172b29`, light `#d5e6fd`) and a 1px border (dark `#3a4640`). The line may continue with "200K ctx" and "8K out" in
  12px secondary.
- Right column, right-aligned:
  - a figure (mono bold 12px, e.g. "3.7K") over "tokens" (12px tertiary)
  - under it, a "USE AS ▾" outlined small button, about 22px tall, x 541–618
  - The menu opens down-right: min width 220px, card fill, border, shadow. Its rows are uppercase 12px:
    1. Main model (star icon), with "current" on the right in primary
    2. a "Auxiliary task" section label in tertiary display face
    3. All auxiliary tasks
    4. one row per task, with "current" when it applies
  - The menu closes on an outside click. An expensive-model confirm dialog can follow.

Card body (padding 16px, 12px vertical gaps):
- Stacked token bar 24px tall (y 443–466), full width, segment widths in proportion to their values. Segments in order:
  cache read (blue), reasoning (purple), input series, output series, all at 70%. Each segment has a faint repeating
  vertical hairline texture every 6–7px at 30% opacity. The bar has square ends.
- Legend under it: 6px round dots plus "Input 3.4K", "Output 316", 12px secondary.
- A three-column centred mini-stat row: values in mono bold 12px (sessions, avg/session, API calls) over 12px tertiary
  captions.
- Footer: a 1px faint top border, 8px top padding, 12px secondary text. Left: "$ 0.0050" and "⚡ 1 tool calls", each
  with a 10px icon. Right: a relative time such as "just now" or "2m ago".
- Capability pills follow: Tools in green, Vision in blue, Reasoning in purple, and the family on a muted fill.

**Model settings dialogs** (reference):
- "Change" opens the **model picker** dialog (see `model-picker--dark.png`):
  - Box about 720px wide, centred on an 85% page-colour backdrop.
  - Header: an uppercase title, then "current: <model> · <provider>" in mono.
  - A filter input.
  - Body: the provider list on the left (about 200px; each row shows the name, then "id · N models" in mono, and the
    selected row has a left accent bar and a tint) and the model list on the right, with "CURRENT" on the current one.
  - Footer: a note on the left, then outlined "Refresh models", outlined "Cancel" and a filled "Switch".
  - After switching, it asks whether to reload.
- "Configure" on Auxiliary tasks opens a dialog:
  - About 672px wide, at most 80% of the viewport tall. A close X sits top-right.
  - Header: the title in display face with an outlined "RESET ALL TO AUTO" button, then a 12px explanatory line.
  - Body: one bordered row per task. Each row has the label (12px medium) with a tertiary hint, then mono 12px showing
    the effective route ("auto (use main model)" or "provider · model"). On the right: a "CHANGE" outlined button, and
    for tasks that inherit, a "FOLLOW <task>" button.
  - Reset asks for confirmation.
- "Configure" on Mixture of agents opens a preset editor.

**States**:
| State | What it shows |
|---|---|
| Loading | spinner, with about 96px padding |
| Error | a card with centred destructive text |
| No models | a card with a 32px chip icon at 40% opacity, a 14px medium line and a 12px tertiary hint |

The page refetches the assignments when the window regains focus.

### 2.2 Ours today
- Three stacked full-width cards:
  1. **Main model**: a text line "fake-model · custom · 128,000 token context" (`data-testid=current-model`) and an inline
     form with a Provider select, a "Model id" input and a "Save model" button.
  2. **Providers**: about 30 rows, each with label, id and description, a "configured"/"no key" badge, and "Sign in"
     with the device-code flow. A note says that keys go on the Keys or System page.
  3. **Side tasks**: rows per task with Provider and Model inputs and a Save button.
- There are no usage figures and no period buttons.

### 2.3 Data available vs missing
| Data | API | Status |
|---|---|---|
| Main model | `GET /api/model/info` (`model`, `provider`, `effective_context_length`…) and `GET /api/model/options` (`providers[]`, `current`) | available |
| Set main or side task | `POST /api/model/set` with `{scope, task?, provider, model}` | available |
| Side tasks | `GET /api/model/auxiliary` (`tasks[]`: `task`, `provider`, `model`, `base_url`, `description`; `main`) | available. There is **no** effective/inherit info. |
| Device sign-in | `api.providerLogin.*` | available. Must keep working, tests pin it. |
| Usage totals | `GET /api/analytics/usage?days=`: total tokens, input, output, cost, sessions | available. **Models used is missing.** |
| Per-model usage (model cards, Use as) | none | **missing** |
| Model listing per provider (picker right pane) | none. Only the `supports_model_listing` flag exists. | **missing** |
| Expensive-model confirm, reload prompt | none | missing |
| Mixture of agents | not a feature of ours | **missing** |

### 2.4 What to build (ours, reference look)
- **Header after-title**: period buttons (uppercase) and refresh. The period drives the right-hand stat card (usage
  query); refresh refetches info, options, auxiliary and usage.
- **Row 1, left: "Model settings" card**, built as in 2.1 with **two** rows. Leave out the Mixture of agents row: we
  have no such feature, so draw no placeholder.
  1. **Main model**:
     - Value line (mono, muted): `provider · model · <compact ctx> ctx`, e.g. `custom · fake-model · 128K ctx`. Keep
       `data-testid="current-model"` on it.
     - If nothing is set, show "(unset)".
     - **CHANGE** (filled) opens our **Switch model dialog**:
       - Use the foundation's Dialog and the reference dialog look: uppercase title, then a mono "current: model ·
         provider" line.
       - Body:
         - Left list: our providers from `/api/model/options`. Each row shows the label, then "name" in mono plus a
           configured / no key mark.
         - Right side: a filter box (it filters the provider list), the "Model id" input, and a short muted hint.
           Our API cannot list models, so the right pane is the input, not a model list.
       - Footer: outlined "Cancel" and filled "Save model". Keep that name, since a test pins it, or rename it and
         update the test.
       - Saving calls `api.model.set({scope:"main", provider, model})`, shows a toast and invalidates `["models"]`.
         These are today's flows.
       - The web-sessions-chat spec plans `apps/web/src/chat/ModelPickerDialog.tsx` (its §10). If it has landed,
         reuse it with a provider/model "apply" callback that calls `api.model.set` with scope main, instead of
         building a second dialog. Otherwise build the frame here so the chat builder can lift it.
  2. **Auxiliary tasks**:
     - Value line: `N tasks · all auto`, or `k override(s) · m auto`. A task counts as an override when `provider` is
       neither empty nor "auto", or when `model` is not empty.
     - **CONFIGURE** (outlined) opens the **Auxiliary tasks dialog**:
       - Header: the title, then our one-line explanation in our words.
       - Rows: one per task, keeping `data-testid="aux-<task>"`. Line 1: the task name (12px medium) with its
         `description` as the tertiary hint. Line 2, mono: "auto (main model)" or "provider · model".
       - Each row has an outlined **CHANGE** button that opens the inline editor for that row: our Provider and Model
         inputs (placeholder "(the main model)") and a Save button. These are today's AuxRow fields and behaviour.
       - Header button **RESET ALL TO AUTO**: a confirm dialog, then `api.model.set({scope:"auxiliary", task,
         provider:"auto", model:""})` for each task that is overridden. This only composes the existing API.
       - There is no "Follow" button, because we have no inherit data.
- **Row 1, right: stat card** (when the flag is on):
  - Rows: Total tokens, Input, Output, Est. cost, Total sessions, from `/api/analytics/usage?days=`.
  - Leave out Models used (no data).
  - When the flag is off: only Total sessions, plus our own 12px tertiary note that names
    `dashboard.show_token_analytics` and links to `/config`.
  - Use the same `useTokenAnalyticsFlag()` hook as Analytics.
- **Row 2: Providers as a card grid**, in place of the reference's per-model usage cards (our real data, same card
  frame).
  - Grid: 1, 2 or 3 columns at the same breakpoints, 16px gap. Keep `data-testid="provider-<name>"` on each card.
  - Card header:
    - Line 1: the provider label in mono bold uppercase, letter-spaced and truncated (no "#rank": we have no usage
      rank), plus a "★ main" pill on the provider in use.
    - Line 2: a secondary badge with `name`.
    - Right column: the state text, "configured" (success tone) or "no key" (muted), in the place of the token figure.
      Under it, a **SIGN IN** outlined small button styled like "USE AS", only when `device_login` is set.
  - Body: `description` in 12px secondary. The footer row (faint top border) shows `family · auth_type` in 12px
    secondary on the left.
  - The DeviceLogin panel expands inside the card body when signing in. It keeps today's markup roles (status, link,
    device-code testid, Cancel/Close).
  - Under the grid: our existing note about API keys (Keys / System), 12px tertiary.
  - Main-provider ring: when `p.name === current.provider`, a 1px primary ring at 40%.
- **States**:
  - Spinner while info/options are pending.
  - An error card (the foundation's error component) per failed query.
  - For an empty provider list, use the reference's empty-card arrangement with our text "No providers are known to this
    install."
  - Optional: refetch `["models"]` on window focus, matching the reference behaviour (cheap). Only do this if it does
    not fight the tests' fake timers.

---------------------------------------------------------------------------------------------------------------------
## 3. Logs (`/logs`)

### 3.1 Reference layout (1440x900)

**Header after-title**:
- A **filter summary badge**: secondary badge, x 323–420, y 14–37 (24px tall). Fill dark `#172b29` / light `#d6e6fd`,
  1px border dark `#394640`. Text about 11px serif-small, uppercase, letter-spaced, muted: "AGENT · ALL · ALL"
  (file · level · component).
- The **refresh** icon button follows at about x 435–447.

**Header end slot**:
- The label "AUTO-REFRESH" (11px, uppercase, letter-spaced, foreground) is followed by a **square switch** at x 1384–1417,
  y 16–34 (34x19), with a 1px border in the header-border colour.
- Off state: a square thumb about 12px at the left (dark `#686c61`, light `#8bb2fd`) on a transparent track.
- On: the thumb moves right, the track fills, and a success badge "● Live" (with a pulsing dot) appears after the
  switch.

**Filter toolbar** (`role=toolbar`):
- Starts at y=75. The groups wrap, with a 24px column gap and a 12–13px row gap.
- Each group is the label followed by a joined segmented control 6px later:
  - The label is about 11px, serif-small, uppercase, letter-spaced about 0.1em, muted (dark `#a19a89`, light `#588ffd`).
  - Segments are 28px tall (y 75–102), square, joined with shared 1px borders (dark `#283834`, light `#c5d9fd`), with
    no gaps and no radius.
  - Segment text is about 11px serif-small, uppercase, letter-spaced, with 10–12px horizontal padding.
  - Inactive segments: page fill, muted-to-foreground text.
  - The active segment is filled in foreground with page-coloured text: dark cream `#ffe6cb` with text `#041c1c`, light
    `#0053fd` with text `#e8f2fd`.
- Row 1:
  - FILE (label x 263): segments AGENT 297–357 (active), ERRORS 358–425, GATEWAY 426–500
  - LEVEL (label x 523): ALL 569–611 (active), DEBUG, INFO, WARNING, ERROR up to x 857
  - COMPONENT (label x 881): ALL 966–1008 (active), GATEWAY, AGENT, TOOLS, CLI, CRON up to x 1298
- Row 2 (y 115–141): LINES with 50, 100 (active), 200 and 500, segments x 305–457.

**Log card** (x 263–1407, y 158–881):
- Header 42px tall (divider at y=200), padding 12px vertical and 16px horizontal. It holds a 16px file icon and an 8px
  gap, then "<file>.log" in display face uppercase bold, about 16–17px, e.g. "AGENT.LOG".
- Body, padding 0:
  - An optional error strip at the top: destructive fill at 10%, a 1px bottom border at destructive 20%, 12px padding
    and 14px destructive text.
  - Then the scroller:
    - Min height 400 and max height the viewport minus 220. Scrolls both ways, padding 16px.
    - Text is mono 12px with a measured line pitch of about 18.7px (use the 20px line height token if close).
      Lines wrap (break-words).
    - Each line is a block with 4px horizontal padding (pulled out by the same amount) and a hover tint at secondary
      20%.
    - Line colours by level: error destructive, warning the warning token, info foreground (dark `#ffe6cb`, light
      `#0053fd`), debug tertiary.
    - The scroller **auto-scrolls to the bottom** after every fetch. The shot shows the newest line at the bottom and
      older lines clipped at the top.
- Empty state: a centred muted line "No log lines" style text, with 32px vertical padding.

**Behaviour**:
- A `?file=` deep link chooses the initial file.
- Changing a filter refetches.
- Auto-refresh polls every 5s while it is on; the reference default is off.
- While loading, the refresh icon shows a spinner.

### 3.2 Ours today
- A stacked vertical list of four `Segmented` fieldsets with labels above them, as separate rounded pills. "ALL" and
  "all" are shown as "everything".
- A "Refresh" text button in the header end slot.
- Polling is always on (every 5s while the tab is visible).
- The card holds a small mono muted "gateway.log" heading (`<h2>`), then an `<ol aria-label="Log lines">` capped at 65vh.
- The page does not auto-scroll to the bottom.
- There is no badge and no switch.

### 3.3 Data
`GET /api/logs?file&lines&level&component` returns `{file, lines[]}`, which is everything the page needs, so nothing is
missing. `lineLevel()` already classifies lines; keep it and its test.

### 3.4 What to build
- **Header after-title**: a badge with `${file} · ${level} · ${component}`, shown uppercase (via CSS), then the
  refresh icon button. Replace today's "Refresh" text button.
- **Header end slot**: "Auto-refresh" (label element, uppercase via CSS) and our `ui/Switch.tsx`, restyled square per
  the foundation or with a square variant, plus the "Live" success badge while it is on.
  - **Decision to record**: the default is **on**, so our current behaviour (always polling while visible) and the
    "refreshes every 5 s" test keep holding. Turning it off stops `useVisiblePolling`.
  - Note in Compare that the reference shot shows it off. Tell the lead about this; if the lead prefers default off,
    change the test.
- **Toolbar**: four groups laid out horizontally and wrapping, label left of the segments, joined square segments.
  Restyle `Segmented` in `ui/controls.tsx`, which is likely foundation work, or add a `variant="joined"`.
  - Keep `<fieldset>` + `<legend>`, which gives `role=group` named by the legend: tests and e2e use
    `getByRole("group", {name: "File"})`.
  - Keep `aria-pressed`.
  - Option labels: set the DOM text to the raw values ("agent", "ALL", "all", "gateway", "500") and get the uppercase
    look from CSS. Show "ALL"/"all" instead of "everything". Tests click "ERROR", "cron", "gateway" and "500";
    testing-library matches DOM text exactly and Playwright matches case-insensitively, so CSS uppercase keeps both
    green.
  - Use a legend label of "Lines" (the reference says "LINES").
- **Log card**: the header is an `<h2>` with the file icon and `{file}.log` (DOM text lowercase, CSS uppercase). The e2e
  test needs the heading named "gateway.log".
  - Body: the error strip, then the scroller `<ol aria-label="Log lines">` with `<li>` rows. Keep `data-level`.
  - Use min-height 400px and max-height `calc(100vh - 220px)`.
  - Auto-scroll to the bottom after each successful fetch (set `scrollTop` to `scrollHeight` in an effect on
    `logs.data`).
  - Empty text: keep "No log lines match." (ours). Error: keep `QueryState` semantics but render them as the strip.
- Optional: a `role="toolbar"` wrapper with `aria-label="Logs"` around the groups.

---------------------------------------------------------------------------------------------------------------------
## 4. File mapping and affected tests

| File | Change |
|---|---|
| `apps/web/src/pages/AnalyticsPage.tsx` | rebuilt per §1 |
| `apps/web/src/pages/AnalyticsPage.test.tsx` | Update the pins: "Tokens per day" becomes the new chart aria-label (e.g. "Daily token usage"). The "7 days" button becomes "7d". "3,000" and "700" become the compact values ("3.0K"/"700"), or read them from the stat list. The "Cost per day" rect count is removed, so assert the daily table rows instead. Keep the empty-state text test (our text). Add: the flag-off card, sorting toggles the arrow and order, and the after-title controls render in the header. |
| `apps/web/src/pages/ModelsPage.tsx` | rebuilt per §2.4. DeviceLogin logic unchanged. |
| `apps/web/src/pages/ModelsPage.test.tsx` | The main-model test must open the CHANGE dialog first. The `current-model` text becomes the new format. The aux test must open CONFIGURE, then CHANGE on `aux-compression`, before typing. Keep the provider and sign-in tests with the same testids. Add: the summary line counts overrides, and the reset-all confirm only calls set for the overridden tasks. |
| `apps/web/src/pages/LogsPage.tsx` | per §3.4 |
| `apps/web/src/pages/LogsPage.test.tsx` | Mostly stays. Add a test that the auto-refresh switch off stops polling, a test of the badge text, and a scroll-to-bottom smoke test if jsdom allows it. |
| `apps/web/src/lib/format.ts` (+ test if it exists) | `formatTokens`, `formatShortDay` |
| `apps/web/src/App.tsx` + new `lib/useTokenAnalyticsFlag.ts` | Extract the flag hook. No behaviour change. `App.test.tsx` pins `show_token_analytics`, so keep it passing. |
| `apps/web/src/shell/PageHeader.tsx` | After-title slot, if the foundation lacks it |
| `apps/web/src/ui/controls.tsx`, `ui/Switch.tsx` | Joined square segments and a square switch, if the foundation has not done them. Coordinate, because other pages use `Segmented`. |
| New shared UI (if not in the foundation) | `ui/StatList.tsx`, `ui/SortHeader.tsx` (or a `ui/DataTable.tsx`), `ui/PeriodButtons.tsx`, `ui/IconRefresh.tsx` |
| `apps/web/e2e/dashboard.spec.ts` | The Logs test uses group "File", button "gateway" and heading "gateway.log". It keeps passing if the DOM text stays lowercase. Do not rename. |
| `apps/web/src/testing/mockApi.ts` | Make sure the `/api/analytics/usage` and `/api/config` mocks cover the flag on/off. Models now also calls usage, so add a default usage mock if one is missing. |
| Capture kit (your copy of `$SP/compare/work-i-web/kit/capture.ts`) | `models-side-tasks`: instead of `scrollMain("Side tasks")`, click CONFIGURE in the Auxiliary tasks row and capture the dialog. For `logs`, add a second capture with the default **agent** file, which matches the reference shot (the current step clicks gateway). |

Icons: line icons from the set the foundation adds (lucide-react at an exact pin is allowed): circular arrows
(refresh), bar chart, trending-up line, CPU chip, sliders, star, file-text, chevron-down, arrows up/down/up-down,
dollar, zap, x.

---------------------------------------------------------------------------------------------------------------------
## 5. Build order
1. Check `docs/notes/ui-parity/web-foundation.md` in the merged w51a work. Map each foundation token or component to the
   roles in §0. Add only what is missing: the after-title slot, period buttons, icon refresh, stat list, sortable table
   header, joined segments and the square switch.
2. Add `formatTokens`/`formatShortDay` and the `useTokenAnalyticsFlag` hook (App uses it; App tests stay green).
3. **Logs** (smallest; data complete): header badge, refresh and switch, toolbar, card, auto-scroll. Run the tests and
   the e2e Logs test.
4. **Analytics**: header controls, stat list and chart grid, daily table, all states, flag-off. Update the tests.
5. **Models**: settings card, the two dialogs, the stat card, the provider card grid. Update the tests.
6. Re-capture in both themes (kit copy, with the capture-step changes from §4), build the compare PDF for analytics,
   models, models-side-tasks (now the aux dialog) and logs, and close gaps.

---------------------------------------------------------------------------------------------------------------------
## 6. Compare checklist (tick per theme: L = light, D = dark)

**Common**
- [ ] L [ ] D Page title in the display face at x≈263; the controls sit right after the title (not in the body).
- [ ] L [ ] D Cards are square with 1px borders in the right colour, no shadow, and the page fill. The gaps between
  cards are 22–24px.
- [ ] L [ ] D Period buttons are 28px tall, square, the active one filled (cream / blue), the others outlined.
- [ ] L [ ] D The refresh icon is ghost and muted, and turns into a spinner while loading.

**Analytics**
- [ ] L [ ] D Row 1 has two equal columns, each 289px tall: the stat list on the left, "Daily token usage" on the right.
- [ ] L [ ] D Stat list: five rows at a 37px pitch, the value left in the display face, the label right in letter-spaced
  uppercase muted text, with a dotted leader between them. The fifth row is Est. cost (decision noted).
- [ ] L [ ] D The chart is 160px tall and fills the width. Input sits on top and output on the bottom, in the series
  colours at 70%. The legend has square swatches. The axis shows the first, middle and last dates in tertiary.
- [ ] L [ ] D The tooltip on bar hover shows the date, Input, Output and Total.
- [ ] L [ ] D Daily breakdown table: icon and title header, sortable columns with arrows, the series colours in the Input
  and Output columns, faint row dividers and the hover tint.
- [ ] L [ ] D The empty, error, loading and flag-off states each render as in §1.2.
- [ ] L [ ] D No per-model, skills or cost chart blocks are drawn (decision noted).

**Models**
- [ ] L [ ] D The "Model settings" card has the sliders icon, the uppercase title and the muted subtitle, and two tinted
  setting rows inset 16px with 12px between them.
- [ ] L [ ] D Main model row: the star in primary, the mono value with `current-model`, and a filled CHANGE button.
- [ ] L [ ] D Auxiliary row: the summary count and an outlined CONFIGURE button. The dialog shows the task rows,
  CHANGE opens the inline editor, and RESET ALL TO AUTO has a confirm.
- [ ] L [ ] D Switch model dialog: the reference frame (title, current line, the provider list on the left, the model id
  input on the right, then Cancel and Save).
- [ ] L [ ] D Stat card on the right: dotted-leader rows. With the flag off, it shows Total sessions and the note.
- [ ] L [ ] D Provider cards in a 3/2/1 grid with a 16px gap: mono uppercase title, id badge, state at top right, SIGN IN
  outlined, description, faint footer line. The main provider has the ring.
- [ ] L [ ] D The device sign-in flow works inside a card (code, link, status, Cancel/Close).

**Logs**
- [ ] L [ ] D The header badge "AGENT · ALL · ALL" updates with the filters, with the refresh icon after it.
- [ ] L [ ] D The end slot has "AUTO-REFRESH" with the square switch, and "Live" while it is on.
- [ ] L [ ] D The toolbar is horizontal and wraps: label left, joined square segments 28px tall, the active segment
  filled. LINES is on the second row at 1440 wide.
- [ ] L [ ] D The log card header shows the file icon and "<FILE>.LOG" in the display face. The body is mono 12px with
  about 19–20px lines, colours by level and the hover tint.
- [ ] L [ ] D The view opens scrolled to the newest line at the bottom, at least 400px tall and up to the viewport
  minus 220.
- [ ] L [ ] D The error strip and the empty line render correctly.
- [ ] Unit tests (Analytics, Models, Logs, App), the e2e Logs test, and `tooling.checks --require-ref` all pass.

---------------------------------------------------------------------------------------------------------------------
## 7. Follow-ups (backend; outside this slice)
- `/api/analytics/usage`: add `api_calls` and `by_model[]`. The `model_usage` table already has per-call rows. Adding
  them would enable the API calls row, the per-model breakdown table, and the Models page's "Models used" and per-model
  usage cards with "Use as".
- Model listing per provider. This would enable a real model list in the Switch model dialog.
- Effective and inherited routes for side tasks. These would enable the "Follow <task>" button and the effective-route
  text.
- Top skills analytics, if skills usage is ever recorded.
