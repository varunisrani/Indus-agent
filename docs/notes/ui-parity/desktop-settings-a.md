# Task: Desktop settings pages, slice A (W52-i) Phase: ui-parity Owner: builder w53i

Status: built (wave 53, 2026-10-24); uncommitted in the w53i worktree for the integrator. Follow-ups at the end.

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): none directly in this wave; the spec
`.agents/wip/w52/specs/desktop-settings-a.md` carries the measured look (it was written from the
reference screenshots `settings-models`, `-models-fallback`, `-models-auxiliary`, `-models-moa`,
`settings-chat`, `settings-general`, `settings-appearance`, `-appearance-typography`,
`-appearance-window-and-layout`, `-appearance-chat-display`, `-appearance-pet`,
`settings-workspace-{projects-and-discovery,shell-environment,files-and-execution}`,
`settings-safety-{approvals,privacy-and-network,checkpoints}`,
`settings-browser-{browser-profile,local-and-private-urls}`,
`settings-passwords-{saved-credentials,password-managers}`, light and dark, and from the reference's
settings shell for structure only). Screenshots are not available in this container.
Evidence: none (spec numbers only).

What it does (own words): the Settings overlay's first seven groups get the reference's page
layout on the W51-b foundation: Model (main, fallbacks, side tasks, mixture of agents), Chat
(behaviour), Appearance (general, theme gallery, typography, window and layout, chat display, pet),
Workspace (projects and discovery, shell environment, files and execution), Safety (approvals,
privacy and network, checkpoints) and two new groups, Browser (browser profile, local and private
URLs) and Passwords & Logins (saved credentials, password managers). Rows are a title, a caption and
a right-aligned control; lists are one comma-separated line; the rail shows an icon per child page
and a footer of three icon buttons. Behaviour and data stay ours: schema rows still write one field
per change with `PUT /api/config`; hand-built rows call the REST routes, the gateway (`pet.*`) and the
bridge (zoom, prefs, fs limits) they called before.

Inputs → outputs: `GET /api/config/schema`, `GET /api/config`, `/api/model/options`,
`/api/model/auxiliary`, `POST /api/model/set`, gateway `model.options`, `pet.list/select/disable`,
bridge `zoom`, `prefs`, `fs`, the theme store and the project store → the pages; edits →
`PUT /api/config` (one field, or the whole list/object for fallbacks, MoA presets, vault sources).

Files: `apps/desktop/src/settings/{pages.ts, navMeta.tsx, SettingsOverlay.tsx, context.tsx,
schemaPage.tsx, search.ts, customPages.tsx, strings.en.ts}` (shared, additive blocks),
new `settings/{fieldCopyA.ts, stringsA.en.ts, pagesA.tsx, modelPages.tsx, appearancePages.tsx,
workspacePages.tsx, settingsA.css}`, `i18n/en.ts` (one spread line), tests.

Edge cases / failure modes:
- schema or config not readable: skeleton rows while loading, a danger caption + Retry on failure;
- a managed config: schema rows disabled with the backend's banner (unchanged);
- options call failed: the model select becomes a free-text field;
- no gateway connection (pet, model list): empty state / free text, never a fake list;
- old links: `view=workspace&page=desktop` lands on Projects & discovery.

Our design differences / decisions: see "Decisions" below (W52-dsa-n from the spec, as built).

Tests we will write: rail order and the new views; `page=desktop` alias; comma list parse; field
label overrides; approvals order; aux row status text; MoA edit writes the whole object; pet
list/select/disable calls; vault-sources switch edits the list; placeholder rows disabled.

Compare checklist (spec §6; ticked after capture, both themes):
- [x] Shell: card inset 48px, border colour, backdrop dim; rail 208px colours; pill on the top edge;
      X at top right; no h2; breadcrumb 12px with chevrons.
- [x] Rail: items in order, icons, chevrons, 8px spacers before Providers/Plugins/About, active
      group box + border, active child soft fill, footer three icons; scroll + truncation with
      Appearance open.
- [x] settings-models: provider/model 288px selects, Apply blue at right (dark: dark text), "Defaults",
      Reasoning, context window input; no mono key lines.
- [x] settings-models-fallback: title, caption, empty line, "+ Add fallback".
- [x] settings-models-auxiliary: "Reset all to main", intro, rows with pill + mono status, Set to
      main / Change.
- [x] settings-models-moa: preset select, toolbar, Default line, reference blocks with right switches,
      Add reference model, Aggregator block (Enabled chip and Delete disabled: W52-dsa-7).
