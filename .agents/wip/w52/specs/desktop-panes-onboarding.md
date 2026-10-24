# W52 spec — desktop slice `desktop-panes-onboarding`

Status: spec ready for a wave-52 page builder (written 2026-10-24, Read + Note only, no code).
Surface: desktop (apps/desktop renderer). Slugs: files-pane, review-pane, right-sidebar, hud, quick-entry,
onboarding, onboarding-other-providers, onboarding-api-key. The terminal pane's chrome is part of this
slice too (it sits inside the files-pane / right-sidebar shots).

Read (Evidence, never copy): `$SP/compare/hermes/desktop/<slug>--{light,dark}.png` + manifest;
`$SP/compare/indus/desktop/` (ours; `onboarding-other-providers` missing, `terminal-pane` and
`preview-pane` exist only on our side). Reference source read for structure/behaviour:
`apps/desktop/src/app/right-sidebar/{index,files/tree,review/index,review/ship-bar,review/scope-row,review/file-tree}`,
`app/shell/sidebar-label`, `components/ui/pane-tab`, `app/hud/hud-shell` (+ HUD rules of `styles.css`),
`app/chat/composer/controls` (HUD buttons), `app/quick-entry/quick-entry-app`, `components/onboarding/{index,providers,flow}`.
Everything below is described in our words; class names, CSS and copy from the reference are NOT to be reused.

Measurements are in CSS px from the 1440x900 @1x shots (HUD shot 620x320, quick-entry shot 720x220).
Colours were sampled from the PNGs (light = the reference's blue light preset, dark = its default dark).
Map every hex to the W51-b foundation token with that value (see `docs/notes/ui-parity/desktop-foundation.md`
once it lands); only add a token if the foundation has none — name it in our scheme.

---------------------------------------------------------------------------------------------------

## 0. Shared palette for this slice (sampled)

| role (suggested token meaning)          | light     | dark      |
|-----------------------------------------|-----------|-----------|
| side-surface (titlebar, sidebars, panes, status bar, tab gutter) | `#f6f8fa` | `#010409` |
| chat-surface (transcript, terminal body, active tab)             | `#fefefe` (`#fbfbfc` behind the composer) | `#0d1015` (`#0e0f12`) |
| seam between panes (2 px)               | `#f3f6f9` | `#01050b` |
| text-primary (ink)                      | `#2c3035` | `#d8e0e6` |
| text-secondary (tree/review row names)  | `#575a5f` | `#aab1b6` |
| text-tertiary (icons, status bar, hints)| `#828589`–`#8a8d91` | `#767b81`–`#7c8188` |
| text-quaternary (idle hover-reveal icons)| `#babdbf` | `#43474c` |
| primary (accent, labels, underlines, focus) | `#0053fd` | `#4a84fe` |
| primary @ ~50% (disabled primary button) | `#7aa4fb` | `#254484` |
| row-active / segmented track (primary-tinted wash) | `#d8e1f0` | `#101826` |
| diff add (counts)                       | `#208a66` | `#54a381` |
| diff remove (counts)                    | `#d33f64` | `#d4566e` |
| modified glyph (amber)                  | `#fca825` | `#d78401` |
| chat scrollbar thumb (8 px)             | `#e3e3e3` | `#23262a` |

Type: UI sans of the foundation (the reference is an Inter-like grotesk); mono only where noted.
"Panel label" style (used for REVIEW, the folder name, empty states): 10.25 px (0.64rem), weight 600,
UPPERCASE, letter-spacing 0.16em, colour primary, preceded by an 8x8 square "dither dot" (radius 1 px,
a fine checker of primary and transparent — draw it ourselves, e.g. a 2 px repeating conic/linear
gradient), gap 8 px, left padding 8 px. Muted variant: same, colour text-quaternary, no left padding.

---------------------------------------------------------------------------------------------------

## 1. Right side layout (slugs files-pane, review-pane, right-sidebar)

### 1.1 Regions at 1440x900

- Titlebar: y 0–33 (34 px), side-surface, no bottom rule. Owned by W51-b (session tabs on the left with a
  2 px primary underline at y 32–33; five 14 px icon buttons at the far right, x ≈1236/1260/1284/1308/1332,
  colour text-tertiary — the last one toggles the right side). This slice only needs the right-side toggle
  wired (see 1.6).
- Status bar: y 880–899 (20 px), side-surface. W51-b.
- Chat zone: x 260 → seam. Its scrollbar thumb (8 px) hugs the seam.
- files-pane shot (files only): seam x 1203–1204 (2 px), Files column x 1205–1439 (≈235 px).
- review-pane / right-sidebar shots: seam x 966–967, Review column x 968–1202 (≈235 px), seam x 1203–1204,
  Files column x 1205–1439. Both side columns are the same width (≈236 px each incl. seam).
- Terminal zone spans the whole right group under both columns: seam y 700–701, tab strip y 702–727
  (26 px, side-surface), body y 728–879 (152 px, chat-surface). So the right group is a column:
  top = row [Review | Files] (flex), bottom = Terminal (≈178 px incl. strip).
- No borders other than the 2 px seams (seam colour is barely different from side-surface — a hairline
  "inset highlight" feel, not a dark rule). No shadows.

### 1.2 Zone chrome rule (our PaneShell)

- Chat zone: no tab strip (the titlebar session tabs replace it — W51-b).
- A zone that holds ONE side pane that labels itself (files, review) shows no tab strip; the pane's own
  28 px header (panel label + actions) is the top of the zone.
- Tool zones (terminal, preview) and any zone with 2+ panes show the strip.
- Tab strip: 26 px, side-surface gutter. Tab: 11 px, weight 500, UPPERCASE in this zone (shot shows
  "TERMINAL"), padding ≈8 px, min width 52 px, max 192 px; idle text text-tertiary, hover darkens the
  background slightly; active tab = chat-surface background, text-primary, 2 px primary underline drawn
  inside its last 2 px (x 1203–1277 in the shot, i.e. tab ≈75 px wide). Adjacent tabs separated by a
  1 px quaternary stroke. Hover shows a close ✕ over the right edge for closeable tabs.
- KEEP for tests/e2e: the tab elements stay in the DOM with `id="pane-tab-<id>"` and role `tab` even when
  the strip is visually hidden (render them visually hidden, or make the self-label header the tab).
  e2e `chat.electron.test.ts › showPane` clicks `#pane-tab-<id>` and the "Open <pane>" buttons; if W51-b
  removed the layout toolbar, give those buttons a home (titlebar layout menu) with the same accessible
  names, or update `showPane` in the same change.

### 1.3 Files pane (slug files-pane)

Header (28 px, x padding 10 px): panel label with the working folder's basename (`RUN-MOCK` in the shot,
label at y≈48), flex spacer, then three 20 px ghost icon buttons (13 px icons):
1. show/hide ignored files — SKIP (our fs listing has no ignore filter; do not invent it).
2. Refresh (lucide `RefreshCw`, spins while loading) — hover-reveal: invisible until the header is
   hovered or focused-within, then text-tertiary → hover text-primary on a subtle hover wash.
