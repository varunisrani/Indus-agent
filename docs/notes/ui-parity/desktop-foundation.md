# Task: Desktop UI foundation (W51-b) Phase: ui-parity Owner: builder w51b

Status: Done for wave 51 (2026-10-24); follow-ups listed under Status at the end.

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `apps/desktop/DESIGN.md` (principles,
surfaces, stroke/colour token roles, buttons, form controls, layout, panel title bars, chat typography),
`apps/desktop/src/styles.css` (the list of custom-property names and the light/dark seed colours only,
lines 150-630), `apps/desktop/src` directory listing.
Also read (our docs, for the settings primitives' API): wave-52 specs `desktop-settings-b.md` §3.1–3.3
and `desktop-settings-a.md` §1–2 (lead request).
Evidence: reference screenshots in the wave-51 compare set (never in the repo): desktop slugs
`new-chat`, `chat-conversation`, `session-sidebar`, `settings-models`, `settings-appearance`,
`empty-state`, `error-state`, `usage-stats-prompt`, `model-picker`, `effort-picker`, each `--light` and
`--dark`. Colours below were measured from those PNGs with a pixel sampler (allowed by the W51 brief).

What it does (own words): the desktop renderer gets the reference's look while keeping every behaviour
and data flow. A quiet, flat, GitHub-like chrome: one dark (or light-grey) band holds the title bar,
the left sidebar and the status bar; the chat sits on a slightly lighter canvas. The window opens on
the chat alone (no Preview or Terminal pane), the new chat shows a large serif "INDUS AGENT" wordmark
with one line of help under it, and the composer is a single bordered bar at the bottom with an attach
"+", the prompt, the model and effort chips, the microphone and a round send button. User messages are
full-width tinted boxes; assistant replies are plain markdown with quiet code blocks and hairline
tables. Settings opens as a large modal card over a dimmed window, with a left category tree (icons,
chevrons, nested pages) and a breadcrumb over the page.

Inputs → outputs: theme mode (light / dark / system, stored per device) and the optional appearance
preset → `--desk-*` tokens on `<html data-theme>`; the existing `--indus-color-*` names are re-pointed
at the new palette so older CSS follows without edits.

Files:
- `apps/desktop/src/theme/deskTokens.ts` (new): the token tables (light, dark) and `deskTokenCss()`.
- `apps/desktop/src/theme/deskTokens.test.ts` (new): every token in both themes, contrast floors.
- `apps/desktop/src/app/theme.tsx`: paints the desk tokens after the base tokens, before a preset.
- `apps/desktop/src/ui/components.tsx`, `ui/ui.css` (new): core components (below).
- `apps/desktop/src/app/overlay/OverlayFrame.tsx`, `overlay.css` (new): the shared overlay card and
  its frames (lead request, wave 52).
- `apps/desktop/src/settings/primitives.tsx`, `settings/navMeta.tsx` (new): settings page primitives
  (lead request; API of the wave-52 settings specs §3.2 / shared controls) and the menu's icons/gaps.
- `apps/desktop/src/settings/schemaPage.tsx`, `settings/pages.ts`: rows without the mono key line,
  compact selects, a `curated` page list with the "More settings" fold.
- `apps/desktop/src/panes/terminal/xtermView.ts`: terminal colours follow the theme.
- `apps/desktop/src/chat/voice/VoiceButtons.tsx`: Mic / Talk as icon buttons (names unchanged).
- `tooling/checks/licenses_allow.json`, `scripts/release/notices_data.json`,
  `scripts/release/licence_texts/OFL-1.1.txt`, `THIRD_PARTY_NOTICES.md`: the two OFL font packages.
- `apps/desktop/src/main.tsx`: font imports (Inter, Abril Fatface from `@fontsource`).
- `apps/desktop/src/styles.css`, `sidebar/sidebar.css`, `panes/panes.css`, `settings/settings.css`:
  re-skinned on the tokens.
- `apps/desktop/src/app/Shell.tsx`: title band, sidebar order (tabs, nav, sessions, footer).
- `apps/desktop/src/panes/presets.ts`: Advanced mode starts on the chat alone.
- `apps/desktop/src/panes/PaneShell.tsx`: the layout toolbar becomes the title band's icon cluster.
- `apps/desktop/src/chat/ChatView.tsx`, `chat/composer/Composer.tsx`, `chat/ModelPicker.tsx`,
  `chat/EffortChip.tsx` (new): empty state, composer bar, chips.
- `apps/desktop/src/settings/SettingsOverlay.tsx`: category icons, chevrons, search pill, breadcrumb.

Edge cases / failure modes:
- A chosen appearance preset must still paint over the base look (order: base → desk → preset).
- Reduced motion keeps every transition at 0 ms (inherited from the base duration tokens).
- Simple mode still hides the status bar and tool panes; the title band stays.
- The window-controls overlay on Windows/Linux keeps its reserved width on the right of the band.
- Fonts load from the bundled `@fontsource` files; no network font fetch (CSP stays `self`).
- The sidebar search must show one clear button only (the native search cancel glyph is hidden).
- The model chip shows the configured model before the picker is first opened (it asks
  `model.options` once the connection is up), so it never says "Choose a model" while the status bar
  names one.
- xterm in the light theme gets a light palette (it was black on white chrome).

Our design differences / decisions:
- W51-b-1: token names are ours (`--desk-*`), grouped by role: `bg-*`, `fg-*`, `stroke-*`, `row-*`,
  `accent*`, `status-*`, `code-*`, sizes. Values are measured from the screenshots, not copied.
- W51-b-2: wordmark font is Abril Fatface (OFL, `@fontsource/abril-fatface@5.3.0`), a heavy Didone
  display face close to the reference's serif wordmark; it is drawn narrower with `scaleX` so the
  12-letter mark spans the same width. UI font is Inter (`@fontsource/inter@5.3.0`, OFL).
- W51-b-3: Advanced mode starts with the chat alone; Preview, Files, Review and Terminal are
  remembered as closed so the toolbar and Ctrl+Shift+T style reopen still bring them back. Simple mode
  is unchanged (Focus).
- W51-b-4: sidebar nav shows New session, Capabilities, Messaging, Artifacts and Scheduled jobs; the
  other destinations (Command center, Webhooks, Learning map, Bots, Profiles, Agents and plugin pages)
  stay one click away in the sidebar footer's icon row and its "More" menu; Settings moves to the
  title band's gear like the reference. Nothing is removed.
- W51-b-5: "Sessions | Bots" tabs head the sidebar; Bots opens the bots page (our bots live there).
- W51-b-6: the effort chip writes the existing per-model preset and `config.set reasoning` for the
  open session, the same calls the model picker's preset row makes; the label shows the preset or
  "Auto" when none is set (we do not claim a default the backend did not report).
- W51-b-7: `CompactSelect` and schema enums keep a real `<select>` under our closed frame
  (`appearance: none` + our chevron): platform keyboard and screen-reader behaviour, and the existing
  tests that read `<select>` values keep working. A custom listbox can replace it later behind the
  same props.
- W51-b-8: key hints (`KeyHint`) draw their caps from a `data-key` attribute (CSS `content`), so a
  shortcut never becomes part of a button's text or accessible name ("New session", not
  "New sessionCtrlN"); the e2e helpers match buttons by exact text.
