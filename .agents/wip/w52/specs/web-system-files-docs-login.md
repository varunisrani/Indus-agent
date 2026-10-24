# W52 spec — web slice `system / files / docs / login` (+ error state, language menu, page notes for theme menu and collapsed rail)

Status: SPEC (Read + Note done 2026-10-24). Builder rebuilds from this note with the reference closed.
Read: reference screenshots `$SP/compare/hermes/web/{system,system-console,files,docs,docs-direct-url,login,error-state,language-menu,theme-menu,sidebar-collapsed}--{light,dark}.png`
(+ manifest.json); reference source read for structure/behaviour only: web `pages/SystemPage.tsx`, `pages/FilesPage.tsx`,
`pages/DocsPage.tsx`, `components/HermesConsoleModal.tsx`, `components/LoadErrorNotice.tsx`, `lib/load-error-copy.ts`,
`hermes_cli/dashboard_auth/login_page.py`. Ours: `$SP/compare/indus/web/{system,system-operations,system-hooks,system-stop-confirm,system-doctor,console,files,files-preview,login,error-state,sidebar-collapsed}--*.png`.
`$SP` = `/tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad`.

Clean-room reminders for the builder: everything below is in our words with measured numbers. Do not open the reference
while building. No reference names in UI text ("Hermes", "Nous", "portal"): where the reference says HERMES CONSOLE we say
INDUS CONSOLE, where it shows the NOUS ■ RESEARCH wordmark on the login page we show INDUS ■ AGENT.

Global measurements (1440×900 @1x, both reference themes): sidebar 240 px wide (1 px right border); page header 52 px tall
(1 px bottom border); page content starts x=263 and ends x=1407 (24 px left gutter, ~32 px on the right including the
scrollbar lane), first content row at y=75 (22–24 px under the header). Cards have SQUARE corners, a 1 px border and NO
fill (card surface = page background). Popovers / modal panels DO have a fill (see "surface" below).

## 0. Colour roles used in this slice (sampled hex; map each to the wave-51 foundation token of the same role)

Dark = reference "teal" dark preset, Light = reference "blue" light preset. If the foundation note
(`docs/notes/ui-parity/web-foundation.md`) names tokens differently, use its token for the role, do not hard-code hex in pages.

| Role | Dark | Light | Seen on |
|---|---|---|---|
| page background | #041c1c | #e8f2fd | everything |
| input fill | #041b1b (a hair darker) | #e8f2fd | path input |
| surface (popover, modal header, menu) | #0e2423 | #dfecfd | console header, language menu |
| card border | #293935 | #c5dafd | all cards, table frame, inputs |
| shell divider (header bottom, sidebar right) | #36443f | #bad3fd | |
| soft row divider (table rows) | #1a2e2b | #d3e3fd | files rows |
| dashed drop-zone border | #293b37 | #c3d9fc | files drop zone |
| foreground (values, buttons, page title) | #ffe6cb | #0053fd | |
| muted (section titles, field labels, nav text) | #ccbda7 | #2f73fd | |
| tertiary (table head, "choose files", dates) | ≈#a79f8d | ≈#518bfd | |
| secondary badge: fill / border / text | #172b29 / #3a4640 / #c6b9a3 | #d5e6fd / #b6d0fd / #3a7afd | "not logged in" style badge |
| success badge: fill / border / text | #0e382a / #1f6a44 / #42ca76 | #d0efea / #a7eacb / #58df8c | "active", "ready", "running" |
| destructive text / icon | #f62b34 (trash icon #cc2831) | #fb3039 (trash #f7505b) | error notice, delete icons |
| destructive border (error card) | #662226 | #efa3ae | error notice |
| warning (folder icon) | #ffbd38 | #ffbd38 | files folder icon |
| modal backdrop | page bg at ~85 % opacity (content still faintly visible) | same | console, dialogs |
| console terminal bg / fg | #000000 / #f0e6d2 | #f5f8fc / #170d02 (with an 8 px black frame, see §2) | console |

Typography roles (the foundation picks the open fonts; use its role names):
- **display-wide**: page title (cap height 14 px ≈ 18–20 px size), wide extended sans.
- **section heading**: 15–16 px, bold, letter-spacing ≈0.15 em, muted colour, sentence case ("Host", "Memory"), 16 px icon
  left of it with 8 px gap.
- **field label**: 12 px UPPERCASE, letter-spacing ≈0.05 em, muted.
- **body/value**: 14 px regular, foreground.
- **button text**: 11–12 px bold monospace-style, letter-spacing ≈0.15 em; some buttons uppercase (noted per button).
- **badge text**: condensed face, 12–13 px, lower case as given ("active", "ready").
- **mono**: paths, commands, console, profile names.

## 1. `system` — System page

### 1.1 Layout
One column, full content width (x 263–1407). Vertical stack of **sections**, 32 px between sections. Each section =
heading row (icon + section heading, ~20 px tall) then 12 px gap then one or more cards (12 px between cards of one section).
Card padding: 16 px top/bottom, 15–16 px left/right (content x starts at 279). No two-column grid any more (ours is 2-col
`xl:grid-cols-2` today — remove it).

Loading: while the first load is pending, the whole page shows one centred spinner (≈96 px from the top), not per-card
"Loading…" text. Each section still degrades on its own when its own request fails (show its card with the §5 error notice).