3. Collapse all (lucide `ListCollapse`/`ChevronsDownUp`) — shown (not hover-gated) only when any folder
   is open; collapses all client-side expanded state (our `dirs` map).
Idle icon colour text-quaternary (`#babdbf` / `#43474c`).

Tree: row height 22 px (icons at y 69, 91, 113… in the shot), indent 10 px per level, base inset 16 px.
Row: [14 px icon column] gap 4 px [name, 12 px, weight 400, text-secondary, truncate with ellipsis].
No chevron column — the folder icon carries open/closed state (lucide `Folder` / `FolderOpen`, `File`;
icon colour text-tertiary). Hover: text-primary + row hover wash. Selected: row-active background,
text-primary. Long names ellipsize, never wrap or scroll sideways.
Git tint: names of changed files coloured — modified amber, added green, conflicted red. Wire to
`bridge.git.status({repo})` (we have `files[].index/worktree`); refresh with the tree. If the folder is not
a repo, no tint (catch and ignore).
Interactions (ours, keep): click folder toggles; click file selects; double-click / Enter previews
(`onPreview`); shift-click adds the path to the chat (`addToChat`, we have it in PaneRenderContext);
F2 renames. Move our per-row "⋯ actions" strip into a right-click context menu (Preview, Rename, Move to
trash / Delete, Show in folder, Copy path) and keep the same accessible names
("Actions for <name>" trigger can stay as a hover-reveal button for keyboard users — tests use it).
Delete confirm (remote) → the foundation's confirm dialog (role `alertdialog`, tests query it).
Lock icon for sensitive files stays (12 px, text-tertiary).
States: loading (blank for ~150 ms, then 4–6 skeleton bars 22 px pitch); folder read error → centered
title (11 px, 600, uppercase, letter-spacing 0.07em, muted) + body (11 px, muted) + "Try again" text
button; empty folder → muted panel label "NO FILES" centered; no working folder → muted panel label
"NO FOLDER" centered (no button: we have no per-chat folder picker — follow-up), remote → keep our remote
note as the body line. Truncated listing → keep our note as an 11 px muted row at the end.