- W51-b-9: the band's layout tools stay real controls (the preset `<select>` laid invisibly over
  its glyph, pane toggles and the Advanced / Simple radios as icon buttons with the old names) so
  every existing label and test keeps working.
- W51-b-10: the dark primary button uses dark text (#151617 on #4a84fe), as measured (lead note).
- W51-b-11: the chat tab shows the conversation's title ("New session" for a draft), uppercase.
- Not taken here (wave 52): tool rows, approval and error cards, menus' full content, settings pages'
  bodies, panes' insides, onboarding, HUD, quick entry.

Tests we will write:
- tokens: both themes define every token; text on canvas/sidebar meets 4.5:1, tertiary 3:1.
- presets: the Advanced initial layout is chat-only with the tool panes on the closed stack.
- sidebar: one clear button for a non-empty search.
- model chip: shows the model from `model.options` without opening the picker.
- existing Shell, Sidebar, PaneShell, Composer, ModelPicker, Settings tests keep passing (labels kept).

## Decisions for the lead
- W51-b-4/5 move rarely used destinations behind the footer row; say if any must stay in the list.

Compare checklist (filled after the rewrite; reopened the screenshots above, not the source):
- [x] tokens light/dark: band / canvas / input / bubble / rows / text ladder / hairlines / accent
  (test: `src/theme/deskTokens.test.ts`)
- [x] fonts: Inter UI, Abril Fatface wordmark (bundled, `main.tsx`)
- [x] window chrome: 34px band, sidebar toggle, uppercase chat tab with accent underline, icon cluster
  right, controls overlay kept clear via `env(titlebar-area-*)`
- [x] left sidebar: Sessions | Bots tabs, New session + Ctrl N caps, four nav rows, borderless search,
  PINNED / SESSIONS accent labels with the four-square glyph, empty "No sessions yet" + New project,
  footer icon row (Command center, theme, More menu)
- [x] default layout: chat alone (test: `src/panes/layoutStore.test.ts::starts on the chat alone…`)
- [x] new chat: wordmark + one caption line, composer bar with +, prompt, model chip, effort chip,
  mic, round send (test: `src/app/Shell.test.tsx::has a sidebar…`)
- [x] transcript: user message as tinted bordered box, plain assistant markdown, quiet code blocks,
  hairline tables with a tinted head
- [x] settings shell: inset card, 208px tinted menu with icons, chevrons, group gaps, breadcrumb, top
  search pill, close X; rows with right controls, pill switch, compact select, no key line
- [x] bugs: model chip names the model before the picker opens
  (test: `src/chat/ModelPicker.test.tsx::names the configured model…`); xterm follows light/dark;
  one clear button in sidebar search (`src/sidebar/Sidebar.test.tsx::a search with no match…`)
- Deliberate differences: the profile rail (ours) stays left of the sidebar; our status bar keeps its
  items (connection, approvals mode, folder, model) in the reference's 22px band; the band shows our
  pane toggles (files, review, preview, terminal) and the Advanced / Simple pair where the reference
  shows its layout-editor and HUD buttons; the wordmark is our text in an open font.
- Not taken (wave 52): tool rows, approval / error cards, menus' contents (slash, mention, model
  list), the settings pages' bodies and curation lists, panes, onboarding, HUD, quick entry.

