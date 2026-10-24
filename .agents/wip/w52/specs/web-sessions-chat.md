# W52 spec: web Sessions + Chat (slice `web-sessions-chat`)

Status: spec ready for the wave-52 page builder (written 2026-10-24, Read + Note phase; no code).
Read: reference screenshots `compare/hermes/web/{metrics-consent-banner,sessions,session-detail,sessions-search,
sessions-prune,empty-state,chat,chat-exchange,model-picker,chat-sidebar-closed}--{light,dark}.png`; ours
`compare/indus/web/{sessions,session-detail,sessions-search,sessions-prune,empty-state,chat,chat-exchange,
session-rename,session-delete}--*.png`; reference source read for structure/behaviour only (Sessions page, its
source table, the delete/confirm dialogs, the shared-metrics banner, the Chat page, chat side panel, chat session
list, workspace picker, copy-last button, model picker dialog). Our code read: `apps/web/src/pages/SessionsPage.tsx`,
`pages/sessions/{SessionDetail.tsx,sources.ts}`, `chat/{ChatHost,ChatRail}.tsx`, `chat/{terminal,eventsFeed}.ts`,
`shell/PageHeader.tsx`, `App.tsx`, `lib/{api,restTypes}.ts`, tests `SessionsPage.test.tsx`, `ChatRail.test.tsx`,
`e2e/{dashboard,chat}.spec.ts`.

Everything below is written in our words. Hex values were sampled from the 1440x900 @1x PNGs (stdlib PNG decode).
Sizes are px at that viewport. "fg" = the theme's main text colour, which is also the accent in both reference
themes (dark: cream on teal; light: blue on pale blue). Where a token name is given in `code`, use the name the
wave-51 web foundation actually ships (`docs/notes/ui-parity/web-foundation.md`, not landed when this was written:
`wt-w51a` had no diff yet) and map by role.

---------------------------------------------------------------------------------------------------------------

## 0. Shared palette for this slice (sampled)

| Role | Dark (teal preset) | Light (blue preset) |
|---|---|---|
| page / sidebar / card background `bg` | `#041c1c` | `#e8f2fd` |
| card fill (stats, inputs) — practically bg | `#041b1b` | `#e8f2fd` |
| primary text = accent `fg` | `#ffe6cb` | `#0053fd` |
| secondary text (meta, labels, nav) | `#ccbda7` | `#2f73fd` |
| tertiary text (previews, small meta) | `#a59e8c`–`#a79f8d` | `#518bfd`–`#588ffd` |
| placeholder text | `#828073` | `#73a2fc` |
| hairline: shell dividers (sidebar edge, header bottom) | `#36443f` | `#bad3fd` |
| hairline: card / row borders | `#293935` | `#c5dafd` |
| hairline: control borders (segmented, search idle) | `#283834` | `#c5d9fd` |
| input border, focused (2 px look: outer/inner) | `#4f5850` / `#424e47` | `#a2c2fc` / `#afcbfd` |
| source badge border | `#4f5850` | `#a2c2fc` |
| success green (Active count, live badge text, assistant label) | `#4ade80` | `#4ade80` |
| live badge fill / border | `#0e382a` / `#1f6a44` | `#d0efea` / `#a7eacb` |
| warning amber (tool rows, search marks) | `#ffbd38` (marks `#efb235`) | `#ffbd38` (marks `#fec144`) |
| destructive red (trash icon, Prune button) | `#fb2c36` | `#fb2c36` |
| on-red text | `#ffffff` | `#ffffff` |
| filled (selected) segment / primary button fill | `#ffe6cb`, text `#192d2b` | `#0053fd`, text `#dbe8fd` |
| hover row / nav hover fill | `#102624` | `#dce9fd` |
| dialog surface (prune) | `#041c1c` | `#e8f2fd` |
| dialog surface (model picker, a raised card) | `#0e2423`, border `#31403b` | `#dfecfd`, border `#bdd4fd` |
| dialog title colour (prune) | `#ffffff` | `#170d02` (near-black, not blue) |
| modal scrim | near-black ~75 % + background blur (`#010a0a` over page) | grey ~60 % + blur (`#5d6165` over page) |
| model-picker scrim | page bg at ~85 % (content barely visible, no blur) | page bg at ~85 % |
| chat terminal background / foreground | `#000000` / `#f0e6d2` | `#f5f8fc` / `#170d02` |

Radii: everything square (0) except: the chat terminal card (8 px), the small outlined buttons on the consent strip
and the floating terminal buttons (`copy last response`, `panel`: ~4 px), checkboxes (2 px). No card shadows except
the chat terminal card (large soft drop shadow, roughly 0 8 32 black at 40 %, visible as a ~20 px gradient ring) and
the dialogs (soft shadow).

Fonts (by role; the foundation picks the open fonts):
- `display-wide`: an extended, geometric uppercase-friendly sans for page titles ("Sessions", "Chat", the chat
  session title) and card titles ("RECENT SESSIONS", "PRUNE OLD SESSIONS"). Title 18 px; card title 18 px uppercase,
  letter-spacing ~0.05em.
- `serif-text`: a small, slightly quirky serif used for session titles, previews, segmented-control labels
  ("Chats", "Overview"), dialog descriptions, the model-picker footer note and title. 12–14 px, normal case.