### 1.4 Review pane (slugs review-pane, right-sidebar)

Header (28 px): panel label `REVIEW` (x≈998), spacer, five 20 px ghost icon buttons at 20 px pitch
(x≈1102/1122/1143/1163/1183), icons 13 px, colour text-secondary-ish (`#5e6065` / `#a6acb1`):
list↔tree toggle (lucide `List` / `ListTree`, disabled with no files), stage all (`Plus`), revert all
(`Undo2`, opens confirm), refresh (`RefreshCw`, spins), close (`X`, closes the pane via the shell).
Tooltips carry the names; accessible names = our existing strings (Stage all, Revert all, Refresh, List/Tree).
The header hides entirely when the folder is not a repo (only the empty label shows).

Scope row (below header, padding 0 8 6): segmented control, full width (x 974–1195), 25 px tall
(y 62–86), track = row-active colour, radius 4 px; 3 equal-ish segments "Uncommitted" / "Branch" /
"Last turn" (11 px, weight 500); active segment = chat-surface fill with 1 px soft shadow, text-primary;
inactive text `#747a84` / `#838b95`. Keep role `radiogroup` + `radio` (tests use "Branch", "Last turn").
Narrow (< ~216 px) → collapse to a small dropdown showing the active scope.
Our branch line ("On main · Not pushed yet / 2 ahead…") is not in the reference header: show it as the
tooltip of the REVIEW label AND as one 10.5 px muted line under the scope row only when ahead/behind
≠ 0 (tests read "2 ahead, 1 behind origin/feature" — it must stay in the DOM).

File list (flex, scrolls): rows 24 px, radius 6 px, inset 14 px + 12 px per depth, gap 6 px:
[status glyph 13 px] [name 12 px text-secondary] [dir 11 px text-tertiary, shrinks first] … [counts].
Status glyph by git letter (lucide): M `SquareDot` amber; A/C `SquarePlus` green; D `SquareMinus` red;
R `SquareArrowRight` sky (`#0ea5e9`-ish at 85%); U `TriangleAlert` red; `?` `SquarePlus` text-quaternary.
Counts: "+N" green and "−N" red, 10.25 px, tabular, gap 4 px (shot: "+2 −1" at x≈1160–1192).
Hover: counts replaced by two 16 px icon buttons — stage/unstage (`Plus`/`Minus`) and revert (`Undo2`,
hover red). Selected row: row-active background, text-primary (right-sidebar shot y 99–118).
Folder rows (tree mode): `Folder`/`FolderOpen` 13 px text-tertiary, name, counts shown only when collapsed.
Single click selects (opens the diff strip); double click previews the file.
Empty: muted panel label "NO CHANGES" centered (our "No changes." / "Nothing to compare against yet."
become the label / its tooltip; keep the text in the DOM for tests if they query it).
Read-only scopes: no stage/revert buttons, ship bar replaced by one centered 10.25 px tertiary line
(our "Read-only view").

Diff strip (only when a file is selected; right-sidebar shot y≈556 onward): 1 px top border
(`#c2d0ee` light — a faint primary seam / `#19263e` dark), header row padding 6 px 10 px: path in mono
10.5 px text-secondary (truncate), counts, stage `Plus` (uncommitted only), close `X` (clears selection).
Body: max 55% of the pane height, scrolls; mono 11 px, line-height 16 px; added lines green text on a
~8% green wash, removed red on ~8% red wash, hunk headers primary, file headers muted. (Our `DiffView`
colouring stays; add the washes; shiki highlighting optional.) Loading → skeleton after 150 ms;
empty diff → centered 10.5 px muted "No diff".

Ship bar (bottom, padding 8 px, gap 6 px; only uncommitted scope with files):
1. Message field: 1-row auto-growing textarea (max 160 px), 1 px stroke `#828589`-ish at low contrast,
   radius 3 px, 12 px text, placeholder in our words with the shortcut, e.g. "Message (Ctrl+Enter to
   commit)"; a 32x24 icon button flush right inside it = "Write it for me" (lucide `Sparkles`; spinner +
   stop while drafting). Keep accessible names "Commit message" and "Write it for me".
2. Split button, full width, 24 px: primary fill, white 12 px "✓ Commit" centred; 28 px chevron segment on
   the right separated by a 1 px lighter line (`#c2d5fc` / `#18284a`); menu: "Commit", "Commit & push" —
   the chosen one becomes the main action (remember per window). Disabled = primary at 50%
   (`#7aa4fb` / `#254484`). Ctrl/Cmd+Enter in the field runs the main action.
