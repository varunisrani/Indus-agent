# W52 spec: desktop slice "command-center" (Read + Note)

Surface: desktop (apps/desktop renderer). Slugs: command-center, command-center-system, command-center-usage,
command-center-maintenance, session-import, cron, cron-new-dialog, profiles, agents, artifacts, webhooks, starmap,
empty-state, layout-editor.

Read: reference screenshots `$SP/compare/hermes/desktop/<slug>--{light,dark}.png` (all 14 slugs, both themes) and
our `$SP/compare/indus/desktop/` (command-center, command-center-system/-usage/-logs, session-import (= our
Maintenance tab), cron, profiles, agents, artifacts, webhooks, starmap, empty-state (= our learning map, wrong
capture); we have no layout-editor and no cron dialog capture). Reference source read for structure only:
`app/overlays/{overlay-view,overlay-split-layout,overlay-breadcrumb-header,panel}.tsx`, `app/command-center/*`,
`app/session-import/*`, `app/cron/index.tsx`, `app/profiles/index.tsx`, `app/agents/index.tsx`,
`app/artifacts/index.tsx`, `app/webhooks/index.tsx`, `app/starmap/{index,star-map}.tsx`, `app/page-search-shell.tsx`,
`components/ui/{button,segmented-control,text-tab,tab-dropdown,empty-state}.tsx`, `components/chat/log-tail.tsx`,
`components/pane-shell/tree/renderer/{edit-bar,layout-picker,tree-group}.tsx`.
Our code read: `apps/desktop/src/app/{command-center,cron,profiles,agents,artifacts,webhooks,learning-map,manage}/*`,
`app/{routes,pages,App}.ts(x)`, `panes/{PaneShell.tsx,presets.ts,layoutStore.ts}`, `sidebar/Sidebar.tsx`, and the
backend routes in `src/indus/web/routes/*.py` they call.

Pixel values are measured on the 1440x900 @1x shots. Colours are sampled hexes (light / dark). Font sizes in px;
"text" = 13px, "caption" = 12px (the reference's base conversation sizes), sans = the foundation's UI font (Inter-like),
mono = the foundation's mono. All copy below in quotes is a short label (1-4 words) that may match; anything longer is
described and must be written in our own words.

---------------------------------------------------------------------------------------------------------------------

## 0. The big picture (biggest differences)

1. **Everything except Artifacts is a full-window overlay card in the reference** (command center, session import,
   cron, profiles, agents, webhooks, star map). Ours renders command center / webhooks / learning map as pages inside
   the chat pane column and cron / profiles / agents as small 640px Radix dialogs. Rebuild them all on ONE overlay card.
2. **Two internal layouts inside that card**: (a) "split" = a 208px tinted left rail with icon nav + a main column
   with breadcrumb (command center); (b) "panel" = header (title + count) then a 208px list column with search,
   rows, a "+" row, and a detail column (cron, profiles, webhooks; agents and star map use only its empty state).
3. **Quiet, text-first controls**: no grey pill buttons. Actions are underlined text links ("Run doctor", "Update"),
   tabs are plain text with an underline on the active one, period pickers are a small segmented control, the only
   filled buttons are the primary blue ones. Tables are gone: lists of two-line rows.
4. **Data gaps** (our backend lacks): usage API-call counts, per-model and per-skill usage, security audit, debug
   share, skill curator, memory reset, cron per-job model, profile path / skill count / .env flag, session-import
   host + turn count + "already imported", webhook skills. Each is handled below as "omit", "placeholder" or "map to
   our field" — never a new backend feature.
5. Layout editor: the reference is a floating "Layouts" card over an edit mode that dashes every zone; ours is a
   toolbar row with a `<select>` and an Advanced/Simple segmented switch. Same store, new UI.

---------------------------------------------------------------------------------------------------------------------

## 1. Shared pieces this slice needs (check `docs/notes/ui-parity/desktop-foundation.md` first)

wt-w51b builds the settings shell (left category nav + page frame) and core components. The command center uses the
SAME card + rail as Settings. If the foundation ships these, use them; if not, build them once under
`apps/desktop/src/app/overlay/` (new folder) and tell the foundation owner in your report. Never copy reference
class strings — write CSS against the foundation tokens.

### 1.1 OverlayCard (full-window card)
- Backdrop: whole window, black at ~22% + 2px blur (light result over the app ≈ #6f7071 behind blurred chrome; dark ≈
  #000104). Click on backdrop closes; Esc closes (topmost layer only; a nested dialog's Esc closes the dialog first).
- Card: inset 48px on all four sides at 1440x900 (= titlebar 34px + 14px), so 1343x804 at (48,48)-(1391,851).
  Radius 12px, 1px border #c8d5f1 / #232f47, surface #fefefe / #0d1015, soft shadow (shadow-md), overflow hidden.
- Close button: ghost icon button, 14px "x" glyph colour #949699 / #767c82, centred at (1366, 67) — i.e. 25px from the
  card's right edge, vertically on the titlebar line. Hover: faint fill + primary text colour. Optional
  `titlebarActions` slot left of the X (session import puts a refresh icon at x≈1336).
- The top 37px strip of the card is a window-drag region (except its buttons).
- Props: `onClose`, `closeLabel`, `titlebarActions?`, `surface?: "default" | "elevated"` (session import uses the
  elevated surface #fdfdfd / #16181d), children.
- Mark the root with a data attribute so composer type-to-focus is suppressed while an overlay is open (our composer
  may already have a check — look for how Settings blocks it).

### 1.2 SplitNav (rail + main) — command center (and Settings, if the foundation does it there)
- Grid: 208px rail | main. Below 760px window width the rail becomes a dropdown in the titlebar strip (nice to have;
  the desktop min width is larger than that in practice — keep it simple: rail always).
- Rail: bg #f6f8fa / #010409, full card height, padding 10px sides, first item top at card+11px (y=59).
- Nav item: height 28px, radius 6px, 1px border transparent; gap 2px between items (pitch 30px: y 72, 102, 132, 162
  centres); icon 16px at x+8, label 13px regular at x+33 (x=92). Idle: text #575a5f / #aab1b6, icon #999c9f / #63686e.
  Hover: faint hover fill, text primary. Active: fill #d8e1f0 / #101826, border #b9cae8 / #1f2c44, text #2a2f35 /
  #d9e0e7, icon at 80% of text colour.
- Main column: max width 1200px centred, horizontal padding clamp(20px, 4vw, 64px) (= 57px from rail edge at 1440,
  content x=314), top pad so the breadcrumb sits on y≈78.
- Breadcrumb row (height ~16px, margin-bottom 12px): "Command Center" 12px #86888b / #81878d, chevron-right 12px same
  colour, current section 12px #2c3035 / #d8e0e6. Optional trailing slot right-aligned on the same row (search,
  segmented control).

### 1.3 Panel kit (master/detail inside OverlayCard) — cron, profiles, webhooks, agents, star map
- Panel content padding: 20px left/right (content x=69), top on the titlebar line (header title at y≈69).
- PanelHeader: title 14px semibold #2c3035 / #d8e0e6 (y≈69), subtitle 12px #9d9fa1 / #6a6f74 under it (y≈87, e.g.
  "0 jobs", "1 profile"); optional actions on the right (leave 32px for the X). margin-bottom 12px.
- PanelBody: row flex, gap 20px; list column 208px wide (x 69-277), detail column flex-1 (x≈301-1362).
- List search: full-width field at the top of the list column, no visible box: magnifier 14px #e2e2e2 / #282b30
  at x≈77, placeholder 13px very faint #d9dadb / #2f3439 ("Search cron jobs…", "Search profiles…"), height ~28px.
- List row: height 28px, radius 6px, 12.5px (0.78rem) medium text #464a51 / #bdc5cb; leading 14px icon #aab0bb /
  #565e69 (or a 6px status dot: green #00bc7d enabled, grey disabled); optional right meta 10px faint; active row fill
  #dfe6f4 / #1b2331 + text primary; hover row fill subtle. A "…" row menu button (20px) appears on hover at the right
  (Edit / Delete etc.).
- Add row: full-width 28px row with a centred "+" icon only, colour #aaabad / #5f6368, hover fill (y≈181 under the
  last row in profiles).