- `sans`: a neutral grotesque for stat labels, meta lines, message bodies, transcript labels. 12–14 px.
- `mono`: JetBrains-Mono-like (OFL, `@fontsource/jetbrains-mono` is allowed) for: model ids in meta, all button
  labels in the page header/toolbar (bold, letter-spacing ~0.15em: "Prune old sessions", "Any chat source",
  "Import sessions", "Cancel", "Prune", "New chat", "Refresh Models", "Switch"), the chat rail (model name, session
  rows, workspace value), the model picker lists, the terminal.
- `condensed`: a tall condensed sans for the tiny source badges ("CLI", "Telegram") and the `live` badge, 11–12 px.
- Sidebar nav is foundation work (uppercase, tracked, 14 px).

Icons: line icons, 1.5 px stroke (lucide-style; use `lucide-react` at an exact pin if the foundation added it,
otherwise whatever icon set the foundation ships). Icons named below by description.

---------------------------------------------------------------------------------------------------------------

## 1. `sessions` — Sessions page, Overview view (default)

### Layout (1440x900, sidebar 240 wide incl. its 1 px right edge at x=239; header 52 tall + 1 px line at y=52)
- Page header (foundation): title "Sessions" at x=262, then a **count chip** right after the title (x=364–386,
  y=14–38: 23x25, 1 px border `#394640`/`#bad3fd`-ish, fill `#172b29`/`#d6e6fd`, number 12 px in tertiary colour).
  The chip shows the list total for the current filter; hidden while the list loads.
- Header end: outlined button **Prune old sessions** (x=1234–1418, y=12–39; 185x28; 1 px `fg` border; archive-box
  icon 14 px + mono bold 12 px tracked label in `fg`; transparent fill).
- Content column: left edge x=263 (23 px after the sidebar edge), right edge x=1407 (32 px from the window edge),
  top y=75 (22 px under the header line). Vertical gap between blocks 16 px. Page scrolls inside the column.
- Block 1 **stats strip** (y=75–130, 56 tall incl. 1 px border `#293935`/`#c6dafe`, padding 16 x 12): five stat
  groups left-aligned, gap 24 px: value above label.
  - value: 18 px semibold tabular, `fg`; "Active in store" value is green `#4ade80` in both themes.
  - label: 12 px sans, secondary colour.
  - Order/labels: **Total**, **Active in store**, **Archived**, **Messages**, **Sources** (Sources = number of
    distinct sources in the stats). Values come from `/api/sessions/stats` (`total`, `active`, `archived`,
    `messages`, `Object.keys(by_source).length`). Hide the strip until stats load.
- Block 2 **toolbar** (y=146–177, controls 30–32 tall, gap 12 px, wraps on narrow widths):
  1. Category segmented control (x=263–443): **Chats** (55 wide) | **Automation** (86) | **All** (39). 1 px
     border `#283834`/`#c5d9fd` around and between segments; selected segment filled `fg` with bg-coloured text;
     others transparent with secondary-colour text; labels in `serif-text` 12 px with ~0.05em tracking.
  2. **Source menu button** (x=455–641, 187 wide): 1 px `fg` border, list-filter icon left, mono bold tracked
     12 px label, chevron-down right (rotates 180° when open). Label: "Any chat source" (Chats), "Any automation
     source" (Automation, truncated with ellipsis at this width), "Any source" (All); after picking: the one source's
     label, "N sources", or "No sources".
  3. View segmented control **Overview** | **History** (x=653–792; same styling as 1). Shown only when the Overview
     has something to show (recent sessions or connected platforms) and no search is typed.
  4. In History view only: the search box (see `sessions-search`).
  5. **Import sessions** outlined button (x=805–938, 134x27, upload icon + `serif-text` 12 px bold label, 1 px `fg`
     border). Hidden while searching.
- Block 3 **Recent sessions card** (y=193–544): 1 px border; header strip 54 tall (clock icon 20 px in secondary
  colour + title "RECENT SESSIONS" in `display-wide` 18 px uppercase `fg`), 1 px divider, body padding 16, rows
  stacked with 12 px gap.
  - Each recent row: 1 px border box, padding 12, ~80 tall, x=279–1391. Left column: title (`serif-text` 14 px
    medium `fg`; untitled → first 60 chars of preview in italic secondary colour); meta line: model id (mono 12 px,
    last path segment) " · N msgs · 2d ago" (sans 12 px, secondary); preview line (`serif-text` 11–12 px tertiary,
    wraps) only when the session has both a title and a preview. Right, vertically centred: **source badge**
    (outlined 1 px `#4f5850`/`#a2c2fc`, 25 tall, database icon 12 px + `condensed` label "CLI"/"Telegram"; label
    colour `#f0d8c0`/`#0d5dfc`). Recent = the 5 newest sessions of the current filter that are not active.
  - When status lists messaging platforms (our `/api/status` `platforms`), a platforms card sits above Recent
    sessions (same card style: name + state pill per platform). Placeholder only if the builder runs out of time.

### States and behaviour
- First load: centred spinner in the content column (reference shows a full-page spinner, not a skeleton).
- Overview auto-refreshes every 5 s (status + newest 50 of the current filter); when the newest id changes, the
  History list refreshes silently. Use react-query `refetchInterval: 5000` on an overview query keyed by the filter.
