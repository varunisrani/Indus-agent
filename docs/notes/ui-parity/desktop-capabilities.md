# Task: Desktop capabilities, messaging and bots tab (W52-k) Phase: ui-parity Owner: builder w53k

Status: built for wave 53 (base f3f7772 = main + w51b + w52 g/h/m), left uncommitted. Partial items are
marked "partial" in the checklist and listed under Follow-ups.

Read: spec `.agents/wip/w52/specs/desktop-capabilities.md` (the measured look; reference screenshots are not
available in this container), `docs/notes/ui-parity/desktop-foundation.md`, `.agents/wip/w52/lead-decisions.md`.
Reference source not opened while writing (the spec carries the behaviour notes). Evidence: none new.

What it does (own words): the Capabilities page loses its title row and gets one header (search | centred text
tabs with counts | profile scope); Skills and Tools become list-and-detail pages with a draggable seam, a sort strip,
switch rows and a detail column; Skills gets a bottom "Skills Hub" section holding our own hub search; Connectors
becomes a card grid with dialogs; Plugins becomes a two-switch table with an install dialog. Messaging becomes a
platform rail with a detail column and a bottom action bar. The sidebar's BOTS tab shows the bot roster in place.

Files: `apps/desktop/src/app/masterDetail.tsx` + `masterDetail.css` (new shared frame), `app/capabilities/*`,
`app/messaging/*`, `app/manage/strings.en.ts`, `bots/BotsRoster.tsx`, `app/Shell.tsx` (sidebar BOTS panel).

## Decisions
- W52-k-1: the Tools tab's per-toolset switch is shown read-only (disabled, tooltip "Change in Settings"). The only
  write would replace `platform_toolsets.cli`, whose default is a bundle, with a hand-made list; that changes what
  the CLI offers in ways the switch cannot show. No backend route added.
- W52-k-2: the Messaging enable switch joins the draft and is saved with "Save changes" (one PUT with the env
  changes), instead of saving at once; it keeps today's single-save behaviour and its test.
- W52-k-3: Plugin catalog: we serve none, so the section starts collapsed with a "No catalog on this backend yet"
  note; no trash icon (no uninstall route); no plugins-folder button (no bridge call opens that folder).
- W52-k-4: Skills: Archive only for agent-made skills (learning-graph `createdBy: agent`, `DELETE
  /api/learning/nodes/skill:<name>` behind a confirm); hub skills keep "Remove" (hub uninstall). No Edit, no
  SKILL.md body (no route); the meta box shows name, category, source, created by, uses.
- W52-k-5: the BOTS sidebar tab shows the roster in the sidebar (W52-x-2: `/bots` stays for deep links and group
  rooms); "New bot" opens the Profiles overlay; "New group" is omitted (no create-room RPC); the presence ring is
  neutral (no live presence); the bell toggle is a per-window preference only (nothing reads it yet).
- W52-k-6: the composer's `/` list is fetched fresh each time (`commands.catalog`), so a skill toggle needs no
  cache invalidation.
- W52-k-7: Connectors: kind word "Local"/"Remote", letter tiles (W52-x-1), group "Configured N"; the enable switch
  stays on the card (as the spec allows for local servers) for every server.

## Compare checklist (tick per slug, light AND dark)