## Tokens (for wave 52)

Source: `apps/desktop/src/theme/deskTokens.ts` (`deskPalettes`, `deskSizes`), painted by
`ThemeProvider` as `--desk-<name>` on `:root[data-theme="indus-light|indus-dark"]`. Use these names in
every stylesheet; the older `--indus-color-*` names are aliases of them (bg→bg-canvas,
bg-raised→bg-raised, fg→fg-1, fg-muted→fg-2, border→stroke-1, accent, accent-fg, success, warning,
danger, focus-ring→accent, code-bg→bg-code, selection). A chosen appearance preset re-derives the
desk surfaces from its colours (`deskFromPresetCss`).

| Token | Role | Light | Dark |
|---|---|---|---|
| `bg-band` | title band, sidebar, status bar, settings menu | #f6f8fa | #010409 |
| `bg-canvas` | chat, pages, overlay card, fields | #ffffff | #0d1015 |
| `bg-input` | composer bar | #fbfbfc | #0c0e12 |
| `bg-raised` | menus, popovers, palette | #ffffff | #151a21 |
| `bg-bubble` | the user's message | #fcfcfd | #0f1621 |
| `bg-code` | fenced code | #f6f8fa | #0f1116 |
| `bg-table-head` | markdown table head | #fafbfc | #11141a |
| `bg-kbd` | key caps | #f3f5f6 | #14171d |
| `bg-pill` | neutral badge | #f3f4f5 | #1a1e24 |
| `bg-card` / `stroke-card` | unselected choice card | #f0f2f8 / #ced9ef | #151a22 / #232d40 |
| `backdrop` | behind modal cards | black 55% | black 62% |
| `row-hover` | hover fill of rows and quiet controls | #e8eef7 | #0b111b |
| `row-active` | current row, open settings group | #d8e1f0 | #101826 |
| `row-active-soft` | current settings page, soft hover | #dee5f2 | #0d1421 |
| `fg-1` | primary text | #24292f | #d8dfe5 |
| `fg-2` | nav and secondary text | #4f5359 | #aab1b6 |
| `fg-3` | captions, placeholders, quiet chips, breadcrumb parents | #71757a | #7f858b |
| `fg-4` | faint (key caps, bullets, disabled) | #b4b7ba | #4a4e54 |
| `stroke-1` | neutral divider | #e3e6ea | #1c2027 |
| `stroke-2` | soft tinted hairline (tables) | #dae3f4 | #1d2535 |
| `stroke-3` | tinted frame (composer, user message, overlay card, search pill) | #c8d5f1 | #232f47 |
| `stroke-field` | text field / select frame | #e3e9f5 | #151b26 |
| `stroke-strong` | strong neutral edge (switch off) | #d0d7de | #2c3035 |
| `stroke-active` | open settings group edge | #b9cae8 | #1f2c44 |
| `accent` | brand blue: tab underline, labels, primary, switch on | #0053fd | #4a84fe |
| `accent-fg` | text on accent | #ffffff | #151617 |
| `accent-soft` / `accent-tint` / `accent-ring` | tinted fills, selected card, its ring | #e6eeff / #eff4fe / #cbdcfe | #13213d / #101722 / #192643 |
| `wordmark` | new-chat wordmark | #0053fd | #c5cbd1 |
| `send-bg` / `send-fg` | round send button | #2c3035 / #fff | #d8dfe6 / #0d0e11 |
| `danger`, `danger-soft`, `warning`, `success` | states | #c4213f, #fbeaee, #9a6516, #1a7f5a | #ef6f86, #2a1218, #d6a24a, #55a583 |
| `code-*` | fg, keyword, function, number, string, comment | GitHub-light-like | GitHub-dark-like |
| `selection` | text selection | accent 18% | accent 32% |

