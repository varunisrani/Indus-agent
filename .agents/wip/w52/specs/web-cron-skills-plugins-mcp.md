# W52 spec — web: Cron · Skills · Plugins · MCP

Status: spec (Read + Note done 2026-10-24). Builder works from this note with the reference closed.
Read: reference screenshots `compare/hermes/web/{cron,cron-create,cron-blueprints,skills,skills-toolsets,skill-editor,plugins,mcp,mcp-catalog,mcp-add-server}--{light,dark}.png`;
ours `compare/indus/web/{cron,cron-new-job,skills,skills-toolsets,skills-hub,plugins,mcp,mcp-test}--*.png`;
reference source read for structure/behaviour only (cron page + schedule builder + blueprints gallery, skills page + toolset
drawer + skill editor dialog, plugins page, MCP page). Our files: `apps/web/src/pages/{CronPage,SkillsPage,PluginsPage,McpPage}.tsx`,
`pages/cron/scheduleBuilder.ts`, `lib/api.ts`, `lib/restTypes.ts`, `ui/{controls,Dialog,Switch,CopyButton}.tsx`,
`shell/PageHeader.tsx`, backend `src/indus/web/routes/{cron,skills,plugins,mcp,system}.py`, `src/indus/cron/blueprints.py`.

Pixel samples were taken with a stdlib PNG reader (`$SP/w52-specs/tools-cspm/{px,ink,scan,bbox}.py`, 1440×900 shots).
"L" = light (blue preset), "D" = dark (teal preset). All hex below are measured, not guessed.

Wave-51 foundation (wt-w51a) had not landed when this was written. Wherever this note says *Button/primary*,
*Badge/success*, *Card*, *Switch*, *Segmented*, *PanelModal*, *Dialog*, *Select*, *Input*, *Label*, *H2 section heading*,
*inline code*, use the foundation component of that role from `docs/notes/ui-parity/web-foundation.md`. If a role is
missing there, build it in `apps/web/src/ui/` to the measurements below (and tell the integrator).

---

## 0. Shared look for this slice (tokens by role)

| role | L | D | notes |
|---|---|---|---|
| page bg / card bg | `#e8f2fd` | `#041c1c` | cards are NOT tinted; only a 1px border separates them |
| fg (titles, primary text, icons) | `#0053fd` | `#ffe6cb` | |
| muted text (descriptions, meta, H2) | `#2f73fd` | `#ccbda7` | |
| tertiary text (hints, sub-headings) | `#518bfd` | `#a79f8d` | plugin card subtitles, catalog hint |
| placeholder | `#71a0fc`–`#74a3fc` | `#828073`–`#858275` | |
| card / input border | `#c5dafd` | `#293935` | 1px, square corners everywhere (radius 0) |
| header bottom line / sidebar line | `#bad3fd` | `#36443f` | |
| tinted panel bg (skills filter panel) | `#e4effd` | `#081f1e` | border `#c2d8fd` / `#2c3c37` |
| modal surface (panel modals) | `#dfecfd` | `#0e2423` | border `#bdd4fd` / `#31403b` |
| input fill inside panel modal | `#e3eefd` | `#0a2020` | border `#c1d7fd` / `#2e3d39` |
| focus ring on inputs | 2px, outer `#9cbefd` inner `#aac7fd` | outer/inner `#47524a` | |
| success badge | bg `#d0efea` border `#a7eacb` text `#55de89` | bg `#0e382a` border `#1f6a44` text `#43ce78` | "scheduled", "active", "http" |
| secondary badge / chip | bg `#d5e6fd` border `#b6d0fd` text `#2069fd` | bg `#172b29` border `#3a4640` text `#dfccb5` | tags, tool chips, count pill, "community" |
| outline badge | transparent, border `#a2c2fc`, text `#4180fd` | border `#4f5850`, text `#bbaf9b` | "default", "auth: oauth", "inactive" |
| warning (pause icon, notes) | `#ffbd38` | `#ffbd38` | |
| destructive (trash, errors) | `#fb2c36` | `#fb2c36` | |
| primary button | bg `#0053fd`, text `#e8f2fd`, 1px darker bottom-right edge `#00297e` | bg `#ffe6cb`, text `#041c1c` | 28px tall, uppercase |
| outlined button | 1px `#0053fd` border, transparent, text fg | 1px `#ffe6cb` | 28px tall, icon prefix |
| inline code (endpoint URLs) | inverted: bg fg `#0053fd`/`#3577fd`, text page bg | bg `#ffe6cb`/`#d3c2ac`, text `#041c1c` | ~19px tall highlight |

