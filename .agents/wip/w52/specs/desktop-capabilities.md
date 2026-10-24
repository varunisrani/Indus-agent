# Wave 52 spec: desktop-capabilities (Capabilities page + Messaging + Bots tab)

Status: spec ready for the wave-52 page builder. Read + Note only; written in our words; no reference code, CSS,
class names or long copy below. Short labels (1-4 words) match the reference where noted.

Read: reference screenshots `$SP/compare/hermes/desktop/{capabilities,skills,tools,mcp,capabilities-plugins,messaging,
bots-tab}--{light,dark}.png` (1440x900); ours `$SP/compare/indus/desktop/{capabilities,skills,tools,mcp,plugins,
messaging,bots}--*.png`; reference source (behaviour only) under `apps/desktop/src/app/{capabilities,messaging}/`,
`app/master-detail.tsx`, `app/page-search-shell.tsx`, `app/overlays/panel.tsx`, `plugins/hermes-bots/{roster-pane-
toolbar,bot-row,roster-sections}.tsx`; our code `apps/desktop/src/app/capabilities/*`, `app/messaging/*`, `bots/*`,
`sidebar/Sidebar.tsx`, backend routes `src/indus/web/routes/{skills,mcp,plugins,messaging,learning,config_env}.py`.
Pixel samples: `$SP/w52-specs/cap-tools/samples.txt` (script `sample.py`, PNG helper `png.py`, zoom tool `crop.py`).

`$SP` = /tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad

---------------------------------------------------------------------------------------------------------------------

## 0. Slug map (reference vs ours) and capture-kit fix

| reference slug | what it shows | our current slug | note |
|---|---|---|---|
| `capabilities` | Capabilities page, default tab = Skills (identical to `skills`) | `capabilities` (shows Plugins tab!) | kit mislabels: ours must capture the default (Skills) tab |
| `skills` | Capabilities > Skills | `skills` | |
| `tools` | Capabilities > Tools (`?tab=toolsets`) | `tools` | |
| `mcp` | Capabilities > Connectors (`?tab=connectors`) | `mcp` | |
| `capabilities-plugins` | Capabilities > Plugins (`?tab=plugins`) | (none; ours is in `capabilities`) | add capture of `?tab=plugins` as `capabilities-plugins` |
| `messaging` | Messaging page, Telegram selected | `messaging` | |
| `bots-tab` | sidebar switched to its BOTS tab, a chat open in main area, right "Scheduled jobs" edge tab | `bots` (a full page) | capture must click the sidebar BOTS tab |

When re-capturing, copy `$SP/compare/work-i-desktop/kit` into your `$S` and fix the three slug entries above
(never edit the original kit).

## 1. Shared frame (from the wave-51 desktop foundation; listed so the page builder knows what it relies on)

- Window 1440x900. Left sidebar 260 px wide (x 0-259), its background is the "chrome" tone: light `#f6f8fa`,
  dark `#010409`. A 1 px soft seam at x 260 (`#fbfcfd` light / `#0d1117` dark, 2 px).
- Title-bar band y 0-33 in chrome tone with the right icon cluster (foundation). Status bar y 880-899 in chrome tone
  (foundation: "Backend ready", folder, "Smart").
- Main surface (x 262-1440, y 34-879): page background light `#fefefe`, dark `#0d1015`.
- Sidebar nav (foundation): New session, Capabilities, Messaging, Artifacts, Scheduled jobs. The active nav row is
  the "selected row" tint (light `#dfe6f4`, dark `#1b2331`) with a 1 px primary-tinted outline in light. Our
  sidebar currently has extra items (Webhooks, Command center, Learning map, Bots, Settings...): the foundation
  decides; this slice only needs the Capabilities and Messaging entries to route here and the Bots entry to be
  replaced by the BOTS sidebar tab (section 8).
- Fonts: the foundation's sans (Inter-like) and mono. All sizes below are px at a 16 px root.

### 1.1 Colour roles used on these pages (sampled from the reference PNGs)