Sizes (`deskSizes`): `band-height` 34px, `status-height` 22px, `sidebar-width` 260px, `row-height` /
`control-height` 28px, `radius-control` 4px, `radius-card` 6px (overlay card 12px), `radius-pill`,
`text-micro` 11, `text-small` 12, `text-ui` 13, `text-nav` 14, `text-chat` 15, `text-title` 18,
`font-ui` (Inter), `font-display` (Abril Fatface), `shadow-menu`, `shadow-card`.

## Components (for wave 52)

Core (`apps/desktop/src/ui/components.tsx`, styles `ui/ui.css`):
- `Button({variant: primary|secondary|outline|ghost|text|danger, size: sm|md, icon})` → `.desk-btn`.
- `IconButton({label, icon, size: sm|md|band, active})` → `.desk-icon-btn` (24px, 4px radius;
  `aria-pressed` / `aria-checked` / `data-active` paint the active fill). Name via `label`, no `title`.
- `Kbd`, `KeyHint({keys})` (caps drawn from `data-key`, never in the button's name).
- `SectionLabel({icon, children, actions})` → `.desk-section-label` (11px uppercase accent) and
  `GroupGlyph` (the four-square mark).
- `Chip({children, caret})` → `.desk-chip` (composer pickers: quiet text + chevron).
- `Wordmark({text, caption})`, `EmptyState({icon, title, action})`.
- `Menu({label, trigger, items: MenuItem[], side: up|down, align: start|end, footer})` → `.desk-menu`
  (click-outside, Escape, arrow keys; items `role="menuitem"`).
- CSS-only: `.desk-tabs` / `.desk-tab` (uppercase strip with accent underline; `aria-current` or
  `aria-selected`), `.desk-search` (borderless search with underline on focus; the engine's cancel
  glyph is hidden everywhere), `.desk-input`, `.desk-select`, `.desk-breadcrumb`.

Overlay card and frames (`apps/desktop/src/app/overlay/OverlayFrame.tsx`, `overlay.css`):
- `OverlayCard({title, closeLabel, onClose, top, className})`: Radix dialog, inset 48px, radius 12px,
  1px `stroke-3`, `shadow-card`, X on the title line (`.desk-overlay-close`), optional pill on the
  top edge (`top`). Settings uses the same classes (`desk-overlay-card`, `desk-overlay-nav`).
- `NavFrame({nav, navLabel, header, navFooter, children})`: 208px tinted nav + main column
  (`.desk-overlay-head` / `.desk-overlay-body`, 56px gutters). `NavEntry({icon, label, current,
  group, expanded, depth, onSelect})` for its rows; `Breadcrumb({label, crumbs})`.
- `ListDetail({title, count, actions, search, add, list, listLabel, detail, empty})`: header with a
  count, 208px searchable list with a "+" row, detail column with a centred `EmptyState` until a row
  is chosen (Cron, Profiles, Webhooks, Agents, Learning map).
- `TwoLineRow({title, detail, meta, selected, onSelect})`, `TextLink({tone})` (underlined text
  action), `Segmented({label, value, options, onChange})` (small radiogroup).

Settings primitives (`apps/desktop/src/settings/primitives.tsx`; styles in `settings.css`):
`PageBody({intro})`, `SettingRow({title, description, control, below, wide, rowId, fieldKey,
highlighted, htmlFor, hint})` (keeps `data-row` / `data-field` / `data-highlighted`), `Switch`,
`SwitchRow` (`role="switch"`, named by the title), `CompactSelect({value, options, onChange, width})`,
`TextField({wide})` (214px / 288px), `FreeCombo({value, suggestions, onCommit, label})`,
`SectionHeading({icon, title, count, aside})`, `GroupLabel`, `SettingsEmpty({title, body})`,
`Pill({tone})`, `TintCard({selected})`, `DangerCard({title})`, `TextButton`, `OutlineButton`,
`PrimaryButton`, `MoreFields({label, open})`, re-exports `Kbd`, `Segmented`, `TextLink`.
`SchemaPage` renders `FieldRow`s with these classes; a `PageDef.curated` list puts the remaining
fields behind "More settings" (it opens itself for a search hit inside it). The settings menu reads
`navMeta.tsx` for each view's icon and the group gaps (before Providers, Plugins, About); a view with
one page shows no chevron (Plugins, Keyboard).

Shell pieces (`app/Shell.tsx`, `panes/PaneShell.tsx`): `.band` (drag region, buttons no-drag),
`PaneShell` props `bandTools` (first in the right cluster) and `chatTitle`; the zone under the
cluster gets `data-band-end` and keeps its tabs clear of it.

## Status

Done for wave 51 (2026-10-24). Tokens, fonts, title band, sidebar, chat-only start, new-chat screen,
composer bar with model and effort chips, base transcript and markdown, settings shell and row
primitives, the shared overlay card and list/detail frames, and the three desktop bugs (model chip,
light terminal, double clear button) are in. Outside `apps/desktop`: the OFL font exceptions
(`tooling/checks/licenses_allow.json`), notices data and text, `THIRD_PARTY_NOTICES.md`, lockfile.
Test helpers changed on purpose: `testing/electron/cdp.ts` (`hasButton` / `click` also match an
icon button's `aria-label`), e2e labels "New session" / "No sessions yet", the e2e `showPane` helper
(Files and Review open from their band toggle), and the layout/pane tests that assumed the Default
start layout now apply it first.

Handed on to wave 52:
- transcript: tool rows, approval / error cards, the user message's time and undo, sticky clipping;
- composer menus: slash / mention / model list contents in the desk menu style; the voice
  conversation button's round look;
- settings pages: bodies, `curated` lists per page, Appearance theme gallery, footer icons;
- panes: files / review / terminal / preview insides; the capture kit's pane steps must find the
  band's icon buttons by `aria-label` ("Open Terminal", "Open Preview", plugin panes);
- session titles from the backend start with "Chat: " for some sessions (the tab and the row show it).
- biome reports 3 `noDescendingSpecificity` warnings (ui.css, settings.css); harmless, tidy later.

## Integration (wave 53, desktop)
Decision ids (§2.7 of `docs/plan/16-what-we-change.md`): W51-b-1 and W51-b-10 → D-16.1210; W51-b-2 →
D-16.1211; W51-b-3 → D-16.1212; W51-b-4, W51-b-5, W51-b-11 → D-16.1213; W51-b-6 → D-16.1214;
W51-b-7, W51-b-8, W51-b-9 → D-16.1215. Wave 52 g, h, m extended the foundation's
`ui/components.tsx` / `ui.css` (menu family, floating HUD, tooltip, `SmallDialog` — now with an
optional `role` —, `PanelLabel`, `MenuItem` tone) and `deskTokens.ts` (`attention`); see D-16.1221,
D-16.1222.