3. Agent row: centered ghost text button, 11 px, muted, underlined (offset 2 px) — our "Ask the agent to
   open it" behaviour (`askAgent`) with a short label in our words (e.g. "Ask Indus to open a PR"); a 28 px
   icon button pinned right (lucide `GitPullRequest`, text-quaternary) = Create pull request through `gh`
   (our `createPr`; opens our inline PR-title form; disabled with tooltip when gh is missing).
Revert / revert-all confirm → foundation confirm dialog (destructive), role `alertdialog`, confirm label
"Discard" (tests use it), path shown in mono 11 px under the description.
Notices (committed / pushed / failed) → foundation toasts instead of the inline status line (keep a
visually hidden `role=status` mirror if a test reads it).

### 1.5 Terminal zone

Strip as in 1.2 ("TERMINAL" tab). Body = chat-surface; xterm theme from tokens (light bg `#fefefe`
fg `#2c3035`, cursor primary; dark bg `#0d1015` fg `#d8e0e6`) — this also fixes the "black terminal in
light theme" bug if W51-b has not. Our per-shell tabs ("bash ✕ +"): restyle as the same tab style
(11 px, no uppercase, 22 px tall) in a slim row, or move them to a 36 px vertical rail on the right edge
(reference keeps instances in a right rail: 28 px square buttons, active marked by a 2 px bar on the
inner edge, a "+" at the bottom revealed on hover). Keep role `tab` named by the shell name (tests).

### 1.6 Opening / layout behaviour

- Default layout = chat only (W51-b). Opening Files or Terminal (our Alt+B "toggle right" and the
  titlebar right-side toggle) builds the right group: Files column on top, Terminal under it.
- Opening Review inserts the Review column to the LEFT of Files in the same top row (as in review-pane).
- Closing the review pane (its ✕) removes only that column. Sizes: side columns 236 px each by default,
  terminal 178 px; separators stay draggable (our `Separator`), min column 160 px.
- Express this as a new preset (e.g. `side`) in `presets.ts` used by `initialLayout` when tool panes are
  opened, or as the shape `PaneShell` produces when opening a pane into an empty right group. Keep
  `basic/focus/default/terminal-deck/quad` working (PaneShell/layoutStore tests).
- The quad preset in our right-sidebar shot is not what the reference shows — do not use it for the
  Compare capture; capture with Review + Files + Terminal open.

---------------------------------------------------------------------------------------------------

## 2. HUD (slug hud) — floating window

Reference shot is 620x320 (our window 560x240: `HUD_DEFAULT_SIZE` in `electron/ambient/hud.ts`; changing it
to 620x320 is optional and touches `hud.test.ts`; the renderer must look right at both).

Shape: a Spotlight-style bar ON TOP, the transcript hanging BELOW it on a dark translucent sheet. No
header, no title text, no grip row.

- Composer bar: full window width, 41 px tall incl. a 1 px border, radius 12 px, fill = chat-surface
  (`#fbfbfc` / `#0e0f12`), border = primary when focused (`#0053fd` / `#4a84fe`), stroke otherwise.
  Contents left→right (vertical centre y≈20): "+" attach (11 px glyph, x 15), input with placeholder in
  our words (15 px, text-tertiary `#84868a` / `#82868c`, x 37), spacer, model chip text "<model> ⌄"
  (12 px, tertiary, x≈320), effort "Med ⌄" (x≈428), mic icon (x≈488), voice button = 26 px circle filled
  text-primary with inverse icon (x 509–534), a small chevron (x≈541), then two HUD-only 14 px icon
  buttons: reset position (lucide `Undo2`/`RotateCcw`, x≈566) and leave HUD (lucide `Minimize2`, x≈594).
  This is our normal chat composer (slots.composer) re-ordered to the top; model/effort/voice are the
  composer's own controls from W51-b / the composer slice — do not fork them.
- Sheet: x 8 → width−8 (inset 8 px each side), from the bar's bottom edge to the window bottom, colour
  `#0c0d12` at ~82% opacity in BOTH themes, bottom corners radius ~12 px, no border.
- Transcript on the sheet: same chat renderer, content left at x≈55 (≈47 px inside the sheet), line pitch
  27 px; agent text pure white `#ffffff` 15 px; user turns NOT in a bubble — gold text `#ffcf6b`, no fill,
  no border, not sticky; tables/code keep structure with muted cells (`#a4a4a6`) and dim borders; tool
  blocks, diff panels, system rows and the timeline/jump buttons are hidden in the HUD.