Section order (reference) and what WE render:

| # | Reference section | Ours | Decision |
|---|---|---|---|
| A | (action log card, only while an action is open) | `ActionLog` per section today | Move to ONE action-log card at the very top of the page (see 1.3) |
| B | Host (stats grid + update footer) | `HostSection` + `UpdateSection` | Merge: update controls become the Host card footer |
| C | provider-portal card | none | **Omit** (no backend; not our product) |
| D | Skill curator | none (no status/pause API) | **Omit** |
| E | Gateway | `DaemonSection` | Restyle as "Daemon" section |
| F | Memory | `MemorySection` | Restyle; keep our provider select + Save + Reset to built-in |
| G | Credential pool | `PoolSection` | Restyle |
| H | Operations (3 cards) | `OperationsSection` | Restyle; only the buttons we have APIs for |
| I | Checkpoints | `CheckpointsSection` | Restyle; keep our keep-days / size-cap inputs |
| J | Shell hooks (+ New hook modal) | `HooksSection` | Restyle; form moves into a modal |

### 1.2 Section by section

**B. Host** — heading icon: server/rack (two stacked rounded rectangles). Heading text: "Host".
- Card content: a 3-column grid (column starts x=279, 657, 1035 → 378 px columns, 24 px column gap), rows 45 px apart
  (label top at y=126, 171, 216, 261). Each tile = field label (12 px uppercase muted) over value (14 px foreground,
  4–6 px below). Some labels carry a 12 px icon before the text (CPU: chip icon; Disk: hard-drive icon).
- Reference tiles in order: OS, Arch, Host, Python, <product> version (+ badge), CPU ("4 cores · 26%"), Memory
  ("2.6 GB / 15.7 GB (16.5%)" = used / total (percent)), Disk (used / total (percent)), Uptime ("30m"), Load avg
  ("0.81 / 1.16 / 1.25").
- **Our data** (`api.system.stats()` → `HostStats`; `api.system.update()` → `UpdateInfo`): we have cpu_count, cpu_percent,
  load, memory total + available, disk total/free/used, current_version. We do NOT have os, arch, hostname, python version,
  uptime. Render, in this order: **INDUS** (value `v<current_version>` + update badge), **CPU** (`N cores · P%`; drop the
  percent part when absent), **Memory** (used = total − available; `used / total (pct%)`; tile hidden when memory null),
  **Disk** (`used / total (pct%)`, hidden when null), **Load avg** (three values with 2 decimals joined by " / ",
  hidden when null). Do not render empty OS/Arch/Host/Python/Uptime tiles. Follow-up F1 (backend, lead decides):
  add `os`, `arch`, `hostname`, `python`, `uptime_s` to `/api/system/stats`; once present, the tiles slot in in the
  reference order (OS, Arch, Host, Python, INDUS, CPU, Memory, Disk, Uptime, Load avg).
- Byte format: one decimal + unit (GB / MB / KB). Our `formatBytes` prints GiB with a space-percent "(50 %)" — tests pin
  "4.0 GiB of 8.0 GiB (50 %)"; change deliberately to the new shape and update that test.
- Version badge (right of the version, 8 px gap): update available → warning badge "N behind"; behind == 0 → success badge
  "latest"; otherwise none.
- **Footer** (always shown: our update check answers for every install method): 16 px above it a 1 px
  card-border divider across the card's inner width, 16 px padding above the row. Row = ghost button with a circular-arrow
  icon "Check for updates" (sentence case, bold mono text, no border) → `api.system.update(true)`; spinner icon while
  fetching. Then, when `update_available && can_apply`: a filled button with a download icon "Update now" → our existing
  confirm dialog ("Update Indus Agent?") → `api.system.applyUpdate()`; its output goes to the page action log.
  When available but not appliable: 12 px muted text naming the hint (our `message`, e.g. the docker hint the test pins).
  When not available and `message` present: 12 px muted message ("could not reach…" style) right of the button, 12 px gap.
- Psutil missing hint: our backend falls back without psutil (no percent); show nothing extra.

**E. Daemon** — heading icon: power symbol. Heading "Daemon" (our word for the background service).
- One card, single row, 16 px padding, space-between. Left: status badge (success "running" / secondary "stopped"; when
  the daemon reports another state, secondary badge with that state) + 14 px muted description (ours: "Process <pid> ·
  code <ver>" when running, "draining (finishing current turns)" appended when draining; when stopped our one-line
  explanation sentence). Right: three small buttons, 8 px apart, UPPERCASE: START (play icon; disabled when running),
  RESTART (circular arrow icon), STOP (power icon, ghost, warning-coloured text; disabled when stopped).
- Behaviour stays ours: STOP opens our confirm dialog ("Stop the daemon?", danger confirm "Stop") before
  `api.daemon.stop()`; START/RESTART call `api.daemon.start/restart()`; each opens the page action log
  (`daemon-<verb>`), then re-queries `["system","daemon"]` on finish. Keep the `Notice` for an odd state as a second
  card row (1 px top divider, 12 px muted text).

