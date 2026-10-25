# Task: Desktop panes, HUD, quick entry and onboarding (W52-m) Phase: ui-parity Owner: builder w52m

Status: Done for wave 52 (2026-10-24); differences and follow-ups under Status at the end.

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): via the wave-52 spec
`desktop-panes-onboarding.md` (Read + Note done by the spec writer from
`apps/desktop/src/app/right-sidebar/{index,files/tree,review/index,review/ship-bar,review/scope-row,review/file-tree}`,
`app/shell/sidebar-label`, `components/ui/pane-tab`, `app/hud/hud-shell` (+ HUD rules of `styles.css`),
`app/chat/composer/controls`, `app/quick-entry/quick-entry-app`, `components/onboarding/{index,providers,flow}`);
this builder did not open reference source, only the screenshots.
Evidence: reference screenshots (wave-51 compare set, never in the repo), desktop slugs `files-pane`,
`review-pane`, `right-sidebar`, `hud`, `quick-entry`, `onboarding`, `onboarding-other-providers`,
`onboarding-api-key`, each `--light` and `--dark`.

What it does (own words): the right side of the desktop window, the floating HUD and quick-entry
windows and the first-run provider setup take the reference's look. Opening Files (or the right-side
toggle) builds a right group: a Files column on top and the Terminal under it; opening Review adds a
column left of Files. Side panes that name themselves (Files, Review) have no tab strip, only a 28px
header with an uppercase accent "panel label" and small icon actions; the Terminal keeps a slim
uppercase strip. The file tree is a quiet 22px list with folder/file outline icons and git-tinted
names; the review pane is a scope segmented control, a list of changed files with square status glyphs
and +/- counts, a diff strip for the selected file and a ship bar (message field, split commit button,
PR link). The HUD is a composer bar on top with a translucent dark sheet under it carrying the
transcript; quick entry is a single card with a "›" prompt and a "Send to" select; onboarding is one
720px card with a notice box, a provider list with a disclosure for key providers, and a key form with
a tile grid. Behaviour and data flows stay ours.

Inputs → outputs: pane open/close events, `bridge.fs`/`bridge.git` data, transcript busy state, the
onboarding flow state (`reopen` reason added) → the DOM described above.

Files: `apps/desktop/src/panes/**` (PaneShell zone chrome, presets, files, review, terminal, panes.css),
`apps/desktop/src/ui/components.tsx` + `ui.css` (PanelLabel, shared), `apps/desktop/src/hud/Hud.tsx`,
`apps/desktop/src/quick/QuickEntry.tsx`, `apps/desktop/src/ambient/*`, `apps/desktop/src/onboarding/*`,
the Electron window sizes for HUD (620x320) and quick entry (640x168) and their tests, e2e selector updates.

Edge cases / failure modes:
- not a git repo: review header hidden, only the empty label; files tree has no tint (errors ignored).
- no working folder / remote folder: muted labels, our remote note kept.
- tests and e2e keep `#pane-tab-<id>`, the "Open <pane>" buttons, `role=radio` scopes, "Discard",
  "Commit message", "Write it for me", "API key", "Paste an API key", "Choose later", "Back".
- reduced motion: HUD sheet shows/hides without fades.

Our design differences / decisions:
- W52-m-1: no "recommended" provider, logos or "Get a key" links (lead decision; no backend data). The
  featured-row component exists but is used only when a provider is marked featured (never today).
- W52-m-2: quick entry "Send to" offers "Current chat" only (lead decision; `quick.submit` has no target).
- W52-m-3: no "show ignored files" toggle in Files (our listing has no ignore filter); no folder picker
  when no folder is set.
- W52-m-4: the right group is a new `side` preset shape produced when a side pane opens into the chat-only
  layout; the old presets keep working.

Tests we will write:
- PaneShell: side panes open into the right group (files top, terminal below, review left of files).
- Files: collapse-all, git tint classes, context menu actions.
- Review: scope radios, branch line in DOM, split button menu, revert dialog.
- Onboarding: notice by reason, disclosure, tile grid replaces the select.
- QuickEntry: no visible Send button; "Send to" select.

- W52-m-5: `PanelLabel` (uppercase accent label with a drawn dithered square) is added to the
  foundation's `ui/components.tsx` / `ui.css` (it did not exist there), not as a second copy.
- W52-m-6: the review notices (committed, pushed, failed) stay an inline `role=status` line at the
  bottom of the pane; the revert confirm is an in-pane modal card (`role=alertdialog`). The desktop
  foundation has no toast or confirm-dialog component yet.
