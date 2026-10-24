# W52 spec: desktop-settings-a (Settings shell list + Model, Chat, Appearance, Workspace, Safety, Browser, Passwords)

Status: spec ready (2026-10-24), for the wave-52 page builder. Read + Note only; no code written.
Read: reference screenshots `settings-models`, `-models-fallback`, `-models-auxiliary`, `-models-moa`,
`settings-chat`, `settings-general`, `settings-appearance`, `-appearance-typography`,
`-appearance-window-and-layout`, `-appearance-chat-display`, `-appearance-pet`,
`settings-workspace-{projects-and-discovery,shell-environment,files-and-execution}`,
`settings-safety-{approvals,privacy-and-network,checkpoints}`,
`settings-browser-{browser-profile,local-and-private-urls}`,
`settings-passwords-{saved-credentials,password-managers}` (each `--light` / `--dark`, 1440x900);
reference source read for structure/behaviour only (settings shell, overlay split layout, row primitives,
subpage tables, model/appearance/vault settings). Our side: `apps/desktop/src/settings/*`,
`theme/AppearancePage.tsx`, `electron/prefs.ts`, `electron/ipc/schemas.ts`, backend config defaults,
`web/routes/models.py`, `protocol/pets.py`, `tools/builtin/browser/vault.py`; foundation in progress at
`$SP/wt-w51b` (`apps/desktop/src/theme/deskTokens.ts`, `docs/notes/ui-parity/desktop-foundation.md`).
Pixel values below were sampled from the PNGs with a stdlib PNG decoder (`$SP/w52-specs/tools-dsa/`).
"L / D" = light / dark. Text colours are the darkest (L) / brightest (D) anti-aliased glyph pixel, so the
true colour is that or slightly stronger.

Slice b (memory, voice, advanced, notifications, billing, providers, gateways, keyboard, tools & keys,
sessions, plugins, about) owns those pages; this note fixes the category list they all share.

---------------------------------------------------------------------------------------------------

## 0. Slug map (reference → our capture → our route)

Our route stays `#/settings?view=<view>&page=<page>` (`pages.ts` `resolveTarget`/`targetQuery`); the
capture kit names a page `settings-<view>-<page>` unless its `CANON` table maps it.

| Reference slug | Ref title | Our capture now | Our view/page after this slice | State today |
|---|---|---|---|---|
| settings-models | Model › Main model | settings-models | model/main | exists, schema rows; restyle + rebuild |
| settings-models-fallback | Model › Fallback models | settings-model-fallbacks | model/fallbacks | exists (textarea); rebuild |
| settings-models-auxiliary | Model › Auxiliary models | settings-model-auxiliary | model/auxiliary | exists (48 raw schema rows); rebuild |
| settings-models-moa | Model › Mixture of Agents | — | model/moa (NEW page) | missing |
| settings-chat | Chat › Behavior | settings-general (kit CANON maps chat/behaviour here) | chat/behaviour | exists; restyle |
| settings-general | Appearance › General | — | appearance/general (NEW page, first child) | missing |
| settings-appearance | Appearance › Theme | settings-appearance | appearance/theme | exists (selects); rebuild as card grid |
| settings-appearance-typography | Appearance › Typography | settings-appearance-typography | appearance/typography | exists (+/- and select); rebuild |
| settings-appearance-window-and-layout | Appearance › Window & layout | settings-appearance-window | appearance/window | exists (tray only); extend |
| settings-appearance-chat-display | Appearance › Chat display | — | appearance/chat-display (NEW) | missing |
| settings-appearance-pet | Appearance › Pet | — | appearance/pet (NEW) | missing |
| settings-workspace-projects-and-discovery | Workspace › Projects & discovery | settings-workspace-projects (+ settings-workspace-desktop) | workspace/projects (absorbs `desktop`; keep `page=desktop` as alias) | split over 2 pages today |
| settings-workspace-shell-environment | Workspace › Shell environment | settings-workspace-shell | workspace/shell | exists; trim |
| settings-workspace-files-and-execution | Workspace › Files & execution | settings-workspace-files | workspace/files | exists; adjust fields |
| settings-safety-approvals | Safety › Approvals | settings-safety-approvals | safety/approvals | exists; reorder + restyle |
| settings-safety-privacy-and-network | Safety › Privacy & network | settings-safety-privacy | safety/privacy | exists; restyle |
| settings-safety-checkpoints | Safety › Checkpoints | settings-safety-checkpoints | safety/checkpoints | exists; restyle |
| settings-browser-browser-profile | Browser › Browser profile | — | browser/profile (NEW view) | missing |
| settings-browser-local-and-private-urls | Browser › Local & private URLs | — | browser/network (NEW) | missing |
| settings-passwords-saved-credentials | Passwords & Logins › Saved credentials | — | passwords/credentials (NEW view) | missing |
| settings-passwords-password-managers | Passwords & Logins › Password managers | — | passwords/sources (NEW) | missing |

Capture kit (builder copies `$SP/compare/work-i-desktop/kit` into its own `$S`, never edits the
original): update `CANON` so names match the reference slugs: `chat/behaviour → settings-chat`,
`appearance/general → settings-general`, `model/fallbacks → settings-models-fallback`,
`model/auxiliary → settings-models-auxiliary`, `model/moa → settings-models-moa`,
`appearance/window → settings-appearance-window-and-layout`, `appearance/chat-display →
settings-appearance-chat-display`, `appearance/pet → settings-appearance-pet`, `workspace/projects →
settings-workspace-projects-and-discovery`, `workspace/shell → settings-workspace-shell-environment`,
`workspace/files → settings-workspace-files-and-execution`, `safety/privacy →
settings-safety-privacy-and-network`, `browser/profile → settings-browser-browser-profile`,
`browser/network → settings-browser-local-and-private-urls`, `passwords/credentials →
settings-passwords-saved-credentials`, `passwords/sources → settings-passwords-password-managers`.
The kit waits for `input[aria-label="Search settings"]` and reads the page title from `h2`; keep a
searchbox with that label reachable (see §1.4) or update the kit's wait in the copy.

---------------------------------------------------------------------------------------------------

## 1. Settings shell as seen on these pages (foundation builds it; this is what each page must show)

### 1.1 Frame (measured, 1440x900)
- Backdrop over the whole window: black at ~22% + 2px blur. Sampled composite over the app:
  L `#6f7071`, D `#000104`. Click on the backdrop closes; Escape closes.
- Card: x 48→1391, y 48→851 (1344 x 804), equal 48px inset on all sides (titlebar 34 + 14).
  1px border L `#c8d5f1` / D `#232f47` (= foundation `stroke-3`), radius visually ~2-4px (corners read
  square at 1x), medium shadow. Card body (main pane) L `#fefefe` / D `#0d1015` (`bg-canvas`).
- Two columns: left rail 208px (x 49→256) bg L `#f6f8fa` / D `#010409` (`bg-band`), no divider line
  between rail and main (the colour change is the edge). Main pane x 257→1390.