**F. Memory** — heading icon: brain. Heading "Memory".
- Card, 16 px gap between rows.
- Row 1 (12 px muted text, wraps): "External provider: " + active name in mono foreground (or "built-in only").
  Right-aligned on the same row (margin-left auto): our **Memory provider** select (label stays for tests/a11y, can be
  visually compact) + **Save** (filled, disabled until changed) + **Reset to built-in** (ghost). Same handlers as today
  (`api.system.setMemoryProvider`).
- Row 2 (1 px top divider, 12 px top padding): 12 px muted "Built-in files — MEMORY.md: <size> · USER.md: <size>" from
  `builtin_files` (size formatted). The reference has three red ghost reset buttons on the right of this row — we have
  no reset API: **omit them** (do not invent).

**G. Credential pool** — heading icon: key. Heading "Credential pool".
- Card, 16 px vertical gaps.
- Form row: 4-column grid (1 : 2 : 1), 12 px gaps, bottom-aligned. Columns: **Provider** (input with our datalist of
  provider names, placeholder e.g. "openrouter"), **API key** (password input spanning 2 columns, placeholder "sk-…"),
  **Label** (placeholder "optional"). Labels above inputs (field-label style). Inputs 34 px tall, 1 px card-border, no radius.
- Below, right-aligned: small filled UPPERCASE button "ADD KEY" (spinner while pending). Our validation message
  ("Pick the provider and paste the key.") shows as a destructive notice under it.
- Empty: 14 px muted line "No pooled credentials yet." (keep ours).
- Groups (from `pool.providers`): provider heading in field-label style (UPPERCASE muted; append our "(from the default
  profile)" note when `inherited`). Each entry = a bordered row (1 px card border, fill = page bg slightly lifted ~40 %
  surface), 12 px horizontal / 8 px vertical padding, 12 px gaps: label (14 px medium) · masked preview (12 px mono
  muted) · outline badge `auth_type` · secondary badge `status` (success tone when "ok") · our outline badge `source` ·
  right-aligned icon-only ghost button (trash icon, destructive colour, aria-label "Remove <label>", disabled when
  inherited) → our existing confirm dialog → `api.system.removeFromPool`. Keep `data-testid="pool-<provider>-<index>"`.
- Drop our explanatory paragraph under the heading (reference has none) — or keep it as 12 px muted text inside the card
  top; builder's choice, prefer dropping for parity.

**H. Operations** — heading icon: activity (pulse line). Heading "Operations". Up to three cards in this section:
1. Button card: wrap row, 8 px gaps, ghost small buttons with 14 px icons, sentence case:
   "Open console" (terminal icon; ONLY when `consoleEnabled()`; opens the §2 console) · "Run doctor" (stethoscope icon →
   `api.system.doctor()`). The reference also has Security audit / Update skills / Prompt size / Support dump / Migrate
   config — **we have no APIs: omit**.
2. Backup card (16 px gaps, rows separated by 1 px dividers with 16 px top padding):
   - "Full backup" label; row: ghost "Create backup" (database icon → `api.system.backup()`, output in the page action
     log) · ghost "Download backup" (download icon; disabled until our backup action finished `done` and returned an
     archive; → `api.system.downloadBackup(archive)`) · 12 px muted truncated archive file name (or "No backup created yet").
   - "Restore from upload" label; row: ghost "Choose restore zip" (upload icon, opens a hidden `.zip` file input) · 12 px
     muted file name or "No archive selected" · right-aligned ghost "Restore upload" (disabled without a file) →
     `api.system.importUpload(file, force)`.
   - "Restore from path" label; input (mono, placeholder "~/.indus/backups/<name>.zip"-style in our words) · right-aligned
     ghost "Restore path" (disabled when empty) → `api.system.importArchive(path, force)`. Keep our absolute-path check
     message.
   - Keep our **"Overwrite existing files"** checkbox (behaviour: `force`) as the last row of this card, 12 px muted label.
     Before either restore runs, show a destructive confirm dialog ("Restore this backup?", our wording, confirm
     "Restore"), matching the reference's consent step.
3. Debug-share card — **omit** (no API).

**I. Checkpoints** — heading icon: database. Heading "Checkpoints".
- Card row, space-between: left 14 px muted "<N> project(s) · <total size>" (from `projects.length`, `total_bytes`);
  right: ghost destructive small button with trash icon "Prune" (disabled when nothing kept).
- Our prune takes rules: put **Keep days** and **Size cap (MB)** inputs (narrow, ~96 px each, field labels) immediately
  left of the Prune button on the same row (or a second row when narrow). Prune opens a destructive confirm dialog (new,
  our words) then `api.system.pruneCheckpoints(days, size)`; output in the page action log. Keep the project list as
  optional 12 px mono rows under a divider when there are projects (ours shows them today; keep, it costs nothing).
  The test pins the button name "Prune checkpoints" — keep that as the accessible name (aria-label) or update the test.

**J. Shell hooks** — heading row is space-between: left icon (terminal) + "Shell hooks"; right a small filled UPPERCASE
button with plus icon "NEW HOOK" → opens the hook modal.
- No hooks: one card, 24 px vertical padding, centred 14 px muted "No shell hooks configured." (our text "No shell hooks."
  may stay).