- Category change resets page to 0, collapses any open row, clears the selection, closes the source menu.
- Source menu (popover under the button, 288 wide, 1 px border, bg, drop shadow): header row with the menu title
  ("Chat sources" / "Automation sources" / "Sources") in 12 px secondary and a **Clear** ghost button when a pick is
  active; then one row per source of this category (plus any picked one): checkbox, source icon in the source colour,
  label, count right-aligned tabular. Closes on outside pointer-down. Multi-select. Selection per category is kept
  separately. Mapping to our list API: none picked → the category's default filter; exactly one → `source`; several
  → `excludeSources` = all known sources minus the picked ones (our API has `source`, `sources`, `exclude_sources`;
  `sources=a,b` is simpler and fine).
- Overview/History toggle: History shows the full list (section 3); typing in search forces History.

### Source table (labels, icons, colours) — ours to write in `pages/sessions/sources.ts`
cli, tui → terminal-prompt icon, accent colour, label "CLI"/"TUI"; telegram → chat-bubble, blue (~`#3a93e6`);
discord → hash, violet; slack/email → message-square, green-teal; whatsapp/signal/sms → globe/chat bubble, success
green; matrix → chat bubble, blue; cron → clock, warning amber; tool, flows → play, amber; oneshot → terminal, amber;
api_server → globe, muted ("API server"); acp → database, muted ("ACP"); webhook → globe, amber; unknown → globe,
muted, label "local" when source is null. Our existing automation set (`cron, tool, oneshot, api_server, acp`) stays;
add `webhook` only if our backend emits it.

---------------------------------------------------------------------------------------------------------------

## 2. `metrics-consent-banner` — first-run strip above the whole shell

What the reference shows: a 32 px strip (plus 1 px bottom line) across the full window width, above both the sidebar
and the page (everything below shifts down 33 px). Fill `#102624`/`#dceafd`, bottom line `#3f4c45`/`#b0ccfd`. Left:
bar-chart icon 14 px `fg`, bold 12 px title, then one 12 px sentence in secondary colour with an underlined
"Details" link; right: three small outlined buttons (1 px `#3a4741`/`#b5d0fd`, ~4 px radius, 12 px `fg` text,
padding 2x8, gap 8) and an X icon button (tertiary colour, full opacity on hover).

**Gap: our backend has no shared-metrics feature** (no consent endpoint, no counters). Do NOT build the consent
banner. Decision for the builder (label it `W52-sc-1`): the slug is **N/A for Indus**; instead build a reusable
`TopStrip` component with exactly this look (height, fill, line, icon + bold title + sentence + small outlined
actions + dismiss X) and use it for our existing system-wide notice, the resource **PressureBanner**
(`shell/PressureBanner.tsx`, keeps its own dismissal rules), rendered full-width above the shell like the reference
strip. The `ProfileBanner` ("Managing profile …") may use the same component in the page column. If the
foundation already restyled these banners, only check the measurements. Compare row: "our strip vs their strip,
look only".

---------------------------------------------------------------------------------------------------------------

## 3. `session-detail` — History view with one row expanded

### History list rows (x=263–1407; each collapsed row 60 tall incl. 1 px border `#293935`/`#c5dafd`; gap 6 px)
Row header (padding 12, items top-aligned, gap 12; whole header clickable to expand/collapse; hover fill
`secondary/30`):
1. checkbox 16x16 (1 px `#36443f`/`#bad3fd`, 2 px radius) — selection only, never toggles expansion.
2. source icon 16 px in the source colour (terminal prompt for CLI, chat bubble blue for Telegram…).
3. text column: title (`serif-text` 14 px `fg`, truncates; untitled → preview italic secondary) followed, when the
   session is active, by a green **live** pill with a pulsing dot; meta line 12 px sans secondary:
   "model · N msgs · N tools (only if >0) · 2d ago" (separators are dots in the hairline colour, so they read as
   gaps); a search snippet line when searching (section 4).
4. right cluster (gap 8, vertically centred to the title): source badge (outlined, 25 tall, source icon + condensed
   label), then four ghost icon buttons ~32x32, icons 16 px in secondary colour: **play** (resume in Chat; hover
   green), **pencil** (rename), **download** (export JSON), **trash** (delete, icon red `#fb2c36`).
- Row tint: selected → accent-tinted border + very light accent fill; active (live) → faint green border/fill;
  otherwise plain.
- Inline rename (replaces the title): 28 px tall input, autofocus, Enter saves, Escape cancels, then a check icon
  button (spinner while saving) and an X icon button. Empty or unchanged value just closes.

### Expanded transcript (inside the same bordered row)
- 1 px divider under the header, area fill ≈ bg at 50 %, padding 16; messages in a column with ~11 px gap; the area
  scrolls internally at ~600 px max height and auto-scrolls to the first search hit.