- Below 760px window width the rail becomes a single dropdown bar (not visible in the captures; keep
  our existing narrow behaviour or do this, builder's choice).

### 1.2 Search pill (rides the top edge of the card)
- Centred on x=720, vertically centred on the card's top border (y 36→59, 24px high), width ~136px
  (x 652→787). Fully rounded. 1px border L `#c8d5f1` / D `#232f47`; fill = card colour
  (L `#fefefe` / D `#0d1015`) so it masks the border behind it; small shadow.
- Content: 12px magnifier icon, label "Search" 12px, text L `#888a8d` / D `#81868b`; then two ghost
  key caps "Ctrl" and "K" (h ~16px, 1px border L `#dededf` / D `#35393e`, fill L `#f7f8f8` / D
  `#191c21`, 10px text). Hover: text goes to foreground.
- Behaviour (ours): click, or typing any printable key while focus is not in a field, opens settings
  search. Reference opens the command palette scoped to Settings; our overlay has its own search
  (`search.ts`, hits list) — keep ours: the pill turns into a 240px searchbox in the same spot
  (`role=searchbox`, `aria-label="Search settings"`, placeholder "Search settings") and the rail list is
  replaced by the hits list (`.settings-hit` buttons, current behaviour). Escape clears, second Escape
  closes the overlay. Decision W52-dsa-1 (keeps `SettingsOverlay.test.tsx:108`, the e2e "proxy" search
  test and the capture kit working).

### 1.3 Close button
- Top-right of the card: X glyph ~12px at (1366, 68), colour L `#949699` / D `#767c82`, 24px hit area,
  hover = row-hover fill + foreground colour. `aria-label` "Close" (keep `Dialog.Close`).

### 1.4 Left category list (the rail)
Padding: 10px left/right (items x 59→246, 188px wide), 10px top (first item top y≈58). Items 28px high,
2px apart (30px pitch). Group breaks are an 8px spacer (pitch 40px across a break).

Order and labels exactly (our view ids in brackets; 1–4-word labels may match):

| # | Label | Icon (lucide / tabler name) | Children shown when open | Our view |
|---|---|---|---|---|
| 1 | Model | Box (cube) | Main model (Box), Fallback models (Box), Auxiliary models (Cpu), Mixture of Agents (Users) | model |
| 2 | Chat | MessageCircle | Behavior (MessageCircle), Attachments (FileImage) | chat |
| 3 | Appearance | Palette | General (Settings2/cog), Theme (Palette), Typography (circle with "A": tabler CircleLetterA), Window & layout (Monitor), Chat display (MessageCircle), Pet (PawPrint) | appearance |
| 4 | Workspace | Monitor | Projects & discovery (FolderOpen), Shell environment (SquareTerminal), Files & execution (FileText) | workspace |
| 5 | Safety | Lock | Approvals (shield with lock: tabler ShieldLock), Privacy & network (Lock), Checkpoints (Archive) | safety |
| 6 | Browser | Globe | Browser profile (Globe), Local & private URLs (Network) | browser (NEW) |
| 7 | Passwords & Logins | ShieldLock | Saved credentials (KeyRound), Password managers (Lock) | passwords (NEW) |
| 8 | Memory & Context | Brain | (slice b) | memory |
| 9 | Voice | Mic | (slice b) | voice (slice b decides) |
| 10 | Advanced | Wrench | (slice b) | advanced |
| 11 | Notifications | Bell | (slice b) | notifications |
| 12 | Billing | BarChart3 | — | none: we have no billing; omit (W52-dsa-2) |
| — | 8px spacer | | | |
| 13 | Providers | Zap | (slice b) | providers |
| 14 | Gateways | Globe | (slice b) | gateways |
| 15 | Keyboard Shortcuts | Keyboard | (slice b) | keyboard |
| 16 | Tools & Keys | KeyRound | (slice b) | (slice b) |
| 17 | Sessions | Archive | (slice b) | sessions |
| — | 8px spacer | | | |
| 18 | Plugins | puzzle / four-squares (LayoutGrid) | — | (slice b) |
| — | 8px spacer | | | |
| 19 | About | Info | (slice b) | about |

Every item that has children shows a 12px chevron at the right (x 228→240): ChevronRight when closed,
ChevronDown when open; the chevron is its own button (toggles the disclosure without navigating;
`aria-expanded`, `aria-label` "Expand: <label>" / "Collapse: <label>"). Plugins has no chevron in the
captures (no plugin pages). Clicking an item opens its first child page and expands it; leaving a
group folds it again unless the person opened it by the chevron.

Item anatomy, top level: 8px left pad, 16px icon (x 68→84), 8px gap, label 13px regular at x 92,
truncated with an ellipsis ("Passwords & Logi…", "Memory & Cont…" when a scrollbar takes 8px).
- idle: no fill, transparent 1px border; label L `#575a5f` / D `#aab1b6` (≈ `fg-2`); icon
  L `#999c9f` / D `#63686e`; chevron L `#787b7f` / D `#8e949a`.
- hover: fill `row-hover` (L `#e8eef7` / D `#0b111b`), label → foreground.
- active (the open group): fill L `#d8e1f0` / D `#101826` (`row-active`) + 1px border L `#b9cae8` /
  D `#1f2c44` (new token, see §2), radius 6px, label/icon L `#2a2f35` / D `#d9e0e7`.

Child anatomy: indented 20px (x 79→246, 168px), 28px high, 14px icon at x 89→103, label 13px at x 110.
- idle: label L `#828589` / D `#7c8188` (`fg-3`), icon a touch lighter.
- active (current page): fill L `#dee5f2` / D `#0d1421` (`row-active-soft`), no border, label
  `font-weight 500`, L `#2a2f35` / D `#d8e0e7`.

Rail footer: three 28px icon buttons at the bottom-left (centres x 71, 99, 127; y 827 when the list
fits, 835 when it scrolls): Download (export config), Upload (import config), RefreshCw (reset to
defaults, hover turns danger). 14px glyphs, colour L `#62666a` / D `#9da3aa`, tooltips on hover. Ours:
export = `GET /api/config` saved as JSON (renderer download); import = file picker → `PUT
/api/config` with the parsed tree (form-tree only, additive, same as `SchemaPage.save`); reset = not
in our API → hide this button or keep it disabled with the "Not available in this build yet." tooltip
(W52-dsa-3). If export/import feel too new for this slice, render all three disabled with that
tooltip; do not invent a reset endpoint.

When a long group is open (Appearance has 6 children) the list overflows: the rail scrolls, an 8px
overlay scrollbar shows, chevrons shift to x≈225, and the group spacers collapse to ~2px (flex
shrink). Model with 4 children still fits.

### 1.5 Main pane frame
- Horizontal content inset 57.6px each side (4vw at 1440, clamp 20→64px): content x 314→1333
  (1019px). Max content width 1200px (not reached at 1440).
- Breadcrumb: top padding 22px → line centred at y≈78. 12px text, gap 6px, 12px ChevronRight
  separators. "Settings" (plain) › "<Group>" (text button, opens the group's first page) ›
  "<Page>" (`aria-current=page`). Colours: first two L `#86888b` / D `#81878d` (`fg-3`); current page
  L `#2c3035` / D `#d8e0e6` (`fg-1`). Keep our `<ol aria-label="Where you are in Settings">` (test 98)
  and keep `Dialog.Title` on it. 12px gap under the breadcrumb.
- The page has no h2 heading: the breadcrumb is the title. (The capture kit reads `h2`; with none it
  falls back to the page id — fine.)
- Page body scrolls inside the pane (80px bottom padding). No "Applies to" chips in these captures
  (single profile); keep our chips when two or more profiles exist, placed between breadcrumb and
  first row, restyled as the segmented control of §2.6.

---------------------------------------------------------------------------------------------------

## 2. Shared row components and tokens (build once in `settings/` on the foundation tokens)

Foundation token names (`--desk-*`, `apps/desktop/src/theme/deskTokens.ts` in wt-w51b) cover most
values; the right-hand column says which. NEW = ask the foundation table for one more token (or define
it locally in `settings.css` with the same name scheme).

| Role | Light | Dark | Token |
|---|---|---|---|
| card / field fill | `#fefefe` | `#0d1015` | bg-canvas |
| rail | `#f6f8fa` | `#010409` | bg-band |
| primary text (labels, values) | `#2c3035` | `#d8e0e6` | fg-1 |
| nav idle text | `#575a5f` | `#aab1b6` | fg-2 |
| captions, breadcrumb, quiet buttons | `#86888b` | `#81878d` | fg-3 |
| disabled text ("Use theme font", "Add preset") | `#c1c2c4` | `#474b51` | fg-4 |
| card border, pill border | `#c8d5f1` | `#232f47` | stroke-3 |
| field border (very faint) | `#ecf2fe` | `#0f141e` | NEW `stroke-field` |
| field inner top shadow (1px) | `#e7e7e7` | `#080a0d` | NEW (or inset shadow) |
| strong neutral border (chip, preview frame, select chevron) | `#d0d7de` | `#30363d` | NEW `stroke-strong` |
| active nav border | `#b9cae8` | `#1f2c44` | NEW `stroke-active` |
| segmented track, slider track, pet tile | `#dfe6f4` | `#1b2331` | ≈ stroke-2 (`#dae3f4`/`#1d2535`) |
| segmented selected border | `#d1d7e4` | `#252c3a` | NEW or stroke-2 |
| pill fill / pill text | `#f6f6f6` / `#828487` | `#1a1e24` / `#878d94` | NEW `bg-pill` + fg-3 |
| accent (switch on, primary button, slider thumb, selected card border) | `#0053fd` | `#4a84fe` | accent |
| primary button text | `#ffffff` | `#151617` (dark text on the light blue!) | accent-fg — foundation has `#ffffff` for dark: flag to w51b |
| selected theme card fill / ring | `#eff4fe` / `#cbdcfe` | `#101722` / `#192643` | NEW `accent-tint`, `accent-ring` |
| unselected theme card fill / border | `#f0f2f8` / `#ced9ef` | `#151a22` / `#232d40` | NEW `bg-card`, `stroke-card` |
| switch off: track border / knob | `#dddfe0` / `#2c3035` | `#2c3035` / `#d8e0e6` | stroke-1-ish / fg-1 |
| outline button border ("Add") | `#e3eaf8` | `#18202e` | ≈ stroke-2 |

Typography: UI font is the foundation's (Inter). Row title 13px weight 500 `fg-1`; caption 12px /
16px line height `fg-3`; breadcrumb 12px; nav 13px; pills 11px; mono status lines 11px monospace
`fg-3`; section labels ("Defaults") 12px regular `fg-3`, no uppercase. Letter-spacing normal everywhere.

### 2.1 `SettingsRow` (replaces `.settings-row` in `customPages.Row` and `schemaPage.FieldRow`)
- Two-column grid when the pane is ≥ 672px: label column `1fr`, control column `minmax(240px,352px)`,
  12px column gap, control right-aligned (`justify-self:end`). Narrow: control drops under the text.
- 12px top and bottom padding, no separators between rows. Title 13px/500; caption 4px under it
  (12/16px, wraps at the label column ≈ 655px wide → descriptions wrap near x 970).
- Measured pitch: title-only row 53px (switch) / 58px (field); one-line caption 68px; two-line 84px.
- `wide` variant: control sits on the title line (right end) and the full width goes to `below`
  (galleries, lists). Used by Theme, Pet, Fallbacks, MoA.
- Optional mono hint line under the caption (11px mono, 45% opacity). Drop our `.settings-row-key`
  (`model.default` mono key under every row) from the visual — the reference shows none. Keep it as
  `title` attribute on the row or behind search (W52-dsa-4). Keep `data-field`, `data-row`,
  `data-highlighted` attributes and the scroll-into-view + outline on a search hit.
- Status notes (Saving…, Saved, errors) stay, as 12px text under the control, `fg-3` / danger.

### 2.2 Switch
36 x 20 track, fully rounded, 16px knob, 2px inset. On: track accent, knob L `#fefefe` / D `#0d1015`.
Off: track = row background with a 1px border (L `#dddfe0` / D `#2c3035`), knob = `fg-1`
(L `#2c3035`, D `#d8e0e6`) on the left. Disabled: 50% opacity (see the second Privacy row).
Keep `role="switch"` + `aria-checked` + `aria-label` = row title (tests query switches by name).
Small variant 28 x 16 (MoA "Enabled" chip).

### 2.3 Select (closed trigger)
Height 30px, radius 2px, 1px `stroke-field` border + 1px inner top shadow, fill `bg-canvas`, 13px
value `fg-1`, 11px left padding, ChevronDown 12px in `stroke-strong`-ish grey (L `#a9aaac` /
D `#666b71`) 12px from the right. Width: fits content (min ~76px: "None", "Manual", "Project",
"Medium") or fixed 288px for Provider/Model, full field width in MoA. Open menu = the foundation
`Menu`/popover (radix select or our menu), items 28px, check on the current item. Placeholder text
`fg-3` ("Search…" for searchable selects; type-to-filter inside the popover). Native `<select>` is not
acceptable visually; if a native one stays for a11y tests, hide it behind the styled trigger.

### 2.4 Text / number input
Same frame as the select (30px, `stroke-field`, inner top shadow), 13px text, 11px padding, width
214px in the control column (x 1119→1332). Placeholder `fg-3`, e.g. "comma-separated values" for
list fields (our words OK: "comma-separated"). List fields become ONE line, comma-separated (today a
textarea, one per line): parse on blur by splitting on commas and trimming; structured lists (objects)
keep the JSON textarea. Commit on blur / Enter (current `FieldRow.commitText` logic).

### 2.5 Buttons
- Primary: h 24, radius 2-4px, accent fill, 12px/500 text (`accent-fg`, D dark text), padding 10px
  ("Apply" is 53px wide). Busy: spinner 14px + label.
- Outline small: h 24, 1px border (`stroke-2`-ish), 12px `fg-1`, optional 12px leading icon ("+ Add").
- Text button: 12px `fg-3`, no frame, hover `fg-1` ("Set to main", "Set default", "Remove", "Delete"
  disabled at `fg-4`).
- Strong text button: 12px `fg-3` with a 1px underline 2px below (L `#e5e6e6` / D `#24282d`), hover
  `fg-1` ("Change", "Reset all to main", "Add reference model", "+ Add fallback", "Add preset").

### 2.6 Segmented control
Track: fill L `#dfe6f4` / D `#1b2331`, radius 4px, 24px high (y 119→142), 1px inner padding.
Options: 12px text `fg-3` (L `#787d87` / D `#888f9a`), equal min width ~52px, padding 0 10px.
Selected option: fill `bg-canvas`, 1px border L `#d1d7e4` / D `#252c3a`, radius 3px, text `fg-1`.
Optional 12px leading icon (Theme mode). `role="radiogroup"` + `role="radio"` children (keeps our
current `.segmented` a11y). Arrow keys move the choice.

### 2.7 Pill (badge)
h 18-20, radius 4px, padding 0 6px, fill `bg-pill`, 11px text `fg-3`. Tone `primary`: accent-tint
fill + accent text (used for "Enabled" sources).

### 2.8 Empty state
Centred block in the content column: title 14px/500 `fg-1`, 6px below a 12px caption `fg-3`, no icon,
~90px below the page intro text.

### 2.9 Slider
Track 4px high, 160px long, fill `stroke-2`, filled part accent; thumb 16px accent circle; value at
right in 12px `fg-3` ("0%"), 30px gap.

### 2.10 Loading / error (all pages)
Loading: skeleton rows keeping the row rhythm (title bar 14x160, caption bar 12x256, control block
32x288 at right), 4 rows; replaces today's "Loading…" text. Error (schema/config/API failed): one
caption-styled danger line at the top of the page plus a text button "Retry" (calls the loader
again). A managed (read-only) config keeps our `.settings-banner` note, restyled as a 12px caption on a
`bg-pill` strip, and disables controls.

---------------------------------------------------------------------------------------------------

## 3. Pages

Common to every page below: frame of §1, breadcrumb "Settings › <Group> › <Page>", rows of §2,
content x 314→1333, first row title baseline ≈ y 120.

### 3.1 settings-models — Model › Main model
Layout (top → bottom):
1. Intro caption (one line, `fg-3`, y≈106): new sessions use this; the composer's model chip switches
   the open chat (our words).
2. Row "Provider": select 288px (x 1045→1332, y 139→168), value e.g. "custom".
3. Row "Model": select 288px (y 197→226), value e.g. "mock-model". Searchable list of models.
4. "Apply" primary button right-aligned under the Model select (x 1280→1332, y 243→266).
   Disabled until provider+model differ from the saved pair or while applying ("Applying…" + spinner).
5. Section label "Defaults" (12px `fg-3`, y≈298), 18px top space.
6. Row "Reasoning": select ~112px (x 1221→1332), value "Medium".
7. 30px extra space, then row "Main model context window (override)" with a two-line caption and a
   214px number input (value "64000"). Caption (our words): overrides the detected window for the
   main model only, 0/empty uses the detected value, side models unaffected.
Dark: same geometry; select borders nearly invisible (`#0f141e`), Apply `#4a84fe` with dark text.
Data (ours):
- Provider options + current: `GET /api/model/options` → `providers[] {name,label,configured}`,
  `current {provider, model}`. Show `label`; unconfigured providers listed with a muted "needs key"
  pill (no inline key entry in this slice; the providers page owns keys).
- Model options: the same `model.options` gateway call our composer `ModelPicker` uses (per provider);
  when unavailable, the Model control becomes a 288px text input (combobox: free text allowed).
- Apply: `POST /api/model/set {scope:"main", provider, model, base_url?}`; on success reload config and
  show "Saved" note 2s.
- Custom provider: when provider is `custom`, show two more rows under Model, before Apply: "Base URL"
  (`model.base_url`, input) and "API mode" (`model.api_mode`, select from schema enum).
- Reasoning: we have no config default for main-model effort. WIRE to the same per-model effort preset
  the composer's effort chip writes (foundation decision W51-b-6); options Auto/Off/Low/Medium/High
  per that store; label "Auto" when unset. If the store is not reachable from Settings, PLACEHOLDER
  (disabled select showing "Auto" + "Not available in this build yet." caption).
- Also under Defaults: row "Speed" for `agent.fast_mode` (select normal/fast/auto, our labels) — the
  reference shows a speed row here when the provider supports it; we always have the field.
- Context window: `model.context_length` (null ↔ empty/0), via `PUT /api/config` patch
  (`patchFor`). Show placeholder "0" when null.
States: loading skeleton (provider/model rows); options call failed → selects become inputs + danger
caption; apply failed → danger note under Apply.

### 3.2 settings-models-fallback — Model › Fallback models
- Wide row, no control column: title "Fallback models" 13px/500 (y≈120), caption (backup
  provider:model pairs tried in order when the main model fails — our words) at y≈142.
- Empty: one caption line "No fallback models yet …" (our words) at y≈170, then text button
  "+ Add fallback" (12px Plus icon, strong-text style, y≈196).
- With entries (ours to build, reference shape): one line per entry, 6px apart: index number (16px
  wide, 11px mono `fg-3`), provider select (min 144px), model input/combobox (min 208px, grows),
  14px X icon button (remove, tooltip "Remove"). Changes save on blur/select (whole list written).
- Under the list, our extra field `fallback.min_switch_reset_s` as a normal row "Return to main after"
  (seconds) — deliberate extra (W52-dsa-5); hide if empty is preferred.
Data: `fallback_providers` (list of `{provider, model}`) from `GET /api/config`, write with
`PUT /api/config {config:{fallback_providers:[…]}}`; provider options from `/api/model/options`.

### 3.3 settings-models-auxiliary — Model › Auxiliary models
- Top right: strong text button "Reset all to main" (y≈110, right edge 1326).
- Intro caption (y≈140): side tasks use the main model unless one is set (our words).
- One wide row per task, 68-69px pitch, starting y≈178:
  - line 1: task name 13px `fg-1` + 8px + pill (short purpose, 11px).
  - line 2 (6px under): mono 11px `fg-3` status: `auto · use main model`, or `<provider> · <model>`,
    plus ` · Reasoning <level>` when set.
  - right, vertically centred: text button "Set to main" then strong text button "Change"
    (right edge 1326, 16px apart). "Set to main" disabled when there is no main model.
  - "Change" expands the row in place (replacing the buttons): provider select (min 128px) + model
    combobox (min 192px) on one line; second line "Reasoning" 12px label + select (Inherit/Off/Low/
    Medium/High); "Apply" primary + "Cancel" text button. Escape cancels.
- Our tasks and pills (labels ours, 1–4 words): Vision "Image analysis"; Compression "Context
  compaction"; Title "Session titles"; Approval "Smart approvals"; Review "Code review"; Curator
  "Skill curation"; Goal judge "Goal checks"; Side question "Quick asides". Order: vision,
  compression, title, approval, review, curator, goal_judge, side_question (or backend order).
Data: `GET /api/model/auxiliary` → `tasks[] {task, provider, model, base_url, description}`, `main`.
Set to main → `POST /api/model/set {scope:"auxiliary", task, provider: main.provider, model:
main.model}`; Reset all → same call with `provider:"auto", model:""` per task (sequential; one
error note if any fails); Change/Apply → `POST /api/model/set {scope:"auxiliary", task, provider,
model}` then reasoning via `PUT /api/config` `auxiliary.<task>.reasoning_effort`. The other
`auxiliary.<task>.*` fields (base_url, timeout_s, fallback) are no longer shown as raw rows here:
base_url appears in the expanded editor only for provider `custom`; timeout/fallback remain
reachable through search (owned by this page, rendered in a collapsed "More" disclosure at the bottom
of the expanded editor) — W52-dsa-6.

### 3.4 settings-models-moa — Model › Mixture of Agents (NEW page `model/moa`)
- Intro caption, two lines (y 106/122), our words: presets appear as models of the Mixture provider;
  the aggregator runs every step and is billed; references advise once per turn.
- Preset select full content width (x 315→1324, 30px, y 139→168), value = preset name.
- Toolbar row (y≈191, 28px): ["Enabled" + small switch] chip (1px `stroke-strong` border, radius 2px,
  x 315→413), text button "Set default", text button "Delete" (disabled when only one preset), a
  160px input "new preset" placeholder, strong text button "Add preset" (disabled until a new unique
  name is typed).
- Line "Default: `<name>`" 12px `fg-3`, name in mono (y≈222).
- For each reference model (block pitch ~180px):
  - title "Reference N" 13px/500 + pill "advises once per turn" (y≈260); mono 11px
    `provider · model` (y≈284).
  - provider select and model select stacked, each 646px wide (x 315→961), 30px, 8px apart
    (y 304→333, 342→371).
  - switch at the far right (x 1289→1325) vertically centred on the two selects = this reference
    on/off.
  - text button "Remove" under the selects (disabled when it is the last reference).
- Strong text button "Add reference model" centred-right under the references (x≈760→880).
- Aggregator block: "Aggregator" + pill "acting model · billed" ; mono line; provider + model selects.
Data: `moa.presets` (`name → {reference_models:[{provider, model, enabled, reasoning_effort}],
aggregator:{provider, model}, reference_timeout, fanout}`) and `moa.default_preset` from
`GET /api/config`; every edit writes the whole `moa.presets` object (and `moa.default_preset` for
Set default) with `PUT /api/config`. Provider options `/api/model/options` minus the Mixture provider
itself. GAP: our presets have no preset-level `enabled`; the "Enabled" chip is PLACEHOLDER (render the
chip with the switch disabled + tooltip "Not available in this build yet.") or omitted — W52-dsa-7.
Empty (no presets): caption "No presets yet" + the new-preset input and "Add preset" only.
Our extra fields `moa.reference_timeout`, `moa.privacy_filter` as normal rows at the bottom.

### 3.5 settings-chat — Chat › Behavior (our page id stays `behaviour`)
Rows (each title + one-line caption, 68px pitch, from y 120):
1. "Personality" — select at right, content width (value "None"). Ours: our persona is free text per
   profile (`GET/PUT /api/profiles/{name}/persona`), not a pick list. WIRE as: select with "None" /
   "Custom" and a text button "Edit" that opens our existing persona editor (today's
   `PersonaPage`) — or keep Personality as its own child page and leave this row out (W52-dsa-8;
   default: keep the child page, row omitted).
2. "Timezone" — searchable select 176px wide (x 1157→1332), placeholder "Search…"; options = IANA zones
   (`Intl.supportedValuesOf("timeZone")`), blank = system. Field `timezone`.
3. "Reasoning blocks" — switch, field `display.show_reasoning`.
Plus ours: `display.busy_input_mode` ("While a reply runs" select interrupt/queue/steer) after row 3.
Chat children in the rail: Behavior, then (reference) Attachments — we have no attachment-mode
setting; our second/third children are Personality and Voice (Voice may move to slice b's Voice view).

### 3.6 settings-general — Appearance › General (NEW page `appearance/general`, first child)
Rows:
1. "Language" — select 128px with a 14px globe/translate icon before the value ("English"),
   control greyed in the capture (disabled because only one language ships). Ours: only English
   ships (D-16.51) → render disabled select "English" (WIRE to `display.language` read-only).
2. "Intro splash" — switch (on). PLACEHOLDER (we always show the new-chat wordmark).
3. "Model pricing" — switch (off). PLACEHOLDER (no pricing data in our picker).
4. "Reopen last chat on launch" — switch. PLACEHOLDER unless the foundation already restores the last
   session (check `app/Shell.tsx`; it does not today).
5. "In-app tips" — switch + two-line caption. PLACEHOLDER.
6. "Guided tours" — switch + two-line caption. PLACEHOLDER.
PLACEHOLDER = switch rendered off and disabled, caption replaced by "Not available in this build
yet." (existing string `settings.notAvailable`). Alternative: drop placeholder rows (W52-dsa-9 —
lead picks; default render them so the page reads the same).

### 3.7 settings-appearance — Appearance › Theme
- Wide row: title "Theme" (y≈123) + caption (desktop palettes; mode applies on top — our words);
  on the title line at the right a segmented control with icons: Sun "Light", Moon "Dark",
  Monitor "System" (x 1092→1324, 24px). WIRE to `useTheme().setMode` (today's ThemePage radios —
  keep `role=radio` names "Light"/"Dark"/"System").
- Search field (y≈181): borderless, 12px search icon at x 318, placeholder 12px `fg-4` "Search themes
  or the VS Code Marketplace…" (our words), full width. Filters the card grid by name/description and,
  on Enter (or debounce 400ms), searches the marketplace (`themes.searchMarketplace`), results listed
  under the grid as rows: name, installs caption, "Install" outline button (existing
  `installMarketplaceTheme`).
- Card grid: 3 columns, 12px gaps, cards 329 x 147 (x 315/656/996; rows y 209/368/528/687).
  Card: radius 2-4px, 1px border `stroke-card`, fill `bg-card`, 8px padding.
  - Preview (80px high, full width, radius 6px, 1px border, small shadow), painted from that preset's
    own colours in the CURRENT mode: background = preset canvas; a 48px left strip = preset band/
    sidebar with a right border; at 12px padding a 10px x 64px rounded bar = preset foreground, 8px
    under it an 8px x 96px bar = preset muted text; bottom-right a 20px x 64px fully rounded pill =
    user-bubble fill + border (fall back to muted).
  - 12px under: name 13px/500 `fg-1`, then one-line description 12px `fg-3` (clamp 2 lines).
  - Selected: 1px accent border + 2px outer ring `accent-ring`, fill `accent-tint`.
  - Hover: border → `stroke-strong`. Click selects (haptic none). User themes get a 24px trash button
    top-right on hover ("Remove theme").
- Ours: 12 presets from `@indus/ui` `presets` (indus, monsoon, laterite, saffron, indigo, basalt,
  lotus, mangrove, dune, harbour, terrace, jade) + `value.userThemes` + `store.contributed()`. Preview
  colours from `presetTheme(id, mode)` (canvas/bg, band, fg, fg-muted, bubble). Descriptions: NEW
  i18n strings, one short line per preset in our words (e.g. "Indus" → "Clay neutrals, river blue").
  The "built in" choice (`store.choose(null)`) = the Indus card. Selection via `store.choose(id)`.
- Our extras that the reference Theme page does not show (keep, below the grid, as normal rows):
  "Accent hue" (slider §2.9, reset text button), "For this profile only" (select), "Use in terminal
  UI" (outline button = `syncTui`). Chat/terminal font rows move to Typography (§3.8).
- Empty search: caption `No themes match "<q>".`

### 3.8 settings-appearance-typography — Appearance › Typography
1. "UI scale" — segmented 90/100/110/125/150/175% (x 1021→1332, each ~52px), caption ends with the
   current value. WIRE: `bridge.zoom.get/set/onChanged`; option → `zoom.set({level:
   Math.log(p/100)/Math.log(1.2)})`; selected = nearest preset to `percent` (none selected if no
   exact match ±1). Keep Ctrl +/−/0 working (main).
2. "Chat text size" — segmented 90/100/110/125/150/175%. WIRE `prefs.chatTextScale`; our schema allows
   80–160, so 175 is out of range: either raise the main-side max to 175 (electron `schemas.ts`
   `chatTextScale` max, `prefs.ts` unchanged default 110) or render 175 disabled (W52-dsa-10).
3. "Chat font" — wide row: caption, then a combobox input (x 315→1233, 30px, placeholder
   "e.g. OpenDyslexic or a CSS font stack" in our words) with a disabled-looking text button
   "Use theme font" (enabled when a value is set; clears it). 12px under: "Preview" 12px `fg-3` +
   sample sentence 15px in the chosen font `fg-2` (`#595c60` / `#adb4b9`). WIRE: per-profile
   `store.setProfile(profile, {chatFont})` (today on Theme page). Combobox suggestions: installed
   font families if a bridge exists, else free text only.
4. "Terminal font" — same shape ("Use default" button), preview label "Glyph preview" mono 11px +
   sample prompt line in the terminal font 14px mono. WIRE `store.setProfile(profile,{terminalFont})`.
When no profile is resolved, rows 3–4 are disabled with the not-available caption.

### 3.9 settings-appearance-window-and-layout — Appearance › Window & layout
Rows (title + caption, right control):
1. "Interface mode" — segmented Simple / Advanced. WIRE: `panes/layoutStore` `setMode` (the same
   switch as the title band's Advanced/Simple toggle).
2. "Session list density" — segmented Compact / Comfortable / Detailed. PLACEHOLDER unless the
   foundation sidebar adds density (it does not today).
3. "Tab strip" — segmented Auto / Always / Never. PLACEHOLDER.
4. "App actions" — segmented Right / Left. PLACEHOLDER.
5. "Minimize to tray" — switch, two-line caption. WIRE `prefs.minimizeToTray` (existing; keep the
   switch name "Minimise to the tray" or update `SettingsOverlay.test.tsx:245` if the label changes;
   keep the tray-unavailable note).
6. "Chat backdrop" — switch. PLACEHOLDER (we have no backdrop art; never add reference art).
7. "File browser" — switch. WIRE to the Files pane open/closed state in `panes/layoutStore` if the
   foundation exposes it; else PLACEHOLDER.
8. "Floating composer" — switch. PLACEHOLDER.

### 3.10 settings-appearance-chat-display — Appearance › Chat display (NEW)
Rows: "Message bubble" (slider 0–100% + value), "Text direction" (segmented Auto / Right-to-left /
Left-to-right, two-line caption), "Hide thread timeline bars" (switch), "Message reactions"
(switch), "Vibe hearts" (switch), "Tool call display" (segmented Product / Technical), "Hide code
diffs" (switch), "Collapse thinking by default" (switch), "Inline embeds" (segmented Ask / Always /
Off, two-line caption).
Ours: "Collapse thinking by default" can WIRE to the inverse of `display.show_reasoning` only if the
lead accepts the duplication with Chat › Behavior (it is the same switch) — default PLACEHOLDER.
Everything else is PLACEHOLDER (no backing feature). This page is mostly placeholders: lead may prefer
to omit the page entirely (W52-dsa-11). If kept, all placeholder controls show the reference default
state (slider 0%, Auto, off, off, on→off since disabled, Product, off, off, Ask) disabled.

### 3.11 settings-appearance-pet — Appearance › Pet (NEW)
- Intro caption (two lines): a small animated companion floats over the app and reacts to the agent
  (our words).
- Row "Choose a pet" with caption and a switch on the right (y≈160). Switch = pet shown/hidden.
- Search field (borderless, icon, placeholder "Search pets…").
- Grid: 3 columns (x 326 / 664 / 1002, 338px pitch), rows 69px pitch, each tile: 40 x 40 thumbnail
  (L `#dfe6f4` / D `#1b2331` square, radius 2px; placeholder = 16px PawPrint in `fg-3`; real sprite
  when available), 10px gap, name 13px and slug 12px `fg-3` under it. Names are `fg-3` while the switch
  is off; hover row-hover fill; selected tile = accent-tint fill + accent border.
- Grid area is its own scroller (~290px tall, clipped mid-row like the capture), then a caption
  "Showing N of M — type to narrow it down." (our words).
Data (ours): gateway `pet.list` → `{pets:[{slug,name,shown}], active, enabled}`; select →
`pet.select {slug}`; switch off → `pet.disable`; switch on with no active pet → select the first.
Thumbnails: `pet.info` gives looks for the active pet only → show the paw placeholder for others
(as in the light capture). There is no public catalogue of thousands of pets — we list the person's
own pets (`M` = their count). Empty: empty state "No pets yet" + caption "Hatch one with /pet in a
chat." (our words). Methods are outside the generated catalogue (see `pet/PetOverlay.tsx`): call
`connection.request("pet.list", {})` the same way.

### 3.12 settings-workspace-projects-and-discovery — Workspace › Projects & discovery
Rows:
1. "Working directory" — input 214px (value "."). Field `terminal.cwd` (schema row).
2. ours: "Default folder for new chats" — input, `data-row="default-directory"` kept (e2e test
   `chat.electron.test.ts:187` fills `[data-row="default-directory"] input`). Project store
   `setDefaultDirectory` (from today's WorkspaceDesktopPage).
3. "Automatic repository discovery" — switch. Project store `setRepoScan({enabled})`.
4. "Repository discovery roots" — input "comma-separated" (caption: empty = home folder). Store
   `setRepoScan({roots})` (split on commas).
5. "Excluded repository paths" — input "comma-separated". Store `setRepoScan({excludes})`.
6. ours: "Scan depth" — number input 0…`MAX_SCAN_DEPTH`. Store `setRepoScan({maxDepth})`.
Routing: drop the `workspace/desktop` page from the nav; `resolveTarget` maps `page=desktop` →
`projects` (alias) so the e2e deep link `view=workspace&page=desktop` and old saved links land here.
Move the manifest rows `default-directory`, `repo-scan` to `page: "projects"`; `data-url-limit` to
`files`. Note: backend also has `projects.default_directory` / `projects.discovery.*`; we keep the
renderer project store as today (behaviour unchanged) — do not let schema rows for `projects.*`
appear twice (they are owned by no page today; keep it that way).

### 3.13 settings-workspace-shell-environment — Workspace › Shell environment
Rows: "Persistent shell" (switch, two-line caption, `terminal.persistent_shell`), "Environment
passthrough" (input comma-separated, `terminal.env_passthrough`). Remove `terminal.backend` and
`terminal.timeout` from this page's `fields` (they fall to Advanced › Terminal's `terminal.` prefix,
slice b) — check with slice b that their Terminal page renders them.

### 3.14 settings-workspace-files-and-execution — Workspace › Files & execution
Rows: "Code execution mode" (select content-width, value "Project", `code_execution.mode` — add to
this page's `fields`; it then wins over Advanced › Tools' `code_execution.` prefix by table order),
"File read limit" (number input, `file_read_max_chars`). Ours after them: "Largest file read into a
page (MB)" (`data-row="data-url-limit"`, `bridge.fs.getLimits/setLimits`, from WorkspaceDesktopPage),
and `tool_output.max_chars` unless slice b moves it to Advanced › Output limits (coordinate).

### 3.15 settings-safety-approvals — Safety › Approvals
Rows in this order: "Approval mode" (select, value "Manual", `approvals.mode`, caption), "Approval
timeout" (number input "300", `approvals.timeout`, caption), "Confirm MCP reloads" (switch, no
caption, `approvals.mcp_reload_confirm`), "Command allowlist" (input comma-separated, no caption,
`command_allowlist`). Then our remaining `approvals.*` fields (deny, cron_mode, single_query_mode,
unattended_mode, denial_breaker_threshold, smart_policy, destructive_slash_confirm) in schema order.
Ordering: `fieldsOnPage` sorts by schema order — add an optional `order` list to `PageDef` (first
these keys, then the rest).
Labels: reference uses Title Case from the field; we derive labels with `fieldLabel(key)` ("Mcp reload
confirm"). Add a small label override map (our words, sentence case: "Approval mode", "Approval
timeout", "Confirm MCP reloads", "Command allowlist") and update `schemaPage.test.tsx:71` (switch name)
and any other pinned label in the same change. Captions come from the schema `description` (ours).

### 3.16 settings-safety-privacy-and-network — Safety › Privacy & network
Reference rows 1–2 ("Collect usage stats", "Share usage stats …" + "What is counted" link) belong to
an analytics feature we do not have → omit (never invent; W52-dsa-12). Rows: "Redact secrets"
(switch on, caption, `security.redact_secrets`), "Allow private URLs" (switch, no caption,
`security.allow_private_urls`), then our other `security.*` fields (secret_store select,
fake_ip_ranges, website_blocklist.*, protected_instruction_files, …) in schema order. Keep
`data-field="security.fake_ip_ranges"` (test 122). Disabled switch look (50% opacity) is used when a
row depends on another (none of ours today).

### 3.17 settings-safety-checkpoints — Safety › Checkpoints
Row "File checkpoints" (switch, caption, `checkpoints.enabled`). Our other `checkpoints.*` fields
(max_snapshots, max_total_size_mb, max_file_size_mb, retention_days, min_interval_hours, auto_prune)
follow, shown only while `checkpoints.enabled` is on (W52-dsa-13: reference shows only the switch).

### 3.18 settings-browser-browser-profile — Browser › Browser profile (NEW view `browser`)
Row "Use my real browser profile" — switch (off), caption up to 5 lines (wraps at the label column):
local browsing copies the default Chromium profile into a managed snapshot each run; the live profile is
never opened; Chromium-family only; off by default (our words). Field `browser.real_profile`; when on,
show "Profile folder" (`browser.real_profile_dir`, input, placeholder "default").
Our other `browser.*` fields (inactivity/command timeouts, snapshot_*, cdp_url, executable_path,
env_passthrough, dialog_*, provider, engine, camofox_url, control_*) are not on any reference page in
this slice: put them on this page under a section label "Engine and timeouts" (§3.1 "Defaults"
style) so search still lands somewhere (W52-dsa-14), unless slice b's Tools & Keys › Settings takes
the `browser.` prefix — first claim wins, coordinate before landing.

### 3.19 settings-browser-local-and-private-urls — Browser › Local & private URLs (`browser/network`)
Rows (title only, switch right, 48px pitch): "Browser private URLs" (off) and "Local browser for
private URLs" (on). Ours: row 1 = `security.allow_private_urls` shown here via `also` (owned by
Safety › Privacy; shown on both pages — our pages model supports `also`); row 2 =
`browser.route_private_local`. Labels via the override map.

### 3.20 settings-passwords-saved-credentials — Passwords & Logins › Saved credentials (NEW view `passwords`)
- Top-right outline button "+ Add" (x 1274→1332, y 107→130).
- Intro caption, two lines (y 148/164): ask the agent to sign in and it asks for the login once;
  passwords are encrypted on this computer and filled into the page, never shown to the model (our
  words).
- Empty state at y≈266: "Nothing saved yet" + one-line caption (our words).
- With items (reference shape, for when an API exists): wide rows: label 13px/500 + source pill;
  caption = origin + identifier; pills "Login" (primary tone) / "Card" / "Address", "2FA"; right: text
  button "Remove" (confirm dialog).
Ours: the local vault exists (`tools/builtin/browser/vault.py`, `indus vault add|list|remove`) but has
NO REST or gateway method. PLACEHOLDER: always the empty state; caption tells the person to add
logins with `indus vault add` in a terminal or let the agent ask in chat; the "+ Add" button is
disabled with tooltip "Not available in this build yet." (W52-dsa-15). Do not add a backend route in
this wave.

### 3.21 settings-passwords-password-managers — Passwords & Logins › Password managers (`passwords/sources`)
- Intro caption, two lines.
- Rows (wide, no control in the not-detected state): "1Password" + pill, caption; "Bitwarden" + pill,
  caption (y 160/224, 64px pitch). Pill text in the capture "Not detected".
Ours: detection/unlock has no API → we cannot say "detected". WIRE what config has:
`browser.vault_sources` (list, default `["local","onepassword","bitwarden"]`): each manager row gets a
switch on the right = whether that source is in the list (PUT `/api/config` with the edited list), and
the pill reads "On" (primary tone) / "Off" (muted). Caption (our words): uses the 1Password /
Bitwarden command-line tool when installed and signed in; the agent asks to unlock once per session.
Add a third row "This computer" for `local` (our vault) — W52-dsa-16.

---------------------------------------------------------------------------------------------------

## 4. Our files and tests

Change (renderer only; no backend changes in this slice):
- `apps/desktop/src/settings/pages.ts` — VIEW_IDS + VIEWS: add `browser`, `passwords`; model pages
  `main, fallbacks, auxiliary, moa`; appearance pages `general, theme, typography, window,
  chat-display, pet`; workspace pages `projects, shell, files` (drop `desktop`, add alias in
  `resolveTarget`: `{workspace: {desktop: "projects"}}`); new `CustomPage` ids (`model-main`,
  `model-fallbacks`, `model-auxiliary`, `model-moa`, `appearance-general`, `appearance-chat-display`,
  `appearance-pet`, `workspace-projects`, `browser-profile`?, `passwords-credentials`,
  `passwords-sources`); `order` for approvals; `also` for browser/network; MANIFEST rows moved/added
  (new rows: moa presets, pet, vault, timezone…); a `group` flag for the spacers and an `icon` per
  view and per page (lucide names, PawPrint, ShieldLock from `@tabler/icons-react` if not in lucide —
  allowed list).
- `apps/desktop/src/settings/SettingsOverlay.tsx` — rail per §1.4 (icons, chevrons, disclosure,
  spacers, footer), search pill per §1.2, breadcrumb per §1.5, chips restyle. (Foundation may already
  do part of this; build on its version.)
- `apps/desktop/src/settings/schemaPage.tsx` — `FieldRow` → §2.1 row; switch/select/input per
  §2.2–2.4; comma-separated lists; label override map; `order`.
- `apps/desktop/src/settings/customPages.tsx` — split: move model pages to new
  `settings/modelPages.tsx`, appearance pages to `settings/appearancePages.tsx` (Theme grid,
  Typography, Window, General, Chat display, Pet), workspace/browser/passwords pages to
  `settings/workspacePages.tsx` / `settings/vaultPages.tsx` (file names ours); shared `Row`, `Toggle`,
  `Segmented`, `Pill`, `TextButton`, `EmptyState`, `Skeleton` in `settings/controls.tsx` (or the
  foundation's `src/ui/` if it ships them).
- `apps/desktop/src/theme/AppearancePage.tsx` — `AppearancePresets` becomes the card grid + extras;
  fonts move to Typography.
- `apps/desktop/src/settings/settings.css` — all new classes on `--desk-*` tokens.
- `apps/desktop/src/settings/strings.en.ts` (+ `i18n/en.ts` if keys live there) — new labels,
  captions (our words), preset descriptions, pet/vault/MoA strings.
- `apps/desktop/src/settings/search.ts` — index the new manifest rows and the new pages' labels.
- Possibly `apps/desktop/electron/ipc/schemas.ts` (chatTextScale max 175) only if W52-dsa-10 picks it.

Tests affected (update only where markup/text deliberately changes):
- `src/settings/SettingsOverlay.test.tsx`: breadcrumb list (98, keep), searchbox "Search settings"
  (108, keep via §1.2), `data-field` (122, keep), chips "Applies to" (130–164, keep), "Base URL"
  (184 = Providers › Endpoints, slice b, untouched), `getByLabelText("Mode")` read as an
  `HTMLSelectElement` with value "manual" (102 = Safety › Approvals `approvals.mode`: changes with the
  label override "Approval mode" and the styled select — read the trigger's value/text instead, or keep
  a hidden native select carrying the label), tray switch name (245), not-available text (272),
  Licences (slice b).
- `src/settings/schemaPage.test.tsx`: switch name "Mcp reload confirm" (71), labels "Smart policy",
  "Command allowlist" (72–73), "Timeout" (87), invalid text (114), reveal button (129); list input now
  comma-separated (adjust the list test).
- `src/settings/search.test.ts`: page ids for moved rows (`default-directory`, `repo-scan`,
  `data-url-limit`), new views.
- `theme/AppearancePage` tests (if any) and `themeStore.test.ts` unaffected (store API unchanged).
- e2e `e2e/core/chat.electron.test.ts` 157–215: keep `.onboarding-form input.settings-input`,
  `[data-row="default-directory"] input`, `input[aria-label="Search settings"]`, `.settings-hit`,
  `[data-field][data-highlighted="true"]`.
New tests: rail order + spacers + chevron disclosure; `page=desktop` alias; comma list parse; aux row
status text (auto vs provider·model); MoA preset edit writes the whole object; pet list/select/disable
calls; vault-sources switch edits the list; placeholder rows are disabled and labelled.

Data available vs missing (summary):
- Available: `/api/model/options`, `model.options` (gateway), `/api/model/set` (main + auxiliary),
  `/api/model/auxiliary`, `/api/config` (+ schema) for every schema field named above, persona REST,
  `pet.list/select/disable/info` (gateway), desktop prefs (tray, chatTextScale), zoom bridge, theme
  store + presets + marketplace bridge, project store (default folder, repo scan), fs limits bridge,
  layout store (interface mode).
- Missing (placeholder or omitted): main-model reasoning default in config (use effort-chip store),
  MoA preset-level enabled, usage stats, vault item list/add API, password-manager detection/unlock,
  pet catalogue, language packs beyond English, intro splash / pricing / reopen last / tips / tours /
  density / tab strip / app actions / backdrop / floating composer / every Chat display option, config
  reset-to-defaults endpoint, chat attachments mode.

---------------------------------------------------------------------------------------------------

## 5. Build order
1. Controls (`controls.tsx` + css): row, switch, select trigger, input, segmented, pill, buttons,
   slider, empty state, skeleton — on the foundation tokens; add the NEW tokens of §2.
2. `pages.ts` restructure (views, pages, icons, spacers, alias, order, also, manifest) + its tests.
3. Shell bits this slice needs if the foundation has not done them: rail (§1.4), pill search (§1.2),
   breadcrumb, footer.
4. Schema-backed pages through the new `FieldRow`: Safety ×3, Workspace shell/files, Browser ×2,
   Chat › Behavior (incl. timezone searchable select).
5. Hand-built: Workspace › Projects, Model › Main, Fallbacks, Auxiliary, MoA.
6. Appearance: Theme grid, Typography, Window & layout, General, Chat display, Pet.
7. Passwords ×2.
8. Tests, then capture with the copied kit (CANON updated), PDF side by side, close gaps.

## 6. Compare checklist (tick in Compare, both themes)
- [ ] Shell: card inset 48px, border colour, backdrop dim; rail 208px colours; pill on the top edge;
      X at top right; no h2; breadcrumb 12px with chevrons.
- [ ] Rail: 19 items in order, icons, chevrons, 8px spacers before Providers/Plugins/About, active
      group box + border, active child soft fill, footer three icons; scroll + truncation with
      Appearance open.
- [ ] settings-models: provider/model 288px selects, Apply blue at right (dark: dark text), "Defaults",
      Reasoning, context window input; no mono key lines.
- [ ] settings-models-fallback: title, caption, empty line, "+ Add fallback".
- [ ] settings-models-auxiliary: "Reset all to main", intro, rows with pill + mono status, Set to
      main / Change.
- [ ] settings-models-moa: preset select, toolbar, Default line, reference blocks with right switches,
      Add reference model, Aggregator block.
- [ ] settings-chat: Personality/Timezone/Reasoning blocks rows (+ busy mode).
- [ ] settings-general: Language (disabled) + five switches (placeholders disabled).
- [ ] settings-appearance: mode segmented with icons, borderless search, 3x4 card grid, selected ring.
- [ ] settings-appearance-typography: two 6-option segmented rows, font comboboxes + previews.
- [ ] settings-appearance-window-and-layout: 4 segmented rows + 4 switches.
- [ ] settings-appearance-chat-display: slider, segmented rows, switches (or omitted per W52-dsa-11).
- [ ] settings-appearance-pet: intro, switch row, search, 3-col tile grid, footnote.
- [ ] settings-workspace-projects-and-discovery: 4 reference rows (+ ours) ; `page=desktop` lands here.
- [ ] settings-workspace-shell-environment: 2 rows only.
- [ ] settings-workspace-files-and-execution: mode select + read limit (+ ours).
- [ ] settings-safety-approvals: 4 rows first, in order.
- [ ] settings-safety-privacy-and-network: redact + private URLs (stats rows deliberately absent).
- [ ] settings-safety-checkpoints: single switch row when off.
- [ ] settings-browser-browser-profile: long caption + switch.
- [ ] settings-browser-local-and-private-urls: two title-only switch rows.
- [ ] settings-passwords-saved-credentials: + Add (disabled), intro, centred empty state.
- [ ] settings-passwords-password-managers: intro, 1Password/Bitwarden rows with pills (+ ours).
- Deliberate differences: (fill at compare) — expected: no Billing item, no usage-stats rows,
  placeholder rows disabled, vault empty-only, our preset names/descriptions, our extra rows below
  the reference rows, en-GB spelling where our house style uses it ("Behaviour").

## 7. Decisions for the lead (W52-dsa-n)
1. Search pill expands into our in-overlay search (keeps tests/kit) instead of a palette page.
2. Omit Billing from the rail (no billing in our product).
3. Rail footer: export/import wired to `/api/config`; reset disabled (no endpoint) — or all disabled.
4. Drop the mono config key under each row (kept as `title` + searchable).
5. Show `fallback.min_switch_reset_s` under the fallback list.
6. Raw `auxiliary.<task>.timeout_s/fallback` move into the Change editor's "More".
7. MoA "Enabled" chip: placeholder (disabled) or omitted — no preset-level flag in our config.
8. Chat › Behavior has no Personality select (our persona is free text, own child page) unless the
   lead wants a "Custom/None + Edit" row.
9. Placeholder rows (General, Window & layout, Chat display): render disabled with the
   not-available caption (default) or omit them.
10. Chat text size 175%: raise the main-side max to 175, or show 175 disabled.
11. Chat display page: keep as an all-placeholder page or omit.
12. Privacy page omits the two usage-stats rows (feature absent).
13. Checkpoint detail fields only while checkpoints are on.
14. Other `browser.*` fields live on Browser › Browser profile under "Engine and timeouts" unless
    slice b claims them.
15. Saved credentials stays an empty state with a CLI hint; "+ Add" disabled.
16. Password managers: switches edit `browser.vault_sources`; pills read On/Off (not detection); a
    third "This computer" row for the local vault.
Also flag to the foundation (w51b): dark `accent-fg` measured `#151617` on `#4a84fe` buttons, not white.