- Each hook = its own card (12 px vertical padding), one row, 12 px gaps: outline badge event · "matcher: <m>" 12 px muted
  (when set) · command 12 px mono, truncates, takes the remaining width · destructive badge "not executable" (when
  false) · success badge "approved" / warning badge "not approved" · icon-only destructive trash button (aria-label
  "Remove hook") → our confirm dialog → `api.system.removeHook`. Keep `data-testid="hook-<grant_id>"`. Our `warnings`
  list shows as a warning notice under the heading.
- **Hook modal** (foundation Dialog; panel max-width 512 px, surface fill, 1 px border, heavy shadow, close X top-right):
  header 20 px padding, 1 px bottom border, title in display face 16 px tracking-wide "New shell hook". Body 20 px
  padding, 16 px gaps: **Event** select (our `events`), **Command** (mono input, autofocus), 2-col row **Tool matcher
  (optional)** / **Timeout (s)**, our consent checkbox "I understand and want this command to run on agent events"
  (unchecked by default — our rule, it gates submit), our warning paragraph in warning colour 12 px, right-aligned filled
  UPPERCASE button "ADD HOOK" (tests pin "Add hook"). Close on success; errors stay in the modal as a destructive notice.

### 1.3 Action log card (shared by all System actions)
- One card at the top of the page (above Host) while an action is open. Header row: 16 px terminal icon (muted) ·
  action name in 14 px mono · badge: warning "running" / success "done" / destructive "exit N" · right: icon-only ghost
  close (X, aria-label "Close log"). Below: a pre block, max-height 288 px, scroll, wraps, 1 px border, fill = page bg at
  ~50 %, 12 px padding, 12 px mono muted text, "Starting…" before the first line.
- Implementation: restyle `apps/web/src/ui/ActionLog.tsx` (keep its polling, `data-testid="action-log-<name>"`, the
  "Exit code N." text the test pins can become the badge — update the test). It is also used by Profiles and Channels:
  restyle only, keep props. On System, lift the "current action" state to `SystemPage` (one log at a time; starting a
  new action replaces it) and pass a starter down to the sections.

### 1.4 States
- Each card's own fetch failing → that section's card shows the §5 load-error notice (with Retry = refetch).
- Buttons disabled while their mutation/action is pending.
- Toasts (foundation) for success/failure as today.

## 2. `system-console` — the console modal