- Loading: centred spinner (32 px band). Error: centred red 14 px text. Empty: "No messages" centred muted.
- Message block (no border, no radius, padding 12, full width):
  | role | fill dark | fill light | label colour |
  |---|---|---|---|
  | user | `#1c2f2d` | `#d0e1fc` | `fg` |
  | assistant | `#0a2f25` | `#d8eff0` | green `#4ade80` |
  | tool | `#1c2b1e` | `#eaebe9` | amber `#ffbd38` |
  | system | muted grey fill | muted | secondary |
  Label row: bold 12 px label ("User", "Assistant", "Tool: <tool name>", "System") + 12 px tertiary relative time
  (when the message has a timestamp) + an amber "match" pill when the block contains the search term (block also
  gets a 1 px amber ring). Body: sans 14 px `fg`, relaxed line height, light Markdown (our `markdownBlocks` is enough:
  paragraphs, headings, fenced code; inline code in mono with a faint tint `#0e2b26`/`#daebf9`).
- Tool call inside an assistant block: a collapsible strip (margin-top 8; 1 px amber border at ~20 %
  `#445128`/`#e2e5c4`; fill amber 5 % `#163726`/`#daede7`; 31 tall): chevron-right (down when open) + tool name in
  mono 12 px amber, call id right-aligned in amber at 50 %. Open → `pre` with the arguments, mono 12 px amber 80 %,
  wraps, top border amber 20 %.
- Tool result messages render as their own amber "Tool: name" block (not folded, unlike ours today).

---------------------------------------------------------------------------------------------------------------

## 4. `sessions-search` — searching

- In History view the toolbar holds a search box after the view toggle (x≈805–1225, 32 tall, 420 wide, flex-1 up to
  ~448): search icon 14 px at left 10, input text mono 12 px, placeholder "Search message content..." in placeholder
  colour; when focused the border reads 2 px (`#4f5850`+`#424e47` / `#a2c2fc`+`#afcbfd`). While typing a non-empty
  query the toggle disappears and the search box moves next to the source button (x=652–1073), the Import button
  hides, and a small X clear button (12 px icon) shows inside the right edge. While a request runs the search icon
  becomes a spinner.
- Search runs **as you type** (300 ms debounce), with the current category/source filter; no Search button.
- Results **replace** the list (no separate "Matches for" card): same row component, plus a snippet line under the
  meta (`serif-text` 11–12 px tertiary, truncated) with each match marked: mark fill `#776628`→`#555024` (dark,
  amber 30 %) / `#f2daa2` (light), mark text amber. Opening a result row expands its transcript with the term
  highlighted and the first hit scrolled into view.
- No results: empty state (section 6) with "No sessions match your search".
- Our API: `GET /api/sessions/search?q=` returns `{results:[{session_id,title,source,snippet,role,last_active}]}` —
  fewer fields than a list row (no model, counts). Builder: render result rows by joining with the current list
  (`api.sessions.one(id)` per hit only when not already in the loaded page, cache with react-query), or show the hit
  row with just title/source/time/snippet. Our search ignores category/source filters today — filter the hits
  client-side by `source` with the same category rules; do not add server parameters.

---------------------------------------------------------------------------------------------------------------

## 5. `sessions-prune` — Prune dialog

- Opened from the header **Prune old sessions** button. Centered dialog x=540–899 (360 wide), y=347–552; 1 px border
  `#293935`/`#c5dafd`; surface = page bg; scrim near-black with blur (dark) / grey with blur (light).
- Header block (padding 16): title "PRUNE OLD SESSIONS" `display-wide` ~15 px uppercase, colour white (dark) /
  near-black `#170d02` (light); close X icon top-right (12 px, tertiary). Description `serif-text` 11–12 px tertiary
  (`#949080` / `#6397fd`), two lines. Use our sentence (already ours): "Remove ended sessions older than a number of
  days. Open sessions are never removed."
- 1 px divider, then label "Older than (days)" (sans 12 px secondary) and a full-width number input (34 tall, 1 px
  border `#434e47`/`#aecafd`, native spinner at right; value preselected; default 90). Note: in the reference the
  label and input touch the dialog's side borders (no side padding); match that only if it does not look broken in
  our build — otherwise keep 16 px side padding (minor).
- Footer (padding 12, right-aligned, gap 8): **Cancel** outlined (97x37, 1 px `fg` border, mono bold tracked 14 px
  `fg`), **Prune** filled red `#fb2c36` with white mono bold label (96x37). Enter in the input = Prune. While running:
  spinner inside Prune, both disabled, dialog not dismissable.
- Behaviour kept from ours (decision `W52-sc-2`): our prune is two-step (dry run then real run) and refuses while the
  daemon runs (409 `daemon_running`). Keep the safety but drop the visible "Check" button: run the dry run
  automatically when the dialog opens and 300 ms after the days value changes; show one 12 px line under the input
  ("N sessions would be removed (M open kept)." or the validation "Enter a whole number of days."); **Prune** is
  enabled when the preview count > 0 and runs the real prune, then toasts "Removed N sessions." and refreshes
  sessions + stats. Conflict text goes in a danger notice above the footer. Update `SessionsPage.test.tsx` (it clicks
  "Prune…", "Check" and "Prune 3").

---------------------------------------------------------------------------------------------------------------

## 6. `empty-state` — Automation filter with nothing in it

- Toolbar: Automation segment selected; source button reads "Any automation s…" (truncated); **no** Overview/History
  toggle (nothing for Overview); search box (x=676–1095) and Import button (x=1107–1241) present.