- Visibility: at rest only the bar shows; the sheet + transcript fade in (≈110 ms) while a turn is
  running, for ~1.1 s after activity, and while the composer is focused; fade out ≈180 ms. Prompts that
  need an answer (approval/clarify) keep it visible. Respect reduced motion (no fades).
- Moving: press-and-hold on empty bar space (our 150 ms hold → `hud.drag({phase,mode:"move"})`) replaces
  the grip row; edge handles stay (invisible, native resize cursors).
- No conversation (`sessionId === null`): bar + our "hud.empty" line in muted text on the sheet.
- Window: transparent html/body (we already have it), the bar and sheet are the only painted parts.

Mapping: `src/hud/Hud.tsx` (drop `.hud-bar` header, render composer first, sheet wrapper, hold-to-move on
bar background, reset/close buttons move into the bar's right end), `src/ambient/ambient.css` (`.hud*`
rules rewritten), `ChatView` gets a `variant="hud"` (or data attribute) so CSS can hide tool rows and
restyle user turns without touching the main window. Strings: keep `hud.reset`, `hud.close` (accessible
names), add a HUD placeholder key if the composer accepts one. E2E `surfaces.electron.test.ts` only checks
the route/hash and `hud.close()` — unaffected.

---------------------------------------------------------------------------------------------------

## 3. Quick entry (slug quick-entry) — floating window

Reference window 640x168 (shot 720x220); ours `QUICK_SIZE` 620x150 (`electron/ambient/quickEntry.ts`,
pinned by `quickEntry.test.ts`) — keep or move to 640x168, the card must not fill the window either way.

- Window is transparent; one card centred vertically with 12 px transparent padding around it.
- Card: width = window − 24 (696 px in the shot, x 12–707), height by content (≈74 px), radius 12 px,
  1 px border (`#c7d4f0` light — a faint primary stroke / `#2a354d` dark), fill elevated surface
  (`#fdfdfd` / `#16181d`), shadow 0 2px 8px rgba(0,0,0,.22) (must fit inside the 12 px padding),
  padding 10 px 14 px, rows gap 8 px.
- Row 1: "›" prompt glyph 15 px muted (`#8c8c8c` / `#868686`), gap 10 px, single-line input 15 px,
  transparent, no border/outline, text-primary; placeholder text-tertiary (`#c4c4c6` / `#4b4f54`).
  Placeholder: our "Ask Indus…" style string (keep `quick.placeholder`, add the ellipsis if wanted).
- Row 2: "Send to" label 11 px muted, then a small native select 11 px (1 px stroke, radius 6 px,
  padding 2 px 6 px, transparent bg, text-primary; give the options explicit themed colours so the OS
  popup stays legible in dark).
  DATA: our `quick.submit({text})` has no target and main delivers to the last-used window's chat. Show
  the select with the one honest option "Current chat" (disabled-looking is fine). "New session" and
  recent sessions need a `target` field on `indus:quick:submit` + a state push from main → follow-up,
  do not fake them.
- Error line (our `quick-error`, role `alert`): 11 px destructive red under row 2.
- Remove the visible Send button and the "Enter to send, Esc to close" footer: Enter sends, Shift+Enter is
  ignored (single-line), Esc dismisses, blur dismisses. Keep the textarea→input change compatible with
  `QuickEntry.test.tsx` (it fires Enter/Escape on the textbox; if it clicks "Send", keep a visually hidden
  submit button with that name or update the test deliberately). "Sending" → input disabled + 55% opacity.
- No "Not connected" state: we have no connection signal in this window (the no-target error covers it).

Mapping: `src/quick/QuickEntry.tsx`, `src/ambient/ambient.css` (`.quick*`), `src/ambient/strings.en.ts`
(+ `quick.sendTo`, `quick.target.current`; locales under `/locales` if desktop strings are mirrored there).

---------------------------------------------------------------------------------------------------

## 4. Onboarding (slugs onboarding, onboarding-other-providers, onboarding-api-key)

### 4.1 Frame

- Full-window overlay, background = chat-surface page (`#fefefe` / `#0d1015`), card centred both ways.
- Card: width 720 px (x 360–1079), radius 12 px, 1 px border (`#f6f6f6` light — almost invisible /
  `#141c27` dark), fill `#fcfcfc` / `#0f1621`, soft shadow fading over ~28 px below (≈0 8px 28px
  rgba(0,0,0,.05) light, darker in dark), overflow hidden. Height by content (picker 418 px; expanded and
  key screens grow to ≈810 px and the card stays centred; inner lists scroll at max 60vh / 42vh).