Switch (skills rows, plugin install toggles): square track 34×19, 1px border `#89b0fd` (D `#696c61`), track fill
`#c5dafd` (D `#293935`); ON thumb 13×13 square, fg colour, 2px inset at right; OFF: track fill = page bg, border
`#bad3fd`, thumb 12×11 `#8bb2fd` (D `#686c61`) at left. No rounding.

Typography roles (cap heights measured → px size). Foundation picks the actual open fonts; match the role:
- *display* (page title in header "Cron", card titles "RUNTIME PROVIDER PLUGINS", dialog title "NEW SKILL", "ALL" card
  title): wide geometric sans, page title ~16px; card titles uppercase ~14px bold, slight tracking.
- *section heading H2* ("Scheduled Jobs (1)", "Your MCP servers (0)", "Catalog (65)"): sans ~14px bold, tracking ≈0.15em,
  muted colour, 16px icon before it, gap 8.
- *serif label* (form labels "PROFILE", "NAME (OPTIONAL)", segmented "Jobs/Blueprints", panel items "ALL (53)",
  "FILTERS", "CATEGORIES", "PLUGIN CATALOG", modal titles "NEW CRON JOB", "ADD MCP SERVER", blueprint titles/descr.,
  hints under fields): a book serif. Labels uppercase ~10px, tracking ≈0.06em, fg; panel items uppercase ~10px tracking
  0.08em; blueprint title 15px sentence case; blueprint description 12px muted.
- *mono* (inputs, selects, schedule "Daily at 08:00", skill names, tool chips, category list, sidebar-style lists):
  monospace ~13px in inputs, 11–12px in meta; tool chips 11px with wide tracking ≈0.15em.
- *button text*: bold mono, ~11px, tracking ≈0.12em; primary buttons uppercase ("CREATE", "INSTALL", "ADD SERVER");
  outlined buttons sentence case ("Learn a skill", "Configure", "Set up", "Browse catalog").
- *badge text*: condensed tall face, ~12px, tracking ≈0.1em, lowercase ("scheduled", "active", "http", "auth: oauth").
- *body/sans*: card titles in lists ~13–14px medium ("Morning digest", "airtable"); descriptions 11–12px muted.

Page frame (all four pages): header 52px + 1px line (title left at x=262; header-end slot right-aligned, 22px from
the right edge). Content area: left padding 22–23px (x 263), right padding 32px (content right edge x 1407),
top padding 22px (first element at y 75). Vertical rhythm between page blocks 24px; inside a block 12px.

Two modal styles (both exist in this slice):
1. **PanelModal** (cron create/edit, MCP add, toolset configure): full-screen wash = page bg at ~85% over the content;
   the sidebar stays crisp. Panel: surface colour (`#dfecfd`/`#0e2423`), 1px border, very soft large shadow, no radius.
   Header 54px with title (serif, uppercase, ~14px) at padding 20px, bottom 1px line; close "×" 12px icon top-right
   (8px inset). Body padding 20px, field gap 16px, label→control gap 8px. Footer action right-aligned (primary, small).
2. **Dialog** (skill editor, "Learn a skill"): dark scrim over everything incl. sidebar (L `#565c65`, D `#0a1211`,
   content behind is blurred/greyed). Dialog surface = page bg, 1px border (`#bdd1f3` / `#263632`). Header 16px padding:
   display title uppercase ~14px; one-line description below in serif 11px muted (`#5d93fd` / `#9a9585`). In the
   reference, dialog title/label/body ink is plain near-black `#170d02` (L) / white (D) rather than the theme fg —
   match it if the foundation Dialog does so; otherwise use fg (decision W52-c-1).

---

## 1. `cron` — Cron jobs list (Jobs tab)

Layout (1440×900):
- Header: title "Cron" (display). Header end: primary button **CREATE** (70×28 at x 1348–1417, y 12–39).
- y 75–102: **Segmented** full content width (x 263–1407, 28px tall, 1px border `#c5d9fd`/`#283834`). Two segments
  left-aligned: "Jobs" (active: filled fg, text page-bg-ish `#c5dafd`/`#35433e`, segment 44px wide) and "Blueprints"
  (inactive: text muted, transparent). Serif ~11px sentence case. The rest of the bar is empty.
- y ~125–175 row: left **H2** with clock icon: "Scheduled Jobs (N)"; right: field group 220px wide (x 1188–1407):
  label "PROFILE" (serif label) above a **Select** 34px tall showing "All profiles" (mono 13px) with chevron.
  H2 baseline aligns with the select bottom (items-end).