- Body: centred column 60 px below the toolbar: clock icon 32 px at 40 % opacity (`#545d54`/`#9dbefc`), 12 px gap,
  message 14 px medium secondary "No sessions in this filter". Variants: "No sessions match your search" (query);
  "No sessions yet" + a 12 px tertiary second line in our words (e.g. "Chats you start appear here.") for Chats with
  no source pick.
- Compare note: our current `empty-state` capture is a different scenario (second profile). Re-capture ours on the
  default profile with the Automation segment selected for the side-by-side; the other-profile view stays a valid
  extra shot.

---------------------------------------------------------------------------------------------------------------

## 7. History extras (no slug, but part of the same page; build if time allows, in this order)

- Selection bar (appears above the list when ≥1 row is checked): 1 px accent 30 % border, accent 6 % fill, padding
  8x12: "N selected" (12 px accent), ghost "Select all on page", ghost "Clear selection", and at the right an outlined
  red "Delete N selected" with trash icon → confirm dialog → our `api.sessions.remove(id)` per id (sequential, stop
  and show the conflict text on the first 409). Shift-click selects a range.
- Pagination: page size 20 (ours is 50 — use 20 to match). Compact pager (chevron-left icon button, "Page X of Y"
  12 px muted, chevron-right) at the right end of the toolbar; full pager under the list ("1–20 of N" left, compact
  pager right). Shown only when total > page size and not searching.
- "Delete empty (N)" button: **gap** (no empty-session count endpoint). Leave it out.
- Delete one session: confirm dialog (same dialog frame as Prune): title "Delete session?", our description
  (`“<title>” and its messages are removed for good.`), Cancel outlined + Delete filled red; conflict notice inside.
- Import sessions: **gap** — our backend has no session import endpoint (`/api/ops/import-upload` restores a whole
  backup archive; do not repurpose it). Show the button disabled with a tooltip in our words (e.g. "Not available
  yet") so the toolbar matches; no file picker. Decision `W52-sc-3`.

---------------------------------------------------------------------------------------------------------------

## 8. `chat` — Chat page, new session

### Layout
- Header: title "Chat" (`display-wide` 18 px). When the live feed reports a session title (`session.title` events,
  already parsed into `LiveSession.title` in `ChatRail.tsx`), the header title becomes that title (chat-exchange
  shows "What is in the cron folder?"); back to "Chat" for an untitled/new session and when leaving the page. Needs
  a title override in the shell header (see Mapping).
- Content padding: top 15 (card top y=68), left 23 (x=263), right 27, bottom 0 (card runs to the window bottom).
- **Terminal card** x=263–1180 (flex-1), radius 8, padding 12 (8 on small screens), background = terminal bg
  (`#000000` dark, `#f5f8fc` light), big soft shadow. The xterm fills it. Terminal font mono, fg `#f0e6d2` dark /
  `#170d02` light.
- Floating **copy last response** button inside the card, bottom-right (inset 16 at ≥1024 px; x≈1002–1165,
  y≈857–884): 1 px border in terminal-fg at 30 %, 4 px radius, fill black 20 % (reads `#d3d6d9` on light), copy icon
  12 px + mono 12 px "copy last response" (→ "copied" for 1.5 s), colour terminal fg at ~70 % opacity, full on
  hover. Text hidden under 400 px width (icon only). Ours copies `feed.lastAnswer` (keep that data path; keep the
  accessible name "Copy last answer" so `ChatRail.test.tsx` still finds it, or update the test). Disabled look
  (40 % opacity) when there is no answer yet.
- **Side panel** (≥1024 px), 240 wide column (content x=1193–1413), gap 12 from the card, flex column gap 12,
  scrolls internally:
  1. top row 32 tall, right-aligned **X** ghost icon button "Collapse side panel" (x=1393–1406, y=76–89).
  2. **Model card** (x=1193–1413, y=109–159, 1 px border `#293935`/`#c5dafd`, padding 8x12): left: "MODEL" label
     (`display-wide`/caps 12 px tracked, tertiary `#a79f8d`/`#5088f8`), under it the model name as a text button
     (mono 14 px medium `fg`, truncates, chevron-down 14 px secondary; underline on hover) that opens the model
     picker (section 10). Right: connection badge (condensed 12 px; `live` green on `#0e382a`/`#d0efea` with border
     `#1f6a44`/`#a7eacb`; `connecting` amber; `idle`/`closed` neutral; `error` red).
  3. Optional reasoning card (only when the model supports it — see Mapping), same card style.
  4. Notice cards when needed: amber (model changed — start a new chat to use it), red (feed error) with an outlined
     small "Reconnect side panel" / "Reload page" button. Our feed notice + Reconnect/Reload buttons go here.
  5. **SESSIONS** header row (y=174–194): caps 12 px tertiary label + refresh icon button (spins while loading).
  6. **WORKSPACE** block: folder-git icon 14 px + caps 12 px tertiary label "WORKSPACE" + refresh icon at the right;
     a select (x=1200–1409, 34 tall, 1 px border, mono 14 px `fg` value like "Default · /tmp/clau…", chevron at
     right) with options: default, known folders, the live cwd, "Custom…" → inline input (Enter/blur commits,
     Escape cancels).
  7. **New chat** outlined button full width (x=1200–1409, y=281–308, 28 tall, message-plus icon left, mono bold
     12 px tracked "New chat" centred).
  8. Session list (fills the remaining height, scrolls): rows 48 px pitch, padding 6x8, 4 px radius: title mono
     14 px medium secondary (`#ccbda7`/`#2e72fc`), truncating; meta 11 px tertiary "just now · 4 msgs · local"
     (msgs only if >0; source only when not "cli"). Current session: accent 10 % fill + 2 px accent left border, text
     `fg`. Hover: faint fill, text `fg`. Loading: spinner + "Loading…"; error: red line + outlined "Retry";
     empty: centred 12 px "No sessions yet".
- Below 1024 px: no side column; a header-end button "Model & tools" (panel-right icon, 12 px, 1 px border 20 %)
  opens the same panel as a right drawer (our current "Details"/"Close" drawer, restyled).

### Behaviour (ours, unchanged)
PTY over `/api/pty`, events feed for the rail, `?resume=` for resuming, our reconnect/ended overlays (restyle them
to the reference look: amber-bordered box top-right on black 80 % for reconnecting/disconnected with an outlined
"Reconnect now"; full-card scrim black 60 % with centred message + "Start new session" for ended — keep that exact
button name, the e2e and unit tests click it).

---------------------------------------------------------------------------------------------------------------

## 9. `chat-exchange` and `chat-sidebar-closed`

- `chat-exchange`: same frame as `chat`; the transcript inside the terminal is our TUI's own rendering (out of scope
  for this slice — the TUI look is the CLI's). Visible differences to close here: header title = session title;
  rail session list shows the new titled session at the top with "just now · 4 msgs · local"; badge stays `live`.
- `chat-sidebar-closed`: the X collapses the side panel; the terminal card then spans x=263–1417. A floating
  **panel** button appears inside the card top-right (x=1339–1405, y=79–102; same style as copy-last: 1 px
  terminal-fg 30 % border, black 20 % fill, panel-right icon 12 px + mono 12 px "panel", 70 % opacity) and reopens
  it. Persist the collapsed state per browser (localStorage key e.g. `indus.chat.panelCollapsed`, every read/write in
  try/catch). The terminal refits on toggle (our fit addon already reacts to resize; make sure a ResizeObserver
  covers the card, not only the window).

---------------------------------------------------------------------------------------------------------------

## 10. `model-picker` — Switch model dialog

### Layout (dialog x=360–1079 → 720 wide; y=90–809 → 720 tall = 80 % of viewport; centred; 1 px border; raised
surface `#0e2423`/`#dfecfd`; scrim = page bg at ~85 %; big shadow)
- X close icon button top-right (x≈1051, y≈106).
- Header (72 tall incl. 1 px divider; padding 20/20/12): title "SWITCH MODEL" in `serif-text` 14 px caps tracked
  `fg`; line under it in mono 12 px secondary: "current: <model> · <provider>".
