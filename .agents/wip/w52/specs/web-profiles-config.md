# W52 spec: web profiles, profile builder and config (slice web-profiles-config)

Status: spec, ready for the wave-52 page builder. Written 2026-10-24 from the Read and Note phase. It contains no reference code, CSS or class names.
Read: reference screenshots `compare/hermes/web/{profiles,profile-actions,profile-builder,config,config-yaml}--{light,dark}.png`;
reference sources (read only) `web/src/pages/ProfilesPage.tsx`, `ProfileBuilderPage.tsx`, `ConfigPage.tsx`,
`components/AutoField.tsx`; our `apps/web/src/pages/{ProfilesPage,ProfileBuilderPage,ConfigPage}.tsx` (+ tests),
`pages/profiles/profileName.ts`, `lib/api.ts`, `lib/restTypes.ts`, `shell/PageHeader.tsx`, `shell/routes.ts`,
backend `src/indus/web/routes/{profiles,config_env,config_view,skills,models}.py`.
Our current shots: `compare/indus/web/{profiles,profile-edit,profile-builder,profile-builder-model,profile-builder-review,config,config-effective}`.

All px values were measured on 1440x900 @1x shots. The reference root font size is effectively **15px**. For example, its 14rem
filter column measures 210px. So "text-xs" there is about 11.25px and "text-sm" about 13px. Where this note gives px, use the px.
Use the role names from the wave-51 foundation (`docs/notes/ui-parity/web-foundation.md`). The hex values below were sampled
from the reference PNGs. Use them to check the foundation tokens, and do not hard-code them in pages.

## 0. Shared palette as sampled (for checking tokens)

| role | dark ("teal") | light ("blue") |
|---|---|---|
| page bg | #041c1c | #e8f2fd |
| fg (primary text, primary button fill) | #ffe6cb | #0053fd |
| fg secondary (~80%: h2, meta lines, descriptions) | #ccbda7 | #2f73fd |
| fg tertiary (~65%: key paths, counts) | #a79f8d | #518bfd |
| fg faint (italic "no description") | #7c7c6f | #79a6fd |
| fg disabled / placeholder / inactive step | #686c61 / #828073 | #8bb1fd / #73a2fc |
| border (cards, inputs) | #293935 | #c5dafd |
| border strong (header bottom, sidebar edge) | #36443f | #bad3fd |
| card / popover / code strip bg | #0e2423 | #dfecfd |
| side panel bg (config filter column) | #081f1e | #e4effd |
| side panel border | #2c3c37 | #c2d8fd |
| list item active bg | #21322f | #ccdefc |
| input bg | #041b1b | #e8f2fd (same as page) |
| muted fill (disabled button, switch track) | #293935 | #c5dafd |
| secondary badge bg / border / text | #172b29 / #3a4640 / #ead5bc | #d5e6fd / #b6d0fd / #1560fd |
| success badge bg / border / text | #0e382a / #1f6a44 / #44cf77 | #d0efea / #a7eacb / #57e089 |
| success icon (check) | green, same as success text | same |
| primary button bevel: 1px top / 1px bottom | #fff3e5 / #7f7365 | #80a9fe / #00297e |
| menu panel bg / border | #0e2423 / #31403b + soft drop shadow | #dfecfd / #bdd4fd + soft shadow |

Fonts, by role (the foundation picks the open-font stand-ins):
- **display-expanded**: a wide geometric grotesk. Used for page titles, card titles and the "Profiles (1)" heading.
- **display-compressed**: a narrow condensed face. Used for badge text ("active", "default", "27 fields").
- **label-serif**: a small uppercase serif. Used for field labels ("PROFILE NAME", "MODEL"), config section labels, the "FILTERS" and "SECTIONS" captions, and dividers.
- **mono**: used for paths, key paths, inputs inside config, and YAML.
- **sans**: body text.

Buttons are all square-cornered (radius 0). Cards are square with a 1px border. There are no rounded corners anywhere except the
builder's step pills, which are fully rounded.