- Header: padding 20 px 20 px 4 px. Title 15 px, weight 600, tight tracking, text-primary (y≈272);
  description 13 px / 20 px line, text-tertiary (`#85878a` / `#828a93`), 4 px below. Keep OUR copy
  (`onboarding.title`, `onboarding.lead`).
- Body: padding 20 px, grid gap 12 px.
- Notice box (first block of the body): x 381–1058, 1 px border (`#cdd8ee` / `#233049`), fill
  (`#f0f2f8` / `#151d2c`), radius 6 px, padding 12 px 16 px, 14 px text muted (`#7f8288` / `#858d98`),
  ~20 px line height, 3 lines in the shot. Our content, our words: first run → a static explanation that
  no provider is set up yet and it can also be done later from Settings › Providers (new key
  `onboarding.notice.firstRun`); opened by a provider failure (`requests.ts` reason `provider-error`) →
  a different sentence (`onboarding.notice.providerError`); manual open from Settings → no box. Needs the
  reason to reach the reducer (add `reason` to the `reopen` event / FlowState — renderer-only change).
- Manual mode: 28 px ghost close ✕ at top-right (12 px from edges), as now.

### 4.2 Picker (slug onboarding; collapsed)

List container gap 8 px, padding 4 px (so rows sit 4 px inside the content edge: x 385–1056).
Row (all providers): full width, radius 6 px, padding 10 px 12 px, ≈64–71 px tall, hover wash; title
14 px weight 600 text-primary; subtitle 12 px / 20 px muted, 4 px below; trailing 16 px icon at right
(x≈1035): `ChevronRight` muted (hover text-primary, nudges 2 px right) or `SquareTerminal` for flows that
finish in a terminal.
Featured row (reference only): primary 6% wash (`#edf2fc` / `#121c2e`), radius 8 px, a 1 px gradient
edge running from blue (bottom-left) to orange (top-right), a 20 px logo, the name, a square UPPERCASE
"recommended" tag (primary fill, inverse 10 px 600 text, 0.16em tracking, dither dot) and a primary
chevron. DECISION for the builder (label it `W52-<x>-1`): our backend has no featured/recommended
provider — do NOT render the tag or a fake featured entry. Build the featured-row component anyway and
use it only when the backend marks a provider as featured (it does not today → never shown). Note it in
the Compare report as an intended difference.
Rows, in order, from our data (`GET /api/model/options` via `flow.ts` helpers):
1. every `deviceProviders()` entry (sign-in) — subtitle in our words ("Sign in in your browser; Indus
   connects by itself" style), chevron; click → `pick-device`.
2. "Use a model running on this computer" (keep our label; tests match /model running on this computer/)
   → `pick-endpoint local`.
3. Disclosure button, centred, 11 px weight 500 muted: "Other providers ⌄" / "Collapse ⌃" (chevron 14 px
   rotates). Open state lives in component state only (flow.ts must not read storage).
Footer row (gap 12 px, space-between): left "Choose later" text button (11 px, 500, muted; first run only,
keep the name — boot e2e looks for the text), right "Paste an API key" text button (keep the name; tests
use /Paste an API key/) → `pick-key` with the first key provider preselected.

### 4.3 Expanded (slug onboarding-other-providers)

Same rows, disclosure open: after row 2 add one row per `keyProviders()` entry (title = `label`,
subtitle = `description` or our generic "Paste a key from <label>"; click → `pick-key` with it
preselected) and "Use a custom OpenAI-compatible endpoint" (keep label) → `pick-endpoint custom`.
The list scrolls inside the card when taller than 60vh (the shot clips the last row behind the
disclosure); the disclosure + footer stay visible below the list.

### 4.4 API key form (slug onboarding-api-key)

Replaces the picker inside the same card (header + notice box stay):
- "‹ Back" centred small text button (11 px, muted, `ChevronLeft` 12 px) — keep name "Back".
- Provider grid: 2 columns, gap 8 px, padding 4 px, max 42vh then scrolls. Tile: radius 8 px, padding
  12 px, fill a hair lighter/darker than the card (`#fdfdfd` / `#0e121a`), transparent 1 px border; name
  14 px weight 500 text-primary; optional short line 12 px muted 4 px below (tiles with a short line
  ≈66 px tall, without ≈46 px; row pitch 74 / 54 px). Selected tile: 1 px primary border + 2 px primary-20%
  ring (`#cadbfd` / `#1b2b4d`). Hover: subtle wash. A provider already configured shows a 14 px `Check`
  muted at the tile's right.
  Tiles from our data: each `keyProviders()` entry (label; short = first clause of description, one line),
  then "Local / custom endpoint" style tile(s) for our two endpoint kinds, then "Another key" (our generic
  variable-name option, `onboarding.key.generic` shortened to ≤4 words).
  Picking a tile scrolls the entry area into view and focuses the input.
- Entry area (gap 8 px): description line 14 px muted (our words, per provider: description or
  generic) with, at right, an 11 px "↗ Get a key" link ONLY if the backend gives a docs URL — it does not
  today, so omit (do not hard-code provider URLs).
  Generic option → a mono "Variable name" input above the key input (our existing behaviour/validation).
  Key input: full width (x 381–1058), 30 px, 1 px stroke, radius 3 px, mono 12 px, placeholder "Paste API
  key"; focused border primary. It must stay a password field (`SecretInput` with eye toggle; accessible
  name "API key" — tests and e2e use it).
  Endpoint tiles → Base URL input (mono, placeholder `LOCAL_ENDPOINT_HINT` for local) + optional key input,
  and the main button reads "Test and continue" (keep name) — i.e. render our endpoint-form step in this
  same layout instead of a separate card.