- [x] settings-chat: Timezone/Reasoning blocks rows (+ busy mode).
- [x] settings-general: Language (disabled) + five switches (placeholders disabled).
- [x] settings-appearance: mode segmented with icons, borderless search, 3-column card grid, selected ring.
- [x] settings-appearance-typography: two 6-option segmented rows, font comboboxes + previews.
- [x] settings-appearance-window-and-layout: 4 segmented rows + 4 switches.
- [x] settings-appearance-chat-display: slider, segmented rows, switches (placeholders).
- [x] settings-appearance-pet: intro, switch row, search, 3-col tile grid, footnote.
- [x] settings-workspace-projects-and-discovery: reference rows (+ ours); `page=desktop` lands here.
- [x] settings-workspace-shell-environment: 2 rows only.
- [x] settings-workspace-files-and-execution: mode select + read limit (+ ours).
- [x] settings-safety-approvals: 4 rows first, in order.
- [x] settings-safety-privacy-and-network: redact + private URLs (stats rows deliberately absent).
- [x] settings-safety-checkpoints: single switch row (details folded).
- [x] settings-browser-browser-profile: long caption + switch.
- [x] settings-browser-local-and-private-urls: two title-only switch rows.
- [x] settings-passwords-saved-credentials: + Add (disabled), intro, centred empty state.
- [x] settings-passwords-password-managers: intro, 1Password/Bitwarden rows with pills (+ ours).
- Deliberate differences: no Billing item (W52-dsa-2); no usage-stats rows on Privacy (W52-dsa-12);
  placeholder rows rendered disabled with "Not available in this build yet." (General, Window & layout,
  Chat display, main-model Reasoning, MoA Enabled/Delete, rail footer); Saved credentials is an empty
  state only (W52-dsa-15); our preset names and descriptions; our extra rows below the reference rows;
  "Behaviour" (en-GB); search pill without the Ctrl K caps (our overlay search, not a palette); side
  tasks in backend order; option labels capitalised from the stored value ("manual" → "Manual");
  checkpoint details and the other browser.* fields behind "More settings" (W52-dsa-13/14);
  the Local & private URLs page shows `security.allow_private_urls` with its Safety label.
- Captures: 21 slugs x light/dark at 1440x900 with the kit copy (`$S/w53i-kit/settings-a.ts`), checked
  against the spec numbers (no reference PNGs in this container).

## Decisions (W52-dsa-n as built, W52-i-n ours)
- W52-dsa-1 search pill expands into our in-overlay search; the dialog no longer auto-focuses it, so
  it stays a 150px "Search" pill until typed into (type-anywhere still reaches it).
- W52-dsa-2 Billing omitted. W52-dsa-3 rail footer: all three buttons disabled with the
  not-available tooltip (no export/import/reset wiring in this slice).
- W52-dsa-4 no key line (kept as the label's `title`; still searchable).
- W52-dsa-5 `fallback.min_switch_reset_s` shown under the fallback list.
- W52-dsa-6 side-task editor: provider/model/reasoning; base URL, timeout and fallback stay
  reachable by search (owned by the page) but are not rendered (no "More" in the editor yet).
- W52-dsa-7 MoA "Enabled" chip disabled (no preset flag); Delete disabled too: `PUT /api/config` is
  additive, so a preset cannot be removed from here.
- W52-dsa-8 Chat › Behavior has no Personality row (persona stays its own child page).
- W52-dsa-9 / -11 placeholder rows rendered disabled; Chat display kept as an all-placeholder page.
- W52-dsa-10 chat text 175%: main-side max raised to 175 (`electron/ipc/schemas.ts`).
- W52-dsa-12 no usage-stats rows. W52-dsa-13 checkpoint details folded under "More settings".
- W52-dsa-14 the other `browser.*` fields on Browser profile behind "More settings" (listed by name so
  `browser.route_private_local` and `browser.vault_sources` reach their own pages).
- W52-dsa-15 Saved credentials: empty state + disabled Add. W52-dsa-16 vault sources switches.
- W52-i-1 `terminal.backend`/`terminal.timeout` and `tool_output.max_chars` leave Workspace (slice B
  claims them, W52-B-3); until slice B lands, `tool_output.max_chars` has no page.
- W52-i-2 `default-directory` stays on Workspace › Projects (e2e deep link and lead decision);
  this conflicts with W52-B-13 (slice B moves the row to Sessions) — integrator picks one owner.
- W52-i-3 new shared hooks: `PageDef.order`; context `schema`, `focusField`, `saveField`, `savePatch`;
  `FIELD_COPY_A` labels (search uses them too); `pageIcon()` child icons; list fields as one
  comma-separated line (structured lists keep the JSON textarea).

## Follow-ups
- Rail footer export/import (`GET`/`PUT /api/config`) and a reset endpoint.
- Main-model reasoning default (wire to the effort-chip preset store), MoA preset delete (needs a
  replace/delete write), vault list/add API, password-manager detection, pet thumbnails for
  non-active pets, installed-font suggestions for the font rows.

## Integration (wave 53, settings)
Landed together with slice B (W52-j) on main + w51b + w52 g/h/m. Decision ids (§2.7 of
`docs/plan/16-what-we-change.md`): W52-dsa-1, -3, -4 → D-16.1230; W52-dsa-2 → D-16.1231;
W52-dsa-5, -6, -7 → D-16.1232; W52-dsa-8, -9, -11 → D-16.1233; W52-dsa-10 → D-16.1234;
W52-dsa-12, -13, -14 → D-16.1235; W52-dsa-15, -16 → D-16.1236; W52-i-1 → D-16.1237; W52-i-2 →
D-16.1238; W52-i-3 → D-16.1239 (and D-16.1240, D-16.1230 for the parts merged with slice B).
Merged at integration: `fieldCopyA.ts` folded into slice B's `settings/fieldCopy.ts` (one map, message
keys, `suggest` became `suggestions`); one `PAGE_ICONS` map in `navMeta.tsx`; `customPages.tsx` keeps
only the table and the persona page (D-16.1246).