- W52-m-7: the list/tree pair stays a `radiogroup` (tests use "Tree"); only the radio for the other
  view is drawn, so the header shows one toggle icon like the reference.
- W52-m-8: the branch line ("On main · 2 ahead, 1 behind …") is the REVIEW label's tooltip and a muted
  line under the scope row only when ahead/behind is not zero (otherwise it stays in the DOM, visually
  hidden).
- W52-m-9: the terminal's shells move to a 36px rail on the right of the terminal body (28px square
  tabs named by the shell, close badge on hover, "+" revealed on hover).
- W52-m-10: Alt+B on a window with no right side opens Files and Terminal (the right group); with a
  right side it hides/shows it as before.

Tests we wrote: `panes/layoutStore.test.ts` (right group shape; other arrangements untouched),
`panes/files/FilesPane.test.tsx` (git tint, collapse all, right-click menu, empty folder),
`onboarding/Onboarding.test.tsx` (notice by reason, disclosure, tiles instead of the select),
`quick/QuickEntry.test.tsx` (one-line input, "Send to" select, no Send button).
Tests updated on purpose: `ReviewPane.test.tsx` (rows show name then folder → found by title;
"Commit & push" lives in the split button's menu; tree folder row reads "src"),
`Onboarding.test.tsx` parity hooks (open "Other providers" first), `electron/ambient/hud.test.ts` and
`quickEntry.test.ts` (window sizes 620x320 / 640x168, lead decision), e2e
`surfaces.electron.test.ts` (provider tile instead of the select).

## Decisions for the lead
- W52-m-6: confirm dialog and toasts were taken in-pane because the foundation has neither; when the
  foundation gets them, swap the review pane's `review-dialog` and notice line over.
- Capture note: parallel builders' copies of the capture script delete every `/tmp/indus-e2e-*`
  folder created during their run, which removed this run's app build mid-capture (terminal "could
  not load", blank HUD). This builder's kit copy uses a private `TMPDIR` under `$S` and no global
  cleanup; other builders' kits should do the same.

Compare checklist (filled after the rewrite; reopened the reference screenshots above, not the source;
side-by-side PDF `$SP/w52m/compare.pdf`):
Right side
- [x] Default window: no right panes; Alt+B / titlebar toggle opens Files on top + Terminal below, 236 px wide.
- [x] Opening Review adds a 236 px column left of Files; seams 2 px, no dark rules, no shadows.
- [x] Files/Review zones have no tab strip; Terminal zone has a 26 px strip with "TERMINAL" + 2 px primary underline.
- [x] `#pane-tab-files` / `#pane-tab-review` still exist; e2e "panes" test passes.
- [x] Panel labels: uppercase, 10.25 px, 0.16em, primary, dither dot (folder name; REVIEW).
- [x] Files: 22 px rows, no chevrons, folder/file outline icons, 12 px secondary names, ellipsis.
- [x] Files: refresh hover-reveal; collapse-all only when a folder is open; changed files tinted.
- [x] Files: right-click menu with our actions; empty/no-folder/error states as labels.
- [x] Review header: 5 icon buttons at 20 px pitch; segmented scope row 25 px on primary-tint track.
- [x] Review rows 24 px with tinted square status glyphs, "+N −N" counts, hover stage/revert.
- [x] Selected row wash; diff strip with mono path header, counts, stage, close; coloured diff lines.
- [x] Ship bar: auto-grow message field with sparkle button, primary split "✓ Commit" + chevron, underlined agent PR link + PR icon.
- [x] Revert confirm is a dialog (in-pane, W52-m-6); notices stay an inline status line (no foundation toast yet).
- [x] Terminal body matches chat surface in light and dark (no black slab in light).

HUD
- [x] No header row; composer bar on top, 41 px, radius 12, primary focus border.
- [x] Bar right end: model, effort, mic, voice circle, reset, leave-HUD icons.
- [x] Sheet inset 8 px, ~82% near-black in both themes, rounded bottom corners.
- [x] Agent text white, user text gold without bubble; tool rows hidden.
- [x] Sheet fades in on activity/focus and out after ~1.1 s idle; hold-to-move on bar background.

Quick entry
- [x] Transparent window, one 12 px-radius card with faint primary border and small shadow, centred.
- [x] "›" + single-line 15 px input; "Send to" + small select "Current chat".
- [x] No Send button / hint footer; Enter sends, Esc and blur dismiss; error line red 11 px.

Onboarding
- [x] 720 px card, 12 px radius, near-invisible border, soft shadow, centred on page surface.
- [x] Header 15 px/600 title + 13 px tertiary description (our copy).
- [x] Notice box (tinted, 1 px tinted border, 14 px muted text) on first run / provider error only.
- [x] Rows: 14 px/600 title, 12 px muted subtitle, right chevron (terminal icon for terminal flows), hover wash.
- [x] No fake "recommended" provider; featured component exists but unused.
- [x] "Other providers ⌄" disclosure expands key providers + custom endpoint; "Collapse ⌃".
- [x] Footer: "Choose later" left, "Paste an API key" right, 11 px muted text buttons.
- [x] Key screen: "‹ Back", 2-column tile grid with primary ring on the selected tile.
- [x] Description line, mono "Paste API key" password input with primary focus border.
- [x] Right-aligned primary key button with key icon (50% when disabled); "Choose later" centred below.
- [x] Endpoint options use the same layout ("Test and continue").
- [x] Onboarding vitest + e2e (`surfaces`, `boot`) pass after the deliberate selector updates.

- Deliberate differences: no "recommended" tag, provider logos or "Get a key" links (W52-m-1); quick
  entry offers "Current chat" only (W52-m-2); no "show ignored files" toggle and no folder picker
  (W52-m-3); the onboarding key form keeps the eye toggle beside the password field and the button
  reads "Save the key" (tests); the terminal zone keeps its Hide / Close actions on the strip; the
  title band keeps our pane toggles and Advanced/Simple pair (foundation); the HUD composer is our
  composer (placeholder "Give Indus a task", model chip, mic, talk, send) where the reference shows its
  own controls; the HUD transcript keeps its scrollbar; review notices are a status line.
- Not taken: the narrow-width scope dropdown (< 216px), shiki highlighting of the diff strip, the
  HUD's "prompt needs an answer keeps the sheet up" rule (the sheet follows focus and transcript
  activity only).