Opened from System → Operations → "Open console" (and, if the foundation keeps it, the sidebar entry; today ours lives
in the sidebar footer as "Admin console" — coordinate with the foundation: reference has no sidebar entry).
- Backdrop: full window, page bg at ~85 %, click on backdrop closes; Esc closes; focus trapped.
- Panel: centred, 960 px wide (max), height = min(82 % of viewport, 760 px) → 738 px at 900 tall (y 81–818),
  1 px border (#31403b dark / #bdd4fd light), surface fill, big soft shadow.
- Header 72 px (16 px x-padding, 12 px y), 1 px bottom border: a 34 px square icon box (1 px border, page-bg fill,
  foreground terminal icon ">_"), 12 px gap, then two lines: title "INDUS CONSOLE" (display face, 16 px, uppercase,
  tracking-wide, foreground) and, 4 px below, a status badge (`connecting` secondary / `ready` success / `running`
  warning / `closed`,`error` destructive) + 8 px + the profile name in 12 px mono muted. When closed/error, an outlined
  small "Reconnect" button follows (redials). Close X at the right (icon-only ghost).
- Body: fills the rest; black container with 8 px padding; inside it an **xterm terminal** (we already depend on
  `@xterm/xterm` 6.0.0 + fit/unicode11/web-links addons for chat): font = our mono, 13 px, line-height 1.25, cursor
  blink, 3000 lines scrollback, fit to the container on resize. Theme colours from the active theme (dark: bg #000000,
  fg #f0e6d2; light: bg #f5f8fc, fg #170d02 → the 8 px black frame shows around the light canvas, as in the reference).
- Behaviour (our protocol stays: `/api/console`, purpose `web-console`, frames ready/output/error/confirm_required/
  complete/clear): the terminal itself is the input line — print the prompt (from the `ready` frame, ours "indus> "),
  echo typed characters, Backspace, Enter sends `{type:"input", line}`, ↑/↓ history (last 200), Ctrl+C sends
  `{type:"cancel"}` (or just a new prompt when idle), Ctrl+L clears. `confirm_required` prints its message in yellow and
  switches the prompt to "Confirm? [y/N] "; "y"/"yes" sends `{type:"confirm", command}`, anything else cancels.
  Errors print red; close codes print our `CLOSE_TEXT` messages in red. Output normalises "\n" to "\r\n". This also
  fixes our bug "admin console `help` columns wrap" (terminal width = panel width).
- Accessibility/tests: keep a role="dialog" with an accessible name; the old tests use `role="log"` and the label
  "Console command" — xterm has its own textarea; rewrite `ConsoleModal.test.tsx` around a fake socket + a terminal
  adapter interface (inject a fake terminal like we inject `connect`), so tests still assert frames sent/received.

## 3. `files` — Files page

### 3.1 Header (page header slots)
- After the title "Files" (12 px gap): an outline badge with the current path (or the locked root when set), condensed
  face 12 px, tertiary text, 1 px border (#4f5850 dark / #a2c2fd light), max-width 352 px, truncated, full path in title.
  Measured: x 320–650, y 14–38.
- Header end (right): icon-only ghost button "Refresh files" (circular arrows; spinner while loading) → refetch.
  Our header "Upload" label-button moves into the page body (below).
  Our `PageHeader` has only an end slot: add an after-title slot (coordinate with the foundation header pattern; if the
  foundation already added it, use it).

### 3.2 Body (vertical stack, 16 px gaps)
1. **Path row** (y 75–108): path input (mono 14 px, 34 px tall, 1 px card border, input fill, flex-grow) · 8 px · small
   outlined UPPERCASE button "GO" (38×28) submits → `api.files.list(path)`; empty → error toast "Path required". 12 px
   gap, then outlined UPPERCASE "UPLOAD" (upload icon; spinner while uploading; opens a hidden multi-file input) and
   "CREATE" (folder-plus icon; opens the create-folder dialog). Outlined button = 1 px foreground border, transparent,
   foreground text, 28 px tall. Ours today has "↑ Up" + bare path text + inline "New folder" form: replace.
2. **Drop zone** (y 124–198, ~76 px tall, full width): a button-like box with 1 px DASHED border (dashed-border colour),
   faint fill, 16 px x / 12 px y padding, space-between. Left: 34 px square icon box (1 px border, upload icon, tertiary
   colour) + two lines: "DROP FILES HERE" (14 px semibold uppercase, tracking ≈0.08 em, foreground) and the current path
   (12 px mono, secondary, truncated). Right: "CHOOSE FILES" (12 px semibold uppercase tracking, tertiary; hidden on
   narrow screens). Click = open file picker. Drag-enter with files → border becomes the primary/foreground colour,
   fill primary at ~10 %, title "RELEASE TO UPLOAD"; while uploading title "UPLOADING" + spinner in the icon box.
   Disabled (no path / uploading) → 60 % opacity, not-allowed cursor. Upload loop = our `api.files.upload(here, file)`
   per file; toast "<n> file(s) uploaded." (test pins "1 file uploaded.").
3. **Table card** (border card, no padding, horizontal scroll under 672 px): a CSS grid with columns
   `name (min 192 px, flexible) | size 112 px | modified 160 px | actions 88 px`, 12 px column gap, 16 px x padding.
   - Head row 31 px: "NAME", "SIZE", "MODIFIED", "ACTIONS" (right-aligned) — 12 px semibold uppercase tracking ≈0.08 em,
     tertiary; 1 px bottom border (card border colour).
   - Parent row (only when `parent` exists), 35 px: up-arrow icon (tertiary) + ".." in mono secondary; whole row is a
     button → open parent; hover = faint fill. Replaces our "↑ Up" button (test pins "↑ Up": update it to the parent row's
     name, e.g. aria-label "Parent folder").
   - Entry rows 44 px, soft row divider between rows, none after the last; hover faint fill.
     Name cell = button: 16 px icon (folder = warning amber; file = tertiary file icon) + name in 14 px mono foreground,
     truncated. Folder click → open it; file click → **our text preview dialog** (keep: it is our behaviour; reference
     downloads instead). Size: 12 px tabular secondary, "-" for folders. Modified: 12 px secondary, absolute
     "Oct 24, 2026, 1:26 PM" style (`Intl.DateTimeFormat` medium date + short time), "-" when unknown (ours shows relative
     "28 s ago" today — change). Actions: right-aligned icon-only ghost buttons, 4 px apart: folders → open-folder icon
     ("Open <name>"); files → download icon ("Download <name>", test pins it); then trash icon in destructive colour
     ("Delete <name>", test pins it) → our confirm dialog ("Delete <name>?", our descriptions) → `api.files.remove`.
   - Loading with no data: centred spinner + "Loading files…" 14 px muted, 48 px vertical padding. Empty folder: centred
     14 px muted (test pins "This folder is empty." — keep our text). Listing error: a destructive strip at the top of the
     card (1 px bottom border destructive/20 %, fill destructive/10 %, 12 px padding, 14 px destructive text).
   - Keep `data-testid="file-<name>"` on rows and `data-testid="current-path"` (put it on the header badge or the input).
4. **Create-folder dialog** (foundation Dialog, max-width 384 px): title "Create folder", description "Target: <path>",
   one input "Folder name" (autofocus, Enter submits), footer: outlined "Cancel", filled "Create" with folder-plus icon
   (spinner while pending). Our name validation (no "/", ".", "..") stays → error toast. On success close, toast
   "Folder created", refetch. Test pins a button named "Create folder" — update the test to the new flow
   (open via "Create", submit via the dialog's "Create").
5. **Preview dialog** (ours, keep): restyle with the foundation Dialog: title = file name, description = full path
   (mono 12 px muted), pre with 1 px border, page-bg fill, 12 px mono, max-height 60 vh; footer outlined "Download"
   + filled/outlined "Close".

### 3.3 Data
All present: `api.files.list/read/mkdir/upload/remove`, `downloadFile` (ticketed). `FileListing.locked_root` decides
the header badge text. We have no `broken_link` field — skip that state. No `can_change_path`: always show the path
input (the backend refuses paths outside a locked root; show the error strip).

## 4. `docs` and `docs-direct-url`

**Reference**: sidebar item "DOCUMENTATION" (book icon) is an in-app page `/docs`: header title "Documentation", body =
a framed panel x 263–1407, y 83–869 (≈30 px top and bottom margin), white fill, 1 px light border (#fffaf5 on white),
2 px radius, containing an iframe of the public docs site (forced light colour scheme so it is readable in dark mode).
**docs-direct-url** in the reference is a bug: reloading `/docs` hits the backend's API explorer instead of the SPA.

**Ours**: `DOCS_LINK` in `shell/routes.ts` is an external link (GitHub `docs/` folder) rendered as "Documentation ↗"
in the sidebar. GitHub refuses to be framed and our index CSP allows no external frames, and our docs domain is still
open (P25-20). Decision (W52-docs-1, lead may override): make `/docs` a real page route with the same frame:
- Sidebar: normal nav item "Documentation" with a book icon, placed after System (foundation renders the item; this
  slice adds the route row with `nav: "main"` and removes the ↗ external special case — coordinate).
- Page: header "Documentation"; body = the framed panel (same geometry; white fill in both themes, forced light text
  colours inside). Inside, until a docs site exists: a centred placeholder block — book icon, 18 px heading
  "Indus Agent documentation", one 14 px sentence in our words saying the docs open in a new tab, and an outlined
  UPPERCASE button with external-link icon "OPEN DOCS" → `DOCS_LINK.href` (new tab, `noopener noreferrer`).
  Keep the URL in one constant so the page can switch to an iframe when the docs domain exists (then the index CSP needs a
  `frame-src` for that one origin — backend change, follow-up F3).
- Plugin slots `docs:top` / `docs:bottom` around the panel (our slot names follow `<page>:top/bottom`; App already
  renders `${pageSlot}:top/bottom` — check `pageSlot` covers `/docs`).
- **docs-direct-url**: do NOT reproduce the collision. Our FastAPI has `docs_url=None`, so a direct load / reload of `/docs`
  returns the SPA and must render the same Documentation page. Add a Playwright check: `page.goto(<url>/docs)` shows the
  "Documentation" heading. Capture our `docs-direct-url` pair as the SPA page (state the deliberate difference in the
  compare note).

## 5. `error-state` — the load-error notice (reference shot: Skills page with GET /api/skills → 500)

A persistent card used instead of a vanishing toast when a page's data cannot load:
- Full content width, placed where the page's content starts (y 75), followed by the normal (empty) page body.
- 1 px destructive border (#662226 / #efa3ae), fill = destructive at ~5 % (practically page bg), square corners,
  12 px x / 8 px y padding, 8 px gap, items top-aligned. Measured box: y 75–152 (78 px).
- Left: 14 px alert-circle icon in destructive colour (2 px down from the top line).
- Right column: line 1 in destructive colour 12 px: "Could not load <what>. Check that the dashboard is running and
  press Retry." (our wording); line 2, 2 px below, 12 px muted: "Details: <error message>" (only when we have one);
  then, 4 px below, an outlined small button with a counter-clockwise arrow icon "Retry" (≈80×25) → refetch.
- `role="alert"`.
- Where: the foundation owns the shared component (its list includes "empty and error states"). If it lands without a
  Retry action, this slice extends our `QueryState` (`apps/web/src/ui/controls.tsx`) with an optional `onRetry` + `what`
  and renders this notice for errors; every page passes `onRetry={() => query.refetch()}`. Page groups adopt it in their
  own slices; this slice wires it on System (per section) and Files (list).
- Compare: our current `error-state` capture is an MCP test failure (a different state). Re-capture with the kit by
  fulfilling GET `/api/skills` with HTTP 500 (Playwright `page.route`) and shooting /skills, so the pair is like for like.

## 6. `login` — sign-in page (server-rendered, `src/indus/web/auth/login_page.py`)

Reference: one fixed dark amber design (light and dark files identical; no theme switch, no prefers-color-scheme). Ours
today follows light/dark with our old clay/teal palette. Rebuild the look; keep our security properties (no script,
strict CSP by style hash, no font files, no `/assets/`, form posts to `<prefix>/auth/password-login`, redirect providers
as `<a class="go" href=…>` links, registry order, error codes via `LOGIN_ERRORS`).

Layout (1440×900):
- Body fixed dark: base #170d02. Backdrop = (a) a 3×3 px checkerboard where every other cell is amber at ~4 % (cells read
  #170d02 / ≈#1f1301) and (b) an amber radial glow at ~6 % from the top centre fading out by ~55 % of the height (top
  pixels reach ≈#2d1c02). Make the backdrop cover the whole viewport (the reference's ends at y≈695, an artefact — note it
  in the compare as a deliberate small difference).
- Content column centred, 416 px wide (x 512–927), top-anchored on short screens with ~6 vh top padding; a 0.6 s
  fade-and-rise (6 px) entrance, disabled under reduced motion (CSS only; allowed by the style hash).
- **Wordmark** (centre, cap top y≈60): "INDUS ■ AGENT" — 17 px semibold UPPERCASE, letter-spacing ≈0.32 em, amber
  #ffac02; the ■ is a 6×6 px amber square with ~0.55 em side margins, slightly raised. 28 px below it the card.
- **Card** (y 107–590): fill #1c1207 (white at 2 % over base), 1 px border #442d05 (amber ~18 %), square, inner bevel
  (1 px lighter top-left, 1 px darker bottom-right) and a large soft drop shadow below. Padding 36 px top, 32 px sides and
  bottom.
- **Title** "SIGN IN" (text "Sign in", uppercased by style): 30 px semibold, letter-spacing 0.05 em, white #ffffff;
  6 px below, subtitle 15 px #afaca8 (≈65 % white), our words, e.g. "Pick a way to sign in to this Indus Agent
  dashboard." 28 px below: the provider list, 12 px gaps.
- **Redirect provider** → full-width amber button-link (fill #ffac02, text #170d02, 12.5 px bold UPPERCASE, letter-spacing
  0.2 em, centred, 15 px vertical padding → 49 px tall (y 269–317), square, inner bevel, hover brightens ~8 %, active
  inverts, focus ring 2 px amber at 3 px offset). Text: "Sign in with <display name>" (uppercased by style). Keep
  `class="go"` (the test extracts the href by it).
- **Password provider** → a block with 12 px gaps: form title "SIGN IN WITH LOCAL ACCOUNT"-style in our words
  (e.g. "Username and password", 11.5 px semibold UPPERCASE, tracking 0.18 em, #bbb8b5); fields: label 11.5 px UPPERCASE
  tracking 0.12 em #96928d over an input 41 px tall (fill #110a02, 1 px border #644202 (amber ~35 %), white text 15 px,
  square; focus: border amber + 1 px amber ring, no outline); labels "Username", "Password" (e2e uses these labels);
  submit = amber button as above with text "Sign in" (e2e clicks `/Sign in/` — keep exactly one button matching).
- **Error** (`?error=`): our `role="alert"` message, 13 px #ff6b6b, above the providers (inside the card).
- **No providers**: keep our "No sign-in method…" sentence inside the card (test pins "No sign-in method"), in the subtitle
  style.
- **Footer** (28 px under the card): centred 12 px UPPERCASE tracking 0.1 em, white ≈45 % (#716c66), flanked by two
  24×1 px amber-35 % rules: our words, e.g. "Remote access · Sign-in required".
- Fonts: system sans stack only (CSP has no font-src; R09.22 rule on login stays). Use bold weights + tracking to get the
  look.
- Tests: `tests/unit/web/auth/test_login_page.py` hashes `LOGIN_STYLE` itself (fine), asserts the form/link markup,
  no script/img, "No sign-in method"; e2e `auth.spec.ts` uses labels "Username"/"Password" and a /Sign in/ button and the
  error copy "That username and password did not match." — all stay valid. Our `<h1>` text changes ("Sign in to Indus
  Agent" → "Sign in"); nothing pins it. pytest must run (Python changed).

## 7. `language-menu` (sidebar footer "EN")

Reference: the footer's language button (12 px "EN" text, muted) opens a popover anchored above it: 150 px wide
(x 133–282, it overhangs the sidebar edge), 300 px tall for 11 languages, surface fill, 1 px border (#31403b / #bdd4fd),
items 26 px tall, 12 px x padding, 12 px text (native names), the current one in foreground semibold with a 12 px check at
the right; others muted; hover faint fill. Closes on pick/outside click/Esc.
Ours: no locale support anywhere (no i18n, backend has none). Decision (W52-lang-1): the foundation footer renders the
"EN" button for parity; its menu lists only "English" (checked). Do not add fake languages. If the foundation omits the
button, nothing to do here. No page work.

## 8. Page-specific notes for `theme-menu` and `sidebar-collapsed` (shell = foundation)

- theme-menu: the popover (≈390 px wide, from the sidebar footer, over the page) must sit above every page layer this
  slice adds: the Files drop-zone/table, the System action-log card, the console is modal and closes the menu. Nothing
  page-side should use z-index ≥ the popover's.
- sidebar-collapsed: rail 52 px + 1 px border; page content then starts at x=75 and still ends at x=1407 (same 22–24 px
  gutters). Pages must be fluid: System host grid stays 3 columns at that width; Files grid keeps its 672 px minimum with
  horizontal scroll inside the card only; Docs frame stretches; the console modal stays centred on the window, not the
  content column.

## 9. Mapping — files to change

| File | Change |
|---|---|
| `apps/web/src/pages/SystemPage.tsx` | single-column section stack; page-level action-log state; page spinner on first load |
| `apps/web/src/pages/system/MachineSections.tsx` | Host (+ update footer, version badge), Daemon restyle; remove `UpdateSection` card |
| `apps/web/src/pages/system/CredentialSections.tsx` | Memory and Credential pool restyle |
| `apps/web/src/pages/system/OperationSections.tsx` | Operations (button card + backup/restore card), Checkpoints, Shell hooks + New-hook modal |
| `apps/web/src/ui/ActionLog.tsx` | restyle (header row with badge + close, bordered pre); keep API/testids (also used by Profiles, Channels) |
| `apps/web/src/console/ConsoleModal.tsx` | xterm-based console panel; add "Open console" entry point used by System; `ConsoleEntry` placement per foundation |
| `apps/web/src/pages/FilesPage.tsx` | header badge + refresh, path row, drop zone, grid table, create-folder dialog, preview restyle |
| `apps/web/src/shell/PageHeader.tsx` | after-title slot (only if the foundation did not add one) |
| `apps/web/src/shell/routes.ts` (+ a new `apps/web/src/pages/DocsPage.tsx`) | `/docs` page route replacing the external link (coordinate with foundation Sidebar) |
| `apps/web/src/ui/controls.tsx` | `QueryState` error → load-error notice with `onRetry` (only if foundation did not) |
| `apps/web/src/lib/format.ts` | byte format "2.6 GB" style + absolute date-time helper (check other pages' tests that pin GiB text before changing the shared function; else add a new helper) |
| `src/indus/web/auth/login_page.py` | new fixed amber style + markup (wordmark, card, title, form title, footer) |
| Tests | `pages/SystemPage.test.tsx`, `pages/FilesPage.test.tsx`, `console/ConsoleModal.test.tsx`, `tests/unit/web/auth/test_login_page.py` (only if markup assertions move), new Docs page test, Playwright `/docs` direct load |
| Compare kit (your copy only) | `capture.ts` selectors that change: "Admin console" button → System "Open console"; files "↑ Up"/"Create folder"; error-state via `/api/skills` 500; add `docs`, `docs-direct-url`, `language-menu` shots |

Data available vs missing (never invent backend features):
- Missing → omit section/control: portal card, skill curator, security audit, update skills, prompt size, support dump,
  migrate config, debug share, memory file resets, other UI languages.
- Missing → follow-up: F1 host fields (os/arch/hostname/python/uptime) in `/api/system/stats`; F2 (optional) a curator
  status API if the product wants the card; F3 docs domain + `frame-src` for the iframe.
- Present → wire: everything else listed per section above.

## 10. Build order

1. Read `docs/notes/ui-parity/web-foundation.md`; list the tokens/components you will use (Button variants filled /
   outlined / ghost / icon, Badge tones, Card, Input, Select, Checkbox, Dialog, Toast, Spinner, page header slots,
   error/empty states).
2. Load-error notice + `QueryState.onRetry` (if not in foundation) — everything else uses it.
3. `ActionLog` restyle (shared; run Profiles/Channels tests after).
4. System page: layout + Host/update → Daemon → Memory → Pool → Operations (+ restore confirm) → Checkpoints → Hooks +
   modal. Update `SystemPage.test.tsx` as you go.
5. Console modal on xterm + System "Open console"; rewrite its tests with a fake terminal.
6. Files page; update `FilesPage.test.tsx`.
7. Docs page + route + Playwright direct-load check.
8. Login page (Python) + pytest for `tests/unit/web/auth`.
9. Re-capture both themes with your kit copy; side-by-side PDF; close gaps.

## 11. Compare checklist (tick per theme: L = light, D = dark)

System
- [ ] L [ ] D single column, sections 32 px apart, heading icon + muted tracked heading, square bordered unfilled cards
- [ ] L [ ] D Host: 3-col tiles (uppercase labels / values), version badge, divider + "Check for updates" footer with message
- [ ] L [ ] D Daemon row: badge + description left, START / RESTART / STOP (warning) right; stop confirm still works
- [ ] L [ ] D Memory: provider readout row + divider + built-in sizes; select/Save/Reset work
- [ ] L [ ] D Pool: 1:2:1 form, right ADD KEY, bordered entry rows with badges + trash
- [ ] L [ ] D Operations: icon ghost buttons; backup/restore card with dividers; restore confirm
- [ ] L [ ] D Checkpoints summary + Prune; Hooks header with NEW HOOK, hook cards, modal
- [ ] L [ ] D action log card on top with running/done/exit badge and close
Console
- [ ] L [ ] D 960×738 panel at y 81, 72 px header with icon box, INDUS CONSOLE, status badge + profile, close X
- [ ] L [ ] D xterm body (black dark / light canvas in black frame), prompt "indus>", help output does not wrap
Files
- [ ] L [ ] D header path badge + refresh icon
- [ ] L [ ] D path input + GO, UPLOAD, CREATE outlined buttons at y 75–108
- [ ] L [ ] D dashed drop zone 76 px with icon box, DROP FILES HERE, path, CHOOSE FILES; drag state
- [ ] L [ ] D grid table: head 31 px uppercase, ".." row, 44 px rows, amber folder icons, open/download + red trash icons, absolute dates
- [ ] L [ ] D create-folder dialog; preview dialog; delete confirm
Docs
- [ ] L [ ] D sidebar "Documentation" is an in-app item; white framed panel 263–1407 × 83–869 with placeholder + OPEN DOCS
- [ ] direct load / reload of /docs shows the same page (no API explorer)
Error state
- [ ] L [ ] D /skills with forced 500: red-bordered notice at y 75, icon, red line, muted details, outlined Retry; Retry refetches
Login
- [ ] fixed dark amber page in both themes: wordmark, 416 px card, SIGN IN, subtitle, amber buttons, form, footer rules
- [ ] wrong password error shows; CSP unchanged (`default-src 'none'`), no script, no font files
Language / theme / rail
- [ ] EN menu (English only, checked) if the foundation shows the button
- [ ] theme popover above all page layers; collapsed rail: content x 75–1407, pages fluid

Decisions to record: W52-docs-1 (docs placeholder page), W52-lang-1 (English-only menu), W52-sys-1 (omit sections
without APIs), W52-sys-2 (keep our checkpoint rules inputs and overwrite checkbox), W52-files-1 (keep text preview on file
click), W52-login-1 (fixed amber look, system fonts, full-viewport backdrop).
