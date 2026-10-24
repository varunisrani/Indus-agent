# Wave 52 spec — desktop slice `desktop-composer-menus`

Status: SPEC (Read + Note done 2026-10-24; reference closed from here on).
Read: reference screenshots `compare/hermes/desktop/{slash-menu,mention-menu,model-picker,effort-picker,composer-help,
composer-attach-menu,command-palette,session-switcher,session-sidebar,context-menu}--{light,dark}.png`; reference
source (behaviour only) under `apps/desktop/src`: composer completion drawer / trigger popover / help hint / attach
menu / model pill / reasoning pill / model catalog menu / reasoning menu panel, floating HUD constants, command palette,
session switcher + its store, shared menu tokens, sidebar session-actions menu, sidebar filter menu.
Ours read: `apps/desktop/src/chat/composer/{Composer.tsx,completion.ts,attachments.ts}`, `chat/ModelPicker.tsx`,
`chat/slash.ts` + `slashCatalog.json`, `contrib/areas.tsx` (CommandPalette), `app/Shell.tsx`, `app/chatSlots.tsx`,
`app/StatusBar.tsx` (`useSessionFacts`), `sidebar/{Sidebar.tsx,sessionList.ts,projects.ts,sidebar.css}`,
`panes/shortcuts.ts`, `settings/keybindings.ts`, `electron/contextMenu.ts`, backend `service/path_completion.py`,
`core/references.py`, `web/routes/sessions.py` (SessionPatch), protocol `ModelOptionsResult`/`SessionInfo`.

All measurements are from the 1440x900 @1x reference shots. Colours are sampled pixels (light = "Nous Blue" look,
dark = default dark). Where a value is the foundation's job (tokens, composer bar, sidebar frame) it is listed so
you can check the foundation matches; use the foundation's token names (see `docs/notes/ui-parity/desktop-foundation.md`
once wave 51b lands) rather than literal hex in components.

Clean-room reminders: no reference code/classes/CSS; labels of 1–4 words may match; every longer sentence below
marked "(our words)" is a suggestion written by us. No `Hermes` text anywhere; our product is "Indus Agent".

---

## 0. Shared building blocks (build these first, used by every slug)

The reference has exactly three floating-surface families. Make one component (or one class set) per family.

### 0.1 `ComposerPopover` — the composer drawer (slash, mention, help)
- Anchored to the composer: left edge = composer left + 3 px (composer x=276, drawer x=279), bottom edge = 4–5 px
  above the composer's top border (drawer bottom y=824, composer top y=829). Opens upward; flips below if no room.
- Width 320 px (max: composer width − 16). Max height 352 px or (viewport − 128), whichever is smaller; scrolls inside
  (thin themed scrollbar; scroll keeps the highlighted row in view WITHOUT scrolling the transcript).
- Padding 4 px. Corner radius ≈ 3 px (corners are almost square in the shots). 1 px border. Soft drop shadow
  (≈ 0 4px 12px rgba(0,0,0,.08) light; barely visible on dark).
- Colours: bg light `#fcfcfd` / dark `#0d0f13`; border light `#dfe4e9` / dark `#23272e`.
- Group header: 10 px, semibold, UPPERCASE, letter-spacing ≈ 0.05em, colour light `#85878a` / dark `#81878b`,
  padding 2 px top (first) / 8 px top (later groups), 8 px sides, 2 px bottom.
- Row: height 28 px (4 px vertical padding + 20 px line), 8 px side padding, 8 px gap, radius 6 px, full width.
  Content: 16 px icon box (icon 14 px) · name (12 px, weight 500, colour light `#292d33` / dark `#d9e0e6`, no shrink
  beyond truncation) · description (12 px, regular, flex 1, truncated with ellipsis, light `#85878a` / dark `#81878b`).
- Highlighted row (keyboard or hover): bg light `#dde4f3` / dark `#1b212f`; text keeps its colours.
- Mouse-down inside the drawer must not blur the editor (prevent default).
- Empty state (no items): 12 px tertiary text, 12 px padding: "No matches" + a hint line "Try @file: or @folder:" (for
  `@`) / "Try /help" (for `/`). Loading state (items pending > ~150 ms): a small spinner + "Looking…".

### 0.2 `MenuSurface` — dropdown/context menus (model, effort, attach, session row menu)
- Use a Radix-style dropdown/context menu (radix-ui is on the allowed list) so focus, typeahead, submenus and Esc
  work; or extend whatever the foundation ships as its Menu.
- Radius ≈ 3 px, 1 px border, shadow (light: visible grey falloff ~6 px below; dark: dark falloff).
- Colours: bg light `#fdfdfd` / dark `#15171c`; border light `#c7d4f0` / dark `#29344c` (accent-tinted hairline);
  separator 1 px light `#d9e2f3` / dark `#232b3b`, full width, 4 px margin above/below.