Common button looks (from the foundation):
- **primary**: fg fill, bg-coloured text, 1px bevel, 28px tall, 11px bold uppercase, letter-spacing ~0.12em.
- **outlined**: transparent, 1px fg border, fg text, otherwise the same as primary.
- **ghost/icon**: no border.
- **disabled** (any kind): muted fill (#293935 / #c5dafd) with fg text at full strength and no border. This is how the builder's Back and Next look.

## 1. profiles (Profiles page)

### Layout (1440x900)
- Shell from the foundation: header 0–52 with the border at y=52, sidebar 0–239, main from x=240.
- Header title "Profiles" at x=263, in display-expanded at ~16px with a 12px cap height, in fg.
- **Header end slot** (right, 22px from the edge, 8px gaps). Reference: `BUILD` (outlined, x1278–1339, 62×28) and `CREATE` (primary, x1348–1417, 70×28), y 12–39.
  - Ours (W52-pc-1): `IMPORT` (outlined), `BUILD` (outlined), `CREATE` (primary). Import is ours and has no reference counterpart.
- Main padding is 23px left and right and 22px top. Content runs x=263–1407 (1145 wide) and is a vertical stack.
- **Active banner**: a card at y 75–114 (40 tall, full width, 1px border, page-bg fill).
  - Inside, ~12px from the left: a check icon (14px, success colour) at x≈280, then the text "Active profile: **default**". The label is in fg-secondary at 12px and the name is in fg, medium weight.
  - Gap below: 26px.
- **Heading** "Profiles (N)" at y 141–154. It has a users icon (16px) and then the text, ~15px bold display-expanded, tracking ~0.15em, in fg-secondary. Gap to the grid: 15px.
- **Grid**: 3 columns at this width (2 below ~1280px, 1 on phones), with 12px gaps. Each card is 374 wide. The default card spans y 170–334 (165 tall).

### Card (each profile)
- 1px border, 16px padding. The rows are separated by 8px.
  1. **Header row**:
     - name, 13px medium, fg.
     - badges, 25px tall, 1px border, square, display-compressed ~13px, 6px horizontal padding, 6px gaps:
       - `active` (success tone) on the sticky profile;
       - `default` (secondary tone) on the default profile;
       - ours: `this dashboard` (outline tone: transparent bg, border colour, fg-secondary text) when `is_current`.
     - the actions trigger, flush right: a ghost icon button, 28×28, with a vertical three-dot icon (16px).
  2. Daemon line: a 6px round dot and 12px text. Grey dot and "Gateway stopped" in the reference. Running is green dot and green text.
  3. Description: 12px fg-secondary, clamped to 2 lines. When empty, the text is "No description" in italic fg-faint.
  4. Meta block, pushed to the bottom, 12px fg-secondary, line height 17px:
     - "Model: <model> (<provider>)";
     - "Skills: N";
     - the profile path in mono, truncated with an ellipsis.

### States
- Loading: a centred spinner with ~96px vertical padding. Use the foundation spinner, not a braille animation library.
- Error: a toast, plus the foundation error state in the page body.
- Empty list: a card with centred muted text ("No profiles yet").
- Inline rename (the card swaps its body):
  - an input with autofocus;
  - the name rule hint under it in muted text, turning danger when the name is invalid;
  - `SAVE` (primary, small) and `CANCEL` (ghost);
  - Enter submits and Escape cancels.

### Our data mapping (gaps)
| reference element | ours |
|---|---|
| "Active profile" banner | `ProfilesResponse.active` (the sticky profile). When `current !== active`, append ` (serving <current>)` in mono muted text. That is in our words. |
| active badge | `is_sticky` |
| default badge | `is_default` |
| gateway running/stopped | **No per-profile field.** Wire to `api.status` per profile: the URL is `forProfile("/api/status", name)`. `/api/status` is a profile-scoped family. Map `daemon.state === "running"` to "Daemon running" with a green dot, and anything else to "Daemon stopped". Use one query per card. If it errors, hide the line. |
| description | `description` (we have no `description_auto`, so there is no "review" badge) |
| Model line | `model` / `provider`. When there is no model, show "Model: inherited" (keep our existing text). |
| Skills: N | **Not in ProfileSummary.** Use the count of `api.skills.list(name)` rows with `enabled`, as a lazy per-card query. If it errors, hide the line. |
| path | **Not provided** by `/api/profiles`. Omit the line. Follow-up: the backend adds `path` to `_row` in `routes/profiles.py`. Do not invent it on the client. |
| has persona | Ours. Not shown on the card any more. The persona editor shows it. |

## 2. profile-actions (card actions menu)

### Look
- Opens under the trigger, aligned to the trigger's right edge with a 4px gap.
- Measured box: x 421–620 (200 wide, min-width 200), y 218–384. Card-colour fill, 1px menu border, large soft drop shadow, square corners.
- Items:
  - full width, 30px tall, 12px horizontal padding, 10px gap between icon and text;
  - text 12px uppercase, tracking ~0.08em, fg;
  - icons 16px in fg;
  - hover: muted fill at about 50%;
  - disabled: 40% opacity.
- A long label wraps onto 2 centred lines in the reference ("MANAGE SKILLS & TOOLS"). We accept a wrap. Do not truncate.
- Closes on an outside mousedown, on Escape (ours: add Escape for accessibility), and after any item runs.
- Only one menu is open at a time.
- a11y: the trigger has `aria-haspopup="menu"`, `aria-expanded` and the label "Actions". The panel has `role="menu"` and the items have `role="menuitem"`.

### Items (reference order) mapped to ours
1. `SET AS ACTIVE` (check icon). Hidden when the profile is already sticky. Calls `api.profile.setSticky(name)`. While the call is pending the item is disabled. Shows a success toast (keep our toast text).
2. `CHANGE MODEL` (CPU-chip icon). Opens the **editor dialog**, model kind.
3. `EDIT DESCRIPTION` (left-align lines icon). Opens the editor dialog, description kind.
4. `EDIT PERSONA` (a bold letter "P" in a 16px slot, standing in for the reference's lettered glyph). Opens the editor dialog, persona kind. The reference's item names its own persona file; ours is PERSONA.md.
5. `MANAGE SKILLS & TOOLS` (package/box icon). Navigates to `/skills?profile=<name>`, which is our ProfileScope search param.
6. `COPY CLI COMMAND` (terminal-prompt icon). Copies `indus -p <name>` to the clipboard. This is client-side only, because `-p/--profile` exists in our CLI. Toast "Command copied" with the command; on failure, a danger toast.
7. Ours: `EXPORT` (download icon). Runs `api.profile.exportArchive`. The ActionLog and the archive line then appear at the bottom of the card, as now.
8. `RENAME` (pencil icon). Has a 1px divider above it. Hidden for the default profile. For `is_current` it is disabled, with our locked reason as its `title`. It starts the inline rename.
9. `DELETE` (trash icon, danger text, danger hover tint). Hidden for the default profile, disabled for `is_current`. It opens our existing typed-name ConfirmDialog, restyled by the foundation. **Keep the type-to-confirm behaviour.**

The reference has a **single editor dialog**, used for model, description and persona:
- Backdrop: page bg at 85%. Panel: max-width ~512px, card fill, 1px border, large shadow, square corners. There is a close X icon button at the top right.
- Header: 20px padding, 12px bottom, with a bottom border. It holds the title in display-expanded ~15px with tracking, then " · <name>" in muted text.
- Body: 20px padding with 16px gaps.
- Kinds:
  - **Model** (ours): two controls on one row.
    - A provider `<select>` from `api.model.options().providers` (label, plus " (no key yet)" when not configured).
    - A model text input. Our options have **no per-provider model list**, so this cannot be a single flat "Provider · model" select.
    - `SAVE` (primary, small, right-aligned). Disabled until both are set. Calls `api.profile.setModel`.
  - **Description**:
    - a label-serif label;
    - a textarea, min height 96;
    - `SAVE` on the right.
    - The reference has an "Auto-generate" ghost button. **We have no describe endpoint, so omit it.**
  - **Persona**:
    - a label-serif label "Persona (PERSONA.md)";
    - a mono textarea, min height 280. It is disabled while the content loads from `api.profile.persona`;
    - `SAVE`, which calls `setPersona`.
- Choosing the same action again for the dialog that is already open closes it.
- Our current single "Edit" modal with three save buttons goes away (W52-pc-2).

## 3. CREATE (quick-create modal) and IMPORT (ours)

The CREATE modal follows the reference's look. Our words go in every sentence.
- Panel: max-width ~448px, with the same frame as the editor dialog. Title "New profile".
- Fields, with 16px gaps and label-serif labels:
  - Name: placeholder "coder", autofocus, Enter submits, `aria-invalid` when the rule fails. Under it, the rule hint in 12px muted text. Use `profileNameProblem`.
  - Clone from: a select with "Fresh profile" plus every existing name. The default is "default", as in the reference.
  - Description (optional): a textarea with min height 64.
  - Model (optional): a provider select and a model input, as in the editor.
- Fieldset "Advanced options": a top border and 16px top padding. Its legend is label-serif and muted. It holds two checkboxes:
  - "Clone everything", which sets `clone_all`. Enabled only when cloning; at 50% when disabled.
  - "No bundled skills", which sets `no_skills`. Enabled only when **not** cloning.
- Footer: `CREATE` (primary, small, right-aligned). It shows "CREATING…" while busy.
- Data:
  - `api.profile.create({name, clone_from, clone_all, description, no_skills})`, then `setModel` when a provider and model were given.
  - If that model call fails, show a danger toast and keep the profile.
  - On success, close the modal, invalidate the list and refresh the scope.
- IMPORT (ours): the same frame, titled "Import a profile".
  - Fields: Archive path (mono) and Name (optional).
  - Footer: an `IMPORT` primary button.
  - Validation and behaviour exactly as in `ImportProfile` today.
  - Keep the labels "Archive path", "Name (optional)" and the button "Import". The tests use them.

## 4. profile-builder (`/profiles/new`)

### Layout
- Header title: the reference shows "Profiles/new". Set our route title to `Profiles/new` (W52-pc-3). It avoids repeating the H2 below.
- Centred column: the content box is x 490–1179 (690 wide). It is centred in main and has 16px padding. Vertical rhythm is 24px between blocks.
- Title row:
  - H2 "New profile": display-expanded, bold, ~40px (cap height 29), fg, at y 93–131.
  - `Cancel` on the right: a ghost button, 13px bold, wide tracking (~0.15em), not uppercase. It goes back to `/profiles` and keeps the scope search.
- **Stepper** at y 152–177. The pills are 26px tall, fully rounded, with 12px horizontal padding, 13px text and 8px gaps.
  - Current pill: fg fill with bg-coloured text.
  - Visited pills (index ≤ current): muted fill with fg text.
  - Future pills: no fill, fg-disabled text.
  - Every pill after the first is disabled (`not-allowed` cursor, 50%) while the name is invalid.
  - The reference has 5 steps: `1. Identity`, `2. Model`, `3. Skills`, `4. MCPs`, `5. Review`.
  - **Ours goes from 6 steps to 5** (W52-pc-4). Our Name and Description steps merge into Identity.
- **Step card**: y 201–368 for Identity. It has a 1px border and 20px padding (the field column runs x 510–1159). Fields are 16px apart, and each label is 6px above its input.
  - Labels are label-serif, ~11px uppercase, letter-spaced, in fg.
  - Inputs are 33px tall, full width, with a 1px border and input bg. Text is mono 13px, with the placeholder in placeholder colour.
- Nav row, 23px below the card:
  - `Back` on the left (ghost; disabled on step 1).
  - On the right, `Next`, or `Create profile` on Review.
  - Both are 37px tall, 13–14px bold, tracking ~0.15em, not uppercase.
  - In the shot both are disabled: the muted fill is #293935 / #c5dafd with fg text, and the box is 73 wide.

### Steps mapped to ours
1. **Identity**:
   - Profile name: placeholder "coder". The error appears only once the user has typed something, in 12px danger. Use `profileNameProblem`, which keeps our reserved and existing-name checks.
   - Description (optional): a single-line input with placeholder "What this profile is for". The placeholder is our words.
   - Ours: Start from, a select ("A fresh profile" / "A copy of <p>'s settings"). Keep it here as the third field so the clone behaviour stays.
2. **Model**:
   - A muted 13px intro line in our words.
   - A filter input, placeholder "Filter…".
   - A scrolling list, max ~288px tall, of full-width row buttons (13px, 12px horizontal and 8px vertical padding). Hover uses the muted fill; the selected row uses a primary tint at ~10%.
   - The first row is "Use default (set later)".
   - Ours: the following rows are **providers** from `api.model.options()`, not provider·model pairs, because there is no model list.
   - Picking a provider shows a "Model" input below the list. The existing `modelProblem` still blocks Next.
   - While loading, show muted "Loading…".
3. **Skills**:
   - A checkbox row, "Keep every bundled skill (recommended)", checked by default. While checked it hides the list.
   - When unchecked:
     - a muted 12px hint;
     - a filter input;
     - a scrolling list, max ~224px tall, of checkbox rows. Each row shows the name (medium), a category badge (secondary) and a muted description line.
     - Unticked rows become `skillsOff`.
   - Ours: a second checkbox, "Start with no bundled skills" (`no_skills`). It disables and hides the list.
   - Hub section, under a top border:
     - the label "Add from the skills hub";
     - an input and a `Search` outlined button (Enter also searches) calling `api.skills.hubSearch`;
     - result rows (name, source badge, description, and an `Add` ghost button);
     - chosen items appear as badges with a × to remove them.
   - After the profile is created, each chosen hub skill is installed as a follow-up outcome. That needs a client-only change: `api.skills.hubInstall(identifier, profile?)` with `forProfile`, because the backend skills family already honours `?profile=`.
4. **MCPs**:
   - Heading "MCP servers" (display-expanded 15px bold), a muted intro line, and "N configured" on the right in 12px muted text.
   - An "Add server" sub-panel: 1px border, faint bg, 16–20px padding. It holds:
     - Server name and Transport, in two columns. Transport is a 2-way segmented control (`HTTP/SSE` | `stdio`): bordered, with a 2px inset; the active segment is fg-filled.
     - For HTTP:
       - a URL input;
       - Authentication, a 3-way segmented control: `None` | `Header` | `OAuth`. These are our McpAuth values. There is **no token field**. Show a muted line in our words saying the header key is set on the Keys page, and for OAuth that sign-in happens on the MCP page.
     - For stdio: Command and Arguments in two columns, then Environment (a mono textarea).
   - An `Add server` primary button on the right. Keep `mcpBody` validation, and pass `auth` for HTTP.
   - Added servers are listed as rows with a border and a muted fill, 16px padding. Each row shows the name, a transport badge, an auth badge, and the URL or command in a muted line, with a `Remove` ghost danger button.
   - Keep the aria-label "Servers to add".
5. **Review**:
   - Label/value rows, 13px. The label column is 96px wide and muted.
   - Rows: Name, Description ("—" when empty), Starts from (ours), Model ("Default (set later)" or "<model> (<provider>)"), Skills, Hub skills (when there are any), MCP servers ("None" or the names).
   - Then `Create profile` (primary). It shows "Creating…" while busy.
- Keep our outcome screen after Create, with the results list and "Back to profiles" / "Change and retry". Style it with a card and a primary link button.
  - The reference instead goes back to /profiles with a toast. Ours keeps the per-follow-up results (W52-pc-5).

## 5. config (Config page, form view)

### Header
- Title "Config".
- End slot: a **search input**, x 1118–1417 (300 wide), y 11–40 (30 tall). It has a 1px border and input bg, a search icon (14px muted) inset 10px, 12px text with 32px left padding, and the placeholder "Search...".
- A clear × ghost button appears while there is text.
- The search input replaces our current header buttons, which move into the toolbar (W52-pc-6).

### Toolbar row (y 75–113)
- On the left: a sliders icon (16px muted) at x≈270, then the config strip.
  - The strip is a mono 13px code box with card-fill bg and 2px/8px padding (x 285–1144, wraps to 2 lines). Its text is fg-secondary.
  - Reference: the config.yaml path. **We have no path API.** Show `config.yaml · <profile>` instead, taking the profile from ProfileScope (selected, else current). Follow-up: expose the path from `/api/config` or `/api/status`.
- On the right, 6px gaps:
  - ghost icon buttons, 34px pitch: download (Export JSON, aria-label "Export JSON"), upload (a label wrapping the hidden file input, aria-label "Import JSON"), and reset (rotate-left icon; form mode only);
  - a 1px × 20px divider in the border colour;
  - `YAML` with a `</>` code icon prefix: outlined in form mode, primary in YAML mode, where its label becomes `Form` with a form-input icon. It is 73×27.
  - `SAVE`, primary, 55×27.
- Ours: also `EFFECTIVE`, an outlined toggle placed after YAML. It shows our effective table (see 7). Keep the accessible name "Effective", which the tests and capture kit use.
- Reset:
  - Scope: the active category (or the search results).
  - It opens a destructive ConfirmDialog, titled in our words, with the field count, and the confirm button "Reset to defaults".
  - On confirm it sets each scoped key to its value from **`/api/config/defaults`**. The endpoint exists; add the client method `api.config.defaults()`. It marks the form dirty, then shows a toast.
  - It replaces our "Reset" (revert unsaved) button (W52-pc-7).
- Save:
  - Keep the dirty gating, so when nothing has changed SAVE shows the disabled muted fill. The reason: saving the full form tree when nothing changed could write defaults into the file.
  - Keep the toast `Saved. ${APPLY_NOTE}`.
  - Drop the always-on APPLY_NOTE Notice box (W52-pc-8). The note stays in the save toast.

### Body (form view): two columns, 16px gap, starting at y=129
- **Filter column**: x 263–472 (210 wide), sticky at top 16px. Panel bg and border.
  - A header row, 31px tall, with a bottom border: a funnel icon (12px tertiary) and "FILTERS" in label-serif 11px, tracking 0.12em, fg-secondary.
  - The caption "SECTIONS": label-serif 11px tertiary, 12px horizontal padding, 8px top padding.
  - The list has 8px padding and a 1px gap between items. It scrolls, with max height viewport − 260px.
  - Items:
    - 23px tall, 8px horizontal padding, square;
    - a category icon (14px), then the name (mono 12px, truncated), then the count right-aligned (12px, tabular figures);
    - inactive: name fg-secondary, count tertiary;
    - active: list-item-active bg, name fg, count secondary;
    - hover: muted tint.
  - Clicking an item clears the search and selects that category. On phones it becomes a horizontal scrolling strip.
- **Section card**: x 488–1407.
  - Header, 48px tall with a bottom border: the category icon (16px) and the category name in display-expanded ~15px uppercase, letter-spaced. On the right, a secondary badge "N fields", 25px tall, in display-compressed.
  - Body: 16px horizontal and 16px bottom padding, with 8px between fields.
- **Field** (by type):
  - Label: label-serif uppercase, ~11px, tracking 0.08em, fg. It is the last key segment with `_` turned into spaces.
  - Hint lines, 2px apart:
    - the key path in mono 12px tertiary, shown when the key has a dot. In ours every key does.
    - the description in 12px fg-secondary.
  - Text, number and enum controls: 34px tall, full width, 1px border, input bg, mono 13px text with 12px padding.
  - **Boolean**: label and hints on the left, the switch on the right.
    - The switch is 34×19 and square, with a muted-fill track and a 1px border (#696c61 / #89b0fd).
    - The knob is 13×13 in fg, on the right when on and the left when off, with no colour change.
  - **List** of scalars: one input, comma-separated, with the placeholder "comma-separated values".
  - Ours: a list that holds objects, such as `fallback_providers`, keeps a mono textarea with one JSON value per line.
  - Spacing: field blocks are ~94px apart, from label top to label top, for text fields.
- **Sub-section divider**: when a key has 3+ segments and its second segment changes, insert a row.
  - The row holds the second segment in label-serif uppercase 11px semibold fg-secondary, then a 1px line that fills the rest of the width. It has 16px top and 8px bottom padding.
  - Reference: the divider marks the first segment when it differs from the category. In ours the category *is* the first segment, so use the second (W52-pc-9).
- **Search mode**:
  - The card title becomes "Search results", with a search icon and the count badge.
  - Fields are grouped under a category divider: the category icon plus the name, a semibold label-serif caption, and a line.
  - When nothing matches: a centred muted line "No fields match '<q>'". That is a short label.
  - Matching covers the key, the human label, the category and the description.
- Loading: a centred spinner, py ~96px.
- Error: the foundation error state.
- Category names: title-case with `_` turned into spaces. Our own short names are fine ("Text-to-speech" etc. are optional).
- Icons: lucide by category:
  - general: settings
  - agent: bot
  - terminal: monitor
  - display: palette
  - delegation: users
  - memory: brain
  - compression: package
  - security: lock
  - browser: globe
  - voice: mic
  - tts: volume
  - stt: ear
  - logging: clipboard list
  - discord: message circle
  - auxiliary: wrench
  - anything else: file-question.
  - Map our own category ids, such as model, providers, fallback, gateway and skills, to suitable lucide icons, with file-question as the fallback.

## 6. config-yaml (raw YAML view)

- Toolbar: same as form mode, minus the reset icon. The `Form` button is filled.
- Card: x 263–1407, y 129–772.
  - Header 41px tall: a file-text icon (16px) and "RAW YAML CONFIGURATION" in display-expanded ~15px uppercase.
  - Then a 1px rule.
  - Body: a borderless textarea, min height 600, mono 13px, line height 21px, 16px/12px padding, 2 spaces of indent per level (16px), resizable in the bottom-right corner, with `spellcheck` off.
- Ours: **there is no raw-YAML endpoint**, so the view is read-only (W52-pc-10).
  - Build the text from `api.config.effective()` entries whose `source === "file"`: nest them by dotted key, then emit YAML with a small serializer of our own (`lib/yaml.ts`).
    - It handles maps, lists, strings (quote them when needed), numbers, booleans and null.
    - Write it ourselves. Do not add a YAML package just for this.
  - Values are already redacted server-side.
  - The textarea is `readOnly`. In YAML mode SAVE is disabled, with a title in our words saying that raw editing is not available yet and the form should be used.
  - If the file has no keys, show a muted "# config.yaml has no settings yet" comment line. That is our words.
  - Loading: a spinner, py 48px.
  - Follow-up: the backend adds `GET/PUT /api/config/raw` (text plus path). Then make the view editable.

## 7. Ours with no reference counterpart: the Effective view

- Keep `EffectiveView` and its table (aria-label "Effective configuration", rows with `data-testid="effective-<key>"`). Re-skin it:
  - Header cells: label-serif 11px uppercase, tertiary.
  - Rows: a 1px top border and 8px vertical padding. Key and value are in mono 12px.
  - The source badge column is right-aligned.
- **Fix the bug** where the "config file" pill wraps: give the badge `whitespace-nowrap` and the column `w-px whitespace-nowrap`.
- Badge tones:
  - default: secondary
  - file: primary-outline
  - env: warning
  - overlay: danger.

## 8. Files to change (ours)

- `apps/web/src/pages/ProfilesPage.tsx`: rewrite. Split it into:
  - `pages/profiles/ProfileCard.tsx`;
  - `ProfileActionsMenu.tsx`: a generic, foundation-styled dropdown menu if the foundation has none; otherwise use the foundation's;
  - `ProfileEditorDialog.tsx`;
  - `CreateProfileDialog.tsx`;
  - `ImportProfileDialog.tsx`.
- `apps/web/src/pages/ProfileBuilderPage.tsx`: change to the 5 steps, with the new step bodies. `STEPS` is exported, and the test imports it.
- `apps/web/src/pages/ConfigPage.tsx`: rewrite. Split it into:
  - `pages/config/FilterColumn.tsx`;
  - `ConfigField.tsx` (replaces `FieldControl`);
  - `YamlView.tsx`;
  - `EffectiveView.tsx`.
- `apps/web/src/lib/api.ts`:
  - add `config.defaults()` (GET `/api/config/defaults`, public, so no profile);
  - add the optional `profile` argument to `skills.hubInstall` (via `forProfile`);
  - wire per-profile status through `forProfile("/api/status", name)` (a new `statusOf(name)` helper or an optional argument).
- `apps/web/src/lib/yaml.ts` (new) plus `yaml.test.ts`.
- `apps/web/src/shell/routes.ts`: change the builder title to `Profiles/new`.
- Tests that pin markup or text we change deliberately. Update them, and keep their intent:
  - `ProfilesPage.test.tsx`:
    - The "Make sticky", "Edit", "Rename", "Export" and "Delete" buttons become menu items. Open the menu with the "Actions" button, then use `getByRole("menuitem", {name: /set as active/i})` and so on.
    - "Save model", "Save description" and "Save persona" become the `SAVE` button of each editor dialog.
    - Import is now reached through the IMPORT header button.
    - The New-profile link becomes the BUILD button (role button or link, to `/profiles/new`).
    - Check that the test still asserts: Rename and Delete are absent for default and disabled for `is_current`.
  - `ProfileBuilderPage.test.tsx`:
    - "6. Review" becomes "5. Review". The Description field is now on step 1.
    - The Provider select becomes provider row buttons plus a Model input.
    - The skills radios become checkboxes.
    - The Transport select becomes a segmented group (`getByRole("group", {name: "Transport"})`).
    - "Add to the list" becomes "Add server". "Drop" becomes "Remove".
  - `ConfigPage.test.tsx`:
    - Headings are now one card per category. Assert the filter list instead (`getByRole("button", {name: /Agent/})`).
    - Fields in other categories need a click on that category first. Keep `aria-label=<full key>` on every control.
    - Reset becomes defaults with a confirm. The new test mocks `/api/config/defaults`.
    - APPLY_NOTE: assert it in the save toast.
    - "Import JSON" stays as the file input's label.
    - Add tests for search, the YAML view (read-only, built from the file-sourced effective entries) and the reset scope.
- `apps/web/e2e/dashboard.spec.ts` (Config save):
  - `agent.max_turns` is no longer on the first card. Click the "Agent" category before filling the field.
  - After the reload, click "Agent" again.
- Capture kit: copy it into your own `$S`. Its profiles, builder and config steps change:
  - Edit becomes Actions → menu, and add a `profile-actions` shot with the menu open.
  - The builder needs one Next to reach Model, then 3 more to reach Review.
  - Add the `config-yaml` shot, taken after clicking `YAML`.
  - The Effective button is unchanged.

## 9. Build order

1. Check that the foundation tokens and components match §0, then add the menu, segmented, switch and badge-tone primitives the foundation lacks. Ask the foundation note first.
2. API additions: `config.defaults`, `hubInstall` with a profile, and per-profile status. Add `lib/yaml.ts` with its test.
3. Config page: toolbar, filter column, section card and fields, search, reset, YAML, Effective. Then the tests, then the e2e change.
4. Profiles page: banner, heading, grid, card, actions menu, editor dialog, rename, delete, CREATE and IMPORT modals. Then the tests.
5. Profile builder: 5 steps, stepper and nav look, steps 2–4 rebuilt. Then the tests.
6. Re-capture both themes with the copied kit and build the compare PDF. Close the gaps.

## 10. Compare checklist (tick against the Hermes shot, both themes)

profiles
- [ ] header: IMPORT, BUILD (outlined) and CREATE (primary, bevelled), 28px tall, 22px from the right
- [ ] the active banner is 40px tall with a green check and the "Active profile: <name>"
- [ ] the "Profiles (N)" heading has the users icon, wide tracking and secondary colour
- [ ] 3-column grid with 12px gaps; square cards, 1px border, 16px padding
- [ ] badges are square, 25px tall, condensed text; active is green and default is secondary
- [ ] the daemon dot line, italic faint "No description", and the 12px meta block pinned to the bottom
- [ ] the three-dot trigger is flush top-right
- [ ] loading spinner, empty card, error state

profile-actions
- [ ] 200px menu with card fill, border and shadow, aligned to the trigger's right edge, 4px below it
- [ ] 30px uppercase 12px items with 16px icons, in the order set-active (when not sticky), model, description, persona, skills, copy CLI, export, divider, rename, delete (danger)
- [ ] closes on an outside click, on Escape and after an item runs; one menu open at a time
- [ ] the editor dialog has a header rule, a "· name" suffix, a right-aligned SAVE, and the persona textarea in mono at 280px

profile-builder
- [ ] the page header reads "Profiles/new"
- [ ] centred 690px column, ~40px bold "New profile", tracked `Cancel` ghost on the right
- [ ] 5 rounded pills (current: fg fill; visited: muted; future: faint), disabled while the name is invalid
- [ ] the card has 20px padding, label-serif uppercase labels and 33px mono inputs with placeholders "coder" and the description text
- [ ] Back and Next are 37px tall, bold and tracked; disabled shows the muted fill
- [ ] steps 2–5 match §4 (filter list, skills checkbox and hub, MCP segmented controls, review rows)

config
- [ ] header search, 300×30, with the icon and "Search..."
- [ ] the toolbar has the sliders icon, the mono strip, the download, upload and reset icons, a divider, YAML (outlined) and SAVE
- [ ] the 210px filter column has the FILTERS and SECTIONS captions, 23px items with icon, name and count, and the active fill
- [ ] the section card has a 48px header with the icon, an uppercase expanded title and the "N fields" badge
- [ ] each field has a serif uppercase label, a mono key path and a secondary description, with 34px inputs
- [ ] the square 34×19 switch is right-aligned; sub-section dividers have a line
- [ ] search results group fields by category; the no-match line
- [ ] reset confirm → defaults; SAVE gated by dirty state (muted when clean)
- [ ] Effective: the source pill does not wrap

config-yaml
- [ ] the button reads `Form` and is filled; the reset icon is hidden
- [ ] the card is titled "RAW YAML CONFIGURATION", with a 600px mono 13px/21px textarea and 2-space indents
- [ ] it is read-only, shows only the file-sourced keys, and SAVE is disabled with an explaining title

## 11. Decisions (labelled for the builder's note)

- W52-pc-1: the header gets IMPORT as a third button; the import form moves into a modal.
- W52-pc-2: one editor dialog per action replaces the combined Edit modal.
- W52-pc-3: the builder's route title becomes `Profiles/new`.
- W52-pc-4: the builder has 5 steps, and Identity holds the name, the description and our "Start from".
- W52-pc-5: the builder keeps our outcome screen instead of navigating away with a toast.
- W52-pc-6: on Config, the header holds the search; Export and Import JSON become toolbar icons.
- W52-pc-7: Reset means "section to defaults" (from `/api/config/defaults`), with a confirm.
- W52-pc-8: the APPLY_NOTE notice box goes; the text lives in the save toast.
- W52-pc-9: sub-section dividers use the second key segment.
- W52-pc-10: the YAML view is read-only and built from the file-sourced effective entries.

Follow-ups (backend, not this slice):
- `path` in `/api/profiles` rows;
- a per-profile daemon state and skill count in `/api/profiles`;
- a per-provider model list in `/api/model/options`;
- `GET/PUT /api/config/raw` with the config path;
- an optional describe endpoint.