| role | light | dark | where |
|---|---|---|---|
| page bg | `#fefefe` | `#0d1015` | every main surface |
| chrome bg (sidebar/status) | `#f6f8fa` | `#010409` | sidebar, status bar, bots edge tab |
| selected row bg | `#dfe6f4` | `#1b2331` | active list row, active backend card, bot row |
| inset panel bg | `#f0f2f8` | `#151a22` | skill meta box, plugins table header band, tool-count chips |
| inset panel border | `#ced9ef` | `#232d40` | meta box, plugin header band top line |
| primary-tint line | `#c8d5f1` | `#232f47` | hub divider + hub frame, quick-setup card border |
| hairline (rows/underline) | `#dae3f4` | `#1d2535` | plugin row separators, connectors search underline |
| card border | `#e8edf7` | `#1f2330` | connector cards |
| card bg | `#fdfdfd` | `#16181d` | connector cards |
| text primary | `#2c3035` | `#d8e0e6` | titles, labels |
| text secondary | `#595c60` | `#adb4b9` | list row text (messaging), card description |
| text tertiary | `#86888b` | `#81878d` | descriptions, captions, inactive tabs, section titles |
| text quaternary | `#adafb0` | `#5b6065` | sort strip, hint lines, versions, "BOTS" label |
| text faint | `#c1c2c4` | `#464a50` | "changes apply" footer, disabled-row subtitle |
| placeholder | `#d9dada` | `#303439` | page search placeholder |
| primary | `#0053fd` | `#4a84fe` | switch on, primary buttons, tab underline (sidebar), ready pills text |
| primary disabled button | `#7ea7fd` | `#2b4a8a` | Save changes with nothing to save |
| primary-tint pill bg | `#c8d7f4` | `#202c46` | Ready / In use pills (text = primary) |
| muted pill bg / text | `#f6f6f6` / `#828487` | `#1a1e24` / `#878d94` | bundled, Disabled, Needs setup, category |
| category pill (skill) | `#e9e9ea` / `#7c7e81` | `#21242a` / `#8b9097` | skill detail category pill |
| good pill bg / text | `#e4f7f0` / `#009966` | `#0c211f` / `#5ee9b5` | Recommended |
| warning text | `#e17100` | `#ffd230` | backend "needs" reasons (with a triangle icon) |
| meta chip (on selected row) | `#d2dbee` / `#727884` | `#222b3d` / `#8a929f` | "×2" on the active row |
| status dot off | `#cecfd0` | `#3c3f44` | messaging platform rows |
| bot avatar sample | `#8b5cf6` | `#8b5cf6` | default bot face colour (ours: seeded per bot) |

Switch (size "xs"): 28x16 px pill. On: track = primary, thumb 12 px circle at the right; thumb is white in light,
page-bg (`#0d1015`) in dark. Off: track = page bg with a 1 px faint grey border, thumb grey (`#7f8285` light,
`#878d93` dark) on the left; the whole off switch is at 60% opacity. Disabled while a write is in flight.

Icon buttons ("ghost xs"): 20x20 visible box, 4 px radius, tertiary ink, hover = selected-row tint + primary text;
give them a 24 px hit area. Icons: use lucide-react (already in our deps) — no codicons (their licence is not on our
allowed list), no reference icon files.

---------------------------------------------------------------------------------------------------------------------

## 2. Capabilities page shell (slugs `capabilities`, `skills`, `tools`, `mcp`, `capabilities-plugins`)

### 2.1 Layout
- No page title row any more (our current "Capabilities" h1 + bordered header goes away). The header is ONE row,
  vertically centred at y≈56 (top padding = title-bar height + 8 px; bottom 8 px), 12 px side padding, laid out as a
  three-cell grid `1fr | auto | 1fr`:
  - left cell: page search field (Skills and Tools tabs only; hidden on Connectors and Plugins). Borderless, no box:
    a 13 px magnifier icon in placeholder ink at x 276-288, then the input text at x 294, 13 px, placeholder
    "Try "<term>"" rotating through terms drawn from the loaded data (skill categories/names; toolset tool names). Max
    width 45% of the viewport.
  - centre cell: the tab row, centred on the page (x≈722-973 at 1440). Text tabs, no pills/boxes: "Skills",
    "Tools", "Connectors", "Plugins" (13 px, regular; active = text primary, inactive = text tertiary; hover =
    primary text). Active tab has a 1 px underline in text-primary colour ~4 px below the baseline (spans the label
    only). Skills and Tools carry a count superscript after the label: 9 px, quaternary ink, raised ~2 px
    (e.g. "Skills 53", "Tools 23"). Count is `null` while loading → a tiny skeleton bar; Connectors/Plugins show no
    count. Gap between tabs ≈16 px. When the header is too narrow for search + tabs, the tabs collapse into a
    dropdown (button with active label + chevron).
  - right cell: trailing slot (empty in the shots). Put our profile scope selector here when more than one profile
    exists (compact borderless select, 12 px, tertiary ink); hidden for a single profile.
- Body below fills the rest (no border between header and body), page bg.
- Loading: a centred loader with a short label until both the skills and toolsets lists answer (Skills/Tools tabs).
  Error: centred empty-panel (icon, "could not load" title, the message, a small "Refresh" button).
- Keyboard: the app's refresh hotkey (Ctrl/Cmd+R inside the page) reloads skills, toolsets and the hub list.

### 2.2 Shared "master-detail" frame (Skills, Tools; Messaging uses the narrow variant)
- Two columns. "Wide" variant: list column = 0.75 fr, detail = 1 fr (at 1440: list x 260-750, detail 750-1440).
  The seam between them is a drag sash (invisible 4 px hit strip; on hover/drag a 1 px line in `#ced9ef`/`#232d40`
  appears); min list 180 px, min detail 320 px; width persists per page id; double-click resets.
  "Rail" variant (Messaging): list column fixed 224 px.