- Filter row (padding 12x20, 1 px divider under): input 32 tall full width with search icon at left, placeholder
  "Filter providers and models…" in mono 14 px; filters both columns, fuzzy-ish (matched letters drawn accent, bold,
  underlined in model ids).
- Body: two columns, provider list 200 wide (1 px right border) | model list (rest). Both scroll.
  - Provider row 45 tall, padding 6x12, mono 12 px: name (medium) + "CURRENT" tag (caps 12 px accent) on the
    provider in use; second line "<slug> · N models" secondary. Selected row: fill `#263734`/`#c8dcfc` + 2 px `fg`
    left bar, text `fg`.
  - Model row 28 tall, mono 12 px secondary, padding 6x12: check icon 12 px (accent when selected, else invisible),
    id, "CURRENT" tag at the right for the model in use. Click selects, double-click applies.
  - Column states: loading (spinner + "loading…"), error (red 12 px), no providers (12 px muted, our sentence + links
    to Keys and Models pages), no match (italic muted).
- Footer (padding 12, 1 px divider, items spaced): left note `serif-text` 11 px secondary in our words, e.g. "Saved
  to config; new chats use it."; right buttons (gap 8): **Refresh Models** outlined (refresh icon + mono bold tracked
  label), **Cancel** outlined, **Switch** filled (`fg` fill, bg text; disabled until a different model is chosen —
  disabled fill `#31403b`/`#bdd4fd` with `fg` text). Spinner inside Switch while applying.

### Data (ours) — `W52-sc-4`
- Providers: `api.model.options()` → `providers[]` (`name`, `label`, `configured`, `supports_model_listing`) and
  `current {provider, model}`. Show configured providers first, then the rest (or only configured — builder's call;
  reference lists all with "0 models").
- **Gap: no per-provider model list in our API.** Model column: for the current provider show the current model
  (with CURRENT); for any selected provider also show a one-line mono input "model id" (Enter = choose) so a person
  can type the id, which is how our Models page sets it (`api.model.set` takes any string). Provider row meta reads
  "<name>" + "· N models" only when we know N (current provider: 1); otherwise just the slug. Do not invent a
  listing endpoint.