- Row: height 24 px (4 px vertical padding, 16 px line), 10 px side padding, 8 px gap, 12 px text, icons 14 px.
  Text light `#2c3035` / dark `#d9e0e6`; icons light `#45494d`–`#595c60` / dark `#aeb4bb`–`#c8cfd5`.
  Hover/focus row: bg light `#e6ebf5`-ish (the active-control tint; use the foundation's "control active" token) /
  dark `#1f2533`-ish. Disabled: 50 % opacity, no pointer.
- Section label: 10 px, weight 500–600, UPPERCASE, wide tracking, light `#585b5f`–`#86888b` / dark `#858a90`–`#afb6bb`,
  padding 4 px top / 2 px bottom / 10 px sides.
- Destructive row: text light `#cf222e` / dark `#f85149`, icon the same red.
- Submenu trigger: chevron-right 16 px at the far right, colour light `#8d8f92` / dark `#80858b`.
- Open/close: 120–150 ms fade + 95 % zoom + 4 px slide from the anchor side.

### 0.3 `FloatingHud` — command palette and session switcher
- Fixed, horizontally centred in the window, top 12 px (below the title bar when the window is narrower than 704 px).
  No dimming backdrop: a transparent full-window click-catcher closes it.
- Radius ≈ 3–4 px, 1 px border light `#f6f6f6` (nearly invisible; the shadow carries the edge) / dark `#141c27`,
  shadow ≈ 0 8px 24px rgba(0,0,0,.10) light, deeper on dark. Bg light `#fcfcfc` / dark `#0f1621`.
- Must be `-webkit-app-region: no-drag` (it overlaps the title-bar drag band).
- Row: 12 px text, 8 px sides, 4 px vertical padding (24 px rows; 26 px when a key-cap chip is present), 8 px gap,
  radius 4–6 px. Selected row bg light `#e3edff` / dark `#17243a`; text light `#1f2328` / dark `#e6edf3`.
- Group heading: plain text (no sticky bar), 10 px (0.64rem), semibold, UPPERCASE, letter-spacing 0.16em, accent
  colour light `#0053fd` / dark `#4a84fe`, padding 10 px top / 4 px bottom / 10 px sides.
- Key-cap chip (`Kbd`, used here, in composer help and in the sidebar "New session" row): height 16–17 px, min width
  16 px, 4 px side padding, 10 px text, radius 3 px, 1 px border; light bg `#eaeff7` border `#c9cfd7` text `#b3b8c2`
  (palette) / sidebar & help variant bg `#f0f1f1`–`#f3f4f5` border `#c9d5e9`; dark bg `#1d2430` border `#3c4451` text
  `#535b67` (palette) / `#12141a`–`#222429` border `#2f384b` (help, sidebar). Combos render as separate caps 4 px
  apart: `Ctrl` `Shift` `N` (on macOS the symbols ⌘ ⇧).

### 0.4 Tooltip (used on session rows, slash rows, pills)
- Inverted: light bg `#2c3035` text `#fefefe`; dark bg `#d8e0e6` text `#0d1015`. 12 px text, 4 px × 8 px padding,
  radius 3 px, a 6 px arrow pointing at the anchor. Delay ≈ 400 ms. Max one line, ellipsis.

---

## 1. `slash-menu` — typing `/` in the composer

### Reference
- Drawer (0.1) at x 279–598, y 473–824 (scrolls; 12+ rows visible).
- First group header "SESSION"; rows like `/new`, `/save`, `/retry`, `/undo`, `/title`, `/branch`, `/compress`,
  `/rollback`, `/stop` with their one-line description truncated. Rows are grouped by category, in catalogue order
  (not alphabetical), each new category gets its own header.
- Every row has the same "terminal" glyph (a small square with `>_`) in accent blue (light `#1254da` / dark `#5f8dee`).
  Skills would get a different glyph and colour; plain commands use this one.
- First row highlighted. Hovering a row ≥ 400 ms shows the full description in a tooltip to the right.
- Keys: ↑/↓ move (wraps), Tab/Enter take, Esc closes; typing filters.

### Ours today
- `Composer.tsx` lines ~420–460: absolute list `w-96 max-h-60`, monospace 14 px command, accent-filled selection,
  alphabetical order from `complete.slash`, no groups, no icons. Arrow keys/Tab/Enter/Esc already work
  (`CompletionSource`, `applyCompletion`).

### Build
- Replace the inline list with `ComposerPopover` (0.1). Keep `role="listbox"` / `role="option"` /
  `aria-activedescendant` wiring and the `listId`.
- Grouping: `complete.slash` returns `CompletionItem{text,display,meta}` without a category. Look the command name up
  in the bundled `slashCatalog.json` (already imported by `slash.ts`; add a `categoryOf(name)` helper there) and
  order groups by first appearance in the catalogue: session → config → tools → info. Header labels: "Session",
  "Config", "Tools", "Info" (rendered uppercase). Items with no catalogue match (plugin/skill commands) go in a last
  group "Other" with a different glyph (puzzle/sparkle icon, lucide) in the secondary accent.
- Name column: `/name` in the UI sans font (NOT monospace), weight 500. Description = `meta`.
- Icon: lucide `SquareTerminal` (or `Terminal`) 14 px, accent colour token.
- Tooltip with the full `meta` on hover (0.4), placement right.
- Data: all available (`complete.slash`, offline catalogue fallback). No backend change.

## 2. `mention-menu` — typing `@`

### Reference
- Drawer (0.1) at x 279–598, y 591–824; 8 rows, no group header for the bare `@` list.
- Rows (label · description): `@diff` git diff · `@staged` staged diff · `@file:` attach file · `@folder:` attach
  folder · `@url:` fetch url · `@git:` git log · then agent profiles (`@default` agent profile, `@<name>` agent
  profile (primary)).
- Icons per kind, coloured per kind: diff (split-square, accent blue `#084dd9` / `#6494f9`), staged (plus-in-square,
  blue), file (page outline, grey `#5e6164` / `#a9b0b4`), folder (folder outline, grey), url (globe, blue), git
  (branch glyph, blue), profile (people/agent glyph, bright blue `#0053fd` / `#4a84fe`).
- Description colour slightly bluer grey: light `#777c86` / dark `#888e99`.
- After `@file:` / `@folder:` the list switches to paths of that kind, with a header showing the scope ("FILE" /
  "FOLDER"); folders keep the caret inside so you can keep drilling.

### Ours today + bug
- Bug from `indus-ui-bugs.md`: "`@` mention menu did not open in a fresh draft". Cause found: `CompletionSource.fetch`
  calls `complete.path` with `session_id: null` and **no `cwd`** for a fresh draft, so the daemon fuzzy-searches its
  own working folder instead of the draft's folder; `@src` then returns nothing and the list is hidden because
  `items.length === 0`. Fix: pass `cwd` (the protocol's `CompletePathParams` has it; `path_completion.complete_path`
  uses `params.cwd` first) = `props.cwd ?? props.newSessionCwd` into `CompletionSource` options and into the request.
  Also show the drawer's empty state instead of hiding it when a trigger is active and the answer is empty.
- A bare `@` already returns the reference forms from the backend (`REFERENCE_HINTS`: `@file:`, `@folder:`, `@diff`,
  `@git:`, `@url:` with short descriptions).

### Build
- Same `ComposerPopover`; icon + colour chosen from the item text prefix: `@diff`, `@file:`, `@folder:`, `@git:`,
  `@url:`; path items: folder if `text` ends with `/`, else file. lucide icons: `Columns2`/`SplitSquareHorizontal`,
  `File`, `Folder`, `GitBranch`, `Globe`.
- Scope header: when the word starts with `@file:` or `@folder:` show a header "File" / "Folder".
- Gaps (do NOT invent): our backend has no `@staged` form and no `@profile` agent mentions. Show only what
  `complete.path` returns. Order: keep the backend order (diff, file, folder, git, url is fine).
- Descriptions come from the backend (`meta`); they are already our words.

## 3. `model-picker` — composer model pill menu

### Reference
- Pill in the composer row: text "Mock Model" + small chevron-down (10 px, 50 % opacity); 12 px, regular, tertiary
  colour (light `#84868a` / dark `#81858b`); 24–28 px tall, 8 px side padding, radius 6 px; hover bg = the chrome
  hover tint and text turns primary. While the model is unknown the label is a small spinner (never "No model").
- Menu (0.2) width 288 px, right-aligned to the pill, opens upward; its bottom edge sits on the composer's top border
  (x 986–1273, y 607–829).
- Top: search field row 37 px tall, no visible box, placeholder "Search models" (12 px, light `#86888b` / dark
  `#858a90`), focus goes to it on open; separator below (y 644).
- Provider group: label "MOCK" (provider name uppercase, 10 px, light `#585b5f` / dark `#afb6bb`), then one row per
  model: hollow star (favourite toggle, light `#bfc0c1` / dark `#51565a`; filled + accent when favourite) · model
  display name 12 px · chevron-right at far right (opens a per-model submenu with that model's options — the same
  content as §4).
- Separator (y 752), then three action rows in tertiary text with tertiary icons: refresh-arrow "Refresh models",
  plus "Add custom model…", gear "Edit models…".
- Favourites group "FAVORITES" appears above providers when any are starred. Keyboard: ↑/↓ through rows, Enter picks,
  Shift+Enter stars, → opens the row's submenu.

### Ours today + bug
- `ModelPicker.tsx`: rounded-full bordered chip, panel `w-80 rounded-lg p-2` with bordered search input, flat list
  with "☆" text star and provider name to the right, effort `<select>` + fast checkbox in a footer, confirm box.
- Bug (`indus-ui-bugs.md`): pill says "Choose a model" after the first chat while the status bar shows the model.
  Cause found: `Composer` never passes `current` to `ModelPicker`, and `model.options` is only fetched when the menu
  opens. Fix: in `Composer`, read `useSessionFacts(connection, sessionId)` (exported from `app/StatusBar.tsx`) and
  pass `current={{provider: info.provider, model: info.model, effort: info.reasoning_effort}}`; for a fresh draft
  (no session) fetch `model.options` once on mount (and on connection change) to show the default. Show a spinner,
  not "Choose a model", while nothing is known yet.

### Build
- Restyle trigger as the ghost pill above; keep the wrapper `data-testid="model-picker"` with the trigger as its
  direct child `<button>` (the compare kit clicks `[data-testid="model-picker"] > button`).
- Menu content (0.2): search row → groups by provider (from `entriesFrom(options)`; group label = `providerName`
  uppercase; unauthenticated providers' rows dimmed with a small "Sign in" note) → favourites group first →
  separator → actions.
- Per-row submenu (chevron): Thinking switch + Effort list (§4 content) bound to that model's preset
  (`prefs.preset/setPreset`, existing), plus "Fast" switch (existing `fast` preset). This replaces the footer
  `<select>`/checkbox.
- Actions: "Refresh models" → re-run `load()` (`model.options`). "Add custom model…" → turns the search field into a
  model-id entry (placeholder "Type a model id" (our words)); Enter applies it via the existing `applyModelPick` with
  the current provider (the backend accepts a typed id for custom/local endpoints per `ProviderOption.models` docs).
  "Edit models…" → open Settings › Model › main (`settings.view.model` page `main`) through the existing settings
  navigation.
- Keep the confirm flow (`Confirming`) but render it as a small in-menu panel styled like 0.2 rows, with two buttons.
- The "deferred"/"failed" note: show as a toast (foundation) or a tertiary line under the pill — not inline text
  that shifts the row.
- Data: all available. No backend change.

## 4. `effort-picker` — composer effort pill menu

### Reference
- Pill right of the model pill: "Med" + chevron, same style as the model pill. Short labels: Min, Low, Med, High,
  XHigh (shown as "X-High"), Max, Ultra; "Off" when thinking is disabled. Hidden when the model has no reasoning.
- Menu (0.2) width 208 px, right-aligned, upward (x 1129–1336, y 584–828).
  - "OPTIONS" label, then row "Thinking" with a switch at the right (on: track light `#0052fc` / dark `#4a84fe`,
    white/dark knob; ≈ 28×16 px).
  - Separator, "EFFORT" label, rows: Minimal, Low, Medium, High, Extra High, Max, Ultra; the current one has a
    check mark at the far right (12 px, light `#4f5256` / dark `#c0c7ce`).
  - Selecting a row applies immediately and closes; toggling Thinking keeps the menu open.

### Ours today
- Effort is a suffix in the model chip ("model · effort") and a `<select>` inside the model panel
  (`EFFORTS = minimal…xhigh, none`). No separate pill.

### Build
- New `EffortPill` next to the model pill (same file or `chat/EffortPicker.tsx`).
- Values: backend accepts `none, minimal, low, medium, high, xhigh, max, ultra` (`providers/profile.py`
  REASONING_LEVELS) — extend `EFFORTS` to include `max` and `ultra`. Thinking switch off = `none`; on = last non-none
  effort (default `medium`).
- Apply for a live session: `config.set {key:"reasoning", value, session_id}` (same call `applyModelPick` makes);
  for a fresh draft store it as the model's preset (`prefs.setPreset`). Current value from `session.info
  .reasoning_effort` via `useSessionFacts` (see §3 fix).
- Hide the pill only if we know the model has no reasoning; we do not have capability data per model → always show
  (gap noted; do not invent a capability API).
- Remove the " · effort" suffix from the model pill once this pill exists.

## 5. `composer-help` — typing `?` alone

### Reference
- When the composer text is exactly `?`, a help drawer (0.1 skin, same anchor, x 279–598, y 444–795) opens; deleting
  the `?` dismisses it. It is not a list (no selection).
- Section "COMMON COMMANDS" (10 px uppercase medium, light `#a4a5a7` / dark `#64686c`): six rows, 26 px pitch, each
  `/command` in MONOSPACE 12 px weight 500 (light `#4b4e52` / dark `#bac0c5`) + description 12 px (light `#9d9fa1` /
  dark `#6a6e73`), 10 px side padding, 8 px gap.
- Section "HOTKEYS": rows with key caps (0.3 help variant) then description: `@` reference files, folders, urls, git ·
  `/` slash command palette · `?` this quick help · `Enter` / `Shift` `Enter` send / newline · `Ctrl` `Shift` `K` send
  next queued turn · (scrolls: open keyboard panel, `Esc` cancel, `↑` `↓` history).
- Footer line: "`/help` lists every command" style hint (our words).

### Ours today
- None. `/help` prints `helpLines()` as a system line.

### Build
- `ComposerHelp` component rendered by `Composer` when `text === "?"` and no attachments (and not while a completion
  drawer is open). Esc clears the `?` and closes.
- Common commands (ours, all exist with `desktop: true`): `/help`, `/new`, `/resume`, `/model`, `/reasoning`,
  `/stop`; description = short label from `slashCatalog.json` (truncate at the first ";" or "—").
- Hotkeys (only ones we really have): `@` "files, folders, urls, git" · `/` "commands" · `?` "this help" · `Enter` /
  `Shift`+`Enter` "send · new line" · `↑` `↓` "earlier prompts" (PromptHistory) · `Esc` "close menus" ·
  `Ctrl`+`L` "focus the composer" (keybindings `composer.focus`). Read combos from `settings/keybindings.ts` so a
  rebinding shows correctly. Do NOT list a queue-send key (we do not have one).
- Footer: `/help` + "shows every command" (our words).

## 6. `composer-attach-menu` — the `+` button

### Reference
- `+` ghost icon button at the composer's left (14 px plus glyph, tertiary colour; 28 px square, radius 6 px; open
  state uses the hover tint).
- Menu (0.2) width 240 px, left-aligned to the button, upward (x 285–524, y 612–831):
  "ATTACH" label · Files… (page icon) · Folder… (open-folder icon) · Images… (picture icon) · Paste image (clipboard
  icon) · URL… (link icon) · separator · Prompt snippets… (speech-bubble-with-lines icon) · separator · tip line
  "Tip: type `@` to reference files inline." (11 px, light `#9d9fa1` / dark `#6e7378`, `@` in a small key cap).
- Prompt snippets opens a modal dialog (max-width ≈ 448 px): title, one-line description, list of 3 snippet buttons
  (icon, bold 14 px label, 12 px caption); clicking inserts the snippet text into the composer and closes.
- URL… opens a small dialog with a URL field; confirming inserts an `@url:` reference.

### Ours today
- `Composer` only renders an "Attach" text button when `props.pickFiles` is given — and `chatSlots.tsx` never passes
  it, so the real app has no attach control at all. Drag/drop and paste of files already work (`attachments.ts`).

### Build
- `AttachMenu` component (0.2) on a `+` button placed at the far left of the composer row (the foundation's composer
  must leave this slot; coordinate).
- Wire (all data/IPCs exist):
  - Files… → `bridge.fs.selectPaths({mode:"open-file", multiple:true})` → `attachmentFromPath` (existing local-mode
    path rule R08.23 stays).
  - Folder… → `selectPaths({mode:"open-dir"})` → insert `@folder:<path relative to cwd or absolute> ` at the caret
    (backend `core/references.py` expands it).
  - Images… → same as Files (no filter support in `selectPaths`; acceptable), images become image attachments via
    the existing `isImage` path.
  - Paste image → disabled (renderer clipboard image read is not available through our bridge; the menu shows it
    disabled, exactly the reference's behaviour for a missing handler). Ctrl+V paste keeps working.
  - URL… → small dialog (foundation Dialog), validate http/https, insert `@url:<url> `.
  - Prompt snippets… → dialog with three snippets written by us, e.g. "Review this change", "Plan the work",
    "Explain this code" with our own one-line captions and inserted texts.
- Pass the real picker in `chatSlots.tsx` (`pickFiles={() => bridge.fs.selectPaths(...)}`) or let the menu call the
  bridge itself; keep `pickFiles` as the injectable seam for tests.

## 7. `command-palette` — Ctrl+K

### Reference
- `FloatingHud` (0.3) 544 px wide (x 448–991), top 12 px; content height to ≈ 365 px (list max ≈ 320 px or 56 vh).
- Search row 44 px: magnifier icon 16 px (light `#6b6c70` / dark `#9aa2ab`) at 12 px from the left, input 12–13 px
  with placeholder "Search sessions, views, and actions" (our copy must differ: e.g. "Search chats, pages and
  commands"), bottom divider 1 px (light `#d0d7de` / dark `#30363d`).
- Group "GO TO": New session (plus icon, `Ctrl` `N`), New window (window icon, `Ctrl` `Shift` `N`), Settings (gear,
  `Ctrl` `,`), Capabilities (wrench), Messaging (speech bubble), Artifacts (cube), Cron (clock), Profiles (people),
  Spawn tree (chip), Memory Graph (graph). Then "PROJECTS" (folders), then sessions, commands, settings fields as you
  scroll/type. Row icons 14 px muted (light `#808793` / dark `#7d8896`); key caps right-aligned at ≈ 55 % opacity.
  Rows that navigate deeper show a chevron-right; toggles show a check when active.
- First row selected; ↑/↓ wrap; Enter runs; Esc closes and returns focus. Filtering is fuzzy over label + keywords.
  Empty: centred "No results" 14 px muted, 24 px vertical padding.

### Ours today
- `contrib/areas.tsx` `CommandPalette` (Ctrl+Shift+P), CSS in `styles.css` `.command-palette*`: centred modal with
  dim backdrop, 14 px rows "Go to X", no icons, no groups, no key caps. Entries: New chat + `PAGES` + plugin commands.

### Build
- Restyle into `FloatingHud`; keep `role="dialog"`, combobox input, listbox/options, keyboard handling and
  `filterPalette`. Keep class `command-palette` on the surface and the `input` inside it (the compare kit waits for
  `.command-palette input`) or update your kit copy.
- Keys: add `CommandOrControl+K` as the primary binding (it is unused today), keep Ctrl+Shift+P as an alias.
  Add it to `settings/keybindings.ts` as `palette.open` if that list is user-rebindable.
- Extend `PaletteEntry` with optional `icon`, `group`, `keys` (accelerator string → key caps), `detail`.
- Groups and entries (ours, only real targets):
  - "Go to": New chat (`Ctrl N`, plus) · New window (`Ctrl Shift N` — pane action `new-window` exists) · Settings
    (`Ctrl ,`, overlay) · Capabilities · Messaging · Artifacts · Webhooks · Command center · Learning map · Bots
    (contributed, only if on) · Scheduled tasks (cron overlay) · Profiles (overlay) · Agents (overlay). Icons: lucide
    equivalents of our sidebar icons (reuse the sidebar's icon map so they match).
  - "Projects": from `ProjectStore.projects(profile)` → run = start a new chat in that project (existing
    `beginNewChat`).
  - "Chats": when the query is non-empty, matching sidebar rows (`rowMatches`) → open session; pinned first.
  - "Commands": plugin palette commands (existing `palette-command` contributions).
- Titles without the "Go to …" prefix (the group says it).

## 8. `session-switcher` — Ctrl+Tab held

### Reference
- `FloatingHud` 304 px wide (x 568–871), top 12 px, padding 4 px, max height ≈ 352 px.
- One row per session (sidebar order): status dot (6 px; grey `#9ca4b1` / `#616d7d`, coloured when working/unread),
  title 12 px truncated (selected light `#1f2328` / dark `#e6edf3`, others light `#585b5f` / dark `#adb5bc`), and a
  right-aligned slot label `⌃1`…`⌃9` in 10 px monospace (light `#acaeaf` / dark `#5c646d`; selected row
  `#626972` / `#a1a9b6`). Selected row bg = HUD selection (light `#e3edff` / dark `#17243a`).
- Behaviour: Ctrl+Tab moves to the next session, Ctrl+Shift+Tab previous; a quick tap switches without showing the
  HUD; holding Tab/Ctrl ≥ 220 ms reveals it; releasing Ctrl opens the selected session; click opens; click-away
  closes. Needs ≥ 2 sessions.

### Ours today
- No switcher. Ctrl+Tab cycles open pane tabs (`panes/shortcuts.ts` → `{kind:"cycle"}`; keybinding `tab.next`). Our
  current "session-switcher" capture is a sidebar search, not a switcher.

### Build
- New `app/SessionSwitcher.tsx` + a tiny store (open, rows, index, reveal timer 220 ms).
- Decision for the builder (record as `W52-<x>-n`): Ctrl+Tab becomes "next chat" through the switcher, using the
  sidebar's live rows (`SessionListStore` state for the foreground profile, pinned first, then by last activity) —
  this matches the default single-chat layout. If the window shows ≥ 2 chat pane tabs, keep the old pane cycle on
  Ctrl+Tab and offer the switcher on Ctrl+\` instead — or simply make the switcher list the open tabs first. Pick one,
  write it down, update `keybindings.ts` label and its test.
- While open: Tab/↓ next, Shift+Tab/↑ previous, Enter or Ctrl-release opens (existing `openSession`/navigate to
  `sessionPath(id)`), Esc closes. `⌃N` labels: show only if you also make Ctrl+1…9 pick row N while the HUD is open
  (our Ctrl+digit outside the HUD jumps pane slots — do not change that).
- Status dot: running → the foundation's "working" dot colour; unread (`UnreadTracker`) → accent; else grey.

## 9. `session-sidebar` — left sidebar sessions part

Overlap: wave 51b builds the sidebar frame (top tabs, nav, footer). This slug owns the search, sections, rows,
tooltip, filter menu and the row context menu (§10). Check the frame against these numbers too.

### Reference (light / dark)
- Sidebar 260 px wide, bg `#f6f8fa` / `#010409`, no right border (main area bg `#fefefe` / `#0d1015`).
- Top tabs at y≈48: "SESSIONS" (11 px, medium, uppercase, primary text `#2c3035` / `#d8dfe5`) with a 2 px accent
  underline (`#0053fd` / `#4a84fe`) spanning x 0–73 at y 60–61; "BOTS" tertiary (`#828589` / `#7c8188`).
- Nav rows (29 px pitch, 14 px text `#575a5f` / `#aab1b6`, 16 px outline icons `#84878a` / `#7b7f86`, 18 px left
  inset): New session (with `Ctrl` `N` key caps at the right edge), Capabilities, Messaging, Artifacts,
  Scheduled jobs.
- Search at y≈238: no box, 13 px magnifier + placeholder "Search sessions…" in very faint text (`#d2d4d7` /
  `#262a2f`) — reads as a quiet field.
- Section header "PINNED" at y≈269: small accent square glyph + label 10–11 px bold uppercase, tracking ≈ 0.12em,
  accent colour. Empty pinned state: pin icon + "Shift-click a chat to pin" (13 px, `#828589` / `#7c8188`).
- Section header "SESSIONS" at y≈341 with a filter (funnel-lines) icon button at the right (`#b9bcbf` / `#44494f`).
- Session rows: 27 px pitch, x 10–242 (8 px inset, radius 4 px), status dot 5 px at x≈24 (`#a8abae` / `#53585d`),
  title 14 px (`#575a5f` / `#aab1b6`) truncated, relative time right-aligned 10 px (`#8b8e92` / `#757a80`: "now",
  "1m", "2m", "18m"). Selected/active row bg `#d8e1f0` / `#101826` (title turns primary). Hover: same tint lighter
  (`#e5eaf4` / `#0a0f1a`).
- Hovering a truncated row shows the inverted tooltip (0.4) with the full title, to the right of the row.
- Footer (y≈864): home icon button (28 px, active bg `#d8e1f0` / `#101826`) left; small ghost icons right
  (plus, new-from-…, more "…", plug/status). Status bar below (y 880–900): "Backend ready" with a pulse icon and the
  folder name, 11 px tertiary.

### Ours today
- `Sidebar.tsx` + `sidebar.css`: bordered search "Search or paste an id" with two clear buttons (bug), Chats/Archived
  segmented control, PROJECTS / CHATS grey headers, rows with hover pop-out/pin/archive icon buttons, `title` =
  cwd tooltip.

### Build
- Search: quiet field style above; ONE clear button (fix the double clear: drop the native `type="search"` cancel
  button via CSS or remove our extra button — keep ours). Keep "paste an id" behaviour; placeholder becomes
  "Search chats…" (≤ 4 words).
- Replace the Chats/Archived segmented control with the "SESSIONS" header's filter icon button opening a
  `MenuSurface` with: View — Chats / Archived (radio, existing `view` state); plus any sort we already support
  (we sort by last activity only → show just that, checked). Do not add filters we cannot compute.
- Sections: "PINNED" (existing `pinned` rows; empty state with "Shift-click a chat to pin" — implement Shift+click on
  a row = toggle pin via `list.setPinned`), then projects groups (existing `groupByProject`, header style = section
  header but in secondary text, not accent), then "SESSIONS" (ungrouped rows). Headers use the accent style above.
- Row: dot (running → working colour, unread → accent, else grey), title, time (`relativeTime`, short form "now",
  "1m", "3h", "2d"). Hover action buttons (pop-out/pin/archive) may stay but only on hover and must not cover the time
  (reference shows none; preferred: move them into the context menu §10 and keep only the time).
- Tooltip with the full title on hover when truncated (0.4); the cwd moves into the tooltip's second line or is
  dropped.
- Keep `aria-current`, `role` semantics and the `session-row` / `session-open` classes used by tests and the kit
  (kit hovers rows for this shot).

## 10. `context-menu` — right-click on a sidebar session row

### Reference
- `MenuSurface` (0.2) at the pointer (x 104–263, 160 px wide; y 423–787), rows 24 px:
  Open in new tab (browser icon) · New window (external-link) · Open in terminal (terminal) · — · Rename… (pencil) ·
  Pin (pin) · Mark as unread (envelope) · Appearance › (palette, submenu) · Copy ID (copy) · — · Branch (fork) ·
  Export (cloud-download) · Move to project › (folder, submenu; label wraps to two lines) · — · Archive (archive box)
  · Delete (trash, destructive red).
- Labels toggle with state: Unpin, Mark as read, Unarchive.
- Also opens from the keyboard (context-menu key / Shift+F10) on a focused row.

### Ours today
- Only the native Electron menu (`electron/contextMenu.ts`: spelling, cut/copy/paste, copy link/image, inspect).
  Sidebar rows have no menu; the kit could not capture it.

### Build
- In-renderer `SessionRowMenu` (Radix ContextMenu on the row, styled 0.2). Call `event.preventDefault()` in the row's
  `contextmenu` handler so the native menu does not also open (the native one is installed from main for every
  web-contents; check that a prevented `contextmenu` DOM event suppresses it — Electron only emits `context-menu`
  when the page does not prevent default).
- Items and wiring (only real actions):
  - Open in new tab → open as a pane tab (pane shell "open" action, if the layout has tabs) — else omit.
  - New window → existing pop-out (`props.onPopOut(row.id)`), label "Open in window".
  - Open in terminal → omit (no "terminal at session cwd" IPC). Gap.
  - Rename… → inline rename in the row (input replaces title; Enter saves, Esc cancels) → `PATCH
    /api/sessions/{durableId}` `{title}` (backend `SessionPatch.title` exists; add `setTitle(row, title)` to
    `SessionListStore` beside `setPinned`/`setArchived`, same fenced patch).
  - Pin / Unpin → `list.setPinned`.
  - Mark as unread / read → `UnreadTracker.markUnread` / `markSeen` (exist).
  - Appearance › → omit (no per-session colour data). Gap.
  - Copy ID → `app/clipboard.ts` writeText(durable id).
  - Branch → run `/branch` for that session (open it, then the existing slash runner) — or omit if that cannot be
    done without opening; decide and note.
  - Export → `GET /api/sessions/{id}/export` (exists) + save via `selectPaths({mode:"save"})` and the fs bridge; if
    writing the file needs a new IPC, omit and note the gap.
  - Move to project › → omit (projects are derived from cwd; no move API). Gap.
  - Archive / Unarchive → `list.setArchived`.
  - Delete → confirm dialog (foundation Dialog, destructive button) → `DELETE /api/sessions/{id}` (route exists);
    on success remove the row and, if it was open, navigate to the fresh draft.
- Keyboard: Shift+F10 / ContextMenu key on a focused row opens the same menu.

---

## 11. Files that change (ours)

| Area | Files |
| --- | --- |
| Shared surfaces | new `apps/desktop/src/components/` (or the foundation's folder): `ComposerPopover.tsx`, `MenuSurface` (or foundation Menu), `FloatingHud.tsx`, `Kbd.tsx`, `Tooltip` (reuse foundation if present); styles via foundation tokens |
| Slash / mention / help | `chat/composer/Composer.tsx`, `chat/composer/completion.ts` (cwd param, loading flag), `chat/slash.ts` (`categoryOf`), new `chat/composer/ComposerHelp.tsx`, `chat/strings.en.ts` |
| Model / effort | `chat/ModelPicker.tsx` (restyle, provider groups, submenu, actions, `EFFORTS` + max/ultra), new `chat/EffortPicker.tsx`, `Composer.tsx` (pass `current`, mount pills), `app/StatusBar.tsx` (reuse `useSessionFacts`, no change expected) |
| Attach | new `chat/composer/AttachMenu.tsx`, `chat/composer/attachments.ts` (folder/url reference helpers), `app/chatSlots.tsx` (real picker) |
| Palette | `contrib/areas.tsx` (`CommandPalette`, `PaletteEntry`, `PALETTE_KEYS`), `contrib/strings.en.ts`, `app/Shell.tsx` (entries with icons/groups/keys), `styles.css` (`.command-palette*` rules) , `settings/keybindings.ts` |
| Switcher | new `app/SessionSwitcher.tsx` (+ store), `panes/shortcuts.ts` (Ctrl+Tab routing), `app/Shell.tsx` (mount), `settings/keybindings.ts` |
| Sidebar + row menu | `sidebar/Sidebar.tsx`, `sidebar/sidebar.css`, `sidebar/sessionList.ts` (`setTitle`, `remove`), `sidebar/strings.en.ts`, new `sidebar/SessionRowMenu.tsx` |
| i18n | every new label in the English packs (`*/strings.en.ts`); `i18n.test.ts`/`packs.test.ts` keep passing (other packs fall back) |

Tests to update/extend (pin old markup today): `chat/composer/Composer.test.tsx` (listbox markup, attach button,
add: help on `?`, `cwd` passed for fresh draft, empty state), `chat/ModelPicker.test.tsx` (chip text, effort select →
submenu/pill; add: pill shows session.info model without opening), `contrib/areas.test.tsx` (palette key, groups),
`sidebar/Sidebar.test.tsx` (segmented control → filter menu, double clear, shift-click pin, row menu actions with a
fake fetch for PATCH/DELETE), `settings/keybindings.test.ts` if a binding is added/relabelled, `panes` shortcut
tests for Ctrl+Tab, `app/Shell.test.tsx`. E2E: `e2e/core/chat.electron.test.ts` if it opens the model picker or
completion list — keep `data-testid="model-picker"`.

Backend: no changes. Data available: slash catalogue + `complete.slash`; `complete.path` (with `cwd`);
`model.options`; `config.set reasoning/fast/model`; `session.info` (model, provider, reasoning_effort);
REST sessions PATCH (title/pinned/archived), DELETE, export; `fs.selectPaths`; clipboard writeText.
Not available → omitted or disabled: `@staged`, `@profile` mentions, per-model reasoning capability, paste-image from
menu, open-in-terminal, per-session appearance, move-to-project, queue-send hotkey.

## 12. Build order
1. Shared surfaces (0.1–0.4) on the foundation tokens; Storybook-free: render them in a vitest snapshot-free test.
2. Bug fixes first (small, testable): mention `cwd` + empty state; model pill `current`; sidebar double clear.
3. Slash + mention drawer restyle with groups/icons; composer help.
4. Model menu restyle + effort pill/menu.
5. Attach menu + URL/snippet dialogs + real picker wiring.
6. Command palette restyle + Ctrl+K + groups/icons/key caps.
7. Sidebar sections/rows/tooltip/filter menu; row context menu + `setTitle`/delete.
8. Session switcher + Ctrl+Tab decision.
9. Capture (copy the kit to your `$S`; add steps: `@` in a fresh draft, `?`, `+`, effort pill, Ctrl+K instead of
   Ctrl+Shift+P, hold Ctrl+Tab, right-click a session row via the in-renderer menu) and compare both themes.

## 13. Compare checklist (tick in the side-by-side PDF, light AND dark)
- [ ] slash-menu: drawer 320 px wide, anchored 3 px inside composer left, ends 5 px above composer; group header
      "SESSION" etc.; terminal glyph in accent; sans `/name` + grey truncated description; 28 px rows; highlight tint
      `#dde4f3`/`#1b212f`; tooltip on hover.
- [ ] mention-menu: opens on bare `@` in a fresh draft; kind icons coloured; file/folder scope header; empty state.
- [ ] model-picker: ghost pill with chevron (no border, no "Choose a model" after a chat); 288 px menu sitting on the
      composer; borderless search; provider label; star + name + chevron rows; refresh/add/edit footer rows.
- [ ] effort-picker: separate "Med"-style pill; 208 px menu; OPTIONS/Thinking switch; EFFORT list with check;
      Max/Ultra present.
- [ ] composer-help: `?` opens it; mono commands; key caps; deleting `?` closes.
- [ ] composer-attach-menu: `+` at composer left; 240 px menu; ATTACH label; 5 items + snippets + tip; Paste image
      disabled; URL and snippets dialogs work.
- [ ] command-palette: Ctrl+K; 544 px HUD at top 12 px, no dim; search row with icon + divider; accent uppercase
      group headings; icons; key caps; selection `#e3edff`/`#17243a`.
- [ ] session-switcher: hold Ctrl+Tab shows 304 px HUD with dots/titles/slots; release opens; quick tap switches.
- [ ] session-sidebar: quiet search with one clear button; PINNED/SESSIONS accent headers; filter menu replaces the
      segmented control; 27 px rows with dot/title/time; selected tint; tooltip for long titles.
- [ ] context-menu: in-renderer menu on right-click/Shift+F10; grouped items with separators; Delete in red;
      rename inline works; native menu does not double-open.
- [ ] No `Hermes`/`Nous` text; no copied long sentences; tokens from the foundation, no stray hex in components.
- [ ] biome, tsc, vitest (desktop), Electron e2e touched specs, `python -m tooling.checks --require-ref` green.