- y 186+: job **Card**s stacked, gap 12. Card: full width, 1px border, padding 15px 15px. Card height with 3 text
  lines = 94px.
  - Line 1 (flex-wrap, gap 8): title (sans 13–14px medium, fg) · state badge (success for scheduled/enabled, warning
    for paused, destructive for error/completed) · outline badge with profile name ("default") · optional outline
    badges: deliver target (if not local), skills ("name" or "N skills"), last result when not ok.
  - Line 2: prompt preview, muted 11–12px, single line truncated (only when the job has a name).
  - Line 3 (gap 16, muted 11–12px): schedule in mono ("Daily at 08:00") · "repeat: forever" · "Last: —" ·
    "Next: <locale date time>" (when overdue, render that item in warning colour with "Overdue since:").
  - Error lines (destructive 11px) under line 3 when the job has a last error.
  - Right side, top-aligned: 4 ghost icon buttons 32px apart (x≈1281/1313/1345/1377): Pause (two bars, **warning**
    colour; when paused → Play icon in success colour), Run now (lightning, fg; spinner while running), Edit (pencil,
    fg), Delete (trash, destructive). Icons 14px.
- Empty: centered card, muted text "No scheduled jobs yet" (ours) + small primary CREATE button.
- Loading: centered spinner, py 96. Load error: the foundation's load-error notice with Retry above the list.

Behaviour: Segmented switches Jobs/Blueprints in place (no route change). Profile filter changes the job list (see
mapping). Icon buttons keep tooltips + aria-labels. Delete opens the foundation confirm dialog.

D: same geometry; badge "scheduled" `#0e382a`/`#43ce78`; icon colours: pause `#ffbd38`, zap/pencil `#ffe6cb`, trash
`#fb2c36`.

### Ours today vs reference
Ours: header "New job" teal pill; cards with rounded corners, badge + raw schedule at right, text buttons
Pause/Run now/Edit/Delete; no segmented tabs, no H2, no profile filter, no "repeat"/"Next" absolute date.

## 2. `cron-create` — New cron job (PanelModal)