- Error: 12 px destructive line under the inputs (role `alert`).
- Action row: right-aligned primary button 28 px, padding 0 10 px, radius 3 px, `KeyRound` 14 px + label;
  label stays "Save the key" (tests) or becomes "Connect" with the tests updated in the same change;
  disabled = 50% primary (`#7da6fc` / `#2c4d90`); busy → spinner icon.
- Below: "Choose later" centred text button (first run only).
- Remove the native provider `<select>`: e2e `surfaces.electron.test.ts` (lines ~288–299) queries
  `.onboarding-form select` and `.onboarding-form input[type="password"]` — update that e2e to click the
  provider tile (by its name) and keep the `.onboarding-form` class on the form so the rest still works.

### 4.5 Other steps (no reference shot here — style only, behaviour unchanged)

checking → one 14 px muted line with our Loader at 28 px (status role). device-starting/polling → step
title 14 px 600; code shown in a bordered mono block (1 px stroke, radius 6 px, `$`-less, copy button at
right); "Open the sign-in page" primary button + "Copy the code" secondary; waiting line with 12 px
spinner; Cancel as a text button. confirm-model → bare centred screen (no card chrome): small mono
uppercase caption ("Default model", 10 px, 0.2em tracking, muted), the model in mono 16 px, model input
(keep label "Model") and primary "Start chatting" (keep name). error → 14 px destructive line with a
14 px alert icon, collapsible "Details" in mono 11 px, actions right-aligned (keep names).

Mapping: `src/onboarding/Onboarding.tsx` (Picker, KeyForm, EndpointForm merged layout, header/notice),
`src/onboarding/onboarding.css` (rewritten on foundation tokens), `src/onboarding/strings.en.ts` (+notice,
subtitles, disclosure labels, "Other providers", "Collapse"), `src/onboarding/flow.ts` (reason on reopen;
nothing else). Tests: `Onboarding.test.tsx` (names kept as listed), `flow.test.ts` (reason field),
e2e `surfaces.electron.test.ts` (provider tile instead of select), `boot.electron.test.ts` ("Choose later").

---------------------------------------------------------------------------------------------------

## 5. Data available vs missing (summary)

| control                         | our source                                   | status |
|---------------------------------|----------------------------------------------|--------|
| file tree, refresh, rename, trash/delete, reveal, copy path, watch | `FileSource` (fs IPC / REST) | have |
| git tint in file tree           | `bridge.git.status` files                     | have — wire |
| show ignored files              | —                                            | missing → omit |
| collapse all                    | client state                                  | have |
| open folder (no workspace)      | no per-chat folder picker                     | missing → label only, follow-up |
| review scopes, stage/unstage/revert, diff, commit, commit & push, write message, create PR / ask agent | `bridge.git.*`, `draftMessage`, `askAgent` | have |
| HUD composer, transcript, reset, close, move, resize | `hud.*`, chat slots          | have |
| HUD auto-fade timing            | transcript store busy/messages               | have (renderer) |
| quick entry target picker (new / recent sessions) | `quick.submit({text})` only | missing → "Current chat" only, follow-up |
| quick entry connected state     | —                                            | missing → no state |
| onboarding featured/recommended provider, provider logos, docs "Get a key" URL | — | missing → omit |
| onboarding notice text          | our own static copy + request reason          | renderer-only |