- List column: 8 px padding all round; scroll area with a stable gutter.
  - Strip above the list (24 px tall, 8 px left / 4 px right inset, 4 px below): left = sort toggle text button
    "↓ Most used" (11 px medium, quaternary ink; flips to an up arrow + "Least used"); right = vertical kebab
    (ghost icon button) opening a small menu: first line "All" with a mini switch (enable/disable everything in the
    tab, not just the search hits; mixed state reads as off), then "Disable unused" (Skills only).
  - Rows ("cap row"): full width, 6 px radius, 44 px tall when there is a subtitle, 32 px otherwise; left padding
    8 px. Title 12.5 px medium in text primary at ~85% when on, regular weight + 60% muted when off; subtitle under
    it 10 px faint-tertiary (truncate, single line). Right side: optional meta chip (rounded 4 px, inset-panel bg,
    9.6 px tabular, tertiary ink: "×N" usage or "N tools"), then the switch with 6 px right margin. Selected row =
    selected-row bg; hover = subtle row hover. Clicking the row selects it; the switch toggles without selecting.
    Rows are at y 115, 159, 203 … (44 px pitch, no gaps).
- Detail column: content centred, max width 672 px, 20 px side padding, 16 px top, 20 px gaps between blocks;
  scrolls on its own. Footer caption pinned under the scroll, right aligned, 10.4 px, faint ink: "Changes apply to
  new sessions." (our words, short; may match). Messaging uses an action bar instead of the caption (section 7).
- Detail header block: title 15 px semibold, tight tracking, text primary; pills inline after it (8 px gap);
  description below (4 px), ~12 px caption size, tertiary ink.
- Empty (no rows / nothing matches): one centred panel across both columns: small search icon (muted), title
  "No <noun> found", one line "Nothing matches "<q>"" or "None available yet" (our words).

## 3. Skills tab (slugs `skills`, `capabilities`)

### 3.1 Upper part (master-detail, wide)
- List rows: title = skill name; subtitle = category in Title Case (e.g. "Productivity", "Creative"); a small badge
  after the category for origin: "learned" (agent-made; default badge tone) or "hub" (muted). Meta chip "×N" only
  when the usage count > 0. Switch = enabled.
- After the installed skills, optionally a sub-heading row (28 px, 10 px uppercase, wide tracking, quaternary):
  "Official catalog", then catalog rows whose switch slot holds a text button "Install" (spinner + "Installing"
  while running). Hidden when there is nothing to show.
- Selection defaults to the first visible row and re-falls to the first when the search filters it out.
- Detail (installed skill):
  - header: name + category pill (rounded-full, 10 px medium, `#e9e9ea`/`#21242a` bg) + origin pill ("learned"
    good tone / "hub" muted) when relevant; description.
  - for editable (local/learned) skills: two small text buttons "Edit" and "Archive" (archive in danger colour).
  - meta box: 12 px padding, 8 px radius, inset-panel bg + border; rows of key/value at 11 px, 16 px line, key
    column 96 px wide in tertiary medium, value in secondary, wraps.
  - below it the full SKILL.md body in a monospace pre box with the same box style (11 px, relaxed leading).
- Footer caption "Changes apply to new sessions."
- Optional bottom editor pane for "Edit": full-bleed strip docked under both columns, 36 px header (file name in
  11 px muted, Save button, collapse chevron, close), resizable top edge, code editor body.