- Section label: 9.6px (0.6rem) medium uppercase, letter-spacing 0.05em, colour #c1c2c4 / #474b51 (e.g. "BLUEPRINTS").
- PanelEmpty: centred in its area: 20px icon #c1c2c4 / #474b51, 8px gap, title 14px medium #414549 / #c5cbd1,
  description 12px #aaabad / #5f6368, max width 384px, line-height ~19px, optional action (primary small button) 8px
  below.
- PanelMeta: two-column definition grid (80px label column, 11px text); label #bbbcbe / #4e5256, value #4c4f53 /
  #bac1c6.
- PanelPill: radius full, 10px medium, padding 2px 6px; tones: good = primary-soft bg #e4ecfd / #131b2c with primary
  text #0053fd / #4a84fe; muted = #e9e9ea / #21242a with #7c7e81 / #8b9097; warn = amber 10% bg, amber text;
  bad = destructive 10% bg, destructive text.
- PanelBlock (prompt preview): radius 4px, bg text-colour at 5%, padding 10px, 11px text at 80%, max-height 192px,
  pre-wrap.

### 1.4 Small controls (if the foundation lacks them)
- **TextButton** ("text"): no box, 11px medium, colour #86888b / #81878d, underline on hover. **TextStrongButton**
  ("textStrong"): same but semibold and always underlined (underline colour ~#e5e6e6 / #24282d, offset 4px).
  Destructive text variant: destructive colour (sampled disabled at 50%: #e68f95 / #83302f).
- **Primary button** small: bg #0053fd / #4a84fe, text #ffffff / #161616, radius 2.5px, 12px medium, padding 4px 10px
  (height 24px; "New cron" 74x24 at (797,515)). Disabled = 50% opacity (#7fa8fe / #2b4a89 look).
- **Outline button**: transparent, 1px inset ring #e7edf7 / #1c222e, text primary (dialog "Cancel").
- **Segmented control**: track radius 5px, padding 2px, bg #dfe6f4 / #1b2331 (import variant #d8e1f0 / #101826);
  options 11px medium, padding 2px 10px, idle text #777d86 / #88909a; active option bg = card surface
  (#fefefe / #0d1015), text primary, radius 3px, small shadow. Height 22-24px.
- **TextTabs**: plain buttons h-28px, 12px medium, idle #86888b / #81878d, active primary text with an underline
  (offset 4px, 25% current colour); optional count meta after the label at 0.72em tertiary ("All 0").
- **LogTail**: well with radius 8px, 1px border #ced9ef / #232d40, bg #f0f2f8 / #151a22; mono 11.2px (0.7rem) lines,
  line-height ~18px, colour #3f4348 / #c6cdd3, pre-wrap; WARNING lines carry a 2px amber rule (#c08532) in the left
  gutter (6px left of the text), ERROR lines the same in destructive red; hover shows three 24px icon buttons top-right
  (jump to top, jump to bottom, copy). Keeps scroll pinned to the bottom unless the user scrolled up. Empty: mono
  muted "No log lines" style text.
- **StatTile**: label 10px medium uppercase, letter-spacing 0.12em, #86888b / #81878d; value 16px semibold tight,
  #2c3035 / #d8e0e6, 4px below.

---------------------------------------------------------------------------------------------------------------------

## 2. Routing change (all overlay slugs)

Reference: these are routes rendered as overlays over the route they were opened from. Ours: `routes.ts` has
`PAGES = [capabilities, messaging, artifacts, webhooks, command-center, learning-map, bots]` and
`OVERLAYS = [settings, cron, profiles, agents]`.

- Move `webhooks`, `command-center`, `learning-map` from PAGES to OVERLAYS; add a new overlay `session-import`.
  Keep the path names (deep links and sidebar nav keep working). Artifacts, capabilities, messaging, bots stay pages.
- Command center section in the query: `/command-center?section=sessions|system|usage|maintenance` (the reference
  uses the same query key; ours keeps `initial` prop too). Read/write it with the router so the section survives
  re-open; default `sessions`.
- Files: `app/routes.ts` (+ `routes.test.ts`: reserved names list and the "overlay returns to the route it was opened
  from" case — add the moved ids), `app/pages.tsx` (PAGE_TEXT/PAGE_ICONS → OVERLAY_TEXT/OVERLAY_ICONS entries;
  icon choices: command center = gauge, webhooks = webhook, learning map = orbit/sparkles, session import =
  arrow-down-to-line or import), `app/manage/managementSlots.tsx` (move the three from `pages` to `overlays`, add
  `"session-import"`), `app/App.tsx` only if the overlay slot typing needs it. Decision label: `W52-<x>-1 command
  center, webhooks, learning map and session import are overlays (reference structure); paths unchanged`.
- Sidebar entries (owned by the foundation): leave them; they navigate to the same paths.

---------------------------------------------------------------------------------------------------------------------

## 3. Per slug

### 3.1 command-center (Sessions section)
Reference (light/dark identical structure): OverlayCard + SplitNav. Rail items, in order: "Sessions" (message-circle
icon), "System" (activity / pulse icon), "Usage" (bar-chart icon), "Maintenance" (wrench icon). Main: breadcrumb
"Command Center › Sessions"; trailing a borderless search field (magnifier 14px at x≈1113, placeholder 13px very
faint #d9dadb / #2f3439 "Search sessions, views, and actions" style text — write our own e.g. "Search sessions",
right edge at x≈1331, max width 40vw).
- List of every session (newest activity first), one row each, pitch 54px (8px vertical padding): line 1 = title
  13px medium #2c3035 / #d8e0e6, truncated with ellipsis (y≈129); line 2 = date-time 12px #86888b / #81878d formatted
  like "Oct 24, 2026, 1:06 PM" (locale medium date + short time). Title column takes the full width; rows are
  left-aligned at x=314.
- Hover on a row reveals three 24px ghost icon buttons at the right (opacity 0 → 1, also on focus-within):
  pin/unpin (bookmark / bookmark-filled, 14px), export (download), delete (trash; hover destructive). Each has a
  tooltip.
- Click a row = open that session (navigate to `#/<id>` and close the overlay).
- Delete opens a confirm dialog (title "Delete session?", destructive "Delete", description in our words naming the
  title); busy and done labels.
- Empty: centred block min-height 192px: 12px tertiary text — no sessions / no matches for the query (debounced
  180ms; filter on title + id, case-insensitive).
- "Load more" button bottom-right when the server page is capped.
Ours today: page with title, grey tabs incl. "Logs", "3 sessions · 3 active · 0 empty", grey "Delete empty sessions"
button, 10 recent links. Data available: `GET /api/sessions?limit=50&offset=` (`sessions`, `total`), titles,
`last_active`; pin = `PATCH /api/sessions/{id}` `{pinned}` (or reuse the sidebar store's `setPinned` if the overlay
can reach it — preferred, so the sidebar updates); export = `GET /api/sessions/{id}/export` — the sidebar has no
export action today; save it through main the way artifacts download does if the bridge can carry it, otherwise show
the export icon disabled (placeholder, no new main-process feature in this slice; follow-up);
delete = `DELETE /api/sessions/{id}`. Stats + "Delete empty sessions" move to Maintenance (3.4). Load more = next
offset while `offset+limit < total`.

### 3.2 command-center-system
Breadcrumb "Command Center › System". Main is two rows: status block, then "Recent logs" filling the rest.
- Status block (y 101-139): 8px dot (green #00bc7d running / amber stopped) + "Messaging gateway running" style
  label 13px medium (write ours: e.g. "Gateway running" / "Gateway stopped"); line 2 12px tertiary: "<product>
  <version> · Active sessions N". Right side, same row as line 1: two text actions 11px: "Restart gateway" (text)
  and "Update" (textStrong; reference says "Update Hermes" — ours "Update Indus" is 2 words, fine). After an action:
  one caption line "<action> · running|done|failed".
- Recent logs header row (y≈179): left "RECENT LOGS" 10px medium uppercase tracking 0.08em tertiary; right, gap 12px:
  TextTabs for the file (agent, errors, gateway, desktop), TextTabs for level (all, info, warning, error), then a
  176px search field ("Search log lines…" style placeholder, magnifier 14px). Active tab = primary text + underline.
- LogTail below (x 315-1333, from y≈203 to the card bottom, 0 bottom gap), 200 lines.
- Loading: centred loader "Loading status" while status is null. Error: inline 12px destructive text with alert icon
  on the header row.
Ours today: two bordered cards "This machine" (cpu_count) and "Health" (key/value table) + a separate Logs tab with
`<select>`s and a plain `<pre>`. Mapping: dot + label from `GET /api/daemon` (`running`, `state`); version and active
sessions from `GET /api/status` (`version`, `active_sessions`); Restart = `POST /api/daemon/restart` then poll
`GET /api/actions/{name}/status` (1.2s, max ~18 polls) and show the caption line; Update = `POST /api/system/update`
(202) polled the same way — if our update answer has no action name, show the started/failed note from its answer.
Logs = the existing `LogViewer` logic (`GET /api/logs?file=&lines=200&level=` for agent/errors/gateway, the bridge
`logs.recent` + `filterByLevel` for desktop); keep the DEBUG level reachable only if you can fit it as a fifth text
tab "debug" (decision `W52-<x>-2`: our level set is all/info/warning/error, + debug if kept). The host stats table
(`/api/system/stats`) has no place in the reference view: drop it from this section (the web System page shows it).

### 3.3 command-center-usage
Breadcrumb "Command Center › Usage"; trailing segmented control "7d" "30d" "90d" (default 30d; reference active =
30d) at x 1203-1333, y 72-95.
- Stats row (y 118-155): three StatTiles in a 3-column grid (columns start x 314 / 659 / 1004), labels
  "SESSIONS", "API CALLS", "TOKENS IN/OUT"; values like "4", "0", "0 / 0" (compact numbers: 1.2K, 3.4M).
- "DAILY TOKENS" section (label y≈194) with a right legend: 8px squares, input = primary at 60% (#6698fe / #3155a0),
  output = emerald at 70% (#4cd0a3 / #04895d), legend text 10.4px tertiary "input" / "output". Chart: 96px tall
  stacked bars (input on top of output), 1px gaps, one bar per day across the full width, tooltip per bar
  "<day> · in N · out N"; under it first and last day 9.6px tertiary at both ends (y≈316). No days → 96px block with
  a centred caption "No activity in this period" style text.
- Two columns (gap 32px, start x 314 / 840): "TOP MODELS" and "TOP SKILLS" (labels 10px uppercase), rows 6 max:
  label mono 11.2px primary, value 10.4px tertiary right-aligned; empty = 12px tertiary sentence.
- No totals at all: centred empty with a "Retry" text button; loading: centred loader.
Ours today: grey "7 days/30 days/90 days" buttons + one sentence "0 tokens in · 0 out · 0.00 USD · 3 sessions".
Data: `GET /api/analytics/usage?days=` → `daily[] {day,input_tokens,output_tokens,cost,sessions}`, `totals {input_tokens,
output_tokens,cost,sessions}`. Gaps: API calls (none) → keep the "API CALLS" tile with a "—" value (placeholder,
per the lead rule) and show our cost as the TOKENS tile's 10px hint line ("$0.00 estimated" style) so real data is not
lost (decision `W52-<x>-3`). Top models / top skills: no API → render both
section labels with their empty sentence (placeholder state; wire later when the analytics route grows `by_model` /
skills). The reference shows "mock-model 0" under top models only because it has that data — we show the empty line.

### 3.4 command-center-maintenance
Breadcrumb "Command Center › Maintenance". Vertical sections, gap 20px, each with a section label (10px uppercase
tertiary, margin-bottom 6px) then rows. Row: py 8px; left = title 13px medium primary + description 12px tertiary
2px below; right = one textStrong action (11px, underlined) right-aligned at x≈1333.
Reference sections: "DIAGNOSTICS" (Run doctor, Security audit, Create backup, Debug share), "SKILL CURATOR" (row with
an "Active" pill: bg #d8f4eb / #0b2a25, text #00d492, 10px; actions "Pause" (text) + "Run now" (textStrong)),
"MEMORY DATA" (a caption line "Built-in memory files … · Active provider: built-in", then two rows "Agent memory
(MEMORY.md)" + size or "empty" in 12px tertiary next to the title, action "Reset memory" / "Reset profile" in
destructive text, disabled when empty). A started action shows a "VIEW LOG" label + a 192px-max mono pre well
(LogTail styling, 10.4px text) tailing `/api/actions/{name}/status` every 1.2s while running; actions are disabled
while one runs. Toast on start/fail.
Ours today: grey buttons Run doctor / Back up / Prune checkpoints, a days input + "Prune old sessions", and the import
card. Mapping (decision `W52-<x>-4`, our rows in the reference row style):
- "DIAGNOSTICS": Run doctor → `POST /api/ops/doctor`; Create backup → `POST /api/ops/backup` (answer carries
  `archive`; show it in the caption line after done). Security audit, Debug share: no backend → keep the rows
  (same look) with the action disabled and a "Not available yet" tooltip (lead rule: disabled control or placeholder,
  listed as a follow-up).
- "SESSIONS" (ours, same row style): "Delete empty sessions" → `DELETE /api/sessions/empty` (description shows
  `GET /api/sessions/stats` "N sessions · N active · N empty" in our words); "Prune old sessions" → `POST
  /api/sessions/prune {older_than_days, dry_run:false}` with the days as a small 64px number input inline before the
  action (keep 90 default); "Prune checkpoints" → `POST /api/ops/checkpoints/prune`. A 409 shows the busy message
  (existing "Not now: …" string) as the inline error line (alert icon, destructive 12px) — keep the test.
- "SKILL CURATOR": no backend → keep the row with a muted "Unavailable" pill instead of "Active" and both actions
  disabled (placeholder); if that reads as clutter in Compare, drop the section and say so in the report.
- "MEMORY DATA": caption from `GET /api/memory` (`active` or "built-in"), two rows with `builtin_files["MEMORY.md"]` /
  `["USER.md"]` sizes ("empty" when 0, else "1.2 KB"); Reset actions: no backend → render them disabled (they are
  disabled in the reference too when the file is empty). Do not invent a reset route.
- A link row "Import from another app" (textStrong "Open") that navigates to the `session-import` overlay, since
  the import leaves this tab (the reference opens import from its own route).
- Tail log well for doctor/backup/prune via `GET /api/actions/{name}/status` when the 202 answer carries an action
  name (check `_start_command`'s answer: it returns the action name; checkpoints prune also 202).

### 3.5 session-import (new overlay `#/session-import`)
OverlayCard with the elevated surface (#fdfdfd / #16181d) and a refresh icon button left of the X (x≈1336, y≈67).
- Header (padding 32px sides, 48px top, 24px bottom): h1 30px semibold tight "Continue from another app" (4-word
  label may match; or ours "Import a conversation") #2c3035 / #d9e0e6 at y≈115; subtitle 14px #595c60 / #afb6bb,
  8px below, in our words (bring a conversation from Claude Code or Codex into Indus and keep going); meta row 20px
  below, 12px secondary, gap 20px: monitor icon + "Reading from <host>", user-circle icon + "Import into <profile>".
- 1px divider #d9e2f3 / #242c3c at y=221, then a two-column body: left 467px (bg #f6f8fa / #010409), right flex
  (surface). Ratio ≈ 0.85fr : 1.6fr, min-left 272px.
- Left column (padding 24px, top 24px): segmented control "All" / "Claude Code" / "Codex" (track #d8e1f0 / #101826,
  272x24 at x 73, y 246); when rows exist a search field below (gap 20px). List (padding 12px sides): date group
  headers 12px medium secondary ("Oct 24"), rows are buttons radius 6px, padding 16px 12px: line 1 12px secondary =
  chat-bubbles icon + source label … right "N messages" (tabular); line 2 14px medium primary title, 2-line clamp;
  line 3 12px secondary cwd (or excerpt), truncated. Hover/selected fill = hover token. Empty: centred title 14px
  medium "No conversations found" + 12px tertiary description (our words); loading "Finding conversations" loader;
  error state with "Retry" secondary button; footer note when some logs were unreadable; "Load more" text button.
- Right column when nothing selected: centred PanelEmpty: chat-bubbles icon #c0c1c3 / #4e5157, title 14px
  #414549 / #c6ccd2, description 12px #aaabad / #64686d (our words: pick a session to read it first).
- Right column with a selection: header (padding 24px 32px 20px): title 20px medium tight; caption "<source> · N
  messages" 12px secondary. Body scroll (padding 0 32px 32px, max 65ch): each message = 12px semibold secondary
  speaker ("You" / source label) + 14px body rendered as markdown (preview only), 28px between messages; a note when
  the preview is shortened. Footer (1px top border, padding 20px 32px): left 12px secondary notice (our words: text is
  copied, the source is untouched, tool output is not carried), right primary button "Continue in Indus" + arrow-right
  icon ("Importing…" while pending; "Open in Indus" when already imported); inline error line under it.
Ours today: a card inside Maintenance with grey source buttons, a candidate button list, an `<ol>` preview and
"Import this session". Data: `GET /api/session-import/candidates?source=claude-code|codex` →
`candidates[] {source,path,title,cwd,modified}`; `POST /api/session-import/preview {source,path}` → `{title,
messages[{role,content,tool?}], skipped}`; `POST /api/session-import/import` → `{session_id, message_count}`.
Gaps: host (none → "Reading from this computer" for a local connection, the connection's host name for remote if the
connection store exposes it), destination profile = the window's profile name; per-row message count (none in the
list → show the source label alone; after preview show `messages.length`); "already imported" (none → always
"Continue"); pagination (none → no "Load more"); refresh = reload candidates (+ preview). After import: navigate to
`#/<session_id>` and close (the reference opens the session); keep a status line if navigation is not possible.
Move `SessionImport` out of `CommandCenter.tsx` into `app/session-import/SessionImport.tsx`; update the Maintenance
test (it clicks "List files" → preview → "Import this session"): move that part into a new
`SessionImport.test.tsx` with the new button text.

### 3.6 cron (Scheduled jobs overlay)
OverlayCard + Panel kit. Header "Scheduled jobs" / subtitle "0 jobs" (count).
- List column: search ("Search cron jobs…"); job rows (status dot or clock icon, title, row menu: Edit / Delete);
  when there are no jobs, a centred 12px tertiary line "No scheduled jobs yet" (y≈163) in the list; then the "+" add
  row (y≈201); then section label "BLUEPRINTS" (mt 12px, px 8px) and one row per blueprint with a rocket icon 12px
  #bdbec0 / #4c5054, title 13px #4c4f53 / #bac1c6 (pitch 28px: 255, 283, 311 …), optional right meta. Clicking a
  blueprint opens the New cron dialog pre-set to it.
- Detail column, no job selected: PanelEmpty with a watch/clock icon, title "No scheduled jobs yet", our-words
  description (schedule a prompt, Indus runs it and delivers the result), primary "New cron" button (74x24).
- Detail column with a job: title 15px semibold + pills (state: good/warn/muted; "script" muted), right actions
  (ghost buttons with icons) "Pause"/"Resume" and "Run now"; PanelMeta rows (Frequency = schedule display, Next run,
  Last run, Deliver to, Model if any); "Edit" and "Run now" actions; section "PROMPT" + PanelBlock; a "RUNS" section
  with the last run (time, status, error) — we only have last run fields.
- With zero jobs AND zero blueprints the whole panel body is one PanelEmpty with the "New cron" button.
Ours today: 640px dialog, "All profiles" checkbox, "New job" button, table Name/Schedule/Last run/Next, inline form
card. Data: `GET /api/cron/jobs` (per profile), pause/resume/trigger/delete routes, `GET /api/cron/blueprints` (skills
with a suggested schedule: `{skill, description, schedule, prompt}` — blueprint row title = skill name, meta =
nothing), `GET /api/cron/delivery-targets` (`targets[] {id,label,kind,available}`). Keep the "All profiles"
aggregation as a small checkbox/toggle row under the search (our feature; decision `W52-<x>-5`), and the profile name
as the row meta when it is on. Keep `loadJobs`/`jobAction` and their test; the blueprint test now goes through the
dialog's "Start from" select.

### 3.7 cron-new-dialog
Centred modal over the cron overlay (the overlay behind gets the dialog backdrop: black ~22% + blur, light result
≈ #bfbfbf). Dialog 512px wide (x 464-975), top y=133, radius ~8px, 1px border #d3ddf2 / #232d40, surface #fbfbfb /
#15171c, padding 16px, close X top-right (x≈952, y≈155).
Contents top to bottom (labels 13px medium primary, field gap 16px, label→control 6px):
1. Title "New cron job" 15px semibold; description 12px #85878a / #858a90, 2 lines, our words (schedule a prompt; cron
   syntax or a plain phrase like "every 15 minutes" — the quoted example is fine).
2. "Start from" select (height 36px, radius 6px, very faint border #e9e9e9 / #0f1014, chevron-down at right): "Custom"
   + one item per blueprint. A blueprint's description shows as a hint under it.
3. "Name" + "Optional" hint (11px #8a8c8e) — text input; focused state = 1px primary border #0053fd / #4a84fe
   (placeholder "Morning briefing" style, 13px #85878a).
4. "Prompt" — textarea ~96px tall, mono placeholder 12.5px (our own example text), resize handle.
5. Two columns (gap 16px): "Frequency" select (Daily, Weekdays, Weekly, Monthly, Hourly, "Every 15 minutes",
   Custom) | "Deliver to" checkbox group: a bordered box (radius 6px, faint border) with one checkbox row per delivery
   target, checkbox 16px primary when checked, label 14px (the reference renders these one size larger than the rest —
   noted as their bug; use 13px).
6. "Model" + "Optional": select "Default (global model)".
7. When Frequency ≠ Custom: summary well (radius 6px, bg #edeff5 / #1c2029, padding 8px 12px, 12px): left medium
   "Every day at 9:00 AM", right mono tertiary the cron expression "0 9 * * *". When Custom: a mono text input "Cron
   expression" + hint.
8. Inline error (destructive 10% bg, alert icon, 12px).
9. Footer right-aligned, gap 8px: outline "Cancel" (64x28), primary "Create cron" (92x28) ("Save changes" when
   editing, "Saving…" while busy).
Presets → expressions: daily `0 9 * * *`, weekdays `0 9 * * 1-5`, weekly `0 9 * * 1`, monthly `0 9 1 * *`, hourly
`0 * * * *`, every 15 minutes `*/15 * * * *`; reverse-map an existing job's expression to a preset (else Custom).
Ours today: inline form (blueprint select, name, prompt, free-text schedule "every 2h", free-text deliver).
Data: `POST /api/cron/jobs {name?, prompt, schedule, deliver?, skills?}` (extra fields are rejected — `extra="forbid"`),
`PUT` for edit. Deliver = comma-joined checked target ids (our `split_tokens` reads commas); targets from
`/api/cron/delivery-targets` (label "Save locally" for local → show it as "This computer"/"This desktop" (short label)
and unavailable platforms disabled with "needs a home channel" style suffix in our words). Model: no per-job model in
our API → render the select disabled with only "Default (global model)" (placeholder state; never send `model`).
Blueprint "Start from": fill name/prompt/schedule/skills from the skill blueprint (our current behaviour) — the
reference's typed slots are not ours. Our free-text schedules ("every 2h") stay valid through Custom.

### 3.8 profiles
OverlayCard + Panel kit. Header "Profiles" / "1 profile".
- List: search ("Search profiles…"), rows with a home icon for the default profile (other profiles: a small coloured
  glyph/initial; our profile rail already has colours — reuse its colour helper), active row fill; row menu: Rename,
  Delete (not for default); "+" add row.
- Detail: name 15px semibold "default" + pills: "Default" (good tone) and ".env" (muted) when present; path line mono
  10.5px #bcbdbf / #4d5155 (y≈141); PanelMeta: "Model" → mono "mock-model · custom" (provider after a faint dot),
  "Skills" → count. Then the persona editor: label "SOUL.MD" style section label (ours: "PERSONA.MD", 11px uppercase
  tracking 0.14em, #c1c2c4 / #474b51) + 12px tertiary description (our words: the system prompt / persona for this
  profile; a different sentence when the file does not exist) + right "Unsaved changes" 10px when dirty; a framed mono
  editor (1px border #dae3f4 / #1d2535, radius 4px, 13px mono text #1f2328 / #e6edf3, line-height 20px, min-height 192px
  — a plain `<textarea>` with mono font is fine, no code editor package needed); a right-aligned primary small button
  with a save icon "Save PERSONA.md" (disabled until dirty; 121x24 at x≈1241, y≈456). Inline error under the editor.
- Dialogs (shared Dialog styling as 3.7): "New profile" (name, copy settings from), "Rename profile", "Delete
  profile" (type the name to confirm — keep our typed-name rule).
- Empty (no profiles): PanelEmpty with "New profile".
Ours today: one dialog with cards: default row + Export/Persona buttons, New profile form, Import form. Data: `GET
/api/profiles` (`name,is_default,is_sticky,is_current,model,provider,description,has_persona`), persona
`GET/PUT /api/profiles/{name}/persona` (`content, exists`), create `POST /api/profiles`, rename `PATCH`, delete
`DELETE` (both through the switcher — keep `ProfileChanges`), export `POST /api/profiles/{name}/export`, import
`POST /api/profiles/import`. Gaps: path, skill count, `.env` flag → omit the path line and the ".env" pill (both are
conditional in the reference too); Skills row → the window's own profile can use `capabilities.summary` (already
fetched for the Capabilities page); other profiles show "—" (placeholder). Show
`description` (if any) as a 12px line under the name. Export and Import stay: Export as a row-menu item and a ghost
action in the detail header; Import as a ghost "Import" action in the PanelHeader actions (opens a small dialog with
the archive path field and the no-keys note). Keep the three existing tests' behaviours (export note, typed delete,
missing persona → create) with new markup.

### 3.9 agents
OverlayCard + Panel kit. Reference capture = empty: centred PanelEmpty, robot icon 20px, title "No live subagents",
description 12px our words (when a turn hands work to helpers, their progress shows here), no action.
With data (not captured): PanelHeader "Agents" + subtitle; a 11px tertiary summary line (workers, active, failed in
destructive); groups per delegation batch: 10.5px uppercase label "<goal> · N workers · N active" (active in primary);
worker rows: 14px status glyph (spinner running, check-circle emerald done, alert-circle destructive failed), goal 13px
medium, last note 10.5px tertiary under it, elapsed timer right 9.6px; expandable details indented 24px: mono 10.7px
lines of tools/files, "FILES" mini label 9.3px uppercase.
Ours today: small dialog with "Open a conversation to see its agents." Data: `delegation.ts` reducer (batches →
workers: index, goal, status, startedAt, endedAt, tools, files, lastNote) — maps 1:1 to the rows above. With no
session or no batches → the PanelEmpty (two empty texts: no conversation open vs no helpers yet).

### 3.10 artifacts (page, not overlay)
Reference: lives in the main area beside the sidebar (sidebar active row "Artifacts"). Page surface #fefefe /
#0d1015; a header row under the titlebar (top pad 34+8px): centred TextTabs "All", "Images", "Files", "Links" each
with a count meta ("All 0") at y≈55; right end of the same row a 14px refresh icon button (x≈1416; spins while
refreshing). A search field appears in the left slot only when there are artifacts. Body:
- Empty: centred title 14px medium "No artifacts found" + 12px tertiary description (our words: images and files from
  sessions appear here).
- Images: a sticky 28px pager row (right: "1-24 of N images" 10px tertiary + compact pagination), then a grid of cards
  min 176px (auto-fill), gap 8px: card radius 8px, 1px border stroke-tertiary, bubble surface; image area (max 160px,
  contain, zoom cursor); text block padding 8px: 10px uppercase tertiary kind label with icon, 12px medium file name,
  10px tertiary path, 10px tertiary "session · time", small actions (open folder, open chat).
- Files/links: the same sticky pager, then one bordered table (radius 8px): header row 10px uppercase tertiary on
  #f0f2f8-ish quinary bg; columns Name (35%; 24px rounded icon tile + name), Location (41%; path with a hover copy
  button), Session (24%; title + 11px time; click opens the chat). Rows 12px secondary, hover fill.
Ours today: page title "Artifacts", grey filter buttons, search input, cards with "Open chat" / "Download". Data:
`extractArtifacts` over recent sessions (`kind: image|file|link`, value, session, time), download via
`bridge.artifacts.download`. Map: counts per kind from our list; refresh = reload; keep Download as a row action
(icon button in the Location cell or a row-hover action) and keep the remote-download test. Pagination is client-side.

### 3.11 webhooks
OverlayCard + Panel kit (no header when there are no subscriptions).
- Receiver-off banner (top of the panel, full width x 69-1370, y 59-186): radius 4px, 1px border #a8c3fc / #213560,
  bg #f1f4fc / #10141c, padding 16px; left globe-with-signal icon 16px primary; title 14px medium primary-text
  "Webhook receiver disabled"; body 14px #80838a / #838990 in our words (webhooks are their own gateway platform;
  turn it on to accept HTTP events; chat channels only matter when a route delivers to one); primary small button with
  globe icon "Enable webhooks" (137x24) ("Enabling…" while busy).
- A second banner (same style, warning icon) when a restart is needed: text + secondary button "Restart gateway".
- Empty (no routes): centred: globe icon 20px #c1c2c4 / #474b51, 12px tertiary "No webhook subscriptions yet." style
  text, primary "+ New subscription" (136x24) — disabled (50%) while the receiver is off.
- With routes: PanelHeader "Subscriptions (N)" + hint subtitle; list column with search + rows (green/grey dot, name,
  row menu Disable/Enable + Delete) + add row; detail: name 15px semibold + "deliver only" warn pill; PanelMeta
  (events as pills, auth/signature, deliver, created); URL copy row (11px mono in a 5%-tinted box with a copy icon);
  "DESCRIPTION" + text; "PROMPT" + PanelBlock.
- New subscription dialog (512px, as 3.7): Name + Description (2 columns), Prompt textarea (96px), Events + Signature
  select (2 columns; ours replaces the reference's Skills field — we have no webhook skills), Deliver to select + "Deliver
  only" switch (2 columns), footer primary "Create". After create the dialog switches to "created" view: two copy rows
  (Webhook URL, Secret — shown once) + "Done".
- Delete → confirm dialog naming the route.
Ours today: page with a muted sentence, a table, and an always-visible "New route" form. Data: `GET /api/webhooks`
(`receiver_enabled, base_url, routes[]` with `name, source, url, auth, events, prompt, deliver, description,
created_at, enabled, secret_set`), `POST /api/webhooks` (`name, events, prompt, deliver, deliver_only, auth,
description`; answer has `secret`), `PUT /api/webhooks/receiver {enabled}` (answer `restart_needed: true`) → wire
"Enable webhooks" and then show the restart banner wired to `POST /api/daemon/restart`; `PUT
/api/webhooks/{name}/enabled`, `DELETE /api/webhooks/{name}`. Routes with `source: "config"` are read-only: no row
menu, a muted "config" pill. Deliver-to options: `GET /api/cron/delivery-targets` (same list as cron) plus "none".
The one-time secret stays only in component state (keep that rule and the `webhook-secret` test id).

### 3.12 starmap (our Learning map)
OverlayCard + Panel kit, no header. Reference capture = empty: PanelEmpty with a lightbulb icon, title "Nothing
learned yet", 12px description in our words (as Indus learns skills and memories they show up here). Error →
PanelEmpty with a warning icon and the error text; loading → centred loader.
With data (not captured, from source): the graph fills the panel; a timeline scrubber (play button + 448px track with
dashed baseline and event dots) floats centred near the top; a legend bottom-left 10px tertiary ("skill" = round dot
in primary, "memory" = small rotated square, and a 9px line explaining radius = age); share controls bottom-right
(copy code, load code — icon buttons; load opens a 448px dialog with a mono code textarea and a full-width primary
button); right-click a node for a context menu (enable/disable skill, delete memory).
Ours today: page "Learning map" with grey filter buttons, share button, slider, an SVG force layout (`layout.ts`) and
a node card. Map: keep `layout.ts`, `graphFromSkills`, share code and node actions (`/api/learning/graph`,
`/api/skills/toggle`, `DELETE /api/learning/nodes/{id}`). Restyle: SVG fills the panel (no box), skills = primary
dots, memories = rotated squares in a second token colour, edges at ~15% text colour; filters all/used/learned become
TextTabs top-left; playback slider becomes the floating top-centre timeline (our slider logic); legend bottom-left;
Share / Load code as icon buttons bottom-right; the node card becomes a small popover near the node (radius 8px,
popover surface, shadow). We draw no radial time rings (decision `W52-<x>-6`: our layout is force-directed; the
radial "core = oldest" axis is not built). The "skills only" note (no `/api/learning/graph`) becomes a 10px tertiary
line under the legend.

### 3.13 empty-state (sidebar search with no match)
Reference: sidebar search field shows the query "zzqx storyboard" 13px primary with one clear "x" (#aaadb0 /
#565b60) at x≈150; section label "RESULTS" 10px semibold uppercase primary-blue (#0053fd / #4a84fe) with the dotted
square glyph used for all sidebar section labels (y≈269); 46px below, a 12px tertiary line (#828589 / #7c8188)
`No sessions match “<query>”.` at x=18, y≈326. The main area keeps the current chat.
Ours: `sidebar/Sidebar.tsx` renders `t("sidebar.noResults")` = "Nothing matches." under "Results". Change: string
`sidebar.noResults` → `No sessions match “{query}”.` (short enough to match; pass the trimmed query), class
`sidebar-note` styled 12px tertiary with 46px top spacing, and make sure only ONE clear button shows (known bug: two
clear buttons). The sidebar itself is the foundation's (wt-w51b) — if w51b already restyled the section label and
search, this is a 2-line change; coordinate rather than restyle the sidebar again. Update `Sidebar.test.tsx` if it
asserts "Nothing matches.".

### 3.14 layout-editor
Reference: a titlebar icon (layout glyph, 2nd of the right cluster at x≈1260) or Ctrl+Shift+\ toggles **layout edit
mode**:
- Every zone (sidebar, chat, review, files, terminal) gets a 1px dashed outline (#6b9bfd / #2f529b, -2px inset), a
  light primary scrim over its content (#ebf1fc / #0b111d, content blurred 2px and faded), and a centred drag chip:
  popover-surface pill (radius 6px, 1px stroke, padding 4px 8px) with a 6-dot grip icon + the pane name 10px semibold
  uppercase letter-spacing 0.16em #585b60 / #afb6bb ("SESSIONS", "REVIEW", "FILES", "TERMINAL", the chat tab title).
  Dragging a chip moves that pane between zones (our `store.move(pane, target)` with drop edges).
- A floating "Layouts" card, centred by default, draggable by its header (position kept for the session): width 416px,
  radius 12px, 1px border stroke-secondary, popover surface #fdfdfd / #15171d, big shadow. Header (padding 12px 16px
  8px): "Layouts" 14px semibold; hint 12px #9d9fa1 / #6e7378 "Pick a layout, or drag panes between zones." (short;
  rewrite in our words) + a kbd chip "Ctrl+Shift+\" (mono 10px, bg 5% text, 1px border, radius 4px); right: ghost
  "Reset" (13px #585b5f / #afb6bb) and outline "Done".
- Body (padding 0 16px 16px), sections with labels 9.6px semibold uppercase tracking 0.16em #adafb0 / #60646a:
  1. "INTERFACE MODE": 2-column grid of option cards (radius 4px, padding 10px, 1px border #cdd8ee / #293347, bg
     #eff1f7 / #1c202a): title 11px medium ("Simple", "Advanced"), description 10px tertiary 2-3 lines in our words
     (Simple: chat and sidebar only; Advanced: terminal, files, diffs, status bar and layouts). Active card: border
     primary #0053fd / #4a84fe, bg #eef3fd / #171d2a, title primary text. Under the grid a 10px faint line (our words:
     this changes what is shown, not what the agent can do).
  2. "TEMPLATES": 4-column grid of template cards (each ~90x84: radius 4px, 1px border, padding 6px; a 48px-tall
     miniature of the layout drawn with rounded grey blocks #d0d4dd / #353b47 and dashed blocks for optional panes;
     name 11px medium under it): "Default", "Basic", "Focus", "Terminal deck", "Quad". Active = primary border + soft
     primary bg + primary-text name.
  3. "CUSTOM": a full-width dashed-border button "+ New grid layout" (12px tertiary), and a text button with a save
     icon "Save current arrangement as a template" (12px tertiary).
Ours today: `PaneShell.tsx` `LayoutToolbar` (a `<select>` of presets incl. "Custom", Files/Review toggles, "Open
<pane>" links, Advanced/Simple segmented) shown as a top row. Data: `LayoutStore` has `mode()/setMode`,
`applyPreset(id)` for `PRESET_IDS = basic, focus, default, terminal-deck, quad`, `move`, `reset`, `open`, `close`.
Map: build `panes/LayoutEditor.tsx` (card + edit-mode overlay layer rendered by `PaneShell` when a new
`editing` flag is on), toggled by a titlebar button (foundation titlebar slot) and a new shortcut Ctrl+Shift+\ in
`panes/shortcuts.ts`. Template miniatures are drawn from each preset's tree (`presets.ts`) with small flex boxes —
no images. "New grid layout" and "Save current arrangement": our store has no custom templates → render them
disabled (placeholder state, title tooltip "Not available yet") — decision `W52-<x>-7`. Lead decision: keep the
`#pane-tab-<id>` ids and the "Open <pane>" buttons for e2e — they must stay in the DOM and clickable (wherever w51b
put the pane toggles); the editor replaces only the preset `<select>` and the mode segmented control. Simple mode keeps hiding terminal/files/review (`SIMPLE_HIDDEN_PANES`).
Tests: `PaneShell.test.tsx` and `interfaceMode.test.tsx` drive the toolbar `<select>` and the mode radios — update them
to drive the editor (role="radio" cards for mode, buttons for templates). Keep `aria-checked` on the mode cards and
template cards so the tests stay semantic.

---------------------------------------------------------------------------------------------------------------------

## 4. Mapping summary (our files)

| Area | Change | Tests affected |
|---|---|---|
| `app/overlay/` (new, unless foundation provides) | OverlayCard, SplitNav, Panel kit, TextTabs, Segmented, LogTail, StatTile, TextButton | new unit tests for close-on-Esc/backdrop |
| `app/routes.ts`, `app/pages.tsx`, `app/manage/managementSlots.tsx` | 3 pages → overlays, + `session-import` | `routes.test.ts`, `managementSlots.test.ts` |
| `app/command-center/CommandCenter.tsx` | rebuild: SplitNav, 4 sections, Logs folded into System, import moved out | `CommandCenter.test.tsx` (tab "Logs" → System's file/level tabs; labels "Log"/"Level" become tab buttons; keep `log-lines` test id on the LogTail) |
| `app/session-import/SessionImport.tsx` (new) | overlay per 3.5 | new `SessionImport.test.tsx` (moved case) |
| `app/cron/CronOverlay.tsx` (+ `CronDialog.tsx`) | Panel + dialog | `CronOverlay.test.tsx` |
| `app/profiles/ProfilesOverlay.tsx` | Panel + dialogs | `ProfilesOverlay.test.tsx` |
| `app/webhooks/WebhooksPage.tsx` → overlay | Panel + banners + dialog | add a small test (secret shown once, enable receiver) |
| `app/agents/AgentsOverlay.tsx` | Panel + rows | (delegation reducer test unchanged) |
| `app/learning-map/LearningMap.tsx` | overlay + restyle | `layout.test.ts` unchanged |
| `app/artifacts/ArtifactsPage.tsx` | page shell with TextTabs + grid/table | `ArtifactsPage.test.tsx` |
| `app/manage/common.tsx`, `manage.css` | `ManageOverlay`/`PageFrame` retire when nothing uses them; keep `useRestValue`, `LoadState` (restyle its error to the shared error state) | — |
| `sidebar/Sidebar.tsx`, `sidebar/strings.en.ts` | no-match line (coordinate w51b) | `Sidebar.test.tsx` |
| `panes/PaneShell.tsx`, `panes/LayoutEditor.tsx` (new), `panes/shortcuts.ts` | layout editor | `PaneShell.test.tsx`, `interfaceMode.test.tsx`, `shortcuts.test.ts` |
| i18n `app/manage/strings.en.ts` (+ other locales via the existing tooling) | new keys; never paste reference sentences | locale completeness tests if any |

Icons: lucide-react (already a dependency) — MessageCircle, Activity, BarChart3, Wrench, Bookmark, BookmarkCheck,
Download, Trash2, Search, ChevronRight, X, RefreshCw, Monitor, CircleUser, MessagesSquare, ArrowRight, Watch/Clock,
Rocket, Plus, Globe, Bot, Lightbulb, Save, GripVertical, LayoutGrid, AlertTriangle, AlertCircle, CheckCircle2.
No codicon font, no reference assets.

## 5. Build order
1. Read `docs/notes/ui-parity/desktop-foundation.md`; list which primitives exist. Build the missing ones (1.1-1.4).
2. Routes (section 2) + empty overlays mounting (each overlay renders OverlayCard with its title) — run routes tests.
3. Command center: Sessions → System (+ logs) → Usage → Maintenance.
4. Session import overlay (move code + test).
5. Cron overlay, then the New cron dialog.
6. Profiles.
7. Webhooks.
8. Agents, then Learning map (empty state first, then the restyle).
9. Artifacts page.
10. Layout editor.
11. Sidebar no-match line (after checking w51b's sidebar).
12. Biome, tsc, vitest, desktop e2e; re-capture with the desktop kit in both themes; side-by-side PDF; iterate.

## 6. Compare checklist (tick per slug, both themes)
- [ ] All overlay slugs: card inset 48px, radius 12px, 1px #c8d5f1 / #232f47 border, X at top-right on the titlebar
      line, Esc and backdrop close, dimmed+blurred app behind.
- [ ] command-center: 208px rail with 4 icon items (active boxed fill + border), breadcrumb "Command Center › Sessions",
      borderless search right, two-line session rows (13px title / 12px date), hover pin/export/delete, no stats line.
- [ ] command-center-system: green dot + gateway line + version/active-sessions caption, text actions "Restart
      gateway" / "Update" right, "RECENT LOGS" label with file tabs + level tabs + log search on one row, tinted log well
      with amber rule on WARNING lines, mono 11px.
- [ ] command-center-usage: 7d/30d/90d segmented right of the breadcrumb, 3 stat tiles (SESSIONS / API CALLS "—" /
      TOKENS IN/OUT with a cost hint), DAILY TOKENS label + legend + 96px bar chart + end dates, TOP MODELS / TOP SKILLS columns with empty lines.
- [ ] command-center-maintenance: uppercase section labels, title+description rows with underlined text actions at the
      right, memory rows with sizes/"empty", action log well when something runs, no grey buttons.
- [ ] session-import: 30px heading, subtitle, meta row with two icons, divider, tinted left column with the 3-way
      segmented control and empty text, right empty state with chat icon; selection → preview + footer button.
- [ ] cron: header "Scheduled jobs" + count, search, "No scheduled jobs yet", "+" row, BLUEPRINTS list with rocket
      icons, right empty with watch icon and blue "New cron".
- [ ] cron-new-dialog: 512px dialog, Start from / Name (Optional) / Prompt (mono placeholder) / Frequency + Deliver to
      (checkbox box) / Model (Optional, disabled) / summary well with cron expression / Cancel + Create cron.
- [ ] profiles: header + count, list with home icon row (active fill), detail name + "Default" pill, model meta in mono,
      PERSONA.MD label + description, framed mono editor, right-aligned save button disabled until dirty.
- [ ] agents: centred robot icon + "No live subagents" + description, nothing else.
- [ ] artifacts: page (not overlay), centred text tabs with counts, refresh icon right, centred empty title + text.
- [ ] webhooks: blue-tinted banner with icon, title, body, "Enable webhooks" button; centred globe empty + disabled
      "New subscription"; after enabling, restart banner.
- [ ] starmap: centred lightbulb + "Nothing learned yet" + description; with data, graph fills the card with floating
      timeline, legend, share buttons.
- [ ] empty-state: sidebar RESULTS label in primary blue, one clear button, `No sessions match “…”.` in 12px tertiary.
- [ ] layout-editor: dashed zones with grip chips, floating 416px "Layouts" card with kbd hint, Reset/Done, interface
      mode cards, 5 template miniatures, CUSTOM section (disabled actions).
- [ ] Dark theme: rail #010409, card #0d1015, dialog #15171c, primary #4a84fe with dark text on filled buttons.

## 6b. Backend follow-ups to list in the report (placeholders in this slice)
Usage: API-call count, per-model and per-skill usage. Maintenance: security audit, debug share, skill curator status /
pause / run, memory file reset. Sessions: export save path through main (if the bridge cannot carry it). Session
import: source host, per-candidate message count, "already imported", paging. Cron: per-job model override. Profiles:
home path, skill count, `.env` flag for every profile. Webhooks: skills per route. Layout: saved custom templates and
"new grid layout".

## 7. Colour table (light / dark, sampled)

| Role | Light | Dark |
|---|---|---|
| overlay card surface | #fefefe | #0d1015 |
| overlay card border | #c8d5f1 | #232f47 |
| rail / tinted column bg (also sidebar, titlebar, statusbar) | #f6f8fa | #010409 |
| rail item active fill / border | #d8e1f0 / #b9cae8 | #101826 / #1f2c44 |
| list row active fill (panel) | #dfe6f4 | #1b2331 |
| text primary | #2c3035 | #d8e0e6 |
| text secondary (nav idle, subtitles) | #575a5f (#595c60) | #aab1b6 (#afb6bb) |
| text tertiary (captions, breadcrumb root, dates) | #86888b | #81878d |
| empty-state title / desc | #414549 / #aaabad | #c5cbd1 / #5f6368 |
| faint (icons, section labels) | #c1c2c4 | #474b51 |
| placeholder (borderless search) | #d9dadb | #2f3439 |
| primary | #0053fd | #4a84fe |
| on-primary text | #ffffff | #161616 |
| primary soft (pill bg) | #e4ecfd | #131b2c |
| segmented track (usage / import) | #dfe6f4 / #d8e1f0 | #1b2331 / #101826 |
| log well bg / border | #f0f2f8 / #ced9ef | #151a22 / #232d40 |
| warning rule | #c08532 | #c08532 |
| success dot / badge bg / badge text | #00bc7d / #d8f4eb / #00d492 | #00bc7d / #0b2a25 / #00d492 |
| chart input / output | #6698fe / #4cd0a3 | #3155a0 / #04895d |
| dialog surface / border | #fbfbfb / #d3ddf2 | #15171c / #232d40 |
| input rest border (very faint) / focus | ~#e9e9e9 / #0053fd | ~#1c222e / #4a84fe |
| schedule summary well | #edeff5 | #1c2029 |
| session-import elevated surface / divider | #fdfdfd / #d9e2f3 | #16181d / #242c3c |
| info banner bg / border | #f1f4fc / #a8c3fc | #10141c / #213560 |
| persona editor border | #dae3f4 | #1d2535 |
| layout card surface | #fdfdfd | #15171d |
| mode/template card idle bg / border | #eff1f7 / #cdd8ee | #1c202a / #293347 |
| mode/template card active bg | #eef3fd | #171d2a |
| template thumb fill | #d0d4dd | #353b47 |
| edit-mode scrim / dashed outline | #ebf1fc / #6b9bfd | #0b111d / #2f529b |
| destructive text (disabled 50% seen) | #e68f95 | #83302f |

Prefer the foundation's token names over raw hexes; these values are what Compare should match.