Captured with `$S/w53k-kit/capabilities.ts` (a trimmed copy of builder j's settings kit) into `$S/w53k-shots`
(7 slugs x 2 themes, real runtime, fresh profile: no skills, no MCP servers, no agent plugins). The reference
screenshots are not in this container; checked against the spec's numbers.

Capabilities shell
- [x] No page title; one header row at y≈56; search left (Skills/Tools only); tabs centred; scope at right only for >1 profile
- [x] Tab labels Skills / Tools / Connectors / Plugins; superscript counts on Skills and Tools; active underline
- [x] Page bg `#fefefe` / `#0d1015`; no borders between header and body; no Files/Review side pane open by default

Skills (`skills`, `capabilities`)
- [x] List column ≈490 px wide, detail centred ≤672 px; sash works and persists
- [x] "↓ Most used" strip + kebab with All switch / Disable unused
- [x] 44 px rows: name + category subtitle, ×N chip when used, 28x16 switch; selected row `#dfe6f4` / `#1b2331`
- [x] Detail: 15 px title + category pill, description, meta box (inset bg + border), footer caption bottom-right
- [x] Hub section from y≈439: primary-tint divider, "Skills Hub" + "Update installed" + "Hide the hub browser", framed box, hint line; our search works inside it
- [x] Toggle optimistic, failure flips back with toast

Tools (`tools`)
- [x] Rows with "N tools" chips, off rows dimmed with off switch style
- [x] Detail: title, description, mono tool chips; Terminal shows "Execution backend" cards with selected card + "In use"
- [x] Toolset switch behaviour per decision (config write or disabled with tooltip)

Connectors (`mcp`)
- [x] "Connectors" title + "Add your own" outline button; borderless search with hairline under it
- [x] Group header with count; 2-column 66 px cards with 36 px tile, name + kind word, description, right lane state + button/switch
- [x] Add dialog and server dialog (test, sign in, remove) work as today's inline controls did

Plugins (`capabilities-plugins`)
- [x] Blurb + "Install from Git" (filled secondary) + folder + refresh icons
- [x] Header band 28 px inset bg with Desktop / Agent in <profile> columns (144 px each)
- [ ] (partial) Rows: name, version, kind badge, bundled pill, description; switch or dash per half; Bots desktop row present and toggles the surface
- [x] Install dialog keeps the 40-char commit rule; catalog section per decision

Messaging (`messaging`)
- [x] 224 px platform rail, 24 px tiles, status dots, selected row tint
- [x] Detail header with state + Needs setup pills; Telegram Quick setup card with green Recommended pill and primary button
- [x] Uppercase tracked section titles; field rows with label, help, 32 px input + icon buttons; Add another for lists
- [x] Bottom action bar: switch + Disabled/Enabled at left, Unsaved changes + Save changes at right (disabled look when clean)
- [x] Pairing sections appear only with data; restart alert works

Bots (`bots-tab`)
- [x] SESSIONS | BOTS strip with primary underline; BOTS panel on page bg
- [x] "Refresh gateway groups", "BOTS" label with bell + plus buttons
- [x] Bot row: face avatar, ring, name, time-ago, preview; opens the forever-chat; menu holds face/upload/hide/section
- [x] Vertical "SCHEDULED JOBS" edge tab at the right edge opens routines

General
- [x] No reference names/logos/assets; letter tiles or approved icon lib only; lucide icons
- [x] Existing behaviours preserved (confirm-before-install, masked credentials, restart banner)
- [ ] (partial) Re-captured with the fixed kit slugs; `$S/compare.pdf` reviewed page by page


Notes on the partial rows:
- Plugins: the Bots surface is not a row of `desktopPluginSource` (the contribution registry's bundled surfaces are
  not in the plugin loader's inventory), so the Desktop column shows only loader plugins; the Desktop switch is wired
  through an optional `DesktopPluginSource.setEnabled` that `contrib/host.ts` does not implement yet (read-only).
- Kit: no side-by-side PDF (no reference captures here); the pages were checked one by one against the spec.

## Integration (wave 53, desktop k + l)
Merged with slice l on f3f7772. Ids: W52-k-1 … W52-k-7 = D-16.1250 … D-16.1256; integration D-16.1268 (one
control kit) and D-16.1269 (live Desktop switch, singular counts). The small controls this slice had in
`masterDetail.tsx` (`PageSearch`, `TextTabs`, `MdPill`, `MetaBox`, `DetailHeader`, `TextAction`, `EmptyPanel`) now
come from the shared `app/kit.tsx` (`QuietSearch fill`, `TextTabs look="page"`, `PanelPill`, `MetaGrid framed`,
`DetailHead level={2}`, `TextAction`) and the foundation `EmptyState look="panel" fill`; `masterDetail.tsx` keeps
the frame (header, columns and seam, strip, switch rows, detail column, sections, bottom section). The Plugins
tab's Desktop switch is live (`desktopPluginSource.setEnabled` over the plugin loader); the Bots row is still
missing from that column (partial stays).

## Follow-ups
- Bots row in the Plugins Desktop column (the contribution registry's bundled surfaces are not in the plugin
  loader's inventory; main.tsx's BotsSurface would need to join it).
- Toolset write route (or a safe config writer for `platform_toolsets.cli`), then enable the Tools switches and "All".
- Execution backend readiness probe (Ready / Needs setup pills, warning lines) — no backend data today.
- Skill catalog list, SKILL.md read/edit routes (Edit pane, body box), plugin uninstall route, plugin catalog.
- Live bot presence; activity toasts reading the bell preference; a create-room RPC for "New group".
- Messaging per-platform docs URLs (the backend sends none; the link renders only when `docs_url` is present).