---------------------------------------------------------------------------------------------------

## 6. Build order

1. Confirm W51-b tokens/components (panel label + dither dot, icon button 20 px, segmented control, split
   button, confirm dialog, toast, skeleton). Add the panel label + dither dot as a shared desktop
   component if W51-b did not (`src/components/` or the foundation's location).
2. Pane shell: zone chrome rule (1.2), tab style, seams, the right-group layout (1.6) + keep test ids.
3. Files pane (1.3) incl. git tint + context menu.
4. Review pane (1.4): header, scope row, rows, diff strip, ship bar, dialogs.
5. Terminal chrome + xterm theme (1.5).
6. Onboarding (4.x) incl. reducer `reason`, e2e update.
7. Quick entry (3).
8. HUD (2).
9. Tests: vitest for panes/onboarding/quick/hud, desktop e2e under xvfb, then re-capture both themes with
   the desktop kit (capture right-sidebar with Review+Files+Terminal, not quad; add an
   `onboarding-other-providers` capture: click "Other providers") and build the side-by-side PDF.

## 7. Compare checklist (tick each in both themes)

Right side
- [ ] Default window: no right panes; Alt+B / titlebar toggle opens Files on top + Terminal below, 236 px wide.
- [ ] Opening Review adds a 236 px column left of Files; seams 2 px, no dark rules, no shadows.
- [ ] Files/Review zones have no tab strip; Terminal zone has a 26 px strip with "TERMINAL" + 2 px primary underline.
- [ ] `#pane-tab-files` / `#pane-tab-review` still exist; e2e "panes" test passes.
- [ ] Panel labels: uppercase, 10.25 px, 0.16em, primary, dither dot (folder name; REVIEW).
- [ ] Files: 22 px rows, no chevrons, folder/file outline icons, 12 px secondary names, ellipsis.
- [ ] Files: refresh hover-reveal; collapse-all only when a folder is open; changed files tinted.
- [ ] Files: right-click menu with our actions; empty/no-folder/error states as labels.
- [ ] Review header: 5 icon buttons at 20 px pitch; segmented scope row 25 px on primary-tint track.
- [ ] Review rows 24 px with tinted square status glyphs, "+N −N" counts, hover stage/revert.
- [ ] Selected row wash; diff strip with mono path header, counts, stage, close; coloured diff lines.
- [ ] Ship bar: auto-grow message field with sparkle button, primary split "✓ Commit" + chevron, underlined agent PR link + PR icon.
- [ ] Revert confirm is a dialog; notices are toasts.
- [ ] Terminal body matches chat surface in light and dark (no black slab in light).

HUD
- [ ] No header row; composer bar on top, 41 px, radius 12, primary focus border.
- [ ] Bar right end: model, effort, mic, voice circle, reset, leave-HUD icons.
- [ ] Sheet inset 8 px, ~82% near-black in both themes, rounded bottom corners.
- [ ] Agent text white, user text gold without bubble; tool rows hidden.
- [ ] Sheet fades in on activity/focus and out after ~1.1 s idle; hold-to-move on bar background.

Quick entry
- [ ] Transparent window, one 12 px-radius card with faint primary border and small shadow, centred.
- [ ] "›" + single-line 15 px input; "Send to" + small select "Current chat".
- [ ] No Send button / hint footer; Enter sends, Esc and blur dismiss; error line red 11 px.

Onboarding
- [ ] 720 px card, 12 px radius, near-invisible border, soft shadow, centred on page surface.
- [ ] Header 15 px/600 title + 13 px tertiary description (our copy).
- [ ] Notice box (tinted, 1 px tinted border, 14 px muted text) on first run / provider error only.
- [ ] Rows: 14 px/600 title, 12 px muted subtitle, right chevron (terminal icon for terminal flows), hover wash.
- [ ] No fake "recommended" provider; featured component exists but unused.
- [ ] "Other providers ⌄" disclosure expands key providers + custom endpoint; "Collapse ⌃".
- [ ] Footer: "Choose later" left, "Paste an API key" right, 11 px muted text buttons.
- [ ] Key screen: "‹ Back", 2-column tile grid with primary ring on the selected tile.
- [ ] Description line, mono "Paste API key" password input with primary focus border.
- [ ] Right-aligned primary key button with key icon (50% when disabled); "Choose later" centred below.
- [ ] Endpoint options use the same layout ("Test and continue").
- [ ] Onboarding vitest + e2e (`surfaces`, `boot`) pass after the deliberate selector updates.