- Apply: `api.model.set({scope:"main", provider, model})` for the active profile, then invalidate
  `["chat","model-info"]` and `["models", …]` queries, close, and show the amber rail notice (our words: "Model set to
  X. Start a new chat to use it."). Refresh Models = refetch `api.model.options()`. No expensive-model confirm (our
  API has no such flag).

---------------------------------------------------------------------------------------------------------------

## 11. Mapping to our code

| File | Change |
|---|---|
| `apps/web/src/pages/SessionsPage.tsx` | Rebuild per sections 1, 3–7: header count chip + "Prune old sessions" (via `HeaderActions` + a new title-addon slot), stats strip, toolbar (category segmented, source menu, Overview/History, debounced search, Import placeholder, compact pager), Overview (recent card, platforms card), History list, selection bar, dialogs. Split into `pages/sessions/{StatsStrip,SessionsToolbar,SourceMenu,SessionRow,RecentSessions,PruneDialog}.tsx`. Keep `data-testid="session-<id>"`, list `aria-label="Sessions"` (→ "Search results" while a query is active), input `aria-label="Search sessions"`. |
| `apps/web/src/pages/sessions/SessionDetail.tsx` | Role-tinted blocks, "Tool: name" amber blocks, collapsible amber tool-call strips (label = tool name; keep the text "Tool call" somewhere accessible or update the test), timestamps, match pill + ring, 600 px internal scroll, centred loading/error/empty. Keep `SnippetText`/`MarkedText` (mark styling changes). |
| `apps/web/src/pages/sessions/sources.ts` | Add source label/icon/colour table and `sourceBelongsToCategory`; keep `AUTOMATION_SOURCES`, `filtersFor` (extend for multi-pick), `CONFLICT_MESSAGES`. |
| `apps/web/src/shell/PageHeader.tsx` | Coordinate with the foundation: (a) an after-title slot (`HeaderTitleAddon` portal, like `HeaderActions`), (b) a title override (`useHeaderTitle(title \| null)` context) used by Chat. If the foundation already added these, use theirs. |
| `apps/web/src/chat/ChatHost.tsx` | Terminal card frame (radius, padding, terminal bg, shadow), floating copy-last and panel buttons, collapsible side panel (240 wide, persisted), narrow-screen "Model & tools" drawer, restyled reconnect/ended overlays, header title from the feed. |
| `apps/web/src/chat/ChatRail.tsx` | Model card with feed-phase badge (fixes the "always answering" bug: badge = `feed.status.phase` → live/connecting/retrying/stopped, not "a model was seen"); model button opens the picker; SESSIONS header with refresh icon; workspace select restyle with icon + refresh + "Custom…" inline input; New chat outlined full-width; session rows restyle; reasoning picker as a card; move "Copy last answer" to the host overlay (lift `feed.lastAnswer` up or pass a callback); remove the visible dashed image dropzone — keep paste/drop on the terminal and rail (already handled by `attachImage`; tests call it directly). |
| `apps/web/src/chat/ModelPickerDialog.tsx` (new) | Section 10, on our Radix dialog primitive (`@radix-ui/react-dialog` already a dependency) with the foundation's dialog skin. |
| `apps/web/src/chat/terminal.ts` | Terminal background/foreground per theme: dark `#000000`/`#f0e6d2`, light `#f5f8fc`/`#170d02` (via foundation tokens if they define terminal colours; otherwise read new CSS vars). ANSI palette is out of scope. |
| `apps/web/src/shell/PressureBanner.tsx` (+ `ProfileScope.tsx` banner) | Optional: `TopStrip` look (section 2), only if the foundation did not already restyle banners. |
| Tests | `pages/SessionsPage.test.tsx`: "Prune…" → "Prune old sessions"; no "Check"/"Search" buttons (debounce: use fake timers or `findBy*`); Delete/Rename/Export become icon buttons — give them `aria-label` "Delete"/"Rename"/"Export JSON" so most pins survive; Resume keeps `aria-label="Resume <title> in Chat"`; Source `<select>` becomes a menu button (update the source-filter test); "Tool call: read_file"/"Result from read_file" texts change. `chat/ChatRail.test.tsx`: `chat-model` testid + `data-source` kept on the model card; "No model configured" kept as the empty label; "Copy last answer" accessible name kept on the overlay button; "New chat", "Reload page", "Start new session", "Reasoning effort" label kept. `e2e/dashboard.spec.ts`: drop the "Search" click (type and wait), results list name. `e2e/chat.spec.ts`: should pass unchanged. |

Data available vs missing (never invent backend features):
- Available: list (`/api/sessions` with source/sources/exclude_sources/order/limit/offset), stats incl. `archived`,
  search, messages, rename, delete (with mutation identity), export, prune (dry run), status platforms, model info,
  model options, model set, feed events (model, provider, reasoning, cwd, title, session id), files upload for images.
- Missing → handling: shared-metrics consent → N/A (TopStrip reuse); session import → disabled placeholder button;
  empty-session count/bulk delete-empty → omit; bulk delete → client loop over `remove`; per-provider model list →
  current model + typed id; search filtered by source → client-side filter; message timestamps only when the store
  has them; per-session `profile` in rows (reference shows it) → not needed, the page is profile-scoped already.