- Interactions: toggling is optimistic (row repaints at once; on failure it flips back and a toast says it failed);
  bulk actions run one by one and end with a success toast "Updated N" (our words); the slash-command cache must be
  invalidated after toggles (ours: the composer's `/` list).

### 3.2 Lower part: Skills Hub section (always visible on the Skills tab)
- Starts at y 439 with a 1 px primary-tint line across the page; resizable from that top edge (default body
  ≈380 px; min 120; max 75% of viewport; always leaves ≥176 px for the list above); collapse state persists.
- Header row (32 px, 12 px side padding): left "Skills Hub" 11 px medium tertiary; right two text buttons
  "Update installed" and "Hide the hub browser" (11 px, tertiary; hover primary). When collapsed the second label
  reads as "Show …" and only the header remains.
- Body: a framed box 12 px inset (x 272-1428), 1 px primary-tint border, 8 px radius, page bg inside. In the
  reference it embeds the remote hub website; in the shot it is empty (offline).
- Hint line under the box: 10.4 px quaternary, our words ("Pick a skill from the hub to add it to this profile").

### 3.3 Our data and mapping (Skills)
| element | our source | status |
|---|---|---|
| list rows (name, description, category, enabled, source) | `GET /api/skills` (profile scoped) | available |
| toggle | `POST /api/skills/toggle {name, enabled}` | available |
| origin badge | `source` (`hub` → "hub"); learned = `createdBy:"agent"` from `GET /api/learning/graph` node `skill:<name>` | available (join) |
| usage "×N" + Most/Least used sort | `useCount` of `skill:<name>` nodes in `GET /api/learning/graph` | available (join; skills without a node count 0) |
| Archive | `DELETE /api/learning/nodes/skill:<name>` (confirm dialog first) | available |
| Edit SKILL.md / full body / frontmatter (version, author, license, platforms, prerequisites, metadata) | none | **gap**: hide Edit; meta box shows only what we have — name, description, category, source, created by; no body box |
| Official catalog rows | none (no catalog list endpoint) | **gap**: omit the sub-section |
| Hub section body | our own hub search instead of an iframe: search field + result rows inside the framed box, from `GET /api/skills/hub/search?q=`; each row name / description / source / trust; "Install" opens our existing confirm dialog → `POST /api/skills/hub/install`; installed rows show "Installed" | available (we never embed a remote site) |
| "Update installed" | `POST /api/skills/hub/update {}` (report → toast) | available |
| Install deep link `?install=` | keep current confirm-first flow | keep |
| page search filter | client-side over name/description/category | available |

## 4. Tools tab (slug `tools`)

- Same master-detail. List rows: title = toolset label; subtitle = its description (or the tool names joined) ;
  meta chip = "N tools" (or "×N" when usage known); switch = enabled. Off rows dim (title `#b5b6b8`/`#53575d`).
- Detail: header (label; a warn pill "Needs keys" only when not configured; description), then a wrap of monospace
  tool chips (10.4 px mono, 6 px radius, inset-panel bg, tertiary ink, optional "×N" suffix in quaternary).
- Terminal toolset adds an "Execution backend" block: label row (13 px medium primary ink, refresh icon button at
  the right), then one card per backend: 10 px padding, 6 px radius; name 13 px medium + status pills; one-line
  description 12 px tertiary; optional warning line 12 px warning colour with a triangle icon. The backend in use is
  the selected card (selected-row bg, 1 px `#afc2e9`/`#2d3d5d` border) with pills "✓ Ready" and "✓ In use"
  (primary-tint pills); others carry a muted "⚠ Needs setup" pill. Clicking a card selects that backend.
- Footer caption as Skills (here pinned at the bottom of the detail column, y≈859).

| element | our source | status |
|---|---|---|
| rows (name, label, description, enabled, tools) | `GET /api/tools/toolsets` | available |
| per-toolset switch + "All" bulk | no write route for toolsets | **gap**: show the switch, wire it to `PUT /api/config` writing the CLI tool selection (`platform_toolsets.cli`) through our settings writer (`settings/settingsApi.ts`), only if the config schema exposes that key; otherwise render the switch disabled with a tooltip "Change in Settings" — do not add a backend route in this slice |
| usage "×N", Most used sort | none | **gap**: chip shows "N tools"; sort strip flips A–Z / Z–A (our words) |
| "Needs keys" pill | none | **gap**: omit |
| Execution backend list | current value `terminal.backend` via `GET /api/config`; options = our own static list (local, docker, ssh, singularity, modal, daytona, vercel_sandbox, our short descriptions) | partial: "In use" pill from config; **no readiness probe** → no Ready/Needs-setup pills and no warning lines; selecting writes `terminal.backend` via `PUT /api/config` |

## 5. Connectors tab (slug `mcp`)

- No page search in the header (tabs row only, at y≈56). Body has 16 px side padding, 12 px vertical gaps.
- Title row: "Connectors" 14 px semibold primary ink at left; right an outline small button "Add your own"
  (24 px tall, 1 px `#e3eaf8`/`#18202e` border, 6 px radius, 12 px label).
- Search row: borderless search field "Search N apps" (13 px, magnifier icon), full width, with a 1 px hairline under
  it (`#dae3f4`/`#1d2535`) and 6 px bottom padding.
- Optional segment control row (All / Connected / … with counts) only when there are >2 segments.
- Group header: "Available" 12 px semibold + count 12 px tertiary tabular (8 px gap).
- Cards: 2-column grid, 12 px gap (cards x 276-840 and 852-1416; 66 px tall; 12-13 px between rows). Card: 1 px card
  border, 8 px radius, card bg, 12 px padding, 12 px gap; hover = slightly darker; selected = primary border +
  selected-row bg.
  - left: 36 px logo tile, 9 px radius (brand logo, or a letter tile: tinted `#dee5f3`/`#232a38` with the initial in
    tertiary ink).
  - middle: line 1 = name 13 px semibold + kind word 11 px tertiary ("Managed", "MCP · Catalog", or for ours
    "Local"/"Remote") + a small verified check icon (muted); line 2 = description 11.5 px secondary, clamp 2 lines;
    or a reason line in the state colour.
  - right lane (124 px, right aligned, 4 px gap): state word 11 px secondary with a 5 px dot when connected/error;
    under it the action button: small outline button "Connect" / "Install" (22 px tall, 12 px label); for local
    servers a mini switch instead.
- Clicking a card opens the connector dialog (details, sign-in, test, remove). Empty: centred panel with plug icon,
  "No connectors yet", and the "Add your own" button. Load failure of a remote catalog: error banner with "Retry".

| element | our source | status |
|---|---|---|
| the list | `GET /api/mcp/servers` (our configured servers only) | available |
| hosted/managed catalog of 100+ apps, logos, "Managed"/"Catalog" kinds, verified marks | none | **gap**: our grid shows only configured servers under a group "Configured N" (our label); kind word = "Local" (command) / "Remote" (url); no logos → letter tiles; no verified mark |
| enable switch on card | `PUT /api/mcp/servers/{name}/enabled` | available |
| card click → dialog | dialog with: transport/command/url (redacted as served), "Test" (`POST …/test` → "N tools answer"), "Sign in" for oauth remote (`POST …/auth` → open URL; poll `GET /api/mcp/oauth/flows/{id}`), "Remove" (`DELETE`, confirm) | available (move today's inline buttons into the dialog) |
| "Add your own" | dialog with today's form (Name, Command, Address, OAuth checkbox) → `POST /api/mcp/servers` | available (move the inline form into a dialog) |
| search "Search N apps" | client filter over configured servers | available |
| state word + dot | `enabled` (On/Off); test result when run | partial |

## 6. Plugins tab (slug `capabilities-plugins`)

- No page search. Top block (12 px padding top, 8 px bottom, 12 px sides): left a caption-size tertiary blurb (our
  words: a plugin can add to the app, the agent or both, and each half has its own switch); right a toolbar:
  secondary button "Install from Git" (28 px tall, filled `#e9edf6`/`#171d28`, 6 px radius, 13 px label), then
  ghost icon buttons: folder-open (open the plugins folder) and refresh (rescan).
- Table (full width of the page, edge to edge):
  - header band 28 px: inset-panel bg, 1 px inset-panel border top and bottom; 11 px tertiary text; first column
    empty; then two fixed 144 px columns: "▢ Desktop" (monitor icon) and "▢ Agent in <profile>" (package icon,
    truncates; when several profiles exist the profile select sits inside this header cell).
  - rows: 10 px vertical / 12 px horizontal padding, 1 px hairline between rows. Cell 1: line 1 = name 13 px medium
    primary + version "v2.0.0" quaternary + kind badge (1 px tertiary-border outline, 4 px radius, 10.4 px, icon +
    "Agent"/"Desktop"/both) + provenance pill "bundled" (muted pill); line 2 = description, caption size,
    secondary/tertiary, wraps (max ~730 px). Desktop cell / Agent cell: the switch for that half, or an em dash
    (quaternary, 36 px wide) when the plugin has no such half. Optional trash icon for removable plugins.
- Bottom "Plugin catalog" section: same resizable bottom-section pattern as the Skills Hub, header label in
  10 px uppercase wide tracking quaternary ("PLUGIN CATALOG"), right "Hide the catalog browser"; framed box; hint
  line under it (our words).
- Empty list: one caption line "No plugins yet." + hint. Error: centred panel + Refresh.

| element | our source | status |
|---|---|---|
| agent half (name, version, description, enabled, source, sha) | `GET /api/plugins` | available ("bundled" pill from source) |
| agent switch | `POST /api/plugins/{name}/enable|disable` | available |
| desktop half | `DesktopPluginSource.list()` (contribution registry, P24-32) — the bundled Bots surface must appear here as a Desktop row with a working switch (its register/unregister) | available |
| Install from Git | dialog holding today's form (repository + 40-char commit, same validation) → `POST /api/plugins/install` | available (form moves into a dialog) |
| open folder / rescan | folder: our bridge if it can open the plugins dir, else hide; rescan = reload both lists | partial |
| uninstall (trash) | `DELETE /api/plugins/{name}` is NOT a route (today we show "not available") | **gap**: hide the trash icon |
| Plugin catalog | none | **gap**: render the section collapsed by default with a placeholder line in the box (our words: "No catalog on this backend yet"), or omit — builder's call, record the decision |

## 7. Messaging page (slug `messaging`)

### 7.1 Layout
- Same page shell as Capabilities but no tabs: header row has only the search field at left ("Try "matrix"",
  rotating platform names) and, at right, status filter text-tabs ("All", "Connected", "Off", … our labels) only when
  more than one status tone is present.
- Optional profile scope strip (hidden for single profile) under the header with a bottom hairline.
- Master-detail, rail variant: list column 224 px (rows x 268-468), detail fills the rest.
- Platform rows: 36 px tall with 4 px between rows (40 px pitch: rows start at y 87, 127, 167 …), 6 px radius,
  8 px x padding, 8 px gap: 24 px avatar tile (4 px radius; brand glyph on a ~20% brand-tint background, or the
  platform initial on a tint), name 13 px regular secondary ink (selected: primary ink + selected-row bg), at the far
  right an optional amber count bubble (pending pairing requests) and a 6 px status dot (off grey
  `#cecfd0`/`#3c3f44`, good green, warn amber, bad red).
- Detail column (centred max 672 px, content x 642-1274 at 1440):
  - header: 24 px avatar tile + title 15 px semibold + state pill (rounded-full, 10.5 px medium, dot + label, e.g.
    "Disabled" muted) + "Needs setup" muted pill when not configured (+ "Gateway stopped" when relevant);
    description under it (caption, tertiary).
  - error banner when the platform reports an error.
  - "PENDING REQUESTS (N)" and "APPROVED USERS (N)" sections only when non-empty: list rows title/description +
    "Approve" (secondary small) or "Revoke" (ghost; confirm dialog, destructive).
  - Telegram only: section "QUICK SETUP": a card (1 px primary-tint border, 6 px radius, 12 px padding): "Quick
    setup" 13 px medium + green "Recommended" pill; caption text (our words); primary button with a QR/scan icon
    (24 px tall, primary bg, white 12.5 px label, 4 px radius).
  - "GET YOUR CREDENTIALS": caption paragraph (tertiary) + a strong text link "Open setup guide ↗" (underlined,
    13 px, tertiary→primary on hover).
  - "REQUIRED", "RECOMMENDED" sections, and a collapsible "ADVANCED (N)" header with a caret.
  - Section titles: 11 px semibold uppercase, letter-spacing 0.14 em, tertiary ink; 12 px margin to content;
    sections 20 px apart.
  - Field rows: label 13 px medium primary (+ "Saved" 10.5 px primary-colour tag when set); help line 12 px tertiary;
    below, the input (full row width minus 40 px, 32 px tall, 1 px very light border `#ecf2fe`/`#0f141e`, 6 px radius,
    page bg, 13 px, placeholder tertiary; password type for secrets, placeholder = masked preview when set) and to
    its right 32 px ghost icon buttons: external-link (field docs) and trash (clear a set single value). List fields
    (allowed IDs): one input per entry with an × to remove, then "+ Add another".
- Action bar pinned at the bottom of the detail column (page bg, 10 px vertical padding, same 672 px centring):
  left a mini switch + "Disabled"/"Enabled" (12 px tertiary); right "Unsaved changes" hint (12 px) when edited and
  the primary "Save changes" button with a save icon (28 px; disabled look `#7ea7fd`/`#2b4a8a` when nothing changed).
- Restart-needed alert at the top of the detail (warning alert, "Restart now" secondary button with spinning icon).

### 7.2 Our data and mapping
| element | our source | status |
|---|---|---|
| platforms (id, name, description, installed, install_hint, enabled, configured, state, reason, fields) | `GET /api/messaging/platforms` | available (api_server/webhook hidden by backend) |
| status dot / state pill tone | our `TONES` map (connected ok; pending_restart, retrying, daemon_stopped, not_configured warn; fatal bad; disabled off) | available |
| save | `PUT /api/messaging/platforms/{id} {enabled?, env?}` | available |
| enable switch in action bar | same PUT with `enabled` (save immediately, like the reference) | available |
| clear (trash) | `PUT … {clear_env:[key]}` | available (backend supports `clear_env`) |
| pending / approved users, approve, revoke | `GET /api/pairing`, `POST /api/pairing/approve`, `POST /api/pairing/revoke` | available |
| Telegram quick setup | our `TelegramQrSetup.tsx` flow (open BotFather, paste token, detect user from first message). There is **no QR create flow** → button label is ours ("Set up in Telegram"), icon a send/arrow, not a QR | available, restyled |
| field label + help text + docs URL per field, platform docs_url | backend sends only the env key | **gap**: a client-side copy map for known keys in our own words (Telegram token, allowed users, home channel …); fallback = the raw key in mono; no external-link icon unless we have our own docs URL; hide "Open setup guide" link (or point to our docs page if one exists) |
| required / recommended / advanced split | `required` flag + `kind` (`policy` → recommended; other optional settings → advanced) | available |
| "Test" button (ours today) | `POST …/test` | keep as a ghost button in the action bar left of Save (not in reference; keep behaviour) |
| not-installed platforms | `installed:false` + `install_hint` | show the detail with a muted note + hint instead of fields |
| platform logos | no reference assets; options: (a) letter tiles with our own per-platform tint (default), (b) `@icons-pack/react-simple-icons` at an exact pin IF the licence guard accepts it (package MIT, icons CC0 — lead decides) | decision `W52-x-1` |

## 8. Bots tab in the sidebar (slug `bots-tab`)

### 8.1 Layout
- The sidebar gets a two-tab strip at its top, under the title band (y≈40-62): "SESSIONS" and "BOTS", 11 px
  medium, uppercase, wide tracking; active = text primary, inactive = tertiary; active tab has a 2-3 px primary
  underline the width of the tab (x 74-121 for BOTS) and a 1 px divider between the tabs. If the foundation sidebar
  already has this strip, only fill the BOTS panel. Strip only shows when the Bots desktop plugin is on.
- BOTS panel background = page bg (`#fefefe`/`#0d1015`), not the chrome tone.
  - top: a centred text button "Refresh gateway groups" (12.5 px, secondary ink) at y≈76.
  - section header row (y≈104-118, 10 px side padding): "BOTS" 11 px semibold uppercase wider tracking quaternary;
    right two 20 px ghost icon buttons: bell / bell-off (activity toasts on/off) and plus (menu: "New bot",
    "New group" — disabled with <2 bots, "New section").
  - bot rows: 6 px inset, 6 px radius, 8 px padding, 10 px gap, ~52 px tall: avatar 24-34 px rounded-square face
    (ours: `BotFace` seeded) ; middle: line 1 = small presence ring (hollow 7 px circle, tertiary) + name 13 px medium
    primary, time-ago 11 px quaternary at the right ("15m"); line 2 = last message preview 12 px tertiary truncated
    (italic when the bot wrote it). Selected/open = selected-row bg; hover = chrome hover. Click opens that bot's
    forever-chat in the main area; rows are draggable into sections.
  - sections (user-made) render as collapsible headers with counts; hidden bots go under a hidden group.
- Right edge "SCHEDULED JOBS" tab: a 26 px wide vertical tab at the window's right edge (x 1414-1440, y 36-150) in
  chrome tone, text rotated 90°, 10 px uppercase tracking, tertiary ink; a 1 px primary-tint line runs down the
  main area's right edge (x≈1412). Clicking opens the routines (cron jobs) pane for the selected bot.
- Main area in the shot is an ordinary chat (transcript + composer) — owned by the transcript/composer slices.

### 8.2 Our data and mapping
| element | our source | status |
|---|---|---|
| roster (profile, avatar, hidden, sections, routines, canonical session id) | RPC `bots.list` | available |
| open chat | `CanonicalChats.open(profile)` → navigate to session | available |
| preview + time-ago | join `canonical.session_id` with the session list (`session.list`: `preview`, `last_active`) already loaded by the sidebar | available (join) |
| presence ring | none | **gap**: render the hollow ring in neutral (no live state) or omit |
| new face / upload picture / hide / sections | RPC `bots.update` — move today's card buttons into a row context menu (right-click / kebab): "New face", "Upload picture", "Hide"/"Unhide", "Move to section…" | available |
| groups ("Refresh gateway groups", group rows) | RPC `groups.list` (refresh = reload `bots.list` + `groups.list`); a group row opens `GroupRoom` | available |
| "New bot" | none (bots are profiles) | **gap**: menu item routes to the Profiles page "new profile" flow; "New group" only if a create-room RPC exists (none seen: `groups.*` has list/send/stop/disband…) → hide "New group" |
| activity toasts bell | local per-window preference (persist in our prefs store) | UI-only |
| Scheduled jobs edge tab | routines from `bots.list`, details via `/api/cron/jobs` | available; the pane itself can reuse the cron page list filtered by profile |
| Bots page route + nav item | today `botsContribution` adds route `bots` + nav entry | replace the nav entry by the sidebar tab; keep the route for deep links or drop it (decision `W52-x-2`); the Plugins tab Desktop switch must still toggle the whole surface |

---------------------------------------------------------------------------------------------------------------------

## 9. Files that change (ours)

- `apps/desktop/src/app/capabilities/CapabilitiesPage.tsx` — new header (search | centred text tabs with counts |
  scope), drop h1 and bordered header; tab counts become a single number (skills total, toolsets visible total);
  `capabilities.summary` still feeds the counts when present, else counts come from the loaded lists.
- Split `apps/desktop/src/app/capabilities/tabs.tsx` into `skills/SkillsTab.tsx` (+ `skills/HubSection.tsx`),
  `toolsets/ToolsetsTab.tsx` (+ `toolsets/BackendPanel.tsx`), `connectors/ConnectorsTab.tsx` (+ card + two dialogs),
  `plugins/PluginsTab.tsx` (+ install dialog); keep exported `mergePlugins`, `FULL_SHA`, row types (tests import them).
- New shared `apps/desktop/src/app/masterDetail.tsx` (MasterDetail with sash, ListColumn, ListStrip + kebab menu,
  CapRow, DetailColumn with footer/action bar, DetailHeader, ToolChip, EmptyPanel, BottomSection with resizable top
  edge and persisted height/collapse). Use the foundation's Switch, Button, Pill/Badge, SearchField, Dialog, Toast
  primitives (see `docs/notes/ui-parity/desktop-foundation.md` once it lands); if one is missing, add it to the
  foundation component folder, not page-local.
- `apps/desktop/src/app/capabilities/strings.en.ts`, `app/manage/strings.en.ts` (messaging/bots keys), `i18n/en.ts`
  spread — new labels: Tools (tab label changes from "Toolsets"), Most used / Least used, All, Disable unused,
  Changes apply…, Skills Hub, Update installed, Hide/Show the hub browser, Add your own, Search N apps, Available/
  Configured, Install from Git, Desktop, Agent in {profile}, bundled, Plugin catalog, Quick setup, Recommended,
  Get your credentials, Required, Recommended, Advanced ({n}), Saved, Add another, Unsaved changes, Save changes,
  Enabled/Disabled, Sessions, Bots, Refresh gateway groups, Scheduled jobs.
- `apps/desktop/src/app/messaging/MessagingPage.tsx` → master-detail page (+ `PlatformRow`, `PlatformDetail`,
  `ActionBar`, `fieldCopy.ts` client copy map, `PlatformTile.tsx`); `TelegramQrSetup.tsx` restyled into the Quick
  setup card (logic unchanged). Retire `manage.css` card usage here (keep `RestartBanner` logic, restyle as alert).
- `apps/desktop/src/sidebar/Sidebar.tsx` (+ `sidebar.css` or tokens) — tab strip if foundation lacks it; new
  `apps/desktop/src/bots/BotsRoster.tsx` (panel), `bots/BotRow.tsx`; `bots/BotsPage.tsx` registration changes
  (sidebar panel contribution instead of nav entry); edge tab `bots/RoutinesEdgeTab.tsx`.
- `apps/desktop/src/app/pages.tsx` / `routes.ts` only if the Bots route is dropped (`CONTRIBUTED_PAGES`).

Tests affected (update only where they pin markup/text deliberately changed):
- `app/capabilities/CapabilitiesPage.test.tsx`: tab role names stay (`/Skills/`, `/Plugins/`) — keep `role="tab"`;
  count text "2 of 3 on" → new count format; the scope `combobox` only renders with >1 profile (fixture has 2: keep a
  native select or a radix select with combobox role); switch accessible name "brew-tea is on" (keep the
  `{name} is on` aria-label on the CapRow switch); `plugin-<name>` testids (keep on table rows); Repository/Commit
  textboxes + Install button + 40-character alert now live inside the "Install from Git" dialog (test clicks the
  button first); install confirmation dialog + `status` "zen was installed" (keep a role=status live region or
  assert the toast).
- `app/messaging/MessagingPage.test.tsx`: "a card shows masked credentials…" — fields now render for the selected
  platform only (first platform selected by default; select others by clicking the row); field accessible names
  change from raw key to the copy-map label (keep `aria-label` = key or update queries); Save button text.
- `bots/*.test.ts(x)` (canonicalChat, GroupRoom) unaffected; add tests for the roster panel (row opens chat, preview
  join, hide via menu).
- E2E: none pin these pages (`e2e/fixtures.ts` only mentions `client.capabilities`). Re-capture with the kit.

## 10. Build order
1. `masterDetail.tsx` shared primitives + unit tests (sash clamps, selection fallback, bottom section collapse).
2. Capabilities shell header (search | tabs | scope) + count logic.
3. Skills tab (list, detail, learning-graph join, archive) → Hub section (our search inside the frame, update).
4. Tools tab (list, chips, terminal backend panel via config; toolset switch per the gap rule).
5. Plugins tab (header toolbar, table, install dialog, desktop half incl. Bots row, catalog section decision).
6. Connectors tab (directory header, search, card grid, add dialog, server dialog).
7. Messaging page (rail list, detail sections, field copy map, action bar, pairing, Telegram card, filters).
8. Bots sidebar tab (strip if needed, roster panel, row menu, edge tab), contribution switch wiring.
9. Strings, tests, biome/tsc/vitest, re-capture both themes, side-by-side PDF, iterate.

## 11. Compare checklist (tick per slug, light AND dark)

Capabilities shell
- [ ] No page title; one header row at y≈56; search left (Skills/Tools only); tabs centred; scope at right only for >1 profile
- [ ] Tab labels Skills / Tools / Connectors / Plugins; superscript counts on Skills and Tools; active underline
- [ ] Page bg `#fefefe` / `#0d1015`; no borders between header and body; no Files/Review side pane open by default

Skills (`skills`, `capabilities`)
- [ ] List column ≈490 px wide, detail centred ≤672 px; sash works and persists
- [ ] "↓ Most used" strip + kebab with All switch / Disable unused
- [ ] 44 px rows: name + category subtitle, ×N chip when used, 28x16 switch; selected row `#dfe6f4` / `#1b2331`
- [ ] Detail: 15 px title + category pill, description, meta box (inset bg + border), footer caption bottom-right
- [ ] Hub section from y≈439: primary-tint divider, "Skills Hub" + "Update installed" + "Hide the hub browser", framed box, hint line; our search works inside it
- [ ] Toggle optimistic, failure flips back with toast

Tools (`tools`)
- [ ] Rows with "N tools" chips, off rows dimmed with off switch style
- [ ] Detail: title, description, mono tool chips; Terminal shows "Execution backend" cards with selected card + "In use"
- [ ] Toolset switch behaviour per decision (config write or disabled with tooltip)

Connectors (`mcp`)
- [ ] "Connectors" title + "Add your own" outline button; borderless search with hairline under it
- [ ] Group header with count; 2-column 66 px cards with 36 px tile, name + kind word, description, right lane state + button/switch
- [ ] Add dialog and server dialog (test, sign in, remove) work as today's inline controls did

Plugins (`capabilities-plugins`)
- [ ] Blurb + "Install from Git" (filled secondary) + folder + refresh icons
- [ ] Header band 28 px inset bg with Desktop / Agent in <profile> columns (144 px each)
- [ ] Rows: name, version, kind badge, bundled pill, description; switch or dash per half; Bots desktop row present and toggles the surface
- [ ] Install dialog keeps the 40-char commit rule; catalog section per decision

Messaging (`messaging`)
- [ ] 224 px platform rail, 24 px tiles, status dots, selected row tint
- [ ] Detail header with state + Needs setup pills; Telegram Quick setup card with green Recommended pill and primary button
- [ ] Uppercase tracked section titles; field rows with label, help, 32 px input + icon buttons; Add another for lists
- [ ] Bottom action bar: switch + Disabled/Enabled at left, Unsaved changes + Save changes at right (disabled look when clean)
- [ ] Pairing sections appear only with data; restart alert works

Bots (`bots-tab`)
- [ ] SESSIONS | BOTS strip with primary underline; BOTS panel on page bg
- [ ] "Refresh gateway groups", "BOTS" label with bell + plus buttons
- [ ] Bot row: face avatar, ring, name, time-ago, preview; opens the forever-chat; menu holds face/upload/hide/section
- [ ] Vertical "SCHEDULED JOBS" edge tab at the right edge opens routines

General
- [ ] No reference names/logos/assets; letter tiles or approved icon lib only; lucide icons
- [ ] Existing behaviours preserved (confirm-before-install, masked credentials, restart banner)
- [ ] Re-captured with the fixed kit slugs; `$S/compare.pdf` reviewed page by page