- PanelModal 720px wide (x 360–1079), top at y 45, max height 90vh (810px); body scrolls; header "NEW CRON JOB".
- Body fields, in order (labels serif uppercase, controls full width, 34px tall, mono 13px, fill `#e3eefd`/`#0a2020`):
  1. PROFILE — Select (profile names; default = the page filter's profile, or "default" when "All").
  2. NAME (OPTIONAL) — Input, placeholder "e.g. Daily summary"; autofocus (shows the 2px focus ring).
  3. PROMPT — textarea 80px min, mono 13px, placeholder in our words, resize handle.
  4. SCHEDULE — Select of modes: "Every interval", "Daily", "Weekly", "Monthly", "Once", "Custom" (labels may be ours).
     - interval → two columns 1fr / 1.4fr, gap 12: EVERY (number input, default 30) + UNIT (Select minutes/hours/days).
     - daily → TIME OF DAY (native time input). weekly → day toggle buttons (small, mono uppercase, outlined when off,
       primary when on, min 40px wide) + TIME OF DAY. monthly → DAY OF MONTH + TIME OF DAY (2 columns).
       once → native datetime-local. custom → mono text input + one-line hint.
     - Preview line under the builder (serif 10px muted): "Sends as:" then the schedule string in mono fg ("every 30m").
  5. DELIVER TO — Select ("Local" + platforms; unavailable ones suffixed with a short "set up first" hint).
     When only local exists, a muted 11px note under it (our words: set up a channel to deliver elsewhere).
  6. SKILLS (OPTIONAL) — bordered box, max-height 144px, scrolls; one row per skill: native checkbox + mono 12px
     name; row hover tint. Below: serif 10px hint (our words: chosen skills load before the prompt runs).
  7. ADVANCED FIELDS — `<details>` box (1px border, 12px padding, open) with uppercase muted summary. See mapping:
     we render a placeholder inside.
  8. Footer: right-aligned small primary "CREATE" (spinner + "Creating…" while busy).
- Edit variant: title "EDIT JOB", no Profile field, footer left shows the job id in mono muted, right "SAVE CHANGES".
- Validation: prompt and schedule required → toast/inline error; close on Esc, on backdrop click, on ×.

## 3. `cron-blueprints` — Blueprints tab

- Same header/segmented (Blueprints active). No H2/profile row.
- Grid 2 columns (x 263–828 and 841–1407; column gap 12, row gap 12). Cards stretch to the tallest in the row.
- Card: 1px border, padding 16. Top row: left block — wand icon (16px, muted) + title (serif 15px, fg); description
  (serif 12px muted, wraps, 6px below); tag chips row (secondary badges, condensed text, 24px tall, gap 4, 8px below).
  Right: small primary "Set up" button (sentence case, 70×28; reference wraps to two lines when space is tight — we keep
  one line, decision W52-c-2).
- Clicking Set up expands the card in place: top border line, one field per slot (label + input/select/time), help
  text under non-text fields, error line (destructive), and a primary "Schedule it" button with a clock icon; the
  Set up button turns into a ghost "Cancel".
- States: loading (spinner + "Loading blueprints…"), error (destructive one-liner), empty ("No blueprints yet").

## 4. `skills` — Skills (All)

- Header: title "Skills" + after-title muted 12px "53/53 enabled" (enabled/total). Header end: search input 300×30
  (x 1118–1417), 1px border, search icon 12px inside left, placeholder "Search..." (mono, placeholder colour);
  clear "×" appears when non-empty.
- Body: two columns, gap 16, top-aligned.
  - **Filter panel** 210px wide (x 263–472), sticky, tinted panel bg + border.
    - Header row 31px: filter icon 12px + "FILTERS" (serif uppercase 10px, tertiary), bottom line.
    - Items (padding 8, gap 4): "ALL (N)" (package icon), "TOOLSETS (N)" (wrench icon), "BROWSE HUB" (search icon).
      Item 27px tall, inset 8px; serif uppercase ~10px tracking 0.08em; active = solid fg bg (`#1763fd` L /
      `#e6d1b9` D) with page-coloured text (`#d0e2fd` / `#263733`).
    - When ALL is active and there is no search: a top-bordered "CATEGORIES" block: heading (serif uppercase, tertiary)
      then rows 23px tall: category name (mono 11px, muted) left, count right (tabular, tertiary). Clicking toggles that
      category filter (active row uses the list-item active style). List scrolls if taller than the viewport.
  - **Skills card** (x 488–1407): header 50px with bottom line: package icon + "ALL" (display, ~14px, fg; or the
    category name when one is chosen) left; right group gap 8: count pill (secondary badge "53 skills", 57×24),
    outlined "Learn a skill" (sparkle icon), outlined "New skill" (plus icon), both 28px tall.
  - Rows (padding 12/10, gap 4, hover tint): Switch at left (top-aligned) → name (mono 14px, fg; muted when disabled)
    → description (sans 12px muted, max 2 lines). Hover-only pencil at right (see mapping). Row pitch ≈61px.
- Search: while the header search has text, the filter panel shows no item active and the card becomes "Skills" with a
  search icon and a "N results" pill; empty → centered muted "No skills match".
- Empty (no skills at all): centered text + primary "Browse hub" + outlined "New skill".

## 5. `skills-toolsets` — Toolsets

- Same header and filter panel (TOOLSETS active, no categories block).
- Right area: grid 3 columns (x 488–786, 798–1096, 1108–1406; gap 12), cards stretch per row.
- Card: 1px border, padding 16/16. Row: toolset icon 20px muted (globe for web, eye for vision, code brackets for code,
  wrench default…) + column: label (sans 14px medium, fg) + status badge (success "active" / outline "inactive");
  tools summary line (sans 12px muted); tool chips (secondary badges, mono 11px wide-tracked, 24px tall, wrap, gap 4);
  outlined "Configure" button with wrench icon (28px) 12px below.
- Configure opens a PanelModal "drawer" (672px wide): header = label (display) + Active/Inactive badge + description;
  body = provider settings (see mapping → placeholder).
- Empty after search: card "No toolsets match".

## 6. `skill-editor` — New skill (Dialog)

- Dialog 720px wide (x 360–1079), vertically centred (y 195–704). Scrim per §0.
- Header: "NEW SKILL" (display uppercase ~14px) + one-line serif description (our words: frontmatter + markdown;
  becomes usable by the agent and by cron jobs). × at top-right.
- Two-column row (gap 12): NAME (placeholder "my-skill", autofocus ring) | CATEGORY (OPTIONAL) (placeholder "devops").
  In the reference these inputs run flush to the dialog's side edges (no side padding) — same for the textarea.
- SKILL.MD label, then a mono 12px textarea min 320px / max 55vh, pre-filled with a short frontmatter template
  (name, description, a heading, one line of guidance — write our own template text).
- Error line (destructive, pre-wrap) when save fails.
- Footer right: ghost "Cancel" (bold mono) + small primary "CREATE SKILL" (uppercase). Edit variant: title
  "EDIT SKILL: <name>", no name/category row, button "SAVE CHANGES".

## 7. `plugins` — Plugins

- Header: title "Plugins" + after-title ghost icon button (refresh arrows, 16px) — spinner while busy.
- Body column, gap 32:
  1. **Card "RUNTIME PROVIDER PLUGINS"** (x 263–1407, y 75–315). Card header 70px: display title uppercase ~14px fg +
     one-line tertiary subtitle (sans 11px, tracking 0.08em); 1px line. Body grid: left 1.35fr | right 0.65fr (min 260),
     gap 24 (left x 279–1014, right x 1037–1391).
     - Left: label "MEMORY PROVIDER" + success badge "active" (when the selection is the active one); Select
       "(built-in / default)" 34px; muted 12px note (our words: the built-in memory files are used); small primary
       "SAVE MEMORY PROVIDER".
     - Right: label "CONTEXT ENGINE"; Select; small primary "SAVE CONTEXT ENGINE".
  2. **Card "INSTALL FROM GITHUB / GIT URL"** (y 346–636): header title + one-line tertiary hint (our words: accepted
     source forms). Body: label "GIT URL OR OWNER/REPO" + full-width mono input (placeholder listing the forms);
     row (gap 32) of two switches with labels (sans 11px secondary): "Force reinstall …" and "Enable after install";
     small primary "INSTALL"; two tertiary hint lines (rescan hint; which plugins can be removed — our words).
  3. **"PLUGIN CATALOG"** section (no card): serif uppercase heading (secondary), one-line tertiary hint, search input
     448px wide ("Search catalog..."), then catalog cards: name (bold 15px) + tier badge (secondary "community" /
     success "official") on the left, ghost "Install" (bold mono) right; description (12px secondary, wraps);
     capability chips (mono 11px boxes); footer meta (maintainer · short sha ↗ · docs ↗ · platforms).
  4. **Installed plugins** section: heading (serif uppercase), then one card per plugin: name (bold) + outline badges
     "source: X", "vX.Y" + status badge (success "enabled" / destructive "disabled"); actions right: ghost
     "Disable"/"Enable", optional "Open tab", "Update", eye toggle, trash; description under (12px secondary).
     Empty → tertiary "No results". Loading → spinner + "Loading…".

## 8. `mcp` — MCP servers

- Header: title "MCP"; header end: primary "ADD SERVER" (x 1316–1417).
- Section 1: H2 with server icon "Your MCP servers (N)" at y 78. Optional warning-colour note "changes apply after a
  restart" (our words) after a toggle.
  - Empty: card (y 108–225) centred muted 13px sentence (our words: no servers yet; they add tools; pick one from the
    catalog or use Add server) + small primary "Browse catalog" with package icon (scrolls smoothly to the catalog).
  - Server card: name (sans 13–14px medium) + transport badge (http = success, stdio = warning) + outline "auth: …"
    + outline "disabled"; second line muted 12px: mono URL or command line (truncate) · "N env vars". Test result line
    (12px): success "Tools: a, b, c" / "Connected, no tools", or destructive error. Card dims to 60% when disabled.
    Actions right (gap 4): ghost "Authenticate" with key icon (oauth servers only), ghost "Disable"/"Enable" with power
    icon (success colour when enabled), icon Test (lightning; spinner while testing), icon Delete (trash, destructive).
- Section 2 (gap 24): H2 with package icon "Catalog (N)" (anchor for Browse catalog) + muted 12px one-liner (our words).
  - Catalog card (stacked, gap 12, ~115px tall): line 1: name (fg medium) + transport badge + outline "auth: <type>" +
    link "source ↗" (12px fg, underlined) + "Installed" success badge when present; description muted 12px;
    "Endpoint:" + inline code URL (inverted highlight) for http, or "Runs:" + inline code command for stdio;
    `<details>` "Setup notes" (▶ disclosure, muted 12px). Right: small primary "INSTALL" (or success "Installed").
  - Empty: card "No catalog entries available."

## 9. `mcp-catalog` — same page scrolled to "Catalog (N)"

Capture = after clicking "Browse catalog": Catalog H2 at the top of the content (y≈63), cards below. No extra UI.

## 10. `mcp-add-server` — Add MCP server (PanelModal)

- PanelModal 480px wide (x 480–959, y 255–644), header "ADD MCP SERVER".
- Fields (gap 16): NAME (placeholder "my-server", autofocus ring) · TRANSPORT Select ("HTTP/SSE" default, "stdio") ·
  http: URL (placeholder "https://example.com/mcp") + AUTHENTICATION Select (None / Bearer token / OAuth) with a
  muted hint under it for token/OAuth · stdio: COMMAND, ARGS, ENVIRONMENT (KEY=VALUE PER LINE) textarea 80px.
- Footer right: small primary "ADD" ("Adding…" while busy). Errors → toast (or inline alert, see mapping).

---

## 11. Mapping to our code

Files that change: `apps/web/src/pages/CronPage.tsx`, `pages/cron/scheduleBuilder.ts` (+ new `pages/cron/Blueprints.tsx`
suggested), `pages/SkillsPage.tsx` (+ new `pages/skills/{SkillEditorDialog,ToolsetDrawer,LearnDialog}.tsx`),
`pages/PluginsPage.tsx`, `pages/McpPage.tsx` (+ `pages/mcp/AddServerModal.tsx`), `lib/restTypes.ts`, `lib/api.ts`
(small client additions only), and the four `*.test.tsx`. Shell: needs a header **after-title slot** (Skills count,
Plugins refresh). `shell/PageHeader.tsx` only has `HeaderActions` (end); if the foundation did not add one, add
`HeaderAfterTitle` the same portal way.

No backend change in this slice. Gaps below are listed for a later backend wave.

### Cron
| control | data / API | action |
|---|---|---|
| CREATE (header) | — | opens create PanelModal (keep accessible name; tests click "New job" → update to "Create") |
| Jobs/Blueprints segmented | — | local state |
| Profile filter | `api.profiles()` + per-profile `GET /api/cron/jobs?profile=` | Options: "All profiles" + each profile. "All" = fan-out one request per profile and merge (tag each row with its profile). A single profile = that request. Fallback if time-boxed: bind to the global ProfileScope and drop "All" (decision W52-c-3). |
| profile outline badge | the row's profile (from the fan-out) or current scope | show |
| state badge | `job.state` (scheduled/paused/running/completed/error) | tones per §1 (running → fg/outline) |
| skills badge | `job.skills` — returned by backend `job_out`, missing in TS `CronJob` | add `skills: string[]` to `CronJob` |
| "repeat: …" | `job.repeat {times, completed}` — returned, missing in TS | add to type; forever when times null |
| Last / Next | `last_run_at`, `next_run_at` | absolute locale date-time ("—" when null); overdue when next < now and state scheduled |
| error lines | `last_error` (returned, missing in TS), `last_status` | destructive line |
| Pause/Resume/Run now/Edit/Delete icons | `api.cron.pause/resume/trigger/remove` | keep aria-labels "Pause", "Resume", "Run now", "Edit", "Delete" so `CronPage.test.tsx` row tests still pass; add `?profile=` when the row's profile differs from the scope |
| Modal Profile select | `api.profiles()`; `api.cron.create(input, profile?)` via existing `forProfile` | small client change |
| Modal schedule builder | replace "helper + Use" with mode select that writes the schedule directly; build strings in OUR grammar (`every 30m`, `daily at 09:00`, `weekdays at …`, cron line). Weekly/monthly → emit a 5-field cron line (our grammar accepts cron lines). Once → our existing one-shot form `in <N><m|h|d>` (today's "Once, after…" kind) with EVERY/UNIT fields instead of a datetime picker; use an absolute date-time only if our grammar accepts one (decision W52-c-4). Edit: parse the job's schedule back into a mode, else Custom with the raw text. | `scheduleBuilder.ts` rewrite + tests |
| Deliver to | `api.cron.targets()` (`available` flag) | unavailable → label suffix instead of `disabled` |
| Skills checklist | `api.skills.list()` names; backend `JobCreate.skills` exists | add `skills?: string[]` to `CronJobInput`; send it (create) — update body accepts? backend `JobUpdate` has no skills → on edit show the list read-only-checked, or omit on edit (decision W52-c-5) |
| Advanced fields | nothing in our job model (provider/model/script/workdir/…) | placeholder: open details box with one muted line in our words ("per-job model and script settings are not available yet") |
| Blueprints tab | `GET /api/cron/blueprints` exists but returns skill rows `{skill, description, schedule, prompt}`, not the starter catalogue nor slot forms; no instantiate route | Card per row: title = skill, description, one tag chip = skill name; "Set up" opens the **create modal prefilled** (prompt, schedule→Custom) instead of the inline slot form. Empty list → empty state. Gap: backend should expose `list_blueprints` + `form()` + a fill/instantiate route (then switch to the inline form of §3). Add `api.cron.blueprints()` + type. |

Tests: `CronPage.test.tsx` pins "New job", "Kind"/"Amount"/"Unit"/"Use", "Schedule" as a text input, "Create", "Save".
Update to the new labels deliberately ("Create", "Every"/"Unit", custom schedule input, "Save changes"). The
`scheduleBuilder` unit expectations (strings) stay in our grammar.

### Skills
| control | data / API | action |
|---|---|---|
| "N/M enabled" | `api.skills.list()` | after-title slot |
| header search | client filter over name/description/category | |
| filter panel ALL/TOOLSETS/BROWSE HUB | views `installed`/`toolsets`/`hub` | replaces `Segmented`; tests click "Toolsets"/"Hub" → update to `/Toolsets/` and "Browse hub" |
| categories | `SkillInfo.category` (string; "" → "General") | client group + counts; prettify (split on -_/ and capitalise) |
| skill row switch | `api.skills.toggle` | keep switch accessible name "Enable <name>" (test) |
| hover pencil (edit SKILL.md) | no read/write content endpoint | omit (decision W52-c-6) |
| New skill → editor dialog | no create endpoint | show the dialog fully (slug `skill-editor`); "CREATE SKILL" saves nothing: replace with a placeholder flow — copy-to-clipboard of the composed SKILL.md (existing `CopyButton`) + muted note where to put it (`<home>/skills/<name>/SKILL.md`). Keep the primary button visually; label stays "Create skill" but it performs the copy (decision W52-c-7). Gap: skill create/update endpoint. |
| Learn a skill | no chat prefill (`ChatHost` reads only `?resume=`) | dialog with three fields; submit disabled + one-line note (placeholder). Gap: chat `?prompt=` prefill. |
| Toolsets grid | `api.skills.toolsets()` (name,label,description,enabled,tools) — no `configured` | cards per §5; no "setup needed" line |
| Configure drawer | no toolset config/toggle endpoint | PanelModal with header (label, badge, description) + muted placeholder body (our words: provider keys live on the Keys page) + link to `/env` |
| Browse hub | existing `HubView` (search, verdict badges, install/update/uninstall) | restyle only: search field + outlined "Search" inside the right card; result cards in §0 badge styles; keep `aria-label="Search the skills hub"`, testids `hub-<id>`, "Blocked by the scanner", alerts (tests) |

### Plugins
| control | data / API | action |
|---|---|---|
| refresh icon | — | refetch `["plugins"]` (and memory/config queries) |
| Memory provider | `api.system.memory()` → `{active, providers, builtin_files}`; `PUT /api/memory/provider` (exists in client, used in `pages/system/CredentialSections.tsx`) | wire; "(built-in / default)" = "" ; "active" badge when selection === active |
| Context engine | config key `context.engine` (default "builtin"), via `api.config.get()` / `api.config.put()` | options: "builtin" + the current value if different. Gap: list of installed engines. |
| Install card | `api.plugins.install({url, sha, force})` — backend requires a sha, accepts owner/repo and `force` | Keep our **COMMIT SHA** field (pinning is our rule) under the identifier, same input style. Force switch → `force`. "Enable after install" → on success call `api.plugins.setEnabled(name, true)` if the response names the plugin, else refetch and leave it. Relax `installProblem()` to accept owner/repo like the backend (decision W52-c-8; keep the 40-char test). |
| Plugin catalog | none | heading + hint + search + empty text (placeholder). Gap: curated plugin catalogue. |
| Installed list | `api.plugins.list()` (name, version, description, enabled, source, sha, state, kind, pinned) | card per §7.4: badges source / v / state; ghost Enable/Disable (replaces `Switch`; update test that clicks switch "Enable weather" → button "Enable"); no update/remove/visibility (no endpoints) |

### MCP
| control | data / API | action |
|---|---|---|
| ADD SERVER → modal | `api.mcp.add` (name, command/args/env or url/auth) | move the current inline `AddServer` form into the PanelModal; default transport **http** (tests that add stdio must pick "stdio" first). Auth "Bearer token" = our `header` mode: no token field, muted hint to set the key on the Keys page (backend refuses literal secrets). Keep our validation messages as inline alert (tests read role=alert) |
| server rows | `api.mcp.servers()` | badges per §8; env → "N env vars" (test pins `FILES_TOKEN=****` → update, or show keys in a `title`) |
| Enable/Disable | `api.mcp.setEnabled` | ghost button with power icon (tests click switch "Enable remote" → update) + restart note |
| Test | `api.mcp.test` | icon button aria-label "Test" (keeps test name); result line "Tools: …" — tests read a "Server tools" list + "Connected. 2 tools" → update, or keep a visually-hidden list (decision W52-c-9) |
| Delete | `api.mcp.remove` + `ConfirmDialog` | trash icon, aria-label "Remove" (keeps tests) |
| Authenticate | backend has `POST /api/mcp/servers/{name}/auth` + `GET /api/mcp/oauth/flows/{id}`; client has none | optional: add `api.mcp.auth/flow` and the button for `auth === "oauth"`; else omit (decision W52-c-10) |
| Catalog | none | "Catalog (0)" H2 + one-liner + empty card (placeholder); Browse catalog scrolls to it. Gap: MCP catalogue. |

UI copy rule: no product names of the reference anywhere ("…-approved", "Hermes will use…", "~/.hermes/…"). Write
every sentence longer than 4 words in our words; say "Indus Agent" where a product name is needed.

---

## 12. Build order

1. Shared bits used by all four pages (only if the foundation lacks them): after-title header slot, PanelModal, square
   Switch, badge tones (success/secondary/outline/warning/destructive), inline-code style, H2 section heading, icon
   ghost button (32px, 14px icon).
2. MCP (smallest, data complete): page sections + empty state + Add server modal + catalog placeholder. Update tests.
3. Cron Jobs tab + create/edit modal + new schedule builder (+ `scheduleBuilder.ts` tests), then profile filter, then
   Blueprints tab.
4. Skills: filter panel + All card + rows + header search/count; Toolsets grid + Configure drawer; skill editor dialog;
   Learn dialog; restyle hub.
5. Plugins: runtime providers card (memory + context engine), install card, catalog placeholder, installed list.
6. Both themes pass in Compare; tsc, biome, vitest; re-capture with the kit (slugs below) and build the PDF.

Capture notes for Compare (our kit): `cron` (Jobs), `cron-create` (click Create; leave name focused), `cron-blueprints`
(click Blueprints), `skills`, `skills-toolsets`, `skill-editor` (New skill), `plugins`, `mcp` (to match the reference
"0 servers" shot, also take a seeded one), `mcp-catalog` (click Browse catalog), `mcp-add-server` (click Add server).
Seed data so the shapes match: one cron job named "Morning digest", daily 08:00, prompt one sentence, profile default.

## 13. Compare checklist (tick per slug, both themes)

Common
- [ ] Header 52px + line; title in display face; actions right; content padding 23/32/22.
- [ ] All corners square; borders 1px in the measured colours; no card tint (except filter panel / modals).
- [ ] Badges: condensed lowercase text, 24px tall, success/secondary/outline colours as measured.
- [ ] Buttons 28px; primary uppercase bold mono with dark 1px bottom-right edge (L).

cron
- [ ] Segmented bar full width 28px, "Jobs" active filled.
- [ ] H2 "Scheduled Jobs (N)" with clock icon left; PROFILE select 220px right, bottoms aligned.
- [ ] Job card: title + state badge + profile badge; prompt line; mono schedule · repeat · Last · Next.
- [ ] Four icon actions right: pause (amber), lightning, pencil, trash (red).
- [ ] Empty and load-error states.

cron-create
- [ ] PanelModal 720px, wash over content, sidebar crisp; header "NEW CRON JOB" + ×.
- [ ] Field order Profile, Name, Prompt, Schedule(mode + Every/Unit), "Sends as:" preview, Deliver to, Skills list, Advanced.
- [ ] Inputs mono 13px, 34px, tinted fill; name has focus ring.
- [ ] Create works for interval/daily/custom; edit variant shows id + Save changes.

cron-blueprints
- [ ] 2-column grid, gap 12; wand icon + serif title; serif description; tag chips; "Set up" primary right.
- [ ] Set up prefills the create modal (our data path).

skills
- [ ] "N/M enabled" after title; search box 300px in header.
- [ ] Filter panel 210px tinted: FILTERS header, ALL/TOOLSETS/BROWSE HUB (active filled), CATEGORIES with counts.
- [ ] Card header: icon + ALL, "N skills" pill, outlined Learn a skill + New skill.
- [ ] Rows: square switch left, mono name, muted description.

skills-toolsets
- [ ] 3-column grid of cards; icon + label + active/inactive badge; summary; mono chips; Configure outlined.
- [ ] Configure drawer opens (placeholder body).

skill-editor
- [ ] Dark scrim over whole app; dialog 720px; NEW SKILL + description; Name | Category row; SKILL.MD textarea with
      template; Cancel + CREATE SKILL footer.

plugins
- [ ] Refresh icon after title.
- [ ] RUNTIME PROVIDER PLUGINS card: memory (select + active badge + note + save) | context engine (select + save).
- [ ] INSTALL FROM GITHUB / GIT URL card: identifier, (sha), two switches, INSTALL, two hints.
- [ ] PLUGIN CATALOG heading, hint, search (placeholder data), installed list cards.

mcp / mcp-catalog
- [ ] ADD SERVER primary in header.
- [ ] "Your MCP servers (N)" H2; empty card with centred text + Browse catalog; server cards with badges + actions.
- [ ] "Catalog (N)" H2 + one-liner + empty card; Browse catalog scrolls there.

mcp-add-server
- [ ] PanelModal 480px; NAME (focus ring), TRANSPORT HTTP/SSE, URL, AUTHENTICATION; ADD bottom-right.
- [ ] stdio shows COMMAND / ARGS / ENVIRONMENT.

## 14. Decisions to record (builder confirms or changes)
W52-c-1 dialog ink colour · W52-c-2 one-line "Set up" · W52-c-3 cron "All profiles" by fan-out (fallback: global scope)
· W52-c-4 "Once" mode only if our grammar accepts it · W52-c-5 skills on edit · W52-c-6 no edit pencil · W52-c-7 skill
editor copies instead of saving · W52-c-8 install accepts owner/repo · W52-c-9 MCP test result rendering ·
W52-c-10 MCP Authenticate button.

## 15. Backend gaps found (not for this slice)
MCP catalogue endpoint; plugin catalogue; skill create/read/update content; toolset toggle/provider config; cron
blueprints route returning the starter catalogue with slot forms + an instantiate route; cron job update with skills;
per-job model/script fields; chat prompt prefill (`/chat?prompt=`); context-engine option list.