## Status

Done (2026-10-24) for all slugs: files-pane, review-pane, right-sidebar, terminal chrome, hud,
quick-entry, onboarding, onboarding-other-providers, onboarding-api-key. Nothing remains open in this
slice except the follow-ups below. Checks: tsc clean, desktop vitest 1706/1706, guards ok, e2e
surfaces + boot pass; e2e `chat.electron.test.ts` failed 5 cases in a run before its "Other providers"
click was added (the custom endpoint row moved behind the disclosure; the later cases cascaded from
the onboarding overlay left open) — fixed in the test, NOT re-run (usage wind-down).
Right group layout, files / review / terminal panes, HUD, quick entry and
onboarding rebuilt on the foundation tokens; captures in both themes re-shot and compared
(`$SP/w52m/compare.pdf`). Shared files touched: `apps/desktop/src/ui/components.tsx` and `ui/ui.css`
(PanelLabel, additive), `apps/desktop/src/app/Shell.tsx` (Alt+B opens the right group, a few lines),
`apps/desktop/src/panes/strings.en.ts`, `onboarding/strings.en.ts`, `ambient/strings.en.ts` (own
blocks). Follow-ups: quick-entry targets need a `target` on `indus:quick:submit` plus a state push;
a per-chat folder picker for the "No folder" state; foundation toast + confirm dialog, then move the
review pane onto them; the HUD's sheet should also stay up while an approval waits.

## Integration (wave 53, desktop)
Applied after w52g and w52h. The foundation now has a toast region (w52g `notify()`) and a small
dialog (w52h `SmallDialog`), so the follow-up above is done: review notices (committed, pushed,
failed) are toasts with a visually hidden `role=status` mirror, and the revert confirm is
`SmallDialog` with `role="alertdialog"` (title "Revert", the files in mono, Cancel / "Discard");
the pane's `review-dialog*` / `review-notice` rules were removed (`review-dialog-path` kept).
`chat.electron.test.ts` ("Other providers" → custom endpoint) ran at integration: its URL field
selector was stale (it filled the endpoint's key field), now `data-ui="onboarding-endpoint-url"`; the
whole file and `surfaces` / `boot` pass. Decision ids: W52-m-1, W52-m-2, W52-m-3 → D-16.1226; W52-m-4,
W52-m-10 → D-16.1227; W52-m-5 → D-16.1222; W52-m-6 – W52-m-9 → D-16.1228; e2e updates D-16.1229.