Known Indus bugs in this area (from `compare/indus-ui-bugs.md`): chat rail badge always "answering" → fixed by the
phase badge above. "Sessions stay active after the CLI exits" is a backend `is_active` matter → out of scope; note it.

---------------------------------------------------------------------------------------------------------------

## 12. Build order

1. Read the foundation note; confirm tokens for the palette in §0, fonts by role, Button (outlined/filled/ghost/
   destructive, icon size), Segmented, Badge (outline/success), Card (+ header), Input, Checkbox, Dialog, Toast,
   empty state. Add missing roles to the foundation's token set only via its documented extension point.
2. Header slots (title addon, title override) — small, unblocks both pages.
3. `sources.ts` table → `SessionRow` (history row + inline rename + icon actions) → `SessionDetail` restyle.
4. Toolbar: category segmented, source menu popover, Overview/History, debounced search, Import placeholder.
5. Overview: stats strip, recent card (+ platforms card), 5 s refresh; empty states.
6. Prune dialog (auto dry run), delete confirm, selection bar + pagination.
7. Fix `SessionsPage.test.tsx` and `e2e/dashboard.spec.ts`; run vitest/tsc/biome.
8. Chat: terminal card + colours → side panel restyle (model card + phase badge, sessions, workspace, new chat) →
   collapse + panel button + persistence → copy-last overlay → header title from feed → narrow drawer.
9. Model picker dialog + wiring + rail notice.
10. Fix `ChatRail.test.tsx`; run chat e2e.
11. TopStrip for the pressure banner (optional).
12. Re-capture both themes with a copy of the web kit (add an Automation-filter empty shot, History+expanded,
    search "cron"-like term, prune open, chat new, chat after one turn, model picker open, panel collapsed) and build
    the side-by-side PDF; iterate.

## 13. Compare checklist (tick per theme: L = light, D = dark)

Sessions (`sessions`)
- [ ] L [ ] D header: display title, count chip right of title, "Prune old sessions" outlined with archive icon at right
- [ ] L [ ] D content edges x=263/1407, top y=75, 16 px block gaps
- [ ] L [ ] D stats strip: 5 groups, 18 px values, green Active value, 12 px labels, 1 px border box 56 tall
- [ ] L [ ] D toolbar: Chats/Automation/All segmented (filled selected), source menu button with filter icon + chevron, Overview/History segmented, Import sessions button
- [ ] L [ ] D Recent sessions card: clock + wide caps title, divider, bordered rows (serif title, mono model meta, preview), outlined source badge with icon on the right
- [ ] L [ ] D colours match §0 (bg, fg, secondary, hairlines)
Session detail (`session-detail`)
- [ ] L [ ] D History rows 60 tall: checkbox, source icon, serif title, meta with gaps, badge + play/pencil/download/red trash
- [ ] L [ ] D expanded: divider, role-tinted blocks (user/assistant/tool fills from §3), bold coloured labels + time
- [ ] L [ ] D tool-call strip amber with chevron, name left, call id right
Search (`sessions-search`)
- [ ] L [ ] D search box replaces the view toggle, X clear inside, no Import button, no Search button
- [ ] L [ ] D result rows show snippet with amber marks; list replaced (no separate results card)
Prune (`sessions-prune`)
- [ ] L [ ] D 360 wide dialog, wide caps title (white / near-black), serif description, label + full-width number input, Cancel outlined + red Prune
- [ ] L [ ] D scrim: dark blurred / grey blurred
- [ ] dry-run preview line works; daemon-running notice shows
Empty (`empty-state`)
- [ ] L [ ] D Automation selected, no view toggle, search + Import present, centred faded clock + "No sessions in this filter"
Consent strip (`metrics-consent-banner`)
- [ ] N/A recorded (no metrics feature); [ ] TopStrip look used for the pressure banner (if built)
Chat (`chat`, `chat-exchange`)
- [ ] L [ ] D terminal card: radius 8, black / #f5f8fc, shadow, 12 px padding, terminal fg colours
- [ ] L [ ] D copy last response floating button bottom-right
- [ ] L [ ] D side panel 240: X, model card (MODEL caps label, mono model + chevron, phase badge `live` green), SESSIONS + refresh, WORKSPACE + icon + refresh + select, New chat outlined, mono session rows with meta, current row accent
- [ ] header title switches to the session title after the first turn; back to "Chat" on New chat
- [ ] badge shows connecting/live/closed, never "answering"
Panel closed (`chat-sidebar-closed`)
- [ ] L [ ] D card full width, "panel" floating button top-right, state survives reload
Model picker (`model-picker`)
- [ ] L [ ] D 720x720 raised dialog, page-bg scrim, caps serif title, mono "current:" line, filter input with icon
- [ ] L [ ] D provider column 200 wide, selected row fill + 2 px bar, CURRENT tags; model column rows with check icon
- [ ] L [ ] D footer note left; Refresh Models / Cancel outlined; Switch filled (disabled until a change)
- [ ] switching saves via `/api/model/set` and the rail shows the amber "start a new chat" notice
Checks
- [ ] biome, tsc, vitest (web), web Playwright e2e, `python -m tooling.checks --require-ref` (similarity/banned/licenses/assets)
- [ ] no reference strings longer than 4 words in UI copy; no Hermes/Nous names; no reference assets
